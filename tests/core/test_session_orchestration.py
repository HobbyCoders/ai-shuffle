"""Drive real session and turn producers, without provider inference."""
import json
import pytest

from app.core import session_orchestration as orch
from app.db import sessions, turns, database
from app.db.base import get_db


def chat(sid, **kwargs):
    if kwargs.get("api_user_id"):
        database.create_api_user(kwargs["api_user_id"], kwargs["api_user_id"], "hash")
    row = sessions.create_session(sid, "agent", title=sid, is_active=True,
                                  model="sonnet", permission_mode="default", **kwargs)
    turn = turns.enqueue_turn(sid, "user", prompt="work", profile_id="agent")
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (turn["id"],))
    return row, turn


@pytest.fixture
def peers(mock_db, monkeypatch):
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    return chat("caller"), chat("peer")




async def test_mcp_adapter_uses_same_owner_fence_and_idempotency(peers, monkeypatch):
    from app.mcp.builtin_tools import create_builtin_tools_server
    from app.core import turn_scheduler
    import app.mcp.inproc as inproc
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "note_wake_pending", lambda sid: None)
    captured = {}
    real_create = inproc.create_tool_server
    def capture(**kwargs):
        captured.update({item.name: item for item in kwargs["tools"]})
        return real_create(**kwargs)
    monkeypatch.setattr(inproc, "create_tool_server", capture)
    create_builtin_tools_server(session_id="caller", profile_id="agent")
    assert {"ListSessions", "MessageSession", "StartSession"} <= captured.keys()
    args = {"session_id": "peer", "message": "coordinate", "request_id": "mcp"}
    result = await captured["MessageSession"].handler(args)
    assert not result.get("isError"), result
    again = await captured["MessageSession"].handler(args)
    assert "already_queued" in str(again)


def test_context_wakes_preserve_chain_identity(peers):
    from app.core.turn_scheduler import _context_payload
    payload = {"orchestration_root": "origin", "orchestration_depth": 3,
               "orchestration_sender": "do-not-copy", "api_user_id": "u"}
    assert _context_payload({"payload": payload}) == {
        "orchestration_root": "origin", "orchestration_depth": 3, "api_user_id": "u"}


def test_principal_owner_is_not_inferred_from_nullable_user_id(peers, monkeypatch):
    monkeypatch.setattr(orch, "_principal", lambda sid: {"owner": sid, "kind": "admin"})
    monkeypatch.setattr("app.core.mobile_tool_identity.validate", lambda principal: None)
    assert not orch.list_sessions("caller", {"scope": "all"})["sessions"]
    with pytest.raises(ValueError, match="eligible"):
        orch._queue("caller", {"session_id": "peer", "message": "go", "request_id": "owner"}, "message")


def test_unknown_or_revoked_caller_cannot_act(peers, monkeypatch):
    with pytest.raises(ValueError):
        orch.list_sessions("unknown", {})
    monkeypatch.setattr(orch, "_principal", lambda sid: {"owner": "one", "kind": "paired"})
    def reject(principal):
        raise PermissionError("revoked")
    monkeypatch.setattr("app.core.mobile_tool_identity.validate", reject)
    with pytest.raises(PermissionError):
        orch._queue("caller", {"title": "Work", "prompt": "go", "request_id": "revoked"}, "start")


async def test_stop_race_cancels_queue_before_kick(peers, monkeypatch):
    from app.core import turn_scheduler
    from app.core.task_registry import task_registry
    from fluid.tools.registry import ToolCtx
    kicks = []
    monkeypatch.setattr(turn_scheduler, "kick", kicks.append)
    monkeypatch.setattr(task_registry, "stop_invalidated", lambda *args: True)
    result = await orch.make_handler(lambda: "caller")("message", {
        "session_id": "peer", "message": "go", "request_id": "stop-race"}, ToolCtx())
    assert result.is_error and "stopped" in result.text
    assert not kicks
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT id FROM turns WHERE session_id='peer' AND state='queued'")
        assert not c.fetchall()




