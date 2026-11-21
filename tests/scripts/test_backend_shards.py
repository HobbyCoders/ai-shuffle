"""Partition invariants, actual xdist collection and workflow security contracts."""
import json
import os
import random
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest
import yaml

from scripts.ci import backend_shards as bs
from scripts.ci.backend_gate import SHARD_WORKERS

ROOT = Path(__file__).resolve().parents[2]
# Tests that start nested two-worker pytest runs. The policy step runs
# tests/scripts with four workers and --timeout=120; with nested workers on the
# same four CPUs, eight runs took 100s on a passing run and over 120s on a
# failing one. Each nested run keeps its own 60s hang guard.
NESTED_XDIST_TIMEOUT = 300


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


@pytest.mark.timeout(NESTED_XDIST_TIMEOUT)
def test_actual_xdist_collection_producing_path(tmp_path):
    # A real two-worker invocation, outside the repository test tree. The
    # generated tests never recurse into this test file or touch app state.
    fixture = tmp_path / "test_fixture.py"
    fixture.write_text("import pytest\n@pytest.mark.parametrize('value', range(12))\ndef test_value(value):\n    assert value >= 0\n")
    manifest = tmp_path / "durations.json"
    nodes = [f"test_fixture.py::test_value[{i}]" for i in range(12)]
    durations = {node: float(i + 1) for i, node in enumerate(nodes)}
    expected = bs.partition(nodes, durations, bs.SHARD_COUNT)
    manifest.write_text(json.dumps({"format": bs.MANIFEST_FORMAT, "durations": bs.hashed_durations(durations)}))
    env = {**os.environ, "PYTHONPATH": str(ROOT), "GITHUB_SHA": "integration-head"}
    # The nested runs go one at a time, each with two workers (the shared
    # machine-wide limit). The hosted policy step runs this file beside three
    # other workers (-n 4 --dist loadfile), hence NESTED_XDIST_TIMEOUT.
    collected = []
    for index in range(1, bs.SHARD_COUNT + 1):
        output = tmp_path / f"shard-{index}"
        result = subprocess.run([
            sys.executable, "-m", "pytest", str(fixture), "--rootdir", str(tmp_path),
            "--confcutdir", str(tmp_path),
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


@pytest.mark.timeout(NESTED_XDIST_TIMEOUT)
def test_failed_tests_leave_a_receipt_naming_them_and_other_faults_leave_none(tmp_path):
    # A complete run whose only fault is failed tests keeps its evidence, for
    # scripts/ci/backend_shard_run.py's one rerun; a collection error does not.
    fixture = tmp_path / "test_fixture.py"
    fixture.write_text("import pytest\n@pytest.mark.parametrize('value', range(12))\n"
                       "def test_value(value):\n    assert value != 3\n")
    manifest = tmp_path / "durations.json"
    nodes = [f"test_fixture.py::test_value[{i}]" for i in range(12)]
    manifest.write_text(json.dumps({"format": bs.MANIFEST_FORMAT, "durations": {}}))
    weights = bs.manifest_weights(nodes, {"format": bs.MANIFEST_FORMAT, "durations": {}})
    shards = bs.partition(nodes, weights, bs.SHARD_COUNT)
    index = next(i for i, shard in enumerate(shards, 1) if "test_fixture.py::test_value[3]" in shard)
    env = {**os.environ, "PYTHONPATH": str(ROOT), "GITHUB_SHA": "integration-head"}

    def run(output, target=fixture):
        return subprocess.run([
            sys.executable, "-m", "pytest", str(target), "--rootdir", str(tmp_path),
            "--confcutdir", str(tmp_path), "-o", "addopts=", "-p", "scripts.ci.backend_shards", "-n", "2",
            "--backend-shard", str(index), "--backend-durations", str(manifest),
            "--backend-results", str(output), "-q"],
            cwd=tmp_path, env=env, text=True, capture_output=True, timeout=60)

    result = run(tmp_path / "failed")
    assert result.returncode == 1, result.stdout + result.stderr
    receipt = json.loads((tmp_path / "failed" / "receipt.json").read_text())
    assert receipt["failed"] == ["test_fixture.py::test_value[3]"]
    assert receipt["count"] == len(shards[index - 1]) and receipt["workers"] == 2
    assert "rerun_passed" not in receipt
    # A collection error elsewhere: both workers agree on the smaller
    # collection and pytest still exits TESTS_FAILED, but there is no receipt.
    (tmp_path / "test_broken.py").write_text("import not_a_module_anywhere\n\ndef test_x():\n    pass\n")
    result = run(tmp_path / "partial", tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert not (tmp_path / "partial" / "receipt.json").exists()
    (tmp_path / "test_broken.py").unlink()
    fixture.write_text("def test_x(:\n")
    result = run(tmp_path / "broken")
    # Nothing collects, so each worker fails inside the partition. xdist then
    # ends with an internal error or with the collection error alone, whichever
    # its controller reads first: never a pass, and never a receipt.
    assert result.returncode != 0, result.stdout + result.stderr
    assert not (tmp_path / "broken" / "receipt.json").exists()


def workflow():
    return yaml.load((ROOT / ".github/workflows/ci-backend.yml").read_text(), Loader=yaml.BaseLoader)


def test_workflow_stable_fail_closed_gate_and_free_runner():
    config = workflow()
    # ci.yml starts it, for a pull request and for main, and records the pass itself.
    assert set(config["on"]) == {"workflow_call", "workflow_dispatch"}
    jobs = config["jobs"]
    assert jobs["backend"]["name"] == "Backend tests"
    assert jobs["backend"]["if"] == "always() && inputs.gated_by_caller != true"
    required = {"backend-select", "backend-shard", "backend-contracts", "backend-security", "backend-postgres"}
    assert set(jobs["backend"]["needs"]) == required
    # backend-map only saves main's test map for pull requests; no verdict waits on it.
    assert set(jobs) == required | {"backend", "backend-map"}
    assert all(job["runs-on"] == "ubuntu-24.04" for job in jobs.values())
    # Only what saves the map may fail: the backend-map job and the shards'
    # upload of their records. Test receipts stay mandatory.
    assert jobs["backend-map"]["continue-on-error"] == "true"
    hits = [step for step in jobs["backend-shard"]["steps"] if "map-hits" in step.get("name", "")]
    assert len(hits) == 1 and hits[0]["continue-on-error"] == "true" and hits[0]["if"] == "env.RECORD_MAP == 'true'"
    assert all("continue-on-error" not in json.dumps(job) for name, job in jobs.items()
               if name not in ("backend-map", "backend-shard"))
    assert all("continue-on-error" not in step for step in jobs["backend-shard"]["steps"] if step is not hits[0])
    # The shards are the ones backend-select chose: all of them, except on a
    # pull request (scripts/ci/backend_gate.py holds every other run to all eight).
    assert jobs["backend-shard"]["strategy"] == {
        "fail-fast": "false", "matrix": {"shard": "${{ fromJSON(needs.backend-select.outputs.shards) }}"}}
    assert jobs["backend-shard"]["needs"] == "backend-select"
    assert jobs["backend-shard"]["if"] == "needs.backend-select.outputs.shards != '[]'"
    # The chooser's count: a rerun of only the failed shards has fewer matrix jobs.
    assert jobs["backend-shard"]["env"]["SHARD_COUNT"] == "${{ needs.backend-select.outputs.count }}"
    assert "job-total" not in json.dumps(jobs)
    assert select_outputs(jobs) >= {"shards", "count", "tests", "selection"}
    # Only a run that publishes the map records it; a pull request never does.
    assert jobs["backend-shard"]["env"]["RECORD_MAP"] == (
        "${{ github.event_name != 'pull_request' && needs.backend-select.outputs.selection == 'all' }}")
    for name in ("backend-security", "backend-postgres"):
        assert jobs[name]["if"] == "always()"
        assert "needs" not in jobs[name]
        assert all("if" not in step for step in jobs[name]["steps"][:-1])
    assert "if" not in jobs["backend-contracts"] and "needs" not in jobs["backend-contracts"]
    for job in ("backend-shard", "backend-contracts"):
        assert jobs[job]["timeout-minutes"] == "35"
    select = jobs["backend-select"]
    assert "if" not in select and "needs" not in select
    choose = next(step for step in select["steps"] if step.get("id") == "choose")
    assert choose["run"] == ('python scripts/ci/backend_test_map.py ci-select --map backend-test-map.json.gz'
                             ' >> "$GITHUB_OUTPUT"')
    restore = next(step for step in select["steps"] if step.get("uses", "").startswith("actions/cache/"))
    # A pull request reads main's newest map and never writes one.
    assert restore["uses"] == "actions/cache/restore@v4" and restore["if"] == "github.event_name == 'pull_request'"
    names = [step.get("name", "") for step in select["steps"]]
    assert "Selected backend shards: ${{ steps.choose.outputs.shards }}" in names
    assert "Backend test selection: ${{ steps.choose.outputs.selection }}" in names
    mapper = jobs["backend-map"]
    assert mapper["if"] == ("github.event_name != 'pull_request' && needs.backend-select.outputs.selection == 'all'"
                            " && needs.backend-shard.result == 'success'")
    assert "--prefix backend-map-hits --count 8" in json.dumps(mapper)
    text = json.dumps(jobs["backend-shard"])
    for target in ("tests/api", "tests/core", "tests/db", "tests/middleware", "tests/contract", "tests/performance",
                   "tests/mcp", "tests/services", "tests/unit", "tests/test_*.py"):
        assert target in text
    for exclusion in ("--deselect=tests/test_scan_blocking_db.py",
                      "--ignore=tests/core/test_container_runtime.py", "--ignore=tests/core/test_container_image_contract.py"):
        assert exclusion in text
    assert f'-n {SHARD_WORKERS}' in text and 'scripts.ci.backend_shards' in text
    run = next(step["run"] for step in jobs["backend-shard"]["steps"] if "backend_shard_run.py" in step.get("run", ""))
    assert '--backend-shard-count "$SHARD_COUNT" --backend-selection "$SELECTION"' in run
    assert 'if [ "$SELECTION" = all ]; then' in run
    assert 'if [ "$RECORD_MAP" = true ]; then\n  record=(-p tests._test_map)' in run
    # The shard's pytest command reruns its failed tests once, through the
    # runner that records the rerun's pass on the receipt (#1748).
    command = next(step["run"] for step in jobs["backend-shard"]["steps"] if step.get("name") == "Run fast test subset")
    # Before it only the targets are chosen (all, or the pull request's files).
    prelude, runner = command.split("python scripts/ci/backend_shard_run.py -- \\\n", 1)
    assert runner.lstrip().startswith("python -m pytest ") and "pytest" not in prelude
    assert command.count("python -m pytest") == 1
    assert "LOCAL_CI_RETRY_FAILED" not in json.dumps(jobs), "the runner sets the retry switch itself"
    for job in ("backend-contracts", "backend-security", "backend-postgres"):
        assert "backend_shard_run.py" not in json.dumps(jobs[job])


def select_outputs(jobs):
    return set(jobs["backend-select"]["outputs"])


def actions_value(expression, event):
    """Evaluate ``${{ github.event_name == 'pull_request' && A || B }}`` the way GitHub does.

    A literal is falsy when it is the number 0, '' or false: `&&` returns its
    left side when that is falsy, `||` its right side when the left is falsy.
    """
    import re
    match = re.fullmatch(r"\$\{\{ github\.event_name == 'pull_request' && (\S+) \|\| (\S+) \}\}", expression)
    assert match, expression

    def literal(text):
        return text[1:-1] if text.startswith("'") else float(text)

    left, right = literal(match[1]), literal(match[2])
    chosen = left if event == "pull_request" else False
    return chosen if chosen not in (0, "", False) else right


def test_a_pull_requests_select_checkout_has_the_history_to_find_the_merge_parents():
    # A number 0 is falsy in an expression: `&& 0 || 1` always gives 1, a
    # shallow merge commit without parents, and every pull request running all.
    checkout = next(step for step in workflow()["jobs"]["backend-select"]["steps"]
                    if step.get("uses", "").startswith("actions/checkout@"))
    depth = checkout["with"]["fetch-depth"]
    assert str(actions_value(depth, "pull_request")) == "0"
    assert str(actions_value(depth, "push")) == "1"
    assert actions_value("${{ github.event_name == 'pull_request' && 0 || 1 }}", "pull_request") == 1


def test_policy_suite_runs_once_in_the_required_security_job():
    jobs = workflow()["jobs"]
    shard = next(s["run"] for s in jobs["backend-shard"]["steps"] if s.get("name") == "Run fast test subset")
    security = jobs["backend-security"]
    policy = next(s for s in security["steps"] if s.get("name") == "Full workflow, publication, security and docs policy regressions")
    assert "backend-security" in jobs["backend"]["needs"]
    assert "if" not in policy and "continue-on-error" not in policy
    for flag, path in (("--ignore", "tests/scripts"), ("--deselect", "tests/test_buyer_docs.py"),
                       ("--ignore", "tests/core/test_docs_content.py")):
        assert f"{flag}={path}" in shard.split()
        assert path not in shard.split()
        assert path in policy["run"].split()


def test_all_security_commands_flags_and_timing_artifacts_remain_unconditional():
    jobs = workflow()["jobs"]
    security = jobs["backend-security"]
    postgres = jobs["backend-postgres"]
    policy_steps = json.dumps(security)
    postgres_steps = json.dumps(postgres)
    for command in ("tests/test_scan_blocking_db.py", "scripts/scan_blocking_db.py",
                    "tests/core/test_agent_isolation_root.py",
                    "python scripts/pip_audit_retry.py --skip-editable",
                    "python scripts/pip_audit_retry.py -r requirements-voice.lock",
                    "tests/scripts", "tests/test_buyer_docs.py", "tests/core/test_docs_content.py"):
        assert command in policy_steps
    for command in ("tests/integration/test_postgres_safety.py", "tests/db/test_usage_budget_postgres.py",
                    "tests/core/test_pg_session_store.py", "tests/core/test_pg_session_store_rewind.py"):
        assert command in postgres_steps
    for job, flag in ((security, "AGENT_ISOLATION_ROOT_REQUIRED"), (postgres, "PG_SAFETY_TEST")):
        assert any(step.get("env", {}).get(flag) == "1" for step in job["steps"])
        assert 'chmod -R go-w' in json.dumps(job)
    contracts = json.dumps(jobs["backend-contracts"])
    for flag in ("BOOT_SMOKE_REQUIRED", "CONTAINER_RUNTIME_TEST_PG", "CONTAINER_RUNTIME_TEST_PRESSURE", "MOBILE_POSTGRES_TEST", "NETWORK_POSTGRES_TEST"):
        assert flag in contracts
    for name in ("backend-security", "backend-postgres", "backend-contracts", "backend-shard"):
        for step in jobs[name]["steps"]:
            run = step.get("run", "")
            if "python -m pytest" in run:
                assert "--durations=50" in run and "--junitxml=" in run
        uploads = [s for s in jobs[name]["steps"] if s.get("uses", "").startswith("actions/upload-artifact@")
                   and not s["with"]["name"].startswith("backend-map-hits-")]
        assert len(uploads) == 1 and uploads[0]["if"] == "always()"


def test_large_fold_ratchets_repeat_everywhere_but_controls_run_once():
    jobs = workflow()["jobs"]
    steps = jobs["backend-contracts"]["steps"]
    label = "Measure large-fold contention on both databases"
    step = next(s for s in steps if s.get("name") == label)
    assert all(s.get("name") != label for s in jobs["backend-postgres"]["steps"])
    assert "backend-contracts" in jobs["backend"]["needs"]
    assert steps.index(step) > next(i for i, s in enumerate(steps) if s.get("name") == "Start the runner's PostgreSQL")
    assert "openssl rand -hex 24" in step["run"]
    assert "::add-mask::%s" in step["run"]
    assert "createdb --owner shuffle_contention_test shuffle_contention_test" in step["run"]
    assert "@127.0.0.1:5432/shuffle_contention_test" in step["run"]
    assert "if" not in step and "continue-on-error" not in step
    assert step["env"]["AI_SHUFFLE_TRANSCRIPT_PRODUCER"] == "1"
    # The same on a pull request and on main: main may skip backend on a pull request's record.
    assert step["env"]["CONTENTION_ATTEMPTS"] == "5"
    run = step["run"]
    assert 'for attempt in $(seq 1 "$CONTENTION_ATTEMPTS"); do' in run
    assert 'selection=()' in run
    assert 'if [ "$attempt" -gt 1 ]; then' in run
    assert "selection=(-k 'test_background_projection or test_large_preparation')" in run
    assert run.count('"${selection[@]}"') == 2
    assert "tests/test_transcript_projection_ingest_budget.py" in run
    assert "tests/db/test_transcript_projection_ingest_postgres.py" in run
    assert "ALLOW_REAL_PG=1 TEST_POSTGRES_DSN=" in run
    assert run.count("--durations=50") == 2
    assert 'reports/large-fold-shim-${attempt}.xml' in run
    assert 'reports/large-fold-pg-${attempt}.xml' in run


def test_native_projection_child_regressions_run_on_the_windows_identity_job():
    native = yaml.safe_load((ROOT / ".github/workflows/ci-instance-identity.yml").read_text())
    steps = native["jobs"]["identity"]["steps"]
    step = next(s for s in steps if s.get("name") == "Exercise native projection child ownership and startup")
    assert "if" not in step and "continue-on-error" not in step
    assert "tests/test_transcript_projection_child_boundary.py" in step["run"]
    assert "tests/test_transcript_projection_preparation.py" in step["run"]
    assert any(s.get("uses") == "actions/setup-node@v4" and s["with"]["node-version"] == "22" for s in steps)


@pytest.mark.parametrize("field,value", [("conclusion", "failure"), ("headBranch", "feature"), ("event", "pull_request"), ("workflowName", "Other")])
def test_refresh_rejects_untrusted_or_failed_runs(monkeypatch, tmp_path, field, value):
    run = {"conclusion": "success", "headBranch": "main", "event": "workflow_dispatch", "workflowName": "CI Backend"}
    run[field] = value
    monkeypatch.setattr(bs.subprocess, "check_output", lambda *a, **k: json.dumps(run).encode())
    with pytest.raises(ValueError, match="successful main"):
        bs.refresh("123", tmp_path / "new", tmp_path / "manifest.json")

@pytest.mark.parametrize("rerun", ["single", "full-rerun", "partial-rerun"])
@pytest.mark.parametrize("fault", [
    None, "wrong-selected", "wrong-collection", "missing-shard", "missing-node", "duplicate-node",
])
def test_main_refresh_downloads_and_validates_complete_execution(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fault: str | None, rerun: str,
) -> None:
    run = {"conclusion": "success", "headBranch": "main", "event": "workflow_dispatch",
           "workflowName": "CI Backend", "headSha": "head", "url": "https://example.invalid/run"}
    monkeypatch.setattr(bs.subprocess, "check_output", lambda *a, **k: json.dumps(run).encode())
    nodes = [f"test_a.py::test_{i}" for i in range(8)]
    shards = bs.partition(nodes, {}, bs.SHARD_COUNT)
    destination = tmp_path / "download"
    def download(command: list[str], check: bool) -> None:
        assert command[:4] == ["gh", "run", "download", "123"] and check is True
        assert command[command.index("--pattern") + 1] == "backend-shard-*-a*"
        assert str(destination) == command[-1]
        for index, shard in enumerate(shards, 1):
            if fault == "missing-shard" and index == 4:
                continue
            receipt = {"shard": index, "head": "head", "count": len(shard), "total": len(nodes),
                       "collection": bs.digest(sorted(nodes)), "partition": bs.digest(shards),
                       "selected": bs.digest(shard), "workers": SHARD_WORKERS, "shards": bs.SHARD_COUNT, "selection": "all"}
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
            attempts = [1, 2] if rerun == "full-rerun" or (
                rerun == "partial-rerun" and index == 2
            ) else [1]
            for attempt in attempts:
                folder = destination / f"backend-shard-{index}-a{attempt}" / "backend-results"
                folder.mkdir(parents=True)
                (folder / "receipt.json").write_text(json.dumps(receipt))
                (folder / "durations.json").write_text(json.dumps({
                    node: seconds * attempt for node, seconds in timings.items()
                }))
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
        expected = {
            node: 1.0 if rerun == "full-rerun" or (
                rerun == "partial-rerun" and index == 2
            ) else 0.5
            for index, shard in enumerate(shards, 1) for node in shard
        }
        assert result["durations"] == bs.hashed_durations(expected)
        assert not (set(result["durations"]) & set(nodes))


def test_main_refresh_refuses_existing_cache_authority(monkeypatch, tmp_path):
    run = {"conclusion": "success", "headBranch": "main", "event": "workflow_dispatch", "workflowName": "CI Backend"}
    monkeypatch.setattr(bs.subprocess, "check_output", lambda *a, **k: json.dumps(run).encode())
    (tmp_path / "cached.json").write_text("untrusted")
    with pytest.raises(ValueError, match="directory must be empty"):
        bs.refresh("123", tmp_path, tmp_path / "manifest.json")
