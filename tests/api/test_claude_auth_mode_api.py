"""GET/PUT /settings/claude-auth: the explicit Claude Code auth mode."""
from unittest.mock import AsyncMock, patch

from app.core import claude_auth_mode, claude_cli_status, encryption
from app.db import database as db

ENDPOINT = "/api/v1/settings/claude-auth"


def _cli(signed_in: bool) -> claude_cli_status.ClaudeCliStatus:
    return claude_cli_status.ClaudeCliStatus(
        installed=True, signed_in=signed_in,
        email="me@example.com" if signed_in else None, plan="max" if signed_in else None,
    )


def _probe(signed_in: bool):
    return patch.object(claude_cli_status, "get_status_async", AsyncMock(return_value=_cli(signed_in)))


def test_reports_mode_account_and_key_last_four(authenticated_client):
    db.set_system_setting("anthropic_api_key", encryption.encrypt_value("sk-ant-api03-secret-ABCD"))
    claude_auth_mode.set_mode("subscription")
    with _probe(True):
        body = authenticated_client.get(ENDPOINT).json()
    assert body["mode"] == "subscription" and body["explicit"] is True
    assert body["cli"]["signed_in"] is True and body["cli"]["email"] == "me@example.com"
    assert body["api_key"] == {"set": True, "last4": "ABCD"}
    assert "secret" not in str(body)


def test_put_switches_mode_and_integrations_follow(authenticated_client):
    db.set_system_setting("anthropic_api_key", encryption.encrypt_value("sk-ant-api03-secret-ABCD"))
    claude_auth_mode.set_mode("subscription")
    integrations = authenticated_client.get("/api/v1/settings/integrations").json()
    assert integrations["claude_auth_mode"] == "subscription"
    assert integrations["claude_code_auth_method"] == "account"
    with _probe(True):
        response = authenticated_client.put(ENDPOINT, json={"mode": "api_key"})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "api_key"
    integrations = authenticated_client.get("/api/v1/settings/integrations").json()
    assert integrations["claude_code_auth_method"] == "api_key"


def test_put_rejects_unknown_mode(authenticated_client):
    with _probe(False):
        assert authenticated_client.put(ENDPOINT, json={"mode": "auto"}).status_code == 400


def test_saving_a_key_does_not_switch_mode_unless_asked(authenticated_client):
    claude_auth_mode.set_mode("subscription")
    with patch("app.core.anthropic_generation.validate_api_key", AsyncMock(return_value=None)):
        r = authenticated_client.post("/api/v1/settings/integrations/anthropic",
                                      json={"api_key": "sk-ant-api03-first-1111"})
        assert r.status_code == 200, r.text
        assert claude_auth_mode.stored_mode() == "subscription"
        r = authenticated_client.post("/api/v1/settings/integrations/anthropic",
                                      json={"api_key": "sk-ant-api03-second-2222", "use_for_claude_code": True})
        assert r.status_code == 200, r.text
    assert claude_auth_mode.stored_mode() == "api_key"


def test_removing_the_key_keeps_api_key_mode(authenticated_client):
    db.set_system_setting("anthropic_api_key", encryption.encrypt_value("sk-ant-api03-secret-ABCD"))
    claude_auth_mode.set_mode("api_key")
    assert authenticated_client.delete("/api/v1/settings/integrations/anthropic").status_code == 200
    assert claude_auth_mode.stored_mode() == "api_key"
