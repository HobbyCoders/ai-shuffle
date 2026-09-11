"""Release notes expose every distribution and safe, version-pinned Docker setup."""
from pathlib import Path
import re
import shlex
import subprocess

import yaml

ROOT = Path(__file__).resolve().parents[2]


def release_body():
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["create-release"]["steps"]
    return next(step["with"]["body"] for step in steps if "with" in step)


def test_release_downloads_include_desktop_mobile_and_docker():
    body = release_body()
    assert "| **Desktop** |" in body
    assert "| **Mobile** |" in body
    assert "| **Docker** |" in body
    assert "https://hobbycoders.com/desktop" in body
    assert "run the installer" in body
    assert "latest code from main" in body
    assert "Start AI Shuffle.cmd" not in body
    assert "https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle" in body
    assert "releases/download/" not in body
    assert "Signed Android APK" not in body
    assert "Desktop installers are opt-in" in body
    assert "https://github.com/${{ github.repository }}/pkgs/container/ai-shuffle" in body


def test_docker_commands_pin_release_and_preserve_runtime_data():
    body = release_body().replace("${{ env.RELEASE_VERSION }}", "0.8.12")
    script = re.search(r"```bash\n(.*?)```", body, re.DOTALL).group(1)
    commands = script.replace("\\\n", "").strip().splitlines()
    image = "ghcr.io/hobbycoders/ai-shuffle:0.8.12"
    assert shlex.split(commands[0]) == ["docker", "pull", image]
    run = shlex.split(commands[1])
    assert run[:3] == ["docker", "run", "-d"]
    assert run[-1] == image
    assert run[run.index("--stop-timeout") + 1] == "90"
    assert run[run.index("--shm-size") + 1] == "1g"
    assert run[run.index("-p") + 1] == "127.0.0.1:8000:8000"
    mounts = [run[i + 1].split(":", 1)[1] for i, arg in enumerate(run) if arg == "-v"]
    assert set(mounts) == {
        "/data", "/workspace", "/home/appuser/.fluid", "/home/appuser/.claude",
        "/home/appuser/.config/gh", "/home/appuser/chrome-profile",
    }
    assert ":latest" not in body
    assert "back up first" in body
    assert "external database must stay on their previous" in body


def test_release_version_outputs_feed_play_and_docker(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/release.yml").read_text())
    steps = workflow["jobs"]["create-release"]["steps"]
    script = next(step["run"] for step in steps if step.get("id") == "version")
    for raw in ("v1.2.3", "V1.2.3", " 1.2.3 ", "v1.2.3-rc.1"):
        env_file, output = tmp_path / "env", tmp_path / "output"
        env_file.write_text("")
        output.write_text("")
        result = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True,
                                env={"VERSION": raw, "GITHUB_ENV": str(env_file),
                                     "GITHUB_OUTPUT": str(output)})
        assert result.returncode == 0, result.stderr
        version = raw.strip().lstrip("vV")
        assert output.read_text() == f"tag=v{version}\n"
        assert f"RELEASE_VERSION={version}\n" in env_file.read_text()
    for raw in ("main", "v1.2", "v1.2.3;exit 0", "../v1.2.3"):
        result = subprocess.run(["bash", "-e", "-c", script], capture_output=True, text=True,
                                env={"VERSION": raw})
        assert result.returncode != 0


def test_release_orchestration_defaults_to_docker_and_play_only():
    workflow = yaml.load((ROOT / ".github/workflows/release.yml").read_text(), Loader=yaml.BaseLoader)
    desktop = workflow["jobs"]["desktop"]
    assert workflow["on"]["workflow_dispatch"]["inputs"]["build_desktop"] == {
        "description": "Also build a Windows installer (desktop normally updates in app)",
        "required": "false", "type": "boolean", "default": "false",
    }
    assert desktop["if"] == "github.event_name == 'workflow_dispatch' && inputs.build_desktop"
    assert desktop["uses"] == "./.github/workflows/desktop-installer.yml"
    assert workflow["jobs"]["docker"]["uses"] == "./.github/workflows/docker-publish.yml"
    assert workflow["permissions"]["packages"] == "write"
    for name in ("desktop", "docker", "android"):
        assert workflow["jobs"][name]["with"]["release_tag"] == "${{ needs.create-release.outputs.release_tag }}"
    assert "./.github/workflows/android.yml" not in str(workflow)


def test_docker_is_called_once_per_release_and_remains_manually_available():
    workflow = yaml.load((ROOT / ".github/workflows/docker-publish.yml").read_text(), Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"workflow_dispatch", "workflow_call"}
    assert workflow["on"]["workflow_call"]["inputs"]["release_tag"]["required"] == "true"
    assert workflow["on"]["workflow_dispatch"]["inputs"]["no_cache"]["default"] == "false"


def test_installation_pages_direct_android_users_to_play():
    for path in ("README.md", "android/README.md"):
        content = (ROOT / path).read_text()
        assert "https://play.google.com/store/apps/details?id=com.hobbycoders.aishuffle" in content
        assert "releases/download/android-latest/" not in content


def test_docker_release_rebuild_keeps_container_verification_gate():
    workflow = yaml.safe_load((ROOT / ".github/workflows/docker-publish.yml").read_text())
    steps = {step["name"]: step for step in workflow["jobs"]["build"]["steps"] if "name" in step}
    gate = "startsWith(github.ref, 'refs/tags/') || inputs.release_tag != ''"
    assert steps["Verify actual container lifecycle and UI"]["if"] == gate
    assert steps["Upload container evidence"]["if"] == f"always() && ({gate})"
    assert steps["Checkout release source"]["with"]["ref"] == "refs/tags/${{ inputs.release_tag }}"
