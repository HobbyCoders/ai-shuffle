"""Unit tests for the derived lifecycle view (app.core.lifecycle_state).

Every scenario is produced by REAL writer sequences — a claimed run in the
durable ``runs`` table (the streaming authority since the v2 cutover), the
task registry's start/complete/clear, the durable parked marker written by
``turn_finalize.persist_turn_end_state`` (V2: the live store and its
end_streaming marker write are deleted), real sessions/turns rows — never by
planting dicts into private state. One narrow exception: the pool-peek union
test (the pooled SDK client cannot be spawned in a unit test).
"""

import pytest

from app.core import lifecycle_state
from app.core.lifecycle_state import (
    STATE_IDLE,
    STATE_PARKED_WITH_WORK,
    STATE_STREAMING,
    STATE_WAKING,
    compute,
)
from app.core.task_registry import task_registry
from app.core.turn_finalize import persist_turn_end_state
from app.db import database
from app.db import runs


@pytest.fixture(autouse=True)
def _fresh_streaming_status():
    """The runs-backed busy view keeps a module-global TTL cache — isolate it."""
    from app.core import streaming_status

    streaming_status._reset_state()
    yield
    streaming_status._reset_state()


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
    # A real turn's busy truth: a lease-valid active run in the durable runs
    # table (claimed by the TurnRecorder at turn start).
    runs.claim_run(sid, "runner-1", 60)

    snap = await compute(sid)

    assert snap.state == STATE_STREAMING
    assert snap.live_store_streaming is True


async def test_expired_lease_reads_idle(mock_db):
    """An active run whose lease lapsed (crashed runner) is NOT streaming."""
    sid = "lstate-expired-lease"
    runs.claim_run(sid, "runner-1", -1)  # lease already expired on insert

    snap = await compute(sid)

    assert snap.live_store_streaming is False
    assert snap.state == STATE_IDLE


async def test_pool_peek_unions_into_streaming(mock_db, monkeypatch):
    """Belt-and-braces: a turn whose run claim FAILED records unfenced and reads
    idle from the runs table, but the pooled client's is_streaming peek still
    knows a turn is in flight — the union must classify it as streaming."""
    from app.core import session_pool as session_pool_module

    class _State:
        is_connected = True
        is_streaming = True

    class _Pool:
        def peek(self, sid):
            return _State()

    monkeypatch.setattr(
        session_pool_module, "get_session_pool", lambda: _Pool()
    )

    snap = await compute("lstate-pool-union")  # no run row at all

    assert snap.pool_streaming is True
    assert snap.state == STATE_STREAMING


async def test_parked_with_work_state(mock_db):
    sid = "lstate-parked"
    database.create_session(session_id=sid, profile_id="default")
    task_registry.start_task(sid, "t1", "agent", "bg work")
    # V2: the turn-end finalize persists the durable parked marker for the
    # live background work (the live store's end_streaming write is deleted).
    await persist_turn_end_state(sid)
    try:
        snap = await compute(sid)
    finally:
        task_registry.clear_session(sid)

    assert snap.state == STATE_PARKED_WITH_WORK
    assert [t["id"] for t in snap.active_tasks] == ["t1"]
    assert [t["id"] for t in snap.background_work] == ["t1"]
    # The finalize persisted the durable parked marker for the live bg work.
    assert snap.parked_task_kind == "agent"
    # Marker + parked state agree: no parked-marker drift.
    assert "class=parked-marker-while-idle" not in _drift_classes(snap)


async def test_waking_outranks_parked_with_work(mock_db):
    """Both a committed follow-up AND visible work: the committed follow-up is
    the stronger claim about what happens next. V2: the wake signal is a queued
    ``source=unpark`` turn (the legacy registry latch is deleted)."""
    from app.db import turns as turns_db

    sid = "lstate-precedence"
    task_registry.start_task(sid, "t1", "agent", "bg work")
    turns_db.enqueue_turn(sid, "unpark", prompt="<task-notification>...")
    try:
        snap = await compute(sid)
    finally:
        task_registry.clear_session(sid)

    assert snap.state == STATE_WAKING
    assert snap.active_tasks  # the work is still visible in the snapshot


async def test_streaming_outranks_everything(mock_db):
    from app.db import turns as turns_db

    sid = "lstate-stream-precedence"
    runs.claim_run(sid, "runner-1", 60)
    task_registry.start_task(sid, "t1", "agent", "bg work")
    turns_db.enqueue_turn(sid, "unpark", prompt="<task-notification>...")
    try:
        snap = await compute(sid)
    finally:
        task_registry.clear_session(sid)

    assert snap.state == STATE_STREAMING


# =============================================================================
# V2 turns — additive lifecycle derivations (D3)
# =============================================================================

async def test_waking_state_via_queued_unpark_turn(mock_db):
    """A queued ``source=unpark`` turn (a wake is imminent) reads as waking and
    folds into the composed ``unpark_pending`` wire value."""
    from app.db import turns as turns_db

    sid = "lstate-waking-unpark-turn"
    turns_db.enqueue_turn(sid, "unpark", prompt="<task-notification>...")

    snap = await compute(sid)

    assert snap.state == STATE_WAKING
    assert snap.unpark_pending is True


