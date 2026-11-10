"""Keep hosted main's proof in parity with every local full-suite stage."""
import importlib.util
import json
from pathlib import Path
import subprocess
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("main_suite", ROOT / "scripts/ci/main_suite.py")
suite = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(suite)
import local_ci


def workflow(name="main-suite.yml"):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_every_local_full_stage_has_an_explicit_hosted_mapping():
    assert set(suite.PARITY) == {stage.name for stage in local_ci.SUITES}
    assert {stage for stage, job in suite.PARITY.items() if job is None} == {"android-app"}
    assert set(suite.PARITY.values()) - {None} <= set(suite.REQUIRED)
    # The SDK stage was explicitly not covered in the owner Linux attest. It
    # remains the separate unsigned Android workflow, never claimed as a pass.
    assert local_ci.android_unavailable.__name__ == "android_unavailable"


def test_root_calls_existing_workflows_and_requires_all_parity_jobs():
    config = workflow()
    assert config["permissions"] == {"contents": "read"}
    assert set(config["on"]) == {"push", "schedule", "workflow_dispatch"}
    assert config["on"]["push"] == {"branches": ["main"]}
    # The daily run skips nothing and cannot displace a waiting push run.
    assert config["on"]["schedule"] == [{"cron": "0 8 * * *"}]
    assert config["concurrency"]["group"] == (
        "main-suite-${{ github.ref }}${{ github.event_name == 'schedule' && '-daily' || '' }}")
    # A burst of merges must not cancel every run and stall the nightly.
    assert config["concurrency"]["cancel-in-progress"] == "false"
    jobs = config["jobs"]
    gate = jobs["main-suite"]
    assert gate["name"] == "Main suite"
    assert gate["if"] == "always()"
    assert set(gate["needs"]) - {"proven"} == set(suite.REQUIRED) == set(jobs) - {
        "main-suite", "journey-issues", "source-ui", "stop-on-failure", "proven"}
    assert "proven" in gate["needs"]
    expected = {
        "backend": "ci-backend", "javascript": "ci-frontend", "fluid": "ci-fluid",
        "windows": "windows-suite", "browser-workspace": "ci-browser-workspace",
        "container": "container-postgres", "identity": "ci-instance-identity",
        "policy": "ci-artifact-policy", "desktop-source": "desktop-source-checks", "desktop": "desktop",
    }
    for job, name in expected.items():
        assert jobs[job]["uses"] == f"./.github/workflows/{name}.yml"
        called = workflow(name + ".yml")
        # The daily run never shares a queue with a push run's called workflows.
        assert called["concurrency"]["group"].endswith("${{ github.event_name == 'schedule' && '-daily' || '' }}")
        assert "workflow_call" in called["on"]
        assert called["on"]["pull_request"] == ""
        assert called["permissions"] == {"contents": "read"}
        assert called["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    for job in ["windows", "identity", "desktop", "policy"]:
        assert jobs[job]["with"] == {"main_suite": "true"}
    on_main = "github.event_name == 'push' || github.ref == 'refs/heads/main'"
    for name in suite.REQUIRED:
        if name in suite.PROVABLE:
            # Skipped only when the lookup answered "true"; a failed or empty lookup runs it.
            assert jobs[name]["needs"] == ["proven"]
            assert jobs[name]["if"] == f"${{{{ !cancelled() && ({on_main}) && needs.proven.outputs.{name} != 'true' }}}}"
        elif name == "javascript":
            assert jobs[name]["needs"] == ["proven"]
            assert jobs[name]["if"] == f"${{{{ !cancelled() && ({on_main}) }}}}"
        else:
            assert "needs" not in jobs[name]
            assert jobs[name]["if"] == on_main
    step = gate["steps"][-1]
    assert step["env"] == {"RESULTS": "${{ toJSON(needs) }}"}
    assert step["run"] == "python scripts/ci/main_suite.py aggregate"


def test_a_push_skips_only_what_a_pull_request_recorded_for_the_same_files():
    config = workflow()
    jobs = config["jobs"]
    lookup = jobs["proven"]
    assert lookup["if"] == "github.event_name == 'push' || github.ref == 'refs/heads/main'"
    assert "needs" not in lookup
    # A failed lookup neither fails the run nor stops it: every area then runs.
    assert lookup["continue-on-error"] == "true"
    spec = importlib.util.spec_from_file_location("stop_on_failure", ROOT / "scripts/ci/stop_on_failure.py")
    stop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stop)
    assert stop.is_advisory(lookup["name"])
    # Advisory lists change with no file changing, so the blocking audits of the
    # recorded areas always run on main, with the pull-request commands.
    audits = "\n".join(step.get("run", "") for step in jobs["supply-chain"]["steps"])
    backend_audit = next(step for step in workflow("ci-backend.yml")["jobs"]["backend-security"]["steps"]
                         if step.get("name") == "Audit public Python dependencies")
    security = workflow("ci-backend.yml")["jobs"]["backend-security"]["steps"]
    install = next(step for step in security if "pip install -e ./fluid" in step.get("run", ""))
    by_name = {step.get("name"): step for step in jobs["supply-chain"]["steps"]}
    assert by_name["Install what the Python audit inspects"]["run"].split() == install["run"].split()
    assert by_name["Audit public Python dependencies"]["run"].split() == backend_audit["run"].split()
    assert by_name["Audit public Python dependencies"]["timeout-minutes"] == backend_audit["timeout-minutes"]
    # No environment switch makes a recorded backend job do less on a pull request than on main.
    assert "refs/heads/main" not in (ROOT / ".github/workflows/ci-backend.yml").read_text()
    assert "for package in frontend tools browser-mcp; do" in audits
    assert 'npx -y npm@11 audit --audit-level=high' in audits
    javascript_text = (ROOT / ".github/workflows/ci-frontend.yml").read_text()
    assert javascript_text.count("npx -y npm@11 audit --audit-level=high") == 3
    for name in suite.PROVABLE:
        text = (ROOT / ".github/workflows" / jobs[name]["uses"].rsplit("/", 1)[1]).read_text()
        assert (" audit --audit-level" in text or "pip_audit" in text) == (name == "backend")
    assert lookup["permissions"] == {"contents": "read", "actions": "read"}
    areas = [*suite.PROVABLE, "javascript-checks"]
    assert lookup["outputs"] == {area: f"${{{{ steps.check.outputs.{area} }}}}" for area in areas}
    step = lookup["steps"][-1]
    assert step["id"] == "check"
    assert step["run"] == f'python3 scripts/ci/proven.py check --areas {",".join(areas)} >> "$GITHUB_OUTPUT"'
    # The JavaScript workflow always runs: it decides the journeys itself.
    javascript = jobs["javascript"]
    assert javascript["permissions"] == {"contents": "read", "actions": "read"}
    assert javascript["with"] == {"checks_proven": "${{ needs.proven.outputs.javascript-checks == 'true' }}"}
    # Every area main may skip is recorded by exactly one job of its pull-request
    # workflow, for this repository's own pull requests only, after its checks passed.
    recorded = {}
    for job in suite.PROVABLE:
        called = workflow(jobs[job]["uses"].rsplit("/", 1)[1])["jobs"]
        (record,) = [item for item in called.values() if item.get("uses") == "./.github/workflows/record-pass.yml"]
        assert record["with"] == {"area": job}
        recorded[job] = record
    called = workflow("ci-frontend.yml")["jobs"]
    recorded["javascript-checks"] = called["record-checks"]
    recorded["journeys"] = called["record-journeys"]
    assert called["record-checks"]["with"] == {"area": "javascript-checks"}
    assert called["record-journeys"]["with"] == {"area": "journeys", "key": "${{ needs.journey-plan.outputs.key }}"}
    own = "github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name == github.repository"
    for record in recorded.values():
        needs = record["needs"]
        assert record["if"] == " && ".join([*(f"needs.{name}.result == 'success'" for name in needs), own])
    # Main differs from a pull request here, or has no pull-request twin: always run.
    assert set(suite.REQUIRED) - set(suite.PROVABLE) == {
        "javascript", "policy", "desktop", "android-and-docs", "supply-chain", "update-reach"}
    # The non-journey JavaScript jobs are exactly the ones the record stands for.
    checks = set(called["record-checks"]["needs"])
    assert checks == set(called["javascript"]["needs"]) - {"journey-plan", "journeys"}
    for name in checks:
        assert called[name]["if"] == "inputs.checks_proven != true"
    assert "if" not in called["journey-plan"]


