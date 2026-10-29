"""Grok voice funded by a connected SuperGrok login (``grok-voice-supergrok``).

Engine selection, provider separation (no xAI API-key fallback either way),
login-required refusals, credential redaction, the audio route's stream class
and the inherited same-chat delegation. No network: provider sockets, token
loads and refreshes are fakes.
"""
import json
import logging
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, Mock

import pytest

from app.core.voice_models import GROK_MODEL, GROK_VOICES, SUPERGROK_MODEL, GROK_URL, model_details, transport_for
from app.db import database as db
from tests.api.test_voice_api import VOICE_ROOT, VOICE_SETTINGS, _api_user, _enable_voice, _make_provider
from tests.unit.test_voice_stream import Upstream

FAKE_LOGIN_TOKEN = "fake-supergrok-access-token"
LOGIN_REQUIRED = (
    "Connect your SuperGrok login in Settings > Providers to use this voice engine. "
    "It does not use an xAI API key."
)


def _make_login(provider_id="prov-grok", *, name="SuperGrok", needs_reauth=False):
    _make_provider(provider_id, name=name, provider_type="grok", auth_token=None, auth_mode="grok")
    db.update_model_provider(provider_id, codex_access_token="fake-stored-token")
    if needs_reauth:
        db.update_model_provider(provider_id, needs_reauth=True)
    return provider_id


def _load_login(monkeypatch, **kwargs):
    from app.core.provider import fluid_credentials
    load = AsyncMock(**({"return_value": FAKE_LOGIN_TOKEN} | kwargs))
    monkeypatch.setattr(fluid_credentials, "_load_grok_access_token", load)
    return load


def test_engine_details_follow_grok_voice_without_api_key_preview():
    details = model_details(SUPERGROK_MODEL)
    assert transport_for(SUPERGROK_MODEL) == "stream"
    assert details["label"] == "Grok voice (SuperGrok subscription)"
    assert details["available"] is True
    assert [v["id"] for v in details["voices"]] == list(GROK_VOICES)
    assert {"label": "Turn detection", "value": "server_vad"} in details["runtime_settings"]
    assert "SuperGrok login" in details["preview_note"]
    assert "unofficial" not in json.dumps(details).lower()


def test_settings_select_the_engine_explicitly(authenticated_client, mock_db):
    c = authenticated_client
    assert c.get(VOICE_SETTINGS).json()["voice_model"] == "gpt-live-1"  # Never the default.
    r = c.put(VOICE_SETTINGS, json={"voice_model": SUPERGROK_MODEL, "voice_provider_id": None})
    assert r.status_code == 200, r.text
    assert r.json()["voice_voice"] == "eve"
    assert r.json()["model_details"]["model"] == SUPERGROK_MODEL
    assert c.put(VOICE_SETTINGS, json={"voice_voice": "ara"}).json()["voice_voice"] == "ara"
    assert c.get(VOICE_SETTINGS).json()["voice_model"] == SUPERGROK_MODEL


def test_settings_reject_an_api_key_provider_pin(authenticated_client, mock_db):
    _make_provider("xai-key", name="xAI", provider_type="xai")
    r = authenticated_client.put(VOICE_SETTINGS, json={"voice_model": SUPERGROK_MODEL, "voice_provider_id": "xai-key"})
    assert r.status_code == 400
    assert db.get_system_setting("voice_model") is None


def test_funding_paths_never_cross():
    from app.api.voice import provider_matches_model
    login = {"provider_type": "grok", "auth_mode": "grok"}
    xai_row_with_login = {"provider_type": "xai", "auth_mode": "grok"}
    assert provider_matches_model(login, SUPERGROK_MODEL)
    assert provider_matches_model(xai_row_with_login, SUPERGROK_MODEL)
    for model in (GROK_MODEL, "gpt-live-1", "gpt-live-1-codex", "gpt-realtime"):
        assert not provider_matches_model(login, model)
        assert not provider_matches_model(xai_row_with_login, model)
    for row in ({"provider_type": "xai"}, {"provider_type": "openai"}, {"provider_type": "chatgpt"},
                {"provider_type": "chatgpt", "auth_mode": "grok"}):
        assert not provider_matches_model(row, SUPERGROK_MODEL)


def _logins_and_key():
    _make_login("prov-stale", name="Stale", needs_reauth=True)
    _make_provider("prov-empty", name="Empty", provider_type="grok", auth_token=None, auth_mode="grok")
    # The connected login is the selected account in grok:subscription.
    _make_login()
    _make_provider("xai-key", name="xAI", provider_type="xai")


