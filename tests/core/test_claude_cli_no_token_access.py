"""The app never refreshes, reads or deletes the Claude CLI's credentials.

Sign-in state comes from `claude auth status`, logout goes through
`claude auth logout`, and nothing at runtime opens the CLI's credential file.
"""

import builtins
import subprocess
from pathlib import Path

import pytest

from app.core import claude_cli_status as cs

ROOT = Path(__file__).resolve().parents[2]


def _status(signed_in=True):
    return cs.ClaudeCliStatus(installed=True, signed_in=signed_in, binary_path="/usr/local/bin/claude",
                              cli_version="2.1.280")


@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    """A config dir with a credentials file nobody may touch."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    creds = claude_dir / ".credentials.json"
    creds.write_text('{"claudeAiOauth": {"accessToken": "never-read"}}')
    monkeypatch.setenv("HOME", str(tmp_path))
    return creds


def _forbid_opening(monkeypatch, creds):
    real_open = builtins.open

    def guarded(file, *args, **kwargs):
        assert Path(file).name != ".credentials.json", f"opened {file}"
        return real_open(file, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", guarded)


def test_no_refresh_call_sites_remain():
    """No turn path, status route or startup step refreshes the token."""
    for rel in ("app/core/query_engine.py", "app/core/query_background.py", "app/core/startup.py",
                "app/api/auth.py", "app/core/usage_limits.py", "app/core/cli_bridge.py"):
        text = (ROOT / rel).read_text()
        assert "ensure_fresh_credentials" not in text, rel
        assert "sync_back_active_account" not in text, rel
        assert "seed_from_credentials_file" not in text, rel
    for rel in ("app/core/cli_bridge.py", "app/api/auth.py", "run_local.py"):
        assert ".credentials.json" not in (ROOT / rel).read_text(), rel


def test_is_authenticated_asks_the_cli(monkeypatch, cli_home):
    from app.core.auth import AuthService
    _forbid_opening(monkeypatch, cli_home)
    monkeypatch.setattr(cs, "probe", lambda resolver=None: _status(False))
    service = AuthService()
    assert service.is_claude_authenticated() is False
    info = service.get_claude_auth_info()
    assert info["authenticated"] is False and "credentials_file" not in info
    cs.invalidate()
    monkeypatch.setattr(cs, "probe", lambda resolver=None: _status(True))
    assert service.is_claude_authenticated() is True
    assert service.get_claude_token_status()["token_valid"] is True


def test_logout_runs_the_cli_and_never_unlinks(monkeypatch, cli_home):
    from app.core.auth import AuthService
    monkeypatch.setattr(cs, "probe", lambda resolver=None: _status(True))
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs.get("env", {})))
        return subprocess.CompletedProcess(argv, 0, "Successfully logged out", "")
    monkeypatch.setattr("app.core.auth.subprocess.run", fake_run)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    result = AuthService().claude_logout()
    assert result["success"] is True
    assert calls[0][0] == ["/usr/local/bin/claude", "auth", "logout"]
    assert "ANTHROPIC_API_KEY" not in calls[0][1]
    assert cli_home.exists()
    assert cs.peek() is None  # the next status read asks the CLI again


def test_failed_logout_reports_and_keeps_the_file(monkeypatch, cli_home):
    from app.core.auth import AuthService
    monkeypatch.setattr(cs, "probe", lambda resolver=None: _status(True))
    monkeypatch.setattr("app.core.auth.subprocess.run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 1, "", "boom"))
    result = AuthService().claude_logout()
    assert result["success"] is False and result["error"] == "boom"
    assert cli_home.exists()


def test_logout_without_cli(monkeypatch, cli_home):
    from app.core.auth import AuthService
    result = AuthService().claude_logout()
    assert result["success"] is False
    assert cli_home.exists()


def test_check_prerequisites_asks_the_cli(monkeypatch, cli_home, caplog):
    from app.core import startup
    _forbid_opening(monkeypatch, cli_home)
    monkeypatch.setattr(cs, "probe", lambda resolver=None: _status(False))
    monkeypatch.setattr(startup, "_check_browser_mcp_prereqs", lambda: None)
    with caplog.at_level("WARNING"):
        startup.check_prerequisites()
    assert "claude auth login" in caplog.text
    assert cs.peek() is not None  # warms the cache /health reads


def test_status_route_does_not_open_credentials(authenticated_client, monkeypatch, cli_home):
    monkeypatch.setattr(cs, "probe", lambda resolver=None: _status(True))
    cs.invalidate()
    _forbid_opening(monkeypatch, cli_home)
    status = authenticated_client.get("/api/v1/auth/status").json()
    assert status["claude_authenticated"] is True
    body = authenticated_client.get("/api/v1/auth/claude/status").json()
    assert body["authenticated"] is True and body["cli"]["signed_in"] is True
    diag = authenticated_client.get("/api/v1/auth/diagnostics").json()
    assert "claude_credentials_exists" not in diag and "credentials_file_size" not in diag
    assert diag["claude_cli_signed_in"] is True
