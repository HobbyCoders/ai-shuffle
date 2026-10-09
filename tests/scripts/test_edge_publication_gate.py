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
        f"compare/{HEAD}...{REVISION}?per_page=1&page=1": {"files": [{"filename": "app/main.py"}]},
        f"actions/runs?event=workflow_dispatch&head_sha={REVISION}&per_page=100&page=1": {
            "workflow_runs": [], "total_count": 0},
        f"actions/runs?event=pull_request&head_sha={HEAD}&per_page=100&page=1": {
            "workflow_runs": [run], "total_count": 1},
    }
    return SimpleNamespace(directory=tmp_path, data=data, pr=pr, run=run,
                           runs=data[f"actions/runs?event=pull_request&head_sha={HEAD}&per_page=100&page=1"])


def check(evidence, trigger=None, api=None):
    return gate.publication_readiness(api or evidence.data.__getitem__, REVISION, evidence.directory, trigger)



@pytest.fixture
def main_evidence(evidence):
    evidence.data[f"git/commits/{REVISION}"]["tree"]["sha"] = "d" * 40
    run = copy.deepcopy(evidence.run)
    run.update(event="workflow_dispatch", head_sha=REVISION, head_branch="main",
               actor={"id": int(gate.OWNER_ID)}, triggering_actor={"id": int(gate.OWNER_ID)})
    runs = evidence.data[f"actions/runs?event=workflow_dispatch&head_sha={REVISION}&per_page=100&page=1"]
    runs.update(workflow_runs=[run], total_count=1)
    evidence.main_run, evidence.main_runs = run, runs
    return evidence


def test_owner_ci_verifies_the_combined_main_tree(main_evidence):
    ready, reason = check(main_evidence)
    assert ready and "Combined main revision" in reason
    assert check(main_evidence, REVISION)[0]
    assert check(main_evidence, HEAD)[0]


@pytest.mark.parametrize("field,value", [
    ("event", "pull_request"), ("head_sha", HEAD), ("head_branch", "other"),
    ("actor", {"id": 7}), ("triggering_actor", {"id": 7}),
    ("actor", None), ("triggering_actor", None),
    ("head_repository", {"full_name": "other/repo"}),
    ("status", "in_progress"), ("conclusion", "failure"),
    ("conclusion", "cancelled"), ("conclusion", "skipped"),
    ("path", ".github/workflows/unrelated.yml"),
])
def test_main_ci_rejects_untrusted_or_incomplete_evidence(main_evidence, field, value):
    main_evidence.main_run[field] = value
    assert not check(main_evidence)[0]


def test_main_ci_includes_concurrent_merge_and_rename_paths(main_evidence):
    (main_evidence.directory / "extra.yml").write_text(
        "name: Extra\non:\n  pull_request:\n    paths: ['fluid/**']\n", encoding="utf-8")
    main_evidence.data[f"compare/{HEAD}...{REVISION}?per_page=1&page=1"]["files"] = [
        {"filename": "misc/renamed.py", "previous_filename": "fluid/old.py"}]
    ready, reason = check(main_evidence)
    assert not ready and "extra.yml" in reason
    run = {**main_evidence.main_run, "id": 21, "path": ".github/workflows/extra.yml"}
    main_evidence.main_runs.update(workflow_runs=[main_evidence.main_run, run], total_count=2)
    assert check(main_evidence)[0]


def test_capped_comparison_requires_every_main_pr_workflow(main_evidence):
    (main_evidence.directory / "extra.yml").write_text(
        "name: Extra\non:\n  pull_request:\n    paths: ['not-in-returned-files/**']\n", encoding="utf-8")
    main_evidence.data[f"compare/{HEAD}...{REVISION}?per_page=1&page=1"]["files"] = [
        {"filename": f"app/{i}.py"} for i in range(300)]
    assert "extra.yml" in check(main_evidence)[1]


