"""Runtime lifecycle reconciler tests (app.core.lifecycle_reconcile).

Drift is created via REAL partial writer sequences — the exact production
half-writes that strand state (mark_unpark_pending with no pump; the turn-end
finalize's parked marker orphaned by a registry force-wipe; a running
background agent whose pooled client is gone) — never by planting dicts. Each
repair test asserts both the repair AND its durable record_error trail; the
negative controls assert that a GENUINE fresh transient (a just-latched waking
session, a first sighting inside the grace window) is NOT repaired.

V2 (Phase-1 deletion): busy truth comes from the durable ``runs`` table (plus
the pool peek), the parked marker is written by
``turn_finalize.persist_turn_end_state``, and R3 (stale sync-engine streaming
map) is RETIRED with the legacy in-memory maps — a tripwire below pins the
retirement.
"""

import pytest

from app.core import lifecycle_reconcile as lr
from app.core.task_registry import task_registry
from app.core.turn_finalize import persist_turn_end_state
from app.db import database
from app.db import runs


@pytest.fixture(autouse=True)
def _fresh_reconciler(monkeypatch):
    """Zero the age gates (confirm-twice still applies) and reset bookkeeping."""
    from app.core import streaming_status

    lr.reset()
    streaming_status._reset_state()  # the runs-backed busy view's TTL cache
    monkeypatch.setattr(lr, "GRACE_SECONDS", 0.0)
    monkeypatch.setattr(lr, "UNPARK_LATCH_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(lr, "ON_DEMAND_DEBOUNCE_SECONDS", 0.0)
    yield
    lr.reset()
    streaming_status._reset_state()


def _error_rows(mock_db, needle):
    rows = mock_db.execute(
        "SELECT message FROM error_log WHERE message LIKE ?", (f"%{needle}%",)
    ).fetchall()
    return [r[0] for r in rows]


# =============================================================================
# R1: stale durable parked marker
# =============================================================================

async def test_r1_orphaned_parked_marker_cleared(mock_db):
    sid = "lrec-marker"
    database.create_session(session_id=sid, profile_id="default")
    # Real strand sequence: turn ends with live bg work (the turn-end finalize
    # persists the marker), then force-cleanup wipes the registry without ever
    # clearing the marker.
    task_registry.start_task(sid, "t1", "agent", "bg work")
    await persist_turn_end_state(sid)
    task_registry.clear_session(sid)
    assert database.get_session(sid)["parked_task_kind"] == "agent"

    # Confirm-twice: first observation must NOT repair.
    assert await lr.reconcile_session(sid) == 0
    assert database.get_session(sid)["parked_task_kind"] == "agent"

    # Second observation repairs.
    assert await lr.reconcile_session(sid) == 1
    assert database.get_session(sid)["parked_task_kind"] is None
    assert _error_rows(mock_db, "cleared the stale marker")


async def test_r1_negative_genuinely_parked_session_untouched(mock_db):
    """The marker is LEGAL while the background work is still running on a
    live pooled client — the reconciler must not touch anything."""
    from unittest.mock import MagicMock

    from app.core.session_pool import SessionState, get_session_pool

    sid = "lrec-marker-live"
    database.create_session(session_id=sid, profile_id="default")
    task_registry.start_task(sid, "t1", "agent", "bg work")
    await persist_turn_end_state(sid)  # turn ended with live bg work: marker set
    # The pooled SDK client that owns the work's completion is alive.
    get_session_pool().put(sid, SessionState(adapter=MagicMock(), is_connected=True))
    try:
        assert await lr.reconcile_session(sid) == 0
        assert await lr.reconcile_session(sid) == 0
        assert database.get_session(sid)["parked_task_kind"] == "agent"
        assert task_registry.get_task(sid, "t1")["status"] == "running"
    finally:
        get_session_pool()._pool.pop(sid, None)  # noqa: SLF001
        task_registry.clear_session(sid)


async def test_r1_other_installs_marker_untouched(mock_db):
    """Instance scope: never clear a marker owned by a peer install."""
    sid = "lrec-marker-peer"
    database.create_session(session_id=sid, profile_id="default")
    mock_db.execute(
        "UPDATE sessions SET instance_id = 'peer-install', parked_task_kind = 'agent' WHERE id = ?",
        (sid,),
    )
    mock_db.commit()

    assert await lr.reconcile_session(sid) == 0
    assert await lr.reconcile_session(sid) == 0
    assert database.get_session(sid)["parked_task_kind"] == "agent"


# =============================================================================
# R2: stale unpark latch
# =============================================================================

async def test_r2_stranded_unpark_latch_cleared(mock_db):
    sid = "lrec-latch"
    # Real strand: the latch was set for a committed follow-up that never
    # started streaming (no pump, no turn).
    task_registry.mark_unpark_pending(sid)

    assert await lr.reconcile_session(sid) == 0  # first sighting: no repair
    assert task_registry.is_unpark_pending(sid) is True

    assert await lr.reconcile_session(sid) == 1
    assert task_registry.is_unpark_pending(sid) is False
    assert _error_rows(mock_db, "unpark-pending")


async def test_r2_negative_fresh_waking_session_not_repaired(mock_db, monkeypatch):
    """A GENUINE fresh waking session (latch younger than the real grace) is
    never repaired, no matter how often it is observed."""
    monkeypatch.setattr(lr, "UNPARK_LATCH_GRACE_SECONDS", 60.0)  # real gate
    sid = "lrec-latch-fresh"
    task_registry.mark_unpark_pending(sid)
    try:
        for _ in range(3):
            assert await lr.reconcile_session(sid) == 0
        assert task_registry.is_unpark_pending(sid) is True
    finally:
        task_registry.clear_unpark_pending(sid)


async def test_r2_negative_streaming_wake_turn_not_repaired(mock_db):
    """Latch + a live turn (a lease-valid active run in the durable runs table
    — e.g. a CLI-owned WAKE turn mid-flight, which by design does NOT clear
    the latch): the follow-up IS running; the reconciler hands off."""
    sid = "lrec-latch-streaming"
    task_registry.mark_unpark_pending(sid)
    runs.claim_run(sid, "runner-1", 60)  # the wake turn is in flight
    try:
        assert await lr.reconcile_session(sid) == 0
        assert await lr.reconcile_session(sid) == 0
        assert task_registry.is_unpark_pending(sid) is True
    finally:
        task_registry.clear_unpark_pending(sid)


# =============================================================================
# R3: retired (stale sync-engine streaming state)
# =============================================================================

def test_r3_stays_retired():
    """V2 Phase-1 deletion tripwire: R3's subject — the sync-engine in-memory
    ``_streaming_sessions`` map — is deleted; busy truth is the durable runs
    table. The reconciler must carry no sync-streaming check: resurrecting one
    would mean a second busy source snuck back in."""
    import inspect

    assert not hasattr(lr, "_check_sync_streaming")
    src = inspect.getsource(lr)
    assert "sync_engine" not in src.replace(
        "sync-engine", ""
    )  # prose mentions the retirement; no code reference
    assert "broadcast_stream" not in src


# =============================================================================
# R4: background work with a dead pooled client
# =============================================================================

async def test_r4_dead_client_task_reaped_and_terminal_hook_fires(mock_db):
    sid = "lrec-dead"
    fired = []
    task_registry.set_terminal_notification_callback(
        lambda s, t: fired.append((s, t["id"], t["status"], t.get("reason")))
    )
    # Real strand: a genuinely backgrounded sub-agent (launch recorded) whose
    # turn ended; the pooled client is gone (nothing in the pool at all).
    task_registry.mark_background_launch(sid, "toolu_bg")
    task_registry.start_task(sid, "t1", "agent", "bg work", tool_use_id="toolu_bg")
    try:
        assert await lr.reconcile_session(sid) == 0  # first sighting
        assert task_registry.get_task(sid, "t1")["status"] == "running"

        assert await lr.reconcile_session(sid) == 1
        reaped = task_registry.get_task(sid, "t1")
        assert reaped["status"] == "killed"
        assert reaped["reason"] == "dead_client"
        # The #514 guaranteed terminal-notification hook fired exactly once.
        assert fired == [(sid, "t1", "killed", "dead_client")]
        assert _error_rows(mock_db, "marked them killed")

        # Idempotent: the task is terminal now; nothing further to reap.
        assert await lr.reconcile_session(sid) == 0
        assert fired == [(sid, "t1", "killed", "dead_client")]
    finally:
        task_registry.set_terminal_notification_callback(None)
        task_registry.clear_session(sid)


async def test_r4_negative_streaming_turn_exempt(mock_db):
    """Mid-turn background work belongs to the turn — never reaped while a
    lease-valid active run says the turn is in flight, even with no pooled
    client visible."""
    sid = "lrec-dead-streaming"
    runs.claim_run(sid, "runner-1", 60)  # the turn is mid-flight
    task_registry.mark_background_launch(sid, "toolu_bg")
    task_registry.start_task(sid, "t1", "agent", "bg work", tool_use_id="toolu_bg")
    try:
        assert await lr.reconcile_session(sid) == 0
        assert await lr.reconcile_session(sid) == 0
        assert task_registry.get_task(sid, "t1")["status"] == "running"
    finally:
        task_registry.clear_session(sid)


async def test_r4_negative_monitor_shell_never_reaped(mock_db):
    """App-managed Monitor shells don't depend on the pooled client."""
    sid = "lrec-shell"
    task_registry.start_task(sid, "sh1", "shell", "npm run dev")
    try:
        assert await lr.reconcile_session(sid) == 0
        assert await lr.reconcile_session(sid) == 0
        assert task_registry.get_task(sid, "sh1")["status"] == "running"
    finally:
        task_registry.clear_session(sid)


# =============================================================================
# R5: streaming (lease-valid active run) with no client — WARN-only
# =============================================================================

async def test_r5_warn_only_no_repair(mock_db):
    sid = "lrec-warn"
    runs.claim_run(sid, "runner-1", 60)  # busy, but NO pooled client at all
    assert await lr.reconcile_session(sid) == 0
    assert await lr.reconcile_session(sid) == 0  # detection never counts as repair
    # Still busy — nothing was force-ended.
    assert runs.get_active_run(sid) is not None
    assert _error_rows(mock_db, "Not auto-repaired")


# =============================================================================
# Safety gates
# =============================================================================

async def test_shutdown_stands_down(mock_db, monkeypatch):
    from app.core import startup

    monkeypatch.setattr(startup, "is_shutting_down", True)
    sid = "lrec-shutdown"
    task_registry.mark_unpark_pending(sid)
    try:
        assert await lr.reconcile_session(sid) == 0
        assert await lr.reconcile_session(sid) == 0
        assert task_registry.is_unpark_pending(sid) is True
        assert await lr.reconcile_all_once() == 0
    finally:
        task_registry.clear_unpark_pending(sid)


async def test_per_check_disable_flag(mock_db, monkeypatch):
    monkeypatch.setitem(lr.CHECKS_ENABLED, "unpark_latch", False)
    sid = "lrec-disabled"
    task_registry.mark_unpark_pending(sid)
    try:
        assert await lr.reconcile_session(sid) == 0
        assert await lr.reconcile_session(sid) == 0
        assert task_registry.is_unpark_pending(sid) is True
    finally:
        task_registry.clear_unpark_pending(sid)


async def test_healed_condition_resets_confirmation(mock_db):
    """A condition that heals between observations loses its credit — the
    confirm-twice counter starts over on the next sighting."""
    sid = "lrec-heal"
    task_registry.mark_unpark_pending(sid)
    assert await lr.reconcile_session(sid) == 0  # observation 1
    task_registry.clear_unpark_pending(sid)
    assert await lr.reconcile_session(sid) == 0  # condition gone: credit reset
    task_registry.mark_unpark_pending(sid)
    try:
        assert await lr.reconcile_session(sid) == 0  # observation 1 again — no repair
        assert task_registry.is_unpark_pending(sid) is True
    finally:
        task_registry.clear_unpark_pending(sid)


async def test_never_raises(mock_db, monkeypatch):
    """A check blowing up is contained, recorded, and doesn't block the rest."""

    async def _boom(session_id, snap):
        raise RuntimeError("check exploded")

    # Blow up the FIRST check in the chain; the unpark-latch check runs after
    # it and must still repair.
    monkeypatch.setattr(lr, "_check_parked_marker", _boom)
    sid = "lrec-raise"
    task_registry.mark_unpark_pending(sid)
    try:
        assert await lr.reconcile_session(sid) == 0
        # The healthy check after the exploding one still ran and repaired on
        # the second pass.
        assert await lr.reconcile_session(sid) == 1
        assert task_registry.is_unpark_pending(sid) is False
        assert _error_rows(mock_db, "check exploded")
    finally:
        task_registry.clear_unpark_pending(sid)


async def test_periodic_sweep_discovers_and_repairs(mock_db):
    """reconcile_all_once discovers candidates from the real sources.

    Assertions are scoped to THIS session's latch (not exact sweep totals):
    under the full suite, leftover state from unrelated tests can legitimately
    appear among the sweep's candidates.
    """
    sid = "lrec-sweep"
    task_registry.mark_unpark_pending(sid)

    await lr.reconcile_all_once()  # observation 1 for the latch
    assert task_registry.is_unpark_pending(sid) is True  # confirm-twice gate

    repaired = await lr.reconcile_all_once()
    assert repaired >= 1
    assert task_registry.is_unpark_pending(sid) is False


async def test_request_reconcile_debounce(mock_db, monkeypatch):
    """The /live drift trigger is debounced per session."""
    import asyncio

    monkeypatch.setattr(lr, "ON_DEMAND_DEBOUNCE_SECONDS", 300.0)
    sid = "lrec-debounce"
    task_registry.mark_unpark_pending(sid)
    try:
        lr.request_reconcile(sid)
        await asyncio.sleep(0.05)
        # Inside the debounce window: further requests are no-ops, so the
        # confirm-twice counter never reaches two and nothing is repaired.
        lr.request_reconcile(sid)
        await asyncio.sleep(0.05)
        lr.request_reconcile(sid)
        await asyncio.sleep(0.05)
        assert task_registry.is_unpark_pending(sid) is True
    finally:
        task_registry.clear_unpark_pending(sid)
