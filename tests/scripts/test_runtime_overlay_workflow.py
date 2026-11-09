"""Keep the Docker overlay acceptance isolated, native and non-publishing."""
from pathlib import Path
import os
import re
import subprocess
import sys

import pytest

import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_native_overlay_acceptance_checks_both_reviewed_candidates_without_secrets():
    text = (ROOT / ".github/workflows/runtime-overlay-acceptance.yml").read_text()
    workflow = yaml.load(text, Loader=yaml.BaseLoader)
    assert set(workflow["on"]) == {"pull_request", "workflow_dispatch"}
    assert workflow["permissions"] == {"contents": "read"}
    assert "secrets." not in text
    assert "pull_request_target" not in text
    job = workflow["jobs"]["acceptance"]
    assert job["strategy"]["fail-fast"] == "false"
    assert "continue-on-error" not in text
    candidates = job["strategy"]["matrix"]["include"]
    assert {(c["arch"], c["machine"], c["runner"]) for c in candidates} == {
        ("amd64", "x86_64", "ubuntu-24.04"),
        ("arm64", "aarch64", "ubuntu-24.04-arm"),
    }
    for candidate in candidates:
        assert re.fullmatch(r"[0-9a-f]{64}", candidate["sha256"])
        assert int(candidate["size"]) > 0
        assert candidate["url"].startswith("https://github.com/astral-sh/python-build-standalone/releases/download/20261026/")
        assert f"-{candidate['machine']}-unknown-linux-gnu-install_only.tar.gz" in candidate["url"]
    steps = job["steps"]
    fetch = next(s["run"] for s in steps if s.get("name") == "Fetch the pinned candidate runtime")
    assert "sha256sum --check" in fetch and "stat -c %s" in fetch
    build = next(s["with"] for s in steps if s.get("uses", "").startswith("docker/build-push-action@"))
    assert build["load"] == "true" and build["push"] == "false"
    assert build["platforms"] == "linux/${{ matrix.arch }}"
    assert build["file"] == "${{ runner.temp }}/overlay-input/Dockerfile"
    assert workflow["env"]["ACCEPTANCE_BASE"] == (
        "python:3.12.15-slim-trixie@sha256:"
        "a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704"
    )
    run = next(s["run"] for s in steps if s.get("name", "").startswith("Signed activation"))
    assert "--rm --user appuser" in run
    assert "-I /app/scripts/release/overlay_acceptance.py" in run
    assert "--docker-dir" not in run  # exercise the image's baked updater
    deps = next(s["run"] for s in steps if s.get("name", "").startswith("Install real application"))
    assert "--rm --user appuser" in deps
    assert '"$work/python/bin/python3" -m venv' in deps
    assert "--require-hashes -r /app/requirements.lock" in deps
    assert "--no-deps --no-build-isolation /opt/fluid" in deps
    assert '"$python" -m pip check' in deps
    assert "psycopg2" in deps
    assert "--capability python-3.12" in run
    assert any(s.get("name") == "Record the baked runtime" for s in steps)
    evidence = steps[-1]
    assert evidence["if"] == "always()"
    assert evidence["with"]["retention-days"] == "3"


@pytest.mark.parametrize("base_count", [0, 1, 2])
def test_acceptance_base_is_pinned_without_editing_the_production_recipe(tmp_path, base_count):
    workflow = yaml.load((ROOT / ".github/workflows/runtime-overlay-acceptance.yml").read_text(),
                         Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["acceptance"]["steps"]
    command = next(s["run"] for s in steps if s.get("name", "").startswith("Pin the disposable"))
    script = command.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    original = "FROM node:22-slim AS frontend-builder\n" + "FROM python:3.12-slim-trixie\n" * base_count
    recipe = tmp_path / "Dockerfile"
    recipe.write_text(original)
    output = tmp_path / "AcceptanceDockerfile"
    result = subprocess.run([sys.executable, "-", str(output)], input=script, text=True,
                            cwd=tmp_path, env={**os.environ, **workflow["env"]}, capture_output=True)
    assert recipe.read_text() == original
    if base_count == 1:
        assert result.returncode == 0, result.stderr
        assert output.read_text() == original.replace("FROM python:3.12-slim-trixie\n",
            "FROM " + workflow["env"]["ACCEPTANCE_BASE"] + "\n")
    else:
        assert result.returncode != 0
        assert "Review the acceptance base" in result.stderr
        assert not output.exists()
