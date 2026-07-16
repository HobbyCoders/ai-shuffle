"""Tests for the boot-time reconcile-from-durable-truth phase (Phase 2.4).

Covers:
- clearing this install's orphaned parked markers (with an error_log entry);
- resetting drifted live-looking ``sessions.status`` values;
- never touching another install's sessions (instance scoping);
- the durable parked marker being set/cleared at the turn-end finalize seam
  (V2: ``turn_finalize.persist_turn_end_state`` is the ONLY marker writer —
  the live store and its start/end_streaming seams are deleted);
- reconcile failures never raising out of startup;
- schema parity + the v51 ``parked_task_kind`` column (fresh schema and the
  idempotent column migration from a v50-shaped database).
"""

import uuid

import pytest

from app.core.config import settings
from app.db import database
from app.core.startup_reconcile import reconcile_from_durable_truth


def _make_session(test_db, *, instance_id=None, parked_task_kind=None,
                  status="active"):
    """Insert a session row and return its id."""
    session_id = f"reconcile-{uuid.uuid4().hex[:12]}"
    database.create_session(session_id=session_id, profile_id="default")
    test_db.execute(
        "UPDATE sessions SET instance_id = ?, parked_task_kind = ?, status = ? "
        "WHERE id = ?",
        (instance_id or settings.effective_instance_id, parked_task_kind,
         status, session_id),
    )
    test_db.commit()
    return session_id


def _session_row(test_db, session_id):
    row = test_db.execute(
        "SELECT status, parked_task_kind, instance_id FROM sessions WHERE id = ?",
        (session_id,),
    ).fetchone()
    assert row is not None
    return row


def _error_log_rows(test_db, session_id):
    return test_db.execute(
        "SELECT source, level, message FROM error_log WHERE session_id = ?",
        (session_id,),
    ).fetchall()


# =============================================================================
# Branch (a): orphaned session state, scoped to this install
# =============================================================================

async def test_orphaned_parked_marker_cleared_with_error_log(mock_db, test_db):
    session_id = _make_session(test_db, parked_task_kind="agent")

    counts = await reconcile_from_durable_truth()

    assert counts["parked_markers_cleared"] == 1
    row = _session_row(test_db, session_id)
    assert row["parked_task_kind"] is None
    assert row["status"] == "active"

    errors = _error_log_rows(test_db, session_id)
    assert len(errors) == 1
    assert errors[0]["source"] == "backend"
    assert errors[0]["level"] == "WARNING"
    assert "parked" in errors[0]["message"]
    assert session_id in errors[0]["message"]


async def test_orphaned_live_looking_status_reset_with_error_log(mock_db, test_db):
    session_id = _make_session(test_db, status="streaming")

    counts = await reconcile_from_durable_truth()

    assert counts["statuses_reset"] == 1
    assert _session_row(test_db, session_id)["status"] == "active"

    errors = _error_log_rows(test_db, session_id)
    assert len(errors) == 1
    assert "streaming" in errors[0]["message"]


async def test_other_instance_sessions_untouched(mock_db, test_db):
    # Another install's session may be legitimately parked/streaming right now.
    foreign_id = _make_session(
        test_db, instance_id="some-other-install-1234",
        parked_task_kind="agent", status="streaming",
    )
    own_id = _make_session(test_db, parked_task_kind="shell")

    counts = await reconcile_from_durable_truth()

    assert counts["parked_markers_cleared"] == 1
    assert counts["statuses_reset"] == 0
    foreign = _session_row(test_db, foreign_id)
    assert foreign["parked_task_kind"] == "agent"
    assert foreign["status"] == "streaming"
    assert _error_log_rows(test_db, foreign_id) == []
    assert _session_row(test_db, own_id)["parked_task_kind"] is None


async def test_idle_sessions_untouched_and_nothing_reconciled(mock_db, test_db):
    session_id = _make_session(test_db)  # active, no marker

    counts = await reconcile_from_durable_truth()

    assert counts == {
        "parked_markers_cleared": 0,
        "statuses_reset": 0,
        "stray_cli_reaped": 0,
        "background_task_rows_killed": 0,
        "turns_reaped": 0,
    }
    assert _session_row(test_db, session_id)["status"] == "active"
    assert _error_log_rows(test_db, session_id) == []


async def test_archived_status_is_terminal_and_untouched(mock_db, test_db):
    session_id = _make_session(test_db, status="archived")

    counts = await reconcile_from_durable_truth()

    assert counts["statuses_reset"] == 0
    assert _session_row(test_db, session_id)["status"] == "archived"


# =============================================================================
# Durable parked marker: set/cleared at the turn-end finalize seam
#
# V2: ``persist_turn_end_state`` is the only marker writer. There is NO
# start-of-turn clear seam anymore (the live store's start_streaming clear is
# deleted): a stale marker is cleared either by the NEXT turn's own finalize
# (no work left → kind=None, pinned below) or by the reconcilers.
# =============================================================================

async def test_turn_end_with_background_agent_sets_marker(mock_db, test_db):
    from app.core.task_registry import task_registry
    from app.core.turn_finalize import persist_turn_end_state

    session_id = _make_session(test_db)
    task_registry.start_task(session_id, "task-1", "agent", "background research")
    try:
        await persist_turn_end_state(session_id)
    finally:
        task_registry.clear_session(session_id)

    assert _session_row(test_db, session_id)["parked_task_kind"] == "agent"