def test_missing_combined_files_and_partial_main_runs_fail_closed(main_evidence):
    comparison = main_evidence.data[f"compare/{HEAD}...{REVISION}?per_page=1&page=1"]
    comparison.pop("files")
    with pytest.raises(ValueError, match="Missing combined-tree"):
        check(main_evidence)
    comparison["files"] = []
    main_evidence.main_runs["total_count"] = 2
    with pytest.raises(ValueError, match="incomplete run history"):
        check(main_evidence)


def test_main_ci_uses_latest_run_and_rechecks_current_revision(main_evidence):
    old = {**main_evidence.main_run, "id": 19}
    main_evidence.main_runs.update(workflow_runs=[old, main_evidence.main_run], total_count=2)
    main_evidence.main_run["conclusion"] = "failure"
    assert not check(main_evidence)[0]
    main_evidence.main_run.update(run_attempt=2, conclusion="success")
    assert check(main_evidence)[0]
    calls = 0
    def api(endpoint):
        nonlocal calls
        if endpoint == "branches/main":
            calls += 1
            if calls == 2:
                return {"protected": True, "commit": {"sha": "e" * 40}}
        return main_evidence.data[endpoint]
    assert not check(main_evidence, api=api)[0]


def test_owner_main_completion_event_reenters_gate(main_evidence):
    event = {"workflow_run": main_evidence.main_run}
    env = event_env("workflow_run")
    assert gate.event_allowed(env, event)
    main_evidence.main_run["triggering_actor"] = {"id": 7}
    assert not gate.event_allowed(env, event)



@pytest.mark.parametrize("field,value,allowed", [
    ("event", "workflow_dispatch", True), ("head_branch", "other", False),
    ("actor.id", 7, False), ("triggering_actor.id", 7, False),
    ("conclusion", "failure", False), ("head_repository.full_name", "other/repo", False),
    # Main moved on: the gate refuses CI for a superseded revision, so the job never starts.
    ("head_sha", "b" * 40, False),
])
def test_workflow_predicate_admits_only_owner_main_ci(field, value, allowed):
    from test_owner_publication_policy import MAIN_SHA, permits, workflow
    expression = workflow("desktop-source.yml")["jobs"]["gate"]["if"]
    fields = {"event": "workflow_dispatch", "head_branch": "main", "actor.id": 76577322,
              "triggering_actor.id": 76577322, "conclusion": "success",
              "head_repository.full_name": gate.REPOSITORY, "head_sha": MAIN_SHA}
    fields[field] = value
    for key, item in fields.items():
        expression = expression.replace("github.event.workflow_run." + key, repr(item))
    assert permits(expression, event="workflow_run") is allowed


@pytest.mark.parametrize("open_pr_heads,allowed", [
    # The PR is still open at this exact commit: it cannot be main's merged PR.
    (["c" * 40], False),
    (["d" * 40, "c" * 40], False),
    # Merged (GitHub lists only open PRs), or the branch has moved on: evaluate.
    ([], True),
    (["d" * 40], True),
])
def test_pr_ci_for_an_open_pr_head_never_starts_the_gate(open_pr_heads, allowed):
    from test_owner_publication_policy import permits, workflow
    expression = workflow("desktop-source.yml")["jobs"]["gate"]["if"]
    assert permits(expression, event="workflow_run", run_head="c" * 40,
                   open_pr_heads=open_pr_heads) is allowed
    # The skip never widens authorization.
    assert not permits(expression, event="workflow_run", run_head="c" * 40, repo="other/repo")


