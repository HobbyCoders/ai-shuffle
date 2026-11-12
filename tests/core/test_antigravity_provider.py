"""Google Antigravity rows: leg choice, the personal-sign-in guard, the API and /usage.

The agent is a scripted stand-in (fluid/tests/fixtures/fake_acp_agent.py)
launched through the binary override, exactly as the real ``agy`` would be.
"""

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import antigravity as api
from app.api.auth import require_admin
from app.core import antigravity as ag
from app.core import encryption
from app.core.provider.harness_adapter import _provider_row_kind
from app.db import database as db

ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / "fluid" / "tests" / "fixtures" / "fake_acp_agent.py"
BASE = "/api/v1/antigravity"


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    """The fake agent behind the binary override, with a private data dir."""
    wrapper = tmp_path / "agy"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    wrapper.chmod(0o755)
    monkeypatch.setenv(ag.BINARY_ENV, str(wrapper))
    monkeypatch.setattr(ag, "_root", lambda: tmp_path / "antigravity")
    return wrapper


@pytest.fixture
def client(mock_db, monkeypatch, fake_agy):
    monkeypatch.setattr(encryption, "_encryption_key", encryption.Fernet.generate_key())
    monkeypatch.setattr(ag, "workers_requiring_update", lambda: [])
    app = FastAPI()
    app.include_router(api.router)
    app.dependency_overrides[require_admin] = lambda: None
    with TestClient(app) as client:
        yield client


def _create(client, mode="oauth-personal", **fields):
    response = client.post(f"{BASE}/providers", json={"name": "Antigravity", "auth_mode": mode, **fields})
    assert response.status_code == 200, response.text
    return response.json()


# --- leg choice -------------------------------------------------------------

@pytest.mark.parametrize("mode", ["oauth-personal", "oauth-business", "gemini-api-key"])
def test_antigravity_rows_take_their_own_leg(mode):
    assert _provider_row_kind("antigravity", mode, None) == "antigravity"
    assert _provider_row_kind("antigravity", mode, "anthropic") == "antigravity"


# --- the guard ----------------------------------------------------------------

def _options(session_id="s1", turn_source=None, **extra):
    return SimpleNamespace(extra={"app_session_id": session_id, **extra}, aih_turn_source=turn_source)


@pytest.fixture
def session_row(monkeypatch):
    rows = {"s1": {"id": "s1"}}
    started = {}
    from app.core import session_orchestration
    from app.db import sessions
    monkeypatch.setattr(sessions, "get_session", lambda sid: rows.get(sid))
    monkeypatch.setattr(session_orchestration, "_start_turn", lambda sid: started.get(sid))
    return rows["s1"], started


def test_owner_typing_in_their_own_chat_is_allowed(session_row):
    assert ag.unattended_reason(_options()) is None
    assert ag.unattended_reason(_options(turn_source="user")) is None
    assert ag.unattended_reason(_options(turn_source="queue")) is None


@pytest.mark.parametrize("field,value,reason", [
    ("automation_id", "auto-1", "an automation run"),
    ("is_worker", True, "a Task or Swarm worker"),
    ("parent_session_id", "parent", "a subagent"),
    ("hidden", True, "a background session"),
])
def test_unattended_sessions_are_named(session_row, field, value, reason):
    session_row[0][field] = value
    assert ag.unattended_reason(_options()) == reason


def test_unattended_turns_and_calls_are_named(session_row):
    _, started = session_row
    assert ag.unattended_reason(_options(turn_source="automation")) == "an automation run"
    assert ag.unattended_reason(_options(turn_source="wakeup")) == "a scheduled wake-up"
    assert ag.unattended_reason(_options(turn_source="auto_continue")) == "an automatic continuation"
    assert ag.unattended_reason(_options(turn_source="promotion")) == "a scheduled or automatic turn"
    assert ag.unattended_reason(_options(one_shot=True)) == "a background call"
    assert ag.unattended_reason(_options(assistant_proactive="x")) == "a background call"
    assert ag.unattended_reason(_options(session_id=None)) == "a background call"
    assert ag.unattended_reason(_options(session_id="missing")) == "a background call"
    started["s1"] = {"id": "t1"}
    assert ag.unattended_reason(_options()) == "a session another agent started"


def test_a_refused_agent_message_names_the_sending_agent(session_row, monkeypatch):
    from app.db import turns
    live = {"payload": {"peer_sender_label": "Release  checks\n(on Laptop)"}}
    monkeypatch.setattr(turns, "get_live_turn", lambda sid: live)
    assert (ag.unattended_reason(_options(turn_source="agent_message"))
            == 'a message from another agent ("Release checks (on Laptop)")')
    live["payload"] = {}
    assert ag.unattended_reason(_options(turn_source="agent_message")) == "a message from another agent"