async def test_turn_end_without_background_work_clears_marker(mock_db, test_db):
    from app.core.task_registry import task_registry
    from app.core.turn_finalize import persist_turn_end_state

    session_id = _make_session(test_db, parked_task_kind="agent")
    task_registry.start_task(session_id, "task-1", "agent", "background research")
    task_registry.complete_task(session_id, "task-1", "completed")
    try:
        await persist_turn_end_state(session_id)
    finally:
        task_registry.clear_session(session_id)

    assert _session_row(test_db, session_id)["parked_task_kind"] is None


async def test_plain_shell_task_does_not_park(mock_db, test_db):
    # Monitor-style shells (kind="shell", not SDK-backgrounded) do not pin the
    # session in the pool, so they must not set the durable parked marker.
    from app.core.task_registry import task_registry
    from app.core.turn_finalize import persist_turn_end_state

    session_id = _make_session(test_db)
    task_registry.start_task(session_id, "shell-1", "shell", "npm run build")
    try:
        await persist_turn_end_state(session_id)
    finally:
        task_registry.clear_session(session_id)

    assert _session_row(test_db, session_id)["parked_task_kind"] is None


# =============================================================================
# Reconcile failure never raises out of startup
# =============================================================================

async def test_reconcile_db_failure_does_not_raise(mock_db, monkeypatch):
    import app.core.startup_reconcile as sr

    def _boom():
        raise RuntimeError("database exploded")

    monkeypatch.setattr(sr, "get_db", _boom)

    counts = await reconcile_from_durable_truth()

    assert counts == {
        "parked_markers_cleared": 0,
        "statuses_reset": 0,
        "stray_cli_reaped": 0,
        "background_task_rows_killed": 0,
        "turns_reaped": 0,
    }


async def test_stray_cli_sweep_suppressed_under_testing(mock_db, monkeypatch):
    # TESTING=1 (set by conftest) must keep the process-table sweep out of the
    # suite even when the flag is on.
    import app.core.startup_reconcile as sr

    def _fail():  # pragma: no cover — must not be reached
        raise AssertionError("sweep must not run under TESTING")

    monkeypatch.setattr(sr, "_sweep_stray_cli_processes", _fail)
    monkeypatch.setattr(settings, "reap_stray_cli_on_boot", True)

    counts = await reconcile_from_durable_truth()

    assert counts["stray_cli_reaped"] == 0


# =============================================================================
# V2 turns — boot reap-and-kick sweep (D3)
# =============================================================================

async def test_boot_reap_requeues_dead_lease_turns_and_kicks(
    mock_db, test_db, monkeypatch
):
    from app.core import turn_scheduler
    from app.db import turns as turns_db

    kicked = []
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: kicked.append(sid))
    tid = turns_db.enqueue_turn("boot-s1", "user", prompt="p")["id"]
    turns_db.start_turn(tid, run_id="run-missing")  # no runs row → dead lease

    counts = await reconcile_from_durable_truth()

    assert counts["turns_reaped"] == 1
    assert turns_db.get_turn(tid)["state"] == "queued"  # user turn requeued
    assert kicked == ["boot-s1"]


# =============================================================================
# Schema: version parity + parked_task_kind column
# =============================================================================

def test_schema_version_parity_v51():
    from app.db.database import SCHEMA_VERSION as db_version
    from app.db.schema_postgres import SCHEMA_VERSION as pg_version

    # database.py and schema_postgres.py must agree on the schema version.
    # Bumped to 62 for the persisted SDK context-usage snapshot (additive
    # nullable sessions.context_usage_json JSONB). v61 was the V2 Phase-4
    # placement layer; v60 was the Phase-3 origin collapse; v59 added the
    # Phase-2 turns table; v58 was the Phase-1 deletion (v2_stream forced TRUE +
    # default flipped); v57 added the opt-in flag; v56 added the Phase-0
    # primitives.
    assert db_version == 62
    assert pg_version == 62
    assert db_version == pg_version


def test_column_exists_after_create_postgres_schema(test_db):
    # The conftest fixture builds the schema via create_postgres_schema().
    cols = [r["name"] for r in test_db.execute(
        "SELECT name FROM pragma_table_info('sessions')"
    ).fetchall()]
    assert "parked_task_kind" in cols


def test_column_migration_from_v50_database(mock_db, test_db):
    # Simulate a v50 database (automation_fires landed, no parked_task_kind)
    # and verify the idempotent column migration adds it.
    from tests.pg_shim import ShimCursor
    from app.db.database import _run_column_migrations

    test_db.execute("ALTER TABLE sessions DROP COLUMN parked_task_kind")
    test_db.commit()
    cols = [r["name"] for r in test_db.execute(
        "SELECT name FROM pragma_table_info('sessions')"
    ).fetchall()]
    assert "parked_task_kind" not in cols

    _run_column_migrations(ShimCursor(test_db.cursor()))
    test_db.commit()

    cols = [r["name"] for r in test_db.execute(
        "SELECT name FROM pragma_table_info('sessions')"
    ).fetchall()]
    assert "parked_task_kind" in cols

    # Idempotent: a second run must not fail on the existing column.
    _run_column_migrations(ShimCursor(test_db.cursor()))
