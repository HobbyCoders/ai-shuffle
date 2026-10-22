"""Partition invariants, actual xdist collection and workflow security contracts."""
from collections import Counter
import json
import os
from pathlib import Path
import random
import subprocess
import sys

import pytest
import yaml

from scripts.ci import backend_shards as bs

ROOT = Path(__file__).resolve().parents[2]


def test_hashed_manifest_keeps_exact_weights_and_unknown_nodes():
    nodes = [f"test_fixture.py::test_value[{i}]" for i in range(12)]
    measured = {node: float(i + 1) for i, node in enumerate(nodes)}
    manifest = {"format": bs.MANIFEST_FORMAT, "durations": bs.hashed_durations(measured)}
    assert not any(node in json.dumps(manifest) for node in nodes)
    assert all(len(key) == 64 for key in manifest["durations"])
    assert bs.manifest_weights(nodes, manifest) == measured
    assert bs.partition(nodes, bs.manifest_weights(nodes, manifest)) == bs.partition(nodes, measured)
    new = nodes + ["test_new.py::test_unknown"]
    weights = bs.manifest_weights(new, manifest)
    assert weights[new[-1]] == 12.0
    assert sorted(node for shard in bs.partition(new, weights) for node in shard) == sorted(new)
    assert bs.manifest_weights(new, {"format": bs.MANIFEST_FORMAT, "durations": {}}) == dict.fromkeys(new, 10.0)


@pytest.mark.parametrize("manifest", [
    {"durations": {}}, {"format": "unknown", "durations": {}},
    {"format": bs.MANIFEST_FORMAT, "durations": {"raw-node-id": 1}},
    {"format": bs.MANIFEST_FORMAT, "durations": {"z" * 64: 1}},
])
def test_malformed_manifest_fails_closed(manifest):
    with pytest.raises(ValueError):
        bs.manifest_weights(list("abcd"), manifest)


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf"), "3", True])
def test_invalid_stale_manifest_weight_is_not_ignored(value):
    manifest = {"format": bs.MANIFEST_FORMAT, "durations": {bs.node_hash("stale-node"): value}}
    with pytest.raises(ValueError, match="invalid duration"):
        bs.manifest_weights(list("abcd"), manifest)


def test_hash_collision_cannot_overwrite_a_duration(monkeypatch):
    monkeypatch.setattr(bs, "node_hash", lambda node: "a" * 64)
    with pytest.raises(ValueError, match="hash collision"):
        bs.hashed_durations({"node-a": 1, "node-b": 2})


def test_deterministic_duration_balancing_and_exact_union():
    nodes = [f"tests/test_a.py::test_{i}" for i in range(100)]
    durations = {node: float(i + 1) for i, node in enumerate(nodes)}
    expected = bs.partition(nodes, durations)
    shuffled = list(nodes)
    random.Random(42).shuffle(shuffled)
    assert bs.partition(shuffled, durations) == expected
    assert Counter(node for shard in expected for node in shard) == Counter(nodes)
    assert all(expected)
    loads = [sum(durations[node] for node in shard) for shard in expected]
    assert max(loads) - min(loads) <= max(durations.values())


def test_unknown_nodes_are_conservative_and_never_dropped():
    nodes = [f"node-{i}" for i in range(16)]
    shards = bs.partition(nodes, {"node-0": 60, "node-1": 0.01})
    assert sorted(node for shard in shards for node in shard) == sorted(nodes)
    assert all(shards)
    # A single known long test must not share with the known short one while
    # another bin could accommodate an unknown conservative long test.
    assert next(s for s in shards if "node-0" in s)


@pytest.mark.parametrize("durations", [{"a": -1}, {"a": float("nan")}, {"a": float("inf")}, {"a": "3"}, {"a": True}])
def test_bad_durations_fail_collection(durations):
    with pytest.raises(ValueError):
        bs.partition(list("abcd"), durations)


@pytest.mark.parametrize("nodes,shards", [
    (list("abcd"), [["a"], ["b"], ["c"], ["c"]]),
    (list("abcd"), [["a"], ["b"], ["c"], []]),
    (list("abcd"), [["a"], ["b"], ["c"]]),
    (list("abcd"), [["a"], ["b"], ["c"], ["d", "e"]]),
    (list("aabc"), [["a"], ["a"], ["b"], ["c"]]),
])
def test_fault_injected_partition(nodes, shards):
    with pytest.raises(ValueError):
        bs.validate_partition(nodes, shards)


@pytest.mark.parametrize("nodes", [[], list("abc"), list("aabcd")])
def test_partition_rejects_empty_or_duplicate_collection(nodes):
    with pytest.raises(ValueError):
        bs.partition(nodes, {})