def test_a_message_to_a_guarded_chat_is_refused_before_it_is_queued(mock_db, monkeypatch):
    """MessageSession to a chat on a guarded row fails for the sender, and
    the receiver is untouched (no turn, so it is never left failed)."""
    from app.core import session_orchestration as so
    from app.db import model_providers

    rows = {
        "personal": {"id": "personal", "provider_type": "antigravity", "auth_mode": "oauth-personal"},
        "lifted": {"id": "lifted", "provider_type": "antigravity", "auth_mode": "oauth-personal",
                   "extra_env_json": {ag.GUARD_OVERRIDE_KEY: "1"}},
        "key": {"id": "key", "provider_type": "antigravity", "auth_mode": "gemini-api-key"},
    }
    monkeypatch.setattr(model_providers, "get_model_provider", lambda pid, **_: rows.get(pid))
    chat = {"id": "r1", "title": "Weekly notes", "provider_id": "personal"}
    refusal = ag.agent_message_refusal(chat)
    assert refusal and refusal.startswith("Not sent:") and '"Weekly notes"' in refusal
    assert "Settings → Providers → Google Antigravity" in refusal
    for pid in ("lifted", "key", "missing", None):
        assert ag.agent_message_refusal({**chat, "provider_id": pid}) is None

    from app.db import sessions
    monkeypatch.setattr(sessions, "get_session", lambda sid: dict(chat))
    monkeypatch.setattr(so, "_local_top_level", lambda row, **_: True)
    monkeypatch.setattr(so, "_owner", lambda row, *a: "owner")
    with pytest.raises(ValueError, match="^Not sent:"):
        so._target({"id": "sender"}, "r1", owner="owner")
    chat["provider_id"] = "lifted"
    from app.core import mobile_tool_identity
    monkeypatch.setattr(mobile_tool_identity, "validate", lambda principal: None)
    monkeypatch.setattr(so, "_principal", lambda sid: None)
    assert so._target({"id": "sender"}, "r1", owner="owner")["id"] == "r1"


async def test_an_automation_on_a_guarded_row_fails_before_a_session_is_minted(monkeypatch):
    """The run fails with the guard's reason, logged as a warning with no
    traceback (an owner rule, not a crash), and mints no empty chat."""
    from app.core import automation_manager as am
    from app.core import automation_service
    from app.core.environment import ProviderTurnRefused
    from app.db import model_providers

    rows = {
        "personal": {"id": "personal", "provider_type": "antigravity", "auth_mode": "oauth-personal"},
        "key": {"id": "key", "provider_type": "antigravity", "auth_mode": "gemini-api-key"},
    }
    monkeypatch.setattr(model_providers, "get_model_provider", lambda pid, **_: rows.get(pid))
    assert ag.unattended_run_refusal("personal", "an automation run") == ag.GUARD_REFUSAL.format(
        reason="an automation run")
    for pid in ("key", "missing", None):
        assert ag.unattended_run_refusal(pid, "an automation run") is None

    monkeypatch.setattr(automation_service, "resolve_automation_model", lambda a: {
        "error": None, "selection": "prov:personal:gemini-3.8-flash", "provider_id": "personal"})
    minted = []
    monkeypatch.setattr(am.database, "create_session", lambda **kw: minted.append(kw))
    manager = am.AutomationManager.__new__(am.AutomationManager)
    monkeypatch.setattr(manager, "_shutdown_begun", lambda: False, raising=False)
    with pytest.raises(ProviderTurnRefused, match="refused for an automation run"):
        await manager._run_prompt_as_session({"id": "auto-1", "profile_id": "p1"}, "run-1", "hi", None)
    assert minted == []


def test_a_failed_check_counts_as_unattended(monkeypatch):
    from app.db import sessions

    def broken(sid):
        raise RuntimeError("db down")
    monkeypatch.setattr(sessions, "get_session", broken)
    assert ag.unattended_reason(_options()) == "a run it could not check"


def test_guard_applies_to_personal_sign_in_only(session_row):
    session_row[0]["automation_id"] = "auto-1"
    personal = {"auth_mode": "oauth-personal"}
    refusal = ag.guard_refusal(personal, _options())
    assert refusal and "an automation run" in refusal and "Settings" in refusal
    assert ag.guard_refusal({"auth_mode": "gemini-api-key"}, _options()) is None
    assert ag.guard_refusal({"auth_mode": "oauth-business"}, _options()) is None
    lifted = {"auth_mode": "oauth-personal", "extra_env_json": {ag.GUARD_OVERRIDE_KEY: "1"}}
    assert ag.guard_refusal(lifted, _options()) is None
    # The override is the app's; the agent never sees it.
    assert ag.agent_extra_env({**lifted, "extra_env_json": {ag.GUARD_OVERRIDE_KEY: "1", "X": "y"}}) == {"X": "y"}


def test_warning_names_the_terms_section_and_faq():
    assert "section 6" in ag.WARNING_TEXT and "FAQ" in ag.WARNING_TEXT
    assert "suspended" in ag.WARNING_TEXT


class _Engine:
    """Stands in for the connected AcpLoop: records process restarts."""

    def __init__(self):
        self.specs = []

    async def replace_spec(self, spec):
        self.specs.append(spec)


def _connected(row_id, session_id="s1"):
    """An adapter as ``_connect_antigravity`` leaves it, minus the process."""
    from app.core.provider.harness_adapter import HarnessAdapter

    adapter = HarnessAdapter.__new__(HarnessAdapter)
    adapter._options = SimpleNamespace(
        extra={"provider_id": row_id, "app_session_id": session_id}, add_dirs=[], aih_turn_source=None)
    adapter._engine = _Engine()
    adapter._antigravity_binding = ag.spawn_binding(db.get_model_provider(row_id, include_token=True))
    adapter._next_app_turn_source = None
    return adapter


async def _turn(adapter, source):
    adapter._next_app_turn_source = source
    await adapter._check_antigravity_turn()


