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


@pytest.mark.parametrize("failure", ["CREATE EXTENSION", "CREATE INDEX"])
def test_mirror_recall_index_failure_does_not_abort_boot(failure, caplog):
    from app.db.schema_postgres import create_session_events_recall_index

    statements = []

    class Cursor:
        def execute(self, sql):
            statements.append(sql)
            if sql.startswith(failure):
                raise RuntimeError("synthetic unavailable extension or index privilege")

        def fetchall(self):
            return []

    create_session_events_recall_index(Cursor())
    assert "SAVEPOINT recall_trgm_events" in statements
    assert statements[-2:] == [
        "ROLLBACK TO SAVEPOINT recall_trgm_events",
        "RELEASE SAVEPOINT recall_trgm_events",
    ]
    assert "Mirrored SessionSearch trigram index unavailable" in caplog.text


@pytest.mark.parametrize("failure", ["CREATE EXTENSION", "CREATE INDEX"])
def test_explicit_recall_cache_failure_propagates_for_transaction_rollback(monkeypatch, failure):
    from app.db import schema_postgres

    statements = []

    class Cursor:
        def execute(self, sql):
            statements.append(sql)
            if sql.startswith(failure):
                raise RuntimeError("synthetic unavailable extension or index privilege")

    monkeypatch.setattr(schema_postgres, "column_exists", lambda *_: False)
    with pytest.raises(RuntimeError):
        schema_postgres.create_session_recall_text(Cursor())
    assert not any(sql.startswith("DROP INDEX") for sql in statements)


def test_startup_never_backfills_optional_recall_cache(mock_db, monkeypatch):
    from app.db import base, schema_postgres

    monkeypatch.setattr(schema_postgres, "create_session_recall_text",
                        lambda _: pytest.fail("Optional cache must not run at startup"))
    with base.get_db() as conn:
        database._set_marker(conn.cursor(), database.MIGRATIONS_FINGERPRINT_KEY, "force-upgrade-test")
    database.init_database()
    for table in ("fluid_session_nodes", "session_store_entries", "session_events"):
        assert "recall_text" not in column_names(mock_db, table)


@pytest.mark.parametrize("free_bytes,allowed", [
    (0, False), (3 * 1000 + 1024 ** 3 - 1, False),
    (3 * 1000 + 1024 ** 3, True), (100 * 1024 ** 3, True),
])
def test_maintenance_requires_capacity_and_bounds_ddl(monkeypatch, free_bytes, allowed):
    from app.db import schema_postgres
    from app.maintenance.session_recall_cache import apply_cache

    statements = []

    class Cursor:
        def execute(self, sql):
            statements.append(sql)

        def fetchone(self):
            return {"bytes": 1000}

    monkeypatch.setattr(schema_postgres, "session_recall_cache_ready", lambda _: False)
    monkeypatch.setattr(schema_postgres, "create_session_recall_text", lambda _: statements.append("MIGRATE"))
    if allowed:
        apply_cache(Cursor(), free_bytes)
        assert statements[-1] == "MIGRATE"
    else:
        with pytest.raises(RuntimeError, match="Insufficient free space"):
            apply_cache(Cursor(), free_bytes)
        assert "MIGRATE" not in statements
    assert statements[:2] == ["SET LOCAL lock_timeout = '2000ms'", "SET LOCAL statement_timeout = '300000ms'"]


def test_maintenance_without_apply_never_opens_database(monkeypatch, capsys):
    from app.db import base
    from app.maintenance.session_recall_cache import main

    monkeypatch.setattr(base, "init_pool", lambda **_: pytest.fail("No explicit approval"))
    assert main([]) == 0
    assert "--apply" in capsys.readouterr().out


@pytest.mark.parametrize("lock_ms,statement_ms", [(0, 300000), (2000, -1)])
def test_maintenance_rejects_unbounded_timeouts(lock_ms, statement_ms):
    from app.maintenance.session_recall_cache import apply_cache

    with pytest.raises(ValueError, match="positive"):
        apply_cache(None, 100 * 1024 ** 3, lock_timeout_ms=lock_ms, statement_timeout_ms=statement_ms)


def test_maintenance_failure_does_not_print_connection_details(monkeypatch, capsys):
    from app.db import base
    from app.maintenance.session_recall_cache import main

    def fail(**_):
        raise RuntimeError("private connection detail")

    closed = []
    monkeypatch.setattr(base, "init_pool", fail)
    monkeypatch.setattr(base, "close_pool", lambda: closed.append(True))
    assert main(["--apply"]) == 1
    assert "private connection detail" not in capsys.readouterr().out
    assert closed == [True]


@pytest.mark.parametrize("indexes,ready", [([], False), (["idx_fluid_recall_text_trgm"], False), (
    ["idx_fluid_recall_text_trgm", "idx_sse_recall_text_trgm", "idx_events_recall_text_trgm"], True,
)])
def test_cache_requires_all_valid_ready_indexes(indexes, ready):
    from app.db.schema_postgres import session_recall_cache_ready

    class Cursor:
        def execute(self, sql):
            assert "i.indisvalid AND i.indisready" in sql

        def fetchall(self):
            return [{"relname": name} for name in indexes]

    assert session_recall_cache_ready(Cursor()) is ready


