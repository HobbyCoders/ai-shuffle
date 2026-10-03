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
import logging

import httpx
import pytest

from app.core import encryption
from app.core import voice_bridge
from app.db import database as db
from tests.helpers import unlock_encryption


VOICE_SETTINGS = "/api/v1/settings/voice"
VOICE_ROOT = "/api/v1/voice"

RAW_KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"


@pytest.fixture(autouse=True)
def _isolate_native_services(monkeypatch):
    """The API client runs lifespan; it must not probe or launch host Chrome."""
    from unittest.mock import AsyncMock
    from app.core import native_chrome, cluster

    monkeypatch.setattr(native_chrome, "ensure_started", AsyncMock())
    monkeypatch.setattr(cluster, "_primary_lan_ip", lambda: "127.0.0.1")


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
        ("get", f"{VOICE_ROOT}/agents"),
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
    def test_api_key_cannot_update_voice_settings(self, client, mock_db):
        from app.api.auth import hash_api_key

        db.create_api_user("voice-integration", "Integration", hash_api_key("aih_voice_test"))
        resp = client.put(
            VOICE_SETTINGS,
            headers={"Authorization": "Bearer aih_voice_test"},
            json={"voice_app_tools_enabled": True, "voice_max_call_minutes": 15},
        )
        assert resp.status_code == 403, resp.text
        assert db.get_system_setting("voice_max_call_minutes") is None

    def test_defaults(self, authenticated_client, mock_db):
        resp = authenticated_client.get(VOICE_SETTINGS)
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "voice_mode_enabled": False,
            "voice_model": "gpt-live-1",
            "voice_voice": "marin",
            "voice_provider_id": None,
            "voice_max_call_minutes": 240,
            "voice_input_model": "fal-ai/whisper",
            "voice_input_models": resp.json()["voice_input_models"],
            "voice_input_mode": "auto",
            "voice_input_silence_seconds": 5,
            "voice_app_tools_enabled": True,
            "model_details": resp.json()["model_details"],
            "voice_options": {"instructions":"", "opening":"", "delegation_instructions":"", "mic_threshold_db":-42.0},
        }
        assert resp.json()["model_details"]["model"] == "gpt-live-1"

    def test_put_roundtrip(self, authenticated_client, mock_db):
        _make_provider()
        resp = authenticated_client.put(VOICE_SETTINGS, json={
            "voice_mode_enabled": True,
            "voice_model": "gpt-live-1",
            "voice_voice": "cedar",
            "voice_provider_id": None,
            "voice_max_call_minutes": 30,
            "voice_app_tools_enabled": False,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json() == {
            "voice_mode_enabled": True,
            "voice_model": "gpt-live-1",
            "voice_voice": "cedar",
            "voice_provider_id": None,
            "voice_max_call_minutes": 30,
            "voice_input_model": "fal-ai/whisper",
            "voice_input_models": resp.json()["voice_input_models"],
            "voice_input_mode": "auto",
            "voice_input_silence_seconds": 5,
            "voice_app_tools_enabled": True,
            "model_details": resp.json()["model_details"],
            "voice_options": {"instructions":"", "opening":"", "delegation_instructions":"", "mic_threshold_db":-42.0},
        }
        assert resp.json()["model_details"]["model"] == "gpt-live-1"
        # Persisted: a fresh GET returns the same values.
        assert authenticated_client.get(VOICE_SETTINGS).json() == resp.json()

    @pytest.mark.parametrize("stored", [None, "false", "0", "true"])
    def test_app_tools_ignore_retired_setting(self, authenticated_client, mock_db, stored):
        if stored is not None:
            db.set_system_setting("voice_app_tools_enabled", stored)
        resp = authenticated_client.get(VOICE_SETTINGS)
        assert resp.status_code == 200, resp.text
        assert resp.json()["voice_app_tools_enabled"] is True

    @pytest.mark.parametrize("legacy_value", [False, True, None])
    def test_app_tools_legacy_updates_are_ignored(
        self, authenticated_client, mock_db, legacy_value
    ):
        resp = authenticated_client.put(VOICE_SETTINGS, json={
            "voice_app_tools_enabled": legacy_value,
            "voice_max_call_minutes": 15,
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["voice_app_tools_enabled"] is True
        assert resp.json()["voice_max_call_minutes"] == 15
        assert db.get_system_setting("voice_app_tools_enabled") is None
        assert authenticated_client.get(VOICE_SETTINGS).json() == resp.json()

    def test_prompt_overrides_are_gone(self, authenticated_client, mock_db):
        """The prompt editors were removed: a stale stored override is ignored
        and the API neither echoes nor accepts the old fields."""
        db.set_system_setting("voice_instructions", "You are Jarvis.")
        body = authenticated_client.get(VOICE_SETTINGS).json()
        assert "voice_instructions" not in body
        assert "voice_instructions_defaults" not in body
        resp = authenticated_client.put(VOICE_SETTINGS, json={"voice_instructions": "x"})
        assert resp.status_code == 200
        assert "voice_instructions" not in resp.json()

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
        assert authenticated_client.get(VOICE_SETTINGS).json()["voice_model"] == "gpt-live-1"

    @pytest.mark.parametrize("model", ["gpt-live-1", "gpt-realtime-2.1", "gpt-realtime-2.1-mini", "gpt-realtime"])
    def test_model_allowlist_accepts_each(self, authenticated_client, mock_db, model):
        resp = authenticated_client.put(VOICE_SETTINGS, json={"voice_model": model})
        assert resp.status_code == (200 if model == "gpt-live-1" else 400), resp.text
        if model == "gpt-live-1":
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

class TestVoiceAgents:
    def test_empty_without_voice_assignments(self, authenticated_client, mock_db):
        resp = authenticated_client.get(f"{VOICE_ROOT}/agents")
        assert resp.status_code == 200, resp.text
        assert resp.json() == []

    def test_lists_voice_assigned_agents_with_their_leg(
        self, authenticated_client, mock_db, monkeypatch
    ):
        from types import SimpleNamespace
        from app.core import provider_agent as pa

        db.create_profile(
            "coder", "Coder", "Writes code",
            {"provider_models": {"__voice__": "sonnet"}},
        )
        _make_provider("prov-or", name="OpenRouter", provider_type="openrouter")
        db.create_profile(
            "scout", "Scout", None,
            {"provider_models": {"__voice__": "prov:prov-or:meta/llama"}},
        )
        # A disabled provider's assignment is never offered.
        _make_provider("prov-off", name="Off", enabled=False)
        db.create_profile(
            "ghost", "Ghost", None,
            {"provider_models": {"__voice__": "prov:prov-off:x"}},
        )
        monkeypatch.setattr(
            pa, "validate_provider_model", lambda *a, **kw: SimpleNamespace(ok=True),
        )

        resp = authenticated_client.get(f"{VOICE_ROOT}/agents")
        assert resp.status_code == 200, resp.text
        assert resp.json() == [
            {"id": "coder", "name": "Coder", "description": "Writes code",
             "model": "sonnet", "provider_id": None, "provider_name": "Claude"},
            {"id": "scout", "name": "Scout", "description": "",
             "model": "meta/llama", "provider_id": "prov-or",
             "provider_name": "OpenRouter"},
        ]


class TestVoicePreview:
    def test_unknown_voice_returns_400(self, authenticated_client, mock_db):
        _make_provider()
        resp = authenticated_client.post(f"{VOICE_ROOT}/preview", json={"voice": "hal9000"})
        assert resp.status_code == 400, resp.text

    def test_no_eligible_provider_returns_400(self, authenticated_client, mock_db):
        resp = authenticated_client.post(f"{VOICE_ROOT}/preview", json={"voice": "marin"})
        assert resp.status_code == 400, resp.text
        assert "OpenAI provider" in resp.json()["detail"]

    @pytest.mark.parametrize("voice", ["cedar", "quartz", "stone", "cinder"])
    def test_happy_path_streams_live_audio_without_enable_flag(self, authenticated_client, mock_db, monkeypatch, voice):
        from app.core import voice_preview
        from unittest.mock import AsyncMock
        unlock_encryption(mock_db)
        _make_provider(auth_token=encryption.encrypt_value(RAW_KEY))
        generate = AsyncMock(return_value=b"RIFF-live-wave")
        monkeypatch.setattr(voice_preview, "generate_live_preview", generate)
        resp = authenticated_client.post(f"{VOICE_ROOT}/preview", json={"voice": voice})
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("audio/wav")
        assert resp.content == b"RIFF-live-wave"
        generate.assert_awaited_once_with(RAW_KEY, voice)

    def test_upstream_rejection_surfaces_as_502(self, authenticated_client, mock_db, monkeypatch):
        from app.core import voice_preview
        from unittest.mock import AsyncMock
        _make_provider()
        monkeypatch.setattr(voice_preview, "generate_live_preview", AsyncMock(side_effect=ValueError("bad voice")))
        resp = authenticated_client.post(f"{VOICE_ROOT}/preview", json={"voice": "verse"})
        assert resp.status_code == 502, resp.text
        assert "bad voice" in resp.json()["detail"]


class TestCreateVoiceSession:
    def test_disabled_flag_returns_403(self, authenticated_client, mock_db):
        _make_provider()
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={"model": "sonnet"})
        assert resp.status_code == 403, resp.text

    def test_no_eligible_provider_returns_400(self, authenticated_client, mock_db):
        _enable_voice()
        _make_provider("prov-chatgpt", provider_type="chatgpt")
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={"model": "sonnet"})
        assert resp.status_code == 400, resp.text
        assert "OpenAI provider" in resp.json()["detail"]

    def test_multiple_providers_choose_stable_automatic_default(self, authenticated_client, mock_db):
        _enable_voice()
        _make_provider("prov-a", name="A")
        _make_provider("prov-b", name="B")
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={"model": "sonnet"})
        assert resp.status_code == 200, resp.text
        assert json.loads(db.get_session(resp.json()["session_id"])["voice_config"])["provider_id"] == "prov-a"

    def test_explicit_provider_disambiguates(self, authenticated_client, mock_db, monkeypatch):
        _enable_voice()
        _make_provider("prov-a", name="A")
        _make_provider("prov-b", name="B")
        _install_mock_transport(monkeypatch, _secret_handler())

        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={"provider_id": "prov-b", "model": "sonnet"})
        assert resp.status_code == 200, resp.text
        row = db.get_session(resp.json()["session_id"])
        assert json.loads(row["voice_config"])["provider_id"] == "prov-b"

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

        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={"model": "sonnet"})
        assert resp.status_code == 200, resp.text
        assert json.loads(db.get_session(resp.json()["session_id"])["voice_config"])["provider_id"] == "prov-b"

    def test_happy_path_uses_live_and_keeps_composer_selection(self, authenticated_client, mock_db, monkeypatch):
        from unittest.mock import AsyncMock
        from app.api import voice
        _enable_voice(voice_model="gpt-realtime-2.1-mini", voice_voice="cedar")
        _make_provider()
        mint = AsyncMock(side_effect=AssertionError("Live must not mint realtime credentials"))
        monkeypatch.setattr(voice, "_mint_client_secret", mint)
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={"profile_id":"default", "model":"sonnet", "effort":"high"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["model"] == "gpt-live-1" and body["client_secret"] == ""
        row = db.get_session(body["session_id"])
        assert row["model"] == "sonnet" and row["effort"] == "high"
        assert row["mode"] == "voice" and row["is_active"]
        config = json.loads(row["voice_config"])
        assert config["model"] == "gpt-live-1" and config["voice"] == "cedar"
        assert config["provider_id"] == "prov-openai"
        mint.assert_not_called()

    def test_creation_persists_normal_session_context(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _enable_voice()
        _make_provider()
        _install_mock_transport(monkeypatch, _secret_handler())
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={
            "model": "gpt-5.6", "effort": "high", "permission_mode": "ask",
            "context_window": 12345, "fast": True, "execution_mode": "local",
        })
        assert resp.status_code == 200, resp.text
        row = db.get_session(resp.json()["session_id"])
        assert row["model"] == "gpt-5.6"
        assert row["effort"] == "high"
        assert row["permission_mode"] == "ask"
        assert row["context_window"] == 12345
        assert row["fast"] in (True, 1)

    def test_v2_existing_branch_is_provisioned_and_voice_bound(
        self, authenticated_client, mock_db, monkeypatch, tmp_path
    ):
        from app.core import checkout_manager, project_resolver

        _enable_voice()
        _make_provider()
        _install_mock_transport(monkeypatch, _secret_handler())
        project_path = tmp_path / "repo"
        project_path.mkdir()
        db.create_project("proj-v2", "Repo", None, str(project_path))
        pv2 = project_resolver.resolve_project_v2({"project_id": "proj-v2"})
        checkout_path = tmp_path / "checkout"
        checkout_path.mkdir()
        prepared = []
        monkeypatch.setattr(
            checkout_manager,
            "ensure_checkout",
            lambda row, branch, **kwargs: prepared.append((row, branch, kwargs))
            or str(checkout_path),
        )

        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={
            "project_id": "proj-v2",
            "project_v2_id": pv2["id"],
            "execution_mode": "worktree",
            "worktree_branch": "feature/voice",
            "base_branch": "main",
            "model": "gpt-5.6",
        })
        assert resp.status_code == 200, resp.text
        row = db.get_session(resp.json()["session_id"])
        assert row["mode"] == "voice"
        assert row["is_active"] in (True, 1)
        assert row["project_v2_id"] == pv2["id"]
        assert row["branch"] == "feature/voice"
        assert row["worktree_id"] is None
        assert row["model"] == "gpt-5.6"
        assert prepared[0][1] == "feature/voice"
        assert prepared[0][2]["create_new_branch"] is False

    def test_v2_auto_branch_and_runner_are_forwarded(
        self, authenticated_client, mock_db, monkeypatch, tmp_path
    ):
        from app.api.sessions_helpers import WorktreeSessionResponse
        from app.core import project_resolver
        import app.api.sessions_worktree as worktree_api

        _enable_voice()
        _make_provider()
        _install_mock_transport(monkeypatch, _secret_handler())
        project_path = tmp_path / "repo"
        project_path.mkdir()
        db.create_project("proj-auto", "Repo", None, str(project_path))
        pv2 = project_resolver.resolve_project_v2({"project_id": "proj-auto"})
        captured = {}

        async def _create(body, project, api_user_id, pv2_row, **kwargs):
            captured.update(body=body, project=project, pv2=pv2_row, kwargs=kwargs)
            return WorktreeSessionResponse(
                session_id="voice-auto", branch_name=body.branch_name,
                base_branch=body.base_branch, status="active",
            )

        monkeypatch.setattr(worktree_api, "_create_v2_branch_session", _create)
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={
            "project_id": "proj-auto",
            "project_v2_id": pv2["id"],
            "execution_mode": "worktree",
            "base_branch": "develop",
            "target_instance_id": "runner-selected", "model": "sonnet",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["session_id"] == "voice-auto"
        body = captured["body"]
        assert body.branch_name.startswith("voice/")
        assert body.create_new_branch is True
        assert body.base_branch == "develop"
        assert body.target_instance_id == "runner-selected"
        overrides = captured["kwargs"]["session_create_overrides"]
        assert overrides["mode"] == "voice"
        assert overrides["is_active"] is True
        assert overrides["voice_config"]["provider_id"] == "prov-openai"

    def test_command_mode_is_refused(self, authenticated_client, mock_db):
        """The wake-word "command" mode has no client left (the Android app
        only sends "chat"), so it is an invalid mode, not a hidden session."""
        _enable_voice()
        _make_provider()
        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions", json={"session_mode": "command", "model": "sonnet"}
        )
        assert resp.status_code == 400, resp.text
        assert resp.json()["detail"] == "session_mode must be 'chat'."
        assert db.get_sessions(limit=50) == []

    def test_chat_mode_session_stays_visible(
        self, authenticated_client, mock_db, monkeypatch
    ):
        _enable_voice()
        _make_provider()
        _install_mock_transport(monkeypatch, _secret_handler())

        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions", json={"session_mode": "chat", "model": "sonnet"}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["session_id"] in {s["id"] for s in db.get_sessions()}

    def test_invalid_session_mode_returns_400(self, authenticated_client, mock_db):
        _enable_voice()
        _make_provider()
        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions", json={"session_mode": "party"}
        )
        assert resp.status_code == 400, resp.text
        assert "session_mode" in resp.json()["detail"]

    def test_missing_composer_model_is_rejected(self, authenticated_client, mock_db):
        _enable_voice()
        _make_provider()
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={})
        assert resp.status_code == 400 and "composer" in resp.json()["detail"]
        assert db.get_sessions(limit=50) == []

    def test_api_user_is_bound_to_its_assigned_agents(self, client, mock_db, monkeypatch):
        from app.api.auth import hash_api_key

        _enable_voice()
        _make_provider()
        _install_mock_transport(monkeypatch, _secret_handler())
        db.create_api_user("voice-restricted", "Restricted", hash_api_key("aih_voice_r"),
                           profile_ids=["default"])
        headers = {"Authorization": "Bearer aih_voice_r"}

        denied = client.post(f"{VOICE_ROOT}/sessions", headers=headers,
                             json={"model": "gpt-5.6", "profile_id": "other-agent"})
        assert denied.status_code == 403, denied.text
        assert db.get_sessions(limit=50) == []

        resp = client.post(f"{VOICE_ROOT}/sessions", headers=headers, json={"model": "gpt-5.6"})
        assert resp.status_code == 200, resp.text
        assert db.get_session(resp.json()["session_id"])["profile_id"] == "default"

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
        # These tests exercise already-pinned legacy WebRTC sessions, not new calls.
        voice_config={"model": db.get_system_setting("voice_model") or "gpt-realtime-2.1",
                      "voice": "marin", "provider_id": provider_id},
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
        unlock_encryption(mock_db)
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
            "options": {},
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
        unlock_encryption(mock_db)
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

    def _tool_names(self, sock):
        return [t["name"] for t in sock.sent[0]["session"]["tools"]]

    @pytest.mark.parametrize("legacy_setting", ["false", "true"])
    def test_browser_attach_declares_app_tools_and_greets(
        self, authenticated_client, mock_db, sidebands, legacy_setting
    ):
        _enable_voice(voice_app_tools_enabled=legacy_setting)
        _make_provider()
        _mk_voice_session()

        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_1"}
        )
        assert resp.status_code == 200, resp.text
        names = self._tool_names(sidebands[0])
        # Dispatch tools lead; the two hardened web tools follow; no code tools.
        assert names[:8] == [
            "dispatch_agent", "check_agent", "list_agents", "message_agent",
            "stop_agent", "respond_approval", "WebSearch", "WebFetch",
        ]
        assert {"createAutomation", "listHooks", "createProfile", "listConnectors"} <= set(names)
        from app.mcp.chat_access_sdk import NAMES, GUIDANCE
        assert set(NAMES) <= set(names)
        assert GUIDANCE in sidebands[0].sent[0]["session"]["instructions"]
        assert not any(n in {"Edit", "Write", "Bash", "Read", "Grep", "Glob"} for n in names)
        instructions = sidebands[0].sent[0]["session"]["instructions"]
        assert instructions.startswith(voice_bridge.VOICE_INSTRUCTIONS)
        assert voice_bridge.APP_TOOLS_INSTRUCTIONS in instructions
        # The browser call opens with a greeting.
        greetings = [
            f for f in sidebands[0].sent
            if f.get("type") == "conversation.item.create"
            and f["item"].get("type") == "message"
        ]
        assert len(greetings) == 1
        assert greetings[0]["item"]["content"][0]["text"] == voice_bridge.GREETING_PROMPT

        # A reconnect (replace-attach) does not greet again.
        authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_2"}
        )
        assert not [
            f for f in sidebands[1].sent
            if f.get("type") == "conversation.item.create"
            and f["item"].get("type") == "message"
        ]
        assert self._tool_names(sidebands[1]) == names
        assert voice_bridge.APP_TOOLS_INSTRUCTIONS in sidebands[1].sent[0]["session"]["instructions"]
        authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup")

    @pytest.mark.parametrize("legacy_setting", ["false", "true"])
    def test_api_users_are_dispatch_only(
        self, authenticated_client, mock_db, sidebands, legacy_setting
    ):
        _enable_voice(voice_app_tools_enabled=legacy_setting)
        _make_provider()
        # An API-key user's session never gets config tools.
        db.create_api_user("api-1", "API One", "hash-1")
        db.create_session(
            session_id="voice-api-user", profile_id="default", title="Voice session",
            provider_id="prov-openai", mode="voice", api_user_id="api-1",
            voice_config={"model":"gpt-realtime-2.1", "voice":"marin", "provider_id":"prov-openai"},
        )
        authenticated_client.post(
            f"{VOICE_ROOT}/sessions/voice-api-user/call", json={"call_id": "rtc_2"}
        )
        assert self._tool_names(sidebands[0]) == [
            "dispatch_agent", "check_agent", "list_agents", "message_agent",
            "stop_agent", "respond_approval", "WebSearch", "WebFetch",
        ]
        assert voice_bridge.APP_TOOLS_INSTRUCTIONS not in sidebands[0].sent[0]["session"]["instructions"]
        authenticated_client.post(f"{VOICE_ROOT}/sessions/voice-api-user/hangup")

    def test_native_tools_cannot_shadow_server_web_tools(
        self, authenticated_client, mock_db, sidebands
    ):
        _enable_voice()
        _make_provider()
        _mk_voice_session()
        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={
                "call_id": "rtc_1",
                "client_tools": [{"name": "WebFetch", "description": "shadow"}],
            },
        )
        assert resp.status_code == 400, resp.text
        assert "collides with a server tool" in resp.json()["detail"]

    def test_native_attach_gets_the_mode_appendix_and_no_greeting(
        self, authenticated_client, mock_db, sidebands
    ):
        _enable_voice()
        _make_provider()
        _mk_voice_session()
        authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call",
            json={"call_id": "rtc_1", "session_mode": "chat"},
        )
        instructions = sidebands[0].sent[0]["session"]["instructions"]
        assert voice_bridge.DEFAULT_CHAT_INSTRUCTIONS in instructions
        assert not [
            f for f in sidebands[0].sent
            if f.get("type") == "conversation.item.create"
        ]
        authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup")

    def test_command_attach_is_refused(
        self, authenticated_client, mock_db, sidebands
    ):
        """No client declares "command" at attach any more; it is refused
        before a sideband opens and the row stays visible."""
        unlock_encryption(mock_db)
        _enable_voice()
        _make_provider(auth_token=encryption.encrypt_value(RAW_KEY))
        _mk_voice_session()

        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call",
            json={"call_id": "rtc_1", "session_mode": "command"},
        )
        assert resp.status_code == 400, resp.text
        assert "session_mode" in resp.json()["detail"]
        assert sidebands == []
        assert SESSION_ID in {s["id"] for s in db.get_sessions()}
        assert not hasattr(voice_bridge, "DEFAULT_COMMAND_INSTRUCTIONS")
        assert set(voice_bridge.DEFAULT_MODE_INSTRUCTIONS) == {"chat"}

    def test_chat_attach_leaves_the_session_visible(
        self, authenticated_client, mock_db, sidebands
    ):
        unlock_encryption(mock_db)
        _enable_voice()
        _make_provider(auth_token=encryption.encrypt_value(RAW_KEY))
        _mk_voice_session()

        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call",
            json={"call_id": "rtc_1", "session_mode": "chat"},
        )
        assert resp.status_code == 200, resp.text
        assert SESSION_ID in {s["id"] for s in db.get_sessions()}

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

        unlock_encryption(mock_db)
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


    def test_hangup_clears_is_active(self, authenticated_client, mock_db, sidebands):
        """Session creation stamps is_active True and nothing used to clear it,
        so every finished call left a permanently "live" session card."""
        _enable_voice()
        _make_provider()
        _mk_voice_session()
        db.set_session_active(SESSION_ID, True)
        authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call", json={"call_id": "rtc_1"}
        )
        assert bool(db.get_session(SESSION_ID)["is_active"]) is True

        assert authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup"
        ).status_code == 200
        assert bool(db.get_session(SESSION_ID)["is_active"]) is False

    def test_hangup_clears_is_active_without_a_bridge(
        self, authenticated_client, mock_db, sidebands
    ):
        """The sideband dropping first is exactly when the row goes stale."""
        _enable_voice()
        _mk_voice_session()
        db.set_session_active(SESSION_ID, True)
        assert authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup"
        ).status_code == 200
        assert bool(db.get_session(SESSION_ID)["is_active"]) is False

    @pytest.mark.parametrize("body,logged", [
        (None, "reason=unspecified"),
        ({"reason": "playback-backlog"}, "reason=playback-backlog"),
        ({"reason": "Bearer sk-not-a-label"}, "reason=unrecognized"),
    ])
    def test_hangup_logs_the_client_reason_without_free_text(
        self, authenticated_client, mock_db, sidebands, caplog, body, logged
    ):
        """#1671: the log must tell an End press from an automatic shutdown,
        and an unexpected reason must never fail the hangup or reach the log."""
        _enable_voice()
        _mk_voice_session()
        with caplog.at_level(logging.INFO, logger="app.api.voice"):
            resp = authenticated_client.post(
                f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup",
                **({"json": body} if body is not None else {}),
            )
        assert resp.status_code == 200, resp.text
        line = next(r.getMessage() for r in caplog.records if "hangup for session" in r.getMessage())
        assert logged in line
        assert "sk-not" not in line

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
            "usage": {}, "estimated_cost_usd": None, "active_tasks": 0,
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
            "usage": {}, "estimated_cost_usd": None, "active_tasks": 0,
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