def test_providers_list_a_connected_login_without_credentials(authenticated_client, mock_db):
    _logins_and_key()
    listed = authenticated_client.get(f"{VOICE_ROOT}/providers").json()
    assert {p["id"]: p["provider_type"] for p in listed} == {"prov-grok": "grok", "xai-key": "xai"}
    assert "token" not in json.dumps(listed)


def test_providers_hide_the_owner_login_from_api_users(client, mock_db):
    _logins_and_key()
    listed = client.get(f"{VOICE_ROOT}/providers", headers=_api_user("voice-api", "aih_supergrok_list")).json()
    assert [p["id"] for p in listed] == ["xai-key"]


class TestCreateSession:
    @pytest.mark.parametrize("selection", ["pinned", "explicit", "automatic"])
    def test_no_login_is_a_clear_400_without_api_key_fallback(self, authenticated_client, mock_db, monkeypatch, selection):
        from app.api import voice
        _enable_voice(voice_model=SUPERGROK_MODEL, voice_voice="eve",
                      voice_provider_id="xai-key" if selection == "pinned" else None)
        _make_provider("xai-key", name="xAI", provider_type="xai")  # Cannot substitute for the login.
        credentials = AsyncMock(side_effect=AssertionError("no API-key fallback"))
        monkeypatch.setattr(voice, "_provider_api_key", credentials)
        body = {"model": "sonnet", "provider_id": "xai-key"} if selection == "explicit" else {"model": "sonnet"}
        response = authenticated_client.post(f"{VOICE_ROOT}/sessions", json=body)
        assert response.status_code == 400, response.text
        assert response.json()["detail"] == LOGIN_REQUIRED
        credentials.assert_not_called()

    def test_api_key_grok_engine_never_uses_the_login(self, authenticated_client, mock_db, monkeypatch):
        _enable_voice(voice_model=GROK_MODEL, voice_voice="eve")
        _make_login()
        load = _load_login(monkeypatch, side_effect=AssertionError("API-key engine must not load the login"))
        response = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={"model": "sonnet"})
        assert response.status_code == 400
        assert "xAI" in response.json()["detail"]
        load.assert_not_called()

    @pytest.mark.parametrize("selection", ["pinned", "explicit", "automatic"])
    def test_api_user_refused_before_credentials(self, client, mock_db, monkeypatch, selection):
        from app.api import voice
        _enable_voice(voice_model=SUPERGROK_MODEL, voice_voice="eve",
                      voice_provider_id="prov-grok" if selection == "pinned" else None)
        _make_login()
        credentials = AsyncMock(side_effect=AssertionError("must refuse before loading credentials"))
        load = _load_login(monkeypatch, side_effect=AssertionError("must not load the owner's login"))
        create = Mock(side_effect=AssertionError("must refuse before creating a session"))
        monkeypatch.setattr(voice, "_provider_api_key", credentials)
        monkeypatch.setattr(db, "create_session", create)
        body = {"model": "sonnet", "profile_id": "default"}
        if selection == "explicit":
            body["provider_id"] = "prov-grok"
        response = client.post(f"{VOICE_ROOT}/sessions", json=body,
                               headers=_api_user("voice-api", "aih_supergrok_session"))
        assert response.status_code == 400, response.text
        credentials.assert_not_called()
        load.assert_not_called()
        create.assert_not_called()

    @pytest.mark.parametrize("pinned", [False, True])
    def test_login_pins_the_call_without_minting_or_leaking(self, authenticated_client, mock_db, monkeypatch, caplog, pinned):
        from app.api import voice
        caplog.set_level(logging.DEBUG)
        _enable_voice(voice_model=SUPERGROK_MODEL, voice_voice="ara",
                      voice_provider_id="prov-grok" if pinned else None)
        _make_provider("xai-key", name="A xAI", provider_type="xai")  # Sorted first; must stay unused.
        _make_login()
        load = _load_login(monkeypatch)
        decrypt = Mock(side_effect=AssertionError("must not read an API-key column"))
        monkeypatch.setattr("app.core.provider.fluid_credentials._decrypt_auth_token", decrypt)
        mint = AsyncMock(side_effect=AssertionError("must never mint an API-key secret"))
        monkeypatch.setattr(voice, "_mint_client_secret", mint)
        response = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={"model": "sonnet", "profile_id": "default"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["model"] == SUPERGROK_MODEL and body["client_secret"] == ""
        row = db.get_session(body["session_id"])
        config = json.loads(row["voice_config"])
        assert (config["model"], config["voice"], config["provider_id"]) == (SUPERGROK_MODEL, "ara", "prov-grok")
        assert row["model"] == "sonnet" and row["provider_id"] is None  # Chat selection stays independent.
        load.assert_awaited_once_with("prov-grok")
        mint.assert_not_called()
        decrypt.assert_not_called()
        assert FAKE_LOGIN_TOKEN not in response.text
        assert FAKE_LOGIN_TOKEN not in caplog.text

    def test_reauth_or_removed_login_is_sanitized(self, authenticated_client, mock_db, monkeypatch):
        from app.core.provider import fluid_credentials
        _enable_voice(voice_model=SUPERGROK_MODEL, voice_voice="eve", voice_provider_id="prov-grok")
        _make_login()
        _load_login(monkeypatch, side_effect=fluid_credentials.ProviderCredentialError("synthetic-private-detail"))
        response = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={"model": "sonnet", "profile_id": "default"})
        assert response.status_code == 400
        assert response.json()["detail"] == "SuperGrok login is unavailable. Reconnect it in Settings > Providers."
        assert "synthetic-private-detail" not in response.text

    def test_needs_reauth_login_is_not_eligible(self, authenticated_client, mock_db, monkeypatch):
        _enable_voice(voice_model=SUPERGROK_MODEL, voice_voice="eve")
        _make_login(needs_reauth=True)
        load = _load_login(monkeypatch, side_effect=AssertionError("ineligible login must not load"))
        response = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={"model": "sonnet"})
        assert response.status_code == 400
        assert response.json()["detail"] == LOGIN_REQUIRED
        load.assert_not_called()

    def test_preview_is_refused_without_provider_traffic(self, authenticated_client, mock_db, monkeypatch):
        from app.core import voice_preview
        _make_login()
        generate = AsyncMock(side_effect=AssertionError("no preview for this engine"))
        monkeypatch.setattr(voice_preview, "generate_live_preview", generate)
        response = authenticated_client.post(f"{VOICE_ROOT}/preview", json={"voice": "eve", "model": SUPERGROK_MODEL})
        assert response.status_code == 400
        generate.assert_not_called()