async def test_guard_changes_apply_to_the_next_turn_of_a_connected_chat(client, session_row):
    # Each turn reads the stored row: the copy a chat took when it connected
    # decides nothing, in either direction.
    from app.core.environment import ProviderConfigError, ProviderTurnRefused

    row = _create(client)
    client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": True})
    adapter = _connected(row["id"])
    await _turn(adapter, "automation")  # lifted: an automatic turn runs
    client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": False})
    with pytest.raises(ProviderConfigError, match="refused for an automation run"):
        await _turn(adapter, "automation")
    with pytest.raises(ProviderTurnRefused, match="refused for a scheduled wake-up"):
        await _turn(adapter, "wakeup")
    await _turn(adapter, "user")  # the owner's own turn still goes through
    await _turn(adapter, None)  # the source names one submit only
    client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": True})
    await _turn(adapter, "automation")
    # Session-level reasons are re-checked too once the guard is back on.
    session_row[0]["automation_id"] = "auto-1"
    await _turn(adapter, "user")
    client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": False})
    with pytest.raises(ProviderConfigError, match="an automation run"):
        await _turn(adapter, "user")
    # The guard is the app's setting, not the agent's: no process restart.
    assert adapter._engine.specs == []


async def test_key_rows_are_never_guarded_and_restart_on_a_new_key(client, session_row):
    row = _create(client, "gemini-api-key", api_key="test-gemini-secret-value")
    adapter = _connected(row["id"])
    await _turn(adapter, "automation")
    assert adapter._engine.specs == []
    from app.api.model_providers import _encrypt_token_or_400
    db.update_model_provider(row["id"], auth_token=_encrypt_token_or_400("test-gemini-other-value"))
    await _turn(adapter, "user")
    # The key lives in the agent's environment: the next turn starts a fresh
    # process with the new one (and resumes the saved conversation).
    assert len(adapter._engine.specs) == 1
    assert adapter._engine.specs[0].env.get("GEMINI_API_KEY") == "test-gemini-other-value"
    await _turn(adapter, "user")
    assert len(adapter._engine.specs) == 1


async def test_a_deleted_or_disabled_row_refuses_the_next_turn(client, session_row):
    from app.core.environment import ProviderConfigError

    row = _create(client, "gemini-api-key", api_key="test-gemini-secret-value")
    adapter = _connected(row["id"])
    db.update_model_provider(row["id"], enabled=False)
    with pytest.raises(ProviderConfigError, match="disabled"):
        await _turn(adapter, "user")
    db.update_model_provider(row["id"], enabled=True)
    await _turn(adapter, "user")
    db.delete_model_provider(row["id"])
    with pytest.raises(ProviderConfigError, match="no longer exists"):
        await _turn(adapter, "user")


class _Running:
    """A connected engine as the registry sees it: records each stop."""

    def __init__(self):
        self.stops = []

    async def stop_agent(self, reason):
        self.stops.append(reason)


def _running(row_id):
    engine = _Running()
    ag.register_agent(row_id, engine, ag.spawn_binding(db.get_model_provider(row_id, include_token=True)))
    return engine


async def test_sign_out_and_delete_end_the_rows_running_agents(mock_db, fake_agy):
    from app.api import model_providers as providers_api

    row, other = await _personal_row(), await _personal_row("Other")
    engine, bystander = _running(row["id"]), _running(other["id"])
    before = ag.spawn_binding(row)
    await ag.sign_out(row)
    assert engine.stops == [ag.STOPPED_SIGNED_OUT] and bystander.stops == []
    # A process started before the sign-out is never reused by the next turn.
    assert ag.spawn_binding(row) != before
    assert (await providers_api.delete_provider(row["id"], token=None)) == {"success": True}
    assert engine.stops[-1] == ag.STOPPED_REMOVED and bystander.stops == []


async def test_a_delete_from_another_worker_ends_the_rows_running_agents(mock_db, fake_agy, monkeypatch):
    from app.core.global_events import global_events
    from app.network import startup

    row = await _personal_row()
    engine = _running(row["id"])
    db.delete_model_provider(row["id"])  # what materialize does for the tombstone
    monkeypatch.setattr(global_events, "_loop", asyncio.get_running_loop(), raising=False)
    startup._on_applied([("model_providers", row["id"], "delete")])
    for _ in range(100):
        if engine.stops:
            break
        await asyncio.sleep(0.05)
    assert engine.stops and engine.stops[0] == ag.STOPPED_REMOVED


async def test_turning_a_row_off_ends_its_running_agents(mock_db, fake_agy, monkeypatch):
    from app.api import model_providers as providers_api
    from app.core.global_events import global_events
    from app.network import startup

    row = await _personal_row()
    engine = _running(row["id"])
    await providers_api.toggle_provider(row["id"], token=None)
    assert engine.stops == [ag.STOPPED_DISABLED]
    await providers_api.toggle_provider(row["id"], token=None)  # back on: nothing to end
    assert engine.stops == [ag.STOPPED_DISABLED]
    # Turned off on another worker.
    db.update_model_provider(row["id"], enabled=False)
    monkeypatch.setattr(global_events, "_loop", asyncio.get_running_loop(), raising=False)
    startup._on_applied([("model_providers", row["id"], "upsert")])
    for _ in range(100):
        if len(engine.stops) > 1:
            break
        await asyncio.sleep(0.05)
    assert engine.stops == [ag.STOPPED_DISABLED, ag.STOPPED_DISABLED]


