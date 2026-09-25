"""Session-bound intake paths must enrol their prompt on the v2 turn queue.

The REST ``/api/v1/conversation`` endpoint and the connector inbound handler both
used to call the query engine directly with a caller-supplied session id. That
path creates no ``turns`` row, so a message arriving while a turn was already
running started a SECOND agent loop against the same session — a forked
conversation and spend nobody accounted for (the same defect the wakeup fire
path had).

Both now go ``turn_manager.submit`` -> ``wait_session_idle`` -> read the outcome
back out of the durable session-event log, which puts them under the
one-live-turn-per-session unique index like every other turn.
"""

from tests.conftest import async_twin
import pytest

from app.core.models import ConversationRequest


# ``id`` is None so the minted session row needs no api_users FK to exist.
API_USER = {"id": None, "profile_ids": ["default"], "project_id": None}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _capture_submit(monkeypatch):
    """Spy on ``turn_manager.submit`` — the seam both intakes now fire through."""
    from app.core import turn_manager

    calls = []

    def _fake_submit(session_id, source, **kwargs):
        calls.append({"session_id": session_id, "source": source, **kwargs})
        return f"turn-{len(calls)}"

    async def _fake_submit_async(session_id, source, **kwargs):
        return _fake_submit(session_id, source, **kwargs)

    # Both seams: the WS/API intakes now await ``submit_async``, while some
    # callers still use the sync form. Patching only one silently stops
    # capturing when a caller migrates.
    monkeypatch.setattr(turn_manager, "submit", _fake_submit)
    monkeypatch.setattr(turn_manager, "submit_async", _fake_submit_async)
    return calls


def _stub_wait(monkeypatch, idle=True):
    """Make ``wait_session_idle`` resolve immediately with ``idle``."""
    from app.core import turn_manager

    async def _fake_wait(session_id, timeout=300.0, poll_interval=0.05):
        return idle

    monkeypatch.setattr(turn_manager, "wait_session_idle", _fake_wait)


def _stub_outcome(monkeypatch, usage=None, text="hello there",
                  error=None, interrupted=False):
    """Stub the durable-log readers the intakes derive their reply from."""
    from app.core import turn_outcome

    monkeypatch.setattr(
        turn_outcome, "final_turn_outcome",
        lambda sid: {"usage": usage or {}, "error": error,
                     "interrupted": interrupted},
    )
    monkeypatch.setattr(
        turn_outcome, "extract_final_assistant_text", lambda sid: text
    )


def _capture_interrupt(monkeypatch):
    from app.core import turn_manager

    calls = []
    _fake_interrupt = lambda *a, **kw: calls.append((a, kw))
    monkeypatch.setattr(turn_manager, "interrupt", _fake_interrupt)
    monkeypatch.setattr(
        turn_manager, "interrupt_async", async_twin(_fake_interrupt)
    )
    return calls


# ---------------------------------------------------------------------------
# REST /conversation
# ---------------------------------------------------------------------------

async def test_conversation_submits_api_turn_and_mints_session(mock_db, monkeypatch):
    from app.api import query as query_api
    from app.db import database

    calls = _capture_submit(monkeypatch)
    _stub_wait(monkeypatch)
    _stub_outcome(
        monkeypatch,
        usage={"total_cost_usd": 0.5, "tokens_in": 11, "tokens_out": 22,
               "model": "claude-sonnet-4", "num_turns": 3},
        text="hello there",
    )

    result = await query_api.conversation(
        ConversationRequest(prompt="what is up"), api_user=API_USER
    )

    assert len(calls) == 1
    assert calls[0]["source"] == "api"
    assert calls[0]["prompt"] == "what is up"
    assert calls[0]["profile_id"] == "default"
    assert calls[0]["project_id"] is None

    # The session row exists BEFORE the turn — submit enqueues, it never creates.
    session_id = result["session_id"]
    assert calls[0]["session_id"] == session_id
    row = database.get_session(session_id)
    assert row is not None
    assert row["title"] == "what is up"

    assert result["response"] == "hello there"
    meta = result["metadata"]
    assert meta.total_cost_usd == 0.5
    assert meta.tokens_in == 11
    assert meta.tokens_out == 22
    assert meta.model == "claude-sonnet-4"
    assert meta.num_turns == 3
    assert meta.terminal_reason is None
    assert meta.duration_ms is not None