class TestStreamingVoiceModels:
    def test_retired_fal_settings_migrate_new_calls_but_pinned_sessions_still_rejoin(self, authenticated_client, mock_db, monkeypatch):
        from app.api import voice
        from app.core.voice_models import FAL_PROVIDER_ID, PERSONAPLEX_MODEL
        from unittest.mock import AsyncMock
        unlock_encryption(mock_db)
        db.set_system_setting("fal_api_key", encryption.encrypt_value("test-fal-key"))
        _enable_voice(voice_model=PERSONAPLEX_MODEL, voice_provider_id=FAL_PROVIDER_ID, voice_voice="NATF2")
        current = authenticated_client.get(VOICE_SETTINGS).json()
        assert current["voice_model"] == "gpt-live-1"
        assert current["voice_voice"] == "marin" and current["voice_provider_id"] is None
        assert authenticated_client.put(VOICE_SETTINGS, json={"voice_provider_id":FAL_PROVIDER_ID}).status_code == 400
        db.create_session(session_id="legacy-fal", profile_id="default", mode="voice", provider_id=FAL_PROVIDER_ID,
            voice_config={"model":PERSONAPLEX_MODEL,"voice":"NATF2","provider_id":FAL_PROVIDER_ID,"options":{}})
        mint = AsyncMock(side_effect=AssertionError("must not mint a Realtime credential"))
        monkeypatch.setattr(voice, "_mint_client_secret", mint)
        assert authenticated_client.post(f"{VOICE_ROOT}/sessions/legacy-fal/secret").json()["model"] == PERSONAPLEX_MODEL
        mint.assert_not_called()

    def test_live_uses_client_delegation_and_persists_selected_model(self, authenticated_client, mock_db, monkeypatch):
        from app.api import voice
        from unittest.mock import AsyncMock
        unlock_encryption(mock_db)
        _make_provider()
        _enable_voice(voice_model="gpt-live-1", voice_voice="marin")
        mint = AsyncMock(side_effect=AssertionError("Live is not Realtime"))
        monkeypatch.setattr(voice, "_mint_client_secret", mint)
        assert authenticated_client.post(f"{VOICE_ROOT}/sessions", json={}).status_code == 400
        response = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={"model": "opus", "effort": "high"})
        assert response.status_code == 200, response.text
        row = db.get_session(response.json()["session_id"])
        assert row["model"] == "opus"
        assert row["provider_id"] is None
        assert row["effort"] == "high"
        assert json.loads(row["voice_config"])["model"] == "gpt-live-1"
        mint.assert_not_called()

    def test_invalid_cross_provider_pair_does_not_write(self, authenticated_client, mock_db):
        from app.core.voice_models import FAL_PROVIDER_ID
        unlock_encryption(mock_db)
        db.set_system_setting("fal_api_key", encryption.encrypt_value("test-fal-key"))
        before = authenticated_client.get(VOICE_SETTINGS).json()
        response = authenticated_client.put(VOICE_SETTINGS, json={"voice_provider_id": FAL_PROVIDER_ID, "voice_model": "gpt-live-1"})
        assert response.status_code == 400
        assert authenticated_client.get(VOICE_SETTINGS).json() == before