async def test_a_new_key_ends_the_rows_running_agents_and_other_edits_do_not(client, session_row):
    from app.api.model_providers import _encrypt_token_or_400

    row = _create(client, "gemini-api-key", api_key="test-gemini-secret-value")
    engine = _running(row["id"])
    db.update_model_provider(row["id"], name="Renamed", models=list(ag.DEFAULT_MODELS[:1]))
    await ag.reconcile_agents([row["id"]])
    assert engine.stops == []  # nothing the agent holds changed
    db.update_model_provider(row["id"], auth_token=_encrypt_token_or_400("test-gemini-other-value"))
    await ag.reconcile_agents([row["id"]])
    assert engine.stops == [ag.STOPPED_KEY_CHANGED]


async def test_a_sign_in_row_counts_as_ready_only_while_signed_in_here(mock_db, fake_agy):
    from app.api.model_providers import _public_provider, _row_with_token_flags
    from app.core import credential_gate

    row = await _personal_row()
    usable = lambda: [r["id"] for r in credential_gate.usable_provider_rows()]  # noqa: E731
    assert row["id"] not in usable()
    assert _public_provider(_row_with_token_flags(row["id"])).antigravity_signed_in is False
    ag._note_signin(row["id"], "signed_in")  # what a status check or completed sign-in records
    assert row["id"] in usable() and ag.signed_in_here(row["id"])
    assert _public_provider(_row_with_token_flags(row["id"])).antigravity_signed_in is True
    # Not even then the implicit default: an explicit pick only.
    assert credential_gate.default_provider_model() is None
    db.update_model_provider(row["id"], enabled=False)
    assert row["id"] not in usable()
    db.update_model_provider(row["id"], enabled=True)
    ag._note_signin(row["id"], "error")  # an error says nothing about the sign-in
    assert ag.signed_in_here(row["id"])
    await ag.sign_out(row)
    assert row["id"] not in usable() and not ag.signed_in_here(row["id"])


async def test_a_signed_out_answer_clears_ready_and_a_stale_answer_is_ignored(mock_db, fake_agy, monkeypatch):
    row = await _personal_row()
    ag._note_signin(row["id"], "signed_in")
    answers = iter([{"state": "signed_out"}])

    async def answer(r):
        return next(answers)
    monkeypatch.setattr(ag, "_probe", answer)
    await ag.probe(row)
    assert not ag.signed_in_here(row["id"])

    async def slow_signed_in(r):
        await ag.forget_row(row["id"])  # a sign-out lands while the agent answers
        return {"state": "signed_in"}
    monkeypatch.setattr(ag, "_probe", slow_signed_in)
    await ag.probe(row)
    assert not ag.signed_in_here(row["id"])


# --- API ----------------------------------------------------------------------

def test_meta_carries_the_warning_and_guard_default(client):
    body = client.get(f"{BASE}/meta").json()
    assert body["provider_type"] == "antigravity" and body["capability"] == "antigravity_acp_v1"
    assert body["warning"] == ag.WARNING_TEXT and body["guard_default"] is True
    modes = {m["id"]: m for m in body["auth_modes"]}
    assert modes["oauth-personal"]["guarded"] and modes["oauth-personal"]["warning"]
    assert not modes["oauth-business"]["headless"] and not modes["gemini-api-key"]["guarded"]


def test_install_status_reports_the_binary(client):
    body = client.get(f"{BASE}/status").json()
    assert body["installed"] is True and body["version"]
    assert body["workers_requiring_update"] == []


def test_key_rows_store_the_key_encrypted_and_never_return_it(client):
    assert client.post(f"{BASE}/providers", json={"name": "A", "auth_mode": "gemini-api-key"}).status_code == 400
    assert client.post(f"{BASE}/providers", json={"name": "A", "auth_mode": "oauth-personal",
                                                  "api_key": "k"}).status_code == 400
    row = _create(client, "gemini-api-key", api_key="test-gemini-secret-value")
    assert "test-gemini-secret-value" not in str(row)
    assert row["provider_type"] == "antigravity" and row["guard_applies"] is False
    stored = db.get_model_provider(row["id"], include_token=True)
    assert encryption.is_encrypted(stored["auth_token"]) and stored["base_url"] == ""
    assert ag.row_api_key(stored) == "test-gemini-secret-value"
    status = client.get(f"{BASE}/providers/{row['id']}/status").json()
    assert status["state"] == "signed_in", status
    assert status["agent"]["agent"] == "Antigravity ACP Server"
    assert status["agent"]["system_prompt"] == "append" and status["agent"]["resume"]
    assert client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": True}).status_code == 400
    personal = _create(client)
    listed = client.get(f"{BASE}/providers").json()["providers"]
    assert {r["id"]: r["guarded"] for r in listed} == {row["id"]: False, personal["id"]: True}
    assert "test-gemini-secret-value" not in str(listed)


def test_rows_carry_gemini_models_with_thinking_levels(client):
    from app.api import model_providers
    from app.core.model_effort import effort_capabilities

    row = _create(client, "gemini-api-key", api_key="test-gemini-secret-value")
    stored = db.get_model_provider(row["id"])
    pro = effort_capabilities("gemini-3.1-pro", stored)
    assert pro["effort_levels"] == ["low", "high"] and pro["default_effort"] is None
    assert effort_capabilities("gemini-3.8-flash", stored)["effort_levels"] == ["low", "medium", "high"]
    # Model curation lists the agent's Gemini models; there is no HTTP catalog.
    catalog_app = FastAPI()
    catalog_app.include_router(model_providers.router)
    catalog_app.dependency_overrides[require_admin] = lambda: None
    with TestClient(catalog_app) as catalog:
        fetched = catalog.post(f"/api/v1/model-providers/{row['id']}/fetch-models")
    assert fetched.status_code == 200, fetched.text
    ids = [m["id"] for m in fetched.json()["models"]]
    assert ids == [m["id"] for m in ag.DEFAULT_MODELS] and all(i.startswith("gemini-") for i in ids)


