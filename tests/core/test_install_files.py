"""The buyer install files in docs/install stay pinned, complete and safe."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest
import yaml

from app import __version__

ROOT = Path(__file__).resolve().parents[2]
COMPOSE = ROOT / "docs/install/compose"
PERSISTENT = {"/data", "/workspace", "/home/appuser/.fluid", "/home/appuser/.claude", "/home/appuser/.local",
              "/home/appuser/.config/gh", "/home/appuser/chrome-profile"}


def env_example():
    values = {}
    for line in (COMPOSE / "env.example").read_text().splitlines():
        if line and not line.startswith("#"):
            key, value = line.split("=", 1)
            values[key] = value
    return values


def service():
    return yaml.safe_load((COMPOSE / "compose.yaml").read_text())["services"]["ai-shuffle"]


def test_compose_runs_a_pinned_release_image_never_a_build_or_moving_tag():
    ai_shuffle = service()
    assert "build" not in ai_shuffle
    image = ai_shuffle["image"]
    assert image == f"ghcr.io/hobbycoders/ai-shuffle-server:${{AI_SHUFFLE_VERSION:-{__version__}}}"
    assert "TOKEN" not in image
    assert "latest" not in image and "edge" not in image
    assert env_example()["AI_SHUFFLE_VERSION"] == __version__


def test_compose_carries_the_runtime_settings_the_image_needs():
    ai_shuffle = service()
    assert ai_shuffle["shm_size"] == "1gb"
    assert ai_shuffle["stop_grace_period"] == "90s"
    assert ai_shuffle["restart"] == "unless-stopped"
    targets = {volume.rsplit(":", 1)[1] for volume in ai_shuffle["volumes"]}
    assert targets == PERSISTENT
    assert ai_shuffle["ports"] == ["${BIND_HOST:-127.0.0.1}:${PORT:-8000}:8000"]
    assert "tty" not in ai_shuffle and "privileged" not in ai_shuffle


def test_env_example_lists_only_what_a_buyer_sets():
    assert set(env_example()) == {"AI_SHUFFLE_VERSION", "BIND_HOST", "PORT",
                                  "COOKIE_SECURE", "OWNER_PASSWORD", "LICENSE_KEY", "WORKSPACE_DIR", "PUID", "PGID"}
    # The owner password is optional and seeds the first start only.
    assert env_example()["OWNER_PASSWORD"] == ""
    assert service()["environment"]["OWNER_PASSWORD"] == "${OWNER_PASSWORD:-}"
    # So is the license key (app.core.license.LICENSE_KEY_ENV), activated once.
    from app.core.license import LICENSE_KEY_ENV
    assert env_example()[LICENSE_KEY_ENV] == ""
    assert service()["environment"][LICENSE_KEY_ENV] == "${LICENSE_KEY:-}"
    assert "first start" in (COMPOSE / "env.example").read_text().split("LICENSE_KEY=")[0].rsplit("---", 1)[1]
    assert env_example()["BIND_HOST"] == "127.0.0.1"
    compose = (COMPOSE / "compose.yaml").read_text()
    for key in env_example():
        assert "${" + key in compose, f"{key} in env.example is not used by compose.yaml"
    text = (COMPOSE / "env.example").read_text()
    assert "0.0.0.0" in text and "Do not use 0.0.0.0" in text


def test_buyer_compose_is_verified_in_ci():
    workflow = yaml.safe_load((ROOT / ".github/workflows/container-postgres.yml").read_text())
    # Paid image build: manual only; every release runs buyer-install below.
    assert set(workflow[True]) == {"workflow_dispatch"}
    steps = workflow["jobs"]["container-postgres"]["steps"]
    assert any("verify_buyer_compose.py" in step.get("run", "") for step in steps)
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    job = release["jobs"]["buyer-install"]
    assert job["runs-on"] == "ubuntu-latest" and job["needs"] == ["create-release", "docker"]
    assert any("verify_buyer_compose.py" in step.get("run", "") for step in job["steps"])


def test_private_registry_is_retired():
    for workflow in ("ci-registry.yml", "deploy-registry.yml"):
        assert not (ROOT / ".github/workflows" / workflow).exists()
    assert not (ROOT / "docs/install/registry.md").exists()
    for name in ("compose.yaml", "env.example", "README.md"):
        text = (COMPOSE / name).read_text()
        assert not re.search(r"pull[ _-]tokens?", text, re.I)
        assert "registry.hobbycoders.com" not in text
    readme = (COMPOSE / "README.md").read_text()
    assert "ghcr.io/hobbycoders/ai-shuffle-server" in readme
    assert "Local use and updates are free" in readme
    # Old checkouts can retain ignored node_modules after the registry is removed.
    # Source archives have no Git metadata; still run the other checks above.
    if not (ROOT / ".git").exists() or shutil.which("git") is None:
        pytest.skip("tracked-file check requires a Git checkout and git")
    tracked = subprocess.run(
        ["git", "ls-files", "--", "android/registry"], cwd=ROOT,
        check=True, capture_output=True, text=True, timeout=10,
    )
    assert not tracked.stdout, tracked.stdout


@pytest.mark.parametrize("tracked", [False, True])
def test_private_registry_retirement_ignores_only_untracked_files(tmp_path, monkeypatch, tracked):
    if shutil.which("git") is None:
        pytest.skip("regression requires git")
    subprocess.run(["git", "init", "--quiet", str(tmp_path)], check=True, timeout=10)
    (tmp_path / ".gitignore").write_text("node_modules/\n")
    leftover = tmp_path / "android/registry/node_modules/x"
    leftover.parent.mkdir(parents=True)
    leftover.write_text("leftover dependency")
    subprocess.run(
        ["git", "check-ignore", "--quiet", "android/registry/node_modules/x"],
        cwd=tmp_path, check=True, timeout=10,
    )
    compose = tmp_path / "docs/install/compose"
    shutil.copytree(COMPOSE, compose)
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    monkeypatch.setitem(globals(), "COMPOSE", compose)
    if tracked:
        # Even an ignored path must fail if someone adds it back to the index.
        subprocess.run(
            ["git", "add", "--force", "android/registry/node_modules/x"],
            cwd=tmp_path, check=True, timeout=10,
        )
        with pytest.raises(AssertionError, match="android/registry/node_modules/x"):
            test_private_registry_is_retired()
    else:
        test_private_registry_is_retired()
    assert leftover.is_file()


def test_retired_install_templates_are_gone():
    assert not (ROOT / "my-ai-shuffle.xml").exists()
    assert not (ROOT / "docs/install/unraid").exists()


def test_install_docs_never_name_the_frozen_public_package():
    for path in (ROOT / "docs/install").rglob("*"):
        # registry.md explains why that package is frozen.
        if path.is_file() and path.name != "registry.md":
            assert not re.search(r"ghcr\.io/hobbycoders/ai-shuffle(?![-\w])", path.read_text(errors="ignore")), path