@pytest.fixture
def managed_cluster_layout(tmp_path, monkeypatch):
    from app.maintenance import session_recall_cache as maintenance

    monkeypatch.setenv("LOCAL_MODE", "0")
    data = tmp_path / "postgres" / "data"
    (data / "base").mkdir(parents=True)
    (data / "global").mkdir()
    (data / "global" / "pg_control").write_bytes(b"fixture control data")
    (data / "PG_VERSION").write_text("16\n")
    (data / "postmaster.pid").write_text(f"123\n{data}\n1000\n54321\n")

    class Cursor:
        def execute(self, sql):
            assert "SHOW" not in sql
            assert "inet_server_port()" in sql

        def fetchone(self):
            return {"host": "127.0.0.1", "port": 54321, "started": 1000}

    return maintenance, Cursor(), tmp_path, data


def test_managed_container_data_directory(managed_cluster_layout):
    maintenance, cursor, root, data = managed_cluster_layout
    assert maintenance.managed_data_directory(cursor, root) == data


def test_custom_desktop_data_directory(managed_cluster_layout):
    maintenance, cursor, root, data = managed_cluster_layout
    assert maintenance.managed_data_directory(cursor, root / "worker-data", root) == data


@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_default_desktop_data_directory(managed_cluster_layout, monkeypatch, platform):
    maintenance, cursor, root, data = managed_cluster_layout
    profile = root / "AI Shuffle"
    profile.mkdir()
    (root / "postgres").rename(profile / "postgres")
    monkeypatch.setenv("LOCAL_MODE", "1")
    monkeypatch.setenv("APPDATA", str(root))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.setattr(maintenance.sys, "platform", platform)
    monkeypatch.setattr(maintenance.Path, "home", lambda: root)
    if platform == "darwin":
        app_data = root / "Library" / "Application Support"
        app_data.mkdir(parents=True)
        profile.rename(app_data / "AI Shuffle")
        profile = app_data / "AI Shuffle"
    data = profile / "postgres" / "data"
    (data / "postmaster.pid").write_text(f"123\n{data}\n1000\n54321\n")
    assert maintenance.managed_data_directory(cursor, root / "worker-data") == data


@pytest.mark.parametrize("invalid", ["missing", "unreadable", "version", "port", "path", "started", "symlink"])
def test_refuse_invalid_managed_directory(managed_cluster_layout, monkeypatch, invalid):
    maintenance, cursor, root, data = managed_cluster_layout
    if invalid == "missing":
        root = root / "absent"
    elif invalid == "unreadable":
        monkeypatch.setattr(maintenance.os, "access", lambda *_: False)
    elif invalid == "version":
        (data / "PG_VERSION").write_text("not a PG16 cluster")
    elif invalid == "symlink":
        (data / "PG_VERSION").unlink()
        (root / "version").write_text("16\n")
        (data / "PG_VERSION").symlink_to(root / "version")
    else:
        fields = ["123", str(data), "1000", "54321"]
        fields[{"port": 3, "path": 1, "started": 2}[invalid]] = "997"
        (data / "postmaster.pid").write_text("\n".join(fields))
    with pytest.raises(maintenance.ManagedDataDirectoryError, match="missing, unreadable or invalid"):
        maintenance.managed_data_directory(cursor, root)


def test_refuse_nonlocal_server(managed_cluster_layout, monkeypatch):
    maintenance, cursor, root, _ = managed_cluster_layout
    monkeypatch.setattr(cursor, "fetchone", lambda: {"host": "192.0.2.1"})
    with pytest.raises(maintenance.ManagedDataDirectoryError, match="not a managed loopback server"):
        maintenance.managed_data_directory(cursor, root)


@pytest.mark.parametrize("unmeasurable", [False, True])
def test_cli_measures_managed_path_or_refuses(managed_cluster_layout, monkeypatch, capsys, unmeasurable):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.db import base

    maintenance, cursor, root, data = managed_cluster_layout
    monkeypatch.setattr(base.settings, "data_dir", root)
    monkeypatch.setattr(base, "init_pool", lambda **_: None)
    monkeypatch.setattr(base, "close_pool", lambda: None)

    @contextmanager
    def connection():
        yield SimpleNamespace(cursor=lambda: cursor)

    monkeypatch.setattr(base, "get_db", connection)
    applied = []
    measured = []

    def disk_usage(path):
        measured.append(path)
        if unmeasurable:
            raise PermissionError("private filesystem details")
        return SimpleNamespace(free=123456)

    monkeypatch.setattr(maintenance.shutil, "disk_usage", disk_usage)
    monkeypatch.setattr(maintenance, "apply_cache", lambda cur, free, **_: applied.append((cur, free)))
    assert maintenance.main(["--apply"]) == (1 if unmeasurable else 0)
    assert measured == [data]
    assert applied == ([] if unmeasurable else [(cursor, 123456)])
    output = capsys.readouterr().out
    if unmeasurable:
        assert "cannot be measured" in output
        assert "private filesystem details" not in output


def test_cli_reports_invalid_layout_before_rewrite(managed_cluster_layout, monkeypatch, capsys):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from app.db import base

    maintenance, cursor, root, _ = managed_cluster_layout
    monkeypatch.setattr(base.settings, "data_dir", root / "absent")
    monkeypatch.setattr(base, "init_pool", lambda **_: None)
    monkeypatch.setattr(base, "close_pool", lambda: None)

    @contextmanager
    def connection():
        yield SimpleNamespace(cursor=lambda: cursor)

    monkeypatch.setattr(base, "get_db", connection)
    monkeypatch.setattr(maintenance.shutil, "disk_usage", lambda _: pytest.fail("Invalid layout"))
    monkeypatch.setattr(maintenance, "apply_cache", lambda *_: pytest.fail("Invalid layout"))
    assert maintenance.main(["--apply"]) == 1
    assert "managed PostgreSQL data directory is missing, unreadable or invalid" in capsys.readouterr().out


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
