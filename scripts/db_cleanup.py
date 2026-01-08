#!/usr/bin/env python3
"""
Database Cleanup Script

Automatically detects and cleans up:
- Orphaned tables (tables in DB but not defined in schema)
- Stale data (expired sessions, old logs, etc.)

Usage:
    python scripts/db_cleanup.py [--dry-run] [--verbose]

Options:
    --dry-run   Show what would be cleaned without actually doing it
    --verbose   Show detailed output
"""

import argparse
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import List, Set, Tuple

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.core.config import settings


# =============================================================================
# Schema Definition - All tables that SHOULD exist in the database
# =============================================================================

# This is the source of truth for what tables are valid.
# Update this list when adding/removing tables from database.py
SCHEMA_TABLES: Set[str] = {
    # Core system
    "schema_version",
    "system_settings",
    "admin",

    # User/profile management
    "profiles",
    "projects",
    "user_preferences",

    # Sessions and messages
    "sessions",
    "session_messages",
    "session_tags",

    # Authentication
    "auth_sessions",
    "api_users",
    "api_user_profiles",
    "api_key_sessions",
    "login_attempts",
    "account_lockouts",
    "pending_2fa_sessions",

    # API and rate limiting
    "rate_limits",
    "request_log",

    # Logging
    "usage_log",
    "sync_log",
    "audit_log",

    # Features
    "subagents",
    "permission_rules",
    "tags",
    "templates",
    "webhooks",
    "notes",

    # Git integration
    "git_repositories",
    "worktrees",
    "project_hooks",

    # Credentials
    "credential_policies",
    "api_user_credentials",
    "api_user_github_config",
    "user_credential_policies",

    # SQLite internal (always keep)
    "sqlite_sequence",
    "sqlite_stat1",
    "sqlite_stat4",
}

# Tables to NEVER drop (safety list)
PROTECTED_TABLES: Set[str] = {
    "schema_version",
    "system_settings",
    "admin",
    "api_users",
    "profiles",
    "sessions",
    "session_messages",
    "sqlite_sequence",
    "sqlite_stat1",
    "sqlite_stat4",
}


# =============================================================================
# Stale Data Cleanup Rules
# =============================================================================

STALE_DATA_RULES = [
    # (table, condition, description)
    ("pending_2fa_sessions", "expires_at < datetime('now')", "expired 2FA sessions"),
    ("request_log", "timestamp < datetime('now', '-30 days')", "request logs older than 30 days"),
    ("sync_log", "created_at < datetime('now', '-7 days')", "sync logs older than 7 days"),
    ("login_attempts", "created_at < datetime('now', '-7 days')", "login attempts older than 7 days"),
    ("account_lockouts", "locked_until < datetime('now')", "expired account lockouts"),
]


# =============================================================================
# Cleanup Functions
# =============================================================================

def get_db_path() -> Path:
    """Get the database path from settings."""
    return settings.db_path


def get_existing_tables(conn: sqlite3.Connection) -> Set[str]:
    """Get set of all tables currently in the database."""
    cursor = conn.cursor()
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    return {row[0] for row in cursor.fetchall()}


def get_table_row_count(conn: sqlite3.Connection, table: str) -> int:
    """Get row count for a table."""
    cursor = conn.cursor()
    try:
        cursor.execute(f"SELECT COUNT(*) FROM {table}")
        return cursor.fetchone()[0]
    except sqlite3.OperationalError:
        return -1


def detect_orphaned_tables(conn: sqlite3.Connection) -> Set[str]:
    """
    Automatically detect orphaned tables by comparing DB tables against schema.

    Returns set of table names that exist in DB but not in SCHEMA_TABLES.
    """
    existing = get_existing_tables(conn)
    orphaned = existing - SCHEMA_TABLES
    # Remove protected tables (shouldn't happen, but safety first)
    orphaned = orphaned - PROTECTED_TABLES
    return orphaned


def drop_orphaned_tables(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    verbose: bool = False,
    force: bool = False
) -> List[Tuple[str, int]]:
    """
    Drop tables that are not defined in the schema.

    Returns list of (table_name, row_count) for dropped/would-be-dropped tables.
    """
    orphaned = detect_orphaned_tables(conn)
    dropped = []

    if not orphaned:
        if verbose:
            print("  No orphaned tables detected")
        return dropped

    for table in sorted(orphaned):
        row_count = get_table_row_count(conn, table)

        # Safety check for non-empty tables without force flag
        if row_count > 0 and not force and not dry_run:
            print(f"  ⚠️  Skipping '{table}' ({row_count} rows) - use --force to drop non-empty tables")
            continue

        if dry_run:
            print(f"  [DRY RUN] Would drop table '{table}' ({row_count} rows)")
        else:
            if verbose:
                print(f"  Dropping table '{table}' ({row_count} rows)...")
            cursor = conn.cursor()
            cursor.execute(f"DROP TABLE IF EXISTS {table}")
            conn.commit()
            if verbose:
                print(f"  ✓ Dropped '{table}'")

        dropped.append((table, row_count))

    return dropped


