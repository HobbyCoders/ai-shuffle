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

def _options(session_id="s1", turn_source="user", person_entered=None, **extra):
    """Run options as ``query_options`` builds them. By default an owner source
    was entered by a person (the runtime's ``turn_provenance``) and any other
    was not."""
    if person_entered is None:
        person_entered = turn_source in ag.OWNER_UI_TURN_SOURCES
    return SimpleNamespace(extra={"app_session_id": session_id, **extra}, aih_turn_source=turn_source,
                           aih_turn_person_entered=person_entered)


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
    assert ag.unattended_reason(_options(turn_source="worktree")) is None


@pytest.mark.parametrize("source,reason", [
    # Person-present elsewhere in the app, but not the owner in the app's UI.
    ("api", "a request through the API"),
    ("api_stream", "a request through the API"),
    ("connector", "a message from a chat connector"),
    ("provider_agent", "a Task or Swarm worker"),
    ("failover", "an automatic retry on another account"),
    ("watch", "a watch"),
    ("held_peer_promotion", "a scheduled or automatic turn"),
    ("some_future_source", "a scheduled or automatic turn"),
    (None, "a run with no known origin"),
    ("", "a run with no known origin"),
])
def test_only_the_owner_in_the_apps_chat_passes(session_row, source, reason):
    assert ag.unattended_reason(_options(turn_source=source)) == reason


class _TurnRows:
    """``turns`` reads for a Retry: the live row and the chat's earlier rows."""

    def __init__(self, monkeypatch, live, earlier):
        from app.db import turns
        self.earlier = earlier
        monkeypatch.setattr(turns, "get_live_turn", lambda sid: live)
        test = self

        class _Cursor:
            def execute(self, sql, params):
                assert "started_at IS NOT NULL" in sql and params[1] == live.get("id")

            def fetchall(self):
                return test.earlier

        class _Conn:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self):
                return _Cursor()

        monkeypatch.setattr(turns, "get_db", lambda: _Conn())


_TYPED = {"person_entered": True}  # what turns.enqueue_turn writes for the owner's own input


@pytest.mark.parametrize("earlier,reason", [
    ([{"id": "t1", "source": "user", "payload": _TYPED}], None),
    ([{"id": "t2", "source": "resume"}, {"id": "t1", "source": "queue", "payload": _TYPED}], None),
    # An owner source without the host's positive key is not a person.
    ([{"id": "t1", "source": "user"}], ag.NOT_ENTERED_REASON),
    ([{"id": "t1", "source": "user", "payload": {"voice_delegation": True}}], ag.VOICE_REASON),
    ([{"id": "t1", "source": "api"}], "a request through the API"),
    ([{"id": "t1", "source": "wakeup"}], "a scheduled wake-up"),
    ([{"id": "t1", "source": "agent_message"}], "a message from another agent"),
    ([{"id": "t2", "source": "resume", "payload": {"api_user_id": "k"}}, {"id": "t1", "source": "user"}],
     "a request through the API"),
    ([], "a retry whose original turn could not be checked"),
])
def test_a_retry_keeps_the_origin_of_the_turn_it_reruns(session_row, monkeypatch, earlier, reason):
    # The owner pressed this Retry (its own row carries the key the resume
    # route writes from ``session_modes.person_entry``).
    _TurnRows(monkeypatch, {"id": "t3", "source": "resume", "payload": _TYPED}, earlier)
    assert ag.unattended_reason(_options(turn_source="resume")) == reason


@pytest.mark.parametrize("retry_payload,reason", [
    ({"person_entry_preview": True}, ag.PREVIEW_REASON),
    ({}, ag.RETRY_NOT_OWNER_REASON),
    ({"person_entered": "yes"}, ag.RETRY_NOT_OWNER_REASON),
])
def test_a_retry_the_owner_did_not_ask_for_is_refused_whatever_it_reruns(
        session_row, monkeypatch, retry_payload, reason):
    # An agent pressing Retry through its preview link re-runs the owner's own
    # typed turn: the Retry is still an agent run.
    _TurnRows(monkeypatch, {"id": "t3", "source": "resume", "payload": retry_payload},
              [{"id": "t1", "source": "user", "payload": _TYPED}])
    assert ag.unattended_reason(_options(turn_source="resume")) == reason


def test_a_retry_sent_with_an_api_key_is_a_request_through_the_api(session_row, monkeypatch):
    _TurnRows(monkeypatch, {"id": "t3", "source": "resume", "payload": {"api_user_id": "k"}},
              [{"id": "t1", "source": "user"}])
    assert ag.unattended_reason(_options(turn_source="resume")) == "a request through the API"


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
    adapter._next_turn_person_entered = False
    return adapter


async def _turn(adapter, source, entered=None):
    # As the query engine hands a turn over: the source, then who entered it.
    adapter.set_turn_source(source)
    adapter.set_turn_person_entered(source in ag.OWNER_UI_TURN_SOURCES if entered is None else entered)
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
    with pytest.raises(ProviderTurnRefused, match="refused for a run with no known origin"):
        await _turn(adapter, None)  # the source names one submit only; none is refused
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


@pytest.mark.parametrize("source,entered,refused", [
    ("user", True, False), ("queue", True, False), ("worktree", True, False),
    ("user", False, True),  # an answer Away mode gave: an owner source no person entered
    ("api", False, True), (None, False, True),
])
async def test_the_connect_check_uses_the_source_of_the_turn_it_serves(
        client, session_row, monkeypatch, source, entered, refused):
    # A new chat's first turn connects before it submits. SessionOptions has
    # no turn source, so the one ``connect_with_sdk_options`` was handed must
    # reach the connect-time guard: the owner's first typed turn runs on a
    # guarded row, an API request does not, and the value is used once.
    from app.core.environment import ProviderConfigError, ProviderTurnRefused
    from app.core.provider.harness_adapter import HarnessAdapter

    row = _create(client)
    adapter = HarnessAdapter.__new__(HarnessAdapter)
    seen = {}

    async def connect(opts):
        seen["source"] = adapter._connect_app_turn_source
        seen["entered"] = adapter._connect_turn_person_entered

    async def nothing(*_args, **_kwargs):
        return None

    monkeypatch.setattr(adapter, "connect", connect, raising=False)
    monkeypatch.setattr(adapter, "prepare_background_completions", nothing, raising=False)
    monkeypatch.setattr(adapter, "reconcile_user_questions", nothing, raising=False)
    try:
        await adapter.connect_with_sdk_options(SimpleNamespace(
            model="gemini-3.1-pro", aih_turn_source=source, aih_turn_person_entered=entered))
    except Exception:  # noqa: BLE001 — only the hand-over before connect is under test
        pass
    assert seen == {"source": source, "entered": entered}

    def no_binary(*_args, **_kwargs):
        raise FileNotFoundError("no agy here")  # past the guard: the next step

    monkeypatch.setattr(ag, "build_spec", no_binary)
    options = SimpleNamespace(extra={"provider_id": row["id"], "app_session_id": "s1"}, add_dirs=[])
    adapter._connect_app_turn_source = source
    adapter._connect_turn_person_entered = entered
    expected = ProviderTurnRefused if refused else ProviderConfigError
    with pytest.raises(expected) as caught:
        await adapter._connect_antigravity(options, None, None, None, None, False)
    if refused:
        assert "refused for" in str(caught.value)
    else:
        assert "no agy here" in str(caught.value)
    assert adapter._connect_app_turn_source is None
    assert adapter._connect_turn_person_entered is False
    # A later bare connect has no source of its own: refused, not reused.
    if not refused:
        with pytest.raises(ProviderTurnRefused, match="no known origin"):
            await adapter._connect_antigravity(options, None, None, None, None, False)


# --- provenance through the real enqueue and turn start -----------------------

@pytest.fixture
def guarded_chat(client):
    """A real chat on a guarded personal sign-in row, and an adapter as a
    connected chat leaves it. Rows go through ``enqueue_turn`` and
    ``start_turn`` as the pump runs them; each turn's provenance is what
    ``SessionRuntime._run_pump`` hands over (``turn_provenance``)."""
    from app.db import sessions

    row = _create(client)
    sessions.create_session(session_id="chat-g", profile_id="default", title="Garden plan")
    return row, _connected(row["id"], session_id="chat-g")


def _start(turn_id, parked_resume=False):
    from app.core.session_runtime import SessionRuntime
    from app.db import turns

    return turns.start_turn(turn_id, can_resume=SessionRuntime._can_resume_parked if parked_resume else None)


async def _run(adapter, started):
    """Hand ``started`` to the leg as the pump and the query engine do."""
    from app.core.turn_scheduler import turn_provenance

    source, entered = turn_provenance(started)
    await _turn(adapter, source, entered)


def _owner_turn_that_parks(prompt="Plan the beds"):
    from app.db import turns

    first = turns.enqueue_turn("chat-g", "user", prompt=prompt, profile_id="default", person_entered=True)
    first = _start(first["id"])
    assert turns.park_turn(first["id"], ["monitor"])
    return first


def _answer(first, human_input, person_entered=None):
    """The question producer Away mode and the owner's own answer share. The
    owner's own answer arrives with the socket's ``person_entry`` (True);
    Away's states nothing."""
    from app.db import interactions, turns

    if person_entered is None:
        person_entered = human_input
    question = interactions.create_interaction("chat-g", "question", {"request_id": "q1"}, None)
    assert interactions.answer_question_and_enqueue(
        question, {"Raised beds?": "Yes"}, None, "Raised beds: Yes", first, human_input=human_input,
        person_entered=person_entered)
    [answer] = turns.get_queued_turns("chat-g")
    return answer


@pytest.mark.parametrize("parked", [True, False])
async def test_an_answer_away_mode_gives_is_refused_and_the_owners_own_passes(guarded_chat, parked):
    # Away mode's answer is admitted with source "user"; the row records that
    # no person gave it, and the guard reads that, parked resume or not.
    from app.core.environment import ProviderTurnRefused
    from app.db import turns

    _, adapter = guarded_chat
    first = _owner_turn_that_parks()
    await _run(adapter, first)  # the owner's first message
    if not parked:
        turns.settle_turn(first["id"], "completed")
    away = _start(_answer(first, human_input=False)["id"], parked_resume=parked)
    assert away["source"] == "user" and (away["id"] == first["id"]) is parked
    with pytest.raises(ProviderTurnRefused, match="refused for a reply sent for you automatically"):
        await _run(adapter, away)
    turns.settle_turn(away["id"], "completed")

    second = _owner_turn_that_parks("And the paths?")
    if not parked:
        turns.settle_turn(second["id"], "completed")
    mine = _start(_answer(second, human_input=True)["id"], parked_resume=parked)
    await _run(adapter, mine)  # the owner answering themselves


