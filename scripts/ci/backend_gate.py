"""Require complete successful backend coverage on every PR, without skip lanes."""
import argparse
import json
import os
from pathlib import Path


def check_gate(needs, receipts, head):
    required = {"backend-shard", "backend-contracts", "backend-security"}
    if set(needs) != required or any(needs[job].get("result") != "success" for job in required):
        raise ValueError("required backend job failed, cancelled, missing or skipped")
    if len(receipts) != 4 or {r["shard"] for r in receipts} != {1, 2, 3, 4}:
        raise ValueError("missing/duplicate shard receipt")
    if (any(r["head"] != head or r["count"] <= 0 or r["workers"] != 2 for r in receipts)
            or len({r["collection"] for r in receipts}) != 1
            or len({r["partition"] for r in receipts}) != 1
            or len({r["total"] for r in receipts}) != 1
            or sum(r["count"] for r in receipts) != receipts[0]["total"]):
        raise ValueError("inconsistent partition receipts")


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
