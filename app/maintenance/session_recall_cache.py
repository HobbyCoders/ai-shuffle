"""Opt-in, offline maintenance: cache SessionSearch JSON text. Never run at startup.

Ships in the runtime source archive, so an installed worker (Docker or
desktop, no git checkout) runs it from its installed source with the
worker's own interpreter and environment (#2083):

    python -m app.maintenance.session_recall_cache --apply
"""
import argparse
import os
from pathlib import Path
import shutil
import sys


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


class ManagedDataDirectoryError(RuntimeError):
    """Safe, credential-free diagnostic for a refused filesystem check."""


def managed_data_directory(cursor, data_dir: Path, desktop_user_data: Path | None = None) -> Path:
    """Match a readable managed PG16 layout to the connected loopback server.

    Container clusters live under DATA_DIR; Electron clusters live under
    userData, separately from the worker DATA_DIR. Only non-secret PostgreSQL
    markers are read, never the managed connection descriptor or desktop config.
    """
    roots = [Path(data_dir)]
    if desktop_user_data is not None:
        roots.append(Path(desktop_user_data))
    else:
        from app.core.platform import DeploymentMode, detect_deployment_mode

        if detect_deployment_mode() == DeploymentMode.LOCAL:
            if sys.platform == "win32":
                app_data = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
            elif sys.platform == "darwin":
                app_data = Path.home() / "Library" / "Application Support"
            else:
                app_data = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            roots.append(app_data / "AI Shuffle")
    cursor.execute("SELECT host(inet_server_addr()) AS host, inet_server_port() AS port, "
                   "EXTRACT(EPOCH FROM pg_postmaster_start_time())::bigint AS started")
    server = cursor.fetchone()
    if server["host"] not in {"127.0.0.1", "::1"}:
        raise ManagedDataDirectoryError("Refusing disk check: PostgreSQL is not a managed loopback server.")
    for root in roots:
        data = root.absolute() / "postgres" / "data"
        try:
            # Never follow a redirected layout or a marker symlink.
            markers = [data / "PG_VERSION", data / "postmaster.pid", data / "global" / "pg_control"]
            if any(p.is_symlink() for p in (data, *data.parents, *markers, data / "global", data / "base")):
                continue
            if not all(p.is_dir() and os.access(p, os.R_OK | os.X_OK)
                       for p in (data, data / "base", data / "global")):
                continue
            if not all(p.is_file() and os.access(p, os.R_OK) for p in markers):
                continue
            if markers[0].read_text(encoding="ascii").strip() != "16":
                continue
            pid = markers[1].read_text(encoding="utf-8").splitlines()
            if (len(pid) < 4 or int(pid[0]) <= 0 or Path(pid[1]) != data
                    or int(pid[3]) != server["port"] or abs(int(pid[2]) - server["started"]) > 1):
                continue
            return data
        except (OSError, ValueError, UnicodeError):
            continue
    raise ManagedDataDirectoryError(
        "Refusing disk check: managed PostgreSQL data directory is missing, unreadable or invalid, "
        "or does not match the connected server. Expected DATA_DIR/postgres/data or "
        "desktop userData/postgres/data; use --desktop-user-data for a custom desktop profile."
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.maintenance.session_recall_cache", description=(
        "Optional SessionSearch cache. Stop worker traffic and schedule maintenance: "
        "this rewrites three tables under exclusive locks, may take minutes and uses extra disk. "
        "It is not a background or startup migration. Without --apply no database is opened. "
        "On an installed worker, run it from the installed source directory with the worker's interpreter."
    ))
    parser.add_argument("--apply", action="store_true", help="Explicitly approve the offline table rewrites")
    parser.add_argument("--desktop-user-data", type=Path, help=(
        "Electron userData directory for a custom/preview desktop profile (not the worker DATA_DIR)"
    ))
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
            data = managed_data_directory(cursor, base.settings.effective_data_dir, args.desktop_user_data)
            try:
                free = shutil.disk_usage(data).free
            except OSError:
                raise ManagedDataDirectoryError(
                    "Refusing disk check: managed PostgreSQL data directory cannot be measured."
                ) from None
            apply_cache(cursor, free, lock_timeout_ms=args.lock_timeout_ms,
                        statement_timeout_ms=args.statement_timeout_ms)
        print("Recall cache committed; the optional cache is now available to searches.")
        return 0
    except Exception as exc:  # noqa: BLE001 — do not print connection/transcript details
        if isinstance(exc, ManagedDataDirectoryError):
            print(str(exc))
        print(f"Recall maintenance failed ({type(exc).__name__}); uncommitted changes were rolled back. "
              "Check free space, permissions and lock contention before retrying. Legacy search remains available.")
        return 1
    finally:
        base.close_pool()


if __name__ == "__main__":
    raise SystemExit(main())
