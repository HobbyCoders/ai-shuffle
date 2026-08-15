"""API tests for voice mode (waves VW1 + VW3 of docs/VOICE_MODE_PLAN.md).

Covers:

* ``GET/PUT /api/v1/settings/voice`` — the four runtime-editable knobs
  (VM13/VM17): roundtrip, model allowlist, provider-reference validation.
* ``GET /api/v1/voice/providers`` — eligibility filtering (VM3: an enabled
  ``openai`` row with a stored API key; never a ChatGPT/Codex subscription row,
  because Realtime is API-key only).
* ``POST /api/v1/voice/sessions`` — flag gate, provider resolution, the
  ephemeral client-secret mint and the ``mode='voice'`` session row.
* VW3's call surface — ``/secret`` re-mint, ``/call`` attach + replace,
  ``/hangup`` and ``/state``.

NO network anywhere: upstream HTTP is stubbed via ``httpx.MockTransport`` (the
shared client the route uses is swapped via a monkeypatch on
``app.core.http_client.get_http_client``, which the route imports at call time,
mirroring tests/api/test_fetch_models_api.py), and the sideband WebSocket is
stubbed at ``app.core.voice_bridge._open_sideband``.
"""

import asyncio
import json

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
        ("post", f"{VOICE_ROOT}/preview"),
        ("post", f"{VOICE_ROOT}/sessions"),
        ("post", f"{VOICE_ROOT}/sessions/s1/secret"),
        ("post", f"{VOICE_ROOT}/sessions/s1/call"),
        ("post", f"{VOICE_ROOT}/sessions/s1/hangup"),
        ("get", f"{VOICE_ROOT}/sessions/s1/state"),
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
            "voice_voice": "marin",
            "voice_provider_id": None,
            "voice_max_call_minutes": 240,
        }

    def test_put_roundtrip(self, authenticated_client, mock_db):
        pid = _make_provider()
        resp = authenticated_client.put(VOICE_SETTINGS, json={
            "voice_mode_enabled": True,
            "voice_model": "gpt-realtime-2.1-mini",
            "voice_voice": "cedar",
            "voice_provider_id": pid,
            "voice_max_call_minutes": 30,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "voice_mode_enabled": True,
            "voice_model": "gpt-realtime-2.1-mini",
            "voice_voice": "cedar",
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

    def test_voice_allowlist_rejects_unknown(self, authenticated_client, mock_db):
        resp = authenticated_client.put(VOICE_SETTINGS, json={"voice_voice": "hal9000"})
        assert resp.status_code == 400
        # Nothing was written.
        assert authenticated_client.get(VOICE_SETTINGS).json()["voice_voice"] == "marin"

    @pytest.mark.parametrize("voice", [
        "marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage",
        "shimmer", "verse",
    ])
    def test_voice_allowlist_accepts_each(self, authenticated_client, mock_db, voice):
        resp = authenticated_client.put(VOICE_SETTINGS, json={"voice_voice": voice})
        assert resp.status_code == 200, resp.text
        assert resp.json()["voice_voice"] == voice

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

class TestVoicePreview:
    def test_unknown_voice_returns_400(self, authenticated_client, mock_db):
        _make_provider()
        resp = authenticated_client.post(f"{VOICE_ROOT}/preview", json={"voice": "hal9000"})
        assert resp.status_code == 400, resp.text

    def test_no_eligible_provider_returns_400(self, authenticated_client, mock_db):
        resp = authenticated_client.post(f"{VOICE_ROOT}/preview", json={"voice": "marin"})
        assert resp.status_code == 400, resp.text
        assert "OpenAI provider" in resp.json()["detail"]

    def test_happy_path_streams_audio(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_KEY))
        captured = {}

        def _handler(request: httpx.Request) -> httpx.Response:
            captured["request"] = request
            return httpx.Response(
                200, content=b"\xff\xf3mp3-bytes", headers={"content-type": "audio/mpeg"}
            )

        _install_mock_transport(monkeypatch, _handler)
        resp = authenticated_client.post(f"{VOICE_ROOT}/preview", json={"voice": "cedar"})
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("audio/mpeg")
        assert resp.content == b"\xff\xf3mp3-bytes"

        request = captured["request"]
        assert str(request.url) == "https://api.openai.com/v1/audio/speech"
        assert request.headers["authorization"] == f"Bearer {RAW_KEY}"
        sent = json.loads(request.content)
        assert sent["voice"] == "cedar"
        assert sent["model"] == "gpt-4o-mini-tts"
        assert sent["input"]

    def test_does_not_require_voice_mode_enabled(self, authenticated_client, mock_db, monkeypatch):
        # Picking a voice is part of setting the feature up before enabling it.
        _unlock(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_KEY))
        _install_mock_transport(
            monkeypatch,
            lambda request: httpx.Response(
                200, content=b"ok", headers={"content-type": "audio/mpeg"}
            ),
        )
        assert db.get_system_setting("voice_mode_enabled") is None
        resp = authenticated_client.post(f"{VOICE_ROOT}/preview", json={"voice": "marin"})
        assert resp.status_code == 200, resp.text

    def test_upstream_rejection_surfaces_as_502(self, authenticated_client, mock_db, monkeypatch):
        _unlock(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_KEY))
        _install_mock_transport(
            monkeypatch,
            lambda request: httpx.Response(400, json={"error": {"message": "bad voice"}}),
        )
        resp = authenticated_client.post(f"{VOICE_ROOT}/preview", json={"voice": "verse"})
        assert resp.status_code == 502, resp.text
        assert "bad voice" in resp.json()["detail"]


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
        _enable_voice(voice_model="gpt-realtime-2.1-mini", voice_voice="cedar")
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
        # The configured voice rides on the mint (VM21) — it can only be set
        # before the model first speaks.
        assert sent["session"]["audio"]["output"]["voice"] == "cedar"
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