def test_record_pass_names_the_record_as_the_lookup_expects():
    spec = importlib.util.spec_from_file_location("proven", ROOT / "scripts/ci/proven.py")
    proven = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(proven)
    config = workflow("record-pass.yml")
    assert set(config["on"]) == {"workflow_call"} and config["permissions"] == {"contents": "read"}
    name, upload = config["jobs"]["record"]["steps"]
    assert 'echo "name=proven-$AREA-$tree${KEY:+-$KEY}" >> "$GITHUB_OUTPUT"' in name["run"]
    assert proven.record_name("$AREA", "$tree") == "proven-$AREA-$tree"
    assert proven.record_name("$AREA", "$tree", "$KEY") == "proven-$AREA-$tree-$KEY"
    assert "git/commits/$GITHUB_SHA" in name["run"] and ".tree.sha" in name["run"]
    assert upload["with"]["name"] == "${{ steps.name.outputs.name }}"
    # Losing a record only loses the shortcut; it must never fail a pull request.
    assert name["continue-on-error"] == upload["continue-on-error"] == "true"


def test_main_suite_has_no_test_secrets_or_paid_runners():
    for name in ["main-suite.yml", "ci-artifact-policy.yml"]:
        config = workflow(name)
        assert "secrets" not in (ROOT / ".github/workflows" / name).read_text()
        for name, job in config["jobs"].items():
            assert "environment" not in job
            if name in ("journey-issues", "stop-on-failure", "proven"):
                assert job["continue-on-error"] == "true"
            else:
                assert "continue-on-error" not in job
            if "uses" not in job:
                assert job["runs-on"] == "ubuntu-latest"