@pytest.mark.parametrize("mode,billing", [
    ("oauth-personal", "subscription"), ("oauth-business", "subscription"),
    ("gemini-api-key", "api"),
])
def test_stats_bill_sign_in_rows_as_a_plan(mode, billing):
    from app.core.usage_attribution import provider_billing
    assert provider_billing("antigravity", mode) == billing


def test_picker_tells_sign_in_rows_apart(client):
    from app.core import model_registry

    ids = {_create(client, mode)["id"]: mode for mode in ("oauth-personal", "oauth-business")}
    ids[_create(client, "gemini-api-key", api_key="test-gemini-secret-value")["id"]] = "gemini-api-key"
    shown = {r["id"]: (r["auth_kind"], r["plan_type"]) for r in model_registry._providers_payload()
             if r["id"] in ids}
    assert {ids[i]: v for i, v in shown.items()} == {
        "oauth-personal": ("subscription", "Personal"),
        "oauth-business": ("subscription", "Business"),
        "gemini-api-key": ("api", None),
    }


@pytest.mark.parametrize("mode", ["oauth-personal", "oauth-business", "gemini-api-key"])
def test_session_options_build_for_every_row(client, mode):
    # A sign-in row stores no token or URL; the agent's env is built by
    # app.core.antigravity, so the CLI overlay neither refuses the row nor
    # copies the key or the guard setting into it.
    from app.core.environment import apply_provider_env

    key = {"api_key": "test-gemini-secret-value"} if mode == "gemini-api-key" else {}
    row = _create(client, mode, **key)
    if mode == "oauth-personal":
        client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": True})
    env = apply_provider_env({}, row["id"], "gemini-3.1-pro")
    assert "test-gemini-secret-value" not in env.values()
    assert ag.GUARD_OVERRIDE_KEY not in env


def test_personal_rows_are_guarded_until_the_owner_lifts_it(client):
    row = _create(client)
    assert row["guard_applies"] and row["guarded"] and not row["allow_unattended"]
    lifted = client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": True}).json()
    assert lifted == {"guard_applies": True, "allow_unattended": True, "guarded": False}
    assert not ag.guarded(db.get_model_provider(row["id"]))
    restored = client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": False}).json()
    assert restored["guarded"] is True


def test_personal_sign_in_runs_the_binary_flow(client):
    row = _create(client)
    status = client.get(f"{BASE}/providers/{row['id']}/status").json()
    assert status["state"] == "signed_out" and status["login"] is None
    started = client.post(f"{BASE}/providers/{row['id']}/login").json()
    assert started["state"] == "awaiting_code", started
    assert started["url"].startswith("https://accounts.google.com/")
    # A sign-in started elsewhere shows on every screen that asks.
    assert client.get(f"{BASE}/providers/{row['id']}/status").json()["login"] == started
    bad = client.post(f"{BASE}/providers/{row['id']}/login/{started['login_id']}/complete",
                      json={"code": "wrong"}).json()
    assert bad["state"] == "failed"
    # A plain message with the next step: not the agent's raw "invalid_grant"
    # and none of its own prompts.
    assert bad["message"] == ag.CODE_REJECTED
    assert client.get(f"{BASE}/providers/{row['id']}/status").json()["login"] is None
    started = client.post(f"{BASE}/providers/{row['id']}/login").json()
    done = client.post(f"{BASE}/providers/{row['id']}/login/{started['login_id']}/complete",
                       json={"code": "good"}).json()
    assert done["state"] == "signed_in", done
    assert client.get(f"{BASE}/providers/{row['id']}/status").json()["state"] == "signed_in"
    # Signed in already: nothing is started, and it says so.
    again = client.post(f"{BASE}/providers/{row['id']}/login").json()
    assert again["state"] == "signed_in" and again["login_id"] is None
    assert again["message"] == ag.ALREADY_SIGNED_IN
    assert client.get(f"{BASE}/providers/{row['id']}/status").json()["login"] is None
    assert client.post(f"{BASE}/providers/{row['id']}/sign-out").json() == {"state": "signed_out"}
    assert client.get(f"{BASE}/providers/{row['id']}/status").json()["state"] == "signed_out"


async def test_a_wrong_code_on_a_signed_in_worker_is_not_a_sign_in(mock_db, fake_agy, monkeypatch):
    """The agent's own refusal of the code wins over the check after it,
    which still finds the earlier sign-in."""
    row = {"id": "p1", "provider_type": "antigravity", "auth_mode": "oauth-personal"}
    monkeypatch.setattr(ag, "signed_in_here", lambda row_id: False)
    started = await ag.start_login(row)
    assert started["state"] == "awaiting_code"

    async def still_signed_in(r):
        return {"state": "signed_in"}
    monkeypatch.setattr(ag, "probe", still_signed_in)
    login = ag.get_login(started["login_id"])
    result = await ag.complete_login(login, "wrong", row)
    await login.proc.wait()
    assert result["state"] == "failed"
    assert result["message"].startswith(ag.CODE_REJECTED) and "still signed in" in result["message"]


def test_business_sign_in_has_no_headless_flow(client):
    row = _create(client, "oauth-business")
    body = client.post(f"{BASE}/providers/{row['id']}/login").json()
    assert body["state"] == "failed" and "browser" in body["message"]