async def test_a_retry_replays_the_exact_row_it_reruns(guarded_chat, client):
    # A parked owner turn resumed automatically ("unpark") while unattended use
    # was allowed fails; the owner restores the guard and presses Retry. The
    # Retry re-runs that row, which no person entered: refused, though the
    # turn began as the owner's ("user" by ``logical_source``).
    from app.core.environment import ProviderTurnRefused
    from app.db import turns

    row, adapter = guarded_chat
    client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": True})
    first = _owner_turn_that_parks()
    wake = turns.enqueue_turn("chat-g", "unpark", prompt="The monitor finished", profile_id="default")
    resumed = _start(wake["id"], parked_resume=True)
    assert resumed["id"] == first["id"] and resumed["source"] == "unpark"
    assert turns.logical_source(resumed) == "user"  # the accounting origin, not who entered it
    await _run(adapter, resumed)  # allowed while lifted
    turns.settle_turn(resumed["id"], "aborted", error="the agent stopped")
    client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": False})

    retry = _start(turns.enqueue_turn("chat-g", "resume", profile_id="default", person_entered=True)["id"])
    with pytest.raises(ProviderTurnRefused, match="refused for a background turn picked up again"):
        await _run(adapter, retry)
    turns.settle_turn(retry["id"], "aborted", error="refused")

    # A Retry of the owner's own typed turn passes, and a Retry of that Retry.
    typed = _start(turns.enqueue_turn("chat-g", "user", prompt="Try again", profile_id="default",
                                      person_entered=True)["id"])
    turns.settle_turn(typed["id"], "aborted", error="the agent stopped")
    for _ in range(2):
        retry = _start(turns.enqueue_turn("chat-g", "resume", profile_id="default", person_entered=True)["id"])
        await _run(adapter, retry)
        turns.settle_turn(retry["id"], "aborted", error="the agent stopped")

    # And a Retry of an Away answer is refused like the answer itself.
    away = _start(_answer(_owner_turn_that_parks("Water?"), human_input=False)["id"], parked_resume=True)
    turns.settle_turn(away["id"], "aborted", error="the agent stopped")
    retry = _start(turns.enqueue_turn("chat-g", "resume", profile_id="default", person_entered=True)["id"])
    with pytest.raises(ProviderTurnRefused, match="refused for a reply sent for you automatically"):
        await _run(adapter, retry)


@pytest.mark.parametrize("human_input", [False, True])
async def test_a_compaction_written_for_an_answer_keeps_who_entered_it(guarded_chat, human_input):
    # A compact-first send, and a compaction queued again before its
    # continuation, are admitted with the answer's own source: who entered
    # the answer travels with it, so an Away answer's compaction is refused
    # on the row like the answer.
    from app.core.environment import ProviderTurnRefused
    from app.db import turns

    _, adapter = guarded_chat
    answer = turns.enqueue_turn("chat-g", "user", prompt="Raised beds: Yes", profile_id="default",
                                payload={"compact_first": True, "question_answer_human_input": human_input},
                                person_entered=human_input)
    compact = turns.get_turn(answer["payload"]["compact_before_turn_id"])
    again = turns.retry_pair_compaction(answer["id"], attempt=2)
    for row in (compact, again):
        assert row["source"] == "user" and row["prompt"] == "/compact"
        started = dict(row, state="running")
        if human_input:
            await _run(adapter, started)
        else:
            with pytest.raises(ProviderTurnRefused, match="refused for a reply sent for you automatically"):
                await _run(adapter, started)


async def test_only_the_hosts_own_admission_marks_a_person(guarded_chat):
    # "A person entered it" is a positive key only enqueue_turn's
    # ``person_entered`` writes. The voice assistant's own request (source
    # "user") and a payload that claims the key are not a person.
    from app.core.environment import ProviderTurnRefused
    from app.db import turns

    _, adapter = guarded_chat
    voice = turns.enqueue_turn("chat-g", "user", prompt="Follow up on the build", profile_id="default",
                               payload={"voice_delegation": True})
    with pytest.raises(ProviderTurnRefused, match="refused for a request written by the voice assistant"):
        await _run(adapter, _start(voice["id"]))
    turns.settle_turn(voice["id"], "aborted", error="refused")

    forged = turns.enqueue_turn("chat-g", "user", prompt="hi", profile_id="default",
                                payload={"person_entered": True})
    assert "person_entered" not in forged["payload"]
    with pytest.raises(ProviderTurnRefused, match="refused for a reply sent for you automatically"):
        await _run(adapter, _start(forged["id"]))
    turns.settle_turn(forged["id"], "aborted", error="refused")

    # Only an owner source carries it.
    wake = turns.enqueue_turn("chat-g", "wakeup", prompt="tick", profile_id="default", person_entered=True)
    assert "person_entered" not in wake["payload"]
    turns.settle_turn(_start(wake["id"])["id"], "aborted", error="skipped")

    typed = turns.enqueue_turn("chat-g", "user", prompt="hi", profile_id="default", person_entered=True)
    assert typed["payload"]["person_entered"] is True
    await _run(adapter, _start(typed["id"]))


def test_only_the_three_admission_points_mark_a_person():
    # The composer send (sent, queued or steering: chat socket, its HTTP
    # transport and its forward to the owner worker), a worktree chat's
    # opening prompt, the owner's own question-card answer, and the Retry
    # route's statement of who pressed it. Each passes the value of ONE
    # decision (``session_modes.person_entry``, from the principal the host
    # authenticated); none writes a literal True. Everything else carries a
    # row's own value or passes nothing.
    import re

    import app as app_pkg

    root = Path(app_pkg.__file__).parent
    literal, keyed, decided, admitted = {}, [], {}, {}
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        name = path.relative_to(root.parent).as_posix()
        if count := len(re.findall(r"person_entered\s*(?::\s*\w+\s*)?=\s*True\b", text)):
            literal[name] = count
        if re.search(r"PERSON_ENTERED_KEY\]\s*=\s*True|[\"']person_entered[\"']\s*:\s*True", text):
            keyed.append(name)
        if name != "app/core/session_modes.py" and (count := len(re.findall(r"\bperson_entry\(", text))):
            decided[name] = count
        if name.startswith("app/api/"):
            for value in re.findall(r"\bperson_entered=([^,)\n]+)", text):
                admitted.setdefault(name, set()).add(value.strip())
    assert literal == {}, literal
    assert keyed == ["app/db/turns.py"], keyed  # entry_payload, behind enqueue_turn's argument
    # The decision is made in one place; the socket and REST read it from there.
    assert decided == {"app/api/chat_ws_handler.py": 1, "app/api/chat_query.py": 1}, decided
    assert admitted == {
        "app/api/chat_ws_handler.py": {"self._person_entry"},
        "app/api/sessions_worktree.py": {"entry"},
        "app/api/sessions.py": {"await run_sync(request_person_entry"},
    }, admitted
    modes = (root / "core" / "session_modes.py").read_text(encoding="utf-8")
    assert ('return caller == CALLER_AUTHENTICATED and (principal or {}).get("kind") in _OWNER_KINDS'
            in modes)
    assert '_OWNER_KINDS = frozenset({"admin", "paired"})' in modes
    interactions = (root / "db" / "interactions.py").read_text(encoding="utf-8")
    assert "person_entered=person_entered if human_input is True else False" in interactions
    # The voice assistant creates branch chats through the same intake, but
    # with no opening prompt (the intake submits only ``if prompt``): every
    # request it writes goes through the voice bridges, which mark nothing.
    voice = (root / "api" / "voice.py").read_text(encoding="utf-8")
    branch_request = voice.split("branch_request = CreateWorktreeSessionRequest(", 1)[1].split(")", 1)[0]
    assert "prompt" not in branch_request, branch_request
    intake = (root / "api" / "sessions_worktree.py").read_text(encoding="utf-8")
    assert "    if prompt:\n        await _persist_and_broadcast_first_message" in intake


@pytest.mark.parametrize("stated,reason", [
    (True, None),
    (False, ag.NOT_ENTERED_REASON),
    ("missing", ag.OUTDATED_WORKER_REASON),  # a worker from before the key: "update it"
    ("preview", ag.PREVIEW_REASON),  # sent through the agent's preview link there
])
async def test_a_forwarded_turn_carries_what_its_worker_stated(guarded_chat, monkeypatch, stated, reason):
    # The peer channel is worker-authenticated; the sending worker states
    # who entered the text outside the payload, which never carries it.
    import re

    from app.core.environment import ProviderTurnRefused
    from app.db import database
    from app.network import control

    _, adapter = guarded_chat
    monkeypatch.setattr("app.core.turn_manager._kick", lambda session_id: None)
    monkeypatch.setattr(control, "_require_owned", lambda sid: database.get_session(sid))
    body = {"session_id": "chat-g", "source": "user", "prompt": "hi", "payload": {"person_entered": True}}
    if stated == "preview":
        body.update(person_entered=False, caller="preview")
    elif stated != "missing":
        body["person_entered"] = stated
    result = await control.handle("turns", body, from_device="paired-peer")
    started = _start(result["turn_id"])
    if reason is None:
        await _run(adapter, started)
    else:
        with pytest.raises(ProviderTurnRefused, match=re.escape(f"refused for {reason}")):
            await _run(adapter, started)


async def test_submit_turn_always_states_who_entered_it(monkeypatch):
    from app.network import control

    sent = []

    async def call(device_id, action, body):
        sent.append(body)
        return {"turn_id": "t"}

    monkeypatch.setattr(control, "call", call)
    await control.submit_turn("dev", "s", prompt="hi")
    await control.submit_turn("dev", "s", prompt="hi", person_entered=True)
    # A preview-link send states no person (its reason travels as the caller).
    await control.submit_turn("dev", "s", prompt="hi", person_entered="preview", caller="preview")
    assert [b["person_entered"] for b in sent] == [False, True, False]
    assert sent[2]["caller"] == "preview"


