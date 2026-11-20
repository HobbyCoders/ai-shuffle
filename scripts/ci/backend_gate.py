"""Require complete successful backend coverage of what the run had to test.

A push to main, the daily run and a manual run test everything: every one of
the eight shards, each receipt saying the selection was ``all``. A pull request
tests the files ``scripts/ci/backend_test_map.py ci-select`` chose for it, in
the shards it chose (none when no test in the shards can reach the change);
main runs everything after the merge. Either way every chosen shard must leave
one consistent receipt, and every other backend job must succeed.

    BACKEND_NEEDS=... BACKEND_SHARDS='[1, 2]' python scripts/ci/backend_gate.py --head SHA [--event NAME]

Prints ``full=true`` when the run tested everything (the only run whose pass
can be recorded for later runs to skip backend on), else ``full=false``.
"""
import argparse
import json
import os
from pathlib import Path


SHARD_COUNT = 8
FULL = list(range(1, SHARD_COUNT + 1))
REQUIRED_JOBS = frozenset({"backend-select", "backend-shard", "backend-contracts", "backend-security",
                           "backend-postgres"})


def check_gate(needs, receipts, head, shards=None, event="push"):
    """Raise ValueError unless the run's backend coverage is complete. Return True for a full run.

    ``shards`` is the list the select job chose; None means a full run.
    """
    shards = FULL if shards is None else shards
    if not isinstance(shards, list) or shards != list(range(1, len(shards) + 1)) or len(shards) > SHARD_COUNT:
        raise ValueError("the chosen backend shards are not 1..n")
    if event != "pull_request" and shards != FULL:
        raise ValueError("only a pull request may run part of the backend suite")
    if set(needs) != REQUIRED_JOBS:
        raise ValueError("required backend job missing or unexpected")
    others = REQUIRED_JOBS - {"backend-shard"}
    if any(needs[job].get("result") != "success" for job in others):
        raise ValueError("required backend job failed, cancelled, missing or skipped")
    if not shards:
        # No test in the shards can reach the change: the shard job is skipped and leaves nothing.
        if needs["backend-shard"].get("result") != "skipped" or receipts:
            raise ValueError("no backend shard was chosen, yet one ran or left a receipt")
        return False
    if needs["backend-shard"].get("result") != "success":
        raise ValueError("required backend job failed, cancelled, missing or skipped")
    if len(receipts) != len(shards) or {r.get("shard") for r in receipts} != set(shards):
        raise ValueError("missing/duplicate shard receipt")
    if (any(r["head"] != head or r["count"] <= 0 or r["workers"] != 2 for r in receipts)
            or any(r.get("shards", SHARD_COUNT) != len(shards) for r in receipts)
            or len({r.get("selection", "all") for r in receipts}) != 1
            or len({r["collection"] for r in receipts}) != 1
            or len({r["partition"] for r in receipts}) != 1
            or len({r["total"] for r in receipts}) != 1
            or sum(r["count"] for r in receipts) != receipts[0]["total"]):
        raise ValueError("inconsistent partition receipts")
    full = shards == FULL and receipts[0].get("selection", "all") == "all"
    if event != "pull_request" and not full:
        raise ValueError("only a pull request may run part of the backend suite")
    # A shard whose tests failed counts only when exactly those tests passed
    # their one rerun (scripts/ci/backend_shard_run.py marks the receipt).
    for r in receipts:
        failed, passed = r.get("failed", []), r.get("rerun_passed", [])
        if not isinstance(failed, list) or not isinstance(passed, list) or sorted(failed) != sorted(passed):
            raise ValueError(f"backend shard {r['shard']} has failed tests without a passing rerun")
    return full


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--head", required=True)
    parser.add_argument("--receipts", default="shard-results")
    parser.add_argument("--event", default=os.environ.get("GITHUB_EVENT_NAME", ""))
    args = parser.parse_args()
    receipts = [json.loads(p.read_text()) for p in Path(args.receipts).glob("**/receipt.json")]
    shards = json.loads(os.environ.get("BACKEND_SHARDS") or "null")
    full = check_gate(json.loads(os.environ["BACKEND_NEEDS"]), receipts, args.head, shards, args.event)
    print("Backend tests: all required coverage succeeded"
          + ("" if full else " (the tests this pull request can reach; main runs every test)"))
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"full={'true' if full else 'false'}\n")


if __name__ == "__main__":
    main()
