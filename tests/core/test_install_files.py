"""The buyer install files in docs/install stay pinned, complete and safe."""
from pathlib import Path
import re

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
    assert image.endswith(f"/ai-shuffle:${{AI_SHUFFLE_VERSION:-{__version__}}}")
    assert "${AI_SHUFFLE_PULL_TOKEN:?" in image, "a missing pull token must fail with a message"
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
    assert set(env_example()) == {"AI_SHUFFLE_PULL_TOKEN", "AI_SHUFFLE_VERSION", "BIND_HOST", "PORT",
                                  "COOKIE_SECURE", "WORKSPACE_DIR", "PUID", "PGID"}
    assert env_example()["AI_SHUFFLE_PULL_TOKEN"] == ""
    assert env_example()["BIND_HOST"] == "127.0.0.1"
    compose = (COMPOSE / "compose.yaml").read_text()
    for key in env_example():
        assert "${" + key in compose, f"{key} in env.example is not used by compose.yaml"
    text = (COMPOSE / "env.example").read_text()
    assert "0.0.0.0" in text and "Do not use 0.0.0.0" in text


def test_buyer_compose_is_verified_in_ci():
    workflow = yaml.safe_load((ROOT / ".github/workflows/container-postgres.yml").read_text())
    assert "docs/install/compose/**" in workflow[True]["pull_request"]["paths"]
    steps = workflow["jobs"]["container-postgres"]["steps"]
    assert any("verify_buyer_compose.py" in step.get("run", "") for step in steps)
    release = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    job = release["jobs"]["buyer-install"]
    assert job["runs-on"] == "ubuntu-latest" and job["needs"] == ["create-release", "docker"]
    assert any("verify_buyer_compose.py" in step.get("run", "") for step in job["steps"])


def unraid():
    import xml.etree.ElementTree as ET
    return ET.parse(ROOT / "docs/install/unraid/ai-shuffle.xml").getroot()


def test_unraid_template_pins_the_release_under_the_buyer_registry_path():
    template = unraid()
    assert template.findtext("Repository") == f"registry.hobbycoders.com/YOUR-PULL-TOKEN/ai-shuffle:{__version__}"
    public = "https://raw.githubusercontent.com/HobbyCoders/ai-shuffle-unraid/main/"
    assert template.findtext("TemplateURL") == public + "ai-shuffle.xml"
    assert template.findtext("Icon") == public + "images/ai-shuffle.png"
    assert template.findtext("Support") == "https://github.com/HobbyCoders/ai-shuffle-unraid/issues"
    text = (ROOT / "docs/install/unraid/ai-shuffle.xml").read_text()
    assert "claude-sdk" not in text and "claude-icon" not in text and ":latest" not in text


def test_unraid_template_runtime_flags_paths_and_interface():
    template = unraid()
    flags = template.findtext("ExtraParams").split()
    assert {"--shm-size=1g", "--stop-timeout=90", "--init", "--pids-limit=8192"} <= set(flags)
    configs = {config.get("Target"): config for config in template.iter("Config")}
    paths = {target: config for target, config in configs.items() if config.get("Type") == "Path"}
    assert set(paths) == PERSISTENT
    for target, config in paths.items():
        assert config.get("Default").startswith("/mnt/user/appdata/ai-shuffle/"), target
        assert config.text == config.get("Default"), target
    assert paths["/workspace"].get("Display") == "always"
    assert paths["/home/appuser/chrome-profile"].get("Display") == "always"
    port = configs["8000"]
    assert port.get("Type") == "Port" and port.get("Default") == "" and port.get("Required") == "true"
    assert "LAN address" in port.get("Description") and "every interface" in port.get("Description")
    owner = configs["OWNER_PASSWORD"]
    assert owner.get("Mask") == "true" and owner.get("Required") == "false"
    assert "CORS_ORIGINS" not in configs


def test_the_old_root_template_is_gone():
    assert not (ROOT / "my-ai-shuffle.xml").exists()


def test_install_docs_never_name_the_frozen_public_package():
    for path in (ROOT / "docs/install").rglob("*"):
        # registry.md explains why that package is frozen.
        if path.is_file() and path.name != "registry.md":
            assert not re.search(r"ghcr\.io/hobbycoders/ai-shuffle(?![-\w])", path.read_text(errors="ignore")), path