@pytest.mark.asyncio
async def test_audio_route_runs_the_supergrok_stream_with_the_login(monkeypatch):
    from app.api import voice
    from app.core import voice_grok, voice_stream
    websocket = AsyncMock()
    websocket.cookies = {}
    websocket.scope = {"type": "websocket"}
    created = []

    class FakeStream:
        def __init__(self, ws, row, config, key, minutes, api_user_id=None, caller=None):
            created.append((config["model"], key, api_user_id))
            self.run = AsyncMock()

    @asynccontextmanager
    async def no_watch(*args, **kwargs):
        yield

    config = {"model": SUPERGROK_MODEL, "voice": "eve", "provider_id": "prov-grok"}
    monkeypatch.setattr("app.api.ws_auth.authenticate_websocket", AsyncMock(return_value=(True, None)))
    monkeypatch.setattr("app.api.ws_key_watch.watch_api_key", no_watch)
    monkeypatch.setattr(voice, "_require_voice_enabled", AsyncMock(return_value={"voice_max_call_minutes": 5}))
    monkeypatch.setattr(voice, "_load_voice_session", AsyncMock(return_value={"id": "call"}))
    monkeypatch.setattr(voice, "_check_voice_session_access", Mock())
    monkeypatch.setattr(voice, "_reject_when_capped", Mock())
    monkeypatch.setattr(voice, "session_config", Mock(return_value=config))
    monkeypatch.setattr(voice, "_resolve_provider_row", Mock(return_value={"id": "prov-grok", "provider_type": "grok"}))
    availability = Mock(side_effect=AssertionError("no native voice runtime is needed"))
    monkeypatch.setattr("app.core.voice_dependencies.availability", availability)
    key = AsyncMock(return_value=FAKE_LOGIN_TOKEN)
    monkeypatch.setattr(voice, "_provider_api_key", key)
    monkeypatch.setattr(voice_grok, "SuperGrokVoiceStream", FakeStream)
    await voice.voice_audio_socket(websocket, "call", token=None, resume=None)
    websocket.accept.assert_awaited_once()
    assert created == [(SUPERGROK_MODEL, FAKE_LOGIN_TOKEN, None)]
    assert key.await_args.kwargs == {"api_user": None}
    assert "call" not in voice_stream.calls


