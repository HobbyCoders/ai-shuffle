"""Core settings routes (``app.api.settings``) through the real app.

Key storage (fal.ai, fal billing, Anthropic) and ``GET /integrations`` must
never hand back key material: keys are stored encrypted, reported masked, and
a locked credential store never downgrades a key to plaintext. The security
instructions, voice, Claude auth mode and loop guard editors are owner-only
and validate before they write.
"""
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from app.api import settings as settings_api
from app.api.auth import hash_api_key
from app.api.settings_crypto import get_decrypted_api_key, set_encrypted_api_key
from app.core import anthropic_generation, encryption
from app.core.query_engine import DEFAULT_SECURITY_INSTRUCTIONS
from app.core.voice_models import LIVE_VOICES, voices_for
from app.db import database
from tests.fakes.http import mock_transport_client

BASE = "/api/v1/settings"
USER_KEY = "aih_settings_user_key"
FAL_KEY = "fal-0123456789abcdef:0123456789abcdef0123456789abcdef"
ANTHROPIC_KEY = "sk-ant-api03-0123456789abcdefghijklmnopqrstuvwxyz"


@pytest.fixture
def c(authenticated_client):
    return authenticated_client


@pytest.fixture
def api_user(mock_db):
    database.create_api_user("settings-user", "Settings User", hash_api_key(USER_KEY))
    return {"Authorization": f"Bearer {USER_KEY}"}


def _fal_validation(monkeypatch, outcome):
    """Serve the fal.ai key check: a status code, or an exception to raise."""
    calls = []

    def handler(request):
        calls.append(request)
        if isinstance(outcome, Exception):
            raise outcome
        return httpx.Response(outcome)

    monkeypatch.setattr(settings_api, "httpx", SimpleNamespace(
        AsyncClient=lambda *a, **kw: mock_transport_client(handler),
        TimeoutException=httpx.TimeoutException,
    ))
    return calls


# ---------------------------------------------------------------------------
# GET /integrations and stored keys: never key material
# ---------------------------------------------------------------------------

def test_integrations_reports_masked_keys_and_stores_ciphertext(c):
    set_encrypted_api_key("fal_api_key", FAL_KEY)
    set_encrypted_api_key("anthropic_api_key", ANTHROPIC_KEY)
    r = c.get(f"{BASE}/integrations")
    assert r.status_code == 200
    body = r.json()
    assert body["fal_api_key_set"] is True
    assert body["fal_api_key_masked"] == f"{FAL_KEY[:7]}...{FAL_KEY[-4:]}"
    assert body["anthropic_api_key_set"] is True
    assert body["anthropic_api_key_masked"] == f"{ANTHROPIC_KEY[:7]}...{ANTHROPIC_KEY[-4:]}"
    assert FAL_KEY not in r.text and ANTHROPIC_KEY not in r.text
    for name in ("fal_api_key", "anthropic_api_key"):
        stored = database.get_system_setting(name)
        assert stored.startswith("enc:") and FAL_KEY not in stored and ANTHROPIC_KEY not in stored


def test_integrations_short_key_is_not_echoed_by_the_mask(c):
    """Regression: the mask showed 7 + 4 characters, so a 10-11 character key
    came back whole in ``fal_api_key_masked``."""
    short = "fal-0123ab"
    set_encrypted_api_key("fal_api_key", short)
    r = c.get(f"{BASE}/integrations")
    assert r.json()["fal_api_key_set"] is True
    assert short not in r.text
    assert r.json()["fal_api_key_masked"] == ""


def test_integrations_with_locked_storage_reports_unset_without_leaking(c, monkeypatch):
    set_encrypted_api_key("fal_api_key", FAL_KEY)
    monkeypatch.setattr(encryption, "_encryption_key", None)
    r = c.get(f"{BASE}/integrations")
    assert r.status_code == 200
    assert r.json()["fal_api_key_set"] is False
    assert r.json()["fal_api_key_masked"] == ""
    assert "enc:" not in r.text and FAL_KEY not in r.text


