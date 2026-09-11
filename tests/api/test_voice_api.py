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
from app.core.voice_options import validate_options
from app.core import voice_bridge
from app.db import database as db


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
            "voice_input_mode": "auto",
            "voice_input_silence_seconds": 5,
            "voice_app_tools_enabled": True,
            "model_details": resp.json()["model_details"],
            "voice_options": {"instructions":"", "opening":"", "delegation_instructions":""},
        }
        assert resp.json()["model_details"]["model"] == "gpt-live-1"

    def test_put_roundtrip(self, authenticated_client, mock_db):
        pid = _make_provider()
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
            "voice_input_mode": "auto",
            "voice_input_silence_seconds": 5,
            "voice_app_tools_enabled": True,
            "model_details": resp.json()["model_details"],
            "voice_options": {"instructions":"", "opening":"", "delegation_instructions":""},
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
        _unlock(mock_db)
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

        async def _create(request, body, project, api_user_id, pv2_row, **kwargs):
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

    def test_command_mode_creates_a_hidden_session(
        self, authenticated_client, mock_db, monkeypatch
    ):
        """A wake-word command session is BORN list-hidden (v75) — it never
        exists in a visible state — but stays fetchable by id."""
        _enable_voice()
        _make_provider()
        _install_mock_transport(monkeypatch, _secret_handler())

        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions", json={"session_mode": "command", "model": "sonnet"}
        )
        assert resp.status_code == 200, resp.text
        session_id = resp.json()["session_id"]

        row = db.get_session(session_id)
        assert row is not None
        assert row["hidden"] in (True, 1)
        assert session_id not in {s["id"] for s in db.get_sessions()}

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

    def test_command_attach_hides_the_session(
        self, authenticated_client, mock_db, sidebands, monkeypatch
    ):
        """The shipped app declares session_mode only at attach time — a
        "command" attach backfills hidden=TRUE on the already-visible row AND
        broadcasts the flip, so a list that rendered the card during the
        create→attach window drops it live."""
        _unlock(mock_db)
        _enable_voice()
        _make_provider(auth_token=encryption.encrypt_value(RAW_KEY))
        _mk_voice_session()
        assert SESSION_ID in {s["id"] for s in db.get_sessions()}

        from app.core.sync_engine import sync_engine
        broadcasts = []

        async def _record(user_id, session_id, updates, source_device_id=None):
            broadcasts.append((user_id, session_id, updates))

        monkeypatch.setattr(sync_engine, "broadcast_user_session_updated", _record)

        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call",
            json={"call_id": "rtc_1", "session_mode": "command"},
        )
        assert resp.status_code == 200, resp.text

        assert SESSION_ID not in {s["id"] for s in db.get_sessions()}
        # Still fully fetchable and attachable — list-visibility only.
        assert db.get_session(SESSION_ID) is not None
        assert len(sidebands) == 1
        assert broadcasts == [
            ("admin", SESSION_ID, {"hidden": True}),
            ("admin", SESSION_ID, {"is_active": True}),
        ]

        # A reconnect re-declaring "command" finds the row already hidden —
        # no redundant hidden write/broadcast. Successful attaches still
        # refresh active membership for connected devices.
        resp = authenticated_client.post(
            f"{VOICE_ROOT}/sessions/{SESSION_ID}/call",
            json={"call_id": "rtc_2", "session_mode": "command"},
        )
        assert resp.status_code == 200, resp.text
        assert broadcasts == [
            ("admin", SESSION_ID, {"hidden": True}),
            ("admin", SESSION_ID, {"is_active": True}),
            ("admin", SESSION_ID, {"is_active": True}),
        ]

        authenticated_client.post(f"{VOICE_ROOT}/sessions/{SESSION_ID}/hangup")

    def test_chat_attach_leaves_the_session_visible(
        self, authenticated_client, mock_db, sidebands
    ):
        _unlock(mock_db)
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
        _unlock(mock_db)
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
        _unlock(mock_db)
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
        _unlock(mock_db)
        db.set_system_setting("fal_api_key", encryption.encrypt_value("test-fal-key"))
        before = authenticated_client.get(VOICE_SETTINGS).json()
        response = authenticated_client.put(VOICE_SETTINGS, json={"voice_provider_id": FAL_PROVIDER_ID, "voice_model": "gpt-live-1"})
        assert response.status_code == 400
        assert authenticated_client.get(VOICE_SETTINGS).json() == before
