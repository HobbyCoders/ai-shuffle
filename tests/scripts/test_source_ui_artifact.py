"""The speculative main UI cache is data, never release authorization."""
import copy
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("ui_artifact", ROOT / "scripts/ci/source_ui_artifact.py")
cache = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cache)
REPO = {"id": 17, "full_name": cache.gate.REPOSITORY}


def workflow(name):
    return yaml.load((ROOT / ".github/workflows" / name).read_text(), Loader=yaml.BaseLoader)


@pytest.fixture
def candidate(tmp_path):
    root = tmp_path / "candidate"
    root.mkdir()
    def git(*args):
        return subprocess.check_output(["git", "-C", str(root), *args], text=True).strip()
    git("init", "--quiet")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Fixture")
    (root / "file").write_text("base")
    git("add", ".")
    git("commit", "--quiet", "-m", "base")
    git("tag", "v1.2.0")
    (root / "file").write_text("candidate")
    git("commit", "--quiet", "-am", "candidate")
    return root, git("rev-parse", "HEAD"), git


class GitHub:
    def __init__(self, revision):
        self.revision = revision
        self.run = {"id": 71, "workflow_id": 12, "path": cache.gate.MAIN_SUITE_PATH,
                    "head_sha": revision, "head_branch": "main", "event": "push",
                    "status": "completed", "conclusion": "success", "run_attempt": 2,
                    "repository": copy.deepcopy(REPO), "head_repository": copy.deepcopy(REPO)}
        self.jobs = [{"name": "Main suite", "status": "completed", "conclusion": "success"}]
        self.artifacts = [{"id": 99, "name": cache.artifact_name(revision, 2), "expired": False,
                           "size_in_bytes": 1000, "workflow_run": {
                               "id": 71, "repository_id": 17, "head_repository_id": 17,
                               "head_branch": "main", "head_sha": revision}}]
        self.calls = []
        self.reads = 0
        self.retry_on_read = None

    def __call__(self, endpoint):
        self.calls.append(endpoint)
        if endpoint == "":
            return copy.deepcopy(REPO)
        if endpoint == "actions/workflows/main-suite.yml":
            return {"id": 12, "path": cache.gate.MAIN_SUITE_PATH}
        if endpoint.startswith("actions/workflows/main-suite.yml/runs?"):
            assert f"head_sha={self.revision}" in endpoint
            return {"workflow_runs": [copy.deepcopy(self.run)]}
        if endpoint == "actions/runs/71":
            self.reads += 1
            result = copy.deepcopy(self.run)
            if self.retry_on_read and self.reads >= self.retry_on_read:
                result.update(status="in_progress", conclusion=None, run_attempt=3)
            return result
        if endpoint == "actions/runs/71/attempts/2/jobs?per_page=100&page=1":
            return {"jobs": copy.deepcopy(self.jobs)}
        if endpoint == "actions/runs/71/artifacts?per_page=100&page=1":
            return {"artifacts": copy.deepcopy(self.artifacts)}
        raise AssertionError(endpoint)


def producer(tmp_path, revision, version):
    # Drive the real ZIP/stamp producer and consumer, not a hand-written stamp.
    assets = tmp_path / "assets"
    (assets / "mobile").mkdir(parents=True)
    (assets / "index.html").write_text("desktop")
    (assets / "mobile/index.html").write_text("phone")
    cache.archive.build_ui_bundle(assets, revision, version, tmp_path / "ui.zip")
    with zipfile.ZipFile(tmp_path / "artifact.zip", "w") as zipped:
        zipped.write(tmp_path / "ui.zip", "ui.zip")
    calls = []
    def download(artifact_id, output):
        calls.append(artifact_id)
        shutil.copyfile(tmp_path / "artifact.zip", output)
    return download, calls


def test_selected_older_main_revision_reuses_latest_attempt_and_original_version(candidate, tmp_path):
    root, revision, git = candidate
    version = cache.source.read_source("version", root, revision)
    download, calls = producer(tmp_path, revision, version)
    # Main may advance, or a release tag may land after this build. The selected
    # older eligible revision and its pre-tag display version must stay pinned.
    git("tag", "v1.3.0")
    (root / "file").write_text("newer unproven main")
    git("commit", "--quiet", "-am", "newer")
    git("checkout", "--quiet", "--detach", revision)
    api = GitHub(revision)
    output = tmp_path / "staged/ui.zip"
    assert cache.prepare(api, root, revision, output, download=download) == {"reused": "true", "version": version}
    assert calls == [99]
    assert output.read_bytes() == (tmp_path / "ui.zip").read_bytes()
    assert cache.bundle_version(output, revision) == version
    assert api.reads == 4
    # The unchanged signer's version policy must accept precisely this pre-tag
    # version, not merely the cache's Python mirror of that contract.
    signer = f"""
        import {{ nightlyVersion }} from {json.dumps((ROOT / 'desktop/scripts/nightly-version.mjs').as_uri())};
        import {{ execFileSync }} from 'node:child_process';
        const [root, revision, resolved, pinned] = process.argv.slice(1);
        process.stdout.write(nightlyVersion({{resolved, pinned, revision,
          git: (...args) => execFileSync('git', ['-C', root, ...args], {{encoding: 'utf8'}}).trim()}}));
    """
    assert subprocess.check_output(["node", "--input-type=module", "-e", signer, str(root), revision,
                                    cache.source.read_source("version", root, revision), version], text=True) == version