def test_integrations_reports_the_billing_key_by_presence_only(c):
    set_encrypted_api_key("fal_billing_api_key", "billing-admin-key-0123456789abcdef")
    r = c.get(f"{BASE}/integrations")
    assert r.json()["fal_billing_api_key_set"] is True
    assert "billing-admin-key" not in r.text


def test_integrations_is_readable_by_api_users_without_key_material(client, api_user):
    set_encrypted_api_key("fal_api_key", FAL_KEY)
    r = client.get(f"{BASE}/integrations", headers=api_user)
    assert r.status_code == 200
    assert FAL_KEY not in r.text


def test_fal_key_is_validated_saved_encrypted_and_masked(c, monkeypatch):
    calls = _fal_validation(monkeypatch, 200)
    r = c.post(f"{BASE}/integrations/fal-ai", json={"api_key": f"  {FAL_KEY}  "})
    assert r.status_code == 200
    assert r.json() == {"success": True, "masked_key": f"{FAL_KEY[:7]}...{FAL_KEY[-4:]}"}
    assert calls[0].headers["Authorization"] == f"Key {FAL_KEY}"
    assert database.get_system_setting("fal_api_key").startswith("enc:")
    assert get_decrypted_api_key("fal_api_key") == FAL_KEY


def test_fal_key_rejected_by_fal_is_not_saved(c, monkeypatch):
    _fal_validation(monkeypatch, 401)
    r = c.post(f"{BASE}/integrations/fal-ai", json={"api_key": FAL_KEY})
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid API key. Authentication failed."
    assert database.get_system_setting("fal_api_key") is None


@pytest.mark.parametrize("outcome", [httpx.ReadTimeout("slow"), httpx.ConnectError("offline"), 500])
def test_fal_key_is_saved_when_validation_cannot_answer(c, monkeypatch, outcome):
    _fal_validation(monkeypatch, outcome)
    assert c.post(f"{BASE}/integrations/fal-ai", json={"api_key": FAL_KEY}).status_code == 200
    assert get_decrypted_api_key("fal_api_key") == FAL_KEY


def test_fal_key_blank_is_400_and_delete_removes_it(c, monkeypatch):
    calls = _fal_validation(monkeypatch, 200)
    assert c.post(f"{BASE}/integrations/fal-ai", json={"api_key": "   "}).status_code == 400
    assert calls == []
    set_encrypted_api_key("fal_api_key", FAL_KEY)
    assert c.delete(f"{BASE}/integrations/fal-ai").json() == {"success": True}
    assert database.get_system_setting("fal_api_key") is None
    assert c.get(f"{BASE}/integrations").json()["fal_api_key_set"] is False


def test_locked_storage_never_saves_a_plaintext_key(authenticated_client, monkeypatch):
    from app.main import app

    _fal_validation(monkeypatch, 200)
    monkeypatch.setattr(encryption, "_encryption_key", None)
    raw = TestClient(app, raise_server_exceptions=False)
    r = raw.post(f"{BASE}/integrations/fal-ai", json={"api_key": FAL_KEY})
    assert r.status_code >= 400
    assert FAL_KEY not in r.text
    assert database.get_system_setting("fal_api_key") is None


@pytest.mark.parametrize("method,path,body", [
    ("post", "/integrations/fal-ai", {"api_key": FAL_KEY}),
    ("delete", "/integrations/fal-ai", None),
    ("post", "/integrations/fal-billing", {"api_key": "billing-key-0123456789"}),
    ("delete", "/integrations/fal-billing", None),
    ("post", "/integrations/anthropic", {"api_key": ANTHROPIC_KEY}),
    ("delete", "/integrations/anthropic", None),
])
def test_key_routes_are_owner_only(client, api_user, method, path, body):
    set_encrypted_api_key("fal_api_key", "fal-existing-key-0123456789abcdef")
    kwargs = {"headers": api_user}
    if body is not None:
        kwargs["json"] = body
    r = getattr(client, method)(BASE + path, **kwargs)
    assert r.status_code == 403
    assert get_decrypted_api_key("fal_api_key") == "fal-existing-key-0123456789abcdef"
    assert database.get_system_setting("anthropic_api_key") is None
    assert database.get_system_setting("fal_billing_api_key") is None