def test_actual_xdist_collection_producing_path(tmp_path):
    # A real two-worker invocation, outside the repository test tree. The
    # generated tests never recurse into this test file or touch app state.
    fixture = tmp_path / "test_fixture.py"
    fixture.write_text("import pytest\n@pytest.mark.parametrize('value', range(12))\ndef test_value(value):\n    assert value >= 0\n")
    manifest = tmp_path / "durations.json"
    nodes = [f"test_fixture.py::test_value[{i}]" for i in range(12)]
    durations = {node: float(i + 1) for i, node in enumerate(nodes)}
    expected = bs.partition(nodes, durations)
    manifest.write_text(json.dumps({"format": bs.MANIFEST_FORMAT, "durations": bs.hashed_durations(durations)}))
    env = {**os.environ, "PYTHONPATH": str(ROOT), "GITHUB_SHA": "integration-head"}
    # This test is run serially locally: no nested workers beyond the shared
    # machine-wide two-worker limit. Hosted policy tests also run it serially.
    collected = []
    for index in range(1, 5):
        output = tmp_path / f"shard-{index}"
        result = subprocess.run([
            sys.executable, "-m", "pytest", str(fixture), "--rootdir", str(tmp_path),
            "-o", "addopts=", "-p", "scripts.ci.backend_shards", "-n", "2",
            "--backend-shard", str(index), "--backend-durations", str(manifest),
            "--backend-results", str(output), "-q"],
            cwd=tmp_path, env=env, text=True, capture_output=True, timeout=60)
        assert result.returncode == 0, result.stdout + result.stderr
        receipt = json.loads((output / "receipt.json").read_text())
        assert receipt["count"] == len(expected[index - 1])
        assert receipt["selected"] == bs.digest(expected[index - 1])
        assert receipt["total"] == 12
        assert receipt["workers"] == 2
        assert receipt["head"] == "integration-head"
        assert "Partition evidence:" in result.stdout
        collected.extend(json.loads((output / "durations.json").read_text()))
    assert len(collected) == len(set(collected)) == 12


def workflow():
    return yaml.load((ROOT / ".github/workflows/ci-backend.yml").read_text(), Loader=yaml.BaseLoader)


def test_workflow_stable_fail_closed_gate_and_free_runner():
    config = workflow()
    assert "pull_request" in config["on"]
    assert not config["on"]["pull_request"]
    jobs = config["jobs"]
    assert jobs["backend"]["name"] == "Backend tests"
    assert jobs["backend"]["if"] == "always()"
    assert set(jobs["backend"]["needs"]) == {"changes", "backend-shard", "backend-contracts", "backend-security"}
    assert all(job["runs-on"] == "ubuntu-latest" for job in jobs.values())
    assert all("continue-on-error" not in json.dumps(job) for job in jobs.values())
    assert jobs["backend-shard"]["strategy"] == {"fail-fast": "false", "matrix": {"shard": ["1", "2", "3", "4"]}}
    assert jobs["backend-security"]["if"] == "always()"
    assert "needs" not in jobs["backend-security"]
    for job in ("backend-shard", "backend-contracts"):
        assert jobs[job]["if"] == "needs.changes.outputs.full_backend == 'true'"
        assert jobs[job]["timeout-minutes"] == "35"
    text = json.dumps(jobs["backend-shard"])
    for target in ("tests/api", "tests/core", "tests/db", "tests/middleware", "tests/contract", "tests/performance",
                   "tests/mcp", "tests/services", "tests/unit", "tests/scripts", "tests/test_*.py"):
        assert target in text
    for exclusion in ("--deselect=tests/test_scan_blocking_db.py", "--ignore=tests/core/test_chat_shell.py",
                      "--ignore=tests/core/test_container_runtime.py", "--ignore=tests/core/test_container_image_contract.py"):
        assert exclusion in text
    assert '-n 2' in text and 'scripts.ci.backend_shards' in text


