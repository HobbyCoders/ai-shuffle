"""Duration-balanced pytest collection plugin; timing data never selects coverage.

Every xdist worker partitions the *actual* unsharded collection, proves exact
coverage, and reports the same partition digest. Only the successful controller
writes receipts/timings. No PR cache is read. A versioned manifest is a scheduling
hint only; unknown nodes cost at least ten seconds (or the observed maximum).
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess

import pytest

MANIFEST = Path(__file__).resolve().parents[2] / "tests/backend-durations.json"


def partition(nodeids, durations, count=4):
    if count < 1 or len(nodeids) < count or len(set(nodeids)) != len(nodeids):
        raise ValueError("duplicate collection or insufficient tests for nonempty shards")
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) or not math.isfinite(v) or v < 0
           for v in durations.values()):
        raise ValueError("invalid duration")
    unknown = max(10.0, max(durations.values(), default=0))
    ordered = sorted(nodeids, key=lambda node: (-durations.get(node, unknown), node))
    shards, loads = [[] for _ in range(count)], [0.0] * count
    for node in ordered:
        index = min(range(count), key=lambda i: (loads[i], i))
        shards[index].append(node)
        loads[index] += max(0.001, durations.get(node, unknown))
    shards = [sorted(shard) for shard in shards]
    validate_partition(nodeids, shards, count)
    return shards


def validate_partition(nodeids, shards, count=4):
    union = [node for shard in shards for node in shard]
    if (len(shards) != count or any(not shard for shard in shards)
            or len(set(union)) != len(union) or Counter(union) != Counter(nodeids)
            or len(set(nodeids)) != len(nodeids)):
        raise ValueError("partition must equal the complete collection exactly once, with no empty shard")


def digest(nodes):
    return hashlib.sha256(json.dumps(nodes, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def pytest_addoption(parser):
    group = parser.getgroup("backend-shards")
    group.addoption("--backend-shard", type=int, choices=range(1, 5))
    group.addoption("--backend-durations", default=str(MANIFEST))
    group.addoption("--backend-results", default="backend-results")


def pytest_configure(config):
    if config.getoption("backend_shard"):
        config.pluginmanager.register(ShardRun(config), "backend-shard-run")


class ShardRun:
    def __init__(self, config):
        self.config = config
        self.evidence = None
        self.workers = []
        self.durations = {}

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, session, config, items):
        nodes = [item.nodeid for item in items]
        manifest = json.loads(Path(config.getoption("backend_durations")).read_text())
        shards = partition(nodes, manifest["durations"])
        index = config.getoption("backend_shard") - 1
        selected = set(shards[index])
        self.evidence = {
            "shard": index + 1, "collection": digest(sorted(nodes)),
            "partition": digest(shards), "selected": digest(sorted(selected)),
            "count": len(selected), "total": len(nodes),
            "head": os.environ.get("GITHUB_SHA", "local"),
        }
        kept, removed = [], []
        for item in items:
            (kept if item.nodeid in selected else removed).append(item)
        items[:] = kept
        config.hook.pytest_deselected(items=removed)
        print(f"Partition invariant: {len(nodes)} nodes, shard sizes {[len(s) for s in shards]}, "
              f"exact union, no duplicates, no empty shard; digest {self.evidence['partition']}")

    @pytest.hookimpl(optionalhook=True)
    def pytest_testnodedown(self, node, error):
        self.workers.append(None if error else node.workeroutput.get("backend_partition"))

    def pytest_runtest_logreport(self, report):
        self.durations[report.nodeid] = self.durations.get(report.nodeid, 0.0) + report.duration

    @pytest.hookimpl(trylast=True)
    def pytest_sessionfinish(self, session, exitstatus):
        if hasattr(self.config, "workerinput"):
            self.config.workeroutput["backend_partition"] = self.evidence
            return
        if exitstatus != 0:
            return
        if (len(self.workers) != 2 or any(not evidence for evidence in self.workers)
                or self.workers[0] != self.workers[1]):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            print("Missing/disagreeing shard worker evidence; expected exactly two workers")
            return
        evidence = {**self.workers[0], "workers": len(self.workers)}
        if (len(self.durations) != evidence["count"]
                or digest(sorted(self.durations)) != evidence["selected"]):
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            print("Executed node IDs do not equal this shard's selected collection")
            return
        output = Path(self.config.getoption("backend_results"))
        output.mkdir(parents=True, exist_ok=True)
        (output / "receipt.json").write_text(json.dumps(evidence, indent=2) + "\n")
        (output / "durations.json").write_text(json.dumps(self.durations, sort_keys=True, indent=2) + "\n")
        print(f"Partition evidence: {json.dumps(evidence, sort_keys=True)}")


def refresh(run_id, directory, output):
    """Explicit maintainer refresh from a successful hosted main run, never a cache."""
    run = json.loads(subprocess.check_output([
        "gh", "run", "view", run_id, "--repo", "HobbyCoders/ai-shuffle", "--json",
        "conclusion,headBranch,headSha,workflowName,url,event"]))
    if (run["conclusion"] != "success" or run["headBranch"] != "main"
            or run["workflowName"] != "CI Backend" or run["event"] not in {"push", "workflow_dispatch"}):
        raise ValueError("duration refresh requires a successful main CI Backend run")
    # Download afresh: do not accept caller-supplied timing/cache authority.
    if directory.exists() and any(directory.iterdir()):
        raise ValueError("duration download directory must be empty")
    subprocess.run(["gh", "run", "download", run_id, "--repo", "HobbyCoders/ai-shuffle",
                    "--pattern", "backend-shard-*", "--dir", str(directory)], check=True)
    receipts, durations = [], {}
    for receipt in sorted(directory.glob("**/receipt.json")):
        receipts.append(json.loads(receipt.read_text()))
        timings = json.loads(receipt.with_name("durations.json").read_text())
        evidence = receipts[-1]
        if len(timings) != evidence["count"] or digest(sorted(timings)) != evidence["selected"]:
            raise ValueError("timings do not match executed shard nodes")
        if durations.keys() & timings.keys():
            raise ValueError("duplicate timing nodes")
        durations.update(timings)
    from scripts.ci.changed_paths import check_gate
    needs = {name: {"result": "success"} for name in
             ("changes", "backend-shard", "backend-contracts", "backend-security")}
    needs["changes"]["outputs"] = {"full_backend": "true"}
    check_gate(needs, receipts, run["headSha"])
    if (len(durations) != sum(r["count"] for r in receipts)
            or digest(sorted(durations)) != receipts[0]["collection"]):
        raise ValueError("timings do not cover collection")
    partition(list(durations), durations)
    output.write_text(json.dumps({"source": run, "durations": durations}, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh", required=True, help="Successful main workflow run ID")
    parser.add_argument("--download-dir", type=Path, required=True, help="New empty scratch directory")
    parser.add_argument("--output", type=Path, default=MANIFEST)
    args = parser.parse_args()
    refresh(args.refresh, args.download_dir, args.output)