KEY_A = {"Authorization": "Bearer aih_voice_key_a"}
KEY_B = {"Authorization": "Bearer aih_voice_key_b"}

# (method, route suffix, JSON body). Hangup last: it ends the call.
CALL_ROUTES = [
    ("post", "secret", None),
    ("post", "call", {"call_id": "rtc_owned"}),
    ("get", "state", None),
    ("get", "config", None),
    ("patch", "context", {"fast": True}),
    ("post", "hangup", None),
]


def _make_keys():
    from app.api.auth import hash_api_key

    db.create_api_user("key-a", "Key A", hash_api_key("aih_voice_key_a"))
    db.create_api_user("key-b", "Key B", hash_api_key("aih_voice_key_b"))


# ============================================================================
# Access — an API key may only operate the voice sessions it owns
# ============================================================================

OWNER_KEY = "aih_voice_owner_key"
OTHER_KEY = "aih_voice_other_key"

# Every session-scoped voice route, with the body that would be a valid call.
# ``call`` carries a call_id so a 403 there provably happens BEFORE validation
# and before any sideband is opened.
CALL_SURFACE = [
    ("post", "secret", {}),
    ("post", "call", {"call_id": "rtc_x"}),
    ("post", "hangup", {}),
    ("get", "state", None),
    ("get", "config", None),
    ("patch", "context", {"effort": "high"}),
]


