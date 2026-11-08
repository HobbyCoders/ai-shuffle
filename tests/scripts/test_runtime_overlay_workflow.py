"""Keep the Docker overlay acceptance isolated, native and non-publishing."""
from pathlib import Path
import re

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