def cleanup_stale_data(
    conn: sqlite3.Connection,
    dry_run: bool = False,
    verbose: bool = False
) -> List[Tuple[str, int, str]]:
    """
    Clean up stale data based on rules.

    Returns list of (table_name, rows_deleted, description) for each cleanup.
    """
    existing_tables = get_existing_tables(conn)
    cleaned = []

    for table, condition, description in STALE_DATA_RULES:
        if table not in existing_tables:
            if verbose:
                print(f"  Table '{table}' not found, skipping")
            continue

        cursor = conn.cursor()

        # Count rows that would be deleted
        try:
            cursor.execute(f"SELECT COUNT(*) FROM {table} WHERE {condition}")
            count = cursor.fetchone()[0]
        except sqlite3.OperationalError as e:
            if verbose:
                print(f"  Error checking {table}: {e}")
            continue

        if count == 0:
            if verbose:
                print(f"  No {description} to clean")
            continue

        if dry_run:
            print(f"  [DRY RUN] Would delete {count} rows from '{table}' ({description})")
        else:
            if verbose:
                print(f"  Deleting {count} rows from '{table}' ({description})...")
            cursor.execute(f"DELETE FROM {table} WHERE {condition}")
            conn.commit()
            if verbose:
                print(f"  ✓ Deleted {count} rows")

        cleaned.append((table, count, description))

    return cleaned


def vacuum_database(conn: sqlite3.Connection, dry_run: bool = False, verbose: bool = False) -> int:
    """
    Vacuum the database to reclaim space.

    Returns the approximate bytes saved (based on file size change).
    """
    db_path = get_db_path()

    if not db_path.exists():
        return 0

    size_before = db_path.stat().st_size

    if dry_run:
        print(f"  [DRY RUN] Would vacuum database (current size: {size_before / 1024 / 1024:.2f} MB)")
        return 0

    if verbose:
        print(f"  Vacuuming database (current size: {size_before / 1024 / 1024:.2f} MB)...")

    # Close and reopen for vacuum (required by SQLite)
    conn.close()

    vacuum_conn = sqlite3.connect(str(db_path))
    vacuum_conn.execute("VACUUM")
    vacuum_conn.close()

    size_after = db_path.stat().st_size
    saved = size_before - size_after

    if verbose:
        print(f"  ✓ Vacuumed. New size: {size_after / 1024 / 1024:.2f} MB (saved {saved / 1024:.1f} KB)")

    return saved


def analyze_database(conn: sqlite3.Connection, verbose: bool = False) -> None:
    """Run ANALYZE to update query planner statistics."""
    if verbose:
        print("  Running ANALYZE...")
    conn.execute("ANALYZE")
    conn.commit()
    if verbose:
        print("  ✓ ANALYZE complete")


def list_all_tables(conn: sqlite3.Connection) -> None:
    """List all tables in the database with their status."""
    existing = get_existing_tables(conn)
    orphaned = detect_orphaned_tables(conn)

    print("\nAll tables in database:")
    print("-" * 60)

    for table in sorted(existing):
        row_count = get_table_row_count(conn, table)
        status = ""
        if table in orphaned:
            status = " [ORPHANED]"
        elif table in PROTECTED_TABLES:
            status = " [PROTECTED]"
        elif table not in SCHEMA_TABLES:
            status = " [UNKNOWN]"
        print(f"  {table}: {row_count} rows{status}")

    print("-" * 60)
    print(f"Total: {len(existing)} tables, {len(orphaned)} orphaned")


# =============================================================================
# Main
# =============================================================================

def main():
    parser = argparse.ArgumentParser(description="Database cleanup script")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be done without doing it")
    parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")
    parser.add_argument("--force", "-f", action="store_true", help="Force drop non-empty orphaned tables")
    parser.add_argument("--skip-vacuum", action="store_true", help="Skip vacuum step")
    parser.add_argument("--list", "-l", action="store_true", help="List all tables and their status")
    args = parser.parse_args()

    db_path = get_db_path()

    print(f"Database Cleanup - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Database: {db_path}")
    if args.dry_run:
        print("Mode: DRY RUN (no changes will be made)")
    print()

    if not db_path.exists():
        print("Database file not found. Nothing to clean.")
        return 0

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    # List mode - just show tables and exit
    if args.list:
        list_all_tables(conn)
        conn.close()
        return 0

    total_tables_dropped = 0
    total_rows_deleted = 0

    # 1. Detect and drop orphaned tables
    print("1. Checking for orphaned tables...")
    orphaned = detect_orphaned_tables(conn)
    if orphaned:
        print(f"   Found {len(orphaned)} orphaned table(s): {', '.join(sorted(orphaned))}")
    dropped = drop_orphaned_tables(conn, dry_run=args.dry_run, verbose=args.verbose, force=args.force)
    total_tables_dropped = len(dropped)
    if dropped:
        print(f"   {'Would drop' if args.dry_run else 'Dropped'} {len(dropped)} orphaned table(s)")
    elif not orphaned:
        print("   No orphaned tables found")
    print()

    # 2. Clean up stale data
    print("2. Cleaning up stale data...")
    cleaned = cleanup_stale_data(conn, dry_run=args.dry_run, verbose=args.verbose)
    total_rows_deleted = sum(count for _, count, _ in cleaned)
    if cleaned:
        print(f"   {'Would delete' if args.dry_run else 'Deleted'} {total_rows_deleted} row(s) across {len(cleaned)} table(s)")
    else:
        print("   No stale data found")
    print()

    # 3. Vacuum database (skip in dry run)
    if not args.skip_vacuum:
        print("3. Optimizing database...")
        if not args.dry_run:
            # Analyze first
            analyze_database(conn, verbose=args.verbose)
        saved = vacuum_database(conn, dry_run=args.dry_run, verbose=args.verbose)
        print()

    # Summary
    print("=" * 50)
    print("Summary:")
    print(f"  - Orphaned tables {'to drop' if args.dry_run else 'dropped'}: {total_tables_dropped}")
    print(f"  - Stale rows {'to delete' if args.dry_run else 'deleted'}: {total_rows_deleted}")
    if not args.dry_run and not args.skip_vacuum:
        print(f"  - Database optimized")
    print()

    if args.dry_run:
        print("Run without --dry-run to apply changes.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