# ============================================================================
# VW3 — call surface (re-mint / attach / hangup / state)
# ============================================================================

SESSION_ID = "voice-sess-1"


class FakeSideband:
    """In-process stand-in for the sideband WebSocket (no OpenAI, no network)."""

    def __init__(self):
        self.sent = []
        self.closed = False
        self._queue: asyncio.Queue = asyncio.Queue()

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def close(self):
        self.closed = True
        await self._queue.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if await self._queue.get() is None:
            raise StopAsyncIteration


def _mk_voice_session(session_id=SESSION_ID, *, provider_id="prov-openai", mode="voice"):
    db.create_session(
        session_id=session_id,
        profile_id="default",
        title="Voice session",
        provider_id=provider_id,
        mode=mode,
    )
    return session_id


@pytest.fixture
def sidebands(monkeypatch):
    """Stub the sideband transport; yields the sockets handed out."""
    from app.core import voice_bridge

    sockets = []

    async def _open(call_id, api_key, client_secret=None):
        sock = FakeSideband()
        sock.call_id = call_id
        sock.api_key = api_key
        sock.client_secret = client_secret
        sockets.append(sock)
        return sock

    monkeypatch.setattr(voice_bridge, "_open_sideband", _open)
    yield sockets
    # The module-level registry outlives a test — never leak a bridge, a call
    # epoch or a cap marker into the next one.
    voice_bridge.registry._bridges.clear()
    voice_bridge.registry._epochs.clear()
    voice_bridge.registry._capped.clear()
    # Same reason: the minted-secret memory is module state on the route.
    from app.api import voice as voice_api
    voice_api._SESSION_CLIENT_SECRETS.clear()


class TestVoiceSecretRemint:
    def test_flag_off_returns_403(self, authenticated_client, mock_db):
        _make_provider()
        _mk_voice_session()
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/secret")
        assert resp.status_code == 403, resp.text

    def test_missing_session_returns_404(self, authenticated_client, mock_db):
        _enable_voice()
        _make_provider()
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions/nope/secret")
        assert resp.status_code == 404, resp.text

    def test_chat_session_returns_404(self, authenticated_client, mock_db):
        _enable_voice()
        _make_provider()
        _mk_voice_session("chat-sess", mode="chat")
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions/chat-sess/secret")
        assert resp.status_code == 404, resp.text

    def test_remint_uses_the_sessions_own_provider(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _unlock(mock_db)
        _enable_voice(voice_model="gpt-realtime-2.1-mini")
        _make_provider("prov-a", name="A", auth_token=encryption.encrypt_value(RAW_KEY))
        _make_provider("prov-b", name="B", auth_token=encryption.encrypt_value("sk-other"))
        _mk_voice_session(provider_id="prov-b")
        captured = {}
        _install_mock_transport(monkeypatch, _secret_handler(captured))

        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/secret")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "client_secret": "ek_test_secret",
            "expires_at": 1786000000,
            "model": "gpt-realtime-2.1-mini",
        }
        # Funded by the row the session was created on, not the ambiguous set.
        assert captured["request"].headers["authorization"] == "Bearer sk-other"

    def test_openai_failure_surfaces_as_502(self, authenticated_client, mock_db, monkeypatch):
        _enable_voice()
        _make_provider()
        _mk_voice_session()
        _install_mock_transport(monkeypatch, _secret_handler(status_code=500))
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/secret")
        assert resp.status_code == 502, resp.text


