"""Offline producing-path check: real venv + hash-locked pip + current Fluid wheel.

Only Node builds are faked. Tiny local wheels avoid network/host dependencies;
wall-clock samples describe this lab, not a full production update.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import zipfile

import pytest

from docker import application_updates as updates
from tests.test_application_updates import B, C, feed


BUILD_BACKEND = r'''import pathlib, zipfile

def build_wheel(wheel_directory, config_settings=None, metadata_directory=None):
    name = "fluid-0.1.0-py3-none-any.whl"
    files = {
        "fluid/__init__.py": "",
        "fluid/fixture_revision.py": pathlib.Path("fixture_revision.py").read_text(),
        "fluid-0.1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: fluid\nVersion: 0.1.0\n",
        "fluid-0.1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: offline-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    files["fluid-0.1.0.dist-info/RECORD"] = "".join(f"{path},,\n" for path in files)
    with zipfile.ZipFile(pathlib.Path(wheel_directory) / name, "w") as wheel:
        for path, text in files.items():
            wheel.writestr(path, text)
    return name
'''


@pytest.mark.skipif(sys.platform != "linux", reason="Docker bin/python environments run on Linux")
def test_real_offline_pip_reuse_runs_current_fluid(feed, monkeypatch, tmp_path):
    updater, publish, *_ = feed
    monkeypatch.delenv("SHUFFLE_HANDED_OVER", raising=False)
    wheel = tmp_path / "third_party_fixture-1.0-py3-none-any.whl"
    files = {
        "third_party_fixture.py": "VALUE = 'locked dependency'\n",
        "third_party_fixture-1.0.dist-info/METADATA": "Metadata-Version: 2.1\nName: third-party-fixture\nVersion: 1.0\n",
        "third_party_fixture-1.0.dist-info/WHEEL": "Wheel-Version: 1.0\nGenerator: offline-fixture\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
    }
    files["third_party_fixture-1.0.dist-info/RECORD"] = "".join(f"{path},,\n" for path in files)
    with zipfile.ZipFile(wheel, "w") as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    lock = f"{wheel.as_uri()} --hash=sha256:{hashlib.sha256(wheel.read_bytes()).hexdigest()}\n"
    monkeypatch.setattr(updater, "build", updates.ContainerUpdates.build.__get__(updater))
    real_run = updater.run_build
    commands = []

    def run(command, cwd, env):
        commands.append(list(command))
        if command[0] == "npm":
            if command[1] == "ci":
                (cwd / "node_modules").mkdir(parents=True, exist_ok=True)
            elif cwd.name == "frontend":
                (cwd / "build").mkdir(parents=True)
                (cwd / "build/index.html").write_text(env["GITHUB_SHA"])
        else:
            real_run(command, cwd, {**env, "PIP_NO_INDEX": "1"})

    monkeypatch.setattr(updater, "run_build", run)

    def stage(revision, sequence):
        entries = {
            "requirements.lock": lock,
            "requirements-native.lock": "# none\n",
            "requirements-voice.lock": "# none\n",
            "app/core/voice_dependencies.py": "raise SystemExit(0)\n",
            "fluid/pyproject.toml": '[build-system]\nrequires=[]\nbuild-backend="fixture_backend"\nbackend-path=["."]\n',
            "fluid/fixture_backend.py": BUILD_BACKEND,
            "fluid/fixture_revision.py": f"REVISION = {revision!r}\n",
        }
        for package in ("frontend", "tools", "browser-mcp"):
            entries[f"{package}/package.json"] = json.dumps({"name": package, "private": True})
            entries[f"{package}/package-lock.json"] = json.dumps({"name": package, "lockfileVersion": 3})
        publish(revision=revision, sequence=sequence,
                entries={f"ai-shuffle-{revision}/{name}": text for name, text in entries.items()})
        start = time.perf_counter()
        index = len(commands)
        updater.check(download=True)
        elapsed = time.perf_counter() - start
        # Do not seed installed Fluid: drive the actual no-deps pip build/install
        # then import from an unrelated cwd with Python's isolated mode.
        python = updater.directory(revision) / "venv/bin/python"
        output = subprocess.check_output([str(python), "-I", "-c",
            "from fluid.fixture_revision import REVISION; import third_party_fixture; print(REVISION)"],
            cwd=tmp_path, text=True, timeout=10)
        assert output.strip() == revision
        return elapsed, commands[index:]

    monkeypatch.setenv("AI_SHUFFLE_PERF_DOCKER_DEPENDENCY_REUSE", "0")
    stage(B, 2)
    before, old_commands = stage(C, 3)
    monkeypatch.setenv("AI_SHUFFLE_PERF_DOCKER_DEPENDENCY_REUSE", "1")
    stage("d" * 40, 4)
    after, new_commands = stage("e" * 40, 5)
    assert sum("--require-hashes" in command for command in old_commands) == 1
    assert sum("--require-hashes" in command for command in new_commands) == 0
    assert sum(command[:2] == ["npm", "ci"] for command in new_commands) == 0
    assert sum("./fluid" in command for command in new_commands) == 1
    print("DEPENDENCY_REUSE_LAB " + json.dumps({"before_s": before, "after_s": after}))
