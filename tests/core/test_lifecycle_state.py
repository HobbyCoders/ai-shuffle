"""Unit tests for the derived lifecycle view (app.core.lifecycle_state).

Every scenario is produced by REAL writer sequences — the live store's
start/end_streaming, the task registry's start/complete/clear, the sync
engine's broadcast_stream_start, real sessions/queued_messages rows — never
by planting dicts into private state. The one direct-assignment exception is
the drain-pump registration, because production itself registers pumps by
direct dict assignment (chat_ws_handler -> ws_cleanup._drain_pumps).
"""

import asyncio

from app.core import lifecycle_state
from app.core.lifecycle_state import (
    STATE_IDLE,
    STATE_PARKED_WITH_WORK,
    STATE_STREAMING,
    STATE_WAKING,
    compute,
)
from app.core.live_session_store import live_session_store
from app.core.sync_engine import sync_engine
from app.core.task_registry import task_registry
from app.db import database


def _drift_classes(snap):
    return [entry.split()[0] for entry in snap.drift]


# =============================================================================
# State derivation
# =============================================================================

async def test_idle_state(mock_db):
    snap = await compute("lstate-idle")
    assert snap.state == STATE_IDLE
    assert snap.active_tasks == []
    assert snap.unpark_pending is False
    assert snap.drift == []


async def test_streaming_state(mock_db):
    sid = "lstate-streaming"
    await live_session_store.start_streaming(sid)
    # Real turn also flips the sync engine (stream_to_websocket does both), so
    # a coherent streaming session shows no drift.
    await sync_engine.broadcast_stream_start(sid, "m1")
    try:
        snap = await compute(sid)
    finally:
        await sync_engine.broadcast_stream_end(sid, "m1")
        await live_session_store.end_streaming(sid)
        live_session_store.reset_all()

    assert snap.state == STATE_STREAMING
    assert snap.live_store_streaming is True
    assert snap.sync_streaming is True
    assert "class=sync-vs-livestore" not in _drift_classes(snap)


async def test_parked_with_work_state(mock_db):
    sid = "lstate-parked"
    database.create_session(session_id=sid, profile_id="default")
    await live_session_store.start_streaming(sid)
    task_registry.start_task(sid, "t1", "agent", "bg work")
    await live_session_store.end_streaming(sid)
    try:
        snap = await compute(sid)
    finally:
        live_session_store.reset_all()
        task_registry.clear_session(sid)

    assert snap.state == STATE_PARKED_WITH_WORK
    assert [t["id"] for t in snap.active_tasks] == ["t1"]
    assert [t["id"] for t in snap.background_work] == ["t1"]
    # end_streaming persisted the durable parked marker for the live bg work.
    assert snap.parked_task_kind == "agent"
    # Marker + parked state agree: no parked-marker drift.
    assert "class=parked-marker-while-idle" not in _drift_classes(snap)


async def test_waking_state_via_unpark_latch(mock_db):
    sid = "lstate-waking"
    task_registry.mark_unpark_pending(sid)
    try:
        snap = await compute(sid)
    finally:
        task_registry.clear_unpark_pending(sid)

    assert snap.state == STATE_WAKING
    assert snap.unpark_latch is True
    assert snap.unpark_pending is True
    # Latch set -> the queued-messages keep-alive is short-circuited (verbatim
    # /live ordering).
    assert snap.queued_count is None


async def test_waking_state_via_queued_messages(mock_db):
    sid = "lstate-waking-queue"
    database.create_session(session_id=sid, profile_id="default")
    database.add_queued_message(sid, "queued follow-up")

    snap = await compute(sid)

    assert snap.state == STATE_WAKING
    assert snap.unpark_latch is False
    assert snap.unpark_pending is True
    assert snap.queued_count == 1


async def test_waking_outranks_parked_with_work(mock_db):
    """Both a committed follow-up AND visible work: the committed follow-up is
    the stronger claim about what happens next."""
    sid = "lstate-precedence"
    task_registry.start_task(sid, "t1", "agent", "bg work")
    task_registry.mark_unpark_pending(sid)
    try:
        snap = await compute(sid)
    finally:
        task_registry.clear_unpark_pending(sid)
        task_registry.clear_session(sid)

    assert snap.state == STATE_WAKING
    assert snap.active_tasks  # the work is still visible in the snapshot


async def test_streaming_outranks_everything(mock_db):
    sid = "lstate-stream-precedence"
    await live_session_store.start_streaming(sid)
    task_registry.start_task(sid, "t1", "agent", "bg work")
    task_registry.mark_unpark_pending(sid)
    try:
        snap = await compute(sid)
    finally:
        task_registry.clear_unpark_pending(sid)
        task_registry.clear_session(sid)
        await live_session_store.end_streaming(sid)
        live_session_store.reset_all()

    assert snap.state == STATE_STREAMING


# =============================================================================
# Drift sensor
# =============================================================================

async def test_drift_sync_vs_livestore(mock_db):
    """sync_engine says streaming, live store says idle — the classic stale
    _streaming_sessions leak after a crashed turn."""
    sid = "lstate-drift-sync"
    await sync_engine.broadcast_stream_start(sid, "m1")
    try:
        snap = await compute(sid)
    finally:
        await sync_engine.clear_streaming_state(sid)

    assert snap.sync_streaming is True
    assert snap.live_store_streaming is False
    assert "class=sync-vs-livestore" in _drift_classes(snap)