@pytest.mark.parametrize("source,payload,joins", [
    ("user", {"person_entered": True}, True),                   # the owner typing
    ("queue", {"person_entered": True}, True),                  # the owner's queued message
    ("user", {"question_answer_human_input": True, "person_entered": True}, True),  # their own answer
    ("user", {"question_answer_human_input": False}, False),    # Away mode's answer
    ("user", {"voice_delegation": True}, False),                # the voice assistant's own request
    ("user", {}, False),                                        # no key: not a person
    ("unpark", {"person_entered": True}, False),                # the key counts only on an owner source
])
def test_text_no_person_entered_never_joins_a_guarded_turn(client, monkeypatch, source, payload, joins):
    from app.core import session_runtime
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.db import sessions, turns

    sessions.create_session(session_id="chat-s", profile_id=None, title="Steer")
    live = turns.start_turn(turns.enqueue_turn("chat-s", "user", prompt="Plan the beds")["id"])
    runtime = session_runtime.SessionRuntime(live)
    adapter = HarnessAdapter.__new__(HarnessAdapter)
    adapter._antigravity_binding = None
    adapter._options = None
    monkeypatch.setattr(runtime, "_live_adapter", lambda: adapter)
    refusal = runtime._steer_refusal
    row = {"id": "t-steer", "source": source, "prompt": "hi", "payload": payload, "profile_id": None,
           "overrides": {}}
    # Another leg: the rule does not apply (the later binding checks may still).
    assert refusal(row) != _NOT_ENTERED_STEER
    adapter = _connected(_create(client)["id"], "chat-s")  # a guarded personal sign-in
    assert (refusal(row) == _NOT_ENTERED_STEER) is not joins


_NOT_ENTERED_STEER = "text no person entered runs as its own turn here"


def _voice_follow_up(session_id):
    from app.db import turns

    return turns.enqueue_turn(session_id, "user", prompt="and the roses too",
                              payload={"voice_delegation": True})


@pytest.mark.parametrize("mode,fields,allow,steers", [
    ("oauth-personal", {}, False, False),                                # guarded
    ("oauth-personal", {}, True, True),                                  # unattended use allowed
    ("oauth-business", {}, None, True),                                  # business sign-in
    ("gemini-api-key", {"api_key": "test-gemini-secret-value"}, None, True),  # API key
])
def test_text_no_person_entered_steers_unless_the_row_is_guarded(client, mode, fields, allow, steers):
    # S14: the restriction is the guard's, not the leg's. A voice follow-up
    # joins a running turn on every unguarded Antigravity row, as on any
    # other provider.
    from app.core import session_runtime
    from app.db import sessions, turns

    row = _create(client, mode, **fields)
    if allow:
        client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": True})
    sessions.create_session(session_id="chat-v", profile_id=None, title="Voice")
    live = turns.start_turn(turns.enqueue_turn("chat-v", "user", prompt="Plan the beds")["id"])
    runtime = session_runtime.SessionRuntime(live)
    adapter = _connected(row["id"], "chat-v")
    runtime._live_adapter = lambda: adapter
    follow_up = _voice_follow_up("chat-v")
    assert (runtime._steer_refusal(follow_up) == _NOT_ENTERED_STEER) is not steers
    assert runtime._steering_guarded_now() is not steers


def test_the_steer_rule_reads_the_guard_fresh_at_each_decision(client):
    # Restored mid-turn: the next follow-up does not steer. Lifted mid-turn: it does.
    from app.core import session_runtime
    from app.db import sessions, turns

    row = _create(client)
    client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": True})
    sessions.create_session(session_id="chat-v", profile_id=None, title="Voice")
    live = turns.start_turn(turns.enqueue_turn("chat-v", "user", prompt="Plan the beds")["id"])
    runtime = session_runtime.SessionRuntime(live)
    adapter = _connected(row["id"], "chat-v")  # connected while lifted
    runtime._live_adapter = lambda: adapter
    follow_up = _voice_follow_up("chat-v")
    assert runtime._steer_refusal(follow_up) is None or runtime._steer_refusal(follow_up) != _NOT_ENTERED_STEER
    client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": False})  # restored
    assert runtime._steer_refusal(follow_up) == _NOT_ENTERED_STEER
    client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": True})  # lifted again
    assert runtime._steer_refusal(follow_up) != _NOT_ENTERED_STEER


def test_a_steer_that_cannot_read_the_row_does_not_steer(client, monkeypatch):
    # An unreadable row counts as guarded: the text waits for its own turn,
    # where the turn-start guard decides it. Other providers never read it.
    from app.core import session_runtime
    from app.db import model_providers, sessions, turns

    row = _create(client, "oauth-business")
    sessions.create_session(session_id="chat-v", profile_id=None, title="Voice")
    live = turns.start_turn(turns.enqueue_turn("chat-v", "user", prompt="Plan the beds")["id"])
    runtime = session_runtime.SessionRuntime(live)
    adapter = _connected(row["id"], "chat-v")
    runtime._live_adapter = lambda: adapter

    def broken(*_a, **_k):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(model_providers, "get_model_provider", broken)
    assert runtime._steer_refusal(_voice_follow_up("chat-v")) == _NOT_ENTERED_STEER
    adapter._antigravity_binding = None  # as any other provider's agent
    assert runtime._steer_refusal(_voice_follow_up("chat-v")) != _NOT_ENTERED_STEER


@pytest.mark.parametrize("allow", [False, True])
async def test_steer_pending_reads_the_guard_off_the_event_loop(client, monkeypatch, allow):
    # Through the real sweep: the row is read in a worker thread, once, and
    # only when a queued row lacks the key. Guarded, the follow-up waits for
    # its own turn; with unattended use allowed it steers like any provider's.
    import threading

    from app.core import antigravity, session_runtime
    from app.db import sessions, turns

    row = _create(client)
    if allow:
        client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": True})
    sessions.create_session(session_id="chat-v", profile_id=None, title="Voice")
    live = turns.start_turn(turns.enqueue_turn("chat-v", "user", prompt="Plan the beds")["id"])
    runtime = session_runtime.SessionRuntime(live)
    adapter = _connected(row["id"], "chat-v")
    steered = []

    async def steer(prompt, **_kw):
        steered.append(prompt)
    adapter.steer = steer
    monkeypatch.setattr(runtime, "_live_adapter", lambda: adapter)

    async def alive():
        return False
    monkeypatch.setattr(runtime, "_turn_is_dying", alive)
    reads = []
    real = antigravity.guarded_now

    def counted(provider_id):
        reads.append(threading.current_thread() is threading.main_thread())
        return real(provider_id)
    monkeypatch.setattr(antigravity, "guarded_now", counted)
    turns.enqueue_turn("chat-v", "queue", prompt="typed", person_entered=True)
    await runtime.steer_pending()
    assert reads == [] and steered == ["typed"]  # nobody needed the read
    _voice_follow_up("chat-v")
    await runtime.steer_pending()
    assert reads == [False]  # one read, in a worker thread
    assert (len(steered) == 2 and "and the roses too" in steered[1]) is allow


@pytest.mark.parametrize("kind,reason", [
    ("task_or_swarm_worker", "a Task or Swarm worker"),
    ("subagent", "a subagent"),
    ("agent_started", "a session another agent started"),
])
async def test_child_and_agent_started_chats_go_through_the_turn_check(client, session_row, kind, reason):
    # The per-turn check every submit and resume runs (``_check_antigravity_turn``),
    # with the owner's own turn source: the session decides, guard on and off.
    from app.core.environment import ProviderTurnRefused

    chat, started = session_row
    if kind == "task_or_swarm_worker":
        chat["is_worker"] = True
    elif kind == "subagent":
        chat["parent_session_id"] = "parent"
    else:
        started["s1"] = {"id": "t-agent"}
    row = _create(client)
    adapter = _connected(row["id"])
    with pytest.raises(ProviderTurnRefused, match=f"refused for {reason}"):
        await _turn(adapter, "user")
    client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": True})
    await _turn(adapter, "user")
    client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": False})
    with pytest.raises(ProviderTurnRefused, match=f"refused for {reason}"):
        await _turn(adapter, "user")


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


async def test_a_task_to_a_sign_in_row_follows_the_guard_and_the_sign_in_here(
        client, monkeypatch, tmp_path):
    """Task's real entry point: the credential pre-flight reads this worker's
    sign-in marker, and a guarded row is refused with the guard's reason."""
    from app.core import model_override, project_resolver, subagent_default
    from app.core import task_dispatch
    from app.core.agent_types import PermissionResultAllow
    from app.core.permission_handler import permission_handler
    from app.core.provider_agent import WorkerOutcome

    row = _create(client)
    db.create_profile("caller", "Caller", None, {"model": "opus"})
    db.create_profile("helper", "Helper", None, {"model": "opus"})
    db.create_session(session_id="parent", profile_id="caller", title="Parent")
    monkeypatch.setattr(model_override, "_APPROVED", set())
    monkeypatch.setattr(project_resolver, "resolve_existing_cwd", lambda _row: str(tmp_path))
    subagent_default.write("sonnet")
    started = []

    async def worker(**kwargs):
        started.append(kwargs["provider_id"])
        return WorkerOutcome(ok=True, session_id="worker", report="done")

    monkeypatch.setattr("app.core.provider_agent.dispatch_worker", worker)

    async def allow(**_kwargs):  # the owner approves the model pick each time
        return PermissionResultAllow(updated_input={})

    monkeypatch.setattr(permission_handler, "request_permission", allow)

    async def task():
        return await task_dispatch.dispatch_task_tool(
            {"prompt": "go", "mode": "sync", "agent": "helper",
             "model": f"prov:{row['id']}:gemini-3.1-pro"},
            caller_session_id="parent", caller_provider_id=None)

    def guard(allow):
        client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": allow})

    ag._note_signin(row["id"], "signed_in")
    refused = await task()
    assert not refused["ok"] and not started
    assert ag.GUARD_REFUSAL.format(reason=ag.AGENT_RUN_REASON) in refused["error"]
    guard(True)
    ran = await task()
    assert ran["ok"], ran
    assert started == [row["id"]]
    guard(False)
    again = await task()
    assert not again["ok"] and "Antigravity provider uses a personal Google sign-in" in again["error"]
    guard(True)
    ag._note_signin(row["id"], "signed_out")  # signed in on another worker or nowhere: not here
    signed_out = await task()
    assert not signed_out["ok"] and "not signed in to Google on this worker" in signed_out["error"]
    assert started == [row["id"]]


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


# --- install and update ---------------------------------------------------------

@pytest.mark.parametrize("system,machine,key", [
    ("linux", "x86_64", "linux_amd64"),
    ("linux", "aarch64", "linux_arm64"),
    ("win32", "AMD64", "windows_amd64"),
    ("win32", "ARM64", "windows_arm64"),
    ("darwin", "x86_64", "darwin_amd64"),
    ("darwin", "arm64", "darwin_arm64"),
    ("linux", "riscv64", None),
    ("win32", "x86", None),
    ("freebsd14", "amd64", None),
])
def test_every_platform_google_publishes_has_its_manifest(system, machine, key):
    assert ag.manifest_key(system, machine) == key
    if key:
        assert ag.manifest_url(key).endswith(f"/manifests/{key}.json")


