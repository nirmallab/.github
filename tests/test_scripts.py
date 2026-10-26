"""Tests for scripts/: run with `uv run --with pytest --with pytest-xdist --with pytest-timeout pytest tests`."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import ci_pytest  # noqa: E402
import report  # noqa: E402
import select_tests  # noqa: E402


# -- ci_pytest: the split ------------------------------------------------------------------


def test_assign_files_is_order_independent_and_balanced():
    ids = [f"tests/test_{i}.py::t{j}" for i in range(7) for j in range(3)]
    durations = {nodeid: float(i + 1) for i in range(7) for nodeid in ids if f"test_{i}.py" in nodeid}
    owner, loads = ci_pytest.assignFiles(ids, durations, 3)
    owner2, _ = ci_pytest.assignFiles(list(reversed(ids)), durations, 3)
    assert owner == owner2
    assert set(owner.values()) == {1, 2, 3}
    assert max(loads) - min(loads) <= 7 * 3  # within one file of each other


def test_unknown_tests_count_as_the_median():
    owner, loads = ci_pytest.assignFiles(["a.py::x", "b.py::y"], {}, 2)
    assert sorted(owner.values()) == [1, 2]
    assert loads == [1.0, 1.0]


@pytest.fixture
def project(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    for i in range(6):
        (tests / f"test_m{i}.py").write_text(textwrap.dedent(f"""
            import pytest
            @pytest.mark.parametrize("n", range({i + 1}))
            def test_x(n):
                assert n >= 0
            @pytest.mark.slow
            def test_slow():
                pass
        """))
    (tmp_path / "pytest.ini").write_text("[pytest]\nmarkers = slow\n")
    return tmp_path


def runPytest(project: Path, out: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(SCRIPTS / "run_pytest.py"), "--out", str(out), "--label", out.name, *args],
        cwd=project, capture_output=True, text=True,
    )


@pytest.mark.parametrize("workers", ["0", "2"])
def test_shards_add_up_to_the_collection(project, workers):
    kept, ran = [], []
    for k in (1, 2, 3):
        out = project / f"out-{k}"
        result = runPytest(project, out, "--shard", f"{k}/3", "--workers", workers, "--", "-m", "not slow")
        assert result.returncode == 0, result.stdout + result.stderr
        status = json.loads((out / "status.json").read_text())
        assert status["outcome"] == "success"
        assert status["collection"]["before_split"] == 21
        kept.append(status["collection"]["selected"])
        ran.append(status["counts"]["tests"])
        assert json.loads((out / "durations.json").read_text())
    assert sum(kept) == 21 and sum(ran) == 21


def test_a_failure_is_reported_with_its_node_id(project):
    (project / "tests" / "test_bad.py").write_text("def test_no():\n    assert False\n")
    out = project / "out"
    result = runPytest(project, out, "--", "tests/test_bad.py")
    status = json.loads((out / "status.json").read_text())
    assert result.returncode == 1
    assert status["outcome"] == "test-failure"
    assert status["counts"]["failed"] == ["tests/test_bad.py::test_no"]


def test_a_hang_is_killed_and_still_reported(project):
    (project / "tests" / "test_hang.py").write_text("import time\ndef test_hang():\n    time.sleep(600)\n")
    out = project / "out"
    result = runPytest(project, out, "--test-timeout", "0", "--hard-timeout", "8", "--", "tests/test_hang.py")
    status = json.loads((out / "status.json").read_text())
    assert result.returncode != 0
    assert status["outcome"] == "hard-timeout"
    assert status["seconds"] < 60


def test_the_test_process_gets_an_isolated_home(project, monkeypatch):
    monkeypatch.setenv("BIOCOGNIA_DIR", "/the/real/one")
    (project / "tests" / "test_env.py").write_text(textwrap.dedent("""
        import os
        def test_env():
            assert os.environ["BIOCOGNIA_DIR"] != "/the/real/one"
            assert "ci-home" in os.environ["XDG_CONFIG_HOME"]
            assert os.environ["BIOCOGNIA_OFFLINE"] == "1"
    """))
    result = runPytest(project, project / "out", "--", "tests/test_env.py")
    assert result.returncode == 0, result.stdout


# -- select_tests ---------------------------------------------------------------------------


@pytest.fixture
def repo(tmp_path, monkeypatch):
    def write(path, text=""):
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    write("pkg/__init__.py")
    write("pkg/tl/__init__.py")
    write("pkg/tl/spatialCount.py", "from pkg.tl._helpers import x\n")
    write("pkg/tl/_helpers.py", "x = 1\n")
    write("pkg/tl/orphan.py")
    write("tests/test_init.py", "import pkg\n")
    write("tests/test_counts.py", "import pkg as sm\nsm.tl.spatialCount()\n")
    write("tests/test_orphan_free.py", "")
    write("website/page.mdx")
    monkeypatch.chdir(tmp_path)
    config = {
        "package": "pkg",
        "aliases": ["sm"],
        "essential": ["tests/test_init.py"],
        "core": ["tests/test_orphan_free.py"],
        "paths": {"none": ["website/**"], "core": ["pyproject.toml"]},
        "flags": {"site": ["website/**"]},
    }
    return select_tests.Selector(config, tmp_path)


def test_a_public_module_maps_to_the_tests_that_call_it(repo):
    result = repo.select([("M", "pkg/tl/spatialCount.py")], "auto")
    assert result["scope"] == "essential"
    assert result["tests"] == ["tests/test_counts.py", "tests/test_init.py"]


def test_a_private_helper_follows_its_importers(repo):
    result = repo.select([("M", "pkg/tl/_helpers.py")], "auto")
    assert "tests/test_counts.py" in result["tests"]


def test_a_module_no_test_names_widens_to_core(repo):
    result = repo.select([("M", "pkg/tl/orphan.py")], "auto")
    assert result["scope"] == "core"
    assert "tests/test_orphan_free.py" in result["tests"]


def test_website_only_needs_nothing(repo):
    result = repo.select([("M", "website/page.mdx")], "auto")
    assert result["scope"] == "none" and result["flags"] == ["site"]


def test_no_base_means_core(repo):
    assert repo.select(None, "auto")["scope"] == "core"


def test_the_budget_defers_the_most_expensive(repo, tmp_path):
    (tmp_path / "ci").mkdir()
    (tmp_path / "ci/durations.json").write_text(json.dumps({"tests/test_counts.py::t": 500}))
    result = repo.select([("M", "pkg/tl/spatialCount.py")], "auto")
    assert result["deferred"] == ["tests/test_counts.py"]


# -- report ------------------------------------------------------------------------


def test_classify():
    ran = {"runner_name": "GitHub Actions 3", "steps": [{"name": "Sync", "conclusion": "failure"}]}
    assert report.classify({**ran, "conclusion": "timed_out"}, None) == "hard-timeout"
    assert report.classify({**ran, "conclusion": "failure"}, None) == "setup-failure"
    assert report.classify({"conclusion": "failure", "runner_name": None, "steps": []}, None) == "not-started"
    assert report.classify({**ran, "conclusion": "failure"}, {"outcome": "session-timeout"}) == "session-timeout"


def test_billing_counts_list_prices():
    job = {"runner_name": "x", "started_at": "2026-10-01T00:00:00Z", "completed_at": "2026-10-01T00:09:30Z"}
    assert report.billed({**job, "labels": ["ubuntu-latest"]}) == 10
    assert report.billed({**job, "labels": ["macos-15"]}) == pytest.approx(10 * 62 / 6)
    assert report.billed({**job, "labels": ["self-hosted", "macOS"]}) == 0


def test_a_lost_shard_is_an_error():
    def record(k, selected, ran):
        return {"label": f"linux shard {k}/2", "seconds": 60, "counts": {"tests": ran},
                "collection": {"shard": f"{k}/2", "before_split": 10, "selected": selected}}
    _, errors = report.shardChecks({"a": record(1, 5, 5), "b": record(2, 5, 5)})
    assert errors == []
    _, errors = report.shardChecks({"a": record(1, 5, 5), "b": record(2, 4, 3)})
    assert any("kept 9 of 10" in e for e in errors) and any("never ran" in e for e in errors)
    _, errors = report.shardChecks({"a": record(1, 5, 5)})
    assert any("only 1 of 2" in e for e in errors)
