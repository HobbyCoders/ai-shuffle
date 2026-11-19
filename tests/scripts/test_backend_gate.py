"""Every backend dependency and every shard receipt is mandatory on every PR."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from scripts.ci.backend_gate import check_gate

ROOT = Path(__file__).resolve().parents[2]


def healthy_gate():
    needs = {name: {"result": "success"} for name in
             ("backend-shard", "backend-contracts", "backend-security", "backend-postgres")}
    receipts = [{"shard": i, "head": "head", "count": 2, "total": 16,
                 "collection": "collection", "partition": "partition", "workers": 2} for i in range(1, 9)]
    return needs, receipts


def test_gate_rejects_a_self_consistent_four_shard_receipt_set():
    needs, receipts = healthy_gate()
    old_receipts = [{**receipt, "total": 8} for receipt in receipts[:4]]
    with pytest.raises(ValueError, match="missing/duplicate shard receipt"):
        check_gate(needs, old_receipts, "head")


def test_gate_requires_complete_coverage():
    needs, receipts = healthy_gate()
    check_gate(needs, receipts, "head")


@pytest.mark.parametrize("job", ["backend-shard", "backend-contracts", "backend-security", "backend-postgres"])
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", "", None])
def test_fault_injected_gate_blocks_unsuccessful_jobs(job, result):
    needs, receipts = healthy_gate()
    needs[job]["result"] = result
    with pytest.raises(ValueError):
        check_gate(needs, receipts, "head")


@pytest.mark.parametrize("fault", ["missing-job", "missing-shard", "duplicate-shard", "empty-shard", "wrong-head", "wrong-total", "wrong-collection", "wrong-partition", "missing-worker", "extra-job"])
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
                      "wrong-partition": ("partition", "other"), "missing-worker": ("workers", 1)}[fault]
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
