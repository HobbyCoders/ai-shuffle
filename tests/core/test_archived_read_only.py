"""Archived chats are read-only transcripts until the owner restores them (#1822).

The start-time guard itself (``SessionRuntime.execute`` refusing every turn
source) is covered in ``test_session_runtime.py``. These tests drive each wake
source and tool against a real archived session row: nothing is queued, the
drop is logged, tools name the archived target, archiving stops the chat's
work, and restoring makes it usable again.
"""
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import archive_guard, session_orchestration as orch, turn_manager, turn_scheduler
from app.core.config import settings
from app.core.task_registry import task_registry
from app.db import database, sessions
from app.db import turns as turns_db
from app.db.base import get_db


@pytest.fixture
def chats(mock_db, monkeypatch):
    """A caller chat with a finished turn, and helpers to add archived chats."""
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)
    monkeypatch.setattr(turn_manager, "_kick", lambda sid: None)
    _chat("caller")


def _chat(sid, *, archived=False):
    sessions.create_session(sid, "agent", title=f"Chat {sid}", is_active=not archived,
                            model="sonnet", permission_mode="default")
    turn = turns_db.enqueue_turn(sid, "user", prompt="work", profile_id="agent")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE turns SET state='completed' WHERE id=%s", (turn["id"],))
        if archived:
            cur.execute("UPDATE sessions SET status='archived' WHERE id=%s", (sid,))
    return turn


def _queued(sid):
    return turns_db.get_queued_turns(sid)


def _dropped(caplog, sid, source):
    return any(r.name == "app.core.archive_guard" and sid in r.getMessage()
               and source in r.getMessage() for r in caplog.records)


# -- wake sources ------------------------------------------------------------

@pytest.mark.parametrize("source", [
    "user", "queue", "api", "automation", "wakeup", "connector", "resume", "unpark",
])
async def test_submit_refuses_an_archived_session(chats, caplog, source):
    """Every turn_manager submit (user send, queue, API, automation, wake-up,
    connector, Retry) is refused before anything is written."""
    _chat("old", archived=True)
    task_registry.mark_user_stopped("old")

    with caplog.at_level("WARNING"), pytest.raises(archive_guard.SessionArchived):
        await turn_manager.submit_async("old", source, prompt="hi", profile_id="agent")

    assert _queued("old") == []
    # A refused send must not lift a Stop either.
    assert task_registry.is_user_stopped("old")
    assert _dropped(caplog, "old", source)


async def test_task_completion_wake_is_dropped(chats, caplog):
    """A background task or subagent finishing does not wake an archived chat."""
    _chat("old", archived=True)
    task_registry.start_task("old", "task-1", "agent", "bg", tool_use_id="tu-1")
    task_registry.mark_background_launch("old", "tu-1")

    with caplog.at_level("WARNING"):
        await turn_scheduler.on_task_terminal("old", {
            "task_id": "task-1", "status": "completed", "tool_use_id": "tu-1",
            "summary": "done",
        })

    assert _queued("old") == []
    assert _dropped(caplog, "old", "unpark")


async def test_restart_recovery_does_not_wake_an_archived_chat(chats, caplog):
    """The likely cause of #1822: the boot reconcile's restart notice."""
    from app.core.startup_reconcile import _announce_interrupted_tasks

    _chat("old", archived=True)
    _chat("parent")
    rows = [
        {"task_id": "shell-1", "session_id": "old", "tool_use_id": "tu-1", "kind": "shell"},
        {"task_id": "shell-2", "session_id": "parent", "tool_use_id": "tu-2", "kind": "shell"},
    ]

    with caplog.at_level("WARNING"):
        woken = await _announce_interrupted_tasks(rows)

    assert woken == 1
    assert _queued("old") == []
    assert [t["source"] for t in _queued("parent")] == ["unpark"]
    assert _dropped(caplog, "old", "restart recovery")


async def test_peer_message_wake_is_dropped(chats, caplog):
    """A peer message (SendMessage / subagent report / held-message promotion)
    queues nothing for an archived chat."""
    _chat("old", archived=True)

    with caplog.at_level("WARNING"):
        assert await turn_scheduler.on_agent_message("old", "caller", "status?") is None
        assert await turn_scheduler.on_agent_message("old", "caller", "now", urgent=True) is None
        assert await turn_scheduler.promote_held_peer_messages("old", "held") is None

    assert _queued("old") == []
    assert _dropped(caplog, "old", "agent_message")


async def test_scheduled_wakeup_is_dropped_not_rearmed(chats, caplog, monkeypatch):
    from app.core import wakeup_service
    from app.db import wakeups as wakeups_db

    _chat("old", archived=True)
    armed = []
    monkeypatch.setattr(wakeup_service, "_arm", lambda row, delay: armed.append(row))
    row = {"id": "w-old", "session_id": "old", "prompt": "continue", "profile_id": "agent",
           "project_id": None, "source": "schedule_wakeup", "model": None, "reason": "later",
           "instance_id": settings.effective_instance_id}
    wakeups_db.create_wakeup(id="w-old", session_id="old", prompt="continue", fire_at=time.time(),
                             profile_id="agent", project_id=None, reason="later", model=None,
                             source="schedule_wakeup", instance_id=settings.effective_instance_id)

    with caplog.at_level("WARNING"):
        await wakeup_service._fire(row)

    assert _queued("old") == []
    assert armed == []
    assert not any(r["id"] == "w-old" for r in
                   wakeups_db.get_pending_wakeups(settings.effective_instance_id))
    assert _dropped(caplog, "old", "wakeup")