def test_all_security_commands_flags_and_timing_artifacts_remain_unconditional():
    jobs = workflow()["jobs"]
    security = jobs["backend-security"]
    text = json.dumps(security)
    for command in ("tests/test_scan_blocking_db.py", "scripts/scan_blocking_db.py",
                    "tests/core/test_chat_shell.py", "tests/core/test_agent_isolation_root.py",
                    "tests/integration/test_postgres_safety.py", "tests/db/test_usage_budget_postgres.py",
                    "tests/core/test_pg_session_store.py", "tests/core/test_pg_session_store_rewind.py",
                    "python -m pip_audit --skip-editable", "python -m pip_audit -r requirements-voice.lock",
                    "tests/scripts", "tests/test_buyer_docs.py", "tests/core/test_docs_content.py"):
        assert command in text
    for flag in ("CHAT_SHELL_REQUIRE_SANDBOX", "AGENT_ISOLATION_ROOT_REQUIRED", "PG_SAFETY_TEST"):
        assert any(step.get("env", {}).get(flag) == "1" for step in security["steps"])
    assert 'chmod -R go-w' in text
    contracts = json.dumps(jobs["backend-contracts"])
    for flag in ("BOOT_SMOKE_REQUIRED", "CONTAINER_RUNTIME_TEST_PG", "CONTAINER_RUNTIME_TEST_PRESSURE", "MOBILE_POSTGRES_TEST", "NETWORK_POSTGRES_TEST"):
        assert flag in contracts
    for name in ("backend-security", "backend-contracts", "backend-shard"):
        for step in jobs[name]["steps"]:
            run = step.get("run", "")
            if "python -m pytest" in run:
                assert "--durations=50" in run and "--junitxml=" in run
        uploads = [s for s in jobs[name]["steps"] if s.get("uses", "").startswith("actions/upload-artifact@")]
        assert len(uploads) == 1 and uploads[0]["if"] == "always()"


@pytest.mark.parametrize("field,value", [("conclusion", "failure"), ("headBranch", "feature"), ("event", "pull_request"), ("workflowName", "Other")])
def test_refresh_rejects_untrusted_or_failed_runs(monkeypatch, tmp_path, field, value):
    run = {"conclusion": "success", "headBranch": "main", "event": "workflow_dispatch", "workflowName": "CI Backend"}
    run[field] = value
    monkeypatch.setattr(bs.subprocess, "check_output", lambda *a, **k: json.dumps(run).encode())
    with pytest.raises(ValueError, match="successful main"):
        bs.refresh("123", tmp_path / "new", tmp_path / "manifest.json")

@pytest.mark.parametrize("fault", [None, "wrong-selected", "wrong-collection", "missing-shard", "missing-node", "duplicate-node"])
def test_main_refresh_downloads_and_validates_complete_execution(monkeypatch, tmp_path, fault):
    run = {"conclusion": "success", "headBranch": "main", "event": "workflow_dispatch",
           "workflowName": "CI Backend", "headSha": "head", "url": "https://example.invalid/run"}
    monkeypatch.setattr(bs.subprocess, "check_output", lambda *a, **k: json.dumps(run).encode())
    nodes = [f"test_a.py::test_{i}" for i in range(8)]
    shards = bs.partition(nodes, {})
    destination = tmp_path / "download"
    def download(command, check):
        assert command[:4] == ["gh", "run", "download", "123"] and check is True
        assert str(destination) == command[-1]
        for index, shard in enumerate(shards, 1):
            if fault == "missing-shard" and index == 4:
                continue
            folder = destination / str(index)
            folder.mkdir(parents=True)
            receipt = {"shard": index, "head": "head", "count": len(shard), "total": len(nodes),
                       "collection": bs.digest(sorted(nodes)), "partition": bs.digest(shards),
                       "selected": bs.digest(shard), "workers": 2}
            timings = {node: 0.5 for node in shard}
            if fault == "wrong-selected" and index == 1:
                receipt["selected"] = "invalid"
            if fault == "wrong-collection":
                receipt["collection"] = "invalid"
            if fault == "missing-node" and index == 1:
                timings.pop(shard[0])
            if fault == "duplicate-node" and index == 2:
                timings = {node: 0.5 for node in shards[0]}
                receipt["selected"] = bs.digest(sorted(timings))
            (folder / "receipt.json").write_text(json.dumps(receipt))
            (folder / "durations.json").write_text(json.dumps(timings))
    monkeypatch.setattr(bs.subprocess, "run", download)
    output = tmp_path / "manifest.json"
    if fault:
        with pytest.raises(ValueError):
            bs.refresh("123", destination, output)
        assert not output.exists()
    else:
        bs.refresh("123", destination, output)
        result = json.loads(output.read_text())
        assert result["source"] == run
        assert result["format"] == bs.MANIFEST_FORMAT
        assert result["durations"] == bs.hashed_durations({node: 0.5 for node in nodes})
        assert not (set(result["durations"]) & set(nodes))


def test_main_refresh_refuses_existing_cache_authority(monkeypatch, tmp_path):
    run = {"conclusion": "success", "headBranch": "main", "event": "workflow_dispatch", "workflowName": "CI Backend"}
    monkeypatch.setattr(bs.subprocess, "check_output", lambda *a, **k: json.dumps(run).encode())
    (tmp_path / "cached.json").write_text("untrusted")
    with pytest.raises(ValueError, match="directory must be empty"):
        bs.refresh("123", tmp_path, tmp_path / "manifest.json")
