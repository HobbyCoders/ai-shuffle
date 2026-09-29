"""Edge publication gates use real Actions filters and deny incomplete CI evidence."""
import copy
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("edge_gate", ROOT / "scripts/ci/edge_publication_gate.py")
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)
REVISION = "a" * 40
HEAD = "b" * 40
TREE = "c" * 40
PATH = ".github/workflows/ci.yml"


@pytest.fixture
def evidence(tmp_path):
    (tmp_path / "ci.yml").write_text("name: CI\non:\n  pull_request:\n    paths: ['app/**']\n", encoding="utf-8")
    pr = {"number": 12, "merged_at": "2026-09-29T00:00:00Z", "merge_commit_sha": REVISION,
          "base": {"ref": "main", "repo": {"full_name": gate.REPOSITORY}},
          "head": {"sha": HEAD, "repo": {"full_name": gate.REPOSITORY}}, "changed_files": 1}
    run = {"id": 20, "run_attempt": 1, "path": PATH, "event": "pull_request", "head_sha": HEAD,
           "head_repository": {"full_name": gate.REPOSITORY}, "status": "completed", "conclusion": "success"}
    data = {
        "branches/main": {"protected": True, "commit": {"sha": REVISION}},
        f"commits/{REVISION}/pulls?per_page=100&page=1": [pr],
        "pulls/12": pr,
        f"git/commits/{REVISION}": {"tree": {"sha": TREE}},
        f"git/commits/{HEAD}": {"tree": {"sha": TREE}},
        "pulls/12/files?per_page=100&page=1": [{"filename": "app/main.py"}],
        f"actions/runs?event=pull_request&head_sha={HEAD}&per_page=100&page=1": {
            "workflow_runs": [run], "total_count": 1},
    }
    return SimpleNamespace(directory=tmp_path, data=data, pr=pr, run=run,
                           runs=data[f"actions/runs?event=pull_request&head_sha={HEAD}&per_page=100&page=1"])


def check(evidence, trigger=None, api=None):
    return gate.publication_readiness(api or evidence.data.__getitem__, REVISION, evidence.directory, trigger)


def test_green_exact_merged_tree_can_publish(evidence):
    assert check(evidence)[0]
    assert check(evidence, HEAD)[0]


@pytest.mark.parametrize("field,value", [("protected", False), ("commit", {"sha": "d" * 40})])
def test_protected_current_main_is_required(evidence, field, value):
    evidence.data["branches/main"][field] = value
    assert not check(evidence)[0]


@pytest.mark.parametrize("change", ["unmerged", "wrong_merge", "base_branch", "base_repo", "fork", "deleted_repo", "duplicate"])
def test_merged_pr_provenance_is_required(evidence, change):
    pr = evidence.pr
    if change == "unmerged": pr["merged_at"] = None
    if change == "wrong_merge": pr["merge_commit_sha"] = "d" * 40
    if change == "base_branch": pr["base"]["ref"] = "other"
    if change == "base_repo": pr["base"]["repo"]["full_name"] = "other/repo"
    if change == "fork": pr["head"]["repo"]["full_name"] = "other/repo"
    if change == "deleted_repo": pr["head"]["repo"] = None
    if change == "duplicate": evidence.data[f"commits/{REVISION}/pulls?per_page=100&page=1"].append(pr)
    assert not check(evidence)[0]


def test_no_pr_or_unrelated_workflow_completion_cannot_publish(evidence):
    assert not check(evidence, "d" * 40)[0]
    evidence.data[f"commits/{REVISION}/pulls?per_page=100&page=1"] = []
    assert not check(evidence)[0]


def test_merge_changes_not_in_tested_head_are_refused(evidence):
    evidence.data[f"git/commits/{REVISION}"]["tree"]["sha"] = "d" * 40
    assert not check(evidence)[0]


@pytest.mark.parametrize("status,conclusion", [("queued", None), ("in_progress", None),
    ("completed", "failure"), ("completed", "cancelled"), ("completed", "timed_out"),
    ("completed", "skipped"), ("completed", "neutral"), ("completed", "action_required")])
def test_only_successful_completed_ci_counts(evidence, status, conclusion):
    evidence.run.update(status=status, conclusion=conclusion)
    assert not check(evidence)[0]


@pytest.mark.parametrize("field,value", [("event", "push"), ("head_sha", "d" * 40),
    ("head_repository", {"full_name": "other/repo"}), ("path", ".github/workflows/unrelated.yml")])
def test_ci_must_be_the_expected_pr_run(evidence, field, value):
    evidence.run[field] = value
    assert not check(evidence)[0]


def test_missing_workflow_cannot_be_hidden_by_another_green_run(evidence):
    (evidence.directory / "second.yml").write_text("name: Second\non:\n  pull_request:\n", encoding="utf-8")
    ready, reason = check(evidence)
    assert not ready and "second.yml" in reason


