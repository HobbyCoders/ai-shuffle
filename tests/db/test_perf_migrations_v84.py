"""v84 performance schema pass + the migration gate.

Covers docs/PERFORMANCE_REVIEW_2026-08.md findings:

* **B3** — ``idx_usage_log_session`` exists on a fresh database AND lands on an
  existing populated one (every session open used to sequentially scan
  ``usage_log``).
* **B4** — ``session_store_summaries.folded_through_id`` (the coalesced-fold
  watermark) exists on both paths.
* **B7** — ``sessions.is_worker`` exists on both paths (its backfill behaviour
  lives in test_sessions_hidden.py, next to the filtering it enables).
* **B11** — the column-migration pass is SKIPPED when the database already
  carries this build's migration fingerprint, and RUNS whenever it does not
  (stale fingerprint, older schema version, forced by env).

The migration paths are exercised through the REAL ``init_database()`` against
the SQLite shim, exactly like the neighbouring migration tests.
"""

from __future__ import annotations

import pytest

from app.db import database
from tests.db.migration_helpers import column_names, schema_version


def _index_exists(conn, name):
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
    ).fetchone()
    return row is not None


def _stored_fingerprint(conn):
    row = conn.execute(
        "SELECT value FROM system_settings WHERE key = ?",
        (database.MIGRATIONS_FINGERPRINT_KEY,),
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Fresh-install shape
# ---------------------------------------------------------------------------

def test_fresh_db_carries_the_v84_index_and_columns(mock_db):
    conn = mock_db
    assert _index_exists(conn, "idx_usage_log_session")  # B3
    assert "folded_through_id" in column_names(conn, "session_store_summaries")  # B4
    assert "is_worker" in column_names(conn, "sessions")  # B7
    assert schema_version(conn) == database.SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Upgrade path (existing, populated database)
# ---------------------------------------------------------------------------

def test_init_upgrades_a_populated_pre_v84_db(mock_db):
    """Stamp v83 and strip the v84 additions from a database that already holds
    rows; the real init path must land all three without touching the data."""
    conn = mock_db
    conn.execute(
        "INSERT INTO sessions (id, profile_id, instance_id) VALUES (?, ?, ?)",
        ("s-legacy-v84", "default", ""),
    )
    conn.execute(
        "INSERT INTO usage_log (session_id, profile_id, model, cost_usd) "
        "VALUES (?, ?, ?, ?)",
        ("s-legacy-v84", "default", "claude-x", 1.25),
    )
    conn.execute(
        "INSERT INTO session_store_summaries "
        "(project_key, session_id, data, mtime_ms) VALUES (?, ?, ?, ?)",
        ("pk", "sdk-legacy", "{}", 7),
    )
    conn.execute("DROP INDEX IF EXISTS idx_usage_log_session")
    conn.execute("ALTER TABLE sessions DROP COLUMN is_worker")
    conn.execute("ALTER TABLE session_store_summaries DROP COLUMN folded_through_id")
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (83)")
    conn.commit()

    assert not _index_exists(conn, "idx_usage_log_session")
    assert "is_worker" not in column_names(conn, "sessions")

    database.init_database()

    assert _index_exists(conn, "idx_usage_log_session")
    assert "is_worker" in column_names(conn, "sessions")
    assert "folded_through_id" in column_names(conn, "session_store_summaries")
    assert schema_version(conn) == database.SCHEMA_VERSION

    # Existing rows survived, with the new columns defaulted.
    assert conn.execute(
        "SELECT cost_usd FROM usage_log WHERE session_id = 's-legacy-v84'"
    ).fetchone()[0] == 1.25
    assert conn.execute(
        "SELECT is_worker FROM sessions WHERE id = 's-legacy-v84'"
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT folded_through_id FROM session_store_summaries "
        "WHERE session_id = 'sdk-legacy'"
    ).fetchone()[0] == 0


def test_v84_migration_is_idempotent(mock_db):
    """Re-running the pass over an already-migrated database changes nothing and
    raises nothing (the gate is bypassed by running the pass directly)."""
    from tests.pg_shim import route_db_to

    conn = mock_db
    with route_db_to(conn) as shim:
        database._run_column_migrations(shim.cursor())
        database._run_column_migrations(shim.cursor())

    assert _index_exists(conn, "idx_usage_log_session")
    assert "is_worker" in column_names(conn, "sessions")
    assert "folded_through_id" in column_names(conn, "session_store_summaries")


# ---------------------------------------------------------------------------
# B11 — the migration gate
# ---------------------------------------------------------------------------

def test_fingerprint_is_stable_and_non_empty():
    fp = database.migrations_fingerprint()
    assert fp
    assert fp == database.migrations_fingerprint()


def test_fingerprint_includes_network_schema(monkeypatch):
    from app.db import network

    before = database.migrations_fingerprint()
    original = database.Path.read_bytes

    def changed(path):
        content = original(path)
        return content + b"\n# network schema change" if path == database.Path(network.__file__) else content

    monkeypatch.setattr(database.Path, "read_bytes", changed)
    assert database.migrations_fingerprint() != before


def test_first_init_runs_the_pass_and_records_the_fingerprint(mock_db, monkeypatch):
    conn = mock_db
    calls = []
    real = database._run_column_migrations
    monkeypatch.setattr(
        database,
        "_run_column_migrations",
        lambda cur: (calls.append(1), real(cur))[1],
    )

    assert _stored_fingerprint(conn) is None
    database.init_database()

    assert calls == [1]
    assert _stored_fingerprint(conn) == database.migrations_fingerprint()


def test_second_init_skips_the_pass_when_the_fingerprint_matches(mock_db, monkeypatch):
    """The whole point of B11: an up-to-date install does not re-run ~1,600
    lines of DDL and information_schema probes on every boot."""
    database.init_database()  # records the fingerprint

    calls = []
    monkeypatch.setattr(
        database, "_run_column_migrations", lambda cur: calls.append(1)
    )
    database.init_database()
    assert calls == []


def test_stale_fingerprint_reruns_the_pass(mock_db, monkeypatch):
    """A fingerprint written by a DIFFERENT build (any edit to the migration
    code changes it) must not be trusted."""
    conn = mock_db
    database.init_database()

    conn.execute(
        "UPDATE system_settings SET value = 'from-an-older-build' WHERE key = ?",
        (database.MIGRATIONS_FINGERPRINT_KEY,),
    )
    conn.commit()

    calls = []
    real = database._run_column_migrations
    monkeypatch.setattr(
        database,
        "_run_column_migrations",
        lambda cur: (calls.append(1), real(cur))[1],
    )
    database.init_database()

    assert calls == [1]
    assert _stored_fingerprint(conn) == database.migrations_fingerprint()


def test_older_schema_version_reruns_the_pass_even_with_a_matching_fingerprint(
    mock_db, monkeypatch
):
    """Version and fingerprint are ANDed: a database stamped at an older
    version still gets the full DDL + column pass."""
    conn = mock_db
    database.init_database()
    conn.execute("DELETE FROM schema_version")
    conn.execute("INSERT INTO schema_version (version) VALUES (80)")
    conn.commit()

    calls = []
    real = database._run_column_migrations
    monkeypatch.setattr(
        database,
        "_run_column_migrations",
        lambda cur: (calls.append(1), real(cur))[1],
    )
    database.init_database()

    assert calls == [1]
    assert schema_version(conn) == database.SCHEMA_VERSION


def test_force_env_var_reruns_the_pass(mock_db, monkeypatch):
    """Operator escape hatch for a hand-edited schema."""
    database.init_database()

    calls = []
    real = database._run_column_migrations
    monkeypatch.setattr(
        database,
        "_run_column_migrations",
        lambda cur: (calls.append(1), real(cur))[1],
    )
    monkeypatch.setenv("FORCE_DB_MIGRATIONS", "1")
    database.init_database()
    assert calls == [1]


def test_failed_migration_never_records_a_fingerprint(mock_db, monkeypatch):
    """A partially-applied pass must be retried on the next boot, so the
    fingerprint is only recorded after the pass completes."""
    conn = mock_db

    def _boom(_cur):
        raise RuntimeError("migration blew up halfway")

    monkeypatch.setattr(database, "_run_column_migrations", _boom)
    with pytest.raises(RuntimeError):
        database.init_database()

    assert _stored_fingerprint(conn) is None

    # Next boot (with the pass working again) completes and records it.
    monkeypatch.undo()
    database.init_database()
    assert _stored_fingerprint(conn) == database.migrations_fingerprint()
