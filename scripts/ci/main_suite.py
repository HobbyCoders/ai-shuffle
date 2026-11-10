"""Hosted full-suite parity helpers and the fail-closed final aggregate.

Reuse local suite implementations rather than maintaining parallel test lists.
The optional native android-app stage is not in Linux attestation coverage:
without an SDK local/full explicitly reports it as not covered (as it does iOS).
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

# Every local suite is accounted for, including the explicitly unavailable SDK
# stage. Tests compare this inventory with local_ci.SUITES to catch additions.
PARITY = {
    "backend": "backend",
    "backend-linux": "backend",
    "fluid": "fluid",
    "frontend": "javascript",
    "mobile": "javascript",
    "tools": "javascript",
    "browser-mcp": "javascript",
    "docs": "android-and-docs",
    "desktop": "desktop",
    "android-bridge": "android-and-docs",
    "android-play": "android-and-docs",
    "android-app": None,  # SDK/Gradle is an explicit local non-coverage item.
    "workflows": "policy",
    "update-reach": "update-reach",
}
REQUIRED = (
    "backend", "javascript", "fluid", "windows", "browser-workspace",
    "container", "identity", "policy", "desktop-source", "desktop",
    "android-and-docs", "supply-chain", "update-reach",
)

# Areas whose pull-request checks are the same as main's: a push may skip one
# when the "proven" job found a pull request's pass record for the same files.
PROVABLE = ("backend", "fluid", "windows", "browser-workspace", "container", "identity", "desktop-source")
LOOKUP = "proven"


def aggregate(results):
    if not isinstance(results, dict) or set(results) != {*REQUIRED, LOOKUP}:
        raise ValueError("Missing or unexpected main-suite dependency")
    lookup = results[LOOKUP]
    found = (lookup.get("outputs") or {}) if lookup.get("result") == "success" else {}
    for name in REQUIRED:
        result = results[name].get("result")
        if result == "skipped" and name in PROVABLE and found.get(name) == "true":
            continue
        if result != "success":
            raise ValueError(f"Main-suite dependency {name} did not succeed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("aggregate", "android-and-docs", "update-reach"))
    args = parser.parse_args(argv)
    if args.stage == "aggregate":
        aggregate(json.loads(os.environ["RESULTS"]))
        print("Every hosted full-suite dependency succeeded, or a pull request passed it on these exact files.")
    elif args.stage == "android-and-docs":
        import local_ci
        ctx = local_ci.Context("full", "HEAD^", None, sys.executable)
        local_ci.android_bridge(ctx)
        local_ci.android_play(ctx)
        local_ci.docs(ctx)
    else:
        from ci.edge_publication_gate import update_reach_readiness
        revision = os.environ["GITHUB_SHA"]
        ready, reason = update_reach_readiness(revision, root=ROOT)
        print(reason)
        if not ready:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
