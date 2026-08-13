"""
SQLite to PostgreSQL data migration tool.

Migrates all data from the SQLite database to PostgreSQL.
Run after setting up the PostgreSQL database and schema.

Usage:
    python -m app.db.migrate_sqlite_to_postgres
    python -m app.db.migrate_sqlite_to_postgres --dry-run
    python -m app.db.migrate_sqlite_to_postgres --sqlite-path /data/app.db --postgres-url postgresql://...

Environment:
    DATABASE_URL must be set to the PostgreSQL connection string.
    The SQLite database path is auto-detected from settings.
"""

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Optional

import json
import psycopg2
import psycopg2.extras

from app.db.base import parse_database_url

# ---------------------------------------------------------------------------
# Migration phases — ordered by foreign-key dependencies
# ---------------------------------------------------------------------------

PHASE_1 = [
    "system_settings",
    "admin",
    "schema_version",
    "profiles",  # v71: lands in the `agents` table (see _TABLE_TARGETS)
    "projects",
    "tags",
    "fal_model_catalog",
    "credential_policies",
]

PHASE_2 = [
    "sessions",
    "api_users",
    "notes",
    "templates",
    "subagents",  # v71: folded into `agents` (see _migrate_table special case)
    "automations",
    "connectors",
    "git_repositories",
    "mobile_devices",
    "webhooks",
    "project_hooks",
]

PHASE_3 = [
    "session_messages",
    "session_tags",
    "usage_log",
    "request_log",
    "audit_log",
    "auth_sessions",
    "api_key_sessions",
    "login_attempts",
    "account_lockouts",
    "api_user_profiles",
    "api_user_credentials",
    "api_user_github_config",
    "user_credential_policies",
    "worktrees",
    "automation_runs",
    "automation_approvals",
    "connector_event_log",
    "connector_sessions",
    "pending_actions",
    "trusted_actions",
    "mobile_schedules",
    "sync_log",
    "rate_limits",
    "queued_messages",
    "permission_rules",
    "user_preferences",
]

ALL_TABLES = PHASE_1 + PHASE_2 + PHASE_3

