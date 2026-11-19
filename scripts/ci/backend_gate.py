"""Require complete successful backend coverage on every PR, without skip lanes."""
import argparse
import json
import os
from pathlib import Path


REQUIRED_JOBS = frozenset({"backend-shard", "backend-contracts", "backend-security", "backend-postgres"})


def check_gate(needs, receipts, head):
    if set(needs) != REQUIRED_JOBS or any(needs[job].get("result") != "success" for job in REQUIRED_JOBS):
        raise ValueError("required backend job failed, cancelled, missing or skipped")
    if len(receipts) != 4 or {r["shard"] for r in receipts} != {1, 2, 3, 4}:
        raise ValueError("missing/duplicate shard receipt")
    if (any(r["head"] != head or r["count"] <= 0 or r["workers"] != 2 for r in receipts)
            or len({r["collection"] for r in receipts}) != 1
            or len({r["partition"] for r in receipts}) != 1
            or len({r["total"] for r in receipts}) != 1
            or sum(r["count"] for r in receipts) != receipts[0]["total"]):
        raise ValueError("inconsistent partition receipts")
    # A shard whose tests failed counts only when exactly those tests passed
    # their one rerun (scripts/ci/backend_shard_run.py marks the receipt).
    for r in receipts:
        failed, passed = r.get("failed", []), r.get("rerun_passed", [])
        if not isinstance(failed, list) or not isinstance(passed, list) or sorted(failed) != sorted(passed):
            raise ValueError(f"backend shard {r['shard']} has failed tests without a passing rerun")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", required=True)
    parser.add_argument("--receipts", default="shard-results")
    args = parser.parse_args()
    receipts = [json.loads(p.read_text()) for p in Path(args.receipts).glob("**/receipt.json")]
    check_gate(json.loads(os.environ["BACKEND_NEEDS"]), receipts, args.head)
    print("Backend tests: all required coverage succeeded")


if __name__ == "__main__":
    main()
