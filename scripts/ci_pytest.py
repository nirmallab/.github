"""pytest plugin for nirmallab CI: split a suite into shards, record what ran.

Loaded with ``-p ci_pytest`` by ``run_pytest.py``, which puts this directory on
``PYTHONPATH``. Nothing here is specific to one repository.

``--ci-shard K/N``
    Keep shard K of N. Whole test files are assigned, longest first, each to
    the shard with the least recorded time so far. Whole files keep a module's
    fixtures (a Node server, a large synthetic dataset) inside one shard, and
    the assignment depends only on the collected node ids and the durations
    file -- never on collection order -- so every shard, and every xdist worker
    inside a shard, computes the same split.

``--ci-durations PATH``
    Where those times come from: a JSON object of node id -> seconds, as this
    plugin writes it. A test it does not know counts as the median of the ones
    it does, so a stale file unbalances the shards but never drops a test.

``--ci-out DIR``
    ``collection.json``: how many tests the shard saw before and after the
    split, and the files it kept. The report job sums the shards against the
    total, which is how a test that no shard ran gets noticed.
    ``durations.json``: setup + call + teardown per node id for this run, which
    the weekly report merges into the next run's ``--ci-durations``.

pytest-split does the same splitting, but its ``--store-durations`` is not
safe under xdist, and its count of what it deselected is not available to a
report. This is the 100 lines that are.
"""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from pathlib import Path

import pytest


def pytest_addoption(parser):
    group = parser.getgroup("ci", "nirmallab CI sharding and reporting")
    group.addoption("--ci-shard", default=None, help="keep shard K of N, as K/N")
    group.addoption("--ci-durations", default=None, help="JSON of node id -> seconds")
    group.addoption("--ci-out", default=None, help="directory for collection.json and durations.json")


def _shard(config):
    value = config.getoption("--ci-shard")
    if not value:
        return None
    index, count = (int(part) for part in value.split("/"))
    if not 1 <= index <= count:
        raise pytest.UsageError(f"--ci-shard {value}: K must be between 1 and N")
    return index, count


def _isWorker(config) -> bool:
    return hasattr(config, "workerinput")


def _durations(config) -> dict[str, float]:
    path = config.getoption("--ci-durations")
    if not path or not Path(path).is_file():
        return {}
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {str(k): float(v) for k, v in data.items() if isinstance(v, (int, float))}


def assignFiles(nodeids, durations, count):
    """Shard number (1-based) for every test file, by least-loaded greedy fill."""
    known = [durations[n] for n in nodeids if n in durations]
    fallback = statistics.median(known) if known else 1.0
    perFile = defaultdict(float)
    for nodeid in nodeids:
        perFile[nodeid.split("::", 1)[0]] += durations.get(nodeid, fallback)
    loads = [0.0] * count
    owner = {}
    # Longest first; the file name breaks ties so the order is total.
    for path, seconds in sorted(perFile.items(), key=lambda item: (-item[1], item[0])):
        target = min(range(count), key=lambda shard: (loads[shard], shard))
        loads[target] += seconds
        owner[path] = target + 1
    return owner, loads


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    # trylast: after -m, -k and --deselect have had their say, so the count
    # before the split is the count the shards must add up to.
    before = len(items)
    shard = _shard(config)
    kept = items
    estimate = None
    if shard:
        index, count = shard
        owner, loads = assignFiles([item.nodeid for item in items], _durations(config), count)
        kept = [item for item in items if owner[item.nodeid.split("::", 1)[0]] == index]
        dropped = [item for item in items if owner[item.nodeid.split("::", 1)[0]] != index]
        if dropped:
            config.hook.pytest_deselected(items=dropped)
        items[:] = kept
        estimate = round(loads[index - 1], 1)
    out = config.getoption("--ci-out")
    # Every xdist worker collects the same thing; one of them is enough.
    if out and (not _isWorker(config) or config.workerinput.get("workerid") == "gw0"):
        Path(out).mkdir(parents=True, exist_ok=True)
        record = {
            "shard": f"{shard[0]}/{shard[1]}" if shard else None,
            "before_split": before,
            "selected": len(kept),
            "estimated_seconds": estimate,
            "files": sorted({item.nodeid.split("::", 1)[0] for item in kept}),
        }
        Path(out, "collection.json").write_text(json.dumps(record, indent=1), encoding="utf-8")


class _Recorder:
    """Durations per node id, in the process that sees every report.

    Under xdist that is the controller, which receives each worker's reports;
    without xdist it is the only process there is.
    """

    def __init__(self, out: Path):
        self.out = out
        self.seconds = defaultdict(float)

    def pytest_runtest_logreport(self, report):
        self.seconds[report.nodeid] += report.duration

    def pytest_sessionfinish(self, session):
        self.out.mkdir(parents=True, exist_ok=True)
        data = {k: round(v, 3) for k, v in sorted(self.seconds.items())}
        (self.out / "durations.json").write_text(json.dumps(data, indent=0), encoding="utf-8")


def pytest_configure(config):
    out = config.getoption("--ci-out")
    if out and not _isWorker(config):
        config.pluginmanager.register(_Recorder(Path(out)), "ci_pytest_recorder")