async def test_list_tool_serializes_postgres_timestamps(peers, monkeypatch):
    from datetime import datetime, timezone
    from fluid.tools.registry import ToolCtx
    real = sessions.get_sessions
    def pg_rows(**kwargs):
        rows = real(**kwargs)
        for row in rows:
            row["updated_at"] = datetime(2026, 9, 25, tzinfo=timezone.utc)
        return rows
    monkeypatch.setattr(sessions, "get_sessions", pg_rows)
    result = await orch.make_handler(lambda: "caller")("list", {}, ToolCtx())
    assert not result.is_error
    assert json.loads(result.text)["sessions"][0]["updated_at"] == "2026-09-25T00:00:00+00:00"
    json.dumps(result.ui)




def test_message_can_start_an_existing_empty_top_level_chat(peers):
    sessions.create_session("empty", "agent", title="Empty peer", is_active=True,
                            model="sonnet", permission_mode="default")
    result = orch._queue("caller", {"session_id": "empty", "message": "take this work", "request_id": "empty"}, "message")
    turn = turns.get_turn(result["turn_id"])
    assert turn["overrides"]["permission_mode"] == "default"
    assert turn["source"] == "agent_message"
    assert "another top-level session" in turn["prompt"]


def test_live_user_turn_resets_parked_peer_ancestry(peers, monkeypatch):
    from app.core import turn_scheduler
    old = {"id": "old-parked", "payload": {"orchestration_depth": 8}}
    monkeypatch.setattr(turn_scheduler, "_latest_context_turn", lambda sid: old)
    live = turns.enqueue_turn("caller", "user", prompt="Continue", profile_id="agent")
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='running' WHERE id=%s", (live["id"],))
    assert orch._context("caller")["id"] == live["id"]
    created = orch._queue("caller", {"title": "Work", "prompt": "go", "request_id": "human"}, "start")
    assert turns.get_turn(created["turn_id"])["payload"]["orchestration_root"] == live["id"]




def test_durable_abort_requires_user_continuation_after_restart(peers):
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='aborted' WHERE id=%s", (peers[1][1]["id"],))
    args = {"session_id": "peer", "message": "retry", "request_id": "after-restart"}
    with pytest.raises(ValueError, match="aborted"):
        orch._queue("caller", args, "message")
    # Only a fresh real user turn supersedes the durable stop, not a peer wake.
    user = turns.enqueue_turn("peer", "user", prompt="Continue", profile_id="agent")
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='running' WHERE id=%s", (user["id"],))
    assert orch._queue("caller", args, "message")["status"] == "queued"


def test_list_active_visible_local_same_owner(peers):
    chat("foreign", api_user_id="someone-else")
    chat("worker", is_worker=True)
    chat("hidden", hidden=True)
    chat("archived")
    sessions.set_session_active("archived", False)
    result = orch.list_sessions("caller", {"scope": "all"})
    assert [r["id"] for r in result["sessions"]] == ["peer"]
    assert "prompt" not in result["sessions"][0]


def test_start_is_top_level_and_atomic_peer_authored(peers):
    result = orch._queue("caller", {"title": "Independent review", "prompt": "Review this work", "request_id": "review"}, "start")
    child = sessions.get_session(result["session_id"])
    assert child["is_active"] and not child["is_worker"] and not child["hidden"]
    assert child["parent_session_id"] is None
    assert child["profile_id"] == "agent"
    assert child["model"] == "sonnet"
    assert child["permission_mode"] == "default"
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM turns WHERE id=%s", (result["turn_id"],))
        turn = turns._row(c.fetchone())
    assert turn["source"] == "agent_message"
    assert "caller" in turn["prompt"] and "Review this work" in turn["prompt"]
    assert turn["payload"]["orchestration_depth"] == 1
    assert turn["payload"]["orchestration_root"] == peers[0][1]["id"]


def test_message_replays_once_and_rejects_changed_request(peers):
    args = {"session_id": "peer", "message": "Coordinate edits", "request_id": "coordinate"}
    first = orch._queue("caller", args, "message")
    again = orch._queue("caller", args, "message")
    assert first["turn_id"] == again["turn_id"]
    assert again["status"] == "already_queued"
    with pytest.raises(ValueError, match="different arguments"):
        orch._queue("caller", {**args, "message": "Different"}, "message")