def test_a_platform_google_does_not_publish_is_refused_by_name(monkeypatch):
    monkeypatch.setattr(ag, "_platform", lambda: ("linux", "riscv64"))
    monkeypatch.setattr(ag, "_install", ag.InstallJob())
    monkeypatch.setattr(ag, "_install_task", None)
    state = ag.start_install()
    assert state["state"] == "failed" and state["kind"] == "install"
    assert "(linux riscv64)" in state["message"] and state["error"] == state["message"]
    assert "Linux x64 only" not in state["message"]


@pytest.fixture
def agy_root(tmp_path, monkeypatch):
    """A private data dir with no override and no agy on PATH."""
    root = tmp_path / "antigravity"
    monkeypatch.delenv(ag.BINARY_ENV, raising=False)
    monkeypatch.setattr(ag, "_root", lambda: root)
    monkeypatch.setattr(ag.shutil, "which", lambda name: None)
    monkeypatch.setattr(ag, "_install", ag.InstallJob())
    monkeypatch.setattr(ag, "_install_task", None)
    return root


def _google(monkeypatch, *, version, url, payload=b"", digest=None):
    """Answer Google's manifest and download; returns the URLs requested."""
    import hashlib

    import httpx

    asked = []

    class _Response:
        def __init__(self, body=None):
            self.body = body

        def raise_for_status(self):
            return self

        def json(self):
            return self.body

        async def aiter_bytes(self, size):
            for i in range(0, len(payload), 7):
                yield payload[i:i + 7]

    class _Stream:
        async def __aenter__(self):
            return _Response()

        async def __aexit__(self, *exc):
            return False

    async def get(self, url, **kw):
        asked.append(url)
        return _Response({"version": version, "url": url_,
                          "sha512": digest or hashlib.sha512(payload).hexdigest()})

    def stream(self, method, url, **kw):
        asked.append(url)
        return _Stream()
    url_ = url
    monkeypatch.setattr(httpx.AsyncClient, "get", get)
    monkeypatch.setattr(httpx.AsyncClient, "stream", stream)
    return asked


def _versions(monkeypatch, table):
    async def version(binary):
        return table.get(str(binary))
    monkeypatch.setattr(ag, "binary_version", version)


def _tarball(name="antigravity", body=b"#!/bin/sh\necho agy\n"):
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(f"antigravity-cli/{name}")
        info.size = len(body)
        tar.addfile(info, io.BytesIO(body))
    return buf.getvalue()


async def _run_job(kind="install"):
    state = ag.start_install(kind)
    assert state["state"] == "running" and state["kind"] == kind
    await ag._install_task
    return ag.install_state()


async def test_windows_installs_the_exe_itself(agy_root, monkeypatch):
    monkeypatch.setattr(ag, "_platform", lambda: ("win32", "AMD64"))
    exe = b"MZ fake windows agy"
    url = "https://storage.googleapis.com/antigravity/windows-x64/cli_windows_x64.exe"
    asked = _google(monkeypatch, version="1.5.7", url=url, payload=exe)
    state = await _run_job()
    assert state == {"kind": "install", "state": "succeeded", "version": "1.5.7", "error": None,
                     "message": "Antigravity 1.5.7 installed."}
    assert asked == [ag.manifest_url("windows_amd64"), url]
    binary = agy_root / "cli" / "1.5.7" / "agy.exe"
    assert binary.read_bytes() == exe and not (binary.parent / "agy.exe.part").exists()
    assert (agy_root / "cli" / "current").read_text(encoding="utf-8") == str(binary)
    assert ag.locate_binary() == (str(binary), "app")


async def test_a_download_that_fails_its_checksum_leaves_nothing(agy_root, monkeypatch):
    monkeypatch.setattr(ag, "_platform", lambda: ("win32", "ARM64"))
    url = "https://storage.googleapis.com/antigravity/windows-arm/cli_windows_arm64.exe"
    _google(monkeypatch, version="1.5.7", url=url, payload=b"tampered", digest="00")
    state = await _run_job()
    assert state["state"] == "failed" and "checksum" in state["message"]
    assert list((agy_root / "cli" / "1.5.7").iterdir()) == []
    assert not (agy_root / "cli" / "current").exists()


@pytest.mark.parametrize("url,expected", [
    ("https://storage.googleapis.com/antigravity/cli.zip", "cannot install (cli.zip)"),
    ("https://example.com/antigravity/cli.tar.gz", "did not name a download"),
])
async def test_an_unknown_or_foreign_download_is_refused(agy_root, monkeypatch, url, expected):
    monkeypatch.setattr(ag, "_platform", lambda: ("linux", "x86_64"))
    _google(monkeypatch, version="1.5.7", url=url, payload=b"x")
    state = await _run_job()
    assert state["state"] == "failed" and expected in state["message"]


async def test_check_for_update_says_up_to_date_and_downloads_nothing(mock_db, fake_agy, monkeypatch):
    version = await ag.binary_version(str(fake_agy))
    assert version
    monkeypatch.setattr(ag, "_platform", lambda: ("linux", "x86_64"))
    monkeypatch.setattr(ag, "_install", ag.InstallJob())
    monkeypatch.setattr(ag, "_install_task", None)
    asked = _google(monkeypatch, version=version, url="https://storage.googleapis.com/fake/cli.tar.gz")
    state = await _run_job("update")
    assert state["state"] == "succeeded" and state["message"] == f"Antigravity is up to date ({version})."
    assert asked == [ag.manifest_url("linux_amd64")]  # the manifest only, no download


async def test_check_for_update_installs_the_newer_release_and_removes_the_old(agy_root, monkeypatch):
    monkeypatch.setattr(ag, "_platform", lambda: ("linux", "aarch64"))
    older = agy_root / "cli" / "1.5.5" / "agy"
    older.parent.mkdir(parents=True)
    older.write_text("older")
    old = agy_root / "cli" / "1.5.6" / "agy"
    old.parent.mkdir(parents=True)
    old.write_text("old")
    old.chmod(0o755)
    (agy_root / "cli" / "current").write_text(str(old), encoding="utf-8")
    new = agy_root / "cli" / "1.5.7" / "agy"
    _versions(monkeypatch, {str(old): "1.5.6"})
    _google(monkeypatch, version="1.5.7", url="https://storage.googleapis.com/a/cli_linux_arm64.tar.gz",
            payload=_tarball())
    state = await _run_job("update")
    assert state["state"] == "succeeded", state
    assert state["message"] == "Antigravity updated from 1.5.6 to 1.5.7."
    assert ag.locate_binary() == (str(new), "app")
    assert new.read_bytes() == b"#!/bin/sh\necho agy\n" and os.access(new, os.X_OK)
    assert not older.parent.exists()  # versions before the replaced one are gone
    # The copy just replaced stays until the next update: a chat that resolved
    # it a moment ago may still be starting it.
    assert old.read_text() == "old"


async def test_check_for_update_never_downgrades(agy_root, monkeypatch):
    monkeypatch.setattr(ag, "_platform", lambda: ("linux", "x86_64"))
    newer = agy_root / "cli" / "1.5.8" / "agy"
    newer.parent.mkdir(parents=True)
    newer.write_text("newer")
    newer.chmod(0o755)
    (agy_root / "cli" / "current").write_text(str(newer), encoding="utf-8")
    _versions(monkeypatch, {str(newer): "1.5.8"})
    asked = _google(monkeypatch, version="1.5.7", url="https://storage.googleapis.com/a/cli.tar.gz",
                    payload=_tarball())
    state = await _run_job("update")
    assert state["state"] == "succeeded" and state["message"] == "Antigravity is up to date (1.5.8)."
    assert asked == [ag.manifest_url("linux_amd64")]  # Google's older release is not downloaded
    assert ag.locate_binary() == (str(newer), "app")


async def test_an_update_keeps_the_copy_the_override_names(agy_root, monkeypatch):
    monkeypatch.setattr(ag, "_platform", lambda: ("linux", "x86_64"))
    pinned = agy_root / "cli" / "1.5.5" / "agy"
    pinned.parent.mkdir(parents=True)
    pinned.write_text("pinned")
    pinned.chmod(0o755)
    monkeypatch.setenv(ag.BINARY_ENV, str(pinned))
    _versions(monkeypatch, {str(pinned): "1.5.5"})
    _google(monkeypatch, version="1.5.7", url="https://storage.googleapis.com/a/cli.tar.gz",
            payload=_tarball())
    state = await _run_job("update")
    assert state["state"] == "succeeded", state
    assert pinned.read_text() == "pinned"  # never pruned from under the override
    assert ag.locate_binary() == (str(pinned), "override")


def test_spawn_binding_names_the_binary_the_spec_was_built_with(agy_root, monkeypatch):
    row = {"id": "row-1", "auth_mode": "gemini-api-key", "extra_env_json": {}}
    monkeypatch.setattr(ag, "row_api_key", lambda _row: "k")
    monkeypatch.setattr(ag, "find_binary", lambda: "/data/antigravity/cli/1.5.7/agy")
    resolved = "/data/antigravity/cli/1.5.6/agy"
    # An update between build_spec and spawn_binding must not split them.
    assert ag.spawn_binding(row, binary=resolved) != ag.spawn_binding(row)
    assert ag.spawn_binding(row, binary=resolved) == ag.spawn_binding(row, binary=resolved)


async def test_an_update_over_agy_on_path_says_the_apps_copy_now_runs(agy_root, monkeypatch, tmp_path):
    monkeypatch.setattr(ag, "_platform", lambda: ("linux", "x86_64"))
    own = tmp_path / "bin" / "agy"
    monkeypatch.setattr(ag.shutil, "which", lambda name: str(own) if name == "agy" else None)
    own.parent.mkdir()
    own.write_text("mine")
    assert ag.locate_binary() == (str(own), "path")
    _versions(monkeypatch, {str(own): "1.4.0"})
    _google(monkeypatch, version="1.5.7", url="https://storage.googleapis.com/a/cli.tar.gz",
            payload=_tarball("agy"))
    state = await _run_job("update")
    assert state["message"] == ("Antigravity updated from 1.4.0 to 1.5.7. "
                                "AI Shuffle's own copy now runs instead of the agy on PATH.")
    assert ag.locate_binary() == (str(agy_root / "cli" / "1.5.7" / "agy"), "app")
    assert own.read_text() == "mine"  # the user's own binary is never touched