class TestVoiceCallAttach:
    def test_flag_off_returns_403(self, authenticated_client, mock_db, sidebands):
        _make_provider()
        _mk_voice_session()
        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_1"}
        )
        assert resp.status_code == 403, resp.text
        assert sidebands == []

    @pytest.mark.parametrize("session_id,mode", [("nope", None), ("chat-sess", "chat")])
    def test_missing_or_chat_session_returns_404(
        self, authenticated_client, mock_db, sidebands, session_id, mode
    ):
        _enable_voice()
        _make_provider()
        if mode:
            _mk_voice_session(session_id, mode=mode)
        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{session_id}/call", json={"call_id": "rtc_1"}
        )
        assert resp.status_code == 404, resp.text

    def test_blank_call_id_returns_400(self, authenticated_client, mock_db, sidebands):
        _enable_voice()
        _make_provider()
        _mk_voice_session()
        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "  "}
        )
        assert resp.status_code == 400, resp.text

    def test_attach_opens_the_sideband_and_configures_it(
        self, authenticated_client, mock_db, sidebands
    ):
        _unlock(mock_db)
        _enable_voice()
        _make_provider(auth_token=encryption.encrypt_value(RAW_KEY))
        _mk_voice_session()

        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_1"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}

        assert len(sidebands) == 1
        assert sidebands[0].call_id == "rtc_1"
        assert sidebands[0].api_key == RAW_KEY       # decrypted server-side only
        update = sidebands[0].sent[0]
        assert update["type"] == "session.update"
        assert update["session"]["audio"]["input"]["turn_detection"]["type"] == "semantic_vad"

        authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup")

    def test_attach_hands_the_minted_secret_to_the_sideband(
        self, authenticated_client, mock_db, sidebands, monkeypatch
    ):
        """The ek the browser created the call with is the handshake fallback.

        OpenAI answers an API-key sideband handshake for a browser-created
        WebRTC call with ``call_id_not_found``; the credential that reportedly
        works is the ephemeral one, so the route has to keep it.
        """
        from app.api import voice as voice_api

        _unlock(mock_db)
        _enable_voice()
        _make_provider(auth_token=encryption.encrypt_value(RAW_KEY))
        _mk_voice_session()
        _install_mock_transport(monkeypatch, _secret_handler(
            # Far-future expiry: the route drops a secret that is already past
            # its expires_at (see test_expired_secret_is_not_offered_...).
            body={"value": "ek_test_secret", "expires_at": 4102444800},
        ))

        assert authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/secret"
        ).status_code == 200

        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_1"}
        )
        assert resp.status_code == 200, resp.text
        assert sidebands[0].api_key == RAW_KEY
        assert sidebands[0].client_secret == "ek_test_secret"

        # Hanging up forgets it — a new call mints its own.
        authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup")
        assert voice_api._recall_client_secret(SESSION_ID) is None

    def test_attach_without_a_minted_secret_still_works(
        self, authenticated_client, mock_db, sidebands
    ):
        _enable_voice()
        _make_provider()
        _mk_voice_session()

        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_1"}
        )
        assert resp.status_code == 200, resp.text
        assert sidebands[0].client_secret is None
        authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup")

    def test_expired_secret_is_not_offered_to_the_sideband(
        self, authenticated_client, mock_db, sidebands
    ):
        from app.api import voice as voice_api

        _enable_voice()
        _make_provider()
        _mk_voice_session()
        voice_api._SESSION_CLIENT_SECRETS[SESSION_ID] = ("ek_stale", 1.0)

        authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_1"}
        )
        assert sidebands[0].client_secret is None
        authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup")

    def test_second_call_id_replaces_the_first(
        self, authenticated_client, mock_db, sidebands
    ):
        _enable_voice()
        _make_provider()
        _mk_voice_session()

        authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_1"}
        )
        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_2"}
        )
        assert resp.status_code == 200, resp.text
        assert [s.call_id for s in sidebands] == ["rtc_1", "rtc_2"]
        assert sidebands[0].closed is True            # old attachment closed first
        assert sidebands[1].closed is False

        from app.core import voice_bridge
        assert voice_bridge.registry.get(SESSION_ID).call_id == "rtc_2"
        authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup")

    def test_connect_failure_surfaces_as_502(
        self, authenticated_client, mock_db, monkeypatch
    ):
        from app.core import voice_bridge

        _enable_voice()
        _make_provider()
        _mk_voice_session()

        async def _boom(call_id, api_key, client_secret=None):
            raise ConnectionError("handshake refused")

        monkeypatch.setattr(voice_bridge, "_open_sideband", _boom)
        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_1"}
        )
        assert resp.status_code == 502, resp.text
        assert voice_bridge.registry.get(SESSION_ID) is None