# ---------------------------------------------------------------------------
# fal billing and Anthropic keys
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("key", ["", "   ", "has space-0123456789", "x" * 4097])
def test_fal_billing_key_rejects_blank_whitespace_and_oversized(c, key):
    r = c.post(f"{BASE}/integrations/fal-billing", json={"api_key": key})
    assert r.status_code == 400
    assert r.json()["detail"] == "Enter an Admin API key without whitespace"
    assert database.get_system_setting("fal_billing_api_key") is None


def test_fal_billing_key_saves_then_resumes_usage_polling(c, monkeypatch):
    from app.network import usage_budget
    resumed = []

    async def resume(url, headers):
        resumed.append((url, headers))

    monkeypatch.setattr(usage_budget, "resume_authentication", resume)
    r = c.post(f"{BASE}/integrations/fal-billing", json={"api_key": " billing-key-0123456789 "})
    assert r.status_code == 200 and r.json() == {"success": True}
    assert get_decrypted_api_key("fal_billing_api_key") == "billing-key-0123456789"
    assert resumed[0][1] == {"Authorization": "Key billing-key-0123456789"}
    assert c.delete(f"{BASE}/integrations/fal-billing").json() == {"success": True}
    assert database.get_system_setting("fal_billing_api_key") is None


def test_fal_billing_key_deferred_resume_is_503_but_key_is_kept(c, monkeypatch):
    from app.core.usage_polling import UsageDeferred
    from app.network import usage_budget

    async def resume(url, headers):
        raise UsageDeferred("cooldown")

    monkeypatch.setattr(usage_budget, "resume_authentication", resume)
    r = c.post(f"{BASE}/integrations/fal-billing", json={"api_key": "billing-key-0123456789"})
    assert r.status_code == 503
    assert r.json()["detail"].startswith("Key saved.")
    assert get_decrypted_api_key("fal_billing_api_key") == "billing-key-0123456789"


@pytest.mark.parametrize("error,status,saved", [
    (None, 200, True),
    (anthropic_generation.AnthropicNetworkError("dns"), 200, True),
    (anthropic_generation.AnthropicAuthError("401"), 400, False),
    (anthropic_generation.AnthropicRateLimitError("429"), 429, False),
    (anthropic_generation.AnthropicApiError("500"), 400, False),
])
def test_anthropic_key_validation_outcomes(c, monkeypatch, error, status, saved):
    async def validate(api_key):
        assert api_key == ANTHROPIC_KEY
        if error:
            raise error

    monkeypatch.setattr(anthropic_generation, "validate_api_key", validate)
    r = c.post(f"{BASE}/integrations/anthropic", json={"api_key": f" {ANTHROPIC_KEY} "})
    assert r.status_code == status, r.text
    assert ANTHROPIC_KEY not in r.text
    assert (get_decrypted_api_key("anthropic_api_key") == ANTHROPIC_KEY) is saved
    if saved:
        assert r.json()["masked_key"] == f"{ANTHROPIC_KEY[:7]}...{ANTHROPIC_KEY[-4:]}"


def test_anthropic_key_switches_mode_only_when_asked(c, monkeypatch):
    from app.core import claude_auth_mode

    async def validate(api_key):
        return None

    monkeypatch.setattr(anthropic_generation, "validate_api_key", validate)
    claude_auth_mode.set_mode("subscription")
    c.post(f"{BASE}/integrations/anthropic", json={"api_key": ANTHROPIC_KEY})
    assert database.get_system_setting(claude_auth_mode.setting_key()) == "subscription"
    c.post(f"{BASE}/integrations/anthropic", json={"api_key": ANTHROPIC_KEY, "use_for_claude_code": True})
    assert database.get_system_setting(claude_auth_mode.setting_key()) == "api_key"
    assert c.post(f"{BASE}/integrations/anthropic", json={"api_key": " "}).status_code == 400
    assert c.delete(f"{BASE}/integrations/anthropic").json() == {"success": True}
    assert database.get_system_setting("anthropic_api_key") is None
    # Removing the key leaves the mode alone.
    assert database.get_system_setting(claude_auth_mode.setting_key()) == "api_key"


