"""Move one install's session data out of a shared database into its own.

Shared-database clusters are retired: every worker owns a private database
and joins the others as a network device. Settings, profiles, keys and the
session catalog travel through the network once the new worker is enrolled,
but the executing state of the sessions an install ran (transcripts, the
fluid node tree, session store entries, messages) has to move once, from
the shared database to that install's new database. This tool does that.

On the install being migrated, pointing at the SHARED database:

    python -m scripts.network_migrate export --instance-id <its instance id> --out sessions.json

Then, after the install has been given a fresh database and has joined the
network (so its device id exists), pointing at the NEW database:

    python -m scripts.network_migrate import --bundle sessions.json

Rows are copied with ``INSERT ... ON CONFLICT DO NOTHING``; re-running an
import is harmless. Coordination rows (runs, turns, wakeups, checkouts) are
never copied: the new worker re-materializes them. ``--dry-run`` reports
counts only.
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import date, datetime
from typing import Any, Dict, Iterable, List

BUNDLE_VERSION = 1

# (table, column that names the session)
SESSION_TABLES = (
    ("sessions", "id"),
    ("session_messages", "session_id"),
    ("session_events", "session_id"),
    ("session_event_counters", "session_id"),
    ("session_store_entries", "session_id"),
    ("session_store_summaries", "session_id"),
    ("session_tags", "session_id"),
    ("pending_interactions", "session_id"),
    ("usage_log", "session_id"),
)
# Fluid transcript tables are keyed by the SDK session id.
FLUID_TABLES = (
    ("fluid_sessions", "session_id"),
    ("fluid_session_nodes", "session_id"),
    ("fluid_session_events", "session_id"),
)


def _encode(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"__bytes__": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, (datetime, date)):
        return {"__datetime__": value.isoformat()}
    if isinstance(value, (dict, list)):
        return {"__json__": json.dumps(value, default=str)}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, dict):
        if "__bytes__" in value:
            return base64.b64decode(value["__bytes__"])
        if "__datetime__" in value:
            return value["__datetime__"]
        if "__json__" in value:
            return value["__json__"]
    return value


def _table_exists(cursor, table: str) -> bool:
    from app.db.base import table_exists
    return table_exists(cursor, table)


def _column_exists(cursor, table: str, column: str) -> bool:
    from app.db.base import column_exists
    return column_exists(cursor, table, column)


def _rows(cursor, table: str, column: str, ids: List[str]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for start in range(0, len(ids), 200):
        chunk = ids[start:start + 200]
        placeholders = ",".join(["%s"] * len(chunk))
        cursor.execute(f"SELECT * FROM {table} WHERE {column} IN ({placeholders})", tuple(chunk))
        out.extend({k: _encode(v) for k, v in dict(r).items()} for r in cursor.fetchall())
    return out


def export_bundle(instance_id: str) -> Dict[str, Any]:
    from app.db.base import get_db
    bundle: Dict[str, Any] = {"version": BUNDLE_VERSION, "instance_id": instance_id, "tables": {}}
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT id, sdk_session_id FROM sessions WHERE instance_id = %s", (instance_id,))
        sessions = [dict(r) for r in cursor.fetchall()]
        session_ids = [s["id"] for s in sessions]
        sdk_ids = [s["sdk_session_id"] for s in sessions if s.get("sdk_session_id")]
        bundle["session_ids"] = session_ids
        for table, column in SESSION_TABLES:
            if _table_exists(cursor, table) and session_ids:
                bundle["tables"][table] = _rows(cursor, table, column, session_ids)
        for table, column in FLUID_TABLES:
            if _table_exists(cursor, table) and sdk_ids:
                bundle["tables"][table] = _rows(cursor, table, column, sdk_ids)
        for table in ("checkouts", "artifacts"):
            if _table_exists(cursor, table) and _column_exists(cursor, table, "runner_id"):
                cursor.execute(f"SELECT * FROM {table} WHERE runner_id = %s", (instance_id,))
                bundle["tables"][table] = [{k: _encode(v) for k, v in dict(r).items()} for r in cursor.fetchall()]
    return bundle


def import_bundle(bundle: Dict[str, Any], *, dry_run: bool = False) -> Dict[str, int]:
    from app.db.base import get_db
    if bundle.get("version") != BUNDLE_VERSION:
        raise SystemExit("Unsupported bundle version")
    counts: Dict[str, int] = {}
    order = [t for t, _ in SESSION_TABLES] + [t for t, _ in FLUID_TABLES] + ["checkouts", "artifacts"]
    with get_db() as conn:
        cursor = conn.cursor()
        try:
            from app.network import engine
            ctx = engine.context()
        except Exception:  # noqa: BLE001
            ctx = None
        device_id = ctx.device_id if ctx else None
        for table in order:
            rows = bundle.get("tables", {}).get(table) or []
            if not rows or not _table_exists(cursor, table):
                counts[table] = 0
                continue
            columns = [c for c in rows[0].keys() if _column_exists(cursor, table, c)]
            if table == "sessions" and device_id and "owner_device_id" in columns:
                for row in rows:
                    row["owner_device_id"] = device_id
            inserted = 0
            for row in rows:
                if dry_run:
                    inserted += 1
                    continue
                values = tuple(_decode(row.get(c)) for c in columns)
                cursor.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))}) "
                    f"ON CONFLICT DO NOTHING", values)
                inserted += cursor.rowcount or 0
            counts[table] = inserted
        if dry_run:
            conn.rollback()
    return counts


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    exp = sub.add_parser("export", help="write the sessions an install owns to a bundle (run against the shared database)")
    exp.add_argument("--instance-id", required=True)
    exp.add_argument("--out", required=True)
    imp = sub.add_parser("import", help="load a bundle into this worker's private database")
    imp.add_argument("--bundle", required=True)
    imp.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    from app.core.startup import init_core  # noqa: F401  (pool + schema)
    from app.db.base import init_pool
    from app.core.config import settings
    init_pool(min_conn=1, max_conn=2)
    if args.command == "export":
        bundle = export_bundle(args.instance_id)
        with open(args.out, "w", encoding="utf-8") as stream:
            json.dump(bundle, stream)
        sizes = {t: len(r) for t, r in bundle["tables"].items()}
        print(json.dumps({"instance_id": args.instance_id, "sessions": len(bundle["session_ids"]), "rows": sizes}, indent=2))
        return 0
    with open(args.bundle, "r", encoding="utf-8") as stream:
        bundle = json.load(stream)
    counts = import_bundle(bundle, dry_run=args.dry_run)
    print(json.dumps({"dry_run": args.dry_run, "inserted": counts, "instance_id": settings.effective_instance_id}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
