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
FAILS_ONCE = ("    if value == 3 and not MARKER.exists():\n"
              "        MARKER.write_text('x')\n"
              "        raise AssertionError('first run fails')\n")


@pytest.fixture
def shard(tmp_path, monkeypatch):
    """One shard of a 12-test suite, the one holding test_value[3]."""
    monkeypatch.setenv(local_ci.RETRY_FAILED_ENV, "1")
    monkeypatch.setattr(local_ci, "FLAKY", [])
    monkeypatch.setattr(local_ci, "RERUN_PASSED", [])
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

    def run(body: str, *extra: str) -> int:
        fixture = tmp_path / "test_fixture.py"
        fixture.write_text("from pathlib import Path\nimport pytest\n"
                           f"MARKER = Path({str(tmp_path / 'ran-once')!r})\n"
                           "@pytest.mark.parametrize('value', range(12))\n"
                           f"def test_value(value):\n{body}")
        return backend_shard_run.main([
            "--results", str(results), "--",
            sys.executable, "-m", "pytest", str(tmp_path), "--rootdir", str(tmp_path),
            "--confcutdir", str(tmp_path), "-o", "addopts=", "-q", "-p", "no:cacheprovider",
            "-n", "2", "-p", "scripts.ci.backend_shards", "--backend-shard", str(index),
            "--backend-durations", str(tmp_path / "durations.json"), "--backend-results", str(results),
            *extra])

    return run, results


def test_a_test_that_passes_its_rerun_passes_the_shard_as_flaky(shard, tmp_path, monkeypatch, capsys):
    run, results = shard
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert run(FAILS_ONCE) == 0
    receipt = json.loads((results / "receipt.json").read_text())
    assert receipt["failed"] == receipt["rerun_passed"] == [FLAKY_NODE]
    out = capsys.readouterr().out
    assert f"::warning title=Flaky test::{FLAKY_NODE} failed, then passed on a rerun" in out
    assert "1 flaky test(s) passed on a rerun" in out
    assert f"- `{FLAKY_NODE}`" in summary.read_text(encoding="utf-8")
    # The marked receipt satisfies the gate like a clean one.
    gate(receipt)


def gate(receipt):
    """backend_gate.check_gate on this receipt plus three clean shards."""
    needs = {name: {"result": "success"} for name in ("backend-shard", "backend-contracts", "backend-security", "backend-postgres")}
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


def test_a_collection_error_elsewhere_leaves_no_receipt_to_rerun_into(shard, tmp_path):
    # The broken module never runs, and the targeted rerun would not import it.
    (tmp_path / "test_broken.py").write_text("import not_a_module_anywhere\n\ndef test_x():\n    pass\n")
    run, results = shard
    assert run(FAILS_ONCE) == 1
    assert not (results / "receipt.json").exists()


def test_a_test_that_fails_then_skips_on_the_rerun_has_not_passed(shard):
    run, results = shard
    assert run(FAILS_ONCE + "    if value == 3:\n        pytest.skip('only under the rerun')\n") == 1
    receipt = json.loads((results / "receipt.json").read_text())
    assert receipt["failed"] == [FLAKY_NODE]
    assert "rerun_passed" not in receipt


def guard(tmp_path: Path, body: str) -> None:
    """An autouse fixture around test_value[3]; ``body`` uses TORN and value."""
    (tmp_path / "conftest.py").write_text(
        "from pathlib import Path\nimport pytest\n"
        f"TORN = Path({str(tmp_path / 'torn-once')!r})\n"
        f"MARKER = Path({str(tmp_path / 'ran-once')!r})\n\n"
        "@pytest.fixture(autouse=True)\n"
        "def guard(request):\n"
        "    value = request.node.callspec.params['value']\n" + body)