def test_main_ci_event_produces_entrypoint_authorization(main_evidence, monkeypatch, tmp_path):
    root = tmp_path / "repo"
    directory = root / ".github/workflows"
    directory.mkdir(parents=True)
    (directory / "ci.yml").write_text((main_evidence.directory / "ci.yml").read_text(), encoding="utf-8")
    event_file = tmp_path / "main-event.json"
    event_file.write_text(json.dumps({"workflow_run": main_evidence.main_run}), encoding="utf-8")
    output = tmp_path / "output"
    for name, value in {**event_env("workflow_run"), "GITHUB_SHA": REVISION,
                        "GITHUB_EVENT_PATH": str(event_file), "GITHUB_OUTPUT": str(output)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(gate, "ROOT", root)
    monkeypatch.setattr(gate, "GitHub", lambda: main_evidence.data.__getitem__)
    monkeypatch.setattr(gate.subprocess, "check_output", lambda *a, **k: REVISION + "\n")
    gate.main()
    assert output.read_text().strip() == "ready=true"
    main_evidence.main_run["conclusion"] = "failure"
    gate.main()
    assert output.read_text().splitlines()[-1] == "ready=false"


def test_combined_tree_refusal_names_the_workflows_to_dispatch_on_main(main_evidence):
    (main_evidence.directory / "extra.yml").write_text(
        "name: Extra\non:\n  pull_request:\n    paths: ['app/**']\n", encoding="utf-8")
    (main_evidence.directory / "late.yml").write_text(
        "name: Late\non:\n  pull_request:\n    paths: ['app/**']\n", encoding="utf-8")
    # ci.yml passed; late.yml is still running for the owner; extra.yml never ran.
    late = {**main_evidence.main_run, "id": 22, "path": ".github/workflows/late.yml", "status": "in_progress",
            "conclusion": None}
    main_evidence.main_runs.update(workflow_runs=[main_evidence.main_run, late], total_count=2)
    ready, reason = check(main_evidence)
    assert not ready
    assert reason.splitlines() == [
        gate.COMBINED_TREE_REFUSAL + ". CI missing, pending, or unsuccessful: "
        ".github/workflows/extra.yml, .github/workflows/late.yml",
        "Dispatch on main:",
        "gh workflow run extra.yml --ref main",
        "Still running on main: late.yml",
    ]
    # A failed or non-owner run is re-dispatched, never reported as running.
    late.update(status="completed", conclusion="failure")
    assert "gh workflow run late.yml --ref main" in check(main_evidence)[1].splitlines()
    # The refusal names commands; it still never authorizes publication.
    late.update(conclusion="success")
    main_evidence.main_runs.update(workflow_runs=[main_evidence.main_run, late,
                                                  {**late, "id": 23, "path": ".github/workflows/extra.yml"}],
                                   total_count=3)
    assert check(main_evidence)[0]


def test_exact_tree_refusal_is_not_a_combined_tree_refusal(evidence):
    evidence.run["conclusion"] = "failure"
    ready, reason = check(evidence)
    assert not ready and reason == "CI missing, pending, or unsuccessful: " + PATH
    assert not reason.startswith(gate.COMBINED_TREE_REFUSAL)


@pytest.mark.parametrize("combined", [True, False])
def test_entrypoint_annotates_only_a_combined_tree_refusal(evidence, monkeypatch, tmp_path, capsys, combined):
    if combined:
        evidence.data[f"git/commits/{REVISION}"]["tree"]["sha"] = "d" * 40
    else:
        evidence.run["conclusion"] = "failure"
    root = tmp_path / "repo"
    directory = root / ".github/workflows"
    directory.mkdir(parents=True)
    (directory / "ci.yml").write_text((evidence.directory / "ci.yml").read_text(), encoding="utf-8")
    event_file = tmp_path / "push.json"
    event_file.write_text("{}", encoding="utf-8")
    output, summary = tmp_path / "output", tmp_path / "summary"
    for name, value in {**event_env("push"), "GITHUB_SHA": REVISION, "GITHUB_EVENT_PATH": str(event_file),
                        "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(summary)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(gate, "ROOT", root)
    monkeypatch.setattr(gate, "GitHub", lambda: evidence.data.__getitem__)
    monkeypatch.setattr(gate.subprocess, "check_output", lambda *a, **k: REVISION + "\n")
    gate.main()
    assert output.read_text().strip() == "ready=false"
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::warning")]
    if not combined:
        assert warnings == []
        return
    assert warnings == ["::warning title=Edge publication refused::" + gate.COMBINED_TREE_REFUSAL
                        + ". CI missing, pending, or unsuccessful: .github/workflows/ci.yml"
                        + "%0ADispatch on main:%0Agh workflow run ci.yml --ref main"]
    assert "gh workflow run ci.yml --ref main" in summary.read_text()


def test_annotation_escapes_workflow_command_data():
    assert gate.warning_annotation("50% done\r\nnext") == (
        "::warning title=Edge publication refused::50%25 done%0D%0Anext")


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


@pytest.fixture(params=sorted(gate.LABEL_FILTERED_WORKFLOWS))
def label_evidence(evidence, request):
    path = request.param
    name = gate.LABEL_FILTERED_WORKFLOWS[path]
    (evidence.directory / "ci.yml").unlink()
    workflow = evidence.directory / Path(path).name
    workflow.write_text(
        f"name: {name}\nrun-name: {json.dumps(gate.UNRELATED_LABEL_RUN_NAME)}\n"
        "on:\n  pull_request:\n    paths: ['app/**']\n", encoding="utf-8")
    evidence.run.update(path=path, display_title=name)
    noop = {**evidence.run, "id": 21, "display_title": name + gate.UNRELATED_LABEL_SUFFIX}
    evidence.runs.update(workflow_runs=[noop, evidence.run], total_count=2)
    jobs = {"total_count": 2, "jobs": [
        {"id": job_id, "run_id": noop["id"], "head_sha": HEAD,
         "status": "completed", "conclusion": "skipped"} for job_id in [30, 31]]}
    jobs_endpoint = "actions/runs/21/attempts/1/jobs?per_page=100&page=1"
    evidence.data[jobs_endpoint] = jobs
    evidence.workflow, evidence.noop = workflow, noop
    evidence.jobs, evidence.jobs_endpoint = jobs, jobs_endpoint
    return evidence


@pytest.mark.parametrize("noop_conclusion", ["success", "skipped"])
@pytest.mark.parametrize("status,conclusion,ready", [
    ("completed", "success", True), ("completed", "failure", False),
    ("completed", "cancelled", False), ("queued", None, False),
    ("in_progress", None, False),
])
def test_verified_label_noop_preserves_genuine_ci_result(label_evidence, noop_conclusion, status, conclusion, ready):
    label_evidence.run.update(status=status, conclusion=conclusion)
    label_evidence.noop["conclusion"] = noop_conclusion
    assert check(label_evidence)[0] is ready


def test_verified_label_noop_cannot_replace_missing_ci(label_evidence):
    label_evidence.runs.update(workflow_runs=[label_evidence.noop], total_count=1)
    assert not check(label_evidence)[0]


def test_latest_genuine_failure_still_wins_before_label_noop(label_evidence):
    older = {**label_evidence.run, "id": 19}
    label_evidence.run["conclusion"] = "failure"
    label_evidence.runs.update(workflow_runs=[older, label_evidence.noop, label_evidence.run], total_count=3)
    assert not check(label_evidence)[0]
    label_evidence.run.update(run_attempt=2, conclusion="success")
    assert check(label_evidence)[0]


@pytest.mark.parametrize("status,conclusion", [
    ("queued", None), ("in_progress", None), ("completed", "failure"),
    ("completed", "cancelled"), ("completed", "timed_out"),
])
def test_reserved_title_never_suppresses_pending_or_failed_run(label_evidence, status, conclusion):
    label_evidence.noop.update(status=status, conclusion=conclusion)
    # No jobs query is necessary: these runs cannot be ignored in any case.
    del label_evidence.data[label_evidence.jobs_endpoint]
    assert not check(label_evidence)[0]


@pytest.mark.parametrize("field,value", [
    ("status", "in_progress"), ("status", None), ("conclusion", "success"),
    ("conclusion", "failure"), ("conclusion", "cancelled"), ("conclusion", None),
    ("run_id", 20), ("head_sha", "d" * 40),
])
def test_reserved_title_requires_all_jobs_skipped_for_exact_run(label_evidence, field, value):
    label_evidence.jobs["jobs"][1][field] = value
    with pytest.raises(ValueError, match="does not prove all jobs skipped"):
        check(label_evidence)


def test_reserved_title_with_no_jobs_cannot_authorize_ci(label_evidence):
    label_evidence.jobs.update(jobs=[], total_count=0)
    with pytest.raises(ValueError, match="does not prove all jobs skipped"):
        check(label_evidence)


def test_label_noop_job_history_must_be_complete(label_evidence):
    label_evidence.jobs["total_count"] = 3
    with pytest.raises(ValueError, match="incomplete run history"):
        check(label_evidence)


@pytest.mark.parametrize("last_conclusion", ["skipped", "success"])
def test_label_noop_checks_every_job_page(label_evidence, last_conclusion):
    job = label_evidence.jobs["jobs"][0]
    label_evidence.jobs.update(
        jobs=[{**job, "id": 100 + index} for index in range(100)], total_count=101)
    label_evidence.data[label_evidence.jobs_endpoint.replace("&page=1", "&page=2")] = {
        "jobs": [{**job, "id": 200, "conclusion": last_conclusion}], "total_count": 101}
    if last_conclusion == "skipped":
        assert check(label_evidence)[0]
    else:
        with pytest.raises(ValueError, match="does not prove all jobs skipped"):
            check(label_evidence)


def test_label_noop_jobs_api_failure_fails_closed(label_evidence):
    def api(endpoint):
        if endpoint == label_evidence.jobs_endpoint:
            raise RuntimeError("jobs unavailable")
        return label_evidence.data[endpoint]
    with pytest.raises(RuntimeError, match="jobs unavailable"):
        check(label_evidence, api=api)


def test_newer_genuine_ci_does_not_read_obsolete_label_noop_jobs(label_evidence):
    label_evidence.run["id"] = 22
    del label_evidence.data[label_evidence.jobs_endpoint]
    assert check(label_evidence)[0]
    label_evidence.run["conclusion"] = "failure"
    assert not check(label_evidence)[0]


def test_label_noop_checks_the_reported_run_attempt(label_evidence):
    label_evidence.noop["run_attempt"] = 2
    label_evidence.data["actions/runs/21/attempts/2/jobs?per_page=100&page=1"] = label_evidence.data.pop(
        label_evidence.jobs_endpoint)
    assert check(label_evidence)[0]


@pytest.mark.parametrize("change", ["missing_run_name", "changed_run_name", "missing_name", "changed_name"])
def test_reserved_title_without_protected_workflow_contract_fails_closed(label_evidence, change):
    config = yaml.load(label_evidence.workflow.read_text(), Loader=gate.UniqueLoader)
    if change == "missing_run_name": config.pop("run-name")
    if change == "changed_run_name": config["run-name"] = "${{ github.event.pull_request.title }}"
    if change == "missing_name": config.pop("name")
    if change == "changed_name": config["name"] = "Another workflow"
    label_evidence.workflow.write_text(yaml.safe_dump(config), encoding="utf-8")
    with pytest.raises(ValueError, match="Untrusted unrelated-label run marker"):
        check(label_evidence)


@pytest.mark.parametrize("title", [
    "Android CI (unrelated label event)", "CI (unrelated label event)",
])
def test_unknown_workflow_marker_never_hides_latest_failure(evidence, title):
    older = {**evidence.run, "id": 19}
    evidence.run.update(display_title=title, conclusion="failure")
    evidence.runs.update(workflow_runs=[older, evidence.run], total_count=2)
    assert not check(evidence)[0]


@pytest.mark.parametrize("title", [
    "User-controlled (unrelated label event)", "Android CI (unrelated label event) extra", "",
])
def test_unrecognized_titles_are_not_ignored(label_evidence, title):
    label_evidence.noop.update(display_title=title, conclusion="failure")
    assert not check(label_evidence)[0]


@pytest.mark.parametrize("change", ["unprotected", "wrong_tree", "fork", "wrong_run_repo", "wrong_run_head"])
def test_label_noop_does_not_relax_publication_provenance(label_evidence, change):
    if change == "unprotected": label_evidence.data["branches/main"]["protected"] = False
    if change == "wrong_tree": label_evidence.data[f"git/commits/{REVISION}"]["tree"]["sha"] = "d" * 40
    if change == "fork": label_evidence.pr["head"]["repo"]["full_name"] = "other/repo"
    if change == "wrong_run_repo": label_evidence.run["head_repository"] = {"full_name": "other/repo"}
    if change == "wrong_run_head": label_evidence.run["head_sha"] = "d" * 40
    assert not check(label_evidence)[0]


def test_manual_main_run_title_never_suppresses_latest_failure(main_evidence):
    path, name = next(iter(gate.LABEL_FILTERED_WORKFLOWS.items()))
    (main_evidence.directory / "ci.yml").rename(main_evidence.directory / Path(path).name)
    main_evidence.main_run.update(path=path, display_title=name)
    marked = {**main_evidence.main_run, "id": 21, "conclusion": "failure",
              "display_title": name + gate.UNRELATED_LABEL_SUFFIX}
    main_evidence.main_runs.update(workflow_runs=[marked, main_evidence.main_run], total_count=2)
    assert not check(main_evidence)[0]


def test_label_workflows_have_the_exact_protected_run_name_contract():
    for path, name in gate.LABEL_FILTERED_WORKFLOWS.items():
        config = yaml.load((ROOT / path).read_text(), Loader=gate.UniqueLoader)
        assert config["name"] == name
        assert config["run-name"] == gate.UNRELATED_LABEL_RUN_NAME


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
    steps = jobs["publish"]["steps"]
    settle = next(i for i, step in enumerate(steps) if step.get("run") == "sleep 120")
    assert steps[settle + 1]["id"] == "settled"
    assert steps[settle + 1]["run"] == "python scripts/ci/edge_publication_gate.py"
    for step in steps[settle + 2:-1]:
        assert step["if"] == "steps.settled.outputs.ready == 'true'"


@pytest.mark.parametrize("merges", [3, 4])
def test_merge_burst_only_final_revision_can_publish(evidence, merges):
    revisions = [f"{i:040x}" for i in range(1, merges)] + [REVISION]
    for revision in revisions[:-1]:
        ready, reason = gate.publication_readiness(evidence.data.__getitem__, revision, evidence.directory)
        assert not ready and "superseded" in reason
    assert check(evidence)[0]


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


@pytest.fixture
def suite_evidence(evidence):
    """The PR's own CI passed, but ci-backend.yml only ran the affected tests there."""
    (evidence.directory / "ci-backend.yml").write_text(
        "name: CI Backend\non:\n  pull_request:\n    paths: ['app/**']\n", encoding="utf-8")
    backend = {**evidence.run, "id": 30, "path": gate.MAIN_SUITES[0]}
    evidence.runs.update(workflow_runs=[evidence.run, backend], total_count=2)
    main_run = {**evidence.run, "id": 31, "path": gate.MAIN_SUITES[0], "event": "workflow_dispatch",
                "head_sha": REVISION, "head_branch": "main", "actor": {"id": int(gate.OWNER_ID)},
                "triggering_actor": {"id": int(gate.OWNER_ID)}}
    evidence.main_run = main_run
    evidence.main_runs = evidence.data[f"actions/runs?event=workflow_dispatch&head_sha={REVISION}&per_page=100&page=1"]
    return evidence


def test_selected_pr_suite_never_authorizes_publication(suite_evidence):
    ready, reason = check(suite_evidence)
    assert not ready
    assert reason.splitlines() == [
        gate.MAIN_SUITE_REFUSAL + ". CI missing, pending, or unsuccessful: " + gate.MAIN_SUITES[0],
        "Dispatch on main:",
        "gh workflow run ci-backend.yml --ref main",
    ]


def test_owner_full_suite_on_main_plus_pr_ci_publishes(suite_evidence):
    suite_evidence.main_runs.update(workflow_runs=[suite_evidence.main_run], total_count=1)
    ready, reason = check(suite_evidence)
    assert ready and reason.startswith("PR #12")
    # The other workflow still needs the PR's own run.
    suite_evidence.run["conclusion"] = "failure"
    ready, reason = check(suite_evidence)
    assert not ready and reason == "CI missing, pending, or unsuccessful: " + PATH


@pytest.mark.parametrize("field,value", [
    ("actor", {"id": 7}), ("head_branch", "other"), ("conclusion", "failure"), ("status", "in_progress"),
])
def test_main_suite_needs_a_finished_owner_run(suite_evidence, field, value):
    suite_evidence.main_run[field] = value
    suite_evidence.main_runs.update(workflow_runs=[suite_evidence.main_run], total_count=1)
    ready, reason = check(suite_evidence)
    assert not ready and reason.startswith(gate.MAIN_SUITE_REFUSAL)
    if field == "status":
        assert reason.splitlines()[-1] == "Still running on main: ci-backend.yml"


def test_main_suites_cover_a_batch_that_ends_in_a_docs_merge(suite_evidence):
    # Earlier merges changed app/; the last one did not. Its revision still
    # needs the full suites, and they are all it needs.
    suite_evidence.data["pulls/12/files?per_page=100&page=1"] = [{"filename": "docs/internal.md"}]
    assert check(suite_evidence)[1].startswith(gate.MAIN_SUITE_REFUSAL)
    suite_evidence.main_runs.update(workflow_runs=[suite_evidence.main_run], total_count=1)
    assert check(suite_evidence)[0]


def test_entrypoint_annotates_a_main_suite_refusal(suite_evidence, monkeypatch, tmp_path, capsys):
    root = tmp_path / "repo"
    directory = root / ".github/workflows"
    directory.mkdir(parents=True)
    for name in ["ci.yml", "ci-backend.yml"]:
        (directory / name).write_text((suite_evidence.directory / name).read_text(), encoding="utf-8")
    event_file = tmp_path / "push.json"
    event_file.write_text("{}", encoding="utf-8")
    output = tmp_path / "output"
    for name, value in {**event_env("push"), "GITHUB_SHA": REVISION, "GITHUB_EVENT_PATH": str(event_file),
                        "GITHUB_OUTPUT": str(output)}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    monkeypatch.setattr(gate, "ROOT", root)
    monkeypatch.setattr(gate, "GitHub", lambda: suite_evidence.data.__getitem__)
    monkeypatch.setattr(gate.subprocess, "check_output", lambda *a, **k: REVISION + "\n")
    gate.main()
    assert output.read_text().strip() == "ready=false"
    warnings = [line for line in capsys.readouterr().out.splitlines() if line.startswith("::warning")]
    assert warnings == ["::warning title=Edge publication refused::" + gate.MAIN_SUITE_REFUSAL
                        + ". CI missing, pending, or unsuccessful: " + gate.MAIN_SUITES[0]
                        + "%0ADispatch on main:%0Agh workflow run ci-backend.yml --ref main"]


def test_main_suites_are_the_selective_pr_workflows():
    for path in gate.MAIN_SUITES:
        config = yaml.load((ROOT / path).read_text(), Loader=gate.UniqueLoader)
        assert "pull_request" in config["on"] and "workflow_dispatch" in config["on"]