# ---------------------------------------------------------------------------
# Security instructions
# ---------------------------------------------------------------------------

def test_security_instructions_default_customise_and_revert(c):
    first = c.get(f"{BASE}/security-instructions").json()
    assert first == {"current": DEFAULT_SECURITY_INSTRUCTIONS, "default": DEFAULT_SECURITY_INSTRUCTIONS,
                     "is_customized": False}
    r = c.put(f"{BASE}/security-instructions", json={"instructions": "  Ask before deleting.  "})
    assert r.status_code == 200
    assert r.json() == {"success": True, "message": "Security instructions updated successfully"}
    assert c.get(f"{BASE}/security-instructions").json() == {
        "current": "Ask before deleting.", "default": DEFAULT_SECURITY_INSTRUCTIONS, "is_customized": True}
    r = c.delete(f"{BASE}/security-instructions")
    assert r.json() == {"success": True, "message": "Security instructions reverted to default"}
    assert c.get(f"{BASE}/security-instructions").json()["is_customized"] is False
    assert database.get_system_setting("security_instructions") is None
    # Reverting twice is harmless.
    assert c.delete(f"{BASE}/security-instructions").status_code == 200


@pytest.mark.parametrize("text", ["", "   \n\t"])
def test_security_instructions_blank_is_400_and_keeps_custom_text(c, text):
    database.set_system_setting("security_instructions", "Keep me")
    r = c.put(f"{BASE}/security-instructions", json={"instructions": text})
    assert r.status_code == 400
    assert r.json()["detail"] == "Instructions cannot be empty. Use DELETE to revert to default."
    assert database.get_system_setting("security_instructions") == "Keep me"


@pytest.mark.parametrize("method,body", [("get", None), ("put", {"instructions": "pwned"}), ("delete", None)])
def test_security_instructions_are_owner_only(client, api_user, method, body):
    database.set_system_setting("security_instructions", "owner text")
    kwargs = {"headers": api_user}
    if body is not None:
        kwargs["json"] = body
    r = getattr(client, method)(f"{BASE}/security-instructions", **kwargs)
    assert r.status_code == 403
    assert "owner text" not in r.text
    assert database.get_system_setting("security_instructions") == "owner text"


# ---------------------------------------------------------------------------
# Voice settings: voices validated per model by voices_for
# ---------------------------------------------------------------------------

VOICE = f"{BASE}/voice"