async def test_install_does_nothing_when_that_version_is_installed(mock_db, fake_agy, monkeypatch):
    import platform

    import httpx

    if os.name != "posix" or platform.machine().lower() not in ("x86_64", "amd64"):
        pytest.skip("the app installs Antigravity on Linux x64 only")
    version = await ag.binary_version(str(fake_agy))
    assert version
    downloads = []

    class _Manifest:
        def raise_for_status(self):
            return self

        def json(self):
            return {"version": version, "url": "https://storage.googleapis.com/fake/cli.tar.gz",
                    "sha512": "00"}

    async def get(self, url, **kw):
        return _Manifest()

    def stream(self, *args, **kw):
        downloads.append(args)
        raise AssertionError("a stale Install button must not download")
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(httpx.AsyncClient, "stream", stream)
    monkeypatch.setattr(ag, "_install", ag.InstallJob())
    monkeypatch.setattr(ag, "_install_task", None)
    ag.start_install()
    await ag._install_task
    assert ag.install_state() == {"state": "done", "version": version, "error": None}
    assert downloads == []


def test_not_installed_is_reported(client, monkeypatch):
    row = _create(client)
    monkeypatch.setenv(ag.BINARY_ENV, "/nonexistent/agy")
    status = client.get(f"{BASE}/providers/{row['id']}/status").json()
    assert status["state"] == "not_installed"
    # Read on the provider's own screen: it points below, not back to itself.
    assert status["message"] == ag.NOT_INSTALLED_HERE and "Settings" not in status["message"]
    assert client.get(f"{BASE}/status").json()["installed"] is False


def test_creation_waits_for_every_worker(client, monkeypatch):
    monkeypatch.setattr(ag, "workers_requiring_update", lambda: ["Older worker"])
    result = client.post(f"{BASE}/providers", json={"name": "A", "auth_mode": "oauth-personal"})
    assert result.status_code == 409
    assert result.json()["detail"]["workers"] == ["Older worker"]

    def broken():
        raise RuntimeError("connection to 10.0.0.9 failed: password")
    monkeypatch.setattr(ag, "workers_requiring_update", broken)
    result = client.post(f"{BASE}/providers", json={"name": "A", "auth_mode": "oauth-personal"})
    assert result.status_code == 503 and "10.0.0.9" not in result.text
    assert not db.get_all_model_providers()


def test_capability_follows_signed_presence(monkeypatch):
    from app.db import network as store
    from app.network import engine

    monkeypatch.setattr(engine, "context", lambda: SimpleNamespace(device_id="self"))
    monkeypatch.setattr(store, "list_members", lambda **kw: [
        {"device_id": "self"}, {"device_id": "new", "name": "New"}, {"device_id": "old", "name": "Old"}])
    monkeypatch.setattr(engine, "all_presence", lambda: {"new": {"capabilities": {ag.CAPABILITY: 1}}})
    assert ag.workers_requiring_update() == ["Old"]


def test_this_worker_advertises_the_capability():
    text = (ROOT / "app" / "network" / "engine.py").read_text()
    assert f'"{ag.CAPABILITY}": 1' in text


# --- /usage ---------------------------------------------------------------------

def test_usage_parses_the_reported_shape():
    windows = ag.normalize_usage({"status": "OK", "quotas": [
        {"model": "gemini-3.1-pro", "remainingFraction": 0.75, "resetTime": "2026-11-12T10:00:00Z"}]})
    assert windows == [{"type": "antigravity_gemini_3_1_pro", "label": "gemini-3.1-pro",
                        "utilization": 25.0, "resets_at": "2026-11-12T10:00:00Z"}]


@pytest.mark.parametrize("payload", [
    None, [], "text", {"status": "ERROR", "error": {"message": "Authentication required"}},
    {"quotas": [{"remainingFraction": 7}]}, {"quotas": [{"remainingFraction": "half"}]},
    {"usage": {"remaining": 0.5}}, {"status": "OK"},
])
def test_usage_that_does_not_parse_is_unavailable(payload):
    assert ag.normalize_usage(payload) is None


async def test_usage_reads_only_on_demand(mock_db, fake_agy, monkeypatch):
    from app.core.provider_usage import ProviderUsageMonitor

    row = {"id": "row-1", "provider_type": "antigravity", "auth_mode": "oauth-personal",
           "extra_env_json": {"FAKE_ACP_SIGNED_IN": "1"}}
    calls = []
    real_read = ag.read_usage

    async def counted(r):
        calls.append(r["id"])
        return await real_read(r)
    monkeypatch.setattr(ag, "read_usage", counted)
    writes = []
    monkeypatch.setattr(db, "upsert_provider_usage_snapshot", lambda *a, **kw: writes.append(("ok", a)))
    monkeypatch.setattr(db, "record_provider_usage_failure", lambda *a, **kw: writes.append(("fail", a)))
    monitor = ProviderUsageMonitor()
    await monitor._poll_antigravity_provider(row)
    assert calls == [] and writes == []
    monitor._on_demand = True
    await monitor._poll_antigravity_provider(row)
    assert calls == ["row-1"] and writes[0][0] == "ok"
    assert writes[0][1][1] == "antigravity" and writes[0][1][3][0]["utilization"] == 25.0
    monkeypatch.setenv("FAKE_ACP_USAGE", "not json")
    row["extra_env_json"]["FAKE_ACP_USAGE"] = "not json"
    await monitor._poll_antigravity_provider(row)
    assert writes[1] == ("fail", ("row-1", "antigravity", "unavailable"))


