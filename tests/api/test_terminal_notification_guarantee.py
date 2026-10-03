"""Kill attribution + guaranteed terminal notification (lifecycle batch, item 4).

(a) ATTRIBUTION: shutdown-origin reaps must stamp
    summary="Server restarted — background task stopped" +
    reason="server_shutdown" so a task killed by a server restart is not
    presented as a mysterious failure. Gated on ``startup.is_shutting_down`` at
    every kill site that cannot otherwise know the reap's origin:
    ``stop_session_task``'s ghost reaps, the force-eviction reap, and the
    drain pump's killed-pending payload.

(b) GUARANTEE: ``task_registry.set_terminal_notification_callback`` +
    ``complete_task`` fire a callback EXACTLY ONCE per task (new
    ``mark_terminal_notified`` latch) when the parent's OWN background work
    transitions to error/killed. The chat layer registers the callback once at
    startup; it synthesizes the durable terminal chip (persisted
    ``add_session_message``) plus the live task_updated event.

    V2 A7 (B5): the legacy STEP 3 — the ``HeadlessChatHandler`` unpark handoff
    via ``_deliver_unpark_impl`` — is STRIPPED from
    ``_surface_terminal_notification``. The v2 scheduler
    (idle_reader → turn_scheduler.on_task_terminal) is now the sole unpark
    producer, so this surface ONLY records (chip + durable row); it never spawns
    a runner or hands off an unpark, live client or not. The accepted flag-OFF
    regression: a user-stop of a live background sub-agent no longer auto-wakes
    the parent, but the durable chip still records it.

V2 (Phase-1 deletion): ``LiveSessionStore.cleanup_ended_session`` — the old
force-reap entry point — is deleted with the live store; the force reap now
lives in ``resource_monitor._cleanup_evicted_sessions`` (reason
"dead"), and that is what the force-reap tests here drive.

Tests drive the REAL paths: real ``complete_task`` transitions, the real
force-eviction reap, the real ``stop_session_task`` reap branches, and the
real drain pump reader.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.api.chat_ws_handler as cwh
import app.core.startup as startup
from app.core import resource_monitor
from app.core import task_persistence
from app.core.task_registry import (
    task_registry,
    SERVER_SHUTDOWN_SUMMARY,
    SERVER_SHUTDOWN_REASON,
    USER_STOP_SUMMARY,
    USER_STOP_REASON,
)
from app.db import database
from app.db.async_database import run_sync
from app.core.provider.events import TaskUpdatedMessage


SESSION = "sess-guarantee"


def _own_task(task_id="t-1", tool_use_id="tu-1", kind="agent"):
    """Register a running background task the PARENT owns (admission set)."""
    task_registry.start_task(SESSION, task_id, kind, "bg work",
                             tool_use_id=tool_use_id)
    task_registry.mark_background_launch(SESSION, tool_use_id)


@pytest.fixture(autouse=True)
async def _isolate(monkeypatch):
    from app.core import turn_scheduler

    # These tests inspect queued delivery; do not launch a live engine turn.
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)
    startup.is_shutting_down = False
    task_registry.reset_all()
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
        startup.is_shutting_down = False
        task_registry.reset_all()


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

    # A parked session with an owned background agent running.
    _own_task("t-reaped", "tu-reaped")

    # Hard-cap force eviction: the pooled client is gone, the reap kills the
    # in-flight agent. (V2: resource_monitor._cleanup_evicted_sessions is the
    # force-reap entry point — reason "dead" = subprocess destroyed.)
    await resource_monitor._cleanup_evicted_sessions([SESSION], {}, "dead")

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


async def test_terminal_surface_carries_tool_use_id_join_key():
    """The subagent CARD settles only when the terminal ``task_updated`` chip
    carries the launch ``tool_use_id`` (the card's ``toolId``). Both the LIVE chip
    (``record_drained_event``) and the DURABLE row must carry it — they share the
    same ``data`` dict (mirror of the #540 enrichment)."""
    _make_db_session()
    cwh.register_terminal_notification_callback()
    _own_task("t-join", "tu-join")

    drained = AsyncMock()
    with patch("app.core.turn_recorder.record_drained_event", drained):
        task_registry.complete_task(SESSION, "t-join", "killed")
        await _drain_terminal_surface()

    # (1) LIVE chip carries the join key.
    drained.assert_awaited()
    live_evt = drained.call_args.args[1]
    assert live_evt["data"]["tool_use_id"] == "tu-join"

    # (2) DURABLE row carries the join key too.
    rows = await _terminal_rows("t-join")
    assert len(rows) == 1, f"expected exactly ONE durable terminal row, got {len(rows)}"
    assert rows[0]["metadata"]["system_data"]["tool_use_id"] == "tu-join"


async def test_terminal_surface_carries_task_id():
    """The chip ``data`` dict must carry ``task_id`` (alongside ``tool_use_id``)
    so turn_recorder's task.updated translation populates it — the reducer bails
    on an empty task_id and the agent's summary would never reach its card."""
    _make_db_session()
    cwh.register_terminal_notification_callback()
    _own_task("t-tid", "tu-tid")

    drained = AsyncMock()
    with patch("app.core.turn_recorder.record_drained_event", drained):
        task_registry.complete_task(SESSION, "t-tid", "killed")
        await _drain_terminal_surface()

    # (1) LIVE chip carries both task_id and the tool_use_id join key.
    drained.assert_awaited()
    live_data = drained.call_args.args[1]["data"]
    assert live_data["task_id"] == "t-tid"
    assert live_data["tool_use_id"] == "tu-tid"

    # (2) DURABLE row carries them too.
    rows = await _terminal_rows("t-tid")
    assert len(rows) == 1, f"expected exactly ONE durable terminal row, got {len(rows)}"
    sys_data = rows[0]["metadata"]["system_data"]
    assert sys_data["task_id"] == "t-tid"
    assert sys_data["tool_use_id"] == "tu-tid"


async def test_callback_records_only_no_unpark_handoff_even_with_live_client():
    """V2 A7 (B5): the terminal surface NO LONGER hands off an unpark, even when
    a live pooled client exists — the scheduler is the sole unpark producer. The
    kill is still recorded durably (chip + row); ``_deliver_unpark_impl`` is
    never called and no runner is spawned."""
    _make_db_session()
    cwh.register_terminal_notification_callback()
    _own_task("t-live", "tu-live")

    state = MagicMock()
    state.adapter.is_alive = MagicMock(return_value=True)
    pool = MagicMock()
    pool.peek = MagicMock(return_value=state)

    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        task_registry.complete_task(SESSION, "t-live", "killed")
        await _drain_terminal_surface()

    # No unpark handoff (step 3 stripped, ``_deliver_unpark_impl`` deleted in
    # PR-B) — but the durable terminal row still lands.
    rows = await _terminal_rows("t-live")
    assert len(rows) == 1, f"expected exactly ONE durable terminal row, got {len(rows)}"
    assert rows[0]["metadata"]["system_data"].get("status") == "killed"


def test_surface_terminal_notification_records_only_never_spawns():
    """Source guard: the terminal surface keeps steps 1 (live chip) & 2 (durable
    row) but the step-3 unpark handoff is gone — its body must not reference the
    deleted ``HeadlessChatHandler`` / ``StreamingContext`` husk nor call
    ``_deliver_unpark_impl``, so it can never spawn a runner."""
    import inspect

    src = inspect.getsource(cwh._surface_terminal_notification)
    # Steps that must remain (the guaranteed terminal SURFACE):
    assert "mark_chipped" in src, "step 1 (live chip) must remain"
    assert "add_session_message" in src, "step 2 (durable row) must remain"
    # The stripped step-3 handoff must be absent from the executable body.
    body = src.split('"""', 2)[-1]  # drop the docstring (which explains the strip)
    assert "_deliver_unpark_impl(" not in body, "step 3 unpark handoff must be gone"
    assert "HeadlessChatHandler(" not in body, "must not instantiate a headless runner"


async def test_callback_durable_chip_only_when_no_live_client():
    """No pooled client → durable chip only, no unpark handoff."""
    _make_db_session()
    cwh.register_terminal_notification_callback()
    _own_task("t-noclient", "tu-noclient")

    pool = MagicMock()
    pool.peek = MagicMock(return_value=None)
    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        task_registry.complete_task(SESSION, "t-noclient", "killed")
        await _drain_terminal_surface()

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
    with patch("app.core.session_pool.get_session_pool", return_value=pool):
        task_registry.complete_task(SESSION, "t-shutdown", "killed")
        await _drain_terminal_surface()

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
    _own_task("t-fc", "tu-fc")

    startup.is_shutting_down = True
    await resource_monitor._cleanup_evicted_sessions([SESSION], {}, "dead")

    # clear_session already ran, but the retention snapshot (get_visible)
    # carries the reaped task's wire — attribution must ride it.
    visible = task_registry.get_visible(SESSION)
    reaped = [t for t in visible if t["id"] == "t-fc"]
    assert reaped, "reaped task must stay visible in the retention window"
    assert reaped[0]["status"] == "killed"
    assert reaped[0]["summary"] == SERVER_SHUTDOWN_SUMMARY
    assert reaped[0].get("reason") == SERVER_SHUTDOWN_REASON


# ===========================================================================
# (c) USER-initiated stop of a LIVE background sub-agent must mark the
#     registry row terminal ITSELF, with user attribution — the adapter has no
#     per-task stop and no CLI "stopped" frame flows back. Exactly-once
#     terminal surface.
# ===========================================================================


def _live_agent_state():
    """A mock pooled state for a LIVE, connected session."""
    state = MagicMock()
    state.is_connected = True
    return state


async def test_stop_session_task_live_agent_marks_registry_killed():
    """User "x" on a RUNNING background agent on a LIVE session: the registry
    row reaches ``killed`` with user attribution."""
    from app.core.query_session_lifecycle import stop_session_task

    _own_task("t-userstop", "tu-userstop")
    assert task_registry.get_task(SESSION, "t-userstop")["status"] == "running"

    with patch("app.core.query_session_lifecycle.get_session_pool") as gp:
        gp.return_value.peek = MagicMock(return_value=_live_agent_state())
        result = await stop_session_task(SESSION, "t-userstop")

    assert result["ok"] is True and result["kind"] == "agent"
    rec = task_registry.get_task(SESSION, "t-userstop")
    assert rec["status"] == "killed", rec
    assert rec["summary"] == USER_STOP_SUMMARY
    assert rec.get("reason") == USER_STOP_REASON
    # It has dropped off the running-only /live control view.
    assert all(t["id"] != "t-userstop" for t in task_registry.get_active(SESSION))


async def test_stop_session_task_live_agent_surfaces_terminal_exactly_once():
    """The user stop drives the SAME guaranteed terminal surface as a #514 kill:
    exactly one durable terminal row, carrying user attribution — and a later
    CLI ``stopped`` frame for the same task must NOT double-surface."""
    from app.core.query_engine import _record_task_message
    from app.core.query_session_lifecycle import stop_session_task

    _make_db_session()
    cwh.register_terminal_notification_callback()
    _own_task("t-once", "tu-once")

    with patch("app.core.query_session_lifecycle.get_session_pool") as gp:
        gp.return_value.peek = MagicMock(return_value=_live_agent_state())
        result = await stop_session_task(SESSION, "t-once")
    assert result["ok"] is True

    # A CLI "stopped" TaskUpdated arriving LATE must be a no-op.
    late = TaskUpdatedMessage(
        subtype="task_updated",
        data={"task_id": "t-once", "patch": {"status": "killed"}},
        task_id="t-once",
        patch={"status": "killed", "end_time": 1},
        status="killed",
        session_id=SESSION,
        uuid="uu-late",
    )
    _record_task_message(SESSION, late)

    await _drain_terminal_surface()
    rows = await _terminal_rows("t-once")
    assert len(rows) == 1, f"expected exactly ONE terminal surface, got {len(rows)}"
    # The ROW keeps the registry vocabulary; the CHIP says the stop was
    # deliberate, so a task the user stopped does not read as a crash.
    assert rows[0]["metadata"]["system_data"].get("status") == "cancelled"
    assert rows[0]["metadata"]["system_data"].get("reason") == USER_STOP_REASON
    assert task_registry.get_task(SESSION, "t-once")["status"] == "killed"


async def test_stop_session_task_live_agent_calls_terminal_callback_once():
    """Registry-level: the live-agent user stop fires the terminal-notification
    callback exactly once (mirrors the #514 exactly-once guarantee)."""
    from app.core.query_session_lifecycle import stop_session_task

    spy = MagicMock()
    task_registry.set_terminal_notification_callback(spy)
    _own_task("t-cb", "tu-cb")

    with patch("app.core.query_session_lifecycle.get_session_pool") as gp:
        gp.return_value.peek = MagicMock(return_value=_live_agent_state())
        await stop_session_task(SESSION, "t-cb")

    spy.assert_called_once()
    _, wire = spy.call_args.args
    assert wire["id"] == "t-cb"
    assert wire["status"] == "killed"
    assert wire.get("reason") == USER_STOP_REASON


async def test_unexpected_kill_without_reader_queues_once_but_stop_never_wakes(monkeypatch):
    from app.core import turn_scheduler
    from app.db import turns

    _make_db_session()
    monkeypatch.setattr(turn_scheduler, "_kick_after_batch_window", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "_try_steer_terminal_notification", AsyncMock(return_value=False))
    cwh.register_terminal_notification_callback()
    _own_task("unexpected", "tu-unexpected")
    task_registry.complete_task(SESSION, "unexpected", "killed", summary="Process disappeared")
    await _drain_terminal_surface()
    (wake,) = [t for t in turns.get_queued_turns(SESSION) if t["source"] == "unpark"]
    assert wake["payload"]["task_ids"] == ["unexpected"]
    assert "Process disappeared" in wake["prompt"]
    # A reader arriving later cannot consume the same terminal twice.
    await turn_scheduler.on_task_terminal(SESSION, {"task_id": "unexpected", "status": "killed", "summary": "Process disappeared"})
    assert len([t for t in turns.get_queued_turns(SESSION) if t["source"] == "unpark"]) == 1
    _own_task("explicit-stop", "tu-stop")
    task_registry.suppress_wake(SESSION, "explicit-stop")
    task_registry.complete_task(SESSION, "explicit-stop", "killed", reason="user_stopped")
    await _drain_terminal_surface()
    assert len([t for t in turns.get_queued_turns(SESSION) if t["source"] == "unpark"]) == 1
