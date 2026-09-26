"""Status and login-terminal routes for the stock Claude Code CLI."""

import pytest

from app.core import claude_cli_status as cs


@pytest.fixture
def signed_in(monkeypatch):
    status = cs.ClaudeCliStatus(
        installed=True, signed_in=True, auth_method="claude.ai", auth_method_label="Claude account",
        email="me@example.com", plan="max", cli_version="2.1.280",
        binary_path="/home/appuser/.local/bin/claude",
    )
    calls = []

    def fake_probe(resolver=None):
        calls.append(1)
        return status
    monkeypatch.setattr(cs, "probe", fake_probe)
    cs.invalidate()  # the app's startup probe may have run first
    return calls


def test_cli_status_route(authenticated_client, signed_in, monkeypatch):
    monkeypatch.delenv("SHUFFLE_DESKTOP", raising=False)
    body = authenticated_client.get("/api/v1/auth/claude/cli").json()
    assert body["signed_in"] is True
    assert body["email"] == "me@example.com"
    assert body["plan"] == "max"
    assert body["cli_version"] == "2.1.280"
    assert body["binary_path"] == "/home/appuser/.local/bin/claude"
    assert body["login"]["command"] == "claude auth login"
    # Cached: a second read does not run the CLI again; refresh does.
    authenticated_client.get("/api/v1/auth/claude/cli")
    assert len(signed_in) == 1
    authenticated_client.get("/api/v1/auth/claude/cli?refresh=true")
    assert len(signed_in) == 2


def test_claude_status_is_the_cli_answer(authenticated_client, signed_in):
    body = authenticated_client.get("/api/v1/auth/claude/status").json()
    assert body["authenticated"] is True
    assert body["signed_in"] is True
    assert body["email"] == "me@example.com"
    for retired in ("expires_at", "has_refresh_token", "credentials_file", "active_account", "accounts_count"):
        assert retired not in body


@pytest.mark.parametrize("method,path", [
    ("post", "/api/v1/auth/claude/login"),
    ("get", "/api/v1/auth/claude/login/poll"),
    ("post", "/api/v1/auth/claude/complete"),
    ("get", "/api/v1/auth/claude/validate"),
    ("get", "/api/v1/auth/claude/accounts"),
    ("post", "/api/v1/auth/claude/accounts/start"),
    ("post", "/api/v1/auth/claude/accounts/complete"),
])
def test_in_app_claude_login_routes_are_gone(authenticated_client, method, path):
    assert getattr(authenticated_client, method)(path).status_code in (404, 405)


def test_relogin_notice_is_shown_once_and_dismissed(authenticated_client, signed_in, mock_db):
    from app.core import claude_relogin_notice as notice
    from app.db.base import get_db
    from app.db import database as db

    assert notice.mark_if_needed() is False  # fresh install: no notice
    assert authenticated_client.get("/api/v1/auth/claude/cli").json()["relogin_notice"] is None
    # An upgrade: the old sign-in left a pointer for another install; the v89
    # migration records the notice and removes the pointer.
    db.set_system_setting("claude_active_account:other-install", "acct-1")
    with get_db() as conn:
        db._retire_claude_accounts(conn.cursor())
    assert db.get_system_setting("claude_active_account:other-install") is None
    assert notice.mark_if_needed() is True
    body = authenticated_client.get("/api/v1/auth/claude/cli").json()["relogin_notice"]
    assert body["logout_command"] == "claude auth logout"
    assert body["login_command"] == "claude auth login"
    assert authenticated_client.post("/api/v1/auth/claude/relogin-notice/dismiss").json() == {"success": True}
    assert authenticated_client.get("/api/v1/auth/claude/cli").json()["relogin_notice"] is None
    assert notice.mark_if_needed() is False  # never comes back


def test_missing_cli_is_reported_not_raised(authenticated_client):
    body = authenticated_client.get("/api/v1/auth/claude/cli").json()
    assert body["installed"] is False
    assert body["signed_in"] is None
    assert body["error"]


def test_desktop_gets_instructions_not_a_terminal(authenticated_client, signed_in, monkeypatch):
    monkeypatch.setenv("SHUFFLE_DESKTOP", "1")
    login = authenticated_client.get("/api/v1/auth/claude/cli").json()["login"]
    assert login["surface"] == "desktop"
    assert login["terminal"] is False
    assert login["docker_command"] is None
    response = authenticated_client.post("/api/v1/auth/claude/cli-login/start")
    assert response.status_code == 409
    assert "claude auth login" in response.json()["detail"]


def test_login_terminal_routes(authenticated_client, monkeypatch):
    from app.core import claude_cli_login

    class FakeTerminal:
        def __init__(self):
            self.sent = []

        def start(self):
            return {"running": True, "output": "", "offset": 0}

        def read(self, offset):
            return {"running": True, "offset": offset}

        def send(self, text, offset):
            if len(text) > 10:
                raise ValueError("Input is too long.")
            self.sent.append(text)
            return {"running": True}

        def cancel(self):
            return {"running": False, "active": False}

    fake = FakeTerminal()
    monkeypatch.setattr(claude_cli_login, "login_terminal", fake)
    assert authenticated_client.post("/api/v1/auth/claude/cli-login/start").json()["running"] is True
    assert authenticated_client.get("/api/v1/auth/claude/cli-login?offset=7").json()["offset"] == 7
    assert authenticated_client.post("/api/v1/auth/claude/cli-login/input", json={"text": "abc#def"}).status_code == 200
    assert fake.sent == ["abc#def"]
    assert authenticated_client.post("/api/v1/auth/claude/cli-login/input", json={"text": "x" * 11}).status_code == 400
    assert authenticated_client.post("/api/v1/auth/claude/cli-login/input", json={}).status_code == 400
    assert authenticated_client.post("/api/v1/auth/claude/cli-login/cancel").json()["active"] is False


def test_routes_need_auth(client):
    assert client.get("/api/v1/auth/claude/cli").status_code in (401, 403)
    assert client.get("/api/v1/auth/claude/status").status_code in (401, 403)
    assert client.post("/api/v1/auth/claude/cli-login/start").status_code in (401, 403)
    assert client.post("/api/v1/auth/claude/cli-login/input", json={"text": "x"}).status_code in (401, 403)


def test_auth_status_hides_account_from_signed_out_callers(client, signed_in):
    body = client.get("/api/v1/auth/status").json()
    assert body["claude_cli"] == {"installed": True, "signed_in": True}