def _api_user(user_id, raw_key, **kwargs):
    """Create an API user and return the headers that authenticate as it."""
    from app.api.auth import hash_api_key

    db.create_api_user(user_id, user_id, hash_api_key(raw_key), **kwargs)
    return {"Authorization": f"Bearer {raw_key}"}


def _mk_owned_voice_session(session_id, api_user_id, *, mode="voice", project_id=None):
    db.create_session(
        session_id=session_id, profile_id="default", title="Voice session",
        provider_id="prov-openai", mode=mode, api_user_id=api_user_id,
        project_id=project_id,
        voice_config={"model": "gpt-realtime-2.1", "voice": "marin", "provider_id": "prov-openai"},

    )
    return session_id


def _hit(client, method, session_id, leaf, body, headers=None):
    kwargs = {"headers": headers} if headers else {}
    if body is not None:
        kwargs["json"] = body
    return getattr(client, method)(f"{VOICE_ROOT}/sessions/{session_id}/{leaf}", **kwargs)



class TestVoiceSessionOwnership:
    def test_key_drives_its_own_session_on_every_route(
        self, client, mock_db, sidebands, monkeypatch
    ):
        _enable_voice()
        _make_provider()
        _make_keys()
        _install_mock_transport(monkeypatch, _secret_handler())
        sid = _mk_owned_voice_session("voice-key-a", "key-a")

        for method, suffix, body in CALL_ROUTES:
            resp = _hit(client, method, sid, suffix, body, KEY_A)
            assert resp.status_code == 200, (suffix, resp.text)

        assert [s.call_id for s in sidebands] == ["rtc_owned"]
        assert sidebands[0].closed is True
        assert bool(db.get_session(sid)["fast"]) is True

    @pytest.mark.parametrize("owner", ["key-b", None], ids=["other-key", "owner"])
    def test_foreign_session_is_refused_on_every_route(
        self, client, mock_db, sidebands, monkeypatch, owner
    ):
        _enable_voice()
        _make_provider()
        _make_keys()
        captured = {}
        _install_mock_transport(monkeypatch, _secret_handler(captured))
        sid = _mk_owned_voice_session("voice-foreign", owner)
        if owner == "key-b":
            # A live call on B's session that A must not be able to end.
            assert client.post(
                f"{VOICE_ROOT}/sessions/{sid}/call", headers=KEY_B,
                json={"call_id": "rtc_b"},
            ).status_code == 200
        db.set_session_active(sid, True)

        for method, suffix, body in CALL_ROUTES:
            resp = _hit(client, method, sid, suffix, body, KEY_A)
            assert resp.status_code == 403, (suffix, resp.text)
            assert resp.json()["detail"] == "Access denied to this session"

        assert "request" not in captured            # no secret was minted for A
        assert [s.call_id for s in sidebands] == (["rtc_b"] if owner else [])
        assert all(not s.closed for s in sidebands)  # B's call survives
        row = db.get_session(sid)
        assert bool(row["is_active"]) is True
        assert bool(row["fast"]) is False
        if owner == "key-b":
            assert voice_bridge.registry.get(sid) is not None
            client.post(f"{VOICE_ROOT}/sessions/{sid}/hangup", headers=KEY_B)

    def test_missing_and_chat_sessions_stay_404_for_a_key(self, client, mock_db):
        _enable_voice()
        _make_keys()
        db.create_session(session_id="chat-a", profile_id="default",
                          mode="code", api_user_id="key-a")
        for sid in ("nope", "chat-a"):
            resp = client.get(f"{VOICE_ROOT}/sessions/{sid}/state", headers=KEY_A)
            assert resp.status_code == 404, resp.text

    def test_owner_is_unrestricted_on_a_key_session(
        self, authenticated_client, mock_db, sidebands, monkeypatch
    ):
        _enable_voice()
        _make_provider()
        _make_keys()
        _install_mock_transport(monkeypatch, _secret_handler())
        sid = _mk_owned_voice_session("voice-key-a", "key-a")

        for method, suffix, body in CALL_ROUTES:
            resp = _hit(authenticated_client, method, sid, suffix, body)
            assert resp.status_code == 200, (suffix, resp.text)
        assert [s.call_id for s in sidebands] == ["rtc_owned"]

    def test_key_started_session_is_owned_by_the_key(
        self, client, mock_db, monkeypatch
    ):
        _enable_voice()
        _make_provider()
        _make_keys()
        _install_mock_transport(monkeypatch, _secret_handler())

        created = client.post(f"{VOICE_ROOT}/sessions", headers=KEY_A,
                              json={"model": "gpt-5.6"})
        assert created.status_code == 200, created.text
        sid = created.json()["session_id"]
        assert db.get_session(sid)["api_user_id"] == "key-a"

        resp = client.patch(f"{VOICE_ROOT}/sessions/{sid}/context", headers=KEY_A,
                            json={"effort": "high"})
        assert resp.status_code == 200, resp.text
        assert db.get_session(sid)["effort"] == "high"
        for suffix in ("state", "config"):
            assert client.get(f"{VOICE_ROOT}/sessions/{sid}/{suffix}",
                              headers=KEY_A).status_code == 200
        assert client.post(f"{VOICE_ROOT}/sessions/{sid}/secret",
                           headers=KEY_A).status_code == 200

        # ...and it is still out of reach of every other key.
        assert client.patch(f"{VOICE_ROOT}/sessions/{sid}/context", headers=KEY_B,
                            json={"effort": "low"}).status_code == 403
        assert client.post(f"{VOICE_ROOT}/sessions/{sid}/hangup",
                           headers=KEY_B).status_code == 403
        assert db.get_session(sid)["effort"] == "high"
        assert client.post(f"{VOICE_ROOT}/sessions/{sid}/hangup",
                           headers=KEY_A).status_code == 200


