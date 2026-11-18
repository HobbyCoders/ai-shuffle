"""Ultrafast speed tier: representation, gating, wire mapping, sync and notes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.api import sessions_permissions as api
from app.api.model_providers import _keep_stored_service_tiers, _parse_models_response
from app.core import model_speed as ms
from app.core.fast_outcome import fast_outcome_note
from app.core.provider.harness_adapter import HarnessAdapter, _native_request_extra
from app.core.provider.types import SessionOptions
from app.core.session_pool import SessionState
from app.db import database as db
from app.network import policy


# ── representation ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("fast,speed,expected", [
    (None, None, "standard"), (False, "ultrafast", "standard"), (True, None, "fast"),
    (True, "fast", "fast"), (True, "ultrafast", "ultrafast"), (True, "bogus", "fast"),
    (True, "standard", "fast"),
])
def test_fast_decides_on_and_speed_refines(fast, speed, expected):
    assert ms.resolve_speed(fast, speed) == expected


def test_speed_fields_keep_fast_for_older_readers():
    assert ms.speed_fields("ultrafast") == {"fast": True, "speed": "ultrafast"}
    assert ms.speed_fields("fast") == {"fast": True, "speed": "fast"}
    assert ms.speed_fields("standard") == {"fast": False, "speed": "standard"}
    assert ms.service_tier_for("ultrafast") == "ultrafast"
    assert ms.service_tier_for("fast") == "priority"
    assert ms.service_tier_for("standard") is None


# ── catalog tiers ──────────────────────────────────────────────────────────

def test_codex_catalog_keeps_known_service_tiers_only():
    payload = {"models": [
        {"slug": "gpt-6.1-sol", "service_tiers": [
            {"id": "priority", "name": "Fast", "description": "x"},
            {"id": "ultrafast", "name": "Ultrafast", "description": "y"},
            {"id": "<script>", "name": "?"}]},
        {"slug": "gpt-6-luna", "service_tiers": [{"id": "priority"}]},
        {"slug": "gpt-5.4", "additional_speed_tiers": ["fast"]},
        {"slug": "old-model"},
    ]}
    models = {m["id"]: m for m in _parse_models_response(payload, provider_type="chatgpt", service_tiers=True)}
    assert models["gpt-6.1-sol"]["service_tiers"] == ["priority", "ultrafast"]
    assert models["gpt-6-luna"]["service_tiers"] == ["priority"]
    assert models["gpt-5.4"]["service_tiers"] == ["priority"]
    assert models["old-model"]["service_tiers"] == []
    # Other catalogs never carry the field.
    plain = _parse_models_response(payload, provider_type="chatgpt")
    assert all("service_tiers" not in m for m in plain)


def test_fresh_catalog_replaces_tiers_and_saved_lists_cannot_forge_them():
    stored = [{"id": "gpt-6.1-sol", "service_tiers": ["priority", "ultrafast"]}, {"id": "gpt-6-astra"}]
    # A plan change: the account's catalog no longer lists Ultrafast.
    merged = ms.merge_service_tiers(stored, [{"id": "gpt-6.1-sol", "service_tiers": ["priority"]}])
    assert merged[0]["service_tiers"] == ["priority"]
    assert merged[1] == {"id": "gpt-6-astra"}
    # A client save keeps the stored tiers whatever it sends; a new model
    # keeps only known ids.
    saved = _keep_stored_service_tiers(
        [{"id": "gpt-6.1-sol", "service_tiers": ["ultrafast"]},
         {"id": "gpt-6-astra", "service_tiers": ["ultrafast", "warp"]}],
        [{"id": "gpt-6.1-sol", "service_tiers": ["priority"]}])
    assert saved == [{"id": "gpt-6.1-sol", "service_tiers": ["priority"]},
                     {"id": "gpt-6-astra", "service_tiers": ["ultrafast"]}]


# ── who may run Ultrafast ──────────────────────────────────────────────────

CHATGPT_PRO = {"provider_type": "chatgpt", "models_json": [
    {"id": "gpt-6.1-sol", "service_tiers": ["priority", "ultrafast"]},
    {"id": "gpt-6-luna", "service_tiers": ["priority"]}]}
CHATGPT_PLUS = {"provider_type": "chatgpt", "models_json": [
    {"id": "gpt-6.1-sol", "service_tiers": ["priority"]}]}


@pytest.mark.parametrize("row,model,expected", [
    (CHATGPT_PRO, "gpt-6.1-sol", True),
    (CHATGPT_PRO, "gpt-6-luna", False),
    (CHATGPT_PLUS, "gpt-6.1-sol", False),
    ({"provider_type": "custom", "auth_mode": "chatgpt", "models_json": CHATGPT_PRO["models_json"]},
     "gpt-6.1-sol", True),
    ({"provider_type": "chatgpt", "models_json": [{"id": "gpt-6.1-sol"}]}, "gpt-6.1-sol", False),
    ({"provider_type": "openai"}, "gpt-6-astra", True),
    ({"provider_type": "openai"}, "gpt-6.1-sol", True),
    ({"provider_type": "openai"}, "gpt-6.2-astra", True),
    ({"provider_type": "openai"}, "gpt-6.2-sol-2026-11-01", True),
    ({"provider_type": "openai"}, "gpt-6-luna", False),
    ({"provider_type": "openai"}, "gpt-5.4", False),
    ({"provider_type": "openrouter"}, "gpt-6.1-sol", False),
    ({"provider_type": "xai"}, "gpt-6.1-sol", False),
    ({"provider_type": "anthropic"}, "opus", False),
    (None, "gpt-6.1-sol", False),
])
def test_ultrafast_rule_per_provider(row, model, expected):
    assert ms.ultrafast_supported_for_row(row, model) is expected


# ── wire mapping ───────────────────────────────────────────────────────────

def _options(**kw):
    return SessionOptions(model=kw.pop("model", "gpt-6.1-sol"), extra={"provider_id": "p"}, **kw)


@pytest.mark.parametrize("fast,speed,row,expected", [
    (False, None, CHATGPT_PRO, {}),
    (True, None, CHATGPT_PRO, {"service_tier": "priority"}),
    (True, "ultrafast", CHATGPT_PRO, {"service_tier": "ultrafast"}),
    # Not offered (a Plus plan): the backend sends Fast, never ``ultrafast``.
    (True, "ultrafast", CHATGPT_PLUS, {"service_tier": "priority"}),
    (False, "ultrafast", CHATGPT_PRO, {}),
])
def test_request_extra_maps_speed_to_service_tier(monkeypatch, fast, speed, row, expected):
    monkeypatch.setattr("app.core.provider.harness_adapter._lookup_provider_row", lambda pid: row)
    assert _native_request_extra("openai_responses", _options(fast=fast, speed=speed)) == expected


def _live_adapter(monkeypatch, row):
    monkeypatch.setattr("app.core.provider.harness_adapter._lookup_provider_row", lambda pid: row)
    adapter = HarnessAdapter()
    adapter._provider_kind = "openai_responses"
    adapter._options = _options(effort="high", fast=False)
    adapter._engine = SimpleNamespace(effort="high", request_extra={"keep": 1}, ctx=SimpleNamespace(extra={}))
    adapter._sdk_options = SimpleNamespace(effort="high", aih_fast=False, aih_speed=None)
    return adapter


@pytest.mark.asyncio
async def test_mid_turn_switch_applies_to_the_next_request(monkeypatch):
    adapter = _live_adapter(monkeypatch, CHATGPT_PRO)
    result = await adapter.set_run_settings(effort="high", fast=True, speed="ultrafast")
    assert result["speed"] == "ultrafast"
    assert adapter._engine.request_extra == {"keep": 1, "service_tier": "ultrafast"}
    await adapter.set_run_settings(effort="high", fast=True, speed="fast")
    assert adapter._engine.request_extra == {"keep": 1, "service_tier": "priority"}
    await adapter.set_run_settings(effort="high", fast=False, speed="standard")
    assert adapter._engine.request_extra == {"keep": 1}
    assert adapter._sdk_options.aih_speed == "standard"


@pytest.mark.asyncio
async def test_set_run_settings_refuses_ultrafast_where_not_offered(monkeypatch):
    adapter = _live_adapter(monkeypatch, CHATGPT_PLUS)
    with pytest.raises(ValueError, match="Ultrafast"):
        await adapter.set_run_settings(effort="high", fast=True, speed="ultrafast")
    assert adapter._engine.request_extra == {"keep": 1}
    delegated = HarnessAdapter()
    delegated._is_delegated = True
    delegated._options = SessionOptions(model="opus", effort="high", fast=False)
    delegated._engine = SimpleNamespace(set_run_settings=AsyncMock(), ctx=None)
    with pytest.raises(ValueError, match="Ultrafast"):
        await delegated.set_run_settings(effort="high", fast=True, speed="ultrafast")


def test_reconnect_detects_a_tier_change_inside_fast():
    from app.core.agent_types import AgentOptions
    from app.core.session_pool import reconnect_option_names

    old, new = AgentOptions(model="gpt-6.1-sol"), AgentOptions(model="gpt-6.1-sol")
    old.aih_fast = new.aih_fast = True
    old.aih_speed, new.aih_speed = "fast", "ultrafast"
    assert "aih_speed" in reconnect_option_names(SessionState(adapter=object(), connect_options=old), new)
    new.aih_speed = "fast"
    assert "aih_speed" not in reconnect_option_names(SessionState(adapter=object(), connect_options=old), new)


# ── live settings API ──────────────────────────────────────────────────────

@pytest.fixture
def live(monkeypatch):
    adapter = _live_adapter(monkeypatch, CHATGPT_PRO)
    state = SessionState(adapter=adapter, is_connected=True, is_streaming=True,
                         connect_options=adapter._sdk_options)
    runtime = SimpleNamespace(turn={"overrides": {}})
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: SimpleNamespace(peek=lambda sid: state))
    monkeypatch.setattr("app.core.session_runtime.get_live_runtime", lambda sid: runtime)
    session = {"id": "s", "model": "gpt-6.1-sol", "effort": "high", "provider_id": "p"}
    monkeypatch.setattr(api.control, "_require_owned", lambda sid: session)
    save = MagicMock()
    monkeypatch.setattr(db, "set_session_run_binding", save)
    return state, runtime, save


@pytest.mark.asyncio
async def test_run_settings_speed_writes_fast_and_speed(live):
    state, runtime, save = live
    result = await api.apply_run_settings("s", api.RunSettingsUpdate(speed="ultrafast"))
    assert result == {"effort": "high", "fast": True, "speed": "ultrafast"}
    save.assert_called_with("s", effort="high", fast=True, speed="ultrafast", clear_effort=False)
    assert runtime.turn["overrides"]["speed"] == "ultrafast"
    assert state.connect_options.aih_speed == "ultrafast"
    # An older client's ``fast: true`` keeps Ultrafast; ``fast: false`` is Standard.
    assert (await api.apply_run_settings("s", api.RunSettingsUpdate(fast=True)))["speed"] == "ultrafast"
    assert (await api.apply_run_settings("s", api.RunSettingsUpdate(fast=False)))["speed"] == "standard"


@pytest.mark.asyncio
async def test_a_parent_hands_down_fast_never_ultrafast(live, monkeypatch):
    state, runtime, save = live
    monkeypatch.setattr(api, "_inherited_fast_skip", lambda session: None)
    await api.apply_run_settings("s", api.RunSettingsUpdate(speed="ultrafast"), propagate=False)
    result = await api.apply_run_settings("s", api.RunSettingsUpdate(fast=True), inherited=True,
                                          propagate=False)
    assert (result["fast"], result["speed"]) == (True, "fast")
    save.assert_called_with("s", effort="high", fast=True, speed="fast", clear_effort=False)
    assert state.adapter._engine.request_extra["service_tier"] == "priority"


@pytest.mark.asyncio
async def test_remote_owner_older_than_speed_degrades_to_fast(monkeypatch):
    calls = []

    async def call(owner, action, body):
        calls.append(body)
        if "speed" in body:
            raise api.control.ControlRefused(
                "1 validation error for RunSettingsUpdate\nspeed\n  Extra inputs are not permitted")
        return {"effort": "high", "fast": body["fast"]}

    monkeypatch.setattr(api.control, "call", call)
    result = await api._call_owner_run_settings("old-peer", "s", {"speed": "ultrafast"})
    assert result == {"effort": "high", "fast": True}
    assert calls == [{"session_id": "s", "speed": "ultrafast", "fast": True},
                     {"session_id": "s", "fast": True}]


# ── storage and peer sync ──────────────────────────────────────────────────

def test_binding_writes_both_and_old_writers_keep_working(mock_db):
    db.create_session("speedy", profile_id=None, model="gpt-6.1-sol", speed="ultrafast")
    row = db.get_session("speedy")
    assert (row["fast"], row["speed"]) == (True, "ultrafast")
    # An older writer that knows only ``fast`` turns it off: Standard.
    db.set_session_run_binding("speedy", fast=False)
    row = db.get_session("speedy")
    assert ms.resolve_speed(row["fast"], row.get("speed")) == "standard"
    db.set_session_run_binding("speedy", speed="fast")
    row = db.get_session("speedy")
    assert (row["fast"], row["speed"]) == (True, "fast")


def test_session_sync_carries_speed_and_older_owner_rows_clear_it():
    coll = policy.collection("sessions")
    assert "speed" in coll.fields and "speed" in coll.late_fields
    # An older owner's record has no ``speed``; its ``fast`` alone decides.
    materialized = coll.materialize_hook({"id": "s", "fast": True})
    assert materialized["speed"] is None
    assert ms.resolve_speed(materialized["fast"], materialized["speed"]) == "fast"
    # A newer owner's record keeps it.
    assert coll.materialize_hook({"id": "s", "fast": True, "speed": "ultrafast"})["speed"] == "ultrafast"


def test_unset_speed_keeps_the_published_content_hash():
    from app.network.engine import content_hash

    coll = policy.collection("sessions")
    base = {name: None for name in coll.fields}
    base.update(id="s", fast=True)
    assert content_hash(coll, {**base, "speed": None}) == content_hash(
        coll, {k: v for k, v in base.items() if k != "speed"})
    assert content_hash(coll, {**base, "speed": "ultrafast"}) != content_hash(coll, {**base, "speed": None})


# ── outcome note ───────────────────────────────────────────────────────────

def test_note_says_ran_on_ultrafast():
    note = fast_outcome_note({"requested": True, "requested_at_end": True, "ultrafast_calls": 3,
                              "speed": "ultrafast", "speed_at_end": "ultrafast"})
    assert note["content"] == "Ran on Ultrafast."
    assert note["outcome"] == "ultrafast"


def test_note_says_ultrafast_was_refused():
    note = fast_outcome_note({"requested": True, "requested_at_end": True, "standard_calls": 2,
                              "speed": "ultrafast", "speed_at_end": "ultrafast",
                              "disabled_reason": "ultrafast_unavailable"})
    assert note["content"] == "Ultrafast isn't available for this account/model; ran on Standard."
    assert note["outcome"] == "standard"


def test_fast_notes_are_unchanged():
    assert fast_outcome_note({"requested": True, "requested_at_end": True,
                              "fast_calls": 2})["content"] == "Ran on Fast."


def test_registry_flags_only_ultrafast_models(mock_db):
    from app.core.model_registry import registry_payload

    db.create_model_provider(provider_id="pro", name="Pro", provider_type="chatgpt",
                             base_url="https://chatgpt.com/backend-api/codex",
                             models=CHATGPT_PRO["models_json"], enabled=True)

    def flags():
        return {m["id"]: m.get("ultrafast", False)
                for p in registry_payload()["providers"] for m in p["models"]}

    assert flags() == {"gpt-6.1-sol": True, "gpt-6-luna": False}
    assert "speed_tiers_stale" not in registry_payload()["providers"][0]
    # The same sign-in moved to Plus: its refreshed catalog has no Ultrafast.
    db.update_model_provider("pro", models_json=CHATGPT_PLUS["models_json"])
    assert flags() == {"gpt-6.1-sol": False}


def test_a_chatgpt_row_saved_before_tiers_asks_for_one_refresh(mock_db):
    from app.core.model_registry import registry_payload

    db.create_model_provider(provider_id="old", name="Old", provider_type="chatgpt",
                             base_url="https://chatgpt.com/backend-api/codex",
                             models=[{"id": "gpt-6.1-sol"}], enabled=True)
    assert registry_payload()["providers"][0]["speed_tiers_stale"] is True
    # Once a catalog fetch stored its answer (even "none"), it stops asking.
    db.update_model_provider("old", models_json=[{"id": "gpt-6.1-sol", "service_tiers": []}])
    assert "speed_tiers_stale" not in registry_payload()["providers"][0]


def test_ultrafast_pick_is_one_canonical_record_per_model(mock_db):
    from app.core.model_effort import canonical_model_preference_key, speed_preference_key

    key = speed_preference_key("gpt-6.1-sol")
    assert key.startswith("composer.modelSpeed.")
    assert canonical_model_preference_key(key) == key
    db.set_user_preference("admin", "admin", key, "ultrafast")
    # A clear stays a replicated null like the Fast record's.
    assert db.delete_user_preference("admin", "admin", key) is True
    assert db.get_user_preference("admin", "admin", key)["value"] is None
