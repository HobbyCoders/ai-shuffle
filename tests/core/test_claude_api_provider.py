"""The saved Anthropic API key as the native "Claude API" provider.

Claude API and the Claude subscription are separate options: the key runs on
fluid's native Anthropic client, the subscription on the Claude Code CLI.
"""

from types import SimpleNamespace

import pytest

from app.core import claude_api_provider as cap
from app.core import claude_auth_mode
from app.core.model_registry import API_MODEL_IDS
from app.db import database

KEY = "sk-ant-test-key"


@pytest.fixture
def key(mock_db, monkeypatch, _api_unit_credentials):
    """An active saved key with credential storage unlocked; the mode is set per test."""
    monkeypatch.setattr(claude_auth_mode, "stored_api_key", lambda: KEY)
    return KEY


def _api_key_mode():
    """API key mode on a worker whose choice pins no key (the legacy local
    setting): the active saved key is the scalar the fixture patches. A synced
    choice pins an exact key instead (tests/core/test_claude_accounts.py)."""
    database.set_system_setting(claude_auth_mode.setting_key(), "api_key")


def test_no_active_key_means_no_row(mock_db, monkeypatch):
    monkeypatch.setattr(claude_auth_mode, "stored_api_key", lambda: None)
    _api_key_mode()
    assert cap.virtual_row() is None
    assert database.get_model_provider(cap.CLAUDE_API_PROVIDER_ID) is None
    assert all(r["id"] != cap.CLAUDE_API_PROVIDER_ID for r in database.get_enabled_model_providers())


def test_row_is_an_api_key_anthropic_provider_without_its_secret(key):
    claude_auth_mode.set_mode("subscription")
    row = database.resolve_model_provider(cap.CLAUDE_API_PROVIDER_ID)
    assert row["provider_type"] == "anthropic" and row["auth_mode"] == "api_key"
    assert row["base_url"] == "https://api.anthropic.com"
    assert row["enabled"] is True and "auth_token" not in row
    assert row["default_model"] == API_MODEL_IDS["sonnet"]
    # Listed for pickers and usable-provider checks, never in the management list.
    assert any(r["id"] == cap.CLAUDE_API_PROVIDER_ID for r in database.get_enabled_model_providers())
    assert any(r["id"] == cap.CLAUDE_API_PROVIDER_ID for r in database.list_usable_model_providers())
    assert all(r["id"] != cap.CLAUDE_API_PROVIDER_ID for r in database.get_all_model_providers())


def test_fallback_list_and_default_are_the_5_5_generation(key):
    # Opus 5.5 and Sonnet 5.5 are the generation Anthropic's CVP covers.
    claude_auth_mode.set_mode("subscription")
    row = cap.virtual_row()
    # The ids the list offered before stay pickable after them, so an old pin
    # still validates before the key's catalog is read.
    assert [m["id"] for m in row["models_json"]] == [
        "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-2",
        "claude-sonnet-5", "claude-opus-5", "claude-fable-5"]
    assert [m["label"] for m in row["models_json"]] == [
        "Haiku 5.5", "Sonnet 5.5", "Opus 5.5", "Fable 5.2", "Sonnet 5", "Opus 5", "Fable 5"]
    assert row["default_model"] == "claude-sonnet-5-5"
    from app.core.provider_agent import validate_provider_model

    assert validate_provider_model(cap.CLAUDE_API_PROVIDER_ID, "claude-opus-5",
                                   provider_row=row, require_servable=False).ok


