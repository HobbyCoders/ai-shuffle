"""Opt-in, offline maintenance: cache SessionSearch JSON text. Never run at startup."""
import argparse
import shutil


def apply_cache(cursor, free_bytes: int, *, lock_timeout_ms: int = 2000,
                statement_timeout_ms: int = 300000) -> None:
    """Caller owns the transaction; any failure must roll it back in full."""
    if min(lock_timeout_ms, statement_timeout_ms) <= 0:
        raise ValueError("Timeouts must be positive")
    from app.db.schema_postgres import create_session_recall_text, session_recall_cache_ready

    cursor.execute(f"SET LOCAL lock_timeout = '{int(lock_timeout_ms)}ms'")
    cursor.execute(f"SET LOCAL statement_timeout = '{int(statement_timeout_ms)}ms'")
    if session_recall_cache_ready(cursor):
        # Also finish retirement on databases migrated by the first PR revision.
        create_session_recall_text(cursor)
        return
    cursor.execute(
        "SELECT COALESCE(SUM(pg_total_relation_size(c.oid)), 0) AS bytes "
        "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = current_schema() "
        "AND c.relname IN ('fluid_session_nodes', 'session_store_entries', 'session_events')"
    )
    required = 3 * int(cursor.fetchone()["bytes"]) + 1024 ** 3
    if free_bytes < required:
        raise RuntimeError("Insufficient free space for the estimated rewrite peak plus reserve")
    create_session_recall_text(cursor)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=(
        "Optional SessionSearch cache. Stop worker traffic and schedule maintenance: "
        "this rewrites three tables under exclusive locks, may take minutes and uses extra disk. "
        "It is not a background or startup migration. Without --apply no database is opened."
    ))
    parser.add_argument("--apply", action="store_true", help="Explicitly approve the offline table rewrites")
    parser.add_argument("--lock-timeout-ms", type=int, default=2000)
    parser.add_argument("--statement-timeout-ms", type=int, default=300000)
    args = parser.parse_args(argv)
    if not args.apply:
        parser.print_help()
        return 0
    if min(args.lock_timeout_ms, args.statement_timeout_ms) <= 0:
        parser.error("timeouts must be positive")
    from app.db import base

    try:
        base.init_pool(min_conn=1, max_conn=1)
        with base.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SHOW data_directory")
            # Managed PG is local. Refuse when its filesystem cannot be checked.
            free = shutil.disk_usage(cursor.fetchone()["data_directory"]).free
            apply_cache(cursor, free, lock_timeout_ms=args.lock_timeout_ms,
                        statement_timeout_ms=args.statement_timeout_ms)
        print("Recall cache committed; the optional cache is now available to searches.")
        return 0
    except Exception as exc:  # noqa: BLE001 — do not print connection/transcript details
        print(f"Recall maintenance failed ({type(exc).__name__}); uncommitted changes were rolled back. "
              "Check free space, permissions and lock contention before retrying. Legacy search remains available.")
        return 1
    finally:
        base.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