def _stream(token=FAKE_LOGIN_TOKEN):
    from app.core.voice_grok import SuperGrokVoiceStream
    return SuperGrokVoiceStream(AsyncMock(), {"id": "s"},
                                {"model": SUPERGROK_MODEL, "voice": "eve", "provider_id": "prov-grok"}, token, 5)


@pytest.mark.asyncio
async def test_stream_opens_xai_realtime_with_the_login_and_drops_it(monkeypatch):
    from app.core import voice_grok
    connect = AsyncMock(return_value="socket")
    monkeypatch.setattr(voice_grok, "connect", connect)
    call = _stream()
    assert await call.open_upstream() == "socket"
    assert connect.await_args.args == (GROK_URL,)
    assert connect.await_args.kwargs["additional_headers"] == {"Authorization": "Bearer " + FAKE_LOGIN_TOKEN}
    assert call.api_key == ""  # Renewals read the login fresh instead (connect_provider).


def _invalid_status(status):
    from websockets.datastructures import Headers
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response
    return InvalidStatus(Response(status, "x", Headers({"x-echo": FAKE_LOGIN_TOKEN}), b""))


@pytest.mark.asyncio
@pytest.mark.parametrize("renewed,expected", [
    (True, "The SuperGrok login was renewed. Start the call again."),
    (False, "SuperGrok refused the login. Reconnect SuperGrok in Settings > Providers; there is no API-key fallback."),
])
async def test_401_renews_through_the_provider_path_without_retrying(monkeypatch, caplog, renewed, expected):
    from app.core import voice_grok, xai_token_refresh
    caplog.set_level(logging.DEBUG)
    connect = AsyncMock(side_effect=_invalid_status(401))
    refresh = AsyncMock(return_value=renewed)
    monkeypatch.setattr(voice_grok, "connect", connect)
    monkeypatch.setattr(xai_token_refresh, "refresh_on_401_once", refresh)
    with pytest.raises(ValueError) as raised:
        await _stream().open_upstream()
    assert str(raised.value) == expected
    assert raised.value.__cause__ is None and raised.value.__context__ is None
    assert connect.await_count == 1  # No automatic retry.
    refresh.assert_awaited_once_with("prov-grok", stale_access_token=FAKE_LOGIN_TOKEN)
    assert FAKE_LOGIN_TOKEN not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("failure,expected", [
    (_invalid_status(403), "SuperGrok voice refused the connection (HTTP 403). Access may be unavailable for this subscription."),
    (OSError("Authorization: Bearer " + FAKE_LOGIN_TOKEN), "SuperGrok voice could not connect. Try again shortly."),
])
async def test_other_connect_failures_are_redacted_and_never_refresh(monkeypatch, failure, expected):
    from app.core import voice_grok, xai_token_refresh
    refresh = AsyncMock(side_effect=AssertionError("only a 401 renews the login"))
    monkeypatch.setattr(voice_grok, "connect", AsyncMock(side_effect=failure))
    monkeypatch.setattr(xai_token_refresh, "refresh_on_401_once", refresh)
    with pytest.raises(ValueError) as raised:
        await _stream().open_upstream()
    assert str(raised.value) == expected
    assert FAKE_LOGIN_TOKEN not in repr(raised.value)
    assert raised.value.__context__ is None
    refresh.assert_not_called()


@pytest.mark.asyncio
async def test_closed_stream_never_dials(monkeypatch):
    from app.core import voice_grok
    connect = AsyncMock(side_effect=AssertionError("closed call must not connect"))
    monkeypatch.setattr(voice_grok, "connect", connect)
    call = _stream()
    call.closed = True
    with pytest.raises(ValueError, match="closed"):
        await call.open_upstream()
    assert call.api_key == ""


@pytest.mark.asyncio
async def test_rejections_name_the_subscription_not_an_api_key():
    call = _stream()
    call.upstream = AsyncMock()
    call.upstream.recv.return_value = json.dumps({"type": "error", "error": {"message": "nope"}})
    with pytest.raises(ValueError, match="SuperGrok voice rejected the session"):
        await call.start_live([])
    call.upstream = Upstream([json.dumps({"type": "error", "error": {"code": "invalid_api_key"}})])
    with pytest.raises(ValueError, match="SuperGrok voice rejected a voice event"):
        _ = [frame async for frame in call.upstream_frames()]