def test_latest_run_wins_over_old_success_and_new_retry_can_recover(evidence):
    old = copy.deepcopy(evidence.run)
    old["id"] -= 1
    evidence.runs["workflow_runs"].append(old)
    evidence.runs["total_count"] += 1
    evidence.run["conclusion"] = "failure"
    assert not check(evidence)[0]
    evidence.run.update(run_attempt=2, conclusion="success")
    assert check(evidence)[0]


def test_missing_all_runs_is_not_success(evidence):
    evidence.runs.update(workflow_runs=[], total_count=0)
    assert not check(evidence)[0]


def test_partial_files_or_run_history_fail_closed(evidence):
    evidence.pr["changed_files"] = 2
    with pytest.raises(ValueError, match="Incomplete PR file"):
        check(evidence)
    evidence.pr["changed_files"] = 1
    evidence.runs["total_count"] = 2
    with pytest.raises(ValueError, match="incomplete run history"):
        check(evidence)


def test_main_is_rechecked_after_reading_ci(evidence):
    calls = 0
    def api(endpoint):
        nonlocal calls
        if endpoint == "branches/main":
            calls += 1
            if calls == 2:
                return {"protected": True, "commit": {"sha": "d" * 40}}
        return evidence.data[endpoint]
    assert not check(evidence, api=api)[0]


def test_api_failure_is_not_success(evidence):
    with pytest.raises(RuntimeError, match="unavailable"):
        check(evidence, api=lambda _: (_ for _ in ()).throw(RuntimeError("unavailable")))


def test_rename_checks_deleted_path_as_well_as_new_path(evidence):
    evidence.data["pulls/12/files?per_page=100&page=1"] = [
        {"filename": "misc/main.py", "previous_filename": "app/main.py"}]
    assert check(evidence)[0]


def test_no_applicable_ci_does_not_authorize_publication(evidence):
    evidence.data["pulls/12/files?per_page=100&page=1"] = [{"filename": "docs/internal.md"}]
    assert not check(evidence)[0]


@pytest.mark.parametrize("value,pattern,expected", [
    ("app/main.py", "app/**", True), ("app/nested/main.py", "app/*", False),
    ("app/main.py", "app/**/*.py", True), ("app/nested/main.py", "app/**/*.py", True),
    ("requirements-native.lock", "requirements*.lock", True),
    ("x.yml", "*.yml", True), ("a/x.yml", "*.yml", False),
])
def test_actions_glob_matching(value, pattern, expected):
    assert gate.glob_match(value, pattern) is expected


def test_filters_honor_negation_ignore_and_target_branch(tmp_path):
    (tmp_path / "filtered.yml").write_text("""name: Filtered
on:
  pull_request:
    branches: [main]
    paths: ['app/**', '!app/tests/**', 'app/tests/required.py']
""", encoding="utf-8")
    assert gate.applicable_workflows(tmp_path, ["app/main.py"])
    assert not gate.applicable_workflows(tmp_path, ["app/tests/example.py"])
    assert gate.applicable_workflows(tmp_path, ["app/tests/required.py"])
    (tmp_path / "filtered.yml").write_text("""name: Filtered
on:
  pull_request:
    paths-ignore: ['docs/**']
""", encoding="utf-8")
    assert not gate.applicable_workflows(tmp_path, ["docs/a.md"])
    assert gate.applicable_workflows(tmp_path, ["docs/a.md", "app/main.py"])
    (tmp_path / "filtered.yml").write_text("name: Other\non:\n  pull_request:\n    branches: [other]\n", encoding="utf-8")
    assert not gate.applicable_workflows(tmp_path, ["app/main.py"])


def test_unsupported_filters_and_duplicate_yaml_fail_closed(tmp_path):
    with pytest.raises(ValueError, match="Unsupported Actions filter"):
        gate.glob_match("foo", "[a-z]*")
    (tmp_path / "duplicate.yml").write_text("name: One\non: {}\non: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate workflow key"):
        gate.applicable_workflows(tmp_path, ["app/main.py"])


def test_pagination_reads_all_pages_and_refuses_truncation():
    calls = []
    def api(endpoint):
        calls.append(endpoint)
        return [1] * 100 if endpoint.endswith("page=1") else [2]
    assert len(gate.pages(api, "example")) == 101
    assert len(calls) == 2
    with pytest.raises(ValueError, match="pagination bound"):
        gate.pages(api, "example", limit=1)


def event_env(kind):
    return {"GITHUB_REPOSITORY": gate.REPOSITORY, "GITHUB_REF": "refs/heads/main",
            "GITHUB_EVENT_NAME": kind, "GITHUB_ACTOR_ID": gate.OWNER_ID,
            "GITHUB_ACTOR": "Quickkill0", "GITHUB_TRIGGERING_ACTOR": "Quickkill0"}