DISPATCH_ONLY_TOOLS = [
    "dispatch_agent", "check_agent", "list_agents", "message_agent",
    "stop_agent", "respond_approval", "WebSearch", "WebFetch",
]


class TestVoiceKeyToolSurface:
    """R6-1 residual: whatever row a key reaches, its call gets dispatch only."""

    @staticmethod
    def _tool_names(sock):
        return [t["name"] for t in sock.sent[0]["session"]["tools"]]

    def test_key_attaching_its_own_session_gets_dispatch_tools_only(
        self, client, mock_db, sidebands
    ):
        # New calls run on the live model's audio stream, which exposes no
        # tools; a session pinned to a Realtime model is the sideband path.
        _enable_voice()
        _make_provider()
        _make_keys()
        sid = _mk_owned_voice_session("voice-key-own", "key-a")
        resp = client.post(f"{VOICE_ROOT}/sessions/{sid}/call", headers=KEY_A,
                           json={"call_id": "rtc_key"})
        assert resp.status_code == 200, resp.text
        assert self._tool_names(sidebands[0]) == DISPATCH_ONLY_TOOLS
        assert voice_bridge.APP_TOOLS_INSTRUCTIONS not in sidebands[0].sent[0]["session"]["instructions"]
        client.post(f"{VOICE_ROOT}/sessions/{sid}/hangup", headers=KEY_A)

    def test_key_reaching_an_unattributed_worker_row_gets_dispatch_tools_only(
        self, client, mock_db, sidebands
    ):
        """A worker row carries no api_user_id and belongs to its parent's key;
        the grant follows the caller, not the row's empty column."""
        _enable_voice()
        _make_provider()
        _make_keys()
        db.create_session(session_id="parent-a", profile_id="default", api_user_id="key-a")
        db.create_session(
            session_id="voice-worker-a", profile_id="default", title="Voice worker",
            provider_id="prov-openai", mode="voice", api_user_id=None,
            parent_session_id="parent-a", is_worker=True,
            voice_config={"model": "gpt-realtime-2.1", "voice": "marin",
                          "provider_id": "prov-openai"},
        )
        resp = client.post(f"{VOICE_ROOT}/sessions/voice-worker-a/call", headers=KEY_A,
                           json={"call_id": "rtc_worker"})
        assert resp.status_code == 200, resp.text
        assert self._tool_names(sidebands[0]) == DISPATCH_ONLY_TOOLS
        client.post(f"{VOICE_ROOT}/sessions/voice-worker-a/hangup", headers=KEY_A)

    @pytest.mark.parametrize("owner", [None, "key-b"], ids=["owner", "other-key"])
    def test_key_cannot_join_a_foreign_session_audio_stream(self, client, mock_db, owner):
        """The live-model audio socket is the path new calls take."""
        from starlette.websockets import WebSocketDisconnect

        _enable_voice()
        _make_provider()
        _make_keys()
        db.create_session(
            session_id="voice-live-foreign", profile_id="default", title="Voice session",
            provider_id="prov-openai", mode="voice", api_user_id=owner,
            voice_config={"model": "gpt-live-1", "voice": "marin", "provider_id": "prov-openai"},
        )
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect(
                f"{VOICE_ROOT}/sessions/voice-live-foreign/audio?token=aih_voice_key_a"
            ):
                pass
        assert refused.value.code == 4403
        assert refused.value.reason == "Access denied to this session"