def test_old_versions_in_use_are_kept_and_nothing_is_stopped(agy_root, monkeypatch):
    from pathlib import Path

    cli = agy_root / "cli"
    for version, name in (("1.5.5", "agy.exe"), ("1.5.6", "agy.exe"), ("1.5.7", "agy.exe")):
        (cli / version).mkdir(parents=True)
        (cli / version / name).write_text(version)
    (cli / "current").write_text(str(cli / "1.5.7" / "agy.exe"))
    running = cli / "1.5.5" / "agy.exe"
    real_unlink = Path.unlink

    def unlink(self, missing_ok=False):
        if self == running:  # Windows: a running agy.exe cannot be deleted
            raise PermissionError(13, "The process cannot access the file", str(self))
        return real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(Path, "unlink", unlink)
    assert ag.prune_versions(cli / "1.5.7") == ["1.5.6"]
    assert running.read_text() == "1.5.5"  # left whole for a later update
    assert (cli / "1.5.7" / "agy.exe").exists() and (cli / "current").exists()


@pytest.fixture
def startup_check(agy_root, monkeypatch):
    """An app-installed agy, an empty settings store, and a recording start."""
    from app.db import database

    binary = agy_root / "cli" / "1.5.6" / "agy"
    binary.parent.mkdir(parents=True)
    binary.write_text("agy")
    binary.chmod(0o755)
    (agy_root / "cli" / "current").write_text(str(binary), encoding="utf-8")
    store, started = {}, []
    monkeypatch.setattr(database, "get_system_setting", lambda key: store.get(key))
    monkeypatch.setattr(database, "set_system_setting", lambda key, value: store.__setitem__(key, value))
    monkeypatch.setattr(ag, "start_update", lambda: started.append(1))
    monkeypatch.setattr(ag, "_platform", lambda: ("win32", "AMD64"))
    monkeypatch.delenv("TESTING", raising=False)
    monkeypatch.delenv("AI_SHUFFLE_PREVIEW", raising=False)
    return store, started


def test_startup_checks_the_apps_own_install_at_most_every_ten_minutes(startup_check):
    store, started = startup_check
    assert ag.update_on_startup() is True and started == [1]
    assert ag.update_on_startup() is False and started == [1]  # throttled
    store[ag._setting_key()] = str(float(store[ag._setting_key()]) - ag.STARTUP_CHECK_SECONDS - 1)
    assert ag.update_on_startup() is True and started == [1, 1]


@pytest.mark.parametrize("case", ["testing", "preview", "override", "path", "unsupported"])
def test_startup_never_checks_tests_previews_or_a_binary_it_did_not_install(
        startup_check, monkeypatch, tmp_path, case):
    store, started = startup_check
    if case == "testing":
        monkeypatch.setenv("TESTING", "1")
    elif case == "preview":
        monkeypatch.setenv("AI_SHUFFLE_PREVIEW", "1")
    elif case == "override":
        override = tmp_path / "custom-agy"
        override.write_text("agy")
        monkeypatch.setenv(ag.BINARY_ENV, str(override))
    elif case == "path":
        (ag._root() / "cli" / "current").unlink()
        monkeypatch.setattr(ag.shutil, "which", lambda name: str(tmp_path / "agy"))
    else:
        monkeypatch.setattr(ag, "_platform", lambda: ("linux", "riscv64"))
    assert ag.update_on_startup() is False
    assert started == [] and store == {}


def test_check_for_update_through_the_api(client, monkeypatch):
    import time

    status = client.get(f"{BASE}/status").json()
    assert status["source"] == "override" and isinstance(status["can_install"], bool)
    monkeypatch.setattr(ag, "_platform", lambda: ("win32", "AMD64"))
    monkeypatch.setattr(ag, "_install", ag.InstallJob())
    monkeypatch.setattr(ag, "_install_task", None)
    _google(monkeypatch, version=status["version"],
            url="https://storage.googleapis.com/antigravity/windows-x64/cli_windows_x64.exe")
    started = client.post(f"{BASE}/update").json()
    assert started["kind"] == "update"
    deadline = time.monotonic() + 15
    while (job := client.get(f"{BASE}/status").json()["install"])["state"] == "running":
        assert time.monotonic() < deadline
        time.sleep(0.05)
    assert job == {"kind": "update", "state": "succeeded", "version": status["version"], "error": None,
                   "message": f"Antigravity is up to date ({status['version']})."}


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


@pytest.mark.parametrize("mode", ["oauth-personal", "oauth-business", "gemini-api-key"])
def test_usage_is_the_agents_own_reading_never_a_link_only_entry(mode):
    from app.core import usage_sources

    row = {"id": "row-1", "provider_type": "antigravity", "auth_mode": mode, "enabled": True,
           "base_url": ""}
    assert [s.id for s in usage_sources.polled_sources(row)] == ["antigravity_usage"]
    assert not any(link.applies(row) for link in usage_sources.USAGE_LINKS.values())
    assert usage_sources.usage_link_for(row) is None


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


async def test_a_generic_edit_keeps_a_guard_restored_after_its_read(mock_db, fake_agy, monkeypatch):
    # Worker A reads the row with the guard lifted; worker B restores it and
    # that update syncs in before A writes. A's edit (no expected_revision)
    # must keep the stored value at write time, not the one it read.
    from app.api import model_providers as providers_api

    row = await _personal_row()
    await api.set_guard(row["id"], api.GuardUpdate(allow_unattended=True), token=None)
    real_update = db.update_model_provider
    restored = []

    def restore_then_write(provider_id, **kw):
        # The synced restore commits after every read the edit made.
        if not restored:
            restored.append(1)
            env = dict(db.get_model_provider(provider_id).get("extra_env_json") or {})
            env.pop(ag.GUARD_OVERRIDE_KEY, None)
            real_update(provider_id, extra_env_json=env)
        return real_update(provider_id, **kw)

    monkeypatch.setattr(providers_api.db, "update_model_provider", restore_then_write)
    await providers_api.update_provider(row["id"], providers_api.ModelProviderUpdate(
        extra_env={"OTHER_SETTING": "1", ag.GUARD_OVERRIDE_KEY: "1"}), token=None)
    monkeypatch.setattr(providers_api.db, "update_model_provider", real_update)
    assert restored
    stored = db.get_model_provider(row["id"])
    assert stored["extra_env_json"] == {"OTHER_SETTING": "1"}
    assert ag.guarded(stored)


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


@pytest.mark.parametrize("unparsed", [False, True], ids=["reading", "unavailable"])
async def test_a_sign_out_during_the_write_leaves_no_reading(mock_db, fake_agy, monkeypatch, unparsed):
    """The sign-out's own clear runs while the reading or failure is being
    written: whichever lands, nothing is left behind afterwards."""
    from app.db import provider_usage_snapshots as snapshots

    row = await _personal_row()
    name = "record_provider_usage_failure" if unparsed else "upsert_provider_usage_snapshot"
    real = getattr(db, name)

    def sign_out_lands(*args, **kwargs):
        ag._signin_epochs[row["id"]] = ag.signin_epoch(row["id"]) + 1  # what forget_row does first
        snapshots.delete_provider_usage_snapshot(row["id"])  # and its clear, before our write
        return real(*args, **kwargs)
    monkeypatch.setattr(db, name, sign_out_lands)

    async def nothing():
        return None
    assert await _poll_with(monkeypatch, row, nothing, unparsed) is None


@pytest.mark.parametrize("mode", ["oauth-personal", "oauth-business"])
async def test_a_sign_in_reading_is_never_shared_between_workers(mock_db, fake_agy, monkeypatch, mode):
    """Each worker has its own Google sign-in: a peer's reading is never
    imported here and this worker's is never published."""
    from app.core import provider_observations as observations
    from app.core.usage_sources import SOURCES
    from app.network import engine

    monkeypatch.setattr(engine, "cached_context", lambda: SimpleNamespace(network_key=b"k" * 32))
    monkeypatch.setattr(encryption, "_encryption_key", encryption.Fernet.generate_key())
    source = SOURCES["antigravity_usage"]
    row = await _personal_row()
    db.update_model_provider(row["id"], auth_mode=mode)
    assert observations._binding(source, row) is None
    polled = []

    async def poll(r):
        polled.append(r["id"])
    monkeypatch.setattr(observations, "_publish", lambda *a: pytest.fail("published"))
    await observations.poll_shared(source, row, poll)
    assert polled == [row["id"]]
    key_row = db.create_model_provider(provider_id="key-row", name="Key", provider_type=ag.PROVIDER_TYPE,
                                       base_url="", auth_mode="gemini-api-key", auth_token=None,
                                       models=list(ag.DEFAULT_MODELS), default_model=None, enabled=True)
    assert observations._binding(source, key_row) is not None  # one key: one reading for all


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


# --- public API routes -------------------------------------------------------

@pytest.mark.parametrize("endpoint,model", [
    ("one_shot_query", "QueryRequest"),
    ("stream_one_shot_query", "QueryRequest"),
    ("conversation", "ConversationRequest"),
    ("stream_conversation", "ConversationRequest"),
    ("start_conversation", "ConversationRequest"),
])
async def test_every_public_api_route_refuses_a_guarded_row_before_the_engine(client, endpoint, model):
    # POST /query, /query/stream (no turn source) and /conversation* (stamped
    # "api") are requests through the API, never the owner typing in the app.
    from unittest.mock import AsyncMock, patch

    from fastapi import HTTPException

    from app.api import query
    from app.core import models

    class Reached(Exception):
        """The engine entry ran: the request got past every route check."""

    streamed = []

    async def stream(**kwargs):
        streamed.append(kwargs["prompt"])
        yield {"type": "done"}

    row = _create(client)
    pick = f"prov:{row['id']}:{row['default_model']}"
    request = getattr(models, model)(prompt="Hi", profile="default",
                                     overrides=models.QueryOverrides(model=pick))
    with patch("app.core.query_engine.execute_query", new=AsyncMock(side_effect=Reached)) as execute, \
            patch("app.api.query._stream_query_with_failover", new=stream), \
            patch("app.api.query.start_background_query", new=AsyncMock(side_effect=Reached)) as start, \
            patch("app.core.turn_manager.submit_async", new=AsyncMock(side_effect=Reached)) as submit:
        with pytest.raises(HTTPException) as exc_info:
            await getattr(query, endpoint)(request, {"id": None, "profile_ids": None})
        assert exc_info.value.status_code == 403
        assert "refused for a request through the API" in exc_info.value.detail
        assert exc_info.value.headers == {"X-Error-Code": ag.GUARD_REFUSED_CODE}
        for entry in (execute, start, submit):
            entry.assert_not_called()
        assert streamed == []

        # Lifted, the same request reaches its engine entry (a key row never
        # stops here): the entry is awaited with the prompt, or the stream is
        # read to its end.
        client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": True})
        reached = {"one_shot_query": execute, "conversation": submit, "start_conversation": start}
        if endpoint in reached:
            with pytest.raises(HTTPException) as exc_info:  # the route turns Reached into a 500
                await getattr(query, endpoint)(request, {"id": None, "profile_ids": None})
            assert exc_info.value.status_code == 500
            reached[endpoint].assert_awaited_once()
            assert reached[endpoint].await_args.kwargs["prompt"] == "Hi"
        else:
            response = await getattr(query, endpoint)(request, {"id": None, "profile_ids": None})
            body = [chunk async for chunk in response.body_iterator]
            assert streamed == ["Hi"]
            assert body == ['event: done\ndata: {"type": "done"}\n\n']
        assert sum(entry.await_count for entry in (execute, start, submit)) == (endpoint in reached)