def test_event_authority_and_manual_owner_gate():
    assert gate.event_allowed(event_env("push"), {})
    assert not gate.event_allowed(event_env("push"), {"deleted": True})
    env = event_env("workflow_dispatch")
    assert gate.event_allowed(env, {})
    for key, value in [("GITHUB_ACTOR_ID", "7"), ("GITHUB_TRIGGERING_ACTOR", "other"),
                       ("GITHUB_REPOSITORY", "other/repo"), ("GITHUB_REF", "refs/heads/other")]:
        assert not gate.event_allowed({**env, key: value}, {})
    for kind in ["pull_request", "pull_request_target", "schedule"]:
        assert not gate.event_allowed(event_env(kind), {})
    event = {"workflow_run": {"event": "pull_request", "conclusion": "success",
                              "head_repository": {"full_name": gate.REPOSITORY}}}
    assert gate.event_allowed(event_env("workflow_run"), event)
    for key, value in [("event", "push"), ("conclusion", "failure"),
                       ("head_repository", {"full_name": "other/repo"})]:
        bad = copy.deepcopy(event)
        bad["workflow_run"][key] = value
        assert not gate.event_allowed(event_env("workflow_run"), bad)


def test_real_entrypoint_writes_authorization_from_produced_event(evidence, monkeypatch, tmp_path):
    root = tmp_path / "repo"
    directory = root / ".github/workflows"
    directory.mkdir(parents=True)
    (directory / "ci.yml").write_text((evidence.directory / "ci.yml").read_text(), encoding="utf-8")
    event_file = tmp_path / "event.json"
    event_file.write_text(json.dumps({"workflow_run": {"event": "pull_request", "conclusion": "success",
        "head_repository": {"full_name": gate.REPOSITORY}, "head_sha": HEAD}}), encoding="utf-8")
    output = tmp_path / "output"
    for name, value in {**event_env("workflow_run"), "GITHUB_SHA": REVISION,
                        "GITHUB_EVENT_PATH": str(event_file), "GITHUB_OUTPUT": str(output)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(gate, "ROOT", root)
    monkeypatch.setattr(gate, "GitHub", lambda: evidence.data.__getitem__)
    monkeypatch.setattr(gate.subprocess, "check_output", lambda *a, **k: REVISION + "\n")
    gate.main()
    assert output.read_text().strip() == "ready=true"
    evidence.run["conclusion"] = "failure"
    gate.main()
    assert output.read_text().splitlines()[-1] == "ready=false"


def test_real_workflow_is_immutable_and_only_edge_publisher_can_use_secrets():
    config = yaml.load((ROOT / ".github/workflows/desktop-source.yml").read_text(), Loader=gate.UniqueLoader)
    assert config["on"]["push"]["branches"] == ["main"]
    assert config["on"]["workflow_run"]["types"] == ["completed"]
    all_ci = {yaml.load(path.read_text(), Loader=gate.UniqueLoader)["name"]
              for path in (ROOT / ".github/workflows").glob("*.yml")
              if "pull_request" in yaml.load(path.read_text(), Loader=gate.UniqueLoader).get("on", {})}
    assert set(config["on"]["workflow_run"]["workflows"]) == all_ci
    assert config["permissions"]["contents"] == "read"
    assert "concurrency" not in config
    jobs = config["jobs"]
    assert jobs["publish"]["concurrency"]["cancel-in-progress"] == "false"
    assert "concurrency" not in jobs["gate"]
    assert "environment" not in jobs["gate"]
    assert "secrets." not in str(jobs["gate"])
    assert jobs["publish"]["needs"] == "gate"
    assert jobs["publish"]["if"] == "needs.gate.outputs.ready == 'true'"
    for job in jobs.values():
        checkout = job["steps"][0]
        assert checkout["with"]["ref"] == "${{ github.sha }}"
        assert checkout["with"]["persist-credentials"] == "false"
        assert not any("download-artifact" in step.get("uses", "") for step in job["steps"])
    step = jobs["publish"]["steps"][-1]
    assert step["if"] == "steps.ci.outputs.ready == 'true'"
    assert step["env"]["SOURCE_CHANNEL"] == "edge"
    assert jobs["publish"]["steps"][-2]["run"] == "python scripts/ci/edge_publication_gate.py"


def test_workflow_event_predicate_executes_for_authorized_events():
    from test_owner_publication_policy import permits, workflow
    expression = workflow("desktop-source.yml")["jobs"]["gate"]["if"]
    for event in ["push", "workflow_dispatch", "workflow_run"]:
        assert permits(expression, event=event)
        assert not permits(expression, event=event, repo="other/repo")
        assert not permits(expression, event=event, ref="refs/heads/unreviewed")
    assert not permits(expression, actor_id="7")
    assert not permits(expression, triggering_actor="other")
    for event in ["pull_request", "pull_request_target", "schedule"]:
        assert not permits(expression, event=event)