# Tables whose ``id`` column is backed by a BIGSERIAL sequence in Postgres.
SERIAL_TABLES = [
    "session_messages",
    "sync_log",
    "usage_log",
    "login_attempts",
    "account_lockouts",
    "user_preferences",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _table_exists_pg(pg_cur, table_name: str) -> bool:
    """Return True if *table_name* exists in the public schema."""
    pg_cur.execute(
        "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = 'public' AND table_name = %s)",
        (table_name,),
    )
    return pg_cur.fetchone()[0]


def _table_exists_sqlite(sqlite_cur, table_name: str) -> bool:
    """Return True if *table_name* exists in the SQLite database."""
    sqlite_cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return sqlite_cur.fetchone() is not None


def _migrate_table(
    sqlite_conn: sqlite3.Connection,
    pg_conn,
    table_name: str,
    dry_run: bool = False,
) -> int:
    """Migrate a single table from SQLite to PostgreSQL.

    Returns the number of rows migrated (or that would be migrated in
    dry-run mode).
    """
    sqlite_cur = sqlite_conn.cursor()

    # Check table exists in SQLite
    if not _table_exists_sqlite(sqlite_cur, table_name):
        print(f"  SKIP {table_name} — not present in SQLite")
        return 0

    sqlite_cur.execute(f"SELECT * FROM {table_name}")  # noqa: S608
    rows = sqlite_cur.fetchall()

    if not rows:
        print(f"  SKIP {table_name} — empty")
        return 0

    col_names = [desc[0] for desc in sqlite_cur.description]

    if dry_run:
        print(f"  DRY-RUN {table_name}: {len(rows)} rows, "
              f"cols={col_names}")
        return len(rows)

    pg_cur = pg_conn.cursor()

    # v71 (agents unification): old sqlite ``subagents`` rows are FOLDED into
    # the pg ``agents`` table — there is no pg subagents table any more.
    if table_name == "subagents":
        from app.db.database import subagent_row_to_agent_config

        folded = 0
        for raw in rows:
            row = dict(zip(col_names, tuple(raw)))
            config = subagent_row_to_agent_config(row)
            try:
                pg_cur.execute(
                    """INSERT INTO agents (id, name, description, is_builtin,
                                           config, mcp_tools, created_at, updated_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                       ON CONFLICT DO NOTHING""",
                    (
                        row["id"],
                        row.get("name") or row["id"],
                        row.get("description"),
                        bool(row.get("is_builtin")),
                        json.dumps(config),
                        json.dumps([]),
                        row.get("created_at"),
                        row.get("updated_at"),
                    ),
                )
                folded += 1
            except Exception as exc:
                pg_conn.rollback()
                print(f"  ERR  subagents fold ({row.get('id')}): {exc}")
                return folded
        pg_conn.commit()
        print(f"  OK   subagents: {folded} rows folded into agents")
        return folded

    # v71: the ``profiles`` table is named ``agents`` in postgres.
    target_table = "agents" if table_name == "profiles" else table_name

    # Check table exists in Postgres
    if not _table_exists_pg(pg_cur, target_table):
        print(f"  SKIP {target_table} — not present in PostgreSQL "
              "(run schema creation first)")
        return 0

    table_name = target_table

    # Build INSERT ... VALUES template
    col_list = ", ".join(col_names)
    placeholders = ", ".join(["%s"] * len(col_names))
    insert_sql = (
        f"INSERT INTO {table_name} ({col_list}) "
        f"VALUES ({placeholders}) "
        f"ON CONFLICT DO NOTHING"
    )

    # Convert rows to plain tuples (sqlite3.Row -> tuple)
    values = [tuple(row) for row in rows]

    try:
        # Use execute_values for fast batch insert
        template = "(" + ", ".join(["%s"] * len(col_names)) + ")"
        insert_values_sql = (
            f"INSERT INTO {table_name} ({col_list}) VALUES %s "
            f"ON CONFLICT DO NOTHING"
        )
        psycopg2.extras.execute_values(
            pg_cur, insert_values_sql, values, template=template, page_size=500
        )
        pg_conn.commit()
        print(f"  OK   {table_name}: {len(rows)} rows")
    except Exception as exc:
        pg_conn.rollback()
        print(f"  ERR  {table_name}: {exc}")
        return 0

    return len(rows)


def _reset_sequences(pg_conn) -> None:
    """Reset SERIAL/BIGSERIAL sequences to follow the current max id."""
    pg_cur = pg_conn.cursor()
    for table in SERIAL_TABLES:
        try:
            if not _table_exists_pg(pg_cur, table):
                continue
            pg_cur.execute(
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), "
                f"COALESCE(MAX(id), 0) + 1, false) FROM {table}"
            )
            pg_conn.commit()
            seq_val = pg_cur.fetchone()[0]
            print(f"  SEQ  {table}.id -> next value {seq_val}")
        except Exception as exc:
            pg_conn.rollback()
            print(f"  SEQ ERR {table}: {exc}")


def _build_tsvectors(pg_conn) -> None:
    """Populate content_tsv for full-text search on session_messages."""
    pg_cur = pg_conn.cursor()
    try:
        pg_cur.execute("""
            UPDATE session_messages
            SET content_tsv = to_tsvector('english', content)
            WHERE role IN ('user', 'assistant')
              AND content IS NOT NULL
              AND content != ''
        """)
        updated = pg_cur.rowcount
        pg_conn.commit()
        print(f"  FTS  session_messages: {updated} rows indexed")
    except Exception as exc:
        pg_conn.rollback()
        print(f"  FTS ERR session_messages: {exc}")


# ---------------------------------------------------------------------------
# Main migration entry point
# ---------------------------------------------------------------------------