# ============================================================================
# fal.ai key policy — a key spends only the fal key its credential policy allows
# ============================================================================
#
# Same resolution as /transcribe and /tts/stream (app.api.settings_audio):
# the owner and paired devices spend the owner's fal key; an API user gets its
# own stored key under ``user_provided`` (the default), and is refused with the
# same 403 when it has none. Regression: the fal voice path (PersonaPlex audio
# stream) handed the owner's fal key to any caller.

OWNER_FAL_KEY = "fal-owner-key-0123456789abcdefghijklmn"
USER_FAL_KEY = "fal-user-own-key-0123456789abcdefghijkl"
FAL_SESSION = "voice-fal-key-a"


@pytest.fixture
def fal_streams(monkeypatch):
    """Capture the key each PersonaPlex audio stream is built with; no network."""
    from app.core import voice_stream

    seen = []

    class _FakeStream:
        def __init__(self, websocket, row, config, key, max_minutes):
            seen.append(key)

        async def run(self):
            return None

    monkeypatch.setattr(voice_stream, "VoiceStream", _FakeStream)
    return seen


def _fal_voice_setup(mock_db, *, policy=None, user_key=None, api_user_id="key-a"):
    from app.core.voice_models import FAL_PROVIDER_ID, PERSONAPLEX_MODEL

    unlock_encryption(mock_db)
    _enable_voice()
    _make_keys()
    db.set_system_setting("fal_api_key", encryption.encrypt_value(OWNER_FAL_KEY))
    if policy:
        db.set_user_credential_policy("key-a", "fal_api_key", policy)
    if user_key:
        db.set_user_credential("key-a", "fal_api_key", encryption.encrypt_value(user_key))
    db.create_session(
        session_id=FAL_SESSION, profile_id="default", title="Voice session",
        provider_id=FAL_PROVIDER_ID, mode="voice", api_user_id=api_user_id,
        voice_config={"model": PERSONAPLEX_MODEL, "voice": "NATF2",
                      "provider_id": FAL_PROVIDER_ID, "options": {}},
    )


class TestVoiceFalKeyPolicy:
    @pytest.mark.parametrize("policy", [None, "user_provided"])
    def test_key_without_its_own_fal_key_is_refused(
        self, client, mock_db, fal_streams, policy
    ):
        from starlette.websockets import WebSocketDisconnect
        from app.api.settings_audio import FAL_KEY_USER_PROVIDED

        _fal_voice_setup(mock_db, policy=policy)

        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect(
                f"{VOICE_ROOT}/sessions/{FAL_SESSION}/audio?token=aih_voice_key_a"
            ):
                pass
        assert refused.value.code == 4403
        assert refused.value.reason == FAL_KEY_USER_PROVIDED[:120]
        assert fal_streams == []  # the owner's key never reached a stream

        resp = client.post(f"{VOICE_ROOT}/sessions/{FAL_SESSION}/secret", headers=KEY_A)
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == FAL_KEY_USER_PROVIDED
        assert OWNER_FAL_KEY not in resp.text

        providers = client.get(f"{VOICE_ROOT}/providers", headers=KEY_A).json()
        assert "__fal_voice__" not in [p["id"] for p in providers]

    def test_key_with_its_own_fal_key_spends_that_key_never_the_owners(
        self, client, mock_db, fal_streams
    ):
        _fal_voice_setup(mock_db, policy="user_provided", user_key=USER_FAL_KEY)

        with client.websocket_connect(
            f"{VOICE_ROOT}/sessions/{FAL_SESSION}/audio?token=aih_voice_key_a"
        ):
            pass
        assert fal_streams == [USER_FAL_KEY]

        resp = client.post(f"{VOICE_ROOT}/sessions/{FAL_SESSION}/secret", headers=KEY_A)
        assert resp.status_code == 200, resp.text
        assert OWNER_FAL_KEY not in resp.text and USER_FAL_KEY not in resp.text
        providers = client.get(f"{VOICE_ROOT}/providers", headers=KEY_A).json()
        assert "__fal_voice__" in [p["id"] for p in providers]

    def test_key_with_its_own_fal_key_works_when_the_owner_has_none(
        self, client, mock_db, fal_streams
    ):
        _fal_voice_setup(mock_db, policy="user_provided", user_key=USER_FAL_KEY)
        db.delete_system_setting("fal_api_key")

        with client.websocket_connect(
            f"{VOICE_ROOT}/sessions/{FAL_SESSION}/audio?token=aih_voice_key_a"
        ):
            pass
        assert fal_streams == [USER_FAL_KEY]

    def test_owner_still_spends_the_owner_fal_key(
        self, client, mock_db, fal_streams, monkeypatch
    ):
        from unittest.mock import AsyncMock
        from app.api import ws_auth

        _fal_voice_setup(mock_db, api_user_id=None)
        # The owner / paired-device contract of authenticate_websocket.
        monkeypatch.setattr(
            ws_auth, "authenticate_websocket", AsyncMock(return_value=(True, None))
        )
        with client.websocket_connect(f"{VOICE_ROOT}/sessions/{FAL_SESSION}/audio"):
            pass
        assert fal_streams == [OWNER_FAL_KEY]


