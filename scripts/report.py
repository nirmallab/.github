"""One verdict for a run of many jobs: what ran, what failed, and why.

    python report.py --artifacts DIR [--optional "bench,perf"] [--durations-out DIR]

Reads every job of the current run from the Actions API (``GITHUB_TOKEN`` with
``actions: read``) and every ``status.json`` that ``run_pytest.py`` wrote
(downloaded into DIR, one directory per artifact). Each job gets one class:

``success``          finished green
``test-failure``     tests failed; the first node ids are listed
``session-timeout``  pytest stopped starting tests at its session limit
``hard-timeout``     killed: by run_pytest.py, or by the job's timeout-minutes
                     (the API says ``timed_out``; ``needs.*.result`` never does)
``setup-failure``    failed before any test ran (install, build, checkout)
``failure``          failed in a step that is not a test run
``cancelled``        cancelled after it started
``not-started``      never got a runner: billing, a self-hosted machine asleep
                     for 24 hours, a concurrency group
``skipped``          not wanted this time (its ``if:`` was false)

Then it checks that sharding lost nothing -- for every group of shards, the
tests the shards kept add up to what each collected before the split, and the
tests that ran add up to what they kept -- prints the run's estimated billed
minutes, merges the Linux durations into one file for the next run, and exits
non-zero unless every job that is not optional succeeded or was skipped.
"""

from __future__ import annotations

import argparse
import datetime
import json
import math
import os
import re
import sys
import urllib.request
from collections import defaultdict
from pathlib import Path

# Per-minute list price relative to the 2-core Linux runner ($0.006).
PRICE = [("macos", 62 / 6), ("windows", 10 / 6), ("arm", 5 / 6)]
SETUP_STEPS = re.compile(r"^(set up|run actions/checkout|run astral-sh/setup-uv|run actions/setup-|"
                         r"checkout|install|sync|build the wheel|build)", re.IGNORECASE)
SHARD = re.compile(r"\s*shard \d+/\d+$")


def api(path: str) -> dict:
    base = os.environ.get("GITHUB_API_URL", "https://api.github.com")
    request = urllib.request.Request(
        f"{base}{path}",
        headers={"Authorization": f"Bearer {os.environ['GITHUB_TOKEN']}",
                 "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def jobs() -> list[dict]:
    repo = os.environ["GITHUB_REPOSITORY"]
    run = os.environ["GITHUB_RUN_ID"]
    attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "1")
    found, page = [], 1
    while True:
        data = api(f"/repos/{repo}/actions/runs/{run}/attempts/{attempt}/jobs?per_page=100&page={page}")
        found += data.get("jobs", [])
        if len(found) >= data.get("total_count", 0) or not data.get("jobs"):
            return found
        page += 1


def parseTime(value: str | None):
    if not value:
        return None
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))


def statuses(root: Path) -> dict[str, dict]:
    found = {}
    for path in sorted(root.glob("*/status.json")):
        try:
            status = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            continue
        status["_dir"] = str(path.parent)
        found[status["label"]] = status
    return found


def statusFor(job: dict, records: dict[str, dict]) -> dict | None:
    name = job["name"]
    for label, record in records.items():
        if name == label or name.endswith(f" / {label}"):
            return record
    return None


def classify(job: dict, record: dict | None) -> str:
    conclusion = job.get("conclusion")
    ran = bool(job.get("runner_name"))
    if conclusion == "success":
        return "success"
    if conclusion == "skipped":
        return "skipped"
    if conclusion == "timed_out":
        return "hard-timeout"
    if conclusion == "cancelled":
        return "cancelled" if ran else "not-started"
    if record:
        return record["outcome"] if record["outcome"] != "success" else "failure"
    steps = job.get("steps") or []
    if not ran or not steps:
        return "not-started"
    failed = next((s["name"] for s in steps if s.get("conclusion") == "failure"), "")
    return "setup-failure" if SETUP_STEPS.match(failed) else "failure"


def billed(job: dict) -> float:
    labels = [label.lower() for label in job.get("labels") or []]
    if "self-hosted" in labels or not job.get("runner_name"):
        return 0.0
    start, end = parseTime(job.get("started_at")), parseTime(job.get("completed_at"))
    if not start or not end:
        return 0.0
    minutes = max(1, math.ceil((end - start).total_seconds() / 60))
    factor = next((f for key, f in PRICE if any(key in label for label in labels)), 1.0)
    return minutes * factor


def wall(job: dict) -> str:
    start, end = parseTime(job.get("started_at")), parseTime(job.get("completed_at"))
    return f"{(end - start).total_seconds() / 60:.1f}" if start and end else "-"