@pytest.mark.parametrize("picked", ["claude-opus-5", "claude-sonnet-5"])
def test_an_explicit_api_version_runs_itself(monkeypatch, picked):
    from app.core import credential_gate
    from app.core.provider.harness_adapter import _native_api_model

    bound = SimpleNamespace(extra={"provider_id": cap.CLAUDE_API_PROVIDER_ID,
                                   "provider_type": "anthropic"})
    assert _native_api_model(picked, bound) == picked
    assert _native_api_model(f"{picked}[1m]", bound) == picked
    # Unbound, and as the default-run gate reads a pick, it is still itself.
    assert _native_api_model(picked, None) == picked
    assert cap.api_model_id(picked) == picked
    row = {"id": cap.CLAUDE_API_PROVIDER_ID, "default_model": "claude-sonnet-5-5",
           "models_json": [{"id": i} for i in (picked, "claude-opus-5-5", "claude-sonnet-5-5")]}
    monkeypatch.setattr(credential_gate, "_default_candidate_rows", lambda servable_only: [row])
    assert credential_gate._claude_api_default(picked) == (cap.CLAUDE_API_PROVIDER_ID, picked)
    family = picked.split("-")[1]
    assert credential_gate._claude_api_default(family) == (cap.CLAUDE_API_PROVIDER_ID, f"{picked}-5")


def test_off_mode_hides_the_row(key):
    claude_auth_mode.set_mode("off")
    assert cap.virtual_row() is None


def test_models_follow_the_active_keys_catalog_only(key):
    claude_auth_mode.set_mode("subscription")
    cap.remember_models("sk-ant-other", [{"id": "claude-other", "label": "Other"}])
    assert all(m["id"] != "claude-other" for m in cap.virtual_row()["models_json"])
    cap.remember_models(KEY, [{"id": "claude-live-1", "label": "Live One"}])
    assert cap.virtual_row()["models_json"] == [{"id": "claude-live-1", "label": "Live One"}]


def test_management_readers_and_writers_never_see_the_virtual_row(key, authenticated_client):
    """Runtime readers resolve claude-api; the exact reader that management,
    writers and sync use is stored rows only, so they answer 404."""
    _api_key_mode()
    assert database.resolve_model_provider(cap.CLAUDE_API_PROVIDER_ID)["id"] == cap.CLAUDE_API_PROVIDER_ID
    assert database.get_model_provider(cap.CLAUDE_API_PROVIDER_ID) is None
    base = f"/api/v1/model-providers/{cap.CLAUDE_API_PROVIDER_ID}"
    assert authenticated_client.post(f"{base}/codex/device/start").status_code == 404
    assert authenticated_client.patch(base, json={"name": "Renamed"}).status_code == 404
    assert authenticated_client.delete(base).status_code == 404


@pytest.mark.parametrize("mode", ["api_key", "subscription", "cloud"])
def test_an_unbound_claude_pick_is_never_moved_onto_the_key(key, mode, tmp_path):
    """Unbound means the subscription in every mode: a chat turn and a one-shot
    generation keep the Claude Code CLI. Only an explicit binding spends the key."""
    from unittest.mock import patch

    import app.core.query_options as qo
    from app.core.generation_options import build_generation_options

    claude_auth_mode.set_mode(mode)
    profile = {"id": "t", "config": {"model": "sonnet", "tools_preset": "claude_code",
                                     "cwd": str(tmp_path)}}
    with patch.object(qo, "get_security_instructions", return_value=""):
        chat, _ = qo.build_options_from_profile(profile=profile)
    assert getattr(chat, "aih_provider_id", None) is None and chat.model == "sonnet"
    assert "ANTHROPIC_API_KEY" not in {k for k, v in (chat.env or {}).items() if v}
    one_shot = build_generation_options({"model": "sonnet"})
    assert one_shot.aih_provider_id is None


def test_a_bound_model_missing_after_a_key_switch_reads_the_catalog_once(key, monkeypatch):
    """The saved list belongs to the old key: read the new key's catalog once
    before refusing a bound model; a model it does not list is still refused."""
    from app.core import claude_api_models
    from app.core.provider_agent import validate_provider_model

    _api_key_mode()
    cap.remember_models(KEY, [{"id": "claude-old-only", "label": "Old"}])
    monkeypatch.setattr(claude_auth_mode, "stored_api_key", lambda: "sk-ant-new-key")
    monkeypatch.setattr(cap, "_refresh_tried", {})
    reads = []

    async def list_models(key, *, client=None):
        reads.append(key)
        return [{"id": "claude-new-only", "label": "New"}]

    monkeypatch.setattr(claude_api_models, "list_models", list_models)
    ok = validate_provider_model(cap.CLAUDE_API_PROVIDER_ID, "claude-new-only", require_servable=False)
    assert ok.ok, ok.reason
    assert reads == ["sk-ant-new-key"]
    missing = validate_provider_model(cap.CLAUDE_API_PROVIDER_ID, "claude-nowhere", require_servable=False)
    assert not missing.ok
    assert reads == ["sk-ant-new-key"]  # the list now belongs to this key: no second read