def test_journey_reporter_is_main_push_only_advisory_and_has_the_only_issue_write_permission():
    config = workflow()
    jobs = config["jobs"]
    reporter = jobs["journey-issues"]
    assert reporter["if"] == "always() && github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert reporter["needs"] == ["javascript"]
    assert reporter["continue-on-error"] == "true"
    assert reporter["permissions"] == {"contents": "read", "actions": "read", "issues": "write"}
    assert [name for name, job in jobs.items() if job.get("permissions", {}).get("issues") == "write"] == ["journey-issues"]
    assert "journey-issues" not in jobs["main-suite"]["needs"]
    step = reporter["steps"][1]
    assert step["run"] == "python3 -m scripts.devloop.journey_issues"
    assert step["continue-on-error"] == "true"
    assert step["timeout-minutes"] == "2"
    assert reporter["timeout-minutes"] == "5"
    assert reporter["steps"][-1]["if"] == "always() && (failure() || steps.report.outcome == 'failure')"
    assert "::warning" in reporter["steps"][-1]["run"]
    # Publishing still checks the whole workflow, making job-level tolerance
    # essential even though the reporter is excluded from the final aggregate.
    assert "github.event.workflow_run.conclusion == 'success'" in workflow("desktop-source.yml")["jobs"]["gate"]["if"]
    uploader = next(step for step in workflow("ci-frontend.yml")["jobs"]["journeys"]["steps"]
                    if step.get("name") == "Upload journey timelines")
    assert uploader["with"]["name"] == "ui-journeys-${{ matrix.shard }}"
    assert uploader["with"]["path"] == "${{ runner.temp }}/ui-journeys/"


def test_stop_on_failure_is_advisory_and_holds_the_only_actions_write_permission():
    jobs = workflow()["jobs"]
    watcher = jobs["stop-on-failure"]
    assert watcher["if"] == "github.event_name == 'push' || github.ref == 'refs/heads/main'"
    assert watcher["continue-on-error"] == "true"
    assert "needs" not in watcher and "stop-on-failure" not in jobs["main-suite"]["needs"]
    assert watcher["permissions"] == {"contents": "read", "actions": "write"}
    assert [name for name, job in jobs.items() if job.get("permissions", {}).get("actions") == "write"] == ["stop-on-failure"]
    step = watcher["steps"][-1]
    assert step["run"] == "python3 scripts/ci/stop_on_failure.py"
    assert step["continue-on-error"] == "true"
    # The names the watcher ignores must be the jobs that cannot fail the run.
    spec = importlib.util.spec_from_file_location("stop_on_failure", ROOT / "scripts/ci/stop_on_failure.py")
    stop = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stop)
    assert stop.is_advisory(watcher["name"]) and stop.is_advisory(jobs["journey-issues"]["name"])
    assert stop.is_advisory("source-ui / build") and stop.GATE == jobs["main-suite"]["name"]
    for name in suite.REQUIRED:
        assert not stop.is_advisory(f"{name} / anything") and not stop.is_advisory(name)


