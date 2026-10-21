"""Cache lab safety: fixture sizing and ownership precede all app imports."""
import importlib.util
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts/profile_cache_budgets.py"


def test_help_has_no_service_side_effects(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--help"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert "--event-bytes" in result.stdout
    assert list(tmp_path.iterdir()) == []


def test_invalid_fixture_exits_before_app_import(tmp_path):
    result = subprocess.run([sys.executable, str(SCRIPT), "--nodes", "0"], cwd=tmp_path,
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 2
    assert "all fixture sizes must be positive" in result.stderr
    assert list(tmp_path.iterdir()) == []


def test_environment_drops_inherited_credentials_and_binds_all_paths(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("cache_lab_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("DATABASE_URL", "sentinel")
    monkeypatch.setenv("OPENAI_API_KEY", "sentinel")
    env = module.owned_environment(tmp_path)
    assert "DATABASE_URL" not in env
    assert "OPENAI_API_KEY" not in env
    assert env["LOCAL_MODE"] == "true"
    for key in ("HOME", "DATA_DIR", "WORKSPACE_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
                "XDG_CACHE_HOME", "AI_SHUFFLE_ENV_FILE"):
        assert Path(env[key]).is_relative_to(tmp_path)
    assert Path(env["AI_SHUFFLE_ENV_FILE"]).read_text() == ""