def migrate(
    sqlite_path: str,
    postgres_url: str,
    dry_run: bool = False,
) -> dict:
    """Run the full migration.

    Returns a dict of ``{table_name: row_count}`` for all migrated tables.
    """
    t0 = time.time()
    results: dict[str, int] = {}

    print("=" * 60)
    print("SQLite -> PostgreSQL Migration")
    print("=" * 60)
    print(f"  SQLite:   {sqlite_path}")
    print(f"  Postgres: {postgres_url.split('@')[-1] if '@' in postgres_url else postgres_url}")
    if dry_run:
        print("  MODE:     DRY RUN (no data will be written)")
    print()

    # Validate SQLite path
    if not Path(sqlite_path).exists():
        print(f"ERROR: SQLite database not found at {sqlite_path}")
        sys.exit(1)

    # Connect to SQLite
    sqlite_conn = sqlite3.connect(sqlite_path)
    sqlite_conn.row_factory = None  # plain tuples

    # Connect to PostgreSQL (skip in dry-run if URL not provided)
    pg_conn = None
    if not dry_run:
        try:
            pg_conn = psycopg2.connect(**parse_database_url(postgres_url))
        except Exception as exc:
            print(f"ERROR: Cannot connect to PostgreSQL: {exc}")
            sys.exit(1)

        # Verify Postgres schema exists
        pg_cur = pg_conn.cursor()
        if not _table_exists_pg(pg_cur, "schema_version"):
            print("ERROR: PostgreSQL schema not initialised. "
                  "Run the application once to create tables, then retry.")
            pg_conn.close()
            sqlite_conn.close()
            sys.exit(1)
    else:
        # For dry-run we still need a pg connection to check table existence
        # but if we can't connect, just proceed without table checks
        try:
            pg_conn = psycopg2.connect(**parse_database_url(postgres_url))
        except Exception:
            pg_conn = None

    # -- Phase 1 -----------------------------------------------------------
    print("Phase 1: Independent tables")
    for table in PHASE_1:
        results[table] = _migrate_table(sqlite_conn, pg_conn, table, dry_run)

    # -- Phase 2 -----------------------------------------------------------
    print("\nPhase 2: Tables with Phase-1 dependencies")
    for table in PHASE_2:
        results[table] = _migrate_table(sqlite_conn, pg_conn, table, dry_run)

    # -- Phase 3 -----------------------------------------------------------
    print("\nPhase 3: Tables with Phase-2 dependencies")
    for table in PHASE_3:
        results[table] = _migrate_table(sqlite_conn, pg_conn, table, dry_run)

    # -- Post-migration steps (skip in dry-run) ----------------------------
    if not dry_run and pg_conn:
        print("\nPost-migration: resetting sequences")
        _reset_sequences(pg_conn)

        print("\nPost-migration: building tsvector index data")
        _build_tsvectors(pg_conn)

    # -- Summary -----------------------------------------------------------
    elapsed = time.time() - t0
    total_rows = sum(results.values())
    migrated_tables = sum(1 for v in results.values() if v > 0)

    print()
    print("=" * 60)
    print("Migration Summary")
    print("=" * 60)
    for table, count in results.items():
        if count > 0:
            print(f"  {table:40s} {count:>8,} rows")
    print("-" * 60)
    print(f"  {'TOTAL':40s} {total_rows:>8,} rows")
    print(f"  Tables migrated: {migrated_tables}/{len(ALL_TABLES)}")
    print(f"  Elapsed: {elapsed:.1f}s")
    if dry_run:
        print("  (DRY RUN — no data was written)")
    print("=" * 60)

    # Cleanup
    sqlite_conn.close()
    if pg_conn:
        pg_conn.close()

    return results


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate data from SQLite to PostgreSQL",
    )
    parser.add_argument(
        "--sqlite-path",
        default=None,
        help="Path to the SQLite database file. "
             "Defaults to the path from application settings.",
    )
    parser.add_argument(
        "--postgres-url",
        default=None,
        help="PostgreSQL connection URL. "
             "Defaults to DATABASE_URL from environment / settings.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be migrated without writing any data.",
    )
    args = parser.parse_args()

    # Resolve defaults from settings if not provided via CLI
    sqlite_path = args.sqlite_path
    postgres_url = args.postgres_url

    if sqlite_path is None or postgres_url is None:
        try:
            from app.core.config import settings
        except Exception:
            pass
        else:
            if sqlite_path is None:
                sqlite_path = str(settings.db_path)
            if postgres_url is None:
                url = settings.database_url
                if url and (url.startswith("postgresql://") or url.startswith("postgres://")):
                    postgres_url = url

    if sqlite_path is None:
        print("ERROR: SQLite path not provided and could not be "
              "auto-detected. Use --sqlite-path.")
        sys.exit(1)
    if postgres_url is None:
        print("ERROR: PostgreSQL URL not provided and DATABASE_URL "
              "is not set. Use --postgres-url.")
        sys.exit(1)

    migrate(sqlite_path, postgres_url, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
