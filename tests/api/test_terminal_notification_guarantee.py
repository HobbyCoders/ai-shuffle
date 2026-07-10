"""Kill attribution + guaranteed terminal notification (lifecycle batch, item 4).

(a) ATTRIBUTION: shutdown-origin reaps must stamp
    summary="Server restarted — background task stopped" +
    reason="server_shutdown" so a task killed by a server restart is not
    presented as a mysterious failure. Gated on ``startup.is_shutting_down`` at
    every kill site that cannot otherwise know the reap's origin:
    ``stop_session_task``'s ghost reaps, ``cleanup_ended_session``'s force
    reap, and the drain pump's killed-pending payload.

(b) GUARANTEE: ``task_registry.set_terminal_notification_callback`` +
    ``complete_task`` fire a callback EXACTLY ONCE per task (new
    ``mark_terminal_notified`` latch) when the parent's OWN background work
    transitions to error/killed. The chat layer registers the callback once at
    startup; it synthesizes the durable terminal chip (live store + persisted
    ``add_session_message``) and hands off ONE consolidated unpark via
    ``_deliver_unpark_impl`` ONLY when a live pooled client exists and the
    server is not shutting down (durable-chip-only during shutdown).

Tests drive the REAL paths: real ``complete_task`` transitions, the real
``cleanup_ended_session`` force reap, the real ``stop_session_task`` reap
branches, and the real drain pump reader.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.chat_ws_handler as cwh
import app.api.ws_cleanup as ws_cleanup
import app.core.startup as startup
from app.core.live_session_store import live_session_store
from app.core.task_registry import (
    task_registry,
    SERVER_SHUTDOWN_SUMMARY,
    SERVER_SHUTDOWN_REASON,
)
from app.db import database
from app.db.async_database import run_sync
from claude_agent_sdk import TaskUpdatedMessage


SESSION = "sess-guarantee"


def _own_task(task_id="t-1", tool_use_id="tu-1", kind="agent"):
    """Register a running background task the PARENT owns (admission set)."""
    task_registry.start_task(SESSION, task_id, kind, "bg work",
                             tool_use_id=tool_use_id)
    task_registry.mark_background_launch(SESSION, tool_use_id)


@pytest.fixture(autouse=True)
async def _isolate():
    startup.is_shutting_down = False
    task_registry.reset_all()
    ws_cleanup._drain_pumps.clear()
    _saved_window = cwh._UNPARK_BATCH_WINDOW_SECONDS
    cwh._UNPARK_BATCH_WINDOW_SECONDS = 0.05
    yield
    cwh._UNPARK_BATCH_WINDOW_SECONDS = _saved_window
    startup.is_shutting_down = False
    task_registry.reset_all()
    ws_cleanup._drain_pumps.clear()
    await live_session_store.cleanup_ended_session(SESSION, force=True)


def _make_db_session():
    try:
        database.create_session(session_id=SESSION, profile_id="default")
    except Exception:
        pass  # already exists


# ===========================================================================
# (b) Registry guarantee — spy callback semantics
# ===========================================================================

def test_callback_fires_exactly_once_on_killed_not_on_completed():
    spy = MagicMock()
    task_registry.set_terminal_notification_callback(spy)

    _own_task("t-1", "tu-1")
    task_registry.complete_task(SESSION, "t-1", "killed")
    spy.assert_called_once()
    sid, wire = spy.call_args.args
    assert sid == SESSION
    assert wire["id"] == "t-1"
    assert wire["status"] == "killed"

    # Duplicate terminal transition: the mark_terminal_notified latch holds.
    task_registry.complete_task(SESSION, "t-1", "killed")
    spy.assert_called_once()

    # A CLEAN completion never fires the callback.
    _own_task("t-2", "tu-2")
    task_registry.complete_task(SESSION, "t-2", "completed")
    spy.assert_called_once()


def test_callback_fires_on_error_too():
    spy = MagicMock()
    task_registry.set_terminal_notification_callback(spy)
    _own_task("t-err", "tu-err")
    task_registry.complete_task(SESSION, "t-err", "error")
    spy.assert_called_once()


def test_callback_skips_foreground_tasks():
    """Only the parent's OWN background work (the unpark admission set) may
    surface — a foreground task's failure already returned inline."""
    spy = MagicMock()
    task_registry.set_terminal_notification_callback(spy)
    # Foreground: registry record exists but no background launch / sdk mark.
    task_registry.start_task(SESSION, "fg-1", "shell", "foreground cmd",
                             tool_use_id="tu-fg")
    task_registry.complete_task(SESSION, "fg-1", "error")
    spy.assert_not_called()