async def test_websocket_send_and_queue_are_refused(chats):
    from app.api.chat_ws_handler import ChatWebSocketHandler

    _chat("old", archived=True)
    handler = ChatWebSocketHandler(websocket=MagicMock(), device_id="d", user_id="admin",
                                   api_user_id=None)
    handler.send_json = AsyncMock()

    await handler.handle_query({"prompt": "hello", "session_id": "old"})
    await handler.handle_queue_message({"content": "later", "session_id": "old"})

    assert _queued("old") == []
    frames = [call.args[0] for call in handler.send_json.await_args_list]
    assert len(frames) == 2
    assert all(f["type"] == "error" and f["session_id"] == "old" and "archived" in f["message"]
               for f in frames)


# -- tools refuse archived targets by name -----------------------------------

async def test_message_session_refuses_an_archived_peer_by_name(chats):
    from fluid.tools.registry import ToolCtx

    _chat("old", archived=True)
    send = orch.make_handler(lambda: "caller")
    outcome = await send("message", {"session_id": "old", "message": "hi",
                                     "request_id": "r1"}, ToolCtx())

    assert outcome.is_error
    assert "Session old (Chat old) is archived" in outcome.text
    assert _queued("old") == []


async def test_send_message_refuses_an_archived_subagent_by_name(chats):
    from fluid.tools.registry import ToolCtx
    from app.core.agent_messaging import make_message

    _chat("parent")
    _chat("child", archived=True)
    task_registry.start_task("parent", "child", "agent", "Fix login", tool_use_id="child",
                             name="fixer")
    outcome = await make_message(lambda: "parent")({"to": "fixer", "message": "go on"}, ToolCtx())

    assert outcome.is_error
    assert "fixer is archived" in outcome.text
    assert _queued("child") == []


async def test_hand_off_refuses_an_archived_chat(chats):
    from app.core import session_handoff

    _chat("old", archived=True)
    with pytest.raises(ValueError, match="archived"):
        session_handoff.request("old", "DesktopWorker")
    assert session_handoff.pending_for("old") is None


# -- archiving stops work, restore brings the chat back -----------------------

async def test_archive_stops_running_and_parked_work(chats):
    """Archive goes through the Stop button's path: the live turn is stopped,
    queued turns are aborted, armed wake-ups deleted and the chat latched, so
    nothing is left to wake it."""
    from app.db import wakeups as wakeups_db
    from app.network import session_active

    _chat("old")
    live = turns_db.enqueue_turn("old", "user", prompt="long job", profile_id="agent")
    assert turns_db.start_turn(live["id"])
    queued = turns_db.enqueue_turn("old", "queue", prompt="next", profile_id="agent")
    wakeups_db.create_wakeup(id="w-arch", session_id="old", prompt="check", fire_at=time.time() + 600,
                             profile_id="agent", project_id=None, reason="later", model=None,
                             source="schedule_wakeup", instance_id=settings.effective_instance_id)

    row = await session_active.update_membership(sessions.get_session("old"), False, True)

    assert row["status"] == "archived"
    assert turns_db.get_turn(live["id"])["state"] in turns_db.TERMINAL_STATES
    assert turns_db.get_turn(queued["id"])["state"] == "aborted"
    assert not any(r["session_id"] == "old" for r in
                   wakeups_db.get_pending_wakeups(settings.effective_instance_id))
    assert task_registry.is_user_stopped("old")


async def test_bulk_archive_handles_each_chat_on_its_own(chats):
    from fluid.tools.registry import ToolCtx
    from fluid.tools.builtins.session_orchestration import SPECS

    for sid in ("old", "old2"):
        _chat(sid)
    live = turns_db.enqueue_turn("old2", "user", prompt="busy", profile_id="agent")
    assert turns_db.start_turn(live["id"])
    ctx = ToolCtx(extra={"session_orchestration": orch.make_handler(lambda: "caller")})
    _, handler = next((s, h) for s, h in SPECS if s.name == "ArchiveSession")

    outcome = await handler({"session_ids": ["old", "old2", "caller"]}, ctx)

    result = json.loads(outcome.text)
    assert result["archived"] == 2
    assert result["refused"] == 1
    assert result["issues"][0]["session_id"] == "caller"
    assert "cannot archive itself" in result["issues"][0]["reason"]
    assert sessions.get_session("caller")["status"] != "archived"
    for sid in ("old", "old2"):
        assert sessions.get_session(sid)["status"] == "archived"
    assert turns_db.get_turn(live["id"])["state"] in turns_db.TERMINAL_STATES


async def test_restore_makes_the_chat_usable_again(chats):
    from fluid.tools.registry import ToolCtx
    from app.network import session_active

    _chat("old")
    await session_active.update_membership(sessions.get_session("old"), False, True)
    assert task_registry.is_user_stopped("old")
    with pytest.raises(archive_guard.SessionArchived):
        await turn_manager.submit_async("old", "user", prompt="hi", profile_id="agent")

    row = await session_active.update_membership(sessions.get_session("old"), False, False)

    assert row["status"] == "active"
    # Restore releases the latch archiving set: peers and wakes work again
    # without the owner having to type first.
    assert not task_registry.is_user_stopped("old")
    turn_id = await turn_manager.submit_async("old", "user", prompt="hi", profile_id="agent")
    assert [t["id"] for t in _queued("old")] == [turn_id]
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET is_active=TRUE WHERE id='old'")
    send = orch.make_handler(lambda: "caller")
    outcome = await send("message", {"session_id": "old", "message": "welcome back",
                                     "request_id": "r-back"}, ToolCtx())
    assert not outcome.is_error, outcome.text


async def test_restoring_a_chat_that_was_not_archived_keeps_its_stop(chats):
    from app.network import session_active

    _chat("old")
    task_registry.mark_user_stopped("old")
    await session_active.update_membership(sessions.get_session("old"), False, False)
    assert task_registry.is_user_stopped("old")
