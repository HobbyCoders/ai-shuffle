"""Every backend dependency and every chosen shard's receipt is mandatory; only a PR may choose fewer."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from scripts.ci.backend_gate import SHARD_WORKERS, check_gate

ROOT = Path(__file__).resolve().parents[2]


JOBS = ("backend-select", "backend-shard", "backend-contracts", "backend-security", "backend-postgres")
SELECTED = "files-0123456789abcdef"


def healthy_gate(shards=8, selection="all"):
    needs = {name: {"result": "success"} for name in JOBS}
    receipts = [{"shard": i, "head": "head", "count": 2, "total": 2 * shards, "shards": shards,
                 "selection": selection, "collection": "collection", "partition": "partition", "workers": SHARD_WORKERS}
                for i in range(1, shards + 1)]
    return needs, receipts


def test_gate_rejects_a_self_consistent_four_shard_receipt_set():
    needs, receipts = healthy_gate()
    old_receipts = [{**receipt, "total": 8} for receipt in receipts[:4]]
    with pytest.raises(ValueError, match="missing/duplicate shard receipt"):
        check_gate(needs, old_receipts, "head")


def test_gate_requires_complete_coverage():
    needs, receipts = healthy_gate()
    check_gate(needs, receipts, "head")


@pytest.mark.parametrize("job", JOBS)
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "", None])
def test_fault_injected_gate_blocks_unsuccessful_jobs(job, result):
    needs, receipts = healthy_gate()
    needs[job]["result"] = result
    with pytest.raises(ValueError):
        check_gate(needs, receipts, "head")


@pytest.mark.parametrize("fault", ["missing-job", "missing-shard", "duplicate-shard", "empty-shard", "wrong-head", "wrong-total", "wrong-collection", "wrong-partition", "missing-worker", "old-two-workers", "extra-job", "wrong-shard-count", "no-shard-count", "wrong-selection", "no-selection"])
def test_fault_injected_gate_blocks_incomplete_evidence(fault):
    needs, receipts = healthy_gate()
    if fault == "missing-job":
        del needs["backend-shard"]
    elif fault == "missing-shard":
        receipts.pop()
    elif fault == "duplicate-shard":
        receipts[0]["shard"] = 2
    elif fault == "extra-job":
        needs["changes"] = {"result": "success", "outputs": {"full_backend": "false"}}
    else:
        key, value = {"empty-shard": ("count", 0), "wrong-head": ("head", "other"),
                      "wrong-total": ("total", 9), "wrong-collection": ("collection", "other"),
                      "wrong-partition": ("partition", "other"), "missing-worker": ("workers", SHARD_WORKERS - 1), "old-two-workers": ("workers", 2),
                      "wrong-shard-count": ("shards", 4), "no-shard-count": ("shards", None),
                      "wrong-selection": ("selection", SELECTED), "no-selection": ("selection", None)}[fault]
        receipts[0][key] = value
    with pytest.raises(ValueError):
        check_gate(needs, receipts, "head")


@pytest.mark.parametrize("result", ["cancelled", "skipped", "failure"])
def test_gate_cli_exits_nonzero_on_unsuccessful_shard(tmp_path, result):
    needs, _ = healthy_gate()
    needs["backend-shard"]["result"] = result
    response = subprocess.run([sys.executable, str(ROOT / "scripts/ci/backend_gate.py"),
                               "--head", "head", "--receipts", str(tmp_path)],
                              env={**os.environ, "BACKEND_NEEDS": json.dumps(needs)}, capture_output=True)
    assert response.returncode != 0
    assert b"cancelled" in response.stderr


def test_gate_rejects_previous_backend_job_set_without_postgres():
    needs, receipts = healthy_gate()
    needs.pop("backend-postgres", None)
    with pytest.raises(ValueError, match="required backend job"):
        check_gate(needs, receipts, "head")


def test_skip_approval_cannot_bypass_gate():
    needs, _ = healthy_gate()
    for job in ("backend-shard", "backend-contracts"):
        needs[job]["result"] = "skipped"
    needs["changes"] = {"result": "success", "outputs": {"full_backend": "false"}}
    with pytest.raises(ValueError):
        check_gate(needs, [], "head")


def test_failed_shard_tests_count_only_with_their_passing_rerun():
    needs, receipts = healthy_gate()
    receipts[2]["failed"] = ["tests/core/test_x.py::test_a", "tests/core/test_y.py::test_b[1]"]
    with pytest.raises(ValueError, match="shard 3 has failed tests without a passing rerun"):
        check_gate(needs, receipts, "head")
    receipts[2]["rerun_passed"] = ["tests/core/test_x.py::test_a"]
    with pytest.raises(ValueError, match="without a passing rerun"):
        check_gate(needs, receipts, "head")
    receipts[2]["rerun_passed"] = "tests/core/test_x.py::test_a"
    with pytest.raises(ValueError, match="without a passing rerun"):
        check_gate(needs, receipts, "head")
    receipts[2]["rerun_passed"] = list(reversed(receipts[2]["failed"]))
    check_gate(needs, receipts, "head")
    receipts[0]["failed"] = []
    check_gate(needs, receipts, "head")


def test_a_pull_request_may_run_the_shards_it_chose():
    needs, receipts = healthy_gate(3, SELECTED)
    assert check_gate(needs, receipts, "head", [1, 2, 3], "pull_request", SELECTED) is False
    # A selected run using all eight shards is still not a full run.
    needs, receipts = healthy_gate(8, SELECTED)
    assert check_gate(needs, receipts, "head", list(range(1, 9)), "pull_request", SELECTED) is False
    needs, receipts = healthy_gate()
    assert check_gate(needs, receipts, "head", list(range(1, 9)), "pull_request", "all") is True


@pytest.mark.parametrize("event", ["push", "schedule", "workflow_dispatch", ""])
def test_only_a_pull_request_may_run_part_of_the_suite(event):
    needs, receipts = healthy_gate(3, SELECTED)
    with pytest.raises(ValueError, match="only a pull request"):
        check_gate(needs, receipts, "head", [1, 2, 3], event, SELECTED)
    needs, receipts = healthy_gate(8, SELECTED)
    with pytest.raises(ValueError, match="only a pull request"):
        check_gate(needs, receipts, "head", list(range(1, 9)), event, SELECTED)
    needs["backend-shard"]["result"] = "skipped"
    with pytest.raises(ValueError, match="only a pull request"):
        check_gate(needs, [], "head", [], event, "none")
    needs, receipts = healthy_gate()
    assert check_gate(needs, receipts, "head", None, event) is True


def test_receipts_from_another_selection_or_shard_count_do_not_count():
    needs, receipts = healthy_gate(3, SELECTED)
    with pytest.raises(ValueError, match="inconsistent"):
        check_gate(needs, receipts, "head", [1, 2, 3], "pull_request", "files-ffffffffffffffff")
    needs, receipts = healthy_gate(8, "all")
    with pytest.raises(ValueError, match="missing/duplicate"):
        check_gate(needs, receipts, "head", [1, 2, 3], "pull_request", SELECTED)


def test_a_pull_request_no_shard_test_reaches_runs_and_leaves_no_shard():
    needs, receipts = healthy_gate()
    needs["backend-shard"]["result"] = "skipped"
    assert check_gate(needs, [], "head", [], "pull_request", "none") is False
    with pytest.raises(ValueError, match="no backend shard was chosen"):
        check_gate(needs, receipts[:1], "head", [], "pull_request", "none")
    needs["backend-shard"]["result"] = "success"
    with pytest.raises(ValueError, match="no backend shard was chosen"):
        check_gate(needs, [], "head", [], "pull_request", "none")
    # Skipping the shards never excuses another job.
    needs["backend-shard"]["result"] = "skipped"
    needs["backend-security"]["result"] = "skipped"
    with pytest.raises(ValueError, match="required backend job"):
        check_gate(needs, [], "head", [], "pull_request", "none")


@pytest.mark.parametrize("shards, selection", [
    ([1, 3], SELECTED), ([0, 1], SELECTED), (list(range(1, 10)), SELECTED), ("[1]", SELECTED),
    ([1, 2], "all"), ([], "all"), ([1], "none"), ([], SELECTED), ([1], ""), ([1], "everything"), ([1], None),
])
def test_a_choice_that_does_not_fit_together_fails(shards, selection):
    needs, receipts = healthy_gate(1, selection)
    with pytest.raises(ValueError):
        check_gate(needs, receipts, "head", shards, "pull_request", selection)


def gate_cli(tmp_path, needs, receipts, **env):
    for receipt in receipts:
        folder = tmp_path / "receipts" / f"backend-shard-{receipt['shard']}"
        folder.mkdir(parents=True)
        (folder / "receipt.json").write_text(json.dumps(receipt))
    output = tmp_path / "output"
    response = subprocess.run([sys.executable, str(ROOT / "scripts/ci/backend_gate.py"), "--head", "head",
                               "--receipts", str(tmp_path / "receipts")],
                              env={**os.environ, "BACKEND_NEEDS": json.dumps(needs), "GITHUB_OUTPUT": str(output),
                                   **env}, capture_output=True, text=True, check=False)
    return response, output.read_text() if output.exists() else ""


def test_gate_cli_says_whether_the_run_tested_everything(tmp_path):
    needs, receipts = healthy_gate(2, SELECTED)
    response, output = gate_cli(tmp_path / "selected", needs, receipts, BACKEND_SHARDS="[1, 2]",
                                BACKEND_SELECTION=SELECTED, GITHUB_EVENT_NAME="pull_request")
    assert response.returncode == 0, response.stderr
    assert output == "full=false\n" and "main runs every test" in response.stdout
    needs, receipts = healthy_gate()
    response, output = gate_cli(tmp_path / "full", needs, receipts, GITHUB_EVENT_NAME="push")
    assert response.returncode == 0, response.stderr
    assert output == "full=true\n"


def test_gate_cli_fails_when_the_choice_could_not_be_read(tmp_path):
    needs, receipts = healthy_gate()
    response, _ = gate_cli(tmp_path, needs, receipts, BACKEND_SHARDS="[1, 2, 3, 4, 5, 6, 7, 8]",
                           BACKEND_SELECTION="", GITHUB_EVENT_NAME="pull_request")
    assert response.returncode != 0 and "does not fit" in response.stderr