# --- sign-in lifecycle: one per row, ended by sign-out, delete or idleness --------

async def _personal_row(name="Antigravity"):
    import uuid as _uuid
    row = db.create_model_provider(provider_id=str(_uuid.uuid4()), name=name, provider_type=ag.PROVIDER_TYPE,
                                   base_url="", auth_mode="oauth-personal", auth_token=None,
                                   models=list(ag.DEFAULT_MODELS), default_model=None, enabled=True)
    return db.get_model_provider(row["id"], include_token=True)


def _row_logins(row_id):
    return [x for x in ag._logins.values() if x.row_id == row_id]


async def _ended(login):
    await asyncio.wait_for(login.proc.wait(), timeout=5)
    return login.proc.returncode is not None


async def test_two_starts_leave_one_sign_in(mock_db, fake_agy):
    row = await _personal_row()
    first, second = await asyncio.gather(ag.start_login(row), ag.start_login(row))
    assert {first["state"], second["state"]} == {"awaiting_code"}
    live = _row_logins(row["id"])
    assert len(live) == 1 and live[0].id == second["login_id"]
    stale = ag.get_login(first["login_id"])
    assert stale is None
    await ag.cancel_login(second["login_id"])
    assert await _ended(live[0]) and not _row_logins(row["id"])


async def test_an_abandoned_sign_in_ends_on_its_own(mock_db, fake_agy, monkeypatch):
    monkeypatch.setattr(ag, "_LOGIN_IDLE_SECONDS", 1.5)
    row = await _personal_row()
    started = await ag.start_login(row)
    login = ag.get_login(started["login_id"])
    assert login is not None and started["state"] == "awaiting_code"
    await asyncio.sleep(2)  # no later start needed to clean it up
    assert ag.get_login(started["login_id"]) is None and await _ended(login)


async def test_sign_out_ends_a_pending_sign_in_and_its_reading(mock_db, fake_agy):
    row = await _personal_row()
    started = await ag.start_login(row)
    login = ag.get_login(started["login_id"])
    home = Path(ag.home_for(row["id"]))
    db.upsert_provider_usage_snapshot(row["id"], "antigravity", None, [{"type": "x", "utilization": 1.0}],
                                      None, {}, 1)
    await ag.sign_out(row)
    assert await _ended(login) and not _row_logins(row["id"]) and not home.exists()
    assert db.get_provider_usage_raw(row["id"]) is None
    late = await ag.complete_login(login, "good", row)
    assert late["state"] == "failed" and not home.joinpath(".fake_acp_signed_in").exists()


async def test_a_local_delete_removes_the_rows_sign_in(mock_db, fake_agy):
    from app.api import model_providers as providers_api

    row = await _personal_row()
    keep = await _personal_row("Other")
    started = await ag.start_login(row)
    login = ag.get_login(started["login_id"])
    home, other = Path(ag.home_for(row["id"])), Path(ag.home_for(keep["id"]))
    (home / "agent-state").write_text("x")
    assert (await providers_api.delete_provider(row["id"], token=None)) == {"success": True}
    assert await _ended(login) and not _row_logins(row["id"]) and not home.exists()
    assert other.is_dir()
    await ag.forget_row(row["id"])  # already gone: nothing to do, no error


async def test_a_delete_from_another_worker_removes_the_rows_sign_in(mock_db, fake_agy, monkeypatch):
    from app.core.global_events import global_events
    from app.network import startup

    row = await _personal_row()
    keep = await _personal_row("Other")
    started = await ag.start_login(row)
    login = ag.get_login(started["login_id"])
    home, other = Path(ag.home_for(row["id"])), Path(ag.home_for(keep["id"]))
    db.delete_model_provider(row["id"])  # what materialize does for the tombstone
    monkeypatch.setattr(global_events, "_loop", asyncio.get_running_loop(), raising=False)
    calls = []
    real = ag.prune_orphan_homes

    async def counted():
        calls.append(1)
        return await real()
    monkeypatch.setattr(ag, "prune_orphan_homes", counted)
    startup._on_applied([("model_providers", row["id"], "delete")])
    for _ in range(100):
        if calls and not home.exists():
            break
        await asyncio.sleep(0.05)
    assert calls and await _ended(login) and not _row_logins(row["id"]) and not home.exists()
    assert other.is_dir()


async def test_startup_prune_finishes_an_earlier_removal(mock_db, fake_agy):
    keep = await _personal_row()
    stale = Path(ag.home_for("row-deleted-while-down"))
    (stale / "left").write_text("x")
    assert await ag.prune_orphan_homes() == 1
    assert not stale.exists() and Path(ag.home_for(keep["id"])).is_dir()


# --- row writers: lock and type rule -------------------------------------------------

async def test_guard_write_does_not_lose_a_concurrent_edit(mock_db, fake_agy):
    from app.core.provider_locks import operation_lock

    row = await _personal_row()
    lock = operation_lock(row["id"])
    await lock.acquire()
    task = asyncio.create_task(api.set_guard(row["id"], api.GuardUpdate(allow_unattended=True), token=None))
    await asyncio.sleep(0.05)
    db.update_model_provider(row["id"], extra_env_json={"OTHER_SETTING": "1"})  # a writer holding the lock
    lock.release()
    await task
    env = db.get_model_provider(row["id"])["extra_env_json"]
    assert env == {"OTHER_SETTING": "1", ag.GUARD_OVERRIDE_KEY: "1"}