async def test_queued_user_turn_is_not_waking(mock_db):
    """Only unpark queued turns trigger waking; a queued user turn does not."""
    from app.db import turns as turns_db

    sid = "lstate-queued-user"
    turns_db.enqueue_turn(sid, "user", prompt="p")

    snap = await compute(sid)

    assert snap.state == STATE_IDLE


async def test_parked_with_work_via_parked_turn(mock_db):
    """A parked turn (foreground done, background work survives) reads as
    parked_with_work even when the registry has no visible tasks."""
    from app.db import turns as turns_db

    sid = "lstate-parked-turn"
    tid = turns_db.enqueue_turn(sid, "user", prompt="p")["id"]
    turns_db.start_turn(tid, run_id="run-x")
    turns_db.park_turn(tid, ["task-1"])

    snap = await compute(sid)

    assert snap.state == STATE_PARKED_WITH_WORK


# =============================================================================
# Drift sensor
# =============================================================================

def test_sync_vs_livestore_sensor_stays_deleted():
    """V2 Phase-1 deletion tripwire: the legacy busy sources (sync_engine's
    ``_streaming_sessions`` map, the live store) are deleted, and with them the
    ``class=sync-vs-livestore`` drift sensor and the snapshot's
    ``sync_streaming`` field. The runs table (unioned with the pool peek) is
    the ONE busy truth — reintroducing a second source here would resurrect
    the split-brain the sensor existed to catch, without the sensor."""
    import inspect

    src = inspect.getsource(lifecycle_state)
    assert "sync-vs-livestore" not in src
    assert "sync_streaming" not in src
    assert "sync_engine" not in src
    from app.core.lifecycle_state import LifecycleSnapshot

    assert "sync_streaming" not in LifecycleSnapshot.__dataclass_fields__


async def test_drift_logs_once_per_transition(mock_db, caplog):
    """Drift logs fire on TRANSITION (newly-appears), not per poll: a persistent
    drift condition sampled repeatedly emits exactly one ``lifecycle-drift``
    line until it clears or the hourly re-log window elapses. (Exercised via
    ``class=bg-work-dead-client`` — a surviving drift class.)"""
    import logging

    lifecycle_state.reset_drift_log_state()
    sid = "lstate-drift-transition"
    # Running background agent with no pooled SDK client → persistent drift.
    task_registry.start_task(sid, "t1", "agent", "bg work")
    try:
        with caplog.at_level(logging.INFO, logger="app.core.lifecycle_state"):
            for _ in range(5):
                snap = await compute(sid)
        assert "class=bg-work-dead-client" in _drift_classes(snap)
        drift_lines = [
            r for r in caplog.records
            if r.getMessage().startswith("lifecycle-drift session=" + sid)
        ]
        assert len(drift_lines) == 1, (
            f"expected one transition log, got {len(drift_lines)}: "
            f"{[r.getMessage() for r in drift_lines]}"
        )
    finally:
        task_registry.clear_session(sid)
        lifecycle_state.reset_drift_log_state()


async def test_drift_relogs_after_clearing(mock_db, caplog):
    """When a drift condition clears and later reappears, the reappearance is a
    fresh transition and logs again."""
    import logging

    lifecycle_state.reset_drift_log_state()
    sid = "lstate-drift-reappear"
    try:
        with caplog.at_level(logging.INFO, logger="app.core.lifecycle_state"):
            task_registry.start_task(sid, "t1", "agent", "bg work")
            await compute(sid)          # transition 1: appears -> logs
            await compute(sid)          # still standing -> suppressed
            task_registry.clear_session(sid)
            await compute(sid)          # cleared -> forgotten, no drift
            task_registry.start_task(sid, "t1", "agent", "bg work")
            await compute(sid)          # transition 2: reappears -> logs
        drift_lines = [
            r for r in caplog.records
            if r.getMessage().startswith("lifecycle-drift session=" + sid)
        ]
        assert len(drift_lines) == 2, [r.getMessage() for r in drift_lines]
    finally:
        task_registry.clear_session(sid)
        lifecycle_state.reset_drift_log_state()


async def test_drift_parked_marker_while_idle(mock_db):
    """The turn-end finalize wrote the durable parked marker, then force-cleanup
    wiped the registry (clear_session) without the marker ever being cleared."""
    sid = "lstate-drift-marker"
    database.create_session(session_id=sid, profile_id="default")
    task_registry.start_task(sid, "t1", "agent", "bg work")
    await persist_turn_end_state(sid)  # writes parked_task_kind="agent"
    task_registry.clear_session(sid)  # force-cleanup wipe: marker now orphaned

    snap = await compute(sid)

    assert snap.state == STATE_IDLE
    assert snap.parked_task_kind == "agent"
    assert "class=parked-marker-while-idle" in _drift_classes(snap)


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
