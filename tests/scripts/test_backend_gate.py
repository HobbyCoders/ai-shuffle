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
             ("backend-shard", "backend-contracts", "backend-security")}
    receipts = [{"shard": i, "head": "head", "count": 2, "total": 8,
                 "collection": "collection", "partition": "partition", "workers": 2} for i in range(1, 5)]
    return needs, receipts


def test_gate_requires_complete_coverage():
    needs, receipts = healthy_gate()
    check_gate(needs, receipts, "head")


@pytest.mark.parametrize("job", ["backend-shard", "backend-contracts", "backend-security"])
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


def test_skip_approval_cannot_bypass_gate():
    needs, _ = healthy_gate()
    for job in ("backend-shard", "backend-contracts"):
        needs[job]["result"] = "skipped"
    needs["changes"] = {"result": "success", "outputs": {"full_backend": "false"}}
    with pytest.raises(ValueError):
        check_gate(needs, [], "head")
