"""API tests for voice mode (wave VW1 of docs/VOICE_MODE_PLAN.md).

Covers the three surfaces VW1 ships:

* ``GET/PUT /api/v1/settings/voice`` — the four runtime-editable knobs
  (VM13/VM17): roundtrip, model allowlist, provider-reference validation.
* ``GET /api/v1/voice/providers`` — eligibility filtering (VM3: an enabled
  ``openai`` row with a stored API key; never a ChatGPT/Codex subscription row,
  because Realtime is API-key only).
* ``POST /api/v1/voice/sessions`` — flag gate, provider resolution, the
  ephemeral client-secret mint and the ``mode='voice'`` session row.

All upstream HTTP is stubbed via ``httpx.MockTransport`` — NO network. The
shared client the route uses is swapped for a MockTransport-backed one via a
monkeypatch on ``app.core.http_client.get_http_client`` (the route imports the
name at call time), mirroring tests/api/test_fetch_models_api.py.
"""

import httpx
import pytest

from app.core import encryption
from app.db import database as db


VOICE_SETTINGS = "/api/v1/settings/voice"
VOICE_ROOT = "/api/v1/voice"

RAW_KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"


def _unlock(mock_db):
    encryption.set_encryption_key("test-password", db)


def _make_provider(
    provider_id="prov-openai",
    *,
    name="OpenAI",
    provider_type="openai",
    auth_token=RAW_KEY,
    enabled=True,
    auth_mode=None,
):
    db.create_model_provider(
        provider_id=provider_id,
        name=name,
        provider_type=provider_type,
        base_url="https://api.openai.com",
        auth_token=auth_token,
        enabled=enabled,
    )
    if auth_mode:
        db.update_model_provider(provider_id, auth_mode=auth_mode)
    return provider_id


def _install_mock_transport(monkeypatch, handler):
    """Point the route's shared HTTP client at a MockTransport running ``handler``."""
    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    from app.core import http_client
    monkeypatch.setattr(http_client, "get_http_client", lambda: mock_client)
    return mock_client


def _enable_voice(**overrides):
    db.set_system_setting("voice_mode_enabled", "true")
    for key, value in overrides.items():
        db.set_system_setting(key, value)


def _secret_handler(captured=None, *, status_code=200, body=None):
    """MockTransport handler returning a canned client-secrets response."""
    def _handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured["request"] = request
        if status_code >= 400:
            return httpx.Response(status_code, json=body or {"error": {"message": "boom"}})
        return httpx.Response(
            status_code,
            json=body or {"value": "ek_test_secret", "expires_at": 1786000000},
        )
    return _handler


# ============================================================================
# Auth
# ============================================================================

class TestUnauthenticated:
    @pytest.mark.parametrize("method,path", [
        ("get", VOICE_SETTINGS),
        ("put", VOICE_SETTINGS),
        ("get", f"{VOICE_ROOT}/providers"),
        ("post", f"{VOICE_ROOT}/sessions"),
    ])
    def test_requires_auth(self, client, method, path):
        kwargs = {"json": {}} if method in ("post", "put") else {}
        resp = getattr(client, method)(path, **kwargs)
        assert resp.status_code in (401, 403), (method, path, resp.status_code)


# ============================================================================
# Settings
# ============================================================================