@pytest.mark.asyncio
async def test_handoff_runs_in_the_same_chat_with_grok_delegation_prompt(monkeypatch):
    from app.core import voice_stream
    call = _stream()
    call.upstream = Upstream([json.dumps(e) for e in [
        {"type": "conversation.item.input_audio_transcription.completed", "transcript": "Check the build."},
        {"type": "response.function_call_arguments.done", "name": "delegate_to_chat_agent", "call_id": "tool-1"},
    ]])
    call.flush = AsyncMock()
    call.settle_speech = AsyncMock()
    delegate = AsyncMock(return_value="The build is green.")
    monkeypatch.setattr(voice_stream, "delegate_to_selected_model", delegate)
    await call.receive_upstream()
    await call.delegation_task
    assert delegate.await_args.args[0] == "s"
    assert "Check the build." in delegate.await_args.args[1]
    items = [json.loads(e).get("item", {}) for e in call.upstream.sent]
    assert any(i.get("type") == "function_call_output" and i.get("call_id") == "tool-1" for i in items)


@pytest.mark.asyncio
async def test_delegation_submits_to_the_same_chat_with_the_saved_prompt(monkeypatch):
    from app.core import api_user_scope, turn_manager, voice_stream
    from app.core.voice_prompts import VOICE_INSTRUCTIONS
    row = {"id": "chat", "mode": "voice", "model": "sonnet", "profile_id": "default", "effort": "high",
           "voice_config": {"model": SUPERGROK_MODEL, "voice": "eve", "provider_id": "prov-grok",
                            "options": {"delegation_instructions": "Be terse."}}}
    monkeypatch.setattr(voice_stream.database, "get_session", Mock(return_value=row))
    monkeypatch.setattr(api_user_scope, "session_api_user_id", Mock(return_value=None))
    submit = AsyncMock(return_value="turn-1")
    monkeypatch.setattr(turn_manager, "submit_async", submit)
    monkeypatch.setattr("app.core.session_runtime.try_steer_pending", AsyncMock(return_value=0))
    on_submitted = AsyncMock()
    assert await voice_stream.delegate_to_selected_model("chat", "Check the build.", on_submitted) == ""
    args, kwargs = submit.await_args
    assert args[0] == "chat" and kwargs["prompt"] == "Check the build."
    assert kwargs["overrides"]["model"] == "sonnet" and kwargs["overrides"]["effort"] == "high"
    assert kwargs["payload"][VOICE_INSTRUCTIONS] == "Be terse."
    assert kwargs["payload"][turn_manager.VOICE_DELEGATION] is True
    on_submitted.assert_awaited_once_with("turn-1")


@pytest.mark.asyncio
async def test_renewal_reads_the_login_fresh_and_never_an_api_key(monkeypatch):
    from app.core import voice_grok
    load = _load_login(monkeypatch, return_value="rotated-login-token")
    connect = AsyncMock(return_value="socket")
    monkeypatch.setattr(voice_grok, "connect", connect)
    call = _stream()
    call.api_key = ""  # Dropped once the call's first socket opened.
    assert await call.connect_provider() == "socket"
    load.assert_awaited_once_with("prov-grok")
    assert connect.await_args.args == (GROK_URL,)
    assert connect.await_args.kwargs["additional_headers"] == {"Authorization": "Bearer rotated-login-token"}


@pytest.mark.asyncio
async def test_renewal_refused_with_401_renews_the_login_for_the_next_attempt(monkeypatch, caplog):
    from app.core import voice_grok, xai_token_refresh
    caplog.set_level(logging.DEBUG)
    _load_login(monkeypatch)
    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(voice_grok, "connect", AsyncMock(side_effect=_invalid_status(401)))
    monkeypatch.setattr(xai_token_refresh, "refresh_on_401_once", refresh)
    with pytest.raises(ValueError) as raised:
        await _stream().connect_provider()
    assert str(raised.value) == "SuperGrok voice could not reconnect (HTTP 401)."
    assert raised.value.__context__ is None and FAKE_LOGIN_TOKEN not in repr(raised.value)
    refresh.assert_awaited_once_with("prov-grok", stale_access_token=FAKE_LOGIN_TOKEN)
    assert FAKE_LOGIN_TOKEN not in caplog.text


@pytest.mark.asyncio
async def test_renewal_without_a_usable_login_fails_plainly(monkeypatch):
    from app.core import voice_grok
    from app.core.provider.fluid_credentials import ProviderCredentialError
    _load_login(monkeypatch, side_effect=ProviderCredentialError("needs reauth"))
    monkeypatch.setattr(voice_grok, "connect", AsyncMock(side_effect=AssertionError("no login, no dial")))
    with pytest.raises(ValueError, match="SuperGrok login is unavailable"):
        await _stream().connect_provider()
