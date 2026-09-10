"""Release notes expose every distribution and safe, version-pinned Docker setup."""
from pathlib import Path
import re
import shlex

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
    assert "AI-Shuffle-Desktop-${{ env.DESKTOP_VERSION }}-win-x64.exe" in body
    assert "releases/download/${{ env.VERSION }}/AI-Shuffle-Android.apk" in body
    assert "https://github.com/${{ github.repository }}/pkgs/container/ai-shuffle" in body


def test_docker_commands_pin_release_and_preserve_runtime_data():
    body = release_body().replace("${{ env.DESKTOP_VERSION }}", "0.8.12")
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
