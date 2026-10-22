"""Exercise the actual owner-only publishing predicates without secrets/deploys."""
import functools
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
MAIN_SHA = "a" * 40
JOBS = [
    ("release.yml", "preflight", "tag"),
    ("platform-installer.yml", "publish", "tag"),
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


def permits(expression, *, actor_id=OWNER, actor="example-maintainer", triggering_actor=None,
            repo=REPO, event="workflow_dispatch", ref=MAIN, sha=MAIN_SHA, run_head=MAIN_SHA,
            open_pr_heads=(), **input_changes):
    """Small evaluation harness for the checked-in, boolean Actions predicates.

    Values are the event context, not a separately implemented authorization rule.
    No GitHub workflow is dispatched, and no credential/signing step is executed.
    """
    inputs = dict(publish_download=True, publish_to_play=True, workers_ready=True,
                  confirm_existing_key_export=True, build_macos=True,
                  release_tag="v0.8.19", candidate_run_id="", launcher_acceptance=False, old_installer_sha256="",
                  stage_only=False, promote="")
    inputs.update(input_changes)
    expression = expression.removeprefix("${{").removesuffix("}}").strip()
    expression = expression.replace("&&", " and ").replace("||", " or ")
    expression = expression.replace("needs.create-release.", "needs.create_release.")
    expression = expression.replace("needs.android-build.", "needs.android_build.")
    expression = re.sub(r"(?<![\w'\"])true(?![\w'\"])", "True", expression)
    expression = re.sub(r"(?<![\w'\"])false(?![\w'\"])", "False", expression)
    expression = re.sub(r"!(?!=)", " not ", expression)
    # Object filter: ``a.list.*.b.c`` is the list of each item's ``b.c``.
    expression = re.sub(r"([\w.]+)\.\*\.([\w.]+)", r"[_path(item, '\2') for item in \1]", expression)
    context = dict(
        github=NS(repository=repo, actor_id=actor_id, actor=actor, sha=sha,
                  triggering_actor=actor if triggering_actor is None else triggering_actor,
                  event_name=event, ref=ref, event=NS(repository=NS(private=True),
                      workflow_run=NS(event="pull_request", conclusion="success", head_sha=run_head,
                                      pull_requests=[NS(head=NS(sha=head)) for head in open_pr_heads],
                                      head_repository=NS(full_name=REPO)))),
        inputs=NS(**inputs),
        needs=NS(build=NS(result="success"), verify=NS(result="success"), android_build=NS(result="skipped"),
                 create_release=NS(outputs=NS(promote_latest="true"))),
        startsWith=lambda value, prefix: value.startswith(prefix), always=lambda: True, cancelled=lambda: False,
        contains=lambda items, value: value in items,
        format=lambda template, *values: template.format(*values),
        _path=lambda item, path: functools.reduce(getattr, path.split("."), item),
    )
    # Globals, so that object-filter comprehensions can resolve the context.
    return bool(eval(expression, {"__builtins__": {}, **context}))


@pytest.mark.parametrize("name,job,kind", JOBS)
def test_owner_gate_covers_direct_and_reusable_publish_paths(name, job, kind):
    condition = workflow(name)["jobs"][job]["if"]
    ref, event = (TAG if kind == "tag" else MAIN), "workflow_dispatch"
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
        assert not permits(condition, ref=TAG, event="push")
    else:
        assert permits(condition, ref=TAG, event="push")


@pytest.mark.parametrize("job", ["bundle", "publish"])
def test_play_accepts_manual_release_caller_only_for_its_exact_tag(job):
    condition = workflow("android-play.yml")["jobs"][job]["if"]
    assert permits(condition, ref=TAG, event="workflow_dispatch")
    assert not permits(condition, ref=TAG, release_tag="")
    assert not permits(condition, ref=TAG, release_tag="v0.8.18")
    assert not permits(condition, ref=TAG, actor_id="7")


@pytest.mark.parametrize("name,job,commands", [
    ("ci-browser-workspace.yml", "smoke",
     ["npm run build --prefix browser-mcp", "npm test --prefix browser-mcp -- test/editors.test.ts"]),
])
def test_pr_checks_stay_on_standard_runners_and_fail_loudly(name, job, commands):
    config = workflow(name)
    assert set(config["on"]) == {"pull_request", "workflow_dispatch"}
    check = config["jobs"][job]
    assert check["runs-on"] == "ubuntu-latest"
    assert "continue-on-error" not in check
    assert all("continue-on-error" not in step for step in check["steps"])
    for command in commands:
        assert any(step.get("run") == command for step in check["steps"])


@pytest.mark.parametrize("job,minutes,lockfiles", [
    ("smoke", 15, ["browser-mcp/package-lock.json"]),
    ("workspace", 25, ["frontend/package-lock.json", "browser-mcp/package-lock.json"]),
])
def test_browser_jobs_have_cold_install_budget_and_lockfile_cache(job, minutes, lockfiles):
    check = workflow("ci-browser-workspace.yml")["jobs"][job]
    assert check["timeout-minutes"] == str(minutes)
    setup_node = next(step for step in check["steps"]
                      if step.get("uses", "").startswith("actions/setup-node@"))
    assert setup_node["with"]["cache"] == "npm"
    assert setup_node["with"]["cache-dependency-path"].splitlines() == lockfiles


@pytest.mark.parametrize("step_name,directory", [
    ("Frontend tests and contract consistency", "frontend"),
    ("Build AI tools from source", "tools"),
    ("Browser MCP build and tests", "browser-mcp"),
])
def test_javascript_audits_use_npm_11_without_reducing_scope(step_name, directory):
    jobs = workflow("ci-frontend.yml")["jobs"]
    job = jobs["browser-mcp" if directory == "browser-mcp" else "frontend"]
    step = next(step for step in job["steps"] if step.get("name") == step_name)
    assert step.get("working-directory", job.get("defaults", {}).get("run", {}).get("working-directory")) == directory
    audits = [command.strip() for command in re.split(r"\n|&&", step["run"])
              if re.search(r"\baudit\b", command)]
    assert audits == ["npx -y npm@11 audit --audit-level=high"]


def test_fluid_ripgrep_uses_signed_ubuntu_sources_not_optional_runner_feeds():
    steps = workflow("ci-fluid.yml")["jobs"]["unit"]["steps"]
    install = next(step for step in steps if step.get("name") == "Install ripgrep")
    command = install["run"]
    assert "Dir::Etc::sourcelist=sources.list.d/ubuntu.sources" in command
    assert "Dir::Etc::sourceparts=-" in command
    assert "sudo apt-get install -y -qq ripgrep" in command
    assert install["timeout-minutes"] == "4"
    assert "timeout --kill-after=10s 90s sh -c" in command
    assert "for attempt in 1 2; do" in command
    assert command.rstrip().endswith("exit 1")
    assert "--allow-unauthenticated" not in command
    assert "continue-on-error" not in install


def test_javascript_chromium_setup_is_cached_bounded_and_sandboxed():
    jobs = workflow("ci-frontend.yml")["jobs"]
    assert jobs["frontend"]["timeout-minutes"] == "20"
    job = jobs["browser-mcp"]
    assert job["runs-on"] == "ubuntu-latest"
    assert job["timeout-minutes"] == "15"
    assert "needs" not in job and "if" not in job
    assert "continue-on-error" not in job
    steps = job["steps"]
    setup = next(step for step in steps if step.get("name") == "Prepare sandboxed Chromium")
    cache = next(step for step in steps if step.get("name") == "Cache managed Chromium")
    tests = next(step for step in steps if step.get("name") == "Browser MCP build and tests")
    assert cache["uses"] == "actions/cache@v4"
    assert cache["with"]["path"] == "~/.cache/ms-playwright"
    assert "hashFiles('browser-mcp/package-lock.json')" in cache["with"]["key"]
    assert steps.index(cache) < steps.index(setup) < steps.index(tests)
    assert setup["timeout-minutes"] == "4"
    assert "if" not in setup  # OS dependencies are needed even on a cache hit.
    assert "sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0" in setup["run"]
    assert "for attempt in 1 2; do" in setup["run"]
    assert "timeout --kill-after=10s 90s npx --no-install playwright-core install --with-deps chromium" in setup["run"]
    assert 'echo "::error::Chromium dependency installation failed' in setup["run"]
    assert setup["run"].rstrip().endswith("exit 1")
    assert tests["timeout-minutes"] == "8"
    assert "timeout --kill-after=10s 420s npm test" in tests["run"]
    assert 'exit "$result"' in tests["run"]
    assert "::error::Browser MCP tests failed" in tests["run"]
    assert all("continue-on-error" not in step for step in steps)
    editor = (ROOT / "browser-mcp/test/editors.test.ts").read_text()
    assert "headless: true, chromiumSandbox: true" in editor


def test_pr_triggers_are_only_for_read_only_secret_free_checks():
    assert set(workflow("release.yml")["on"]) == {"workflow_dispatch"}
    allowed = {
        "ci-backend.yml", "ci-fluid.yml", "ci-frontend.yml", "desktop.yml",
        "desktop-source-checks.yml", "windows-suite.yml", "ci-instance-identity.yml",
        "ci-artifact-policy.yml", "ci-browser-workspace.yml", "container-postgres.yml",
    }
    found = set()
    for path in sorted((ROOT / ".github/workflows").glob("*.yml")):
        config = workflow(path.name)
        assert "pull_request_target" not in config["on"], path.name
        if "pull_request" in config["on"]:
            found.add(path.name)
            assert config["permissions"] == {"contents": "read"}, path.name
            assert "secrets." not in path.read_text(), path.name
    assert found == allowed


def test_android_signing_steps_are_owner_gated_without_gating_unsigned_ci():
    build = workflow("android.yml")["jobs"]["build"]
    assert "if" not in build and "needs" not in build
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
    assert not permits(job["if"], launcher_acceptance=True, old_installer_sha256="a" * 64)


def test_late_restore_acceptance_is_owner_only_and_reads_only_its_installer_url():
    """The late restore point under a 0.9.6 launcher, proven on Windows from a branch."""
    job = workflow("desktop-installer.yml")["jobs"]["late-restore-acceptance"]
    sha = "a" * 64
    assert permits(job["if"], launcher_acceptance=True, old_installer_sha256=sha)
    assert permits(job["if"], launcher_acceptance=True, old_installer_sha256=sha, ref="refs/heads/fix/old-launcher-restore-point")
    assert not permits(job["if"], launcher_acceptance=True)
    assert not permits(job["if"], old_installer_sha256=sha)
    for changes in [{"actor_id": "7"}, {"triggering_actor": "another-maintainer"}, {"repo": "someone/ai-shuffle"},
                    {"event": "pull_request"}, {"event": "push"}, {"event": "workflow_run"}, {"event": "schedule"}]:
        assert not permits(job["if"], launcher_acceptance=True, old_installer_sha256=sha, **changes), changes
    assert job["permissions"] == {"contents": "read"}
    assert "needs" not in job and "environment" not in job
    text = yaml.safe_dump(job)
    assert re.findall(r"secrets\.\w+", text) == ["secrets.OLD_INSTALLER_URL"] and "vars." not in text
    # The secret reaches only the download step, which checks the file's hash.
    download = job["steps"][0]
    assert download["env"]["OLD_INSTALLER_URL"] == "${{ secrets.OLD_INSTALLER_URL }}"
    assert "Get-FileHash" in download["run"] and "OLD_INSTALLER_SHA256" in download["run"]
    assert "Write-Host $env:OLD_INSTALLER_URL" not in download["run"] and "echo" not in download["run"].lower()
    for forbidden in ["gh release upload", "gh release create", "publish-installer", "wrangler", "git push", "git tag"]:
        assert forbidden not in text, forbidden
    uploads = [step["with"]["name"] for step in job["steps"] if str(step.get("uses", "")).startswith("actions/upload-artifact")]
    assert uploads == ["late-restore-evidence"]
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