@pytest.mark.parametrize("event", ["push", "workflow_dispatch"])
def test_trusted_main_events_can_reuse(candidate, tmp_path, event):
    root, revision, _ = candidate
    version = cache.source.read_source("version", root, revision)
    download, _ = producer(tmp_path, revision, version)
    api = GitHub(revision)
    api.run["event"] = event
    assert cache.prepare(api, root, revision, tmp_path / "staged.zip", download=download)["reused"] == "true"


@pytest.mark.parametrize("field,value", [
    ("event", "pull_request"), ("event", "pull_request_target"), ("head_branch", "feature"),
    ("head_sha", "0" * 40), ("path", ".github/workflows/lookalike.yml"), ("workflow_id", 13),
    ("status", "in_progress"), ("conclusion", "failure"), ("conclusion", "cancelled"),
    ("run_attempt", 0), ("run_attempt", True),
    ("repository", {"id": 18, "full_name": "fork/ai-shuffle"}),
    ("head_repository", {"id": 18, "full_name": "fork/ai-shuffle"}),
])
def test_untrusted_or_unsuccessful_run_never_supplies_a_release_artifact(candidate, tmp_path, field, value):
    root, revision, _ = candidate
    api = GitHub(revision)
    api.run[field] = value
    def forbidden(*args):
        pytest.fail("Untrusted run must not download")
    result = cache.prepare(api, root, revision, tmp_path / "ui.zip", download=forbidden)
    assert result == {"reused": "false", "version": cache.source.read_source("version", root, revision)}


@pytest.mark.parametrize("mutation", ["missing", "expired", "duplicate", "old-attempt", "wrong-sha", "fork", "wrong-run", "branch", "huge", "aggregate-failed", "aggregate-missing"])
def test_missing_or_invalid_cache_keeps_the_gated_rebuild(candidate, tmp_path, mutation):
    root, revision, _ = candidate
    api = GitHub(revision)
    if mutation == "missing":
        api.artifacts = []
    elif mutation == "expired":
        api.artifacts[0]["expired"] = True
    elif mutation == "duplicate":
        api.artifacts *= 2
    elif mutation == "old-attempt":
        api.artifacts[0]["name"] = cache.artifact_name(revision, 1)
    elif mutation == "wrong-sha":
        api.artifacts[0]["workflow_run"]["head_sha"] = "0" * 40
    elif mutation == "fork":
        api.artifacts[0]["workflow_run"]["head_repository_id"] = 18
    elif mutation == "wrong-run":
        api.artifacts[0]["workflow_run"]["id"] = 70
    elif mutation == "branch":
        api.artifacts[0]["workflow_run"]["head_branch"] = "feature"
    elif mutation == "huge":
        api.artifacts[0]["size_in_bytes"] = cache.MAX_ARTIFACT_SIZE + 1
    elif mutation == "aggregate-failed":
        api.jobs[0]["conclusion"] = "failure"
    elif mutation == "aggregate-missing":
        api.jobs = []
    def forbidden(*args):
        pytest.fail("Invalid cache must not download")
    assert cache.prepare(api, root, revision, tmp_path / "ui.zip", download=forbidden)["reused"] == "false"


@pytest.mark.parametrize("failure", ["deleted", "malformed", "unsafe", "version", "revision", "retry"])
def test_download_validation_and_retry_races_fall_back(candidate, tmp_path, failure):
    root, revision, _ = candidate
    api = GitHub(revision)
    version = cache.source.read_source("version", root, revision)
    download, _ = producer(tmp_path, "0" * 40 if failure == "revision" else revision,
                           "9.9.9" if failure == "version" else version)
    if failure == "deleted":
        def download(*args):
            raise subprocess.CalledProcessError(1, "gh")
    elif failure == "malformed":
        (tmp_path / "artifact.zip").write_bytes(b"not a zip")
    elif failure == "unsafe":
        with zipfile.ZipFile(tmp_path / "artifact.zip", "a") as zipped:
            zipped.writestr("../escape", "untrusted")
    elif failure == "retry":
        api.retry_on_read = 3
    output = tmp_path / "staged.zip"
    assert cache.prepare(api, root, revision, output, download=download) == {"reused": "false", "version": version}
    assert not output.exists()


def test_version_must_name_real_first_parent_history(candidate):
    root, revision, _ = candidate
    resolved = cache.source.read_source("version", root, revision)
    assert cache.valid_version(root, revision, resolved, resolved)
    assert not cache.valid_version(root, revision, "1.3.0", f"1.2.0-dev.2+g{revision[:7]}")
    assert not cache.valid_version(root, revision, "1.3.0", "1.2.0-dev.1+g0000000")
    assert not cache.valid_version(root, revision, "1.3.0", f"8.0.0-dev.1+g{revision[:7]}")