class TestVoiceSettings:
    def test_defaults(self, authenticated_client, mock_db):
        resp = authenticated_client.get(VOICE_SETTINGS)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "voice_mode_enabled": False,
            "voice_model": "gpt-realtime-2.1",
            "voice_provider_id": None,
            "voice_max_call_minutes": 240,
        }

    def test_put_roundtrip(self, authenticated_client, mock_db):
        pid = _make_provider()
        resp = authenticated_client.put(VOICE_SETTINGS, json={
            "voice_mode_enabled": True,
            "voice_model": "gpt-realtime-2.1-mini",
            "voice_provider_id": pid,
            "voice_max_call_minutes": 30,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "voice_mode_enabled": True,
            "voice_model": "gpt-realtime-2.1-mini",
            "voice_provider_id": pid,
            "voice_max_call_minutes": 30,
        }
        # Persisted: a fresh GET returns the same values.
        assert authenticated_client.get(VOICE_SETTINGS).json() == resp.json()

    def test_partial_update_leaves_other_keys(self, authenticated_client, mock_db):
        authenticated_client.put(VOICE_SETTINGS, json={"voice_max_call_minutes": 15})
        resp = authenticated_client.put(VOICE_SETTINGS, json={"voice_mode_enabled": True})
        assert resp.status_code == 200, resp.text
        assert resp.json()["voice_max_call_minutes"] == 15
        assert resp.json()["voice_mode_enabled"] is True

    def test_model_allowlist_rejects_unknown(self, authenticated_client, mock_db):
        resp = authenticated_client.put(VOICE_SETTINGS, json={"voice_model": "gpt-4o-realtime"})
        assert resp.status_code == 400
        # Nothing was written.
        assert authenticated_client.get(VOICE_SETTINGS).json()["voice_model"] == "gpt-realtime-2.1"

    @pytest.mark.parametrize("model", ["gpt-realtime-2.1", "gpt-realtime-2.1-mini", "gpt-realtime"])
    def test_model_allowlist_accepts_each(self, authenticated_client, mock_db, model):
        resp = authenticated_client.put(VOICE_SETTINGS, json={"voice_model": model})
        assert resp.status_code == 200, resp.text
        assert resp.json()["voice_model"] == model

    def test_max_minutes_must_be_positive(self, authenticated_client, mock_db):
        assert authenticated_client.put(VOICE_SETTINGS, json={"voice_max_call_minutes": 0}).status_code == 400
        assert authenticated_client.put(VOICE_SETTINGS, json={"voice_max_call_minutes": -5}).status_code == 400

    def test_provider_must_be_eligible(self, authenticated_client, mock_db):
        # Unknown id.
        resp = authenticated_client.put(VOICE_SETTINGS, json={"voice_provider_id": "nope"})
        assert resp.status_code == 400
        # A ChatGPT-subscription row is never eligible (Realtime is API-key only).
        chatgpt = _make_provider("prov-chatgpt", provider_type="chatgpt")
        resp = authenticated_client.put(VOICE_SETTINGS, json={"voice_provider_id": chatgpt})
        assert resp.status_code == 400
        assert authenticated_client.get(VOICE_SETTINGS).json()["voice_provider_id"] is None

    def test_provider_can_be_cleared(self, authenticated_client, mock_db):
        pid = _make_provider()
        authenticated_client.put(VOICE_SETTINGS, json={"voice_provider_id": pid})
        resp = authenticated_client.put(VOICE_SETTINGS, json={"voice_provider_id": None})
        assert resp.status_code == 200, resp.text
        assert resp.json()["voice_provider_id"] is None


# ============================================================================
# Provider eligibility
# ============================================================================

class TestVoiceProviders:
    def test_empty_when_nothing_configured(self, authenticated_client, mock_db):
        resp = authenticated_client.get(f"{VOICE_ROOT}/providers")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    def test_openai_api_key_row_is_eligible(self, authenticated_client, mock_db):
        _make_provider(name="Prod OpenAI")
        resp = authenticated_client.get(f"{VOICE_ROOT}/providers")
        assert resp.status_code == 200, resp.text
        assert resp.json() == [{"id": "prov-openai", "name": "Prod OpenAI"}]
        # The token blob never travels.
        assert RAW_KEY not in resp.text

    @pytest.mark.parametrize("kwargs", [
        {"provider_type": "chatgpt"},                     # subscription row
        {"provider_type": "openai", "auth_mode": "chatgpt"},  # codex auth mode
        {"provider_type": "openrouter"},                  # wrong vendor
        {"provider_type": "custom"},                      # wrong vendor
        {"enabled": False},                               # disabled
        {"auth_token": None},                             # no stored key
    ])
    def test_ineligible_rows_are_filtered(self, authenticated_client, mock_db, kwargs):
        _make_provider("prov-x", **kwargs)
        resp = authenticated_client.get(f"{VOICE_ROOT}/providers")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []


# ============================================================================
# Session creation
# ============================================================================

class TestCreateVoiceSession:
    def test_disabled_flag_returns_403(self, authenticated_client, mock_db):
        _make_provider()
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={})
        assert resp.status_code == 403, resp.text

    def test_no_eligible_provider_returns_400(self, authenticated_client, mock_db):
        _enable_voice()
        _make_provider("prov-chatgpt", provider_type="chatgpt")
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={})
        assert resp.status_code == 400, resp.text
        assert "OpenAI provider" in resp.json()["detail"]

    def test_ambiguous_providers_return_400(self, authenticated_client, mock_db):
        _enable_voice()
        _make_provider("prov-a", name="A")
        _make_provider("prov-b", name="B")
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={})
        assert resp.status_code == 400, resp.text
        assert "more than one" in resp.json()["detail"].lower()

    def test_explicit_provider_disambiguates(self, authenticated_client, mock_db, monkeypatch):
        _enable_voice()
        _make_provider("prov-a", name="A")
        _make_provider("prov-b", name="B")
        _install_mock_transport(monkeypatch, _secret_handler())

        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={"provider_id": "prov-b"})
        assert resp.status_code == 200, resp.text
        row = db.get_session(resp.json()["session_id"])
        assert row["provider_id"] == "prov-b"

    def test_ineligible_explicit_provider_returns_400(self, authenticated_client, mock_db):
        _enable_voice()
        _make_provider()
        _make_provider("prov-chatgpt", provider_type="chatgpt")
        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions", json={"provider_id": "prov-chatgpt"}
        )
        assert resp.status_code == 400, resp.text

    def test_settings_provider_wins_over_the_single_row_fallback(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _enable_voice(voice_provider_id="prov-b")
        _make_provider("prov-a", name="A")
        _make_provider("prov-b", name="B")
        _install_mock_transport(monkeypatch, _secret_handler())

        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={})
        assert resp.status_code == 200, resp.text
        assert db.get_session(resp.json()["session_id"])["provider_id"] == "prov-b"

    def test_happy_path(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _enable_voice(voice_model="gpt-realtime-2.1-mini")
        # Store the key the way the provider API does — encrypted — to prove the
        # shared omni decryption path is what feeds the mint.
        _make_provider(auth_token=encryption.encrypt_value(RAW_KEY))
        captured = {}
        _install_mock_transport(monkeypatch, _secret_handler(captured))

        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions", json={"profile_id": "default"}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["client_secret"] == "ek_test_secret"
        assert body["expires_at"] == 1786000000
        assert body["model"] == "gpt-realtime-2.1-mini"

        # Upstream request shape.
        request = captured["request"]
        assert str(request.url) == "https://api.openai.com/v1/realtime/client_secrets"
        assert request.headers["authorization"] == f"Bearer {RAW_KEY}"
        import json as _json
        sent = _json.loads(request.content)
        assert sent["session"]["type"] == "realtime"
        assert sent["session"]["model"] == "gpt-realtime-2.1-mini"
        assert sent["session"]["audio"]["input"]["transcription"]["model"] == "gpt-4o-mini-transcribe"
        assert sent["expires_after"]["anchor"] == "created_at"

        # The session row is real, badged voice, and bound to the provider.
        row = db.get_session(body["session_id"])
        assert row is not None
        assert row["mode"] == "voice"
        assert row["provider_id"] == "prov-openai"
        assert row["title"] == "Voice session"

    def test_openai_error_surfaces_as_502(self, authenticated_client, mock_db, monkeypatch):
        _enable_voice()
        _make_provider()
        _install_mock_transport(
            monkeypatch,
            _secret_handler(status_code=500, body={"error": {"message": "upstream exploded"}}),
        )

        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={})
        assert resp.status_code == 502, resp.text
        detail = resp.json()["detail"]
        assert "500" in detail
        assert "upstream exploded" in detail
        # No orphan session row was written.
        assert db.get_sessions(limit=50) == []

    def test_transport_failure_surfaces_as_502(self, authenticated_client, mock_db, monkeypatch):
        _enable_voice()
        _make_provider()

        def _boom(request):
            raise httpx.ConnectError("no route to host")

        _install_mock_transport(monkeypatch, _boom)
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={})
        assert resp.status_code == 502, resp.text
