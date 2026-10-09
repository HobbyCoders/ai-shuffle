"""Exercise the actual owner-only publishing predicates without secrets/deploys."""
from pathlib import Path
import re
from types import SimpleNamespace as NS

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
OWNER = "76577322"
REPO = "HobbyCoders/ai-shuffle"
MAIN = "refs/heads/main"
TAG = "refs/tags/v0.8.19"
DEPLOY = ["deploy-hub.yml", "deploy-license.yml", "deploy-registry.yml", "deploy-site.yml"]
JOBS = [(name, "deploy", "main") for name in DEPLOY] + [
    ("release.yml", "preflight", "tag"),
    ("release.yml", "create-release", "tag"),
    ("release.yml", "source-stable", "tag"),
    ("desktop-installer.yml", "build", "release"),
    ("desktop-installer.yml", "verify", "release"),
    ("desktop-installer.yml", "publish", "release"),
    ("desktop-macos-artifact.yml", "macos", "release"),
    ("docker-publish.yml", "build", "release"),
    ("android-play.yml", "bundle", "release"),
    ("android-play.yml", "publish", "release"),
    ("android-play-enrollment.yml", "export", "main"),
    ("android.yml", "publish", "release"),
    ("withdraw-release.yml", "withdraw", "main"),
]


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


def permits(expression, *, actor_id=OWNER, actor="Quickkill0", triggering_actor=None,
            repo=REPO, event="workflow_dispatch", ref=MAIN, **input_changes):
    """Small evaluation harness for the checked-in, boolean Actions predicates.

    Values are the event context, not a separately implemented authorization rule.
    No GitHub workflow is dispatched, and no credential/signing step is executed.
    """
    inputs = dict(publish_download=True, publish_to_play=True, workers_ready=True,
                  confirm_existing_key_export=True, build_macos=True,
                  release_tag="v0.8.19", candidate_run_id="", launcher_acceptance=False)
    inputs.update(input_changes)
    expression = expression.removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = expression.replace("needs.create-release.", "needs.create_release.")
    expression = re.sub(r"(?<![\w'\"])true(?![\w'\"])", "True", expression)
    expression = re.sub(r"(?<![\w'\"])false(?![\w'\"])", "False", expression)
    context = dict(
        github=NS(repository=repo, actor_id=actor_id, actor=actor,
                  triggering_actor=actor if triggering_actor is None else triggering_actor,
                  event_name=event, ref=ref, event=NS(repository=NS(private=True),
                      workflow_run=NS(event="pull_request", conclusion="success",
                                      head_repository=NS(full_name=REPO)))),
        inputs=NS(**inputs),
        needs=NS(build=NS(result="success"), verify=NS(result="success"),
                 create_release=NS(outputs=NS(promote_latest="true"))),
        startsWith=lambda value, prefix: value.startswith(prefix), always=lambda: True,
    )
    return bool(eval(expression, {"__builtins__": {}}, context))


@pytest.mark.parametrize("name,job,kind", JOBS)
def test_owner_gate_covers_direct_and_reusable_publish_paths(name, job, kind):
    condition = workflow(name)["jobs"][job]["if"]
    ref, event = (TAG, "push") if kind == "tag" else (MAIN, "workflow_dispatch")
    assert permits(condition, ref=ref, event=event)
    for changes in [
        {"actor_id": "7"},
        {"actor_id": "41898282", "actor": "github-actions[bot]"},
        {"triggering_actor": "another-maintainer"},
        {"repo": "someone/ai-shuffle"},
        {"event": "pull_request"},
        {"event": "pull_request_target"},
        {"event": "workflow_run"},
        {"event": "schedule"},
        {"ref": "refs/heads/unreviewed"},
        {"ref": "refs/tags/not-a-release"},
        {"event": "push", "ref": MAIN},
    ]:
        context = dict(ref=ref, event=event)
        context.update(changes)
        assert not permits(condition, **context), (name, job, context)
    if kind == "main":
        assert not permits(condition, ref=TAG)
    elif kind == "tag":
        assert permits(condition, ref=TAG, event="workflow_dispatch")
        assert not permits(condition, ref=MAIN)
    else:
        assert permits(condition, ref=TAG, event="push")


@pytest.mark.parametrize("name,job,required_paths,commands", [
    ("ci-license.yml", "license-server", ["android/license/**"],
     ["npm run typecheck", "npm test"]),
    ("ci-registry.yml", "registry", ["android/registry/**"],
     ["npm run typecheck", "npm test"]),
    ("ci-browser-workspace.yml", "smoke", ["browser-mcp/**", "app/core/browser*.py"],
     ["npm run build --prefix browser-mcp", "npm test --prefix browser-mcp -- test/editors.test.ts"]),
])
def test_zero_failure_paid_checks_keep_automatic_coverage_on_standard_runners(
        name, job, required_paths, commands):
    config = workflow(name)
    assert "workflow_dispatch" in config["on"]
    for path in required_paths:
        assert path in config["on"]["pull_request"]["paths"]
    check = config["jobs"][job]
    assert check["runs-on"] == "ubuntu-24.04"
    assert "continue-on-error" not in check
    assert all("continue-on-error" not in step for step in check["steps"])
    for command in commands:
        assert any(step.get("run") == command for step in check["steps"])
    if name == "ci-browser-workspace.yml":
        assert "ci:full" not in check["if"].replace("github.event.label.name == 'ci:full'", "")
        workspace = config["jobs"]["workspace"]
        assert workspace["needs"] == "smoke"
        assert workspace["runs-on"] == "blacksmith-2vcpu-ubuntu-2404"
        assert "contains(github.event.pull_request.labels.*.name, 'ci:full')" in workspace["if"]