def test_concurrent_callers_share_one_catalog_read(key, monkeypatch):
    """A second caller during the read waits for it, then finds the model,
    instead of hitting the cooldown and refusing."""
    import threading

    from app.core import claude_api_models

    _api_key_mode()
    monkeypatch.setattr(cap, "_refresh_tried", {})
    started, release, reads = threading.Event(), threading.Event(), []

    async def list_models(key, *, client=None):
        reads.append(key)
        started.set()
        release.wait(5)
        return [{"id": "claude-live", "label": "Live"}]

    monkeypatch.setattr(claude_api_models, "list_models", list_models)
    results = []
    first = threading.Thread(target=lambda: results.append(cap.refresh_models_once()))
    first.start()
    assert started.wait(5)
    second = threading.Thread(target=lambda: results.append(cap.refresh_models_once()))
    second.start()
    release.set()
    first.join(5), second.join(5)
    assert results == [True, True] and reads == [KEY]


def test_a_failed_read_refuses_without_retrying_and_keeps_the_key_out_of_logs(key, monkeypatch, caplog):
    from app.core import claude_api_models

    _api_key_mode()
    monkeypatch.setattr(cap, "_refresh_tried", {})
    reads = []

    async def list_models(key, *, client=None):
        reads.append(key)
        raise claude_api_models.CatalogError("Could not read the model list from Anthropic. Try again.")

    monkeypatch.setattr(claude_api_models, "list_models", list_models)
    assert cap.refresh_models_once() is False
    assert cap.refresh_models_once() is False and reads == [KEY]  # cooldown
    assert KEY not in caplog.text


def test_a_bare_claude_pick_clears_a_key_binding(key, mock_db):
    """An id both catalogs list is still the subscription when picked bare."""
    claude_auth_mode.set_mode("subscription")
    shared = API_MODEL_IDS["opus"]
    row = database.resolve_model_provider(cap.CLAUDE_API_PROVIDER_ID)
    assert shared in [m["id"] for m in row["models_json"]]

    database.create_session(session_id="api-chat", profile_id="default", project_id=None,
                            title="API", model=f"prov:{cap.CLAUDE_API_PROVIDER_ID}:{shared}")
    assert database.get_session("api-chat")["provider_id"] == cap.CLAUDE_API_PROVIDER_ID
    database.set_session_run_binding("api-chat", model=shared)
    assert database.get_session("api-chat")["provider_id"] is None
    # The explicit composite still binds the key.
    database.set_session_run_binding("api-chat", model=f"prov:{cap.CLAUDE_API_PROVIDER_ID}:{shared}")
    assert database.get_session("api-chat")["provider_id"] == cap.CLAUDE_API_PROVIDER_ID


def test_task_and_start_session_names_never_bind_the_key(key):
    from app.core import model_override

    claude_auth_mode.set_mode("subscription")
    composite = f"prov:{cap.CLAUDE_API_PROVIDER_ID}:{API_MODEL_IDS['opus']}"
    # A bare id both catalogs list is not the key: refused here (the
    # subscription takes family names), with the explicit option named.
    with pytest.raises(ValueError, match=composite):
        model_override.resolve(API_MODEL_IDS["opus"], runs_here=False)
    assert model_override.resolve("opus", runs_here=False).provider_id is None
    assert model_override.resolve(composite, runs_here=False).provider_id == cap.CLAUDE_API_PROVIDER_ID


def test_a_claude_api_worker_row_keeps_the_explicit_binding():
    """A Task worker on the key stores the composite, so a later turn that
    resends the row's model (a limit resume) stays on the key."""
    from app.core.provider_agent import worker_session_fields

    fields = worker_session_fields(worker_session_id="w", profile_id="p", title="t",
                                   parent_session_id=None, provider_id=cap.CLAUDE_API_PROVIDER_ID,
                                   model_id=API_MODEL_IDS["opus"], effort=None)
    assert fields["model"] == f"prov:{cap.CLAUDE_API_PROVIDER_ID}:{API_MODEL_IDS['opus']}"


