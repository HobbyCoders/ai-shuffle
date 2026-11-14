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


def workflow(name="ci.yml"):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def test_every_local_full_stage_has_an_explicit_hosted_mapping():
    assert set(suite.PARITY) == {stage.name for stage in local_ci.SUITES}
    assert {stage for stage, job in suite.PARITY.items() if job is None} == {"android-app"}
    assert set(suite.PARITY.values()) - {None} <= set(suite.REQUIRED)
    # The SDK stage was explicitly not covered in the owner Linux attest. It
    # remains the separate unsigned Android workflow, never claimed as a pass.
    assert local_ci.android_unavailable.__name__ == "android_unavailable"


SUFFIX = "${{ github.event_name == 'schedule' && '-daily' || github.event_name == 'workflow_dispatch' && '-manual' || '' }}"
# A manual run on any ref but main fails closed: nothing runs, so the verdict fails.
ON_MAIN_IF_MANUAL = "github.event_name != 'workflow_dispatch' || github.ref == 'refs/heads/main'"
CALLED = {
    "backend": "ci-backend", "javascript": "ci-frontend", "fluid": "ci-fluid",
    "windows": "windows-suite", "browser-workspace": "ci-browser-workspace",
    "container": "container-postgres", "identity": "ci-instance-identity",
    "policy": "ci-artifact-policy", "desktop-source": "desktop-source-checks", "desktop": "desktop",
    "android": "android", "android-minified": "ci-android-minified",
}


