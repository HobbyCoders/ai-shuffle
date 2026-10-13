"""What ``delete_session`` leaves behind.

None of the session-scoped runtime tables (``turns``, ``session_events``,
``session_event_counters``, ``runs``, ``pending_interactions``,
``background_tasks``) carries a foreign key into ``sessions``, so before this
they simply accumulated: the live DB was found holding turn rows, event logs
and runner leases for sessions deleted long ago, plus child sessions still
pointing at a ``parent_session_id`` that no longer resolved.

Three different lifetimes are pinned here, and the difference between them is
the point:

* runtime state is DELETED (it describes how a session ran and means nothing
  without it),
* ``usage_log`` is UNLINKED (it is the spend ledger the analytics endpoints
  aggregate — deleting rows would rewrite finished cost reports),
* child sessions SURVIVE with a NULL parent pointer (a fork or a ProviderAgent
  worker is an independently readable transcript; a delete the user asked for
  on the parent is not a delete of the children).
"""

import uuid

import pytest

from app.db import database
from app.db import sessions as sessions_db


def _mk(session_id=None, **kwargs):
    session_id = session_id or f"ses-{uuid.uuid4().hex[:8]}"
    database.create_session(session_id=session_id, profile_id="default", **kwargs)
    return session_id


def _count(conn, table, session_id):
    return conn.execute(
        f"SELECT COUNT(*) FROM {table} WHERE session_id = ?", (session_id,)
    ).fetchone()[0]


def _seed_runtime_rows(conn, session_id):
    """One row in every session-scoped runtime table, written directly so the
    test does not depend on six different write paths."""
    conn.execute(
        "INSERT INTO turns (id, session_id, state, source) VALUES (?, ?, ?, ?)",
        (f"turn-{session_id}", session_id, "completed", "user"),
    )
    conn.execute(
        "INSERT INTO session_events (id, session_id, seq, event_type, payload) "
        "VALUES (?, ?, ?, ?, ?)",
        (f"ev-{session_id}", session_id, 1, "turn.completed", "{}"),
    )
    conn.execute(
        "INSERT INTO session_event_counters (session_id, next_seq) VALUES (?, ?)",
        (session_id, 2),
    )
    conn.execute(
        "INSERT INTO runs (id, session_id, runner_id, fence_token, state, "
        "lease_expires_at) VALUES (?, ?, ?, ?, ?, ?)",
        (f"run-{session_id}", session_id, "runner-1", 1, "expired", "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO pending_interactions "
        "(id, session_id, kind, payload, state, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (f"pi-{session_id}", session_id, "permission", "{}", "expired",
         "2026-01-01"),
    )
    conn.execute(
        "INSERT INTO background_tasks (task_id, session_id, status) VALUES (?, ?, ?)",
        (f"task-{session_id}", session_id, "killed"),
    )
    conn.commit()


RUNTIME_TABLES = (
    "turns",
    "session_events",
    "session_event_counters",
    "runs",
    "pending_interactions",
    "background_tasks",
)


def test_delete_removes_every_session_scoped_runtime_row(mock_db):
    sid = _mk()
    _seed_runtime_rows(mock_db, sid)
    for table in RUNTIME_TABLES:
        assert _count(mock_db, table, sid) == 1, table

    assert sessions_db.delete_session(sid) is True

    for table in RUNTIME_TABLES:
        assert _count(mock_db, table, sid) == 0, table


def test_delete_leaves_another_sessions_runtime_rows_alone(mock_db):
    """The cleanup is scoped by session_id, not a table wipe."""
    doomed = _mk()
    keeper = _mk()
    _seed_runtime_rows(mock_db, doomed)
    _seed_runtime_rows(mock_db, keeper)

    sessions_db.delete_session(doomed)

    for table in RUNTIME_TABLES:
        assert _count(mock_db, table, keeper) == 1, table


def test_delete_unlinks_the_usage_ledger_without_dropping_it(mock_db):
    """Spend history survives the session it was spent on. Deleting these rows
    would silently change what the analytics endpoints report for past days."""
    sid = _mk()
    database.log_usage(
        session_id=sid, profile_id="default", model="m",
        tokens_in=100, tokens_out=20, cost_usd=0.5, duration_ms=1000,
    )
    assert _count(mock_db, "usage_log", sid) == 1

    sessions_db.delete_session(sid)

    assert _count(mock_db, "usage_log", sid) == 0
    orphaned = mock_db.execute(
        "SELECT session_id, cost_usd FROM usage_log WHERE session_id IS NULL"
    ).fetchall()
    assert len(orphaned) == 1
    assert orphaned[0][1] == pytest.approx(0.5)


def test_delete_keeps_children_and_clears_their_parent_pointer(mock_db):
    """A fork / ProviderAgent worker is an independent transcript: the parent's
    delete unlinks it, never cascades into it."""
    parent = _mk()
    child = _mk(parent_session_id=parent)
    _seed_runtime_rows(mock_db, child)

    sessions_db.delete_session(parent)

    row = database.get_session(child)
    assert row is not None, "child session must survive its parent's delete"
    assert row["parent_session_id"] is None
    # The child is still a real session, so its own runtime rows stay valid.
    for table in RUNTIME_TABLES:
        assert _count(mock_db, table, child) == 1, table
    assert database.get_session(parent) is None


def test_delete_of_a_session_without_dependents_still_reports_success(mock_db):
    sid = _mk()
    assert sessions_db.delete_session(sid) is True
    assert sessions_db.delete_session(sid) is False