def test_automation_list_lookups_resolve_the_key_row(key):
    from app.core.automation_service import ModelLookups

    claude_auth_mode.set_mode("subscription")
    assert ModelLookups().prefetch().provider(cap.CLAUDE_API_PROVIDER_ID)["id"] == cap.CLAUDE_API_PROVIDER_ID


def test_bound_turns_take_the_native_anthropic_leg(key):
    from app.core.provider.harness_adapter import provider_kind_for_sdk_options

    claude_auth_mode.set_mode("subscription")
    bound = SimpleNamespace(model=API_MODEL_IDS["sonnet"], aih_provider_id=cap.CLAUDE_API_PROVIDER_ID)
    assert provider_kind_for_sdk_options(bound) == "anthropic"
    # The subscription (no provider) stays on the Claude Code CLI.
    assert provider_kind_for_sdk_options(SimpleNamespace(model="sonnet", aih_provider_id=None)) == "delegated"


@pytest.mark.asyncio
async def test_credentials_are_the_saved_key(key):
    from app.core.provider.fluid_credentials import resolve_provider_credentials

    _api_key_mode()
    creds = await resolve_provider_credentials("anthropic", cap.CLAUDE_API_PROVIDER_ID)
    assert creds.api_key == KEY
    assert creds.base_url == "https://api.anthropic.com"


def test_one_shot_generation_bound_to_claude_api_runs_natively(key):
    from app.core.generation_options import build_generation_options

    claude_auth_mode.set_mode("subscription")
    options = build_generation_options({"model": API_MODEL_IDS["sonnet"],
                                        "provider": cap.CLAUDE_API_PROVIDER_ID})
    assert options.aih_provider_id == cap.CLAUDE_API_PROVIDER_ID
    assert options.aih_provider_type == "anthropic"
    assert options.model == API_MODEL_IDS["sonnet"]
    # The standard provider overlay: the Anthropic fallback is blocked.
    assert (options.env or {}).get("ANTHROPIC_API_KEY") == ""


@pytest.mark.parametrize("state", ["no_key", "off", "locked"])
def test_a_claude_api_binding_without_its_key_fails_plainly(key, monkeypatch, state):
    """Key removed, Claude off or storage locked: a clear refusal, never the
    CLI or another key."""
    from app.core.provider_agent import validate_provider_model

    claude_auth_mode.set_mode("off" if state == "off" else "api_key")
    if state == "no_key":
        monkeypatch.setattr(claude_auth_mode, "stored_api_key", lambda: None)
    elif state == "locked":
        def locked():
            raise RuntimeError("credential storage locked")
        monkeypatch.setattr(claude_auth_mode, "stored_api_key", locked)
    result = validate_provider_model(cap.CLAUDE_API_PROVIDER_ID, API_MODEL_IDS["sonnet"],
                                     require_servable=False)
    assert not result.ok and result.reason


def test_one_shot_generation_on_the_subscription_stays_delegated(key):
    from app.core.generation_options import build_generation_options

    claude_auth_mode.set_mode("subscription")
    options = build_generation_options({"model": "sonnet"})
    assert options.aih_provider_id is None


def test_no_message_text_is_consent_to_bill_the_key(key):
    """S13: the API model's names are also the subscription's, and phrasing
    ("don't use the Claude API") cannot be judged safely: no text consents."""
    from app.core.model_override import memory_names, named_by_user

    model = f"prov:{cap.CLAUDE_API_PROVIDER_ID}:claude-sonnet-5"
    catalog = {model: ("claude-sonnet-5", "Claude Sonnet 5")}
    for text in ("Start the review on claude-sonnet-5.",
                 "Use claude-sonnet-5, but don't use the Claude API.",
                 f"Don't use {model}.",
                 "Run it on Claude API with claude-sonnet-5.",
                 f"Use {model}."):
        assert not named_by_user(model, [text], catalog), text
        assert not memory_names(model, text, catalog), text