async def test_guard_write_does_not_lose_a_synced_edit(mock_db, fake_agy, monkeypatch):
    # A synced edit takes no local lock: it lands between the read and the write.
    row = await _personal_row()
    real = api._row
    calls = []

    async def read_then_sync(provider_id, **kw):
        found = await real(provider_id, **kw)
        if not calls:
            db.update_model_provider(provider_id, extra_env_json={"OTHER_SETTING": "1"})
        calls.append(1)
        return found
    monkeypatch.setattr(api, "_row", read_then_sync)
    await api.set_guard(row["id"], api.GuardUpdate(allow_unattended=True), token=None)
    assert len(calls) == 2
    env = db.get_model_provider(row["id"])["extra_env_json"]
    assert env == {"OTHER_SETTING": "1", ag.GUARD_OVERRIDE_KEY: "1"}


async def test_a_generic_edit_keeps_the_guard_and_the_type(mock_db, fake_agy):
    from fastapi import HTTPException
    from app.api import model_providers as providers_api

    row = await _personal_row()
    await api.set_guard(row["id"], api.GuardUpdate(allow_unattended=True), token=None)
    await providers_api.update_provider(row["id"], providers_api.ModelProviderUpdate(
        extra_env={"OTHER_SETTING": "1"}), token=None)
    env = db.get_model_provider(row["id"])["extra_env_json"]
    assert env == {"OTHER_SETTING": "1", ag.GUARD_OVERRIDE_KEY: "1"}
    await providers_api.update_provider(row["id"], providers_api.ModelProviderUpdate(
        extra_env={ag.GUARD_OVERRIDE_KEY: "0"}), token=None)
    assert ag.guard_lifted(db.get_model_provider(row["id"]))  # only the guard endpoint changes it
    for other in ("openrouter", "custom", "google"):
        with pytest.raises(HTTPException) as refused:
            await providers_api.update_provider(row["id"], providers_api.ModelProviderUpdate(
                provider_type=other), token=None)
        assert refused.value.status_code == 400
    assert db.get_model_provider(row["id"])["provider_type"] == ag.PROVIDER_TYPE
    other = db.create_model_provider(provider_id="row-openrouter", name="OR", provider_type="openrouter",
                                     base_url="https://openrouter.ai/api", auth_token=None, models=[],
                                     default_model=None, enabled=True)
    with pytest.raises(HTTPException) as refused:  # and nothing becomes an Antigravity row
        await providers_api.update_provider(other["id"], providers_api.ModelProviderUpdate(
            provider_type="antigravity"), token=None)
    assert refused.value.status_code == 400
    assert db.get_model_provider(other["id"])["provider_type"] == "openrouter"


# --- usage readings that must not land ------------------------------------------------

async def _poll_with(monkeypatch, row, during, unparsed=False):
    from app.core.provider_usage import ProviderUsageMonitor

    async def read(r):
        await during()
        return None if unparsed else {"status": "OK", "quotas": [{"model": "gemini-3.1-pro", "remainingFraction": 0.5,
                                                                  "resetTime": "2026-11-12T10:00:00Z"}]}
    monkeypatch.setattr(ag, "read_usage", read)
    monitor = ProviderUsageMonitor()
    monitor._on_demand = True
    await monitor._poll_antigravity_provider(row)
    return db.get_provider_usage_raw(row["id"])


async def test_a_reading_is_written_when_nothing_changed(mock_db, fake_agy, monkeypatch):
    row = await _personal_row()

    async def nothing():
        return None
    assert await _poll_with(monkeypatch, row, nothing) is not None
    assert (await _poll_with(monkeypatch, row, nothing, unparsed=True) or {}).get("usage_error") == "unavailable"


@pytest.mark.parametrize("unparsed", [False, True], ids=["reading", "unavailable"])
@pytest.mark.parametrize("change", ["key", "disable", "sign_out", "delete"])
async def test_a_reading_started_before_a_change_is_not_written(mock_db, fake_agy, monkeypatch, change, unparsed):
    from app.api.model_providers import _encrypt_token_or_400

    monkeypatch.setattr(encryption, "_encryption_key", encryption.Fernet.generate_key())
    row = await _personal_row()

    async def during():
        if change == "key":
            db.update_model_provider(row["id"], auth_token=_encrypt_token_or_400("test-gemini-other-value"))
        elif change == "disable":
            db.update_model_provider(row["id"], enabled=False)
        elif change == "sign_out":
            await ag.sign_out(row)
        else:
            db.delete_model_provider(row["id"])
    assert await _poll_with(monkeypatch, row, during, unparsed) is None


async def test_a_cancelled_usage_read_ends_its_process(mock_db, fake_agy, monkeypatch):
    procs = []
    real = asyncio.create_subprocess_exec

    async def spawn(*args, **kw):
        proc = await real(*args, **kw)
        procs.append(proc)
        return proc
    monkeypatch.setattr(ag.asyncio, "create_subprocess_exec", spawn)
    row = {"id": "row-1", "provider_type": "antigravity", "auth_mode": "oauth-personal",
           "extra_env_json": {"FAKE_ACP_SIGNED_IN": "1", "FAKE_ACP_USAGE_SLEEP": "60"}}
    task = asyncio.create_task(ag.read_usage(row))
    for _ in range(100):
        if procs:
            break
        await asyncio.sleep(0.02)
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.wait_for(procs[0].wait(), timeout=5)
    assert procs[0].returncode is not None