def shardChecks(records: dict[str, dict]) -> tuple[list[str], list[str]]:
    """Rows for the balance table, and errors for anything lost."""
    groups = defaultdict(list)
    for record in records.values():
        collection = record.get("collection") or {}
        if collection.get("shard"):
            groups[SHARD.sub("", record["label"])].append(record)
    rows, errors = [], []
    for group, members in sorted(groups.items()):
        count = int(members[0]["collection"]["shard"].split("/")[1])
        befores = {m["collection"]["before_split"] for m in members}
        kept = sum(m["collection"]["selected"] for m in members)
        ran = sum(m["counts"]["tests"] for m in members)
        total = max(befores)
        times = [m["seconds"] / 60 for m in members]
        rows.append(f"| {group} | {len(members)}/{count} | {min(times):.1f} | {max(times):.1f} "
                    f"| {total} | {kept} | {ran} |")
        if len(members) < count:
            errors.append(f"{group}: only {len(members)} of {count} shards reported")
        if len(befores) > 1:
            errors.append(f"{group}: shards collected different totals {sorted(befores)}")
        elif len(members) == count and kept != total:
            errors.append(f"{group}: the shards kept {kept} of {total} collected tests")
        if ran < kept:
            errors.append(f"{group}: {kept - ran} selected tests never ran (timeout or crash)")
    return rows, errors


def mergeDurations(records: dict[str, dict], out: Path) -> int:
    merged: dict[str, float] = {}
    linux = [r for r in records.values() if r.get("os") == "Linux"] or list(records.values())
    for record in linux:
        path = Path(record["_dir"], "durations.json")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for nodeid, seconds in data.items():
            merged[nodeid] = max(merged.get(nodeid, 0.0), float(seconds))
    if merged:
        out.mkdir(parents=True, exist_ok=True)
        (out / "durations.json").write_text(json.dumps(dict(sorted(merged.items())), indent=0),
                                            encoding="utf-8")
    return len(merged)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--artifacts", default="ci-status")
    parser.add_argument("--optional", default="", help="comma-separated job-name fragments")
    parser.add_argument("--durations-out", default="")
    args = parser.parse_args()
    optional = [o.strip() for o in args.optional.split(",") if o.strip()]
    try:
        private = bool(api(f"/repos/{os.environ['GITHUB_REPOSITORY']}").get("private", True))
    except Exception:
        private = True  # count the minutes rather than wrongly call them free

    records = statuses(Path(args.artifacts))
    rows, failures, totals = [], [], defaultdict(int)
    minutes = 0.0
    for job in sorted(jobs(), key=lambda j: j["name"]):
        if job.get("status") != "completed":
            continue  # this job
        record = statusFor(job, records)
        cls = classify(job, record)
        totals[cls] += 1
        cost = billed(job) if private else 0.0
        minutes += cost
        counts = record["counts"] if record else None
        runner = "self-hosted" if "self-hosted" in (job.get("labels") or []) else ", ".join(job.get("labels") or [])
        rows.append(
            f"| {job['name']} | {runner} | {cls} | "
            + (f"{counts['tests']} | {counts['failures'] + counts['errors']} | {counts['skipped']}" if counts else "- | - | -")
            + f" | {wall(job)} | {cost:.0f} |"
        )
        is_optional = any(fragment in job["name"] for fragment in optional)
        if cls not in ("success", "skipped"):
            failed = (record or {}).get("counts", {}).get("failed", [])
            failures.append((job["name"], cls, failed, is_optional))

    balance, lost = shardChecks(records)
    merged = mergeDurations(records, Path(args.durations_out)) if args.durations_out else 0
    blocking = [f for f in failures if not f[3]]
    verdict = "green" if not blocking and not lost else "red"

    lines = [f"## Run report: {verdict}", ""]
    lines.append(", ".join(f"{n} {cls}" for cls, n in sorted(totals.items())) + ".")
    lines.append(f"Estimated billed: **{minutes:.0f} Linux-equivalent minutes**." if private
                 else "Public repository: hosted minutes are free.")
    lines += ["", "| job | runner | outcome | tests | failed | skipped | wall min | billed |",
              "|---|---|---|---|---|---|---|---|", *rows, ""]
    if failures:
        lines.append("### Not green\n")
        for name, cls, failed, is_optional in failures:
            lines.append(f"- **{name}**: {cls}" + (" (optional)" if is_optional else ""))
            lines += [f"  - `{n}`" for n in failed[:10]]
            if len(failed) > 10:
                lines.append(f"  - ... and {len(failed) - 10} more")
        lines.append("")
    if balance:
        lines += ["### Shards", "", "| group | reported | fastest min | slowest min | collected | kept | ran |",
                  "|---|---|---|---|---|---|---|", *balance, ""]
    for error in lost:
        lines.append(f"- :x: {error}")
    slowest = sorted(((s, n) for r in records.values() for n, s in r.get("slowest", [])), reverse=True)[:15]
    if slowest:
        lines.append("\n<details><summary>Slowest tests in this run</summary>\n")
        lines += [f"- {s:.1f} s `{n}`" for s, n in slowest]
        lines.append("\n</details>")
    if merged:
        lines.append(f"\nDurations for {merged} tests uploaded as `ci-durations`.")
    text = "\n".join(lines) + "\n"

    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)
    for name, cls, failed, is_optional in blocking:
        print(f"::error::{name}: {cls}" + (f" -- {', '.join(failed[:3])}" if failed else ""))
    for error in lost:
        print(f"::error::{error}")
    return 0 if verdict == "green" else 1


if __name__ == "__main__":
    sys.exit(main())