def test_sign_in_rows_are_the_owners_own_and_key_rows_are_not():
    from app.core import credential_gate

    assert credential_gate.provider_row_is_subscription("antigravity", "oauth-personal")
    assert credential_gate.provider_row_is_subscription("antigravity", "oauth-business")
    assert credential_gate.provider_row_is_subscription("antigravity", "terminal-login")
    assert not credential_gate.provider_row_is_subscription("antigravity", "gemini-api-key")
    assert not credential_gate.provider_row_is_subscription("google", "api_key")


@pytest.mark.parametrize("mode,refused", [
    ("oauth-personal", True), ("oauth-business", True), ("gemini-api-key", False),
])
def test_an_api_user_cannot_run_the_owners_google_sign_in(client, mode, refused):
    from app.core import credential_gate

    key = {"api_key": "test-gemini-secret-value"} if mode == "gemini-api-key" else {}
    row = _create(client, mode, **key)
    options = SimpleNamespace(aih_provider_id=row["id"], aih_provider_type="antigravity",
                              aih_provider_auth_mode=mode)
    refusal = credential_gate.requester_refusal(options, "api-user-1")
    assert (refusal == credential_gate.SUBSCRIPTION_OWNER_ONLY_MESSAGE) is refused
    assert credential_gate.requester_refusal(options, None) is None  # the owner
    assert credential_gate.selection_uses_subscription(f"prov:{row['id']}:{row['default_model']}") is refused


# --- permission mode mid-chat -------------------------------------------------

#: Every permission mode the app can pick, and the agent mode it must reach.
#: Bypass and Auto have no agent equivalent: the agent runs in its normal
#: mode (the host approves every request on Bypass; Auto is the Claude CLI's
#: alone and runs as Ask here), never staying in Plan.
_APP_TO_AGENT_MODE = {
    "default": "default",
    "acceptEdits": "accept-edits",
    "plan": "plan",
    "bypassPermissions": "default",
    "auto": "default",
}


def test_the_mode_table_covers_every_app_permission_mode():
    from app.api.voice_context import VALID_PERMISSION_MODES
    from app.core.session_modes import PERMISSION_MODE_STRICTNESS
    from fluid.permissions.modes import PERMISSION_MODES

    # Plan is the composer's plan toggle, the rest the permission picker's.
    every = set(VALID_PERMISSION_MODES) | set(PERMISSION_MODE_STRICTNESS) | set(PERMISSION_MODES) | {"plan"}
    assert every == set(_APP_TO_AGENT_MODE)


