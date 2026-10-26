"""Choose the tests a push runs, from what it changed.

    python select_tests.py --config ci/selection.toml --base <sha> [--head <sha>] [--scope auto]

The answer is one of three scopes:

``none``       Nothing a Python test can see changed (the website, prose).
``essential``  The repository's fixed fast set, plus the test files that refer
               to what changed, capped by predicted time.
``core``       ``essential`` plus the configured ``core`` set: the change was
               too broad to map (a conftest, a shared helper, the lock file,
               a module no test names).

"Refers to" is textual and deliberately generous. For a changed
``pkg/sub/mod.py`` a test file is selected when it says ``pkg.sub.mod`` (or
``alias.sub.mod``), ``from pkg.sub import ... mod``, ``from pkg.sub.mod import``,
or is called ``test_mod.py``. A changed private ``_helper.py`` is followed one
step to the modules that import it. A changed test file selects itself. A
``[map]`` table in the config covers what text cannot see.

The time cap uses the durations file: mapped files are added cheapest first
until ``budget_seconds`` is spent; the rest are listed as deferred, and run in
the weekly full suite. Changed test files are never deferred.

Writes ``scope``, ``tests``, ``deferred``, ``flags``, ``reason`` and ``changed`` to
``$GITHUB_OUTPUT`` and a table to ``$GITHUB_STEP_SUMMARY`` when those exist,
and prints the selected files one per line, so it also works locally::

    python select_tests.py --config ci/selection.toml --base origin/main | xargs pytest
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import statistics
import subprocess
import sys
import tomllib
from collections import defaultdict
from pathlib import Path

ZERO = "0" * 40


def git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True).stdout


def changedFiles(base: str, head: str) -> list[tuple[str, str]] | None:
    """(status letter, path) per changed file, or None when there is no usable base."""
    if not base or base == ZERO:
        return None
    try:
        git("cat-file", "-e", f"{base}^{{commit}}")
        out = git("diff", "--name-status", "-M", f"{base}...{head}")
    except subprocess.CalledProcessError:
        return None
    changes = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2:
            changes.append((parts[0][0], parts[-1]))
    return changes


def match(path: str, patterns) -> bool:
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


class Selector:
    def __init__(self, config: dict, root: Path):
        self.cfg = config
        self.root = root
        self.package = config["package"]
        self.names = [self.package, *config.get("aliases", [])]
        self.testRoots = config.get("test_roots", ["tests"])
        self.paths = config.get("paths", {})
        self._texts: dict[str, str] | None = None
        self._files: list[str] | None = None
        self._sources: dict[str, str] | None = None
        self._seen: dict[tuple[str, int], set[str]] = {}

    # -- the test files and their text, read once ---------------------------------

    def testFiles(self) -> list[str]:
        if self._files is None:
            self._files = sorted(
                path.relative_to(self.root).as_posix()
                for top in self.testRoots
                for path in (self.root / top).rglob("*.py")
                if path.name.startswith("test_") or path.name.endswith("_test.py")
            )
        return self._files

    def sources(self) -> dict[str, str]:
        """The package's own modules, for following a private helper to its importers."""
        if self._sources is None:
            self._sources = {
                p.relative_to(self.root).as_posix(): p.read_text(encoding="utf-8", errors="replace")
                for p in (self.root / self.package).rglob("*.py")
            }
        return self._sources

    def texts(self) -> dict[str, str]:
        if self._texts is None:
            self._texts = {f: (self.root / f).read_text(encoding="utf-8", errors="replace")
                           for f in self.testFiles()}
        return self._texts

    def expand(self, patterns) -> set[str]:
        """Test files named by a list of paths or globs."""
        found = set()
        for pattern in patterns:
            if any(ch in pattern for ch in "*?["):
                found |= {f for f in self.testFiles() if fnmatch.fnmatchcase(f, pattern)}
            elif (self.root / pattern).exists():
                found.add(pattern)
        return found

    # -- mapping one changed file to test files -----------------------------------

    def references(self, path: str, depth: int = 0) -> set[str]:
        key = (path, depth)
        if key not in self._seen:
            self._seen[key] = set()  # an import cycle ends here
            self._seen[key] = self._references(path, depth)
        return self._seen[key]

    def _references(self, path: str, depth: int) -> set[str]:
        parts = Path(path).with_suffix("").parts
        if parts[0] != self.package:
            stem = Path(path).stem
            pattern = re.compile(rf"\b{re.escape(stem)}\b")
            return {f for f, text in self.texts().items() if pattern.search(text)}
        inner = list(parts[1:])
        if inner and inner[-1] == "__init__":
            inner = inner[:-1]
        if not inner:
            return set()  # the package's own __init__: no mapping, the caller widens
        stem = inner[-1]
        if stem.startswith("_") and depth < 2:
            # A private helper: follow it to the modules that import it.
            word = re.compile(rf"^\s*(?:from|import)\s[^\n]*\b{re.escape(stem)}\b", re.MULTILINE)
            importers = [p for p, text in self.sources().items() if p != path and word.search(text)]
            found = set()
            for importer in importers:
                found |= self.references(importer, depth + 1)
            found |= self._direct(inner, stem)
            return found
        return self._direct(inner, stem)

    def _direct(self, inner: list[str], stem: str) -> set[str]:
        dotted = r"\.".join(map(re.escape, inner))
        parent = r"\.".join(map(re.escape, inner[:-1]))
        names = "|".join(map(re.escape, self.names))
        pkg = re.escape(self.package)
        patterns = [
            rf"\b(?:{names})\.{dotted}\b",
            rf"(?:from|import)\s+{pkg}\.{dotted}\b",
            rf"from\s+{pkg}{r'.' + parent if parent else ''}\s+import\s+[^\n]*\b{re.escape(stem)}\b",
        ]
        regex = re.compile("|".join(f"(?:{p})" for p in patterns))
        found = {f for f, text in self.texts().items() if regex.search(text)}
        lowered = stem.lower().lstrip("_")
        found |= {f for f in self.texts() if Path(f).stem.lower() in (f"test_{lowered}", f"{lowered}_test")}
        return found

    # -- the whole change --------------------------------------------------------------

    def select(self, changes, scope: str) -> dict:
        essential = self.expand(self.cfg.get("essential", []))
        core = self.expand(self.cfg.get("core", []))
        flags = set()
        mapped: dict[str, set[str]] = defaultdict(set)  # test file -> why
        pinned: set[str] = set()
        widen: list[str] = []
        relevant = False
        for status, path in changes or []:
            for flag, patterns in self.cfg.get("flags", {}).items():
                if match(path, patterns):
                    flags.add(flag)
            if match(path, self.paths.get("none", [])):
                continue
            relevant = True
            if match(path, self.paths.get("core", [])):
                widen.append(path)
                continue
            if any(path.startswith(f"{top}/") for top in self.testRoots) and path in self.texts():
                pinned.add(path)
                continue
            hits = set()
            for pattern, targets in self.cfg.get("map", {}).items():
                if fnmatch.fnmatchcase(path, pattern):
                    hits |= self.expand(targets)
            if path.endswith(".py") and status != "D":
                hits |= self.references(path)
            if path.endswith(".py") and path.startswith(f"{self.package}/") and (status == "D" or not hits):
                widen.append(path)
            for hit in hits:
                mapped[hit].add(path)

        if scope == "auto":
            if changes is None:
                scope, reason = "core", "no base commit to compare with"
            elif not relevant:
                scope, reason = "none", "only files no test reads changed"
            elif widen:
                scope, reason = "core", "too broad to map: " + ", ".join(widen[:5])
            else:
                scope, reason = "essential", f"{len(changes)} changed file(s) mapped"
        else:
            reason = f"requested scope {scope}"

        if scope in ("none", "full"):
            return {"scope": scope, "tests": [], "deferred": [], "flags": sorted(flags),
                    "reason": reason, "mapped": {}}
        chosen = set(essential) | pinned
        if scope == "core":
            chosen |= core
        chosen, deferred = self.budget(chosen, [f for f in mapped if f not in chosen])
        return {"scope": scope, "tests": sorted(chosen), "deferred": sorted(deferred),
                "flags": sorted(flags), "reason": reason,
                "mapped": {f: sorted(v) for f, v in mapped.items()}}

    def budget(self, chosen: set[str], candidates: list[str]) -> tuple[set[str], list[str]]:
        limit = float(self.cfg.get("budget_seconds", 240))
        times = self.fileTimes()
        fallback = statistics.median(times.values()) if times else 10.0
        spent = 0.0
        deferred = []
        for path in sorted(candidates, key=lambda f: (times.get(f, fallback), f)):
            cost = times.get(path, fallback)
            if spent + cost <= limit:
                chosen.add(path)
                spent += cost
            else:
                deferred.append(path)
        return chosen, deferred

    def fileTimes(self) -> dict[str, float]:
        path = self.root / self.cfg.get("durations", "ci/durations.json")
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return {}
        times: dict[str, float] = defaultdict(float)
        for nodeid, seconds in data.items():
            times[nodeid.split("::", 1)[0]] += float(seconds)
        return dict(times)