def test_a_claude_api_switch_always_asks_the_owner(key, monkeypatch):
    """S13: Task/StartSession onto Claude API skips named, remembered and
    receipt evidence and goes to the owner's prompt; the chat already on it
    (the owner's pick) needs nothing."""
    import asyncio

    from app.core import model_override

    model = f"prov:{cap.CLAUDE_API_PROVIDER_ID}:claude-sonnet-5"
    monkeypatch.setattr(model_override, "chat_of", lambda sid: sid)
    monkeypatch.setattr(model_override, "_has_approval", lambda *a, **k: False)
    monkeypatch.setattr(model_override, "_has_named", lambda *a, **k: True)
    monkeypatch.setattr(model_override, "remembered_by_owner", lambda *a, **k: object())
    monkeypatch.setattr("app.core.api_user_scope.viewer_api_user_id", lambda chat: "u1")
    asked = []

    async def approve(session_id, profile_id, **kwargs):
        asked.append(kwargs["model"])
        raise PermissionError("owner said no")
    monkeypatch.setattr(model_override, "_approve", approve)

    with pytest.raises(PermissionError):
        asyncio.run(model_override.accept("chat-1", None, model=model, baseline="sonnet",
                                          action="start a review"))
    assert asked == [model]
    assert asyncio.run(model_override.accept("chat-1", None, model=model, baseline=model,
                                             action="start a review")) is None


def test_a_failed_approval_scope_lookup_refuses_without_asking(key, monkeypatch):
    """S16: a transient DB error finding the approval's API user is a
    retryable refusal, never an exception or the owner's ``None`` scope."""
    import asyncio

    from app.core import model_override

    model = f"prov:{cap.CLAUDE_API_PROVIDER_ID}:claude-sonnet-5"
    monkeypatch.setattr(model_override, "chat_of", lambda sid: sid)
    monkeypatch.setattr(model_override, "_APPROVED", {("chat-1", model, None)})

    def broken(chat):
        raise ConnectionError("database restarting")
    monkeypatch.setattr("app.core.api_user_scope.viewer_api_user_id", broken)

    async def approve(*a, **k):
        raise AssertionError("must not prompt on a failed lookup")
    monkeypatch.setattr(model_override, "_approve", approve)

    with pytest.raises(PermissionError, match="Try again"):
        asyncio.run(model_override.accept("chat-1", None, model=model, baseline="sonnet",
                                          action="start a review"))


def test_a_failed_first_chat_lookup_refuses_without_asking(key, monkeypatch):
    """Round 6: the first database read (the chat behind the session) fails
    the same retryable way."""
    import asyncio

    from app.core import model_override
    from app.db import database

    model = f"prov:{cap.CLAUDE_API_PROVIDER_ID}:claude-sonnet-5"

    def broken(*a, **k):
        raise ConnectionError("database restarting")
    monkeypatch.setattr(database, "get_session", broken)

    async def approve(*a, **k):
        raise AssertionError("must not prompt on a failed lookup")
    monkeypatch.setattr(model_override, "_approve", approve)

    with pytest.raises(PermissionError, match="Try again"):
        asyncio.run(model_override.accept("chat-1", None, model=model, baseline="sonnet",
                                          action="start a review"))


def test_a_late_catalog_write_for_an_old_key_keeps_the_new_keys_list(key, monkeypatch):
    """A slow read for key A that passed the active-key check before the owner
    switched to B must not replace B's saved list."""
    _api_key_mode()
    monkeypatch.setattr(claude_auth_mode, "stored_api_key", lambda: "sk-ant-b")
    cap.remember_models("sk-ant-b", [{"id": "claude-b-only", "label": "B"}])
    monkeypatch.setattr(claude_auth_mode, "stored_api_key", lambda: KEY)
    cap.remember_models(KEY, [{"id": "claude-a-only", "label": "A"}])  # A's late write
    monkeypatch.setattr(claude_auth_mode, "stored_api_key", lambda: "sk-ant-b")
    assert cap.virtual_row()["models_json"] == [{"id": "claude-b-only", "label": "B"}]