class TestAudioResume:
    """A browser reopening a dropped call replaces the leftover connection."""

    @pytest.fixture
    def leftover(self, monkeypatch):
        import asyncio
        from app.core import voice_stream

        class _Leftover:
            """A call whose browser socket died without the server noticing."""
            def __init__(self):
                self.closed = False
                self.finished = asyncio.Event()

            async def close(self):
                self.closed = True
                voice_stream.calls.pop(FAL_SESSION, None)  # run()'s cleanup
                self.finished.set()

        call = _Leftover()
        monkeypatch.setitem(voice_stream.calls, FAL_SESSION, call)
        return call

    def test_plain_second_connection_is_still_refused(self, client, mock_db, fal_streams, leftover):
        from starlette.websockets import WebSocketDisconnect

        _fal_voice_setup(mock_db, policy="user_provided", user_key=USER_FAL_KEY)
        with pytest.raises(WebSocketDisconnect) as refused:
            with client.websocket_connect(f"{VOICE_ROOT}/sessions/{FAL_SESSION}/audio?token=aih_voice_key_a"):
                pass
        assert refused.value.code == 4409
        assert not leftover.closed and fal_streams == []

    def test_resume_replaces_the_leftover_connection(self, client, mock_db, fal_streams, leftover):
        _fal_voice_setup(mock_db, policy="user_provided", user_key=USER_FAL_KEY)
        with client.websocket_connect(f"{VOICE_ROOT}/sessions/{FAL_SESSION}/audio?token=aih_voice_key_a&resume=1"):
            pass
        assert leftover.closed
        assert fal_streams == [USER_FAL_KEY]

    def test_resume_cannot_end_another_users_call(self, client, mock_db, fal_streams, leftover):
        from starlette.websockets import WebSocketDisconnect

        _fal_voice_setup(mock_db, policy="user_provided", user_key=USER_FAL_KEY, api_user_id="key-b")
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect(f"{VOICE_ROOT}/sessions/{FAL_SESSION}/audio?token=aih_voice_key_a&resume=1"):
                pass
        assert not leftover.closed and fal_streams == []