class TestVoiceHangup:
    def test_hangup_without_a_bridge_is_ok(self, authenticated_client, mock_db, sidebands):
        _enable_voice()
        _mk_voice_session()
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"ok": True}

    def test_hangup_stops_the_bridge_and_is_idempotent(
        self, authenticated_client, mock_db, sidebands
    ):
        from app.core import voice_bridge

        _enable_voice()
        _make_provider()
        _mk_voice_session()
        authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_1"}
        )

        assert authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup"
        ).status_code == 200
        assert sidebands[0].closed is True
        assert voice_bridge.registry.get(SESSION_ID) is None
        # Hanging up twice is fine.
        assert authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup"
        ).status_code == 200

    def test_hangup_on_a_missing_session_returns_404(self, authenticated_client, mock_db):
        _enable_voice()
        assert authenticated_client.post(
            f"{VOICE_ROOT}/sessions/nope/hangup"
        ).status_code == 404


class TestVoiceState:
    def test_state_without_a_bridge(self, authenticated_client, mock_db):
        _enable_voice()
        _mk_voice_session()
        resp = authenticated_client.get(f"{VOICE_ROOT}/sessions/{SESSION_ID}/state")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "active": False, "connected": False, "started_at": None, "model": None,
            "usage": {}, "estimated_cost_usd": None,
        }

    def test_state_while_attached(self, authenticated_client, mock_db, sidebands):
        _enable_voice(voice_model="gpt-realtime-2.1-mini")
        _make_provider()
        _mk_voice_session()
        authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_1"}
        )

        body = authenticated_client.get(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/state"
        ).json()
        assert body["active"] is True
        assert body["connected"] is True
        assert body["model"] == "gpt-realtime-2.1-mini"
        assert isinstance(body["started_at"], float)

        authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup")
        after = authenticated_client.get(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/state"
        ).json()
        assert after == {
            "active": False, "connected": False, "started_at": None, "model": None,
            "usage": {}, "estimated_cost_usd": None,
        }

    def test_state_for_a_chat_session_returns_404(self, authenticated_client, mock_db):
        _mk_voice_session("chat-sess", mode="chat")
        assert authenticated_client.get(
            f"{VOICE_ROOT}/sessions/chat-sess/state"
        ).status_code == 404

    def test_state_reports_usage_and_a_cost_estimate(
        self, authenticated_client, mock_db, sidebands
    ):
        from app.core import voice_bridge

        _enable_voice(voice_model="gpt-realtime-2.1")
        _make_provider()
        _mk_voice_session()
        authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_1"}
        )

        # Stand in for a `response.done` the fake socket cannot deliver.
        voice_bridge.registry.get(SESSION_ID)._accumulate_usage({
            "input_tokens": 1_000, "output_tokens": 500,
            "input_token_details": {"text_tokens": 200, "audio_tokens": 800},
            "output_token_details": {"text_tokens": 100, "audio_tokens": 400},
        })

        body = authenticated_client.get(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/state"
        ).json()
        assert body["usage"] == {
            "input_tokens": 1_000, "output_tokens": 500,
            "input_text_tokens": 200, "input_audio_tokens": 800,
            "output_text_tokens": 100, "output_audio_tokens": 400,
        }
        # (200·$4 + 100·$24 + 800·$32 + 400·$64) / 1M tokens.
        assert body["estimated_cost_usd"] == pytest.approx(0.0544)

        authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup")


# ============================================================================
# VW5 — the post-cap grace window
# ============================================================================

CAP_DETAIL = "Call length cap reached (240 minutes). Hang up and start a new call."


class TestCallLengthCapGrace:
    """Once the cap fires, an automatic reconnect must not restart the clock."""

    def _capped_session(self):
        from app.core import voice_bridge

        _enable_voice()
        _make_provider()
        _mk_voice_session()
        voice_bridge.registry.mark_capped(SESSION_ID, 240)

    def test_attach_returns_409_inside_the_window(
        self, authenticated_client, mock_db, sidebands
    ):
        self._capped_session()
        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_2"}
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == CAP_DETAIL
        assert sidebands == []                     # no new sideband was opened

    def test_secret_remint_returns_409_inside_the_window(
        self, authenticated_client, mock_db, sidebands, monkeypatch
    ):
        self._capped_session()
        _install_mock_transport(monkeypatch, _secret_handler())
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/secret")
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == CAP_DETAIL

    def test_hangup_clears_the_window_so_a_new_call_works(
        self, authenticated_client, mock_db, sidebands
    ):
        self._capped_session()
        assert authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup"
        ).status_code == 200

        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_3"}
        )
        assert resp.status_code == 200, resp.text
        assert [s.call_id for s in sidebands] == ["rtc_3"]
        authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup")

    def test_the_window_expires(
        self, authenticated_client, mock_db, sidebands, monkeypatch
    ):
        from app.core import voice_bridge

        self._capped_session()
        monkeypatch.setattr(voice_bridge, "CAP_GRACE_SECONDS", -1.0)
        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_4"}
        )
        assert resp.status_code == 200, resp.text
        authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup")
