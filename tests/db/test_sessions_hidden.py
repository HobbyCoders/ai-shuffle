"""Migration + filtering tests for the session list-visibility flag (v75).

v75 adds ONE nullable ``sessions.hidden BOOLEAN`` column: TRUE keeps a session
out of the sessions-list queries (wake-word "command" voice sessions are born
hidden), NULL/FALSE — every pre-existing row — stays visible. Hidden rows are
list-hidden ONLY: still fetchable by id, still fully functional. Delegated
workers use the same contract; a persisted ``provider_agent`` turn also hides
workers created before the flag was set at creation time.

Mirrors tests/db/test_sessions_mode_migration.py (the v74 sessions.mode
precedent this column follows exactly).
"""

import uuid

from app.db import database


def _column_names(conn, table):
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _schema_version(conn):
    return conn.execute("SELECT version FROM schema_version").fetchone()[0]


def _mk(session_id=None, **kwargs):
    session_id = session_id or str(uuid.uuid4())
    database.create_session(session_id=session_id, profile_id="default", **kwargs)
    return session_id


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------

def test_fresh_db_has_hidden_column_and_version(mock_db):
    """A freshly built DB (create_postgres_schema) carries sessions.hidden and
    the current schema version."""
    conn = mock_db
    assert "hidden" in _column_names(conn, "sessions")
    assert _schema_version(conn) == database.SCHEMA_VERSION


def test_init_upgrades_pre_v75_db(mock_db):
    """EXISTING-DB path: a v74-shaped sessions table (no hidden) is upgraded in
    place — the guarded ADD COLUMN lands hidden and the version bumps, with the
    existing row preserved (its hidden NULL = visible) and no exception."""
    conn = mock_db
    conn.execute("ALTER TABLE sessions DROP COLUMN hidden")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (74)")
    conn.execute(
        "INSERT INTO sessions (id, profile_id, instance_id) VALUES (?, ?, ?)",
        ("s-legacy-hidden", "default", ""),
    )
    conn.commit()

    assert "hidden" not in _column_names(conn, "sessions")

    database.init_database()

    assert "hidden" in _column_names(conn, "sessions")
    assert _schema_version(conn) == database.SCHEMA_VERSION
    row = conn.execute(
        "SELECT hidden FROM sessions WHERE id = 's-legacy-hidden'"
    ).fetchone()
    assert row[0] is None
    # And a NULL-hidden legacy row is visible in the list.
    assert "s-legacy-hidden" in {s["id"] for s in database.get_sessions()}


# ---------------------------------------------------------------------------
# create_session + list filtering
# ---------------------------------------------------------------------------

def test_hidden_session_is_list_hidden_but_fetchable(mock_db):
    """hidden=True drops the session from get_sessions but get_session still
    returns it, and include_hidden=True lists it."""
    visible_id = _mk()
    hidden_id = _mk(hidden=True)

    listed = {s["id"] for s in database.get_sessions()}
    assert visible_id in listed
    assert hidden_id not in listed

    # Fully fetchable by id — list-visibility only.
    row = database.get_session(hidden_id)
    assert row is not None
    assert row["hidden"] in (True, 1)

    all_listed = {s["id"] for s in database.get_sessions(include_hidden=True)}
    assert {visible_id, hidden_id} <= all_listed


def test_paginated_excludes_hidden_from_items_and_total(mock_db):
    """The shared WHERE clause keeps hidden rows out of BOTH the page and the
    COUNT, so hidden sessions never inflate the total."""
    visible_id = _mk()
    hidden_id = _mk(hidden=True)

    sessions, total = database.get_sessions_paginated()
    ids = {s["id"] for s in sessions}
    assert visible_id in ids
    assert hidden_id not in ids
    assert total == 1

    sessions, total = database.get_sessions_paginated(include_hidden=True)
    assert {visible_id, hidden_id} <= {s["id"] for s in sessions}
    assert total == 2


def test_cursor_excludes_hidden(mock_db):
    """Keyset pagination applies the same default exclusion; the limit+1
    has_more probe never counts hidden rows."""
    visible_id = _mk()
    hidden_id = _mk(hidden=True)

    rows, next_cursor, next_cursor_id, has_more = database.get_sessions_cursor(limit=1)
    assert {s["id"] for s in rows} == {visible_id}
    assert has_more is False

    rows, *_ = database.get_sessions_cursor(include_hidden=True)
    assert {visible_id, hidden_id} <= {s["id"] for s in rows}


def test_legacy_provider_worker_is_list_hidden_by_turn_source(mock_db):
    """Workers created before hidden=True was stamped are still excluded from
    every sidebar-list query, while include_hidden keeps diagnostics possible."""
    from app.db.turns import enqueue_turn

    visible_id = _mk()
    worker_id = _mk()
    enqueue_turn(worker_id, "provider_agent", prompt="delegated work")

    assert {s["id"] for s in database.get_sessions()} == {visible_id}

    rows, total = database.get_sessions_paginated()
    assert {s["id"] for s in rows} == {visible_id}
    assert total == 1

    rows, *_ = database.get_sessions_cursor()
    assert {s["id"] for s in rows} == {visible_id}

    assert worker_id in {
        s["id"] for s in database.get_sessions(include_hidden=True)
    }


def test_set_session_hidden_toggles(mock_db):
    """set_session_hidden flips visibility both ways and 404s on a missing id."""
    session_id = _mk()
    assert session_id in {s["id"] for s in database.get_sessions()}

    updated = database.set_session_hidden(session_id, True)
    assert updated is not None
    assert session_id not in {s["id"] for s in database.get_sessions()}

    database.set_session_hidden(session_id, False)
    assert session_id in {s["id"] for s in database.get_sessions()}

    assert database.set_session_hidden("nope", True) is None


# ---------------------------------------------------------------------------
# API response model
# ---------------------------------------------------------------------------

def test_session_model_coalesces_null_hidden_to_false():
    """The API model reads NULL hidden as False (visible) and passes True through."""
    from datetime import datetime, timezone
    from app.core.models import Session

    base = {
        "id": "s1",
        "profile_id": "default",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    assert Session(**base, hidden=None).hidden is False
    assert Session(**base).hidden is False
    assert Session(**base, hidden=True).hidden is True
    assert Session(**base, hidden=1).hidden is True
