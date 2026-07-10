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
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.chat_ws_handler as cwh
import app.api.ws_cleanup as ws_cleanup
import app.core.startup as startup
from app.core import task_persistence
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
    # ROOT-CAUSE ISOLATION (cross-test flake): every ``start_task`` /
    # ``complete_task`` also enqueues a durable ``task_persistence`` snapshot,
    # whose consumer writes to the DB on its OWN ``asyncio.to_thread``. The test
    # harness routes the whole DB layer at ONE shared ``check_same_thread=False``
    # sqlite connection, so that snapshot write runs concurrently with the
    # terminal-surface coroutine's own ``to_thread`` durable write — two worker
    # threads committing on one connection. One commit then trips
    # ``sqlite3.OperationalError: cannot commit transaction - SQL statements in
    # progress``; the surface write swallows it and drops the ``session_messages``
    # row this file asserts on. The collision is timing-dependent, so it only
    # bit under full-suite CPU load (the tests passed when run alone).
    # ``task_persistence`` is orthogonal to the terminal-surface guarantee — it
    # writes the ``background_tasks`` table, never ``session_messages`` — so
    # disable it here to give the surface write exclusive use of the connection.
    with patch.object(task_persistence, "is_enabled", return_value=False):
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


async def _drain_terminal_surface(*, timeout: float = 5.0):
    """Await the fire-and-forget terminal-surface coroutine to completion.

    ``complete_task`` fires the registry callback SYNCHRONOUSLY, and
    ``_on_task_terminal`` schedules ``_surface_terminal_notification`` via
    ``loop.create_task`` — so by the time ``complete_task`` (or the
    force-eviction reap that calls it) returns, the surface task already exists
    on this loop. The surface coroutine writes its durable row from a worker
    thread (``run_sync`` → ``asyncio.to_thread``).

    The original tests slept a FIXED, short budget (0.1–0.2s) and then read the
    row; under full-suite CPU load that budget was too small, so the write had
    not landed yet → the test flaked (0 rows). Polling the DB to wait for the
    row is NOT a fix here: the shim shares ONE ``check_same_thread=False``
    sqlite connection, so a poll-read issued from a worker thread races the
    surface write on that same connection and the (swallowed) write loses.
    Instead, await the surface coroutine ITSELF — deterministic under any load,
    and no concurrent DB access to corrupt the single-connection write.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = [
            t for t in asyncio.all_tasks()
            if t is not asyncio.current_task()
            and not t.done()
            and getattr(t.get_coro(), "__name__", "")
            == "_surface_terminal_notification"
        ]
        if not pending:
            return
        await asyncio.wait(pending, timeout=deadline - time.monotonic())


async def _terminal_rows(task_id: str):
    """Durable ``system`` terminal rows the surface callback persisted for ``task_id``."""
    rows = await run_sync(database.get_session_messages, SESSION)
    return [
        r for r in rows
        if r["role"] == "system"
        and ((r.get("metadata") or {}).get("system_data") or {}).get("task_id")
        == task_id
    ]


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

    # Await the scheduled surface coroutine rather than guessing a fixed sleep.
    await _drain_terminal_surface()
    terminal_rows = await _terminal_rows("t-reaped")
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
        await _drain_terminal_surface()

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
        await _drain_terminal_surface()

    deliver.assert_not_awaited()
    assert await _terminal_rows("t-noclient"), (
        "expected a durable terminal chip for t-noclient"
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
        await _drain_terminal_surface()

    deliver.assert_not_awaited()
    assert await _terminal_rows("t-shutdown"), (
        "expected a durable terminal chip for t-shutdown"
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
