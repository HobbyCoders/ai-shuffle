"""Google Antigravity rows: leg choice, the personal-sign-in guard, the API and /usage.

The agent is a scripted stand-in (fluid/tests/fixtures/fake_acp_agent.py)
launched through the binary override, exactly as the real ``agy`` would be.
"""

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
    assert ag.unattended_reason(_options(turn_source="automation")) == "a scheduled or automatic turn"
    assert ag.unattended_reason(_options(one_shot=True)) == "a background call"
    assert ag.unattended_reason(_options(assistant_proactive="x")) == "a background call"
    assert ag.unattended_reason(_options(session_id=None)) == "a background call"
    assert ag.unattended_reason(_options(session_id="missing")) == "a background call"
    started["s1"] = {"id": "t1"}
    assert ag.unattended_reason(_options()) == "a session another agent started"


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


def test_adapter_refuses_an_unattended_turn_on_a_guarded_row():
    from app.core.environment import ProviderConfigError
    from app.core.provider.harness_adapter import HarnessAdapter

    adapter = HarnessAdapter.__new__(HarnessAdapter)
    adapter._antigravity_row = {"auth_mode": "oauth-personal"}
    adapter._next_app_turn_source = "automation"
    with pytest.raises(ProviderConfigError, match="scheduled or automatic"):
        adapter._check_antigravity_turn()
    # The source names one submit only: the next owner turn goes through.
    adapter._check_antigravity_turn()
    adapter._next_app_turn_source = "user"
    adapter._check_antigravity_turn()
    adapter._antigravity_row = {"auth_mode": "gemini-api-key"}
    adapter._next_app_turn_source = "automation"
    adapter._check_antigravity_turn()


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
    assert client.get(f"{BASE}/providers/{row['id']}/status").json()["state"] == "signed_out"
    started = client.post(f"{BASE}/providers/{row['id']}/login").json()
    assert started["state"] == "awaiting_code", started
    assert started["url"].startswith("https://accounts.google.com/")
    bad = client.post(f"{BASE}/providers/{row['id']}/login/{started['login_id']}/complete",
                      json={"code": "wrong"}).json()
    assert bad["state"] == "failed"
    # The agent's verdict reaches the owner; its own prompts do not.
    assert "invalid_grant" in bad["message"]
    assert "authorization code" not in bad["message"] and "visit" not in bad["message"]
    started = client.post(f"{BASE}/providers/{row['id']}/login").json()
    done = client.post(f"{BASE}/providers/{row['id']}/login/{started['login_id']}/complete",
                       json={"code": "good"}).json()
    assert done["state"] == "signed_in", done
    assert client.get(f"{BASE}/providers/{row['id']}/status").json()["state"] == "signed_in"
    assert client.post(f"{BASE}/providers/{row['id']}/sign-out").json() == {"state": "signed_out"}
    assert client.get(f"{BASE}/providers/{row['id']}/status").json()["state"] == "signed_out"


def test_business_sign_in_has_no_headless_flow(client):
    row = _create(client, "oauth-business")
    body = client.post(f"{BASE}/providers/{row['id']}/login").json()
    assert body["state"] == "failed" and "browser" in body["message"]


def test_not_installed_is_reported(client, monkeypatch):
    row = _create(client)
    monkeypatch.setenv(ag.BINARY_ENV, "/nonexistent/agy")
    assert client.get(f"{BASE}/providers/{row['id']}/status").json()["state"] == "not_installed"
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
    monkeypatch.setattr(db, "upsert_provider_usage_snapshot", lambda *a: writes.append(("ok", a)))
    monkeypatch.setattr(db, "record_provider_usage_failure", lambda *a: writes.append(("fail", a)))
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
