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
                  release_tag="v0.8.19", candidate_run_id="")
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


def test_no_automatic_production_site_publication_but_ci_still_automatic():
    for name in DEPLOY:
        assert set(workflow(name)["on"]) == {"workflow_dispatch"}
    for name in ["ci-backend.yml", "ci-frontend.yml", "android.yml"]:
        assert "pull_request" in workflow(name)["on"]
    assert "pull_request" in workflow("android-play.yml")["on"]
    assert "pull_request" in workflow("android-play-enrollment.yml")["on"]


def test_android_signing_steps_are_owner_gated_without_gating_unsigned_ci():
    build = workflow("android.yml")["jobs"]["build"]
    assert "if" not in build
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