def writeOutputs(result: dict, changes) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"scope={result['scope']}\n")
            fh.write(f"tests={' '.join(result['tests'])}\n")
            fh.write(f"deferred={' '.join(result['deferred'])}\n")
            fh.write(f"flags={' '.join(result['flags'])}\n")
            fh.write(f"reason={result['reason']}\n")
            fh.write(f"changed={' '.join(p for s, p in changes or [] if s != 'D')}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        lines = [f"### Test selection: `{result['scope']}`", "", result["reason"], ""]
        if result["tests"]:
            lines.append(f"<details><summary>{len(result['tests'])} test file(s)</summary>\n")
            for test in result["tests"]:
                why = result["mapped"].get(test)
                lines.append(f"- `{test}`" + (f" &larr; {', '.join(why[:3])}" if why else ""))
            lines.append("\n</details>\n")
        if result["deferred"]:
            lines.append(f"Deferred to the weekly full suite (over the time budget): "
                         f"{', '.join(f'`{d}`' for d in result['deferred'])}\n")
        if changes:
            lines.append(f"<details><summary>{len(changes)} changed file(s)</summary>\n")
            lines += [f"- {s} `{p}`" for s, p in changes[:200]]
            lines.append("\n</details>\n")
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--config", default="ci/selection.toml")
    parser.add_argument("--base", default="")
    parser.add_argument("--head", default="HEAD")
    parser.add_argument("--scope", default="auto", choices=["auto", "essential", "core", "full"])
    args = parser.parse_args()
    config = tomllib.loads(Path(args.config).read_text(encoding="utf-8"))
    changes = changedFiles(args.base, args.head) if args.scope == "auto" else []
    result = Selector(config, Path.cwd()).select(changes, args.scope)
    writeOutputs(result, changes)
    print(f"scope: {result['scope']} ({result['reason']})", file=sys.stderr)
    for test in result["tests"]:
        print(test)
    return 0


if __name__ == "__main__":
    sys.exit(main())