async def test_drift_parked_marker_while_idle(mock_db):
    """end_streaming wrote the durable parked marker, then force-cleanup wiped
    the registry (clear_session) without the marker ever being cleared."""
    sid = "lstate-drift-marker"
    database.create_session(session_id=sid, profile_id="default")
    await live_session_store.start_streaming(sid)
    task_registry.start_task(sid, "t1", "agent", "bg work")
    await live_session_store.end_streaming(sid)  # writes parked_task_kind
    task_registry.clear_session(sid)  # force-cleanup wipe: marker now orphaned
    live_session_store.reset_all()

    snap = await compute(sid)

    assert snap.state == STATE_IDLE
    assert snap.parked_task_kind == "agent"
    assert "class=parked-marker-while-idle" in _drift_classes(snap)


async def test_drift_unpark_latch_without_pump(mock_db):
    sid = "lstate-drift-unpark"
    task_registry.mark_unpark_pending(sid)
    try:
        snap = await compute(sid)
    finally:
        task_registry.clear_unpark_pending(sid)

    assert snap.pump_running is False
    assert "class=unpark-no-pump" in _drift_classes(snap)


async def test_no_unpark_drift_while_pump_runs(mock_db):
    """A live drain pump makes the latch coherent — no drift. (Registration by
    direct dict assignment mirrors the production writer exactly.)"""
    from app.api import ws_cleanup

    sid = "lstate-pump-ok"
    task_registry.mark_unpark_pending(sid)
    pump = asyncio.create_task(asyncio.sleep(30))
    ws_cleanup._drain_pumps[sid] = pump  # noqa: SLF001 — production writer shape
    try:
        snap = await compute(sid)
    finally:
        ws_cleanup._drain_pumps.pop(sid, None)
        pump.cancel()
        task_registry.clear_unpark_pending(sid)

    assert snap.pump_running is True
    assert "class=unpark-no-pump" not in _drift_classes(snap)


async def test_drift_background_work_with_dead_pool_client(mock_db):
    """Registry says a background agent is running, but no pooled SDK client
    exists to deliver its completion — the strand-forever precursor."""
    sid = "lstate-drift-deadclient"
    task_registry.start_task(sid, "t1", "agent", "bg work")
    try:
        snap = await compute(sid)
    finally:
        task_registry.clear_session(sid)

    assert snap.background_work
    assert snap.pool_present is False
    assert "class=bg-work-dead-client" in _drift_classes(snap)


async def test_monitor_shell_does_not_trip_dead_client_drift(mock_db):
    """App-managed Monitor shells (kind=shell, NOT sdk-backgrounded) are
    independent of the pooled client and must not read as strandable work."""
    sid = "lstate-monitor-shell"
    task_registry.start_task(sid, "sh1", "shell", "npm run dev")
    try:
        snap = await compute(sid)
    finally:
        task_registry.clear_session(sid)

    assert snap.state == STATE_PARKED_WITH_WORK  # still visible on the taskbar
    assert snap.background_work == []  # but it parks nothing
    assert "class=bg-work-dead-client" not in _drift_classes(snap)


# =============================================================================
# Timestamped unpark latch (reconciler age-gate input)
# =============================================================================

def test_unpark_pending_age_lifecycle():
    sid = "lstate-latch-age"
    assert task_registry.unpark_pending_age(sid) is None
    task_registry.mark_unpark_pending(sid)
    age = task_registry.unpark_pending_age(sid)
    assert age is not None and 0.0 <= age < 5.0
    task_registry.clear_unpark_pending(sid)
    assert task_registry.unpark_pending_age(sid) is None


def test_unpark_pending_age_cleared_with_session():
    sid = "lstate-latch-clear-session"
    task_registry.mark_unpark_pending(sid)
    task_registry.clear_session(sid)
    assert task_registry.is_unpark_pending(sid) is False
    assert task_registry.unpark_pending_age(sid) is None


def test_unpark_pending_age_re_mark_resets_clock():
    sid = "lstate-latch-remark"
    task_registry.mark_unpark_pending(sid)
    task_registry._unpark_pending_since[sid] -= 100.0  # age it
    assert task_registry.unpark_pending_age(sid) >= 100.0
    task_registry.mark_unpark_pending(sid)  # re-latch resets the clock
    try:
        assert task_registry.unpark_pending_age(sid) < 5.0
    finally:
        task_registry.clear_unpark_pending(sid)


async def test_session_row_passthrough_avoids_db_fetch(mock_db, monkeypatch):
    """When the caller passes the already-fetched sessions row (the /live hot
    path), compute must not re-read it."""

    def _must_not_run(*a, **k):  # pragma: no cover - failure path
        raise AssertionError("get_session must not be re-fetched")

    monkeypatch.setattr(lifecycle_state.database, "get_session", _must_not_run)

    snap = await compute(
        "lstate-row-pass", session_row={"id": "lstate-row-pass", "parked_task_kind": "agent"}
    )
    assert snap.parked_task_kind == "agent"