def test_voice_accepts_a_voice_of_the_live_model(c):
    assert "cedar" in voices_for("gpt-live-1")
    r = c.put(VOICE, json={"voice_voice": "cedar", "voice_max_call_minutes": 30, "voice_mode_enabled": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["voice_voice"], body["voice_max_call_minutes"], body["voice_mode_enabled"]) == ("cedar", 30, True)
    assert [v["id"] for v in body["model_details"]["voices"]] == list(LIVE_VOICES)
    assert database.get_system_setting("voice_voice") == "cedar"


@pytest.mark.parametrize("voice", ["NATF0", "af_heart", "Cedar", ""])
def test_voice_outside_the_models_list_is_400(c, voice):
    before = c.get(VOICE).json()
    r = c.put(VOICE, json={"voice_voice": voice, "voice_mode_enabled": not before["voice_mode_enabled"]})
    assert r.status_code == 400
    assert r.json()["detail"] == f"voice_voice must be one of {list(voices_for('gpt-live-1'))}"
    assert c.get(VOICE).json() == before


@pytest.mark.parametrize("patch,status,detail", [
    ({"voice_model": "gpt-realtime"}, 400, "Voice mode uses GPT Live 1."),
    ({"voice_provider_id": "prov-1"}, 400,
     "Voice providers are selected automatically from configured OpenAI API providers."),
    ({"voice_max_call_minutes": 0}, 400, "voice_max_call_minutes must be a positive integer"),
    ({"voice_input_model": "fal-ai/unknown"}, 400, "Choose a supported voice transcription model."),
    ({"voice_options": {"instructions": "x"}, "voice_options_model": "gpt-realtime"}, 409,
     "The selected voice model changed. Reload settings before saving."),
    ({"voice_options": None}, 400, "voice_options must be an object; use an empty string to reset a prompt."),
    ({"voice_options": {"delegation_model": "opus"}}, 400,
     "Choose the model and effort in the chat composer, not voice settings."),
])
def test_voice_invalid_updates_write_nothing(c, patch, status, detail):
    before = c.get(VOICE).json()
    r = c.put(VOICE, json={"voice_mode_enabled": not before["voice_mode_enabled"], **patch})
    assert r.status_code == status, r.text
    assert r.json()["detail"] == detail
    assert c.get(VOICE).json() == before


def test_voice_saved_values_that_are_invalid_fall_back_to_defaults(c):
    database.set_system_setting("voice_voice", "NATF0")
    database.set_system_setting("voice_max_call_minutes", "soon")
    database.set_system_setting("voice_provider_id", "__fal_voice__")
    database.set_system_setting("voice_options:gpt-live-1", "[1, 2]")
    body = c.get(VOICE).json()
    assert body["voice_voice"] == "marin"
    assert body["voice_max_call_minutes"] == 240
    assert body["voice_provider_id"] is None
    assert body["voice_options"] == c.put(VOICE, json={}).json()["voice_options"]
    database.set_system_setting("voice_max_call_minutes", "-5")
    assert c.get(VOICE).json()["voice_max_call_minutes"] == 240


def test_voice_settings_are_owner_only_to_change(client, api_user):
    assert client.get(VOICE, headers=api_user).status_code == 200
    r = client.put(VOICE, json={"voice_voice": "cedar"}, headers=api_user)
    assert r.status_code == 403
    assert database.get_system_setting("voice_voice") is None


# ---------------------------------------------------------------------------
# Claude auth mode and loop guard
# ---------------------------------------------------------------------------

def test_claude_auth_mode_rejects_unknown_modes(c):
    r = c.put(f"{BASE}/claude-auth", json={"mode": "oauth"})
    assert r.status_code == 400
    assert r.json()["detail"].startswith("Unknown Claude Code auth mode")
    r = c.put(f"{BASE}/claude-auth", json={"mode": "api_key"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "api_key"
    assert {"mode", "cli", "api_key", "gate"} <= set(body)
    assert c.get(f"{BASE}/claude-auth").json()["mode"] == "api_key"


def test_claude_auth_payload_never_carries_the_key(c):
    set_encrypted_api_key("anthropic_api_key", ANTHROPIC_KEY)
    r = c.get(f"{BASE}/claude-auth")
    assert r.status_code == 200
    assert ANTHROPIC_KEY not in r.text
    assert r.json()["api_key"]["set"] is True


def test_loop_guard_validates_ranges(c):
    r = c.put(f"{BASE}/loop-guard", json={"session_turns_per_hour": 0})
    assert r.status_code == 400
    assert "session_turns_per_hour" in r.json()["detail"]
    r = c.put(f"{BASE}/loop-guard", json={"session_turns_per_hour": 20})
    assert r.status_code == 200
    assert r.json()["session_turns_per_hour"] == 20
    assert c.get(f"{BASE}/loop-guard").json()["session_turns_per_hour"] == 20


@pytest.mark.parametrize("action", ["confirm", "dismiss"])
def test_loop_guard_unknown_held_turn_is_404(c, action):
    r = c.post(f"{BASE}/loop-guard/held/missing/{action}")
    assert r.status_code == 404
    assert r.json()["detail"] == "That held turn is gone"


@pytest.mark.parametrize("method,path,body", [
    ("put", "/claude-auth", {"mode": "api_key"}),
    ("put", "/loop-guard", {"enabled": False}),
    ("post", "/loop-guard/held/x/confirm", None),
    ("post", "/loop-guard/held/x/dismiss", None),
    ("put", "/text-generation", {"text_generation_effort": "low"}),
])
def test_auth_and_loop_guard_editors_are_owner_only(client, api_user, method, path, body):
    kwargs = {"headers": api_user}
    if body is not None:
        kwargs["json"] = body
    assert getattr(client, method)(BASE + path, **kwargs).status_code == 403