def test_a_test_that_fails_in_teardown_then_skips_there_has_not_passed(shard, tmp_path):
    # The body passes both times; the rerun's teardown skips before its check.
    guard(tmp_path, "    yield\n"
                    "    if value == 3 and not TORN.exists():\n"
                    "        TORN.write_text('x')\n"
                    "        raise AssertionError('teardown fails once')\n"
                    "    if value == 3:\n"
                    "        pytest.skip('teardown skips on the rerun')\n")
    run, results = shard
    assert run("    pass\n") == 1
    receipt = json.loads((results / "receipt.json").read_text())
    assert receipt["failed"] == [FLAKY_NODE] and "rerun_passed" not in receipt
    assert local_ci.RERUN_PASSED == []


def test_a_test_skipped_in_setup_on_the_rerun_has_not_passed(shard, tmp_path):
    guard(tmp_path, "    if value == 3 and MARKER.exists():\n"
                    "        pytest.skip('setup skips on the rerun')\n"
                    "    yield\n")
    run, results = shard
    assert run(FAILS_ONCE) == 1
    assert "rerun_passed" not in json.loads((results / "receipt.json").read_text())
    assert local_ci.RERUN_PASSED == []


def test_a_warning_on_the_rerun_is_still_a_pass(shard):
    run, results = shard
    assert run(FAILS_ONCE + "    import warnings\n    warnings.warn('only a warning')\n") == 0
    assert json.loads((results / "receipt.json").read_text())["rerun_passed"] == [FLAKY_NODE]


def test_a_failed_scratch_cleanup_is_not_rerun(shard, tmp_path):
    # test_scratch's cleanup raises after the session finished (and after the
    # receipt was written): that run must not be recoverable by a clean rerun.
    (tmp_path / "conftest.py").write_text(
        "from contextlib import contextmanager\n"
        "import tempfile\n"
        "from scripts.ci import test_scratch\n\n"
        "@contextmanager\n"
        "def leaky(prefix, root=None):\n"
        "    yield tempfile.mkdtemp(prefix=prefix)\n"
        "    raise PermissionError(13, 'cleanup refused')\n\n"
        "test_scratch.scratch_directory = leaky\n")
    run, results = shard
    assert run(FAILS_ONCE, "-p", "scripts.ci.test_scratch") == 1
    assert local_ci.FLAKY == [] and local_ci.RERUN_PASSED == []
    receipt = json.loads((results / "receipt.json").read_text())
    assert receipt["failed"] == [FLAKY_NODE] and "rerun_passed" not in receipt
    with pytest.raises(ValueError, match="without a passing rerun"):
        gate(receipt)


def test_a_clean_shard_with_owned_scratch_writes_its_receipt(shard):
    run, results = shard
    assert run("    pass\n", "-p", "scripts.ci.test_scratch") == 0
    assert json.loads((results / "receipt.json").read_text())["failed"] == []


def test_a_clean_shard_leaves_an_unmarked_receipt(shard):
    run, results = shard
    assert run("    pass\n") == 0
    receipt = json.loads((results / "receipt.json").read_text())
    assert receipt["failed"] == [] and "rerun_passed" not in receipt


def test_a_rerun_is_recorded_only_for_exactly_the_receipts_failed_tests(tmp_path):
    receipt = tmp_path / "receipt.json"
    a, b, c = "tests/a.py::test_a", "tests/b.py::test_b", "tests/c.py::test_c"
    with pytest.raises(ValueError, match="no shard receipt"):
        backend_shard_run.mark_rerun(receipt, [a], [a])
    receipt.write_text(json.dumps({"shard": 1, "failed": [a, b]}))
    for rerun, passed in (([a], [a]), ([a, b, c], [a, b, c]), ([a, b], [a]), ([a, b], [a, b, c])):
        with pytest.raises(ValueError):
            backend_shard_run.mark_rerun(receipt, rerun, passed)
    assert "rerun_passed" not in json.loads(receipt.read_text())
    backend_shard_run.mark_rerun(receipt, [b, a, a], [a, b])
    assert json.loads(receipt.read_text())["rerun_passed"] == [a, b]


def test_only_a_pytest_command_is_accepted():
    with pytest.raises(SystemExit):
        backend_shard_run.main(["--", sys.executable, "-c", "pass"])