async def test_conversation_continues_existing_session(mock_db, monkeypatch):
    from app.api import query as query_api
    from app.db import database

    database.create_session(session_id="sess-known", profile_id="default")
    calls = _capture_submit(monkeypatch)
    _stub_wait(monkeypatch)
    _stub_outcome(monkeypatch, text="continued")

    result = await query_api.conversation(
        ConversationRequest(prompt="again", session_id="sess-known"),
        api_user=API_USER,
    )

    assert result["session_id"] == "sess-known"
    assert [c["session_id"] for c in calls] == ["sess-known"]


async def test_conversation_rejects_unknown_session(mock_db, monkeypatch):
    from fastapi import HTTPException

    from app.api import query as query_api

    calls = _capture_submit(monkeypatch)
    _stub_wait(monkeypatch)
    _stub_outcome(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await query_api.conversation(
            ConversationRequest(prompt="hi", session_id="nope"),
            api_user=API_USER,
        )

    assert exc.value.status_code == 400
    assert exc.value.detail == "Unknown session_id"
    assert calls == []


async def test_conversation_timeout_is_504_and_leaves_turn_alone(mock_db, monkeypatch):
    from fastapi import HTTPException

    from app.api import query as query_api

    _capture_submit(monkeypatch)
    _stub_wait(monkeypatch, idle=False)
    _stub_outcome(monkeypatch)
    interrupts = _capture_interrupt(monkeypatch)

    with pytest.raises(HTTPException) as exc:
        await query_api.conversation(
            ConversationRequest(prompt="slow one"), api_user=API_USER
        )

    assert exc.value.status_code == 504
    # The turn is durable — the HTTP wait gave up, the turn keeps running.
    assert interrupts == []


async def test_conversation_error_outcome_is_500(mock_db, monkeypatch):
    from fastapi import HTTPException

    from app.api import query as query_api

    _capture_submit(monkeypatch)
    _stub_wait(monkeypatch)
    _stub_outcome(monkeypatch, error="Turn aborted: crash")

    with pytest.raises(HTTPException) as exc:
        await query_api.conversation(
            ConversationRequest(prompt="boom"), api_user=API_USER
        )

    assert exc.value.status_code == 500


async def test_conversation_into_busy_session_queues_instead_of_racing(
    mock_db, monkeypatch
):
    """The whole point of the fix: a REST message into a LIVE session queues.

    No submit spy here — the real ``turn_manager.submit`` runs, so the assertion
    is on the durable ``turns`` table: the user's turn is still the one live
    turn and the API turn sits behind it, rather than a second agent loop
    starting against the same session.
    """
    from app.api import query as query_api
    from app.db import database
    from app.db import turns as turns_db

    sid = "sess-busy"
    database.create_session(session_id=sid, profile_id="default")
    live = turns_db.enqueue_turn(sid, "user", prompt="do work")
    turns_db.start_turn(live["id"])
    assert turns_db.get_live_turn(sid)["state"] == "running"

    _stub_wait(monkeypatch)
    _stub_outcome(monkeypatch, text="queued reply")

    await query_api.conversation(
        ConversationRequest(prompt="me too", session_id=sid), api_user=API_USER
    )

    assert turns_db.get_live_turn(sid)["id"] == live["id"]
    queued = turns_db.get_queued_turns(sid)
    assert len(queued) == 1
    assert queued[0]["source"] == "api"
    assert queued[0]["state"] == "queued"
    assert queued[0]["prompt"] == "me too"


# ---------------------------------------------------------------------------
# Connector inbound
# ---------------------------------------------------------------------------

class _FakeConnector:
    """Minimal connector instance recording what the manager sends it."""

    MAX_MESSAGE_LENGTH = None

    def __init__(self):
        self.sent = []
        self.typing = 0
        self.acks = 0
        self.reads = 0

    async def send_ack_reaction(self, chat_id, message_id):
        self.acks += 1
        return True

    async def mark_as_read(self, chat_id, message_id):
        self.reads += 1
        return True

    async def send_typing_indicator(self, chat_id):
        self.typing += 1
        return True

    async def send_message(self, outgoing):
        self.sent.append(outgoing)
        return True


def _incoming(text="hey there"):
    from app.core.connectors.base import IncomingMessage

    return IncomingMessage(
        connector_id="conn-1",
        platform_chat_id="chat-1",
        platform_user_id="user-1",
        platform_user_name="Someone",
        text=text,
        platform_message_id="msg-1",
    )


def _wire_connector(monkeypatch, manager, instance, session_row,
                    create_session=None, api_user=None):
    """Patch the DB helpers the inbound path touches."""
    from app.core.connectors import manager as manager_module
    from app.core.connectors.manager import ConnectorManager

    manager._connectors["conn-1"] = instance

    db = manager_module.database
    monkeypatch.setattr(db, "get_connector_session", lambda cid, chat: session_row)
    monkeypatch.setattr(
        db, "get_connector",
        lambda cid: {"id": cid, "type": "discord", "name": "Discord",
                     "api_user_id": "api-user-1"},
    )
    monkeypatch.setattr(
        db, "get_api_user",
        lambda uid: api_user or {"id": uid, "profile_ids": ["default"], "project_id": None},
    )
    monkeypatch.setattr(db, "get_all_profiles", lambda: [{"id": "default"}])
    monkeypatch.setattr(
        db, "update_connector_session_ai_session", lambda *a, **kw: None
    )
    monkeypatch.setattr(db, "create_connector_event_log", lambda **kw: None)
    monkeypatch.setattr(
        db, "update_connector_event_dispatched", lambda *a, **kw: None
    )
    if create_session is not None:
        monkeypatch.setattr(db, "create_session", create_session)
    monkeypatch.setattr(
        ConnectorManager, "_build_connector_context",
        staticmethod(lambda row, inst, chat: "connector context"),
    )


async def test_connector_message_submits_connector_turn(mock_db, monkeypatch):
    from app.core.connectors.manager import ConnectorManager

    manager = ConnectorManager()
    instance = _FakeConnector()
    _wire_connector(
        monkeypatch, manager, instance,
        session_row={"id": "cs-1", "session_id": "ai-sess-1"},
    )

    calls = _capture_submit(monkeypatch)
    _stub_wait(monkeypatch)
    _stub_outcome(monkeypatch, text="from the queue")

    await manager.handle_incoming_message(_incoming())

    assert len(calls) == 1
    assert calls[0]["source"] == "connector"
    assert calls[0]["session_id"] == "ai-sess-1"
    assert calls[0]["prompt"] == "hey there"
    assert calls[0]["profile_id"] == "default"
    # Connector turns always pin the permission mode to Ask (``default``) so
    # prompts funnel out through the connector instead of inheriting a
    # profile-level bypass.
    assert calls[0]["overrides"] == {
        "system_prompt_append": "connector context",
        "permission_mode": "default",
    }

    assert len(instance.sent) == 1
    assert instance.sent[0].text == "from the queue"


async def test_connector_bails_when_session_cannot_be_minted(mock_db, monkeypatch):
    """No session row → nothing to enrol a turn on, so drop rather than fork."""
    from app.core.connectors.manager import ConnectorManager

    def _boom(**kwargs):
        raise RuntimeError("no session for you")

    manager = ConnectorManager()
    instance = _FakeConnector()
    _wire_connector(
        monkeypatch, manager, instance,
        session_row={"id": "cs-1", "session_id": None},
        create_session=_boom,
    )

    calls = _capture_submit(monkeypatch)
    _stub_wait(monkeypatch)
    _stub_outcome(monkeypatch)

    await manager.handle_incoming_message(_incoming())

    assert calls == []
    assert instance.sent == []
    assert instance.typing == 0


async def test_connector_session_mints_with_api_user_run_config(mock_db, monkeypatch):
    """A freshly minted connector session binds the API user's preconfigured
    model/effort and ALWAYS pins the permission mode to Ask."""
    from app.core.connectors.manager import ConnectorManager

    captured = {}

    def _fake_create_session(**kwargs):
        captured.update(kwargs)
        return {"id": kwargs["session_id"]}

    manager = ConnectorManager()
    instance = _FakeConnector()
    _wire_connector(
        monkeypatch, manager, instance,
        session_row={"id": "cs-1", "session_id": None},
        create_session=_fake_create_session,
        api_user={
            "id": "api-user-1",
            "profile_ids": ["default"],
            "project_id": None,
            "model": "opus",
            "effort": "high",
        },
    )

    calls = _capture_submit(monkeypatch)
    _stub_wait(monkeypatch)
    _stub_outcome(monkeypatch, text="minted")

    await manager.handle_incoming_message(_incoming())

    assert captured["model"] == "opus"
    assert captured["effort"] == "high"
    assert captured["permission_mode"] == "default"

    # The same binding rides every turn's overrides.
    assert len(calls) == 1
    assert calls[0]["overrides"]["model"] == "opus"
    assert calls[0]["overrides"]["effort"] == "high"
    assert calls[0]["overrides"]["permission_mode"] == "default"


async def test_connector_turn_rebinds_when_api_user_config_changes(mock_db, monkeypatch):
    """``connector`` is a _BINDING_SOURCES member: editing the API user
    re-binds EXISTING conversations on their next message."""
    from app.core.connectors.manager import ConnectorManager

    manager = ConnectorManager()
    instance = _FakeConnector()
    _wire_connector(
        monkeypatch, manager, instance,
        session_row={"id": "cs-1", "session_id": "ai-sess-1"},
        api_user={
            "id": "api-user-1",
            "profile_ids": ["default"],
            "project_id": None,
            "model": "fable",
            "effort": "max",
        },
    )

    calls = _capture_submit(monkeypatch)
    _stub_wait(monkeypatch)
    _stub_outcome(monkeypatch, text="rebound")

    await manager.handle_incoming_message(_incoming())

    assert calls[0]["overrides"]["model"] == "fable"
    assert calls[0]["overrides"]["effort"] == "max"
    assert calls[0]["overrides"]["permission_mode"] == "default"


async def test_connector_turn_without_api_user_config_asks_only(mock_db, monkeypatch):
    """No model/effort on the API user → no binding overrides, but the
    permission mode is STILL pinned to Ask."""
    from app.core.connectors.manager import ConnectorManager

    manager = ConnectorManager()
    instance = _FakeConnector()
    _wire_connector(
        monkeypatch, manager, instance,
        session_row={"id": "cs-1", "session_id": "ai-sess-1"},
    )

    calls = _capture_submit(monkeypatch)
    _stub_wait(monkeypatch)
    _stub_outcome(monkeypatch, text="plain")

    await manager.handle_incoming_message(_incoming())

    assert "model" not in calls[0]["overrides"]
    assert "effort" not in calls[0]["overrides"]
    assert calls[0]["overrides"]["permission_mode"] == "default"


@pytest.mark.parametrize("bound,api_user,expected", [
    (None, None, "no API user bound"),
    ("api-user-1", {"id": "api-user-1", "profile_ids": []}, "no agent profile"),
])
async def test_connector_turn_is_refused_without_a_bound_api_user(
    mock_db, monkeypatch, bound, api_user, expected
):
    """No API user (or one without a profile) → the chat gets one line and no
    turn runs; the first profile in the database is never used instead."""
    from app.core.connectors import manager as manager_module
    from app.core.connectors.manager import ConnectorManager

    manager = ConnectorManager()
    instance = _FakeConnector()
    _wire_connector(
        monkeypatch, manager, instance,
        session_row={"id": "cs-1", "session_id": "ai-sess-1"},
        api_user=api_user,
    )
    db = manager_module.database
    monkeypatch.setattr(
        db, "get_connector",
        lambda cid: {"id": cid, "type": "discord", "name": "Discord", "api_user_id": bound},
    )
    if bound is None:
        monkeypatch.setattr(db, "get_api_user", lambda uid: None)
    created = []
    monkeypatch.setattr(db, "create_connector_session", lambda **kw: created.append(kw))

    calls = _capture_submit(monkeypatch)
    _stub_wait(monkeypatch)
    _stub_outcome(monkeypatch)

    await manager.handle_incoming_message(_incoming())

    assert calls == []
    assert created == []
    assert len(instance.sent) == 1
    assert expected in instance.sent[0].text