def test_cache_failure_or_owner_only_pass_does_not_weaken_the_existing_gate(candidate, tmp_path):
    root, revision, _ = candidate
    def unavailable(endpoint):
        raise subprocess.CalledProcessError(1, "gh")
    assert cache.prepare(unavailable, root, revision, tmp_path / "ui.zip")["reused"] == "false"
    api = GitHub(revision)
    api.jobs = []  # Owner-attested candidates still use the normal source builder.
    assert cache.prepare(api, root, revision, tmp_path / "ui.zip")["reused"] == "false"


def test_source_ui_is_parallel_optional_and_secret_free_but_not_a_suite_substitute():
    main = workflow("main-suite.yml")
    speculative = main["jobs"]["source-ui"]
    assert "needs" not in speculative and "secrets" not in speculative
    assert "github.ref == 'refs/heads/main'" in speculative["if"]
    assert "github.repository == 'HobbyCoders/ai-shuffle'" in speculative["if"]
    assert "github.event_name == 'push'" in speculative["if"] and "github.event_name == 'workflow_dispatch'" in speculative["if"]
    assert speculative["uses"] == "./.github/workflows/source-ui-bundle.yml"
    assert speculative["with"] == {"source-ref": "${{ github.sha }}", "optional": "true",
                                   "artifact-name": "source-ui-${{ github.sha }}-${{ github.run_attempt }}"}
    assert "source-ui" not in main["jobs"]["main-suite"]["needs"]
    build = workflow("source-ui-bundle.yml")
    assert build["permissions"] == {"contents": "read"}
    assert build["on"]["workflow_call"]["inputs"]["optional"] == {"type": "boolean", "default": "false"}
    job = build["jobs"]["build"]
    assert job["continue-on-error"] == "${{ inputs.optional }}"
    assert job["runs-on"] == "ubuntu-latest" and "environment" not in job
    assert "secrets" not in (ROOT / ".github/workflows/source-ui-bundle.yml").read_text()
    checkout, control = job["steps"][:2]
    assert checkout["with"]["persist-credentials"] == control["with"]["persist-credentials"] == "false"
    assert control["with"]["ref"] == "${{ github.workflow_sha }}"
    resolve = next(step for step in job["steps"] if step.get("id") == "source")
    assert "publication_source.py version" in resolve["run"]
    build_step = next(step for step in job["steps"] if "build_ui_bundle.py" in step.get("run", ""))
    assert build_step["env"]["UI_VERSION"] == "${{ steps.source.outputs.version }}"
    assert job["steps"][-1]["with"]["retention-days"] == "7"


def test_publication_stages_validated_cache_or_requires_successful_fallback_and_keeps_rechecks():
    jobs = workflow("desktop-source.yml")["jobs"]
    gate = jobs["gate"]
    assert gate["outputs"]["ui-reused"] == "${{ steps.version.outputs.reused }}"
    pin = next(step for step in gate["steps"] if step.get("id") == "version")
    assert pin["env"]["REVISION"] == "${{ steps.ci.outputs.revision }}"
    assert "python -I scripts/ci/source_ui_artifact.py" in pin["run"]
    upload = gate["steps"][-1]
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert "steps.version.outputs.reused == 'true'" in upload["if"]
    assert upload["with"]["name"] == "source-ui"
    fallback = jobs["ui"]
    assert "needs.gate.outputs.ui-reused != 'true'" in fallback["if"]
    assert fallback["with"]["version"] == "${{ needs.gate.outputs.version }}"
    assert "optional" not in fallback["with"]
    publish = jobs["publish"]
    assert " ".join(publish["if"].split()) == (
        "always() && needs.gate.result == 'success' && needs.gate.outputs.ready == 'true' && "
        "((needs.gate.outputs.ui-reused == 'true' && needs.ui.result == 'skipped') || "
        "(needs.gate.outputs.ui-reused != 'true' && needs.ui.result == 'success'))")
    assert publish["env"]["SOURCE_VERSION"] == "${{ needs.gate.outputs.version }}"
    assert publish["environment"] == "account-hub-production"
    rechecks = [step for step in publish["steps"] if "edge_publication_gate.py" in step.get("run", "")]
    assert len(rechecks) == 2
    assert publish["concurrency"] == {"group": "desktop-source-publish", "cancel-in-progress": "false"}
    assert all("source_ui_artifact.py" not in step.get("run", "") for step in publish["steps"])
    # This change does not move or replace any full-suite PR trigger.
    for name in ("ci-backend", "ci-frontend", "ci-fluid", "container-postgres"):
        assert "pull_request" in workflow(name + ".yml")["on"]
    stable = workflow("release.yml")["jobs"]
    for name in ("ui-stable",):
        assert "optional" not in stable[name]["with"]
