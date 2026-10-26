"""Run pytest for nirmallab CI under three timeouts and say what happened.

    python run_pytest.py --out DIR --label NAME [options] -- [pytest arguments]

Run it with the project's own interpreter (``uv run --no-sync python ...``):
pytest is started as ``sys.executable -m pytest``.

Timeouts, innermost first, so that a hang ends as a failed step with its
reports written -- never as a job GitHub cancels, which uploads nothing:

* ``--test-timeout`` (pytest-timeout ``--timeout``): one test.
* ``--session-timeout`` (pytest-timeout ``--session-timeout``): pytest stops
  starting tests and writes its JUnit file. On POSIX this script also sends
  SIGINT two minutes later in case the session is stuck inside one test.
* ``--hard-timeout``: the process tree is killed. Keep it a few minutes under
  the job's ``timeout-minutes``.

Writes ``DIR/status.json`` (what the report job reads), next to the plugin's
``collection.json`` and ``durations.json`` and pytest's ``junit.xml``, and a
short table to ``$GITHUB_STEP_SUMMARY``. Exits non-zero unless every selected
test passed or skipped.

Isolation: the test process gets config, data and cache directories under
``$RUNNER_TEMP``, so a suite running on someone's own machine (a self-hosted
runner) cannot read or write their real licence, Plexora data root, telemetry
queue or Numba cache.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import os
import platform
import signal
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Forced: a value from the runner's own environment is exactly what must not leak in.
ISOLATED_DIRS = {
    "XDG_CONFIG_HOME": "config",
    "XDG_DATA_HOME": "data",
    "XDG_CACHE_HOME": "cache",
    "XDG_STATE_HOME": "state",
    "MPLCONFIGDIR": "matplotlib",
    "NUMBA_CACHE_DIR": "numba",
    "BIOCOGNIA_DIR": "biocognia",
    "SCIMAPPRO_CONFIG_DIR": "scimappro",
    "PLEXORA_DATA_PATH": "plexora",
    "SPATIAL_WORKSPACE_DIR": "workspace",
}
WINDOWS_DIRS = {"APPDATA": "appdata-roaming", "LOCALAPPDATA": "appdata-local"}
# Defaults only: a workflow may say otherwise.
DEFAULTS = {
    "BIOCOGNIA_OFFLINE": "1",
    "BIOCOGNIA_NO_HEARTBEAT": "1",
    "SCIMAPPRO_TESTING": "1",
    "PLEXORA_TELEMETRY": "off",
    "MPLBACKEND": "Agg",
    "PYTHONUTF8": "1",
    "PYTHONFAULTHANDLER": "1",
}
INTERRUPT_GRACE = 120


def has(module: str) -> bool:
    return importlib.util.find_spec(module) is not None


def version(dist: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in importlib.metadata.version(dist).split(".")[:2])
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return (0,)


def isolatedEnv() -> dict[str, str]:
    env = dict(os.environ)
    base = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()) / "ci-home"
    dirs = dict(ISOLATED_DIRS)
    if os.name == "nt":
        dirs.update(WINDOWS_DIRS)
    for var, name in dirs.items():
        path = base / name
        path.mkdir(parents=True, exist_ok=True)
        env[var] = str(path)
    for var, value in DEFAULTS.items():
        env.setdefault(var, value)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(HERE), env.get("PYTHONPATH")]))
    return env


def buildCommand(args, extra: list[str]) -> tuple[list[str], list[str]]:
    out = Path(args.out)
    cmd = [sys.executable, "-m", "pytest", "-p", "ci_pytest", f"--ci-out={out}",
           f"--junitxml={out / 'junit.xml'}", "-o", "junit_family=xunit2", "-rfE"]
    notes = []
    if args.shard:
        cmd.append(f"--ci-shard={args.shard}")
    if args.durations:
        cmd.append(f"--ci-durations={args.durations}")
    workers = (args.workers or "0").strip()
    if workers not in ("0", "1"):
        if has("xdist"):
            # loadfile: a module's tests share one worker, as they share one shard.
            cmd += ["-n", workers, "--dist", "loadfile", "--max-worker-restart", "4"]
        else:
            notes.append(f"pytest-xdist is not installed, so -n {workers} was dropped")
    if has("pytest_timeout"):
        if args.test_timeout:
            cmd.append(f"--timeout={args.test_timeout}")
        if args.session_timeout:
            if version("pytest-timeout") >= (2, 3):
                cmd.append(f"--session-timeout={args.session_timeout}")
            else:
                notes.append("pytest-timeout < 2.3 has no --session-timeout")
    else:
        notes.append("pytest-timeout is not installed: no per-test timeout")
    disabled = any(a in ("no:randomly", "-pno:randomly") for a in extra)
    if args.seed and has("pytest_randomly") and not disabled:
        # Every shard of a run must shuffle the same way, or reports differ.
        cmd.append(f"--randomly-seed={args.seed}")
    return cmd + extra, notes


def killTree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True)
    else:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def run(cmd: list[str], env: dict[str, str], session: int, hard: int) -> tuple[int, str, float]:
    """Return (exit code, how it ended, seconds)."""
    kwargs = {"env": env}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    start = time.monotonic()
    proc = subprocess.Popen(cmd, **kwargs)
    interrupted = False
    while True:
        try:
            rc = proc.wait(timeout=5)
            break
        except subprocess.TimeoutExpired:
            pass
        elapsed = time.monotonic() - start
        if hard and elapsed >= hard:
            print(f"::error::Hard timeout after {elapsed / 60:.1f} min: killing the test process tree.", flush=True)
            killTree(proc)
            proc.wait()
            return -9, "hard-timeout", time.monotonic() - start
        if session and not interrupted and os.name != "nt" and elapsed >= session + INTERRUPT_GRACE:
            print("::warning::The session timeout passed and pytest is still running: sending SIGINT.", flush=True)
            os.killpg(proc.pid, signal.SIGINT)
            interrupted = True
    elapsed = time.monotonic() - start
    if interrupted or (session and elapsed >= session and rc != 0):
        return rc, "session-timeout", elapsed
    return rc, "", elapsed


def nodeid(case: ET.Element) -> str:
    classname = case.get("classname", "")
    name = case.get("name", "")
    parts = classname.split(".")
    # tests.test_x.TestY -> tests/test_x.py::TestY
    for cut in range(len(parts), 0, -1):
        candidate = "/".join(parts[:cut]) + ".py"
        if Path(candidate).is_file():
            rest = parts[cut:]
            return "::".join([candidate, *rest, name])
    return f"{classname}::{name}"


def readJunit(path: Path) -> dict:
    counts = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0, "failed": []}
    if not path.is_file():
        return counts
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return counts
    for case in root.iter("testcase"):
        counts["tests"] += 1
        if case.find("failure") is not None:
            counts["failures"] += 1
            counts["failed"].append(nodeid(case))
        elif case.find("error") is not None:
            counts["errors"] += 1
            counts["failed"].append(nodeid(case))
        elif case.find("skipped") is not None:
            counts["skipped"] += 1
    return counts


def outcome(rc: int, ended: str, counts: dict) -> str:
    if ended:
        return ended
    if rc == 0:
        return "success"
    if rc == 1:
        return "test-failure"
    if rc == 5:
        return "no-tests"
    if counts["failures"] or counts["errors"]:
        return "test-failure"
    return "error"  # 2 interrupted, 3 internal error, 4 usage error, or killed


def readJson(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def summarise(status: dict) -> str:
    c = status["counts"]
    line = (f"| {status['label']} | {status['outcome']} | {c['tests']} | {c['failures'] + c['errors']} "
            f"| {c['skipped']} | {status['seconds'] / 60:.1f} min |")
    text = ["| job | outcome | tests | failed | skipped | time |", "|---|---|---|---|---|---|", line, ""]
    if c["failed"]:
        text.append("Failed:\n")
        text += [f"- `{n}`" for n in c["failed"][:20]]
        text.append("")
    if status["slowest"]:
        text.append("<details><summary>Slowest tests</summary>\n")
        text += [f"- {s:.1f} s `{n}`" for n, s in status["slowest"][:15]]
        text.append("\n</details>\n")
    if status["notes"]:
        text += [f"> {n}" for n in status["notes"]]
    return "\n".join(text) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--workers", default="0")
    parser.add_argument("--shard", default="")
    parser.add_argument("--durations", default="")
    parser.add_argument("--test-timeout", type=int, default=600)
    parser.add_argument("--session-timeout", type=int, default=0)
    parser.add_argument("--hard-timeout", type=int, default=0)
    parser.add_argument("--seed", default="")
    parser.add_argument("--allow-empty", action="store_true")
    args, extra = parser.parse_known_args()
    if extra[:1] == ["--"]:
        extra = extra[1:]
    if args.shard in ("1/1", ""):
        args.shard = ""
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    cmd, notes = buildCommand(args, extra)
    print("$", " ".join(cmd), flush=True)
    rc, ended, seconds = run(cmd, isolatedEnv(), args.session_timeout, args.hard_timeout)

    counts = readJunit(out / "junit.xml")
    result = outcome(rc, ended, counts)
    if result == "no-tests" and args.allow_empty:
        result = "success"
    durations = readJson(out / "durations.json") or {}
    status = {
        "label": args.label,
        "outcome": result,
        "returncode": rc,
        "seconds": round(seconds, 1),
        "counts": counts,
        "collection": readJson(out / "collection.json"),
        "slowest": sorted(durations.items(), key=lambda kv: -kv[1])[:50],
        "python": platform.python_version(),
        "os": platform.system(),
        "arch": platform.machine(),
        "runner": os.environ.get("RUNNER_ENVIRONMENT", "local"),
        "notes": notes,
    }
    (out / "status.json").write_text(json.dumps(status, indent=1), encoding="utf-8")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(summarise(status))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as fh:
            fh.write(f"outcome={result}\n")
    for note in notes:
        print(f"::warning::{note}")
    if result != "success":
        shown = ", ".join(counts["failed"][:5])
        print(f"::error::{args.label}: {result}" + (f" ({shown})" if shown else ""))
        return rc or 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