def ci_plan():
    spec = importlib.util.spec_from_file_location("ci_plan", ROOT / "scripts/ci/plan.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_one_workflow_checks_pull_requests_and_main_and_requires_all_parity_jobs():
    config = workflow()
    assert config["name"] == "CI"
    assert config["permissions"] == {"contents": "read"}
    assert set(config["on"]) == {"pull_request", "push", "schedule", "workflow_dispatch"}
    # Every pull request, whatever it changes: the plan decides what runs.
    assert config["on"]["pull_request"] == ""
    assert config["on"]["push"] == {"branches": ["main"]}
    # The daily run skips nothing and cannot displace a waiting push run.
    assert config["on"]["schedule"] == [{"cron": "0 8 * * *"}]
    assert config["concurrency"]["group"] == "ci-${{ github.event.pull_request.number || github.ref }}" + SUFFIX
    # A newer push replaces a pull request's run. On main a burst of merges
    # must not cancel every run and stall the nightly.
    assert config["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    jobs = config["jobs"]
    gate = jobs["ci"]
    assert gate["name"] == "CI"
    # The verdict starts with the run (after the lookup it reads), so it never
    # queues for a runner once the last job finishes; it judges the jobs itself.
    assert gate["if"] == "${{ !cancelled() }}"
    assert gate["needs"] == ["plan"]
    assert set(suite.REQUIRED) == set(jobs) - {"ci", "source-ui", "plan"}
    for job, name in CALLED.items():
        assert jobs[job]["uses"] == f"./.github/workflows/{name}.yml"
        called = workflow(name + ".yml")
        # The daily run never shares a queue with a push run's called workflows.
        assert called["concurrency"]["group"].endswith(SUFFIX)
        # Nothing but ci.yml starts these checks for a pull request or a push:
        # one run, one verdict, and no area starts twice.
        assert set(called["on"]) == {"workflow_call", "workflow_dispatch"}, name
        assert called["permissions"] == {"contents": "read"}
        assert called["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert sorted(job for job, item in jobs.items() if "uses" in item) == sorted([*CALLED, "source-ui"])
    for job in ["identity", "desktop", "policy"]:
        assert jobs[job]["with"] == {"main_suite": "true"}
    assert jobs["windows"]["with"] == {"main_suite": "true", "gated_by_caller": "true"}
    assert jobs["backend"]["with"] == jobs["browser-workspace"]["with"] == {"gated_by_caller": "true"}
    # Publishing stays with the owner's manual run and the release workflow.
    assert "with" not in jobs["android"] and "with" not in jobs["android-minified"]
    for name in suite.REQUIRED:
        if name in suite.PROVABLE:
            # Skipped only when the plan answered "true"; a failed or empty plan runs it.
            assert jobs[name]["needs"] == ["plan"]
            assert jobs[name]["if"] == f"${{{{ !cancelled() && ({ON_MAIN_IF_MANUAL}) && needs.plan.outputs.{name} != 'true' }}}}"
        elif name == "javascript":
            assert jobs[name]["needs"] == ["plan"]
            assert jobs[name]["if"] == f"${{{{ !cancelled() && ({ON_MAIN_IF_MANUAL}) }}}}"
        elif name in suite.MAIN_ONLY:
            assert "needs" not in jobs[name]
            assert jobs[name]["if"] == "github.event_name != 'pull_request' && github.ref == 'refs/heads/main'"
        else:
            assert "needs" not in jobs[name]
            assert jobs[name]["if"] == ON_MAIN_IF_MANUAL
    step = next(step for step in gate["steps"] if step.get("id") == "verdict")
    assert step["env"] == {
        "GH_TOKEN": "${{ github.token }}", "RESULTS": "${{ toJSON(needs) }}",
        "RECORD": "${{ github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository }}"}
    assert step["run"] == "python scripts/ci/main_gate.py"
    source = (ROOT / "scripts/ci/main_gate.py").read_text()
    assert "from ci.main_suite import LOOKUP, REQUIRED, aggregate" in source and "aggregate(results, event)" in source


def test_a_run_skips_only_the_areas_whose_files_already_passed():
    config = workflow()
    jobs = config["jobs"]
    plan = ci_plan()
    lookup = jobs[suite.LOOKUP]
    assert lookup["if"] == ON_MAIN_IF_MANUAL
    assert "needs" not in lookup
    # A failed lookup neither fails the run nor stops it: every area then runs.
    assert lookup["continue-on-error"] == "true"
    assert main_gate().Plan().is_advisory(lookup["name"])
    # Advisory lists change with no file changing, so the blocking audits of the
    # recorded areas always run, with the commands of the areas themselves.
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
    # The plan answers for exactly the areas the verdict may see skipped, and the JavaScript checks.
    areas = [*suite.PROVABLE, "javascript-checks"]
    assert sorted(plan.AREAS) == sorted(areas)
    assert lookup["outputs"] == {**{area: f"${{{{ steps.plan.outputs.{area} }}}}" for area in plan.AREAS},
                                 "records": "${{ steps.plan.outputs.records }}"}
    step = lookup["steps"][-1]
    assert step["id"] == "plan"
    assert step["run"] == 'python3 scripts/ci/plan.py plan >> "$GITHUB_OUTPUT"'
    # The JavaScript workflow always runs: it decides the journeys itself.
    javascript = jobs["javascript"]
    assert javascript["permissions"] == {"contents": "read", "actions": "read", "pull-requests": "read"}
    assert javascript["with"] == {"checks_proven": "${{ needs.plan.outputs.javascript-checks == 'true' }}",
                                  "gated_by_caller": "true"}
    # The verdict job alone records an area's pass, after it judged the area's
    # jobs (and its receipts): one upload step per area, named by the plan.
    gate = jobs["ci"]
    uploads = {step["with"]["name"]: step for step in gate["steps"]
               if step.get("uses", "").startswith("actions/upload-artifact")}
    receipts = {"backend": "backend-receipts", "windows": "windows-receipts"}
    assert sorted(uploads) == sorted(f"${{{{ steps.verdict.outputs.pass-{area} }}}}" for area in plan.AREAS)
    for area in plan.AREAS:
        step = uploads[f"${{{{ steps.verdict.outputs.pass-{area} }}}}"]
        wanted = f"!cancelled() && steps.verdict.outputs.pass-{area} != ''"
        if area in receipts:
            wanted += f" && steps.{receipts[area]}.outcome == 'success'"
        assert step["if"] == f"${{{{ {wanted} }}}}"
        # Losing a record only loses the shortcut; it must never fail a run.
        assert step["continue-on-error"] == "true"
        assert step["with"] == {"name": step["with"]["name"], "path": "pass.txt", "retention-days": "7", "overwrite": "true"}
    ids = [step.get("id") for step in gate["steps"]]
    assert ids.index("verdict") < ids.index("backend-receipts") < ids.index("windows-receipts")
    names = [step.get("name") for step in gate["steps"]]
    assert ids.index("windows-receipts") < names.index("Record that backend passed on these files")
    # The UI journeys alone keep the older whole-tree record (scripts/ci/proven.py).
    called = workflow("ci-frontend.yml")["jobs"]
    for name in CALLED.values():
        records = [key for key, item in workflow(name + ".yml")["jobs"].items()
                   if item.get("uses") == "./.github/workflows/record-pass.yml"]
        assert records == (["record-journeys"] if name == "ci-frontend" else []), name
    assert called["record-journeys"]["with"] == {"area": "journeys", "key": "${{ needs.journey-plan.outputs.key }}"}
    own = "github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name == github.repository"
    record = called["record-journeys"]
    assert record["if"] == " && ".join([*(f"needs.{name}.result == 'success'" for name in record["needs"]), own])
    # These read no recorded pass: they differ per run or have no files of their own.
    assert set(suite.REQUIRED) - set(suite.PROVABLE) == {
        "javascript", "policy", "android-and-docs", "supply-chain", "update-reach"}
    # The non-journey JavaScript jobs are exactly the ones the record stands for.
    checks = set(called["javascript"]["needs"]) - {"journey-plan", "journeys"}
    for name in checks:
        assert called[name]["if"] == "inputs.checks_proven != true"
    assert "if" not in called["journey-plan"]
    excepted = set(plan.AREAS["javascript-checks"]["except"])
    assert excepted == {"javascript / " + called[name].get("name", name).split(" (")[0]
                        for name in ("journey-plan", "journeys")} | {"javascript / record-journeys"}


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
    for name in ["ci.yml", "ci-artifact-policy.yml"]:
        config = workflow(name)
        assert "secrets" not in (ROOT / ".github/workflows" / name).read_text()
        for name, job in config["jobs"].items():
            assert "environment" not in job
            if name == suite.LOOKUP:
                assert job["continue-on-error"] == "true"
            else:
                assert "continue-on-error" not in job
            if "uses" not in job:
                assert job["runs-on"] == "ubuntu-24.04"


def main_gate():
    spec = importlib.util.spec_from_file_location("main_gate", ROOT / "scripts/ci/main_gate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_gate_reports_journeys_on_main_pushes_only_and_never_fails_on_it():
    jobs = workflow()["jobs"]
    gate = jobs["ci"]
    steps = {step.get("id"): step for step in gate["steps"]}
    report = steps["report"]
    # A red sibling shard must not suppress recovered cases from another shard.
    assert report["if"] == "always() && github.event_name == 'push' && github.ref == 'refs/heads/main'"
    assert report["run"] == "python3 -m scripts.devloop.journey_issues"
    assert report["continue-on-error"] == "true"
    assert report["timeout-minutes"] == "2"
    assert gate["steps"][-1]["if"] == "always() && steps.report.outcome == 'failure'"
    assert "::warning" in gate["steps"][-1]["run"]
    # Reporting comes after the verdict and the receipt checks, never before them.
    names = [step.get("id") or step.get("name") for step in gate["steps"]]
    assert names.index("verdict") < names.index("report")
    assert [name for name, job in jobs.items() if job.get("permissions", {}).get("issues") == "write"] == ["ci"]
    # Publishing checks the whole workflow; the reporter step can never fail it.
    assert "github.event.workflow_run.conclusion == 'success'" in workflow("desktop-source.yml")["jobs"]["gate"]["if"]
    uploader = next(step for step in workflow("ci-frontend.yml")["jobs"]["journeys"]["steps"]
                    if step.get("name") == "Upload journey timelines")
    assert uploader["with"]["name"] == "ui-journeys-${{ matrix.shard }}"
    assert uploader["with"]["path"] == "${{ runner.temp }}/ui-journeys/"


def test_the_gate_alone_may_cancel_the_run_and_checks_the_receipts_the_skipped_verdicts_checked():
    jobs = workflow()["jobs"]
    gate = jobs["ci"]
    assert gate["permissions"] == {"contents": "read", "actions": "write", "issues": "write"}
    assert [name for name, job in jobs.items() if job.get("permissions", {}).get("actions") == "write"] == ["ci"]
    # android.yml holds the owner's publishing job, which asks for write access; a
    # caller must allow what a called job asks for. It runs from no event of ci.yml.
    assert [name for name, job in jobs.items() if job.get("permissions", {}).get("contents") == "write"] == ["android"]
    publish = workflow("android.yml")["jobs"]["publish"]
    assert publish["permissions"] == {"contents": "write"}
    assert "inputs.publish_download" in publish["if"] and "github.actor_id == '76577322'" in publish["if"]
    assert "github.event_name == 'pull_request'" not in publish["if"] and "'schedule'" not in publish["if"]
    assert "(github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v'))" in publish["if"]
    # It holds its runner for the whole run, which is longer than any job.
    assert int(gate["timeout-minutes"]) >= 150
    assert gate["runs-on"] == "ubuntu-24.04"
    module = main_gate()
    assert module.GATE == gate["name"] == "CI"
    steps = gate["steps"]
    # The same receipt checks as the called workflows' own verdict jobs.
    backend = workflow("ci-backend.yml")["jobs"]["backend"]["steps"]
    windows = workflow("windows-suite.yml")["jobs"]["desktop-gate"]["steps"]
    commands = "\n".join(step.get("run", "") for step in steps)
    for step in backend + windows:
        for line in step.get("run", "").splitlines():
            if "shard_evidence.py" in line or "backend_gate.py" in line or "--desktop-evidence" in line:
                assert line.strip() in commands
    downloads = [step["with"]["pattern"] for step in steps if step.get("uses", "").startswith("actions/download-artifact")]
    assert downloads == [step["with"]["pattern"] for step in backend + windows
                         if step.get("uses", "").startswith("actions/download-artifact")]
    for step in steps:
        if step.get("uses", "").startswith("actions/download-artifact") or "shard_evidence" in step.get("run", ""):
            # Also after another area failed, so this area's pass can still be recorded.
            assert step["if"] in ("${{ !cancelled() && steps.verdict.outputs.backend == 'true' }}",
                                  "${{ !cancelled() && steps.verdict.outputs.windows == 'true' }}")


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
    results[suite.LOOKUP] = {"result": "success", "outputs": dict(found or {})}
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
        results[suite.LOOKUP] = {"result": "failure", "outputs": {}}
        with pytest.raises(ValueError):
            suite.aggregate(results)
    for area in set(suite.REQUIRED) - set(suite.PROVABLE):
        results = full({area: "true"})
        results[area]["result"] = "skipped"
        with pytest.raises(ValueError):
            suite.aggregate(results)
    results = full()
    del results[suite.LOOKUP]
    with pytest.raises(ValueError):
        suite.aggregate(results)


def test_aggregate_accepts_the_main_only_jobs_skipped_on_a_pull_request_only():
    assert suite.MAIN_ONLY == ("update-reach",)
    for name in suite.MAIN_ONLY:
        results = full()
        results[name]["result"] = "skipped"
        suite.aggregate(results, "pull_request")
        for event in ("push", "schedule", "workflow_dispatch", ""):
            with pytest.raises(ValueError):
                suite.aggregate(results, event)
        results[name]["result"] = "failure"
        with pytest.raises(ValueError):
            suite.aggregate(results, "pull_request")
    # Nothing else may be skipped on a pull request without a recorded pass.
    for name in set(suite.REQUIRED) - set(suite.MAIN_ONLY):
        results = full()
        results[name]["result"] = "skipped"
        with pytest.raises(ValueError):
            suite.aggregate(results, "pull_request")


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
