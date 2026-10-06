"""/settings/claude-auth/cloud: the owner-only "Cloud provider or gateway" method (#1862)."""
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest

from app.core import claude_auth_mode, claude_cli_status, claude_cloud_auth
from app.db import database as db

CLOUD = "/api/v1/settings/claude-auth/cloud"
MODE = "/api/v1/settings/claude-auth"
TOKEN = "fake-gateway-token-SECRET-000"
GATEWAY = {"method": "gateway", "acknowledge_warning": True,
           "values": {"base_url": "https://gw.example/v1", "auth_token": TOKEN}}


@pytest.fixture(autouse=True)
def _probe_and_files(monkeypatch, tmp_path):
    status = claude_cli_status.ClaudeCliStatus(installed=True, signed_in=True, email="me@example.com")
    monkeypatch.setattr(claude_cloud_auth, "_files_dir", lambda: tmp_path / "claude-cloud")
    with patch.object(claude_cli_status, "get_status_async", AsyncMock(return_value=status)):
        yield


def test_save_returns_no_secret_and_logs_none(authenticated_client, caplog):
    caplog.set_level(logging.DEBUG)
    r = authenticated_client.put(CLOUD, json=GATEWAY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert TOKEN not in r.text
    assert body["values"] == {"base_url": "https://gw.example/v1"}
    assert body["secrets"] == {"auth_token": {"set": True}}
    assert body["status"]["ready"] is True and body["status"]["method"] == "gateway"
    assert body["auth"]["mode"] == "subscription"  # saving does not switch
    gateway = next(m for m in body["methods"] if m["id"] == "gateway")
    assert gateway["allowlist"] == ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"]
    assert "smallest permissions" in body["warning"] and "address you enter" in body["gateway_warning"]
    for path in (CLOUD, MODE, "/api/v1/settings/integrations"):
        assert TOKEN not in authenticated_client.get(path).text
    assert TOKEN not in caplog.text


def test_saving_needs_the_warning_acknowledged(authenticated_client):
    r = authenticated_client.put(CLOUD, json={**GATEWAY, "acknowledge_warning": False})
    assert r.status_code == 400 and "warning" in r.text
    assert db.get_system_setting(claude_cloud_auth.CONFIG_SETTING) is None


@pytest.mark.parametrize("url", ["http://gw.example", "https://u:p@gw.example", "https://gw.example?a=1",
                                 "https://gw.example#x"])
def test_bad_base_url_is_refused_without_echoing_the_token(authenticated_client, url):
    r = authenticated_client.put(CLOUD, json={**GATEWAY, "values": {"base_url": url, "auth_token": TOKEN}})
    assert r.status_code == 400
    assert TOKEN not in r.text
    assert db.get_system_setting(claude_cloud_auth.SECRETS_SETTING) is None


def test_wrong_types_do_not_echo_values(authenticated_client):
    r = authenticated_client.put(CLOUD, json={**GATEWAY, "values": {"base_url": "https://gw.example",
                                                                     "auth_token": [TOKEN]}})
    assert r.status_code == 400 and TOKEN not in r.text
    r = authenticated_client.put(CLOUD, json={**GATEWAY, "values": [TOKEN]})
    assert r.status_code == 400 and TOKEN not in r.text


def test_turning_on_needs_acknowledgement_and_a_saved_method(authenticated_client):
    claude_auth_mode.set_mode("subscription")
    r = authenticated_client.put(MODE, json={"mode": "cloud", "acknowledge_warning": True})
    assert r.status_code == 400 and "first" in r.text
    authenticated_client.put(CLOUD, json=GATEWAY)
    r = authenticated_client.put(MODE, json={"mode": "cloud"})
    assert r.status_code == 400 and "warning" in r.text
    assert claude_auth_mode.stored_mode() == "subscription"
    r = authenticated_client.put(MODE, json={"mode": "cloud", "acknowledge_warning": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["mode"] == "cloud" and body["mode_label"] == "Cloud provider or gateway"
    assert body["cloud"] == {"method": "gateway", "method_label": "LLM gateway", "configured": True,
                             "ready": True, "missing": [], "storage_locked": False}
    assert [m["value"] for m in body["modes"]] == ["subscription", "api_key", "cloud", "off"]
    assert TOKEN not in r.text
    # Switching back is the ordinary mode switch.
    assert authenticated_client.put(MODE, json={"mode": "subscription"}).json()["mode"] == "subscription"


@pytest.mark.real_agent_gate
def test_activate_in_one_call_and_clear(authenticated_client):
    from app.core import credential_gate

    r = authenticated_client.put(CLOUD, json={**GATEWAY, "activate": True})
    assert r.status_code == 200, r.text
    assert r.json()["auth"]["mode"] == "cloud" and r.json()["auth"]["claude_reason"] is None
    r = authenticated_client.delete(CLOUD)
    assert r.status_code == 200
    assert db.get_system_setting(claude_cloud_auth.CONFIG_SETTING) is None
    assert db.get_system_setting(claude_cloud_auth.SECRETS_SETTING) is None
    assert claude_auth_mode.stored_mode() == "cloud"  # like removing the API key
    assert r.json()["auth"]["claude_reason"] == credential_gate.CLOUD_NOT_SET_MESSAGE


def test_locked_storage_is_409(authenticated_client, monkeypatch):
    from app.core import encryption

    monkeypatch.setattr(encryption, "is_encryption_ready", lambda: False)
    r = authenticated_client.put(CLOUD, json=GATEWAY)
    assert r.status_code == 409 and TOKEN not in r.text


def test_owner_only(client):
    from app.api.auth import hash_api_key

    db.create_api_user("cloud-user", "Cloud User", hash_api_key("aih_cloud_user_key"))
    headers = {"Authorization": "Bearer aih_cloud_user_key"}
    assert client.get(CLOUD, headers=headers).status_code == 403
    assert client.put(CLOUD, json=GATEWAY, headers=headers).status_code == 403
    assert client.delete(CLOUD, headers=headers).status_code == 403
    assert client.put(MODE, json={"mode": "cloud", "acknowledge_warning": True},
                      headers=headers).status_code == 403
    assert db.get_system_setting(claude_cloud_auth.CONFIG_SETTING) is None


def test_mode_summary_for_non_owner_has_no_values(authenticated_client):
    authenticated_client.put(CLOUD, json=GATEWAY)
    body = authenticated_client.get(MODE).json()
    assert "gw.example" not in json.dumps(body["cloud"])


# ─── security review fixes (PR #1960) ─────────────────────────────────────

@pytest.mark.parametrize("payload", [
    {"acknowledge_warning": True, "values": {"auth_token": TOKEN}},             # no method
    {"method": 5, "acknowledge_warning": True, "values": {"auth_token": TOKEN}},
    {**GATEWAY, "acknowledge_warning": "yes " + TOKEN},
    {**GATEWAY, "activate": {"t": TOKEN}},
    [TOKEN],
    TOKEN,
])
def test_a_malformed_request_is_422_without_any_submitted_value(authenticated_client, caplog, payload):
    """Finding 6b: the framework's 422 would echo the body (secrets included);
    these routes answer with field names and reasons only, and log no value."""
    caplog.set_level(logging.DEBUG)
    r = authenticated_client.put(CLOUD, json=payload)
    assert r.status_code == 422
    assert TOKEN not in r.text and "input" not in r.json()["detail"][0]
    assert r.json()["detail"][0]["loc"][0] == "body"
    assert TOKEN not in caplog.text
    assert db.get_system_setting(claude_cloud_auth.SECRETS_SETTING) is None


def _as_preview(client, monkeypatch):
    from app.api import auth as auth_api
    from app.core import preview_access

    # Only the preview cookie authenticates: no owner override, and the test
    # client counts as the LAN peer a real agent browser is.
    client.app.dependency_overrides.pop(auth_api.require_admin, None)
    monkeypatch.setattr(preview_access, "_private_peer", lambda request: True)
    monkeypatch.setattr(preview_access, "verify",
                        lambda token: preview_access.Grant(session_id="s1", expires_at=2**40))
    client.cookies.set(preview_access.COOKIE_NAME, "preview-token")


def test_an_agent_preview_reads_only_non_secret_fields_and_cannot_change(authenticated_client, monkeypatch):
    assert authenticated_client.put(CLOUD, json=GATEWAY).status_code == 200
    _as_preview(authenticated_client, monkeypatch)
    r = authenticated_client.get(CLOUD)
    assert r.status_code == 200, r.text
    assert TOKEN not in r.text
    assert r.json()["secrets"] == {"auth_token": {"set": True}}
    assert r.json()["values"] == {"base_url": "https://gw.example/v1"}
    for method in ("put", "delete"):
        kwargs = {"json": {**GATEWAY, "values": {"base_url": "https://other.example",
                                                  "auth_token": "fake-preview-token"}}} if method == "put" else {}
        r = getattr(authenticated_client, method)(CLOUD, **kwargs)
        assert r.status_code == 403, (method, r.text)
        assert r.json()["detail"].startswith("Agent previews cannot change")
    r = authenticated_client.put(MODE, json={"mode": "cloud", "acknowledge_warning": True})
    assert r.status_code == 403
    assert claude_cloud_auth.stored()["values"] == {"base_url": "https://gw.example/v1"}
    assert claude_auth_mode.stored_mode() != "cloud"
