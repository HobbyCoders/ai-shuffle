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
from tests.db.migration_helpers import column_names, schema_version


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
    assert "hidden" in column_names(conn, "sessions")
    assert schema_version(conn) == database.SCHEMA_VERSION


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

    assert "hidden" not in column_names(conn, "sessions")

    database.init_database()

    assert "hidden" in column_names(conn, "sessions")
    assert schema_version(conn) == database.SCHEMA_VERSION
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


def test_new_provider_worker_is_list_hidden_at_creation(mock_db):
    """A worker created the way ProviderAgent creates one (hidden=True, and
    since v84 is_worker=True) never reaches a sidebar list."""
    visible_id = _mk()
    worker_id = _mk(hidden=True, is_worker=True)

    assert {s["id"] for s in database.get_sessions()} == {visible_id}

    rows, total = database.get_sessions_paginated()
    assert {s["id"] for s in rows} == {visible_id}
    assert total == 1

    rows, *_ = database.get_sessions_cursor()
    assert {s["id"] for s in rows} == {visible_id}

    assert worker_id in {
        s["id"] for s in database.get_sessions(include_hidden=True)
    }


def test_is_worker_alone_hides_a_session_from_every_list(mock_db):
    """v84: the materialized worker flag hides a row on its own — the list
    queries filter on the column instead of running a correlated
    ``NOT EXISTS (... turns ...)`` probe per row (finding B7)."""
    visible_id = _mk()
    worker_id = _mk(is_worker=True)

    assert {s["id"] for s in database.get_sessions()} == {visible_id}

    rows, total = database.get_sessions_paginated()
    assert {s["id"] for s in rows} == {visible_id}
    assert total == 1

    rows, *_ = database.get_sessions_cursor()
    assert {s["id"] for s in rows} == {visible_id}

    # Still fetchable by id, and still listable with include_hidden.
    assert database.get_session(worker_id) is not None
    assert worker_id in {
        s["id"] for s in database.get_sessions(include_hidden=True)
    }


def test_legacy_provider_worker_is_hidden_after_is_worker_backfill(mock_db):
    """Workers created BEFORE hidden=True was stamped carry no flag of their
    own — only a ``provider_agent`` turn. The one-time v84 backfill materializes
    ``is_worker`` for exactly those rows, which is what lets the list queries
    drop the per-row subquery without un-hiding them."""
    from app.db.turns import enqueue_turn

    conn = mock_db
    visible_id = _mk()
    worker_id = _mk()
    enqueue_turn(worker_id, "provider_agent", prompt="delegated work")
    # Pre-migration shape: the flag is not set yet, so the row is still listed.
    conn.execute("UPDATE sessions SET is_worker = NULL")
    conn.commit()
    assert worker_id in {s["id"] for s in database.get_sessions()}

    database.init_database()

    assert {s["id"] for s in database.get_sessions()} == {visible_id}

    rows, total = database.get_sessions_paginated()
    assert {s["id"] for s in rows} == {visible_id}
    assert total == 1

    rows, *_ = database.get_sessions_cursor()
    assert {s["id"] for s in rows} == {visible_id}

    assert worker_id in {
        s["id"] for s in database.get_sessions(include_hidden=True)
    }

    # The plain session was NOT touched by the backfill.
    row = conn.execute(
        "SELECT is_worker FROM sessions WHERE id = ?", (visible_id,)
    ).fetchone()
    assert row[0] in (None, 0, False)


def test_is_worker_backfill_is_idempotent_and_runs_once(mock_db):
    """The backfill is marker-gated: a second init_database() does not re-run
    it (and re-running it by hand changes nothing)."""
    from app.db.turns import enqueue_turn

    conn = mock_db
    worker_id = _mk()
    enqueue_turn(worker_id, "provider_agent", prompt="delegated work")
    conn.execute("UPDATE sessions SET is_worker = NULL")
    conn.commit()

    database.init_database()
    assert database.get_system_setting(database.IS_WORKER_BACKFILL_MARKER) == "1"
    first = conn.execute(
        "SELECT is_worker FROM sessions WHERE id = ?", (worker_id,)
    ).fetchone()[0]
    assert first in (True, 1)

    # Second boot: marker present -> skipped (returns None, not a row count).
    from tests.pg_shim import route_db_to

    with route_db_to(conn) as shim:
        assert database._backfill_sessions_is_worker(shim.cursor()) is None

    database.init_database()
    again = conn.execute(
        "SELECT is_worker FROM sessions WHERE id = ?", (worker_id,)
    ).fetchone()[0]
    assert again in (True, 1)


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