class TestConversationAttachment:
    """Isolated router contract; no application lifecycle or real voice provider."""
    @pytest.fixture
    def attach_client(self, mock_db, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api import voice
        require_auth = voice.require_auth
        from app.api import voice_context
        app = FastAPI()
        app.include_router(voice.router)
        app.include_router(voice_context.router)
        app.dependency_overrides[require_auth] = lambda: "owner"
        monkeypatch.setattr(voice, "get_api_user_from_request", lambda _: None)
        _make_keys()
        _make_provider()
        db.set_system_setting("voice_mode_enabled", True)
        db.set_system_setting("voice_model", "gpt-realtime-2.1")
        _install_mock_transport(monkeypatch, lambda request: httpx.Response(200, json={"value": "attached-secret"}))
        with TestClient(app) as client:
            yield client

    def test_attach_reuses_identity_binding_history_and_saved_call_settings(self, attach_client, mock_db):
        from app.db import database
        database.create_profile("normal-agent", "Normal", None, {"model": "agent-text-model"})
        database.create_session("home", "normal-agent", model="text-model", mode="chat", permission_mode="default", provider_id="text-provider",
            voice_config={"model":"gpt-realtime-2.1", "voice":"marin", "provider_id":"prov-openai"})
        before = database.get_session("home")
        response = attach_client.post("/api/v1/voice/sessions/home/attach", json={})
        assert response.status_code == 200, response.text
        assert response.json()["session_id"] == "home" and response.json()["client_secret"] == "attached-secret"
        after = database.get_session("home")
        for key in ("profile_id", "model", "provider_id", "mode", "permission_mode"):
            assert after[key] == before[key]
        assert len(database.get_sessions()) == 1
        saved = json.loads(after["voice_config"])
        database.set_system_setting("voice_model", "changed-default")
        again = attach_client.post("/api/v1/voice/sessions/home/attach", json={})
        assert again.status_code == 200 and again.json()["model"] == saved["model"]
        assert attach_client.post("/api/v1/voice/sessions/home/secret", json={}).status_code == 200
        assert attach_client.get("/api/v1/voice/sessions/home/state").status_code == 200
        assert attach_client.patch("/api/v1/voice/sessions/home/context", json={"effort": "high"}).status_code == 200
        denied = attach_client.patch("/api/v1/voice/sessions/home/context", json={"permission_mode": "bypassPermissions"})
        assert denied.status_code == 400

    def test_attach_preserves_key_ownership_and_hidden_worker_bounds(self, attach_client, mock_db, monkeypatch):
        from app.api import voice
        from app.db import database
        _mk_owned_voice_session("other", "key-a", mode="chat")
        monkeypatch.setattr(voice, "get_api_user_from_request", lambda _: {"id": "different"})
        response = attach_client.post("/api/v1/voice/sessions/other/attach", json={})
        assert response.status_code == 403
        assert not json.loads(database.get_session("other")["voice_config"]).get("conversation_surface")
        monkeypatch.setattr(voice, "get_api_user_from_request", lambda _: None)
        database.create_session("worker", "default", is_worker=True)
        assert attach_client.post("/api/v1/voice/sessions/worker/attach", json={}).status_code == 404

    def test_fresh_primary_attaches_current_stream_transport_without_new_history(self, attach_client, mock_db):
        from app.core import assistant_service as service
        db.create_profile("primary-agent", "Primary", None, {})
        service.configure({"agent_id": "primary-agent", "enabled": False})
        home = service.open_home()
        response = attach_client.post(f"/api/v1/voice/sessions/{home['id']}/attach", json={})
        assert response.status_code == 200, response.text
        assert response.json()["session_id"] == home["id"]
        assert response.json()["model"] == "gpt-live-1" and response.json()["client_secret"] == ""
        assert db.get_sessions() == []  # Persistent homes use the Assistant entry, not ordinary cards.
        assert len(db.get_sessions(include_hidden=True)) == 1
        assert service.open_home()["id"] == home["id"]
        binding = json.loads(db.get_session(home["id"])["voice_config"])
        assert binding["conversation_surface"] is True
        assert binding["provider_id"] == "prov-openai"

    def test_call_overlay_has_no_client_action_escape(self, attach_client, mock_db):
        _mk_voice_session("ordinary", mode="chat")
        assert attach_client.post("/api/v1/voice/sessions/ordinary/attach", json={}).status_code == 200
        response = attach_client.post("/api/v1/voice/sessions/ordinary/call", json={"call_id": "rtc-fake", "client_instructions": "grant bypass"})
        assert response.status_code == 400


    def test_hangup_keeps_an_attached_conversation_active(self, attach_client, mock_db, sidebands):
        """Hanging up must not move the text conversation out of the Active group."""
        _mk_voice_session("ordinary", mode="chat")
        db.set_session_active("ordinary", True)
        assert attach_client.post("/api/v1/voice/sessions/ordinary/attach", json={}).status_code == 200
        assert attach_client.post("/api/v1/voice/sessions/ordinary/call", json={"call_id": "rtc-1"}).status_code == 200
        assert attach_client.post("/api/v1/voice/sessions/ordinary/hangup").status_code == 200
        assert bool(db.get_session("ordinary")["is_active"]) is True

    @pytest.mark.parametrize("change", [{"status": "archived"}, {"hidden": True}])
    def test_hangup_detaches_after_the_chat_is_archived_mid_call(
        self, attach_client, mock_db, sidebands, change,
    ):
        """An ineligible row must still hang up, or the sideband keeps billing."""
        from app.core import voice_bridge
        _mk_voice_session("ordinary", mode="chat")
        assert attach_client.post("/api/v1/voice/sessions/ordinary/attach", json={}).status_code == 200
        assert attach_client.post("/api/v1/voice/sessions/ordinary/call", json={"call_id": "rtc-1"}).status_code == 200
        assert voice_bridge.registry.get("ordinary") is not None
        if "hidden" in change:
            db.set_session_hidden("ordinary", True)
        else:
            db.update_session(session_id="ordinary", **change)
        assert attach_client.get("/api/v1/voice/sessions/ordinary/state").status_code == 200
        assert attach_client.post("/api/v1/voice/sessions/ordinary/hangup").status_code == 200
        assert voice_bridge.registry.get("ordinary") is None
        assert sidebands[0].closed is True
        # Starting a new call on the ineligible row is still refused.
        assert attach_client.post(
            "/api/v1/voice/sessions/ordinary/call", json={"call_id": "rtc-2"},
        ).status_code == 404

class TestVoiceSessionAccess:
    @pytest.fixture(autouse=True)
    def _voice_ready(self, mock_db, monkeypatch, sidebands):
        _enable_voice()
        _make_provider()
        _install_mock_transport(monkeypatch, _secret_handler())
        self.sidebands = sidebands

    # -- creation stamps the owner -------------------------------------------

    @pytest.mark.parametrize("execution_mode", ["local", None])
    def test_api_key_creation_records_the_creator_as_owner(self, client, execution_mode):
        owner = _api_user("api-owner", OWNER_KEY)
        body = {"model": "sonnet"}
        if execution_mode:
            body["execution_mode"] = execution_mode
        resp = client.post(f"{VOICE_ROOT}/sessions", json=body, headers=owner)
        assert resp.status_code == 200, resp.text
        assert db.get_session(resp.json()["session_id"])["api_user_id"] == "api-owner"

    def test_owner_creation_leaves_the_row_admin_owned(self, authenticated_client):
        resp = authenticated_client.post(f"{VOICE_ROOT}/sessions", json={"model": "sonnet"})
        assert resp.status_code == 200, resp.text
        assert db.get_session(resp.json()["session_id"])["api_user_id"] is None

    def test_creator_can_use_the_session_it_just_created(self, client):
        owner = _api_user("api-owner", OWNER_KEY)
        created = client.post(
            f"{VOICE_ROOT}/sessions", json={"model": "sonnet"}, headers=owner,
        )
        assert created.status_code == 200, created.text
        session_id = created.json()["session_id"]
        # Transport-agnostic routes (a fresh session is GPT Live, so no sideband).
        for method, leaf, body in [
            ("post", "secret", {}), ("get", "state", None), ("get", "config", None),
            ("patch", "context", {"effort": "high"}), ("post", "hangup", {}),
        ]:
            resp = _hit(client, method, session_id, leaf, body, owner)
            assert resp.status_code == 200, (leaf, resp.text)

    # -- the owner keeps working ----------------------------------------------

    @pytest.mark.parametrize("method,leaf,body", CALL_SURFACE)
    def test_owning_api_key_operates_its_session(self, client, method, leaf, body):
        owner = _api_user("api-owner", OWNER_KEY)
        _mk_owned_voice_session("voice-owned", "api-owner")
        resp = _hit(client, method, "voice-owned", leaf, body, owner)
        assert resp.status_code == 200, (leaf, resp.text)
        if leaf == "call":
            assert len(self.sidebands) == 1
            _hit(client, "post", "voice-owned", "hangup", {}, owner)

    @pytest.mark.parametrize("method,leaf,body", CALL_SURFACE)
    def test_admin_operates_an_api_users_session(
        self, authenticated_client, method, leaf, body
    ):
        _api_user("api-owner", OWNER_KEY)
        _mk_owned_voice_session("voice-owned", "api-owner")
        resp = _hit(authenticated_client, method, "voice-owned", leaf, body)
        assert resp.status_code == 200, (leaf, resp.text)
        if leaf == "call":
            _hit(authenticated_client, "post", "voice-owned", "hangup", {})

    # -- everyone else is refused ---------------------------------------------

    @pytest.mark.parametrize("method,leaf,body", CALL_SURFACE)
    @pytest.mark.parametrize(
        "owner_id", ["api-owner", None], ids=["api-user-owned", "admin-owned"],
    )
    def test_other_api_key_is_denied(self, client, method, leaf, body, owner_id):
        if owner_id:
            _api_user(owner_id, OWNER_KEY)
        other = _api_user("api-other", OTHER_KEY)
        _mk_owned_voice_session("voice-owned", owner_id)

        resp = _hit(client, method, "voice-owned", leaf, body, other)

        assert resp.status_code == 403, (leaf, resp.text)
        assert self.sidebands == []
        # Nothing was touched: the row is untouched and no secret was minted.
        row = db.get_session("voice-owned")
        assert row["effort"] is None
        assert row["is_active"] in (False, 0)
        from app.api import voice as voice_api
        assert "voice-owned" not in voice_api._SESSION_CLIENT_SECRETS

    def test_denied_hangup_leaves_the_owners_call_running(self, client):
        owner = _api_user("api-owner", OWNER_KEY)
        other = _api_user("api-other", OTHER_KEY)
        _mk_owned_voice_session("voice-owned", "api-owner")
        assert _hit(client, "post", "voice-owned", "call", {"call_id": "rtc_1"}, owner).status_code == 200

        assert _hit(client, "post", "voice-owned", "hangup", {}, other).status_code == 403
        assert _hit(client, "post", "voice-owned", "call", {"call_id": "rtc_2"}, other).status_code == 403

        state = _hit(client, "get", "voice-owned", "state", None, owner).json()
        assert state["active"] is True and state["connected"] is True
        assert len(self.sidebands) == 1 and not self.sidebands[0].closed
        _hit(client, "post", "voice-owned", "hangup", {}, owner)

    def test_project_restricted_key_needs_project_access_too(self, client, tmp_path):
        db.create_project("proj-a", "A", None, str(tmp_path / "a"))
        db.create_project("proj-b", "B", None, str(tmp_path / "b"))
        # Owns the session, but its key was since restricted to another project.
        restricted = _api_user("api-owner", OWNER_KEY, project_id="proj-b")
        _mk_owned_voice_session("voice-owned", "api-owner", project_id="proj-a")
        assert _hit(client, "get", "voice-owned", "state", None, restricted).status_code == 403

    # -- the 404 contract is unchanged, and precedes the access check ---------

    @pytest.mark.parametrize("method,leaf,body", CALL_SURFACE)
    def test_chat_or_missing_session_is_404_for_any_api_key(self, client, method, leaf, body):
        _api_user("api-owner", OWNER_KEY)
        other = _api_user("api-other", OTHER_KEY)
        _mk_owned_voice_session("chat-owned", "api-owner", mode="chat")
        assert _hit(client, method, "chat-owned", leaf, body, other).status_code == 404
        assert _hit(client, method, "nope", leaf, body, other).status_code == 404