@pytest.mark.parametrize("start,to", [
    (start, to) for start in _APP_TO_AGENT_MODE for to in _APP_TO_AGENT_MODE if start != to
])
async def test_a_mid_chat_permission_change_reaches_the_agent(fake_agy, tmp_path, monkeypatch, start, to):
    # Every app mode to every other one, through the adapter's hot-change
    # entry point, down to the frame the agent receives: the ACP engine must
    # get a mode name, never a native approval policy it cannot map, and the
    # agent must leave the mode it was in when the new one says so.
    agent_mode = _APP_TO_AGENT_MODE[to]
    import json as _json

    from app.core.provider.harness_adapter import _ANTIGRAVITY_KIND, HarnessAdapter
    from fluid.engine.acp import AcpLoop
    from fluid.store.base import MemoryStore

    log = tmp_path / "agent.log"
    spec = ag.build_spec({"id": "row-1", "provider_type": "antigravity", "auth_mode": "gemini-api-key",
                          "auth_token": "test-gemini-not-a-key"})
    # The agent's environment is built from scratch: the fake reads its own.
    spec.env.update({"FAKE_ACP_LOG": str(log), "FAKE_ACP_STATE": str(tmp_path)})
    loop = AcpLoop(store=MemoryStore(), spec=spec, cwd=str(tmp_path), permission_mode=start)
    adapter = HarnessAdapter.__new__(HarnessAdapter)
    adapter._engine = loop
    adapter._is_delegated = False
    adapter._provider_kind = _ANTIGRAVITY_KIND
    adapter._options = SimpleNamespace(permission_mode=start)
    adapter._record_applied = lambda *a: None
    try:
        await loop._ensure_session()
        await loop._ensure_process()  # the agent and its session, as after a first turn
        await adapter.set_permission_mode(to)
    finally:
        await loop.aclose()
    frames = [_json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    modes = [f["params"]["modeId"] for f in frames if f.get("method") == "session/set_mode"]
    assert modes[-1] == agent_mode, modes
    # And the mode the host answers the agent's requests in: Auto is the
    # Claude CLI's alone, so it runs as Ask here (effective_permission_mode)
    # and the host prompts; Bypass approves every request.
    assert loop.permission_mode == ("default" if to == "auto" else to)


async def _mode_adapter(tmp_path, start, **agent_env):
    from app.core.provider.harness_adapter import _ANTIGRAVITY_KIND, HarnessAdapter
    from fluid.engine.acp import AcpLoop
    from fluid.store.base import MemoryStore

    spec = ag.build_spec({"id": "row-1", "provider_type": "antigravity", "auth_mode": "gemini-api-key",
                          "auth_token": "test-gemini-not-a-key"})
    spec.env.update({"FAKE_ACP_LOG": str(tmp_path / "agent.log"), "FAKE_ACP_STATE": str(tmp_path), **agent_env})
    loop = AcpLoop(store=MemoryStore(), spec=spec, cwd=str(tmp_path), permission_mode=start)
    adapter = HarnessAdapter.__new__(HarnessAdapter)
    adapter._engine = loop
    adapter._is_delegated = False
    adapter._provider_kind = _ANTIGRAVITY_KIND
    adapter._options = SimpleNamespace(permission_mode=start)
    adapter.recorded = []
    adapter._record_applied = lambda *a: adapter.recorded.append(a)
    await loop._ensure_session()
    await loop._ensure_process()
    return adapter, loop


@pytest.mark.parametrize("to,agent_env", [
    ("default", {"FAKE_ACP_REJECT_MODES": "default"}),            # the agent refuses the change
    ("bypassPermissions", {"FAKE_ACP_REJECT_MODES": "default"}),  # Bypass runs in its normal mode
    ("default", {"FAKE_ACP_MODES": "accept-edits,plan"}),         # no mode for Ask, nor a normal one
])
async def test_a_mode_change_the_agent_does_not_take_is_not_recorded(fake_agy, tmp_path, to, agent_env):
    # The app keeps showing the mode the agent is in (Accept Edits), never a
    # stricter one it is not in, and the person gets a plain reason (409).
    from fluid.engine.acp.loop import AcpModeRejected

    adapter, loop = await _mode_adapter(tmp_path, "acceptEdits", **agent_env)
    try:
        with pytest.raises(AcpModeRejected, match="it was not changed; it stays in its accept-edits mode"):
            await adapter.set_permission_mode(to)
        assert isinstance(AcpModeRejected("x"), ValueError)  # sessions_permissions answers 409 with it
        assert loop.permission_mode == "acceptEdits" and adapter._options.permission_mode == "acceptEdits"
        assert adapter.recorded == []
    finally:
        await loop.aclose()


async def test_a_resumed_agent_left_in_a_looser_mode_runs_no_turn(fake_agy, tmp_path):
    # At connect: an agent that will not leave a mode doing more without
    # asking than the app's (here an unknown "yolo" mode) fails the start
    # with the reason; one left in a stricter mode runs.
    from fluid.engine.acp.loop import AcpModeRejected

    with pytest.raises(AcpModeRejected, match="no mode that matches Ask"):
        await _mode_adapter(tmp_path, "default", FAKE_ACP_MODES="yolo,careful")
    adapter, loop = await _mode_adapter(tmp_path, "acceptEdits", FAKE_ACP_REJECT_MODES="accept-edits")
    try:
        assert loop._agent_mode == "default"  # stricter than shown: allowed to run
    finally:
        await loop.aclose()


# --- held agent messages after the guard is restored ------------------------

def _held_text(sender, text):
    from app.core import turn_scheduler
    from fluid.context.peer import format_peer_message

    return turn_scheduler._held_entry(format_peer_message(sender, text).strip(), sender=sender)


@pytest.fixture
def held_chat(client, monkeypatch):
    """A chat on an Antigravity row with one held agent message, and the
    notices its sender would get."""
    from app.core import agent_messaging, session_runtime, turn_scheduler, wakeup_service
    from app.db import sessions

    turn_scheduler._delivered_held.clear()
    told = []

    async def deliver(target, sender, text, **kw):
        told.append((target, sender, text))
        return agent_messaging.DELIVERED, None

    monkeypatch.setattr(agent_messaging, "deliver_message", deliver)
    monkeypatch.setattr(session_runtime, "get_live_runtime", lambda sid: object() if sid == "worker-1" else None)

    def make(mode="oauth-personal", allow=False, **fields):
        row = _create(client, mode, **fields)
        if allow is not None and mode == "oauth-personal":
            client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": True})
        chat = {"id": "chat-ag", "title": "Garden plan", "provider_id": row["id"]}
        monkeypatch.setattr(sessions, "get_session", lambda sid: chat if sid == "chat-ag" else None)
        wakeup_service.restore_held_peer_messages(
            "chat-ag", _held_text("worker-1", "halfway there"), delay_seconds=None,
            profile_id=None, project_id=None, model=None)
        if mode == "oauth-personal" and not allow:
            client.put(f"{BASE}/providers/{row['id']}/guard", json={"allow_unattended": False})
        return row

    yield make, told
    for task in list(wakeup_service._timers.values()):
        task.cancel()
    wakeup_service._timers.clear()


async def test_a_held_message_does_not_ride_the_owners_turn_after_the_guard_returns(held_chat):
    from app.core import turn_scheduler

    make, told = held_chat
    make()  # held while lifted, then the owner restored the guard
    owner_turn = {"id": "t-owner", "source": "user", "payload": {}}
    assert await turn_scheduler.fold_held_peer_messages_for_turn("chat-ag", owner_turn) == ""
    await asyncio.sleep(0.05)
    assert len(told) == 1
    target, receiver, text = told[0]
    assert (target, receiver) == ("worker-1", "chat-ag")
    assert text.startswith('Not sent: chat "Garden plan" runs on a personal Google sign-in')
    # Handled once: not delivered later, not told twice.
    assert await turn_scheduler.fold_held_peer_messages_for_turn("chat-ag", owner_turn) == ""
    await asyncio.sleep(0.05)
    assert len(told) == 1


def _held_rows(session_id="chat-ag"):
    from app.core import wakeup_service
    from app.db import wakeups as wakeups_db

    return wakeups_db.get_session_wakeups(session_id, wakeup_service.HELD_PEER_SOURCE)


async def test_a_held_message_is_refused_where_it_would_be_promoted(held_chat):
    # Both promotion points decide before they take: the timer before its
    # claim, the worker's release through the same take as the owner's turn.
    from app.core import turn_scheduler, wakeup_service
    from app.db import turns as turns_db

    make, told = held_chat
    make()
    [row] = _held_rows()
    await wakeup_service._fire(row)  # the hold's timer
    assert _held_rows() == [] and turns_db.get_queued_turns("chat-ag") == []
    await asyncio.sleep(0.05)
    assert len(told) == 1 and told[0][0] == "worker-1"

    make()
    async with turn_scheduler._terminal_delivery_lock("chat-ag"):  # as release_held_progress_from
        assert await turn_scheduler._take_held_peer_messages("chat-ag") == ""
    assert _held_rows() == [] and turns_db.get_queued_turns("chat-ag") == []
    await asyncio.sleep(0.05)
    assert len(told) == 2


def _fail_read(monkeypatch, lookup):
    """Make one of the screening's two lookups fail; return the undo."""
    from app.db import model_providers, sessions

    module, name = (sessions, "get_session") if lookup == "session" else (model_providers, "get_model_provider")
    real = getattr(module, name)

    def broken(*_args, **_kwargs):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(module, name, broken)
    return lambda: monkeypatch.setattr(module, name, real)


@pytest.mark.parametrize("lookup", ["session", "provider"])
@pytest.mark.parametrize("allow", [False, True])
async def test_an_inconclusive_screening_keeps_held_messages_back(held_chat, monkeypatch, lookup, allow):
    # When the chat or its provider cannot be read, nobody can tell whether
    # the row is guarded: the messages are not delivered and not dropped. The
    # owner's turn starts without them; they ride a later turn once the
    # check can be made (and only if the row allows it then).
    from app.core import turn_scheduler

    make, told = held_chat
    make(allow=allow)
    owner_turn = {"id": "t-owner", "source": "user", "payload": {}}
    undo = _fail_read(monkeypatch, lookup)
    assert await turn_scheduler.fold_held_peer_messages_for_turn("chat-ag", owner_turn) == ""
    await asyncio.sleep(0.05)
    assert told == []  # nothing refused either: the check never concluded
    undo()
    folded = await turn_scheduler.fold_held_peer_messages_for_turn(
        "chat-ag", {"id": "t-owner-2", "source": "user", "payload": {}})
    await asyncio.sleep(0.05)
    if allow:
        assert "halfway there" in folded and told == []
    else:
        assert folded == "" and len(told) == 1  # refused, its sender told, once


@pytest.mark.parametrize("lookup", ["session", "provider"])
async def test_an_inconclusive_screening_does_not_promote_held_messages(held_chat, monkeypatch, lookup):
    # Inconclusive takes nothing: the held row stays in the store (never
    # deleted and put back), at the timer and at a worker's release alike.
    from app.core import turn_scheduler, wakeup_service
    from app.db import turns as turns_db

    make, told = held_chat
    make(allow=True)
    [row] = _held_rows()
    armed = []
    monkeypatch.setattr(wakeup_service, "_arm", lambda r, delay: armed.append((r["id"], delay)))
    undo = _fail_read(monkeypatch, lookup)
    await wakeup_service._fire(row)
    async with turn_scheduler._terminal_delivery_lock("chat-ag"):
        assert await turn_scheduler._take_held_peer_messages("chat-ag") == ""
    undo()
    assert [r["id"] for r in _held_rows()] == [row["id"]]  # still there, untouched
    assert armed and armed[0][0] == row["id"]  # its timer tries again
    assert turns_db.get_queued_turns("chat-ag") == []
    folded = await turn_scheduler.fold_held_peer_messages_for_turn(
        "chat-ag", {"id": "t-owner", "source": "user", "payload": {}})
    assert "halfway there" in folded  # kept, not lost
    assert told == []


async def test_a_take_never_starts_before_the_screening_decides(held_chat, monkeypatch):
    # The order itself: nothing reads the held rows destructively while the
    # screening is still out, so a process exit there loses nothing.
    from app.core import turn_scheduler, wakeup_service

    make, _ = held_chat
    make(allow=True)
    seen = []

    async def screening(session_id):
        seen.append(len(_held_rows(session_id)))
        return None
    monkeypatch.setattr(turn_scheduler, "held_delivery_refusal", screening)
    taken = wakeup_service.take_session_wakeups

    async def take(session_id, source):
        assert seen, "taken before the screening decided"
        return await taken(session_id, source)
    monkeypatch.setattr(wakeup_service, "take_session_wakeups", take)
    folded = await turn_scheduler.fold_held_peer_messages_for_turn(
        "chat-ag", {"id": "t-owner", "source": "user", "payload": {}})
    assert seen == [1] and "halfway there" in folded and _held_rows() == []


async def test_held_messages_on_another_provider_ride_as_on_main(held_chat, monkeypatch):
    # A chat on any other provider (here: none bound) is screened, delivers,
    # and takes exactly what main's take took; nothing is refused or kept.
    from app.core import turn_scheduler
    from app.db import sessions

    make, told = held_chat
    make(allow=True)
    monkeypatch.setattr(sessions, "get_session",
                        lambda sid: {"id": sid, "title": "Plain", "provider_id": None})
    folded = await turn_scheduler.fold_held_peer_messages_for_turn(
        "chat-ag", {"id": "t-owner", "source": "user", "payload": {}})
    assert "halfway there" in folded and _held_rows() == [] and told == []
    # And with no held row at all nothing is looked up.
    monkeypatch.setattr(sessions, "get_session", lambda sid: (_ for _ in ()).throw(AssertionError("read")))
    assert await turn_scheduler.fold_held_peer_messages_for_turn(
        "chat-ag", {"id": "t-owner-2", "source": "user", "payload": {}}) == ""


@pytest.mark.parametrize("mode,fields,allow", [
    ("oauth-personal", {}, True),           # the switch on
    ("oauth-business", {}, None),           # business sign-ins are not guarded
    ("gemini-api-key", {"api_key": "test-gemini-secret-value"}, None),
])
async def test_a_held_message_still_rides_when_the_row_allows_it(held_chat, mode, fields, allow):
    from app.core import turn_scheduler

    make, told = held_chat
    make(mode, allow=allow, **fields)
    folded = await turn_scheduler.fold_held_peer_messages_for_turn(
        "chat-ag", {"id": "t-owner", "source": "user", "payload": {}})
    assert "halfway there" in folded
    await asyncio.sleep(0.05)
    assert told == []


async def test_failed_screenings_never_use_up_the_submit_retries(held_chat, monkeypatch):
    # S13, the reviewer's sequence: five screenings that cannot finish, then
    # one that decides, a claim, and ONE failed agent lookup. That is the
    # first submission attempt: the wake is put back, never dropped.
    from app.core import turn_scheduler, wakeup_service

    make, _ = held_chat
    make(allow=True)
    [row] = _held_rows()
    monkeypatch.setattr(wakeup_service, "_arm", lambda r, delay: None)
    dropped = []

    async def announce(r, reason):
        dropped.append(reason)
    monkeypatch.setattr(wakeup_service, "_announce_dropped_wake", announce)
    screenings = []

    async def screening(session_id):
        screenings.append(session_id)
        if len(screenings) <= 5:
            raise RuntimeError("database unavailable")
        return None
    monkeypatch.setattr(turn_scheduler, "held_delivery_refusal", screening)
    for _ in range(5):
        await wakeup_service._fire(row)
    assert wakeup_service._screen_attempts[str(row["id"])] == 5
    assert str(row["id"]) not in wakeup_service._retry_attempts  # no submission tried yet
    lookups = []

    async def lookup(r):
        lookups.append(r["id"])
        raise wakeup_service.AgentLookupFailed("profile store unavailable")
    monkeypatch.setattr(wakeup_service, "_resolve_profile_id", lookup)
    await wakeup_service._fire(row)
    assert lookups == [row["id"]]
    assert wakeup_service._retry_attempts[str(row["id"])] == 1  # attempt 1 of the budget
    assert str(row["id"]) not in wakeup_service._screen_attempts  # cleared once it decided
    assert dropped == [] and [r["id"] for r in _held_rows()] == [row["id"]]  # put back
    wakeup_service._retry_attempts.pop(str(row["id"]), None)


@pytest.fixture
def release_setup(held_chat, monkeypatch):
    """A worker that ended, one receiver holding its note, a promotion that
    records what it delivered, and a release retry with a short backoff."""
    from app.core import turn_scheduler
    from app.db import sessions

    make, told = held_chat
    state = {"provider_id": None, "fail": 0}
    promoted = []

    def get_session(sid):
        if sid in ("worker-1", "worker-2", "worker-3"):
            return {"id": sid, "is_worker": True}
        if sid != "chat-ag":
            return None
        if state["fail"]:
            state["fail"] -= 1
            raise RuntimeError("database unavailable")
        return {"id": sid, "title": "Garden plan", "provider_id": state["provider_id"]}

    async def promote(session_id, held, **kw):
        promoted.append((session_id, held))
        return f"turn-{len(promoted)}"

    def setup(provider="antigravity"):
        if provider == "antigravity":
            state["provider_id"] = make(allow=True)["id"]
        else:
            make(allow=True)
            state["provider_id"] = None  # a chat on any other provider
        monkeypatch.setattr(sessions, "get_session", get_session)

    monkeypatch.setattr(turn_scheduler, "promote_held_peer_messages", promote)
    monkeypatch.setattr(turn_scheduler, "_HELD_RELEASE_RETRY_BASE_SECONDS", 0.01)
    monkeypatch.setattr(turn_scheduler, "_HELD_RELEASE_RETRY_MAX_SECONDS", 0.02)
    yield setup, state, promoted, told
    for task in list(turn_scheduler._held_release_retries.values()):
        task.cancel()
    turn_scheduler._held_release_retries.clear()


async def _settle_retries():
    from app.core import turn_scheduler

    for _ in range(200):
        if not turn_scheduler._held_release_retries:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the release retry never decided")


@pytest.mark.parametrize("provider", ["antigravity", "other"])
async def test_an_inconclusive_release_retries_until_it_delivers_once(release_setup, provider):
    # S15: the worker ended; the receiver's screening could not finish. The
    # release does not read that as "nothing pending": it retries until it
    # decides, then delivers exactly once, on any provider.
    from app.core import turn_scheduler

    setup, state, promoted, told = release_setup
    setup(provider)
    state["fail"] = 3  # three screenings fail, the fourth decides
    assert await turn_scheduler.release_held_progress_from("worker-1") == 0
    assert promoted == [] and len(_held_rows()) == 1  # nothing taken yet
    assert "chat-ag" in turn_scheduler._held_release_retries
    await _settle_retries()
    assert [sid for sid, _ in promoted] == ["chat-ag"] and "halfway there" in promoted[0][1]
    assert _held_rows() == [] and told == []


async def test_a_decided_release_does_not_retry(release_setup):
    from app.core import turn_scheduler

    setup, _, promoted, _ = release_setup
    setup("other")
    assert await turn_scheduler.release_held_progress_from("worker-1") == 1
    assert turn_scheduler._held_release_retries == {} and len(promoted) == 1


async def test_release_retries_never_deliver_twice(release_setup, monkeypatch):
    # Two releases for one receiver share one retry, and a retry that loses
    # to the receiver's own next turn finds nothing and ends: no second
    # delivery, no "not sent".
    from app.core import turn_scheduler

    setup, state, promoted, told = release_setup
    setup()
    # The receiver's turn surely comes first.
    monkeypatch.setattr(turn_scheduler, "_HELD_RELEASE_RETRY_BASE_SECONDS", 0.2)
    state["fail"] = 2
    await turn_scheduler.release_held_progress_from("worker-1")
    await turn_scheduler.release_held_progress_from("worker-1")
    assert len(turn_scheduler._held_release_retries) == 1
    state["fail"] = 0
    folded = await turn_scheduler.fold_held_peer_messages_for_turn(
        "chat-ag", {"id": "t-owner", "source": "user", "payload": {}})
    assert "halfway there" in folded  # the receiver's own turn took them first
    await _settle_retries()
    assert promoted == [] and told == [] and _held_rows() == []


def _hold_note(sender, text):
    from app.core import wakeup_service

    wakeup_service.restore_held_peer_messages(
        "chat-ag", _held_text(sender, text), delay_seconds=None,
        profile_id=None, project_id=None, model=None)


async def test_a_release_retry_never_wakes_the_receiver_for_a_running_senders_note(
        release_setup, monkeypatch):
    # P3: worker-1 ended and its release could not decide. While the retry
    # waits, the receiver's own turn takes worker-1's note, then worker-2
    # (still running) leaves an ordinary note. The retry finds nothing owed
    # and ends: the receiver is not woken, worker-2's note stays held until
    # worker-2 ends (or the receiver's next turn).
    from app.core import turn_scheduler

    setup, state, promoted, told = release_setup
    setup()
    monkeypatch.setattr(turn_scheduler, "_HELD_RELEASE_RETRY_BASE_SECONDS", 0.2)
    state["fail"] = 1
    assert await turn_scheduler.release_held_progress_from("worker-1") == 0
    assert turn_scheduler._held_release_owed == {"chat-ag": {"worker-1"}}
    folded = await turn_scheduler.fold_held_peer_messages_for_turn(
        "chat-ag", {"id": "t-owner", "source": "user", "payload": {}})
    assert "halfway there" in folded
    _hold_note("worker-2", "still digging")
    await _settle_retries()
    assert promoted == [] and told == []
    [row] = _held_rows()
    assert "still digging" in row["prompt"]  # still held for its own release
    assert turn_scheduler._held_release_owed == {}
    assert await turn_scheduler.release_held_progress_from("worker-2") == 1
    assert len(promoted) == 1 and "still digging" in promoted[0][1] and _held_rows() == []


async def test_a_release_retry_still_owed_delivers_every_held_note_once(release_setup, monkeypatch):
    # worker-1's note is still held when the retry wakes: it takes and
    # promotes all the receiver's held notes once, as the due release would
    # have (worker-2's note, left meanwhile, rides along as on main).
    from app.core import turn_scheduler

    setup, state, promoted, told = release_setup
    setup()
    monkeypatch.setattr(turn_scheduler, "_HELD_RELEASE_RETRY_BASE_SECONDS", 0.1)
    state["fail"] = 1
    assert await turn_scheduler.release_held_progress_from("worker-1") == 0
    _hold_note("worker-2", "still digging")
    await _settle_retries()
    assert len(promoted) == 1
    assert "halfway there" in promoted[0][1] and "still digging" in promoted[0][1]
    assert _held_rows() == [] and told == [] and turn_scheduler._held_release_owed == {}


async def test_two_ended_senders_share_one_release_retry(release_setup, monkeypatch):
    # Two workers ended and neither release could decide: one retry, owed for
    # both. The receiver's own turn takes their notes; a third worker, still
    # running, then leaves one. The shared retry ends without waking anyone.
    from app.core import turn_scheduler

    setup, state, promoted, told = release_setup
    setup()
    _hold_note("worker-2", "second half")
    monkeypatch.setattr(turn_scheduler, "_HELD_RELEASE_RETRY_BASE_SECONDS", 0.2)
    state["fail"] = 2
    assert await turn_scheduler.release_held_progress_from("worker-1") == 0
    assert await turn_scheduler.release_held_progress_from("worker-2") == 0
    assert len(turn_scheduler._held_release_retries) == 1
    assert turn_scheduler._held_release_owed == {"chat-ag": {"worker-1", "worker-2"}}
    folded = await turn_scheduler.fold_held_peer_messages_for_turn(
        "chat-ag", {"id": "t-owner", "source": "user", "payload": {}})
    assert "halfway there" in folded and "second half" in folded
    _hold_note("worker-3", "just started")
    await _settle_retries()
    assert promoted == [] and told == []
    [row] = _held_rows()
    assert "just started" in row["prompt"]


async def test_a_release_that_joins_a_finished_retry_gets_a_new_one(release_setup, monkeypatch):
    # S16: worker-1's retry delivers and its loop returns. Before its done
    # callback runs, worker-2's release cannot decide and asks for a retry.
    # The finished task must not absorb it: a new retry, owed for worker-2,
    # delivers worker-2's note once, and the old callback clears nothing of it.
    from app.core import turn_scheduler

    setup, state, promoted, told = release_setup
    setup()
    loop = asyncio.get_running_loop()
    seen = {}

    def worker_2_joins():
        # Runs after worker-1's loop returned, before its done callback.
        old = turn_scheduler._held_release_retries["chat-ag"]
        seen["old"] = old
        seen["old_done"] = old.done()
        _hold_note("worker-2", "second half")
        turn_scheduler._retry_held_release("chat-ag", "worker-2")
        seen["new"] = turn_scheduler._held_release_retries["chat-ag"]

    async def promote(session_id, held, **kw):
        promoted.append((session_id, held))
        if len(promoted) == 1:
            loop.call_soon(worker_2_joins)  # queued ahead of the done callback
        return f"turn-{len(promoted)}"

    monkeypatch.setattr(turn_scheduler, "promote_held_peer_messages", promote)
    state["fail"] = 1
    assert await turn_scheduler.release_held_progress_from("worker-1") == 0
    for _ in range(200):
        if "new" in seen:
            break
        await asyncio.sleep(0.005)
    await asyncio.sleep(0)  # the old task's done callback runs now
    assert seen["old_done"] is True and seen["new"] is not seen["old"]
    assert turn_scheduler._held_release_retries.get("chat-ag") is seen["new"]
    assert "worker-2" in turn_scheduler._held_release_owed["chat-ag"]
    assert seen["old"] not in turn_scheduler._kick_tasks  # the old callback still ran
    await _settle_retries()
    assert len(promoted) == 2 and "halfway there" in promoted[0][1]
    assert "second half" in promoted[1][1] and "halfway there" not in promoted[1][1]
    assert _held_rows() == [] and told == [] and turn_scheduler._held_release_owed == {}


async def test_a_release_retry_whose_owed_read_fails_keeps_trying(release_setup, monkeypatch):
    # The check for an owed note cannot read the store: that is "could not
    # decide", never "none left". Nothing is taken, the retry keeps going,
    # and once the read works it delivers once.
    from app.core import turn_scheduler
    from app.db import wakeups as wakeups_db

    setup, state, promoted, told = release_setup
    setup()
    monkeypatch.setattr(turn_scheduler, "_HELD_RELEASE_RETRY_BASE_SECONDS", 0.01)
    real_read = wakeups_db.get_session_wakeups
    reads = {"failed": 0, "broken": True}

    def read(session_id, source=None):
        if reads["broken"]:
            reads["failed"] += 1
            raise RuntimeError("database unavailable")
        return real_read(session_id, source)

    state["fail"] = 1
    assert await turn_scheduler.release_held_progress_from("worker-1") == 0
    monkeypatch.setattr(wakeups_db, "get_session_wakeups", read)
    for _ in range(200):
        if reads["failed"] >= 3:
            break
        await asyncio.sleep(0.01)
    assert reads["failed"] >= 3
    assert "chat-ag" in turn_scheduler._held_release_retries and promoted == []
    reads["broken"] = False
    assert len(_held_rows()) == 1  # nothing was taken
    await _settle_retries()
    assert len(promoted) == 1 and "halfway there" in promoted[0][1] and _held_rows() == []