def test_callback_survives_callback_exception():
    """A raising callback must never break complete_task."""
    def _boom(*a, **k):
        raise RuntimeError("boom")

    task_registry.set_terminal_notification_callback(_boom)
    _own_task("t-1", "tu-1")
    task_registry.complete_task(SESSION, "t-1", "killed")  # must not raise
    rec = task_registry.get_task(SESSION, "t-1")
    assert rec["status"] == "killed"


# ===========================================================================
# (b) Real chat-layer callback: force-eviction reap → exactly one durable
#     terminal surface (RED today: silence)
# ===========================================================================

async def test_force_eviction_reap_surfaces_exactly_one_terminal_row():
    _make_db_session()
    cwh.register_terminal_notification_callback()

    # A REAL parked live session with an owned background agent running.
    await live_session_store.start_streaming(SESSION, initial_messages=[])
    await live_session_store.end_streaming(SESSION, metadata={})
    _own_task("t-reaped", "tu-reaped")

    # Hard-cap force eviction: the pooled client is gone, the reap kills the
    # in-flight agent.
    removed = await live_session_store.cleanup_ended_session(SESSION, force=True)
    assert removed is True

    # Let the scheduled callback surface work run.
    for _ in range(20):
        await asyncio.sleep(0.01)

    rows = await run_sync(database.get_session_messages, SESSION)
    terminal_rows = [
        r for r in rows
        if r["role"] == "system"
        and ((r.get("metadata") or {}).get("system_data") or {}).get("task_id")
        == "t-reaped"
    ]
    assert len(terminal_rows) == 1, (
        f"expected exactly ONE durable terminal surface, got "
        f"{len(terminal_rows)}: {terminal_rows}"
    )
    sysdata = terminal_rows[0]["metadata"]["system_data"]
    assert sysdata.get("status") == "killed"
    assert (r_meta := terminal_rows[0]["metadata"]).get("subtype") == "task_updated", r_meta


async def test_callback_hands_off_one_consolidated_unpark_with_live_client():
    _make_db_session()
    cwh.register_terminal_notification_callback()
    _own_task("t-live", "tu-live")

    state = MagicMock()
    state.adapter.is_alive = MagicMock(return_value=True)
    pool = MagicMock()
    pool.peek = MagicMock(return_value=state)

    deliver = AsyncMock(return_value=True)
    with patch("app.core.session_pool.get_session_pool", return_value=pool), \
         patch.object(cwh, "_deliver_unpark_impl", new=deliver):
        task_registry.complete_task(SESSION, "t-live", "killed")
        for _ in range(20):
            await asyncio.sleep(0.01)
            if deliver.await_count:
                break

    deliver.assert_awaited_once()
    kwargs = deliver.await_args.kwargs
    assert kwargs["session_id"] == SESSION
    pending = kwargs["pending"]
    assert len(pending) == 1 and pending[0]["task_id"] == "t-live"
    assert pending[0]["status"] == "killed"


async def test_callback_durable_chip_only_when_no_live_client():
    """No pooled client → durable chip only, no unpark handoff."""
    _make_db_session()
    cwh.register_terminal_notification_callback()
    _own_task("t-noclient", "tu-noclient")

    pool = MagicMock()
    pool.peek = MagicMock(return_value=None)
    deliver = AsyncMock(return_value=True)
    with patch("app.core.session_pool.get_session_pool", return_value=pool), \
         patch.object(cwh, "_deliver_unpark_impl", new=deliver):
        task_registry.complete_task(SESSION, "t-noclient", "killed")
        for _ in range(10):
            await asyncio.sleep(0.01)

    deliver.assert_not_awaited()
    rows = await run_sync(database.get_session_messages, SESSION)
    assert any(
        r["role"] == "system"
        and ((r.get("metadata") or {}).get("system_data") or {}).get("task_id")
        == "t-noclient"
        for r in rows
    )


async def test_callback_durable_chip_only_during_shutdown():
    """DECISION: durable-chip-only during shutdown — never a new follow-up."""
    _make_db_session()
    cwh.register_terminal_notification_callback()
    _own_task("t-shutdown", "tu-shutdown")

    state = MagicMock()
    state.adapter.is_alive = MagicMock(return_value=True)
    pool = MagicMock()
    pool.peek = MagicMock(return_value=state)

    startup.is_shutting_down = True
    deliver = AsyncMock(return_value=True)
    with patch("app.core.session_pool.get_session_pool", return_value=pool), \
         patch.object(cwh, "_deliver_unpark_impl", new=deliver):
        task_registry.complete_task(SESSION, "t-shutdown", "killed")
        for _ in range(10):
            await asyncio.sleep(0.01)

    deliver.assert_not_awaited()
    rows = await run_sync(database.get_session_messages, SESSION)
    assert any(
        r["role"] == "system"
        and ((r.get("metadata") or {}).get("system_data") or {}).get("task_id")
        == "t-shutdown"
        for r in rows
    )


