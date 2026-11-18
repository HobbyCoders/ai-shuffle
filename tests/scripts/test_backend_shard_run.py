"""scripts/ci/backend_shard_run.py: a Linux backend shard reruns its failed tests once.

The end-to-end cases run a real two-worker shard (scripts.ci.backend_shards)
on a generated suite outside the repository test tree, through the same
local_ci.run retry the hosted Windows parts use.
"""
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "ci"))
import backend_shard_run  # noqa: E402  pyright: ignore[reportMissingImports]
import local_ci  # noqa: E402  pyright: ignore[reportMissingImports]
import windows_parts  # noqa: E402  pyright: ignore[reportMissingImports]
from scripts.ci import backend_shards as bs  # noqa: E402
from scripts.ci.backend_gate import check_gate  # noqa: E402

FLAKY_NODE = "test_fixture.py::test_value[3]"


@pytest.fixture
def shard(tmp_path, monkeypatch):
    """One shard of a 12-test suite, the one holding test_value[3]."""
    monkeypatch.setenv(local_ci.RETRY_FAILED_ENV, "1")
    monkeypatch.setattr(local_ci, "FLAKY", [])
    monkeypatch.setenv("PYTHONPATH", str(ROOT))
    monkeypatch.setenv("GITHUB_SHA", "integration-head")
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    # Node ids are reported relative to the suite's rootdir, as CI's are to the checkout.
    monkeypatch.setattr(windows_parts, "ROOT", tmp_path.resolve())
    nodes = [f"test_fixture.py::test_value[{i}]" for i in range(12)]
    manifest = {"format": bs.MANIFEST_FORMAT, "durations": {}}
    (tmp_path / "durations.json").write_text(json.dumps(manifest))
    shards = bs.partition(nodes, bs.manifest_weights(nodes, manifest))
    index = next(i for i, nodes in enumerate(shards, 1) if FLAKY_NODE in nodes)
    results = tmp_path / "results"

    def run(body: str) -> int:
        fixture = tmp_path / "test_fixture.py"
        fixture.write_text("from pathlib import Path\nimport pytest\n"
                           f"MARKER = Path({str(tmp_path / 'ran-once')!r})\n"
                           "@pytest.mark.parametrize('value', range(12))\n"
                           f"def test_value(value):\n{body}")
        return backend_shard_run.main([
            "--results", str(results), "--",
            sys.executable, "-m", "pytest", str(fixture), "--rootdir", str(tmp_path),
            "--confcutdir", str(tmp_path), "-o", "addopts=", "-q", "-p", "no:cacheprovider",
            "-n", "2", "-p", "scripts.ci.backend_shards", "--backend-shard", str(index),
            "--backend-durations", str(tmp_path / "durations.json"), "--backend-results", str(results)])

    return run, results


def test_a_test_that_passes_its_rerun_passes_the_shard_as_flaky(shard, tmp_path, monkeypatch, capsys):
    run, results = shard
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    flaky_once = ("    if value == 3 and not MARKER.exists():\n"
                  "        MARKER.write_text('x')\n"
                  "        raise AssertionError('first run fails')\n")
    assert run(flaky_once) == 0
    receipt = json.loads((results / "receipt.json").read_text())
    assert receipt["failed"] == receipt["rerun_passed"] == [FLAKY_NODE]
    out = capsys.readouterr().out
    assert f"::warning title=Flaky test::{FLAKY_NODE} failed, then passed on a rerun" in out
    assert "1 flaky test(s) passed on a rerun" in out
    assert f"- `{FLAKY_NODE}`" in summary.read_text(encoding="utf-8")
    # The marked receipt satisfies the gate like a clean one.
    needs = {name: {"result": "success"} for name in ("backend-shard", "backend-contracts", "backend-security")}
    others = [{**receipt, "shard": i, "failed": [], "rerun_passed": []} for i in range(1, 5) if i != receipt["shard"]]
    for other in others:
        other["count"] = (receipt["total"] - receipt["count"]) // 3
    others[-1]["count"] += receipt["total"] - receipt["count"] - sum(o["count"] for o in others)
    check_gate(needs, [receipt, *others], "integration-head")


def test_a_test_that_fails_twice_fails_the_shard_and_records_no_rerun_pass(shard):
    run, results = shard
    assert run("    assert value != 3\n") == 1
    receipt = json.loads((results / "receipt.json").read_text())
    assert receipt["failed"] == [FLAKY_NODE]
    assert "rerun_passed" not in receipt
    assert local_ci.FLAKY == []


def test_a_clean_shard_leaves_an_unmarked_receipt(shard):
    run, results = shard
    assert run("    pass\n") == 0
    receipt = json.loads((results / "receipt.json").read_text())
    assert receipt["failed"] == [] and "rerun_passed" not in receipt


def test_a_rerun_is_recorded_only_for_exactly_the_receipts_failed_tests(tmp_path):
    receipt = tmp_path / "receipt.json"
    with pytest.raises(ValueError, match="no shard receipt"):
        backend_shard_run.mark_rerun(receipt, ["tests/a.py::test_a"])
    receipt.write_text(json.dumps({"shard": 1, "failed": ["tests/a.py::test_a", "tests/b.py::test_b"]}))
    for passed in (["tests/a.py::test_a"], ["tests/a.py::test_a", "tests/b.py::test_b", "tests/c.py::test_c"]):
        with pytest.raises(ValueError, match="differ"):
            backend_shard_run.mark_rerun(receipt, passed)
    assert "rerun_passed" not in json.loads(receipt.read_text())
    backend_shard_run.mark_rerun(receipt, ["tests/b.py::test_b", "tests/a.py::test_a", "tests/a.py::test_a"])
    assert json.loads(receipt.read_text())["rerun_passed"] == ["tests/a.py::test_a", "tests/b.py::test_b"]


def test_only_a_pytest_command_is_accepted():
    with pytest.raises(SystemExit):
        backend_shard_run.main(["--", sys.executable, "-c", "pass"])
