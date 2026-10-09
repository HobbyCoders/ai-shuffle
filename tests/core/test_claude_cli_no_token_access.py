"""The app never reads, refreshes, writes or deletes the Claude CLI's credentials.

Sign-in state comes from `claude auth status` and logout goes through
`claude auth logout`. No module reads the CLI's credential file or its
`claudeAiOauth` token, and none calls Anthropic's private subscription
endpoints with it: Anthropic's terms do not let a third-party app collect or
intermediate Claude.ai session tokens (docs/reviews/compliance-2026-10-09.md,
finding 1; docs/reviews/prod-readiness-2026-09-23/BRIEF.md). Claude plan usage
comes only from what the stock `claude` CLI reports in its own output.
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
    for rel in ("app/core/cli_bridge.py", "app/api/auth.py", "run_local.py", "entrypoint.sh", ".env.example"):
        assert ".credentials.json" not in (ROOT / rel).read_text(), rel


# Names that only a reader of the CLI's sign-in, or a client of Anthropic's
# private subscription endpoints, would need.
_FORBIDDEN_MARKERS = (
    ".credentials.json",
    "claudeAiOauth",
    "/api/oauth/usage",
    "reset_rate_limits",
    "oauth-2025-04-20",
)
# Deny lists that stop agents reading the file; they name it to protect it.
_DENY_LISTS = {
    "fluid/fluid/tools/builtins/bash_risk.py": (".credentials.json",),
    "fluid/fluid/tools/hardening.py": (".credentials.json",),
}
_SCANNED_SUFFIXES = {".py", ".ts", ".js", ".mjs", ".cjs", ".svelte", ".rs", ".sh"}


def _offenders(root: Path) -> list[str]:
    """Every scanned file under *root* that names a forbidden marker."""
    paths = [root / rel for rel in ("run_local.py", "entrypoint.sh")]
    for top in ("app", "fluid/fluid", "frontend/src", "desktop/src", "browser-mcp/src", "tools"):
        if (root / top).is_dir():
            paths += [p for p in (root / top).rglob("*")
                      if p.suffix in _SCANNED_SUFFIXES and "node_modules" not in p.parts]
    found = []
    for path in paths:
        if not path.is_file():
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        text = path.read_text(encoding="utf-8", errors="ignore")
        allowed = _DENY_LISTS.get(rel, ())
        found += [f"{rel}: {m}" for m in _FORBIDDEN_MARKERS if m in text and m not in allowed]
    return found


def test_no_code_reads_the_cli_sign_in():
    """No app, Fluid, frontend or desktop code reads the CLI's sign-in token."""
    assert _offenders(ROOT) == []


def test_scan_catches_a_reader(tmp_path):
    """The scan is not vacuous: a reader anywhere it looks is caught."""
    (tmp_path / "app" / "core").mkdir(parents=True)
    (tmp_path / "app" / "core" / "x.py").write_text('P = "~/.claude/.credentials.json"\n')
    (tmp_path / "frontend" / "src").mkdir(parents=True)
    (tmp_path / "frontend" / "src" / "y.ts").write_text("data.claudeAiOauth.accessToken\n")
    (tmp_path / "fluid" / "fluid" / "tools").mkdir(parents=True)
    (tmp_path / "fluid" / "fluid" / "tools" / "hardening.py").write_text('"/.claude/.credentials.json"\n')
    assert sorted(_offenders(tmp_path)) == [
        "app/core/x.py: .credentials.json",
        "frontend/src/y.ts: claudeAiOauth",
    ]


def test_claude_resets_routes_are_gone(authenticated_client):
    assert authenticated_client.get("/api/v1/usage/claude/resets").status_code in (404, 405)
    assert authenticated_client.post("/api/v1/usage/claude/resets/weekly", json={}).status_code in (404, 405)
    assert authenticated_client.post("/api/v1/usage/claude/resets/session", json={}).status_code in (404, 405)


def test_cli_status_keeps_org_and_config_dir_server_side():
    status = cs.ClaudeCliStatus(signed_in=True)
    cs.parse_json_status({"loggedIn": True, "orgId": "org-1", "configDirectory": "/home/u/.claude",
                          "email": "a@b.c"}, status)
    assert status.org_id == "org-1" and status.config_dir == "/home/u/.claude"
    public = status.to_dict()
    assert "org_id" not in public and "config_dir" not in public


def test_is_authenticated_asks_the_cli(monkeypatch, cli_home):
    from app.core.auth import AuthService
    _forbid_opening(monkeypatch, cli_home)
    monkeypatch.setattr(cs, "probe", lambda resolver=None: _status(False))
    service = AuthService()
    assert service.is_claude_authenticated() is False
    cs.invalidate()
    monkeypatch.setattr(cs, "probe", lambda resolver=None: _status(True))
    assert service.is_claude_authenticated() is True


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
    with caplog.at_level("INFO"):
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
    assert body["authenticated"] is True and body["signed_in"] is True
    diag = authenticated_client.get("/api/v1/auth/diagnostics").json()
    assert "claude_credentials_exists" not in diag and "credentials_file_size" not in diag
    assert diag["claude_cli_signed_in"] is True


def test_logout_clears_the_relogin_notice(monkeypatch, cli_home, mock_db):
    from app.core import claude_relogin_notice as notice
    from app.core.auth import AuthService
    from app.db import database as db
    from app.db.base import get_db
    db.set_system_setting("claude_active_account:old", "acct")
    with get_db() as conn:
        db._retire_claude_accounts(conn.cursor())
    assert notice.mark_if_needed() is True
    monkeypatch.setattr(cs, "probe", lambda resolver=None: _status(True))
    monkeypatch.setattr("app.core.auth.subprocess.run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""))
    assert AuthService().claude_logout()["success"] is True
    assert notice.pending() is False
    assert cli_home.exists()