@pytest.mark.parametrize("kind", ["foreign", "hidden", "worker", "archived", "self"])
def test_targets_cannot_be_guessed_outside_scope(peers, kind):
    if kind != "self":
        chat("blocked", **({"api_user_id": "other"} if kind == "foreign" else
                         {"hidden": True} if kind == "hidden" else
                         {"is_worker": True} if kind == "worker" else {}))
        if kind == "archived":
            sessions.set_session_active("blocked", False)
    with pytest.raises(ValueError):
        orch._queue("caller", {"session_id": "caller" if kind == "self" else "blocked",
                    "message": "go", "request_id": kind}, "message")


def test_stop_is_not_cleared(peers, monkeypatch):
    from app.core.task_registry import task_registry
    monkeypatch.setattr(task_registry, "is_user_stopped", lambda sid: sid == "peer")
    with pytest.raises(ValueError, match="stopped"):
        orch._queue("caller", {"session_id": "peer", "message": "go", "request_id": "stop"}, "message")
    assert not orch.list_sessions("caller", {})["sessions"]


def test_start_budget_survives_fresh_handler_and_retries(peers):
    args = {"title": "Work", "prompt": "Do work", "request_id": "0"}
    ids = []
    for i in range(orch.MAX_STARTS):
        ids.append(orch._queue("caller", {**args, "request_id": str(i)}, "start")["session_id"])
    assert len(set(ids)) == orch.MAX_STARTS
    assert orch._queue("caller", args, "start")["session_id"] == ids[0]
    with pytest.raises(ValueError, match="budget"):
        orch._queue("caller", {**args, "request_id": "overflow"}, "start")


def test_message_budget_and_hop_limit(peers, monkeypatch):
    monkeypatch.setattr(orch, "MAX_ACTIONS", 2)
    args = {"session_id": "peer", "message": "go"}
    for i in range(2):
        orch._queue("caller", {**args, "request_id": str(i)}, "message")
    with pytest.raises(ValueError, match="budget"):
        orch._queue("caller", {**args, "request_id": "third"}, "message")
    original = orch._context("caller")
    monkeypatch.setattr(orch, "_context", lambda sid: {**original, "payload": {"orchestration_depth": 8}})
    with pytest.raises(ValueError, match="hop"):
        orch._queue("caller", {**args, "request_id": "hop"}, "message")


def test_chain_continuation_keeps_budget(peers):
    created = orch._queue("caller", {"title": "Work", "prompt": "go", "request_id": "first"}, "start")
    with get_db() as conn:
        conn.cursor().execute("UPDATE turns SET state='completed' WHERE id=%s", (created["turn_id"],))
    followup = orch._queue(created["session_id"], {"session_id": "peer", "message": "coordinate", "request_id": "next"}, "message")
    with get_db() as conn:
        c = conn.cursor()
        c.execute("SELECT * FROM turns WHERE id=%s", (followup["turn_id"],))
        row = turns._row(c.fetchone())
    assert row["parent_turn_id"] == peers[0][1]["id"]
    assert row["payload"]["orchestration_depth"] == 2


def test_creation_rolls_back_if_enqueue_fails(peers, monkeypatch):
    before = sessions.get_sessions()
    def fail(*a, **k):
        raise RuntimeError("queue failed")
    monkeypatch.setattr(turns, "enqueue_turn", fail)
    with pytest.raises(RuntimeError, match="queue failed"):
        orch._queue("caller", {"title": "Work", "prompt": "go", "request_id": "atomic"}, "start")
    assert {r["id"] for r in sessions.get_sessions()} == {r["id"] for r in before}


async def test_native_tool_to_durable_queue(peers, monkeypatch):
    from fluid.tools.builtins.session_orchestration import SPECS
    from fluid.tools.registry import ToolCtx
    from app.core import turn_scheduler
    kicks = []
    monkeypatch.setattr(turn_scheduler, "kick", kicks.append)
    monkeypatch.setattr(turn_scheduler, "note_wake_pending", lambda sid: None)
    ctx = ToolCtx(extra={"session_orchestration": orch.make_handler(lambda: "caller")})
    _, handler = next((s, h) for s, h in SPECS if s.name == "MessageSession")
    result = await handler({"session_id": "peer", "message": "go", "request_id": "adapter"}, ctx)
    assert not result.is_error, result.text
    assert json.loads(result.text)["status"] == "queued"
    assert kicks == ["peer"]
