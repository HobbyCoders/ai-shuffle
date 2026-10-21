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


def test_private_client_requires_explicit_chromium_before_import(tmp_path):
    result = subprocess.run(
        ["node", str(ROOT / "scripts/profile_idle_client.mjs"), "http://127.0.0.1:9001",
         str(tmp_path / "missing-playwright.mjs")],
        cwd=tmp_path, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode != 0
    assert "Provide an explicit Chromium executable path" in result.stderr
    assert "ERR_MODULE_NOT_FOUND" not in result.stderr

def test_backend_lab_environment_excludes_inherited_data_and_credentials(tmp_path, monkeypatch):
    from scripts.profile_backend_costs import isolated_environment
    monkeypatch.setenv("DATA_DIR", "/not-the-fixture")
    monkeypatch.setenv("OWNER_SENTINEL", "not-a-credential")
    env = isolated_environment(tmp_path)
    assert env["DATA_DIR"] == str(tmp_path / "data")
    assert env["HOME"] == str(tmp_path / "home")
    assert env["WORKSPACE_DIR"] == str(tmp_path / "workspace")
    assert env["LOCAL_MODE"] == "true"
    assert "OWNER_SENTINEL" not in env
    assert Path(env["AI_SHUFFLE_ENV_FILE"]).read_text() == ""


def test_pruning_setting_failure_is_not_masked_by_unbound_timer(mock_db, monkeypatch):
    from contextlib import contextmanager
    from app.db import retention
    from scripts.profile_backend_costs import prune_lab
    monkeypatch.setattr(retention, "get_db", retention.get_db)
    rolled_back = []
    class Cursor:
        def execute(self, sql, args=None):
            if sql.startswith("SET LOCAL"):
                raise RuntimeError("synthetic setting failure")
    class Connection:
        def cursor(self):
            return Cursor()
        def rollback(self):
            rolled_back.append(True)
    connection = Connection()
    @contextmanager
    def get_db():
        yield connection
    with pytest.raises(RuntimeError, match="synthetic setting failure"):
        prune_lab(10, get_db, connection)
    assert rolled_back == [True]


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
