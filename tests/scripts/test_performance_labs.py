"""Exercise the performance CLIs without starting services or accessing live data."""
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("script,option", [
    ("scripts/profile_backend_costs.py", "--skip-nodes"),
    ("scripts/profile_isolated_idle.py", "--playwright-module"),
])
def test_profile_help_exits_without_starting_services(script, option, tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / script), "--help"],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr
    assert option in result.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("target", [
    "http://127.0.0.1:8000",
    "http://127.0.0.1:8002",
    "http://localhost:8000",
    "https://example.com:9001",
])
def test_private_client_rejects_live_or_nonlocal_targets_before_import(target, tmp_path):
    result = subprocess.run(
        ["node", str(ROOT / "scripts/profile_idle_client.mjs"), target,
         str(tmp_path / "missing-playwright.mjs")],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert "Refusing non-isolated/live target" in result.stderr
    assert "ERR_MODULE_NOT_FOUND" not in result.stderr
    assert list(tmp_path.iterdir()) == []
