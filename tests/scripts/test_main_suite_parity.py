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
    assert set(config["on"]) == {"push", "workflow_dispatch"}
    assert "main" in config["on"]["push"]["branches"]
    assert config["concurrency"]["cancel-in-progress"] == "true"
    jobs = config["jobs"]
    gate = jobs["main-suite"]
    assert gate["name"] == "Main suite"
    assert gate["if"] == "always()"
    assert set(gate["needs"]) == set(suite.REQUIRED) == set(jobs) - {"main-suite"}
    expected = {
        "backend": "ci-backend", "javascript": "ci-frontend", "fluid": "ci-fluid",
        "windows": "windows-suite", "browser-workspace": "ci-browser-workspace",
        "container": "container-postgres", "identity": "ci-instance-identity",
        "policy": "ci-artifact-policy", "desktop-source": "desktop-source-checks", "desktop": "desktop",
    }
    for job, name in expected.items():
        assert jobs[job]["uses"] == f"./.github/workflows/{name}.yml"
        called = workflow(name + ".yml")
        assert "workflow_call" in called["on"]
        assert called["on"]["pull_request"] == ""
        assert called["permissions"] == {"contents": "read"}
        assert called["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    for job in ["windows", "identity", "desktop"]:
        assert jobs[job]["with"] == {"main_suite": "true"}
    for name in suite.REQUIRED:
        assert jobs[name]["if"] == "github.event_name == 'push' || github.ref == 'refs/heads/main'"
    step = gate["steps"][-1]
    assert step["env"] == {"RESULTS": "${{ toJSON(needs) }}"}
    assert step["run"] == "python scripts/ci/main_suite.py aggregate"


def test_main_suite_has_no_test_secrets_or_paid_runners():
    for name in ["main-suite.yml", "ci-artifact-policy.yml"]:
        config = workflow(name)
        assert "secrets" not in (ROOT / ".github/workflows" / name).read_text()
        for job in config["jobs"].values():
            assert "environment" not in job and "continue-on-error" not in job
            if "uses" not in job:
                assert job["runs-on"] == "ubuntu-latest"


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


def test_complete_main_suite_aggregate_accepts():
    suite.aggregate({name: {"result": "success"} for name in suite.REQUIRED})


@pytest.mark.parametrize("name", suite.REQUIRED)
@pytest.mark.parametrize("result", ["failure", "cancelled", "skipped", None])
def test_missing_or_incomplete_parity_dependency_fails_aggregate(name, result):
    results = {key: {"result": "success"} for key in suite.REQUIRED}
    if result is None:
        del results[name]
    else:
        results[name]["result"] = result
    with pytest.raises(ValueError):
        suite.aggregate(results)