def test_no_automatic_production_site_publication_but_ci_still_automatic():
    for name in DEPLOY:
        assert set(workflow(name)["on"]) == {"workflow_dispatch"}
    for name in ["ci-backend.yml", "ci-frontend.yml", "android.yml"]:
        assert "pull_request" in workflow(name)["on"]
    assert "pull_request" in workflow("android-play.yml")["on"]
    assert "pull_request" in workflow("android-play-enrollment.yml")["on"]


def test_android_signing_steps_are_owner_gated_without_gating_unsigned_ci():
    build = workflow("android.yml")["jobs"]["build"]
    assert build["if"] == "needs.changes.outputs.apk == 'true'"
    assert build["needs"] == "changes"
    guarded = [step for step in build["steps"] if "secrets." in str(step)]
    assert len(guarded) >= 2
    for step in guarded:
        condition = step["if"]
        assert permits(condition)
        assert permits(condition, event="push", ref=TAG)
        assert not permits(condition, actor_id="7")
        assert not permits(condition, triggering_actor="another-maintainer")
        assert not permits(condition, event="pull_request")
        assert not permits(condition, ref="refs/heads/unreviewed")
        assert not permits(condition, publish_download=False)


def test_existing_opt_ins_and_success_dependencies_still_fail_closed():
    for name, job, flag in [
        ("android-play.yml", "publish", "publish_to_play"),
        ("android-play-enrollment.yml", "export", "confirm_existing_key_export"),
        ("android.yml", "publish", "publish_download"),
        ("desktop-macos-artifact.yml", "macos", "build_macos"),
    ]:
        assert not permits(workflow(name)["jobs"][job]["if"], **{flag: False})
    jobs = workflow("desktop-installer.yml")["jobs"]
    assert "needs.verify.result == 'success'" in jobs["publish"]["if"]
    assert "needs.build.result == 'success'" in jobs["verify"]["if"]
    assert not permits(jobs["build"]["if"], candidate_run_id="previous-run")
    for job in ["check", "build", "verify", "publish"]:
        assert not permits(jobs[job]["if"], launcher_acceptance=True), job


def test_launcher_acceptance_is_owner_only_and_publishes_nothing():
    """#1821 D5: the launcher swap is proven on Windows from a branch, without any release power."""
    jobs = workflow("desktop-installer.yml")["jobs"]
    job = jobs["launcher-acceptance"]
    assert permits(job["if"], launcher_acceptance=True)
    assert permits(job["if"], launcher_acceptance=True, ref="refs/heads/fix/1821-launcher-swap")
    assert not permits(job["if"])
    for changes in [{"actor_id": "7"}, {"triggering_actor": "another-maintainer"}, {"repo": "someone/ai-shuffle"},
                    {"event": "pull_request"}, {"event": "push"}, {"event": "workflow_run"}, {"event": "schedule"}]:
        assert not permits(job["if"], launcher_acceptance=True, **changes), changes
    assert job["permissions"] == {"contents": "read"}
    assert "needs" not in job and "environment" not in job
    text = yaml.safe_dump(job)
    assert "secrets." not in text and "vars." not in text
    for forbidden in ["gh release upload", "gh release create", "publish-installer", "wrangler", "git push", "git tag"]:
        assert forbidden not in text, forbidden
    uploads = [step["with"]["name"] for step in job["steps"] if str(step.get("uses", "")).startswith("actions/upload-artifact")]
    assert uploads == ["launcher-swap-evidence"]
    assert "windows" in job["runs-on"]


def test_windows_installer_requires_tagged_unit_tests_before_building():
    jobs = workflow("desktop-installer.yml")["jobs"]
    check = jobs["check"]
    assert check["runs-on"] == "ubuntu-latest"
    assert check["if"] == jobs["build"]["if"]
    assert check["steps"][0]["with"]["ref"] == "refs/tags/${{ inputs.release_tag }}"
    assert any(step.get("run") == "npm ci && npm test" for step in check["steps"])
    assert jobs["build"]["needs"] == "check"
    assert "always()" not in jobs["build"]["if"]
    assert "continue-on-error" not in check
    assert all("continue-on-error" not in step for step in check["steps"])
    assert "windows" in jobs["build"]["runs-on"]
    assert "windows" in jobs["verify"]["runs-on"]