# ===========================================================================
# (a) Shutdown-origin kill attribution
# ===========================================================================

async def test_stop_session_task_reap_stamps_server_shutdown():
    from app.core.query_session_lifecycle import stop_session_task

    _own_task("t-ghost", "tu-ghost")
    startup.is_shutting_down = True
    with patch("app.core.query_session_lifecycle.get_session_pool") as gp:
        gp.return_value.peek = MagicMock(return_value=None)
        result = await stop_session_task(SESSION, "t-ghost")

    assert result["ok"] is True
    rec = task_registry.get_task(SESSION, "t-ghost")
    assert rec["status"] == "killed"
    assert rec["summary"] == SERVER_SHUTDOWN_SUMMARY
    assert rec["reason"] == SERVER_SHUTDOWN_REASON


async def test_stop_session_task_reap_unstamped_when_not_shutting_down():
    from app.core.query_session_lifecycle import stop_session_task

    _own_task("t-ghost2", "tu-ghost2")
    with patch("app.core.query_session_lifecycle.get_session_pool") as gp:
        gp.return_value.peek = MagicMock(return_value=None)
        result = await stop_session_task(SESSION, "t-ghost2")

    assert result["ok"] is True
    rec = task_registry.get_task(SESSION, "t-ghost2")
    assert rec["status"] == "killed"
    assert rec["summary"] != SERVER_SHUTDOWN_SUMMARY
    assert rec.get("reason") != SERVER_SHUTDOWN_REASON


async def test_force_cleanup_reap_stamps_server_shutdown():
    await live_session_store.start_streaming(SESSION, initial_messages=[])
    await live_session_store.end_streaming(SESSION, metadata={})
    _own_task("t-fc", "tu-fc")

    startup.is_shutting_down = True
    await live_session_store.cleanup_ended_session(SESSION, force=True)

    # clear_session already ran, but the retention snapshot (get_visible)
    # carries the reaped task's wire — attribution must ride it.
    visible = task_registry.get_visible(SESSION)
    reaped = [t for t in visible if t["id"] == "t-fc"]
    assert reaped, "reaped task must stay visible in the retention window"
    assert reaped[0]["status"] == "killed"
    assert reaped[0]["summary"] == SERVER_SHUTDOWN_SUMMARY
    assert reaped[0].get("reason") == SERVER_SHUTDOWN_REASON


async def test_pump_killed_payload_carries_server_shutdown_summary():
    """The pump's killed-pending payload (TaskUpdated-alone terminal) carries
    the shutdown attribution when the reap happens during teardown."""

    class _FakeClient:
        def __init__(self, messages):
            self._messages = list(messages)

        async def receive_messages(self):
            for m in self._messages:
                yield m

    def _fake_state(client):
        state = MagicMock()
        state.is_streaming = False
        state.stale_turn_undrained = False
        state.client = client
        state.adapter.is_alive = MagicMock(return_value=True)
        return state

    _own_task("t-killed", "tu-killed")
    startup.is_shutting_down = True

    handler = cwh.ChatWebSocketHandler(
        websocket=MagicMock(), device_id="dev-1", user_id="admin",
        api_user_id=None,
    )
    handler.send_json = AsyncMock(return_value=True)
    handler._deliver_unpark = AsyncMock(return_value=True)

    killed = TaskUpdatedMessage(
        subtype="task_updated",
        data={"task_id": "t-killed", "patch": {"status": "killed"}},
        task_id="t-killed",
        patch={"status": "killed", "end_time": 1},
        status="killed",
        session_id=SESSION,
        uuid="uu-killed",
    )
    client = _FakeClient([killed])
    pool = MagicMock()
    pool.peek = MagicMock(return_value=_fake_state(client))
    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        await handler._drain_notifications(SESSION, "claude-code", None, None)

    handler._deliver_unpark.assert_awaited_once()
    pending = handler._deliver_unpark.await_args.kwargs["pending"]
    assert len(pending) == 1
    assert pending[0]["task_id"] == "t-killed"
    assert pending[0]["summary"] == SERVER_SHUTDOWN_SUMMARY
