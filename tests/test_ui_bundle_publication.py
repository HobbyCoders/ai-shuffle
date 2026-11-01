"""UI publication artifacts are data in the signing job, never candidate code."""
import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

from scripts import source_archive as archive

REVISION = "b" * 40
VERSION = "1.1.0-dev.1"
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def assets(tmp_path):
    root = tmp_path / "build"
    (root / "mobile").mkdir(parents=True)
    (root / "index.html").write_text("desktop")
    (root / "mobile/index.html").write_text("phone")
    return root


def test_bundle_is_reproducible_and_binds_both_shells_and_build_constants(assets, tmp_path):
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    ui = archive.build_ui_bundle(assets, REVISION, VERSION, first)
    assert archive.build_ui_bundle(assets, REVISION, VERSION, second) == ui
    assert first.read_bytes() == second.read_bytes()
    assert archive.describe_ui_bundle(first, REVISION, VERSION) == ui
    for revision, version in (("c" * 40, VERSION), (REVISION, "9.9.9")):
        with pytest.raises((ValueError, KeyError)):
            archive.describe_ui_bundle(first, revision, version)


def test_missing_phone_shell_and_zip_expansion_are_rejected(assets, tmp_path, monkeypatch):
    (assets / "mobile/index.html").unlink()
    with pytest.raises(ValueError, match="both desktop and phone"):
        archive.build_ui_bundle(assets, REVISION, VERSION, tmp_path / "missing.zip")
    (assets / "mobile/index.html").write_text("phone")
    output = tmp_path / "bounded.zip"
    archive.build_ui_bundle(assets, REVISION, VERSION, output)
    monkeypatch.setattr(archive, "UI_MAX_EXPANDED", 1)
    with pytest.raises(ValueError, match="expansion"):
        archive.describe_ui_bundle(output, REVISION, VERSION)


@pytest.mark.parametrize("name", ["../escape", "file:stream", "alias\\name", "INDEX.HTML"])
def test_unsafe_or_duplicate_ui_files_are_refused(assets, tmp_path, name):
    output = tmp_path / "hostile.zip"
    archive.build_ui_bundle(assets, REVISION, VERSION, output)
    with zipfile.ZipFile(output, "a") as zipped:
        zipped.writestr(f"ai-shuffle-{REVISION}/{name}", "unsafe")
    with pytest.raises(ValueError, match="unsafe"):
        archive.describe_ui_bundle(output, REVISION, VERSION)


def test_ci_builder_never_passes_publication_secrets_to_candidate_npm(assets, tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("build_ui_fixture", ROOT / "scripts/build_ui_bundle.py")
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    source = tmp_path / "candidate"
    (source / "frontend").mkdir(parents=True)
    import shutil
    shutil.copytree(assets, source / "frontend/build")
    monkeypatch.setenv("DESKTOP_APPLICATION_SIGNING_KEY", "not-a-secret-fixture")
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "not-a-secret-fixture")
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *args, **kwargs: REVISION + "\n")
    calls = []
    monkeypatch.setattr(builder.subprocess, "run", lambda command, **kwargs: calls.append((command, kwargs)))
    result = builder.build(source, REVISION, VERSION, tmp_path / "ci.zip")
    npm = [kwargs["env"] for command, kwargs in calls if command[0] == "npm"]
    assert len(npm) == 2
    assert all("DESKTOP_APPLICATION_SIGNING_KEY" not in env and "CLOUDFLARE_API_TOKEN" not in env for env in npm)
    assert all(env["APP_VERSION"] == VERSION and env["GITHUB_SHA"] == REVISION for env in npm)
    assert result["revision"] == REVISION and result["version"] == VERSION


def test_nightly_and_both_stable_paths_use_the_unprivileged_bundle_job():
    workflow = (ROOT / ".github/workflows/source-ui-bundle.yml").read_text()
    assert "contents: read" in workflow and "persist-credentials: false" in workflow
    assert "secrets." not in workflow and "ubuntu-latest" in workflow
    nightly = (ROOT / ".github/workflows/desktop-source.yml").read_text()
    assert "uses: ./.github/workflows/source-ui-bundle.yml" in nightly
    assert "SOURCE_UI_BUNDLE_FILE:" in nightly and "- cron: '0 6 * * *'" in nightly
    # One version per run: a release tag pushed between the UI build and the
    # signer must not stamp the bundle and the manifest differently (v1.2.0).
    assert "version: ${{ needs.gate.outputs.version }}" in nightly
    assert "SOURCE_VERSION: ${{ needs.gate.outputs.version }}" in nightly
    assert "publication_source.py version" in nightly
    stable = (ROOT / ".github/workflows/release.yml").read_text()
    assert "needs: [create-release, docker, buyer-install, ui-stable]" in stable
    assert "needs: [preflight, stage-buyer, ui-stable]" in stable
    assert stable.count("SOURCE_UI_BUNDLE_FILE:") == 2