def test_parity_jobs_reuse_local_commands_and_strict_reach(monkeypatch):
    calls = []
    for name in ("android_bridge", "android_play", "docs"):
        monkeypatch.setattr(local_ci, name, lambda ctx, name=name: calls.append((name, ctx.mode)))
    suite.main(["android-and-docs"])
    assert calls == [("android_bridge", "full"), ("android_play", "full"), ("docs", "full")]
    jobs = workflow()["jobs"]
    reach = jobs["update-reach"]["steps"][-1]
    assert reach["run"] == "python scripts/ci/main_suite.py update-reach"
    source = (ROOT / "scripts/ci/main_suite.py").read_text()
    assert "update_reach_readiness(revision, root=ROOT)" in source
    assert "if not ready:" in source
    chain = [step.get("run", "") for step in jobs["supply-chain"]["steps"]]
    assert "python scripts/secret_scan.py" in chain
    assert "python scripts/dependency_audit.py" in chain
    policy = workflow("ci-artifact-policy.yml")["jobs"]["policy"]["steps"]
    commands = "\n".join(step.get("run", "") for step in policy)
    for name in ["owner_publication_policy", "edge_publication_gate", "local_ci", "local_ci_preflight",
                 "local_ci_stages", "security_gate", "cloud_split", "main_suite_parity"]:
        assert f"tests/scripts/test_{name}.py" in commands


def test_policy_pr_and_manual_selection_and_dependencies_are_unchanged():
    config = workflow("ci-artifact-policy.yml")
    assert config["on"]["workflow_call"]["inputs"]["main_suite"]["default"] == "false"
    steps = config["jobs"]["policy"]["steps"]
    ordinary = [step["run"] for step in steps if step.get("if") == "inputs.main_suite != true"]
    assert ordinary == [
        "python -m pip install PyYAML==6.0.3 pytest==9.1.1 httpx==0.27.2 cryptography==50.0.1",
        'python -m pytest -q -o addopts="" --confcutdir=tests/scripts '
        "tests/scripts/test_owner_publication_policy.py tests/scripts/test_edge_publication_gate.py",
        # The push gate runs no suites: PRs carry the Android/docs parity checks.
        "python scripts/ci/main_suite.py android-and-docs",
    ]
    expanded = [step["run"] for step in steps if step.get("if") == "inputs.main_suite == true"]
    assert expanded[0] == "pip install -r requirements.txt -r requirements-dev.txt && pip install -e ./fluid"
    assert "tests/scripts/test_main_suite_parity.py" in expanded[1]


def full(found=None):
    results = {name: {"result": "success"} for name in suite.REQUIRED}
    results["proven"] = {"result": "success", "outputs": dict(found or {})}
    return results


def test_complete_main_suite_aggregate_accepts():
    suite.aggregate(full())


def test_aggregate_accepts_a_skip_only_for_an_area_the_lookup_proved():
    for area in suite.PROVABLE:
        results = full({area: "true"})
        results[area]["result"] = "skipped"
        suite.aggregate(results)
        for found in ({}, {area: "false"}, {area: ""}, {"javascript-checks": "true"}):
            results = full(found)
            results[area]["result"] = "skipped"
            with pytest.raises(ValueError):
                suite.aggregate(results)
        # A lookup that failed has no outputs and proves nothing.
        results = full()
        results[area]["result"] = "skipped"
        results["proven"] = {"result": "failure", "outputs": {}}
        with pytest.raises(ValueError):
            suite.aggregate(results)
    for area in set(suite.REQUIRED) - set(suite.PROVABLE):
        results = full({area: "true"})
        results[area]["result"] = "skipped"
        with pytest.raises(ValueError):
            suite.aggregate(results)
    results = full()
    del results["proven"]
    with pytest.raises(ValueError):
        suite.aggregate(results)


@pytest.mark.parametrize("name", suite.REQUIRED)
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", None])
def test_missing_or_incomplete_parity_dependency_fails_aggregate(name, result):
    results = full()
    if result is None:
        del results[name]
    else:
        results[name]["result"] = result
    with pytest.raises(ValueError):
        suite.aggregate(results)
