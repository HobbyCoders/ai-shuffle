"""Worker-network tables: network state, members, sync log, watermarks, conflicts.

Everything here is local to one worker's private database. Nothing in these
tables is ever copied as-is to another worker; the sync engine reads them to
build envelopes and writes them while applying envelopes it received.

Schema is created by :func:`create_schema`, which ``schema_postgres`` calls
for fresh databases and ``database._run_column_migrations`` calls on upgrade
(every statement is idempotent).
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from app.db.base import get_db, row_to_dict, rows_to_list

logger = logging.getLogger(__name__)

CONFLICT_RETENTION_DAYS = 30
LANE_SETTINGS = "settings"
LANE_HISTORY = "history"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, default=str)


def _load(value: Any, default=None):
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def create_schema(cursor) -> None:
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS network_state (
            id INTEGER PRIMARY KEY DEFAULT 1,
            network_id TEXT NOT NULL,
            name TEXT,
            founder_device_id TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            key_version INTEGER NOT NULL DEFAULT 1,
            clock BIGINT NOT NULL DEFAULT 0,
            bootstrap_state TEXT NOT NULL DEFAULT 'ready',
            keyring JSONB NOT NULL DEFAULT '{}',
            recovery JSONB,
            hub JSONB,
            legacy_instance_map JSONB NOT NULL DEFAULT '{}',
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            CHECK (id = 1)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS network_members (
            device_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'worker',
            platform TEXT,
            signing_public TEXT NOT NULL,
            encryption_public TEXT NOT NULL,
            generation INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            last_seq_accepted BIGINT,
            incarnation INTEGER NOT NULL DEFAULT 1,
            capabilities JSONB NOT NULL DEFAULT '{}',
            endpoints JSONB NOT NULL DEFAULT '[]',
            version TEXT,
            legacy_instance_id TEXT,
            last_seen TIMESTAMP WITH TIME ZONE,
            added_by TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS network_membership_records (
            id TEXT PRIMARY KEY,
            record JSONB NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_ops (
            ingest_id BIGSERIAL PRIMARY KEY,
            origin_device_id TEXT NOT NULL,
            incarnation INTEGER NOT NULL,
            seq BIGINT NOT NULL,
            collection TEXT NOT NULL,
            record_id TEXT NOT NULL,
            op TEXT NOT NULL,
            clock BIGINT NOT NULL,
            lane TEXT NOT NULL DEFAULT 'settings',
            envelope JSONB NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (origin_device_id, incarnation, seq)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_sync_ops_record ON sync_ops(collection, record_id)")
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_records (
            collection TEXT NOT NULL,
            record_id TEXT NOT NULL,
            clock BIGINT NOT NULL,
            origin_device_id TEXT NOT NULL,
            content_hash TEXT NOT NULL DEFAULT '',
            deleted BOOLEAN NOT NULL DEFAULT FALSE,
            order_seq BIGINT,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            PRIMARY KEY (collection, record_id)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_watermarks (
            origin_device_id TEXT NOT NULL,
            incarnation INTEGER NOT NULL,
            last_seq BIGINT NOT NULL DEFAULT 0,
            min_retained_seq BIGINT NOT NULL DEFAULT 1,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            PRIMARY KEY (origin_device_id, incarnation)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_pending (
            origin_device_id TEXT NOT NULL,
            incarnation INTEGER NOT NULL,
            seq BIGINT NOT NULL,
            envelope JSONB NOT NULL,
            received_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            PRIMARY KEY (origin_device_id, incarnation, seq)
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_conflicts (
            id BIGSERIAL PRIMARY KEY,
            collection TEXT NOT NULL,
            record_id TEXT NOT NULL,
            losing_clock BIGINT NOT NULL,
            losing_device_id TEXT NOT NULL,
            losing_record JSONB,
            winning_clock BIGINT NOT NULL,
            winning_device_id TEXT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            expires_at TIMESTAMP WITH TIME ZONE NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_peers (
            device_id TEXT PRIMARY KEY,
            base_url TEXT,
            delivered_incarnation INTEGER NOT NULL DEFAULT 0,
            delivered_seq BIGINT NOT NULL DEFAULT 0,
            last_contact TIMESTAMP WITH TIME ZONE,
            last_error TEXT,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS network_joins (
            id TEXT PRIMARY KEY,
            role TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            device_id TEXT,
            name TEXT,
            platform TEXT,
            signing_public TEXT,
            encryption_public TEXT,
            fingerprint TEXT,
            grant_id TEXT,
            poll_secret_hash TEXT,
            endpoint_url TEXT,
            inviter_fingerprint TEXT,
            legacy_instance_id TEXT,
            response JSONB,
            error TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            expires_at TIMESTAMP WITH TIME ZONE
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS network_plugins_desired (
            id TEXT PRIMARY KEY,
            version TEXT,
            enabled BOOLEAN NOT NULL DEFAULT TRUE,
            config JSONB NOT NULL DEFAULT '{}',
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS network_memory_records (
            id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            scope TEXT NOT NULL DEFAULT '',
            content TEXT NOT NULL DEFAULT '',
            content_hash TEXT NOT NULL DEFAULT '',
            deleted BOOLEAN NOT NULL DEFAULT FALSE,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS network_catalog_cursors (
            session_id TEXT PRIMARY KEY,
            published_seq BIGINT NOT NULL DEFAULT 0,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS network_presence (
            device_id TEXT PRIMARY KEY,
            endpoints JSONB NOT NULL DEFAULT '[]',
            capabilities JSONB NOT NULL DEFAULT '{}',
            version TEXT,
            platform TEXT,
            incarnation INTEGER NOT NULL DEFAULT 1,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sync_blobs (
            hash TEXT PRIMARY KEY,
            size BIGINT NOT NULL DEFAULT 0,
            present BOOLEAN NOT NULL DEFAULT FALSE,
            holders JSONB NOT NULL DEFAULT '[]',
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)


# ---------------------------------------------------------------------------
# Network state
# ---------------------------------------------------------------------------

def _state_row(row) -> Optional[dict]:
    if not row:
        return None
    state = row_to_dict(row)
    for key in ("keyring", "recovery", "hub", "legacy_instance_map"):
        state[key] = _load(state.get(key), {} if key != "recovery" and key != "hub" else None)
    return state


def get_state(_conn=None) -> Optional[dict]:
    if _conn is not None:
        cursor = _conn.cursor()
        cursor.execute("SELECT * FROM network_state WHERE id = 1")
        return _state_row(cursor.fetchone())
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM network_state WHERE id = 1")
        return _state_row(cursor.fetchone())


def create_state(*, network_id: str, name: Optional[str], founder_device_id: str, keyring: Dict[str, Any],
                 bootstrap_state: str = "ready", generation: int = 0, key_version: int = 1,
                 legacy_instance_map: Optional[Dict[str, str]] = None, _conn=None) -> dict:
    sql = """INSERT INTO network_state (id, network_id, name, founder_device_id, generation, key_version, clock,
                                        bootstrap_state, keyring, legacy_instance_map, created_at, updated_at)
             VALUES (1, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s, %s)
             ON CONFLICT (id) DO NOTHING"""
    now = _now()
    params = (network_id, name, founder_device_id, generation, key_version, bootstrap_state, _json(keyring),
              _json(legacy_instance_map or {}), now, now)
    if _conn is not None:
        _conn.cursor().execute(sql, params)
        return get_state(_conn)
    with get_db() as conn:
        conn.cursor().execute(sql, params)
    return get_state()


def update_state(_conn=None, **fields) -> Optional[dict]:
    if not fields:
        return get_state(_conn)
    sets, params = [], []
    for key, value in fields.items():
        if key not in ("name", "generation", "key_version", "clock", "bootstrap_state", "keyring", "recovery",
                       "hub", "legacy_instance_map", "founder_device_id"):
            raise ValueError("Unknown network_state field: " + key)
        if key in ("keyring", "recovery", "hub", "legacy_instance_map"):
            value = None if value is None else _json(value)
        sets.append(f"{key} = %s")
        params.append(value)
    sets.append("updated_at = %s")
    params.append(_now())
    sql = "UPDATE network_state SET " + ", ".join(sets) + " WHERE id = 1"
    if _conn is not None:
        _conn.cursor().execute(sql, tuple(params))
        return get_state(_conn)
    with get_db() as conn:
        conn.cursor().execute(sql, tuple(params))
    return get_state()


def delete_state(_conn=None) -> None:
    sql = "DELETE FROM network_state WHERE id = 1"
    if _conn is not None:
        _conn.cursor().execute(sql)
        return
    with get_db() as conn:
        conn.cursor().execute(sql)


def lock_state(cursor) -> Optional[dict]:
    """Row-lock the state for a clock or sequence bump (the shim ignores FOR UPDATE)."""
    cursor.execute("SELECT * FROM network_state WHERE id = 1 FOR UPDATE")
    return _state_row(cursor.fetchone())


def bump_clock(cursor, observed: int = 0) -> int:
    """Lamport tick: max(local, observed) + 1, persisted."""
    cursor.execute("UPDATE network_state SET clock = CASE WHEN clock > %s THEN clock ELSE %s END + 1 WHERE id = 1",
                   (observed, observed))
    cursor.execute("SELECT clock FROM network_state WHERE id = 1")
    row = cursor.fetchone()
    return int(row["clock"]) if row else 0


def observe_clock(cursor, observed: int) -> None:
    cursor.execute("UPDATE network_state SET clock = CASE WHEN clock > %s THEN clock ELSE %s END WHERE id = 1",
                   (observed, observed))


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

def _member_row(row) -> Optional[dict]:
    if not row:
        return None
    member = row_to_dict(row)
    member["capabilities"] = _load(member.get("capabilities"), {}) or {}
    member["endpoints"] = _load(member.get("endpoints"), []) or []
    return member


def upsert_member(*, device_id: str, name: str, kind: str, signing_public: str, encryption_public: str,
                  generation: int, status: str = "active", platform: Optional[str] = None,
                  added_by: Optional[str] = None, legacy_instance_id: Optional[str] = None,
                  last_seq_accepted: Optional[int] = None, _conn=None) -> dict:
    sql = """INSERT INTO network_members (device_id, name, kind, platform, signing_public, encryption_public,
                                          generation, status, last_seq_accepted, legacy_instance_id, added_by,
                                          created_at, updated_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
             ON CONFLICT (device_id) DO UPDATE SET
                 name = EXCLUDED.name, kind = EXCLUDED.kind,
                 platform = COALESCE(EXCLUDED.platform, network_members.platform),
                 signing_public = EXCLUDED.signing_public, encryption_public = EXCLUDED.encryption_public,
                 generation = EXCLUDED.generation, status = EXCLUDED.status,
                 last_seq_accepted = EXCLUDED.last_seq_accepted,
                 legacy_instance_id = COALESCE(EXCLUDED.legacy_instance_id, network_members.legacy_instance_id),
                 added_by = COALESCE(EXCLUDED.added_by, network_members.added_by),
                 updated_at = EXCLUDED.updated_at"""
    now = _now()
    params = (device_id, name, kind, platform, signing_public, encryption_public, generation, status,
              last_seq_accepted, legacy_instance_id, added_by, now, now)
    if _conn is not None:
        _conn.cursor().execute(sql, params)
        return get_member(device_id, _conn=_conn)
    with get_db() as conn:
        conn.cursor().execute(sql, params)
    return get_member(device_id)


def get_member(device_id: str, _conn=None) -> Optional[dict]:
    sql = "SELECT * FROM network_members WHERE device_id = %s"
    if _conn is not None:
        cursor = _conn.cursor()
        cursor.execute(sql, (device_id,))
        return _member_row(cursor.fetchone())
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, (device_id,))
        return _member_row(cursor.fetchone())


def list_members(_conn=None, *, include_revoked: bool = True) -> List[dict]:
    sql = "SELECT * FROM network_members" + ("" if include_revoked else " WHERE status = 'active'") + " ORDER BY created_at, device_id"
    if _conn is not None:
        cursor = _conn.cursor()
        cursor.execute(sql)
        return [_member_row(r) for r in cursor.fetchall()]
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(sql)
        return [_member_row(r) for r in cursor.fetchall()]


def touch_member(device_id: str, *, capabilities: Optional[dict] = None, endpoints: Optional[list] = None,
                 version: Optional[str] = None, incarnation: Optional[int] = None, name: Optional[str] = None,
                 platform: Optional[str] = None, _conn=None) -> None:
    sets = ["last_seen = %s", "updated_at = %s"]
    now = _now()
    params: List[Any] = [now, now]
    if capabilities is not None:
        sets.append("capabilities = %s")
        params.append(_json(capabilities))
    if endpoints is not None:
        sets.append("endpoints = %s")
        params.append(_json(endpoints))
    if version is not None:
        sets.append("version = %s")
        params.append(version)
    if incarnation is not None:
        sets.append("incarnation = %s")
        params.append(incarnation)
    if name:
        sets.append("name = %s")
        params.append(name)
    if platform:
        sets.append("platform = %s")
        params.append(platform)
    params.append(device_id)
    sql = "UPDATE network_members SET " + ", ".join(sets) + " WHERE device_id = %s"
    if _conn is not None:
        _conn.cursor().execute(sql, tuple(params))
        return
    with get_db() as conn:
        conn.cursor().execute(sql, tuple(params))


def rename_member(device_id: str, name: str) -> Optional[dict]:
    with get_db() as conn:
        conn.cursor().execute("UPDATE network_members SET name = %s, updated_at = %s WHERE device_id = %s",
                              (name, _now(), device_id))
    return get_member(device_id)


def delete_all_members(_conn=None) -> None:
    sql = "DELETE FROM network_members"
    if _conn is not None:
        _conn.cursor().execute(sql)
        return
    with get_db() as conn:
        conn.cursor().execute(sql)


# ---------------------------------------------------------------------------
# Membership records (the synced collection's local table)
# ---------------------------------------------------------------------------

def put_membership_record(record_id: str, record: dict, _conn=None) -> None:
    sql = """INSERT INTO network_membership_records (id, record, created_at) VALUES (%s, %s, %s)
             ON CONFLICT (id) DO UPDATE SET record = EXCLUDED.record"""
    params = (record_id, _json(record), _now())
    if _conn is not None:
        _conn.cursor().execute(sql, params)
        return
    with get_db() as conn:
        conn.cursor().execute(sql, params)


def list_membership_records(_conn=None) -> List[dict]:
    sql = "SELECT id, record, created_at FROM network_membership_records ORDER BY created_at, id"
    def _rows(cursor):
        cursor.execute(sql)
        out = []
        for row in cursor.fetchall():
            item = row_to_dict(row)
            item["record"] = _load(item.get("record"), {})
            out.append(item)
        return out
    if _conn is not None:
        return _rows(_conn.cursor())
    with get_db() as conn:
        return _rows(conn.cursor())


# ---------------------------------------------------------------------------
# Sync ops log
# ---------------------------------------------------------------------------

def next_origin_seq(cursor, device_id: str, incarnation: int) -> int:
    cursor.execute("SELECT MAX(seq) AS s FROM sync_ops WHERE origin_device_id = %s AND incarnation = %s",
                   (device_id, incarnation))
    row = cursor.fetchone()
    current = row["s"] if row and row["s"] is not None else 0
    return int(current) + 1


def insert_op(cursor, env: dict, lane: str) -> bool:
    cursor.execute(
        """INSERT INTO sync_ops (origin_device_id, incarnation, seq, collection, record_id, op, clock, lane, envelope, created_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (origin_device_id, incarnation, seq) DO NOTHING""",
        (env["origin_device_id"], env["origin_incarnation"], env["origin_seq"], env["collection"], env["record_id"],
         env["op"], env["clock"], lane, _json(env), _now()))
    return bool(cursor.rowcount)


def ops_since(watermarks: Dict[str, Dict[str, int]], *, limit: int = 500, lane: Optional[str] = None,
              exclude_origin: Optional[str] = None, _conn=None) -> List[dict]:
    """Ops the caller lacks: per origin/incarnation, everything past its watermark.

    ``watermarks`` is ``{origin_device_id: {incarnation: last_seq}}``. Origins
    the caller has never seen come back from seq 1. Results are ordered per
    origin by (incarnation, seq) so the receiver can apply them in order.
    """
    def _query(cursor):
        cursor.execute("SELECT DISTINCT origin_device_id, incarnation FROM sync_ops ORDER BY origin_device_id, incarnation")
        streams = [(r["origin_device_id"], int(r["incarnation"])) for r in cursor.fetchall()]
        out: List[dict] = []
        for origin, incarnation in streams:
            if origin == exclude_origin:
                continue
            last = int((watermarks.get(origin) or {}).get(str(incarnation), (watermarks.get(origin) or {}).get(incarnation, 0)) or 0)
            budget = limit - len(out)
            if budget <= 0:
                break
            params: List[Any] = [origin, incarnation, last]
            sql = "SELECT envelope FROM sync_ops WHERE origin_device_id = %s AND incarnation = %s AND seq > %s"
            if lane:
                sql += " AND lane = %s"
                params.append(lane)
            sql += " ORDER BY seq LIMIT %s"
            params.append(budget)
            cursor.execute(sql, tuple(params))
            out.extend(_load(r["envelope"], {}) for r in cursor.fetchall())
        return out
    if _conn is not None:
        return _query(_conn.cursor())
    with get_db() as conn:
        return _query(conn.cursor())


def ops_after_ingest(ingest_id: int, *, limit: int = 200, _conn=None) -> List[Tuple[int, dict]]:
    def _query(cursor):
        cursor.execute("SELECT ingest_id, envelope FROM sync_ops WHERE ingest_id > %s ORDER BY ingest_id LIMIT %s",
                       (ingest_id, limit))
        return [(int(r["ingest_id"]), _load(r["envelope"], {})) for r in cursor.fetchall()]
    if _conn is not None:
        return _query(_conn.cursor())
    with get_db() as conn:
        return _query(conn.cursor())


def own_watermark(cursor, device_id: str, incarnation: int) -> int:
    return next_origin_seq(cursor, device_id, incarnation) - 1


def local_watermarks(_conn=None) -> Dict[str, Dict[str, int]]:
    """Everything this worker holds, as ``{origin: {incarnation: last_seq}}``.

    Includes our own outbox (from sync_ops) so a peer knows what to skip.
    """
    def _query(cursor):
        cursor.execute("SELECT origin_device_id, incarnation, MAX(seq) AS s FROM sync_ops GROUP BY origin_device_id, incarnation")
        out: Dict[str, Dict[str, int]] = {}
        for row in cursor.fetchall():
            out.setdefault(row["origin_device_id"], {})[str(int(row["incarnation"]))] = int(row["s"] or 0)
        cursor.execute("SELECT origin_device_id, incarnation, last_seq FROM sync_watermarks")
        for row in cursor.fetchall():
            bucket = out.setdefault(row["origin_device_id"], {})
            key = str(int(row["incarnation"]))
            bucket[key] = max(bucket.get(key, 0), int(row["last_seq"]))
        return out
    if _conn is not None:
        return _query(_conn.cursor())
    with get_db() as conn:
        return _query(conn.cursor())


def count_ops(_conn=None) -> int:
    def _query(cursor):
        cursor.execute("SELECT COUNT(*) AS n FROM sync_ops")
        return int(cursor.fetchone()["n"])
    if _conn is not None:
        return _query(_conn.cursor())
    with get_db() as conn:
        return _query(conn.cursor())


def prune_ops(*, keep_days: int = 30, _conn=None) -> int:
    """Drop old ops but never a record's latest op (snapshots still need it)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).isoformat()
    sql = """DELETE FROM sync_ops WHERE created_at < %s AND ingest_id NOT IN (
                 SELECT MAX(ingest_id) FROM sync_ops GROUP BY collection, record_id)"""
    if _conn is not None:
        cursor = _conn.cursor()
        cursor.execute(sql, (cutoff,))
        return cursor.rowcount or 0
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, (cutoff,))
        return cursor.rowcount or 0


# ---------------------------------------------------------------------------
# Record versions
# ---------------------------------------------------------------------------

def get_record(cursor, collection: str, record_id: str) -> Optional[dict]:
    cursor.execute("SELECT * FROM sync_records WHERE collection = %s AND record_id = %s", (collection, record_id))
    row = cursor.fetchone()
    return row_to_dict(row) if row else None


def put_record(cursor, collection: str, record_id: str, *, clock: int, origin_device_id: str, content_hash: str,
               deleted: bool, order_seq: Optional[int] = None) -> None:
    cursor.execute(
        """INSERT INTO sync_records (collection, record_id, clock, origin_device_id, content_hash, deleted, order_seq, updated_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
           ON CONFLICT (collection, record_id) DO UPDATE SET
               clock = EXCLUDED.clock, origin_device_id = EXCLUDED.origin_device_id,
               content_hash = EXCLUDED.content_hash, deleted = EXCLUDED.deleted,
               order_seq = EXCLUDED.order_seq, updated_at = EXCLUDED.updated_at""",
        (collection, record_id, clock, origin_device_id, content_hash, bool(deleted), order_seq, _now()))


def records_for_collection(cursor, collection: str) -> Dict[str, dict]:
    cursor.execute("SELECT * FROM sync_records WHERE collection = %s", (collection,))
    return {r["record_id"]: row_to_dict(r) for r in cursor.fetchall()}


def count_records(_conn=None) -> Dict[str, int]:
    def _query(cursor):
        cursor.execute("SELECT collection, COUNT(*) AS n FROM sync_records WHERE deleted = FALSE GROUP BY collection")
        return {r["collection"]: int(r["n"]) for r in cursor.fetchall()}
    if _conn is not None:
        return _query(_conn.cursor())
    with get_db() as conn:
        return _query(conn.cursor())


# ---------------------------------------------------------------------------
# Watermarks and pending (out-of-order) ops
# ---------------------------------------------------------------------------

def get_watermark(cursor, origin: str, incarnation: int) -> int:
    cursor.execute("SELECT last_seq FROM sync_watermarks WHERE origin_device_id = %s AND incarnation = %s",
                   (origin, incarnation))
    row = cursor.fetchone()
    return int(row["last_seq"]) if row else 0


def set_watermark(cursor, origin: str, incarnation: int, last_seq: int) -> None:
    cursor.execute(
        """INSERT INTO sync_watermarks (origin_device_id, incarnation, last_seq, updated_at)
           VALUES (%s, %s, %s, %s)
           ON CONFLICT (origin_device_id, incarnation) DO UPDATE SET
               last_seq = EXCLUDED.last_seq, updated_at = EXCLUDED.updated_at""",
        (origin, incarnation, last_seq, _now()))


def highest_incarnation(cursor, origin: str) -> int:
    cursor.execute("SELECT MAX(incarnation) AS i FROM sync_watermarks WHERE origin_device_id = %s", (origin,))
    row = cursor.fetchone()
    return int(row["i"]) if row and row["i"] is not None else 0


def stash_pending(cursor, env: dict) -> None:
    cursor.execute(
        """INSERT INTO sync_pending (origin_device_id, incarnation, seq, envelope, received_at)
           VALUES (%s, %s, %s, %s, %s)
           ON CONFLICT (origin_device_id, incarnation, seq) DO NOTHING""",
        (env["origin_device_id"], env["origin_incarnation"], env["origin_seq"], _json(env), _now()))


def pop_pending(cursor, origin: str, incarnation: int, seq: int) -> Optional[dict]:
    cursor.execute("SELECT envelope FROM sync_pending WHERE origin_device_id = %s AND incarnation = %s AND seq = %s",
                   (origin, incarnation, seq))
    row = cursor.fetchone()
    if not row:
        return None
    cursor.execute("DELETE FROM sync_pending WHERE origin_device_id = %s AND incarnation = %s AND seq = %s",
                   (origin, incarnation, seq))
    return _load(row["envelope"], {})


def pending_holes(_conn=None) -> List[dict]:
    """Streams with buffered out-of-order ops: what we hold and what is missing."""
    def _query(cursor):
        cursor.execute("SELECT origin_device_id, incarnation, MIN(seq) AS lo, COUNT(*) AS n FROM sync_pending GROUP BY origin_device_id, incarnation")
        out = []
        for row in cursor.fetchall():
            out.append({"origin_device_id": row["origin_device_id"], "incarnation": int(row["incarnation"]),
                        "first_pending": int(row["lo"]), "count": int(row["n"]),
                        "watermark": get_watermark(cursor, row["origin_device_id"], int(row["incarnation"]))})
        return out
    if _conn is not None:
        return _query(_conn.cursor())
    with get_db() as conn:
        return _query(conn.cursor())


def clear_pending_stream(cursor, origin: str, incarnation: int) -> None:
    cursor.execute("DELETE FROM sync_pending WHERE origin_device_id = %s AND incarnation = %s", (origin, incarnation))


# ---------------------------------------------------------------------------
# Conflicts
# ---------------------------------------------------------------------------

def add_conflict(cursor, *, collection: str, record_id: str, losing_clock: int, losing_device_id: str,
                 losing_record: Any, winning_clock: int, winning_device_id: str) -> None:
    now = datetime.now(timezone.utc)
    cursor.execute(
        """INSERT INTO sync_conflicts (collection, record_id, losing_clock, losing_device_id, losing_record,
                                       winning_clock, winning_device_id, created_at, expires_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (collection, record_id, losing_clock, losing_device_id, _json(losing_record), winning_clock,
         winning_device_id, now.isoformat(), (now + timedelta(days=CONFLICT_RETENTION_DAYS)).isoformat()))


def list_conflicts(limit: int = 200) -> List[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sync_conflicts WHERE expires_at > %s ORDER BY created_at DESC LIMIT %s",
                       (_now(), limit))
        rows = rows_to_list(cursor.fetchall())
    for row in rows:
        row["losing_record"] = _load(row.get("losing_record"))
    return rows


def delete_conflict(conflict_id: int) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sync_conflicts WHERE id = %s", (conflict_id,))
        return bool(cursor.rowcount)


def expire_conflicts() -> int:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM sync_conflicts WHERE expires_at <= %s", (_now(),))
        return cursor.rowcount or 0


# ---------------------------------------------------------------------------
# Peers
# ---------------------------------------------------------------------------

def upsert_peer(device_id: str, *, base_url: Optional[str] = None, delivered: Optional[Tuple[int, int]] = None,
                error: Optional[str] = None, contacted: bool = False, _conn=None) -> None:
    now = _now()
    sql = """INSERT INTO sync_peers (device_id, base_url, delivered_incarnation, delivered_seq, last_contact, last_error, updated_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s)
             ON CONFLICT (device_id) DO UPDATE SET
                 base_url = COALESCE(EXCLUDED.base_url, sync_peers.base_url),
                 delivered_incarnation = CASE WHEN %s THEN EXCLUDED.delivered_incarnation ELSE sync_peers.delivered_incarnation END,
                 delivered_seq = CASE WHEN %s THEN EXCLUDED.delivered_seq ELSE sync_peers.delivered_seq END,
                 last_contact = COALESCE(EXCLUDED.last_contact, sync_peers.last_contact),
                 last_error = EXCLUDED.last_error,
                 updated_at = EXCLUDED.updated_at"""
    inc, seq = delivered if delivered else (0, 0)
    has_delivered = delivered is not None
    params = (device_id, base_url, inc, seq, now if contacted else None, error, now, has_delivered, has_delivered)
    if _conn is not None:
        _conn.cursor().execute(sql, params)
        return
    with get_db() as conn:
        conn.cursor().execute(sql, params)


def list_peers() -> List[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sync_peers ORDER BY device_id")
        return rows_to_list(cursor.fetchall())


# ---------------------------------------------------------------------------
# Joins
# ---------------------------------------------------------------------------

def _join_row(row) -> Optional[dict]:
    if not row:
        return None
    join = row_to_dict(row)
    join["response"] = _load(join.get("response"))
    return join


def create_join(join: dict) -> dict:
    columns = ["id", "role", "status", "device_id", "name", "platform", "signing_public", "encryption_public",
               "fingerprint", "grant_id", "poll_secret_hash", "endpoint_url", "inviter_fingerprint",
               "legacy_instance_id", "response", "error", "created_at", "updated_at", "expires_at"]
    now = _now()
    values = {**join, "created_at": now, "updated_at": now}
    if "response" in values and values["response"] is not None:
        values["response"] = _json(values["response"])
    params = tuple(values.get(c) for c in columns)
    with get_db() as conn:
        conn.cursor().execute(
            f"INSERT INTO network_joins ({', '.join(columns)}) VALUES ({', '.join(['%s'] * len(columns))})", params)
    return get_join(join["id"])


def get_join(join_id: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM network_joins WHERE id = %s", (join_id,))
        return _join_row(cursor.fetchone())


def update_join(join_id: str, **fields) -> Optional[dict]:
    sets, params = [], []
    for key, value in fields.items():
        if key == "response":
            value = None if value is None else _json(value)
        sets.append(f"{key} = %s")
        params.append(value)
    sets.append("updated_at = %s")
    params.extend([_now(), join_id])
    with get_db() as conn:
        conn.cursor().execute("UPDATE network_joins SET " + ", ".join(sets) + " WHERE id = %s", tuple(params))
    return get_join(join_id)


def list_joins(role: Optional[str] = None, status: Optional[str] = None) -> List[dict]:
    sql, params = "SELECT * FROM network_joins", []
    clauses = []
    if role:
        clauses.append("role = %s")
        params.append(role)
    if status:
        clauses.append("status = %s")
        params.append(status)
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY created_at DESC"
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, tuple(params))
        return [_join_row(r) for r in cursor.fetchall()]


def delete_join(join_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("DELETE FROM network_joins WHERE id = %s", (join_id,))
        return bool(cursor.rowcount)


def expire_joins() -> int:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE network_joins SET status = 'expired', updated_at = %s WHERE status = 'pending' AND expires_at IS NOT NULL AND expires_at < %s",
                       (_now(), _now()))
        return cursor.rowcount or 0


# ---------------------------------------------------------------------------
# Blobs
# ---------------------------------------------------------------------------

def upsert_blob(hash_value: str, *, size: int, present: Optional[bool] = None, holder: Optional[str] = None,
                _conn=None) -> None:
    def _apply(cursor):
        cursor.execute("SELECT holders, present FROM sync_blobs WHERE hash = %s", (hash_value,))
        row = cursor.fetchone()
        holders = list(_load(row["holders"], []) or []) if row else []
        if holder and holder not in holders:
            holders.append(holder)
        is_present = bool(row["present"]) if row and present is None else bool(present)
        cursor.execute(
            """INSERT INTO sync_blobs (hash, size, present, holders, updated_at) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (hash) DO UPDATE SET size = EXCLUDED.size, present = EXCLUDED.present,
                   holders = EXCLUDED.holders, updated_at = EXCLUDED.updated_at""",
            (hash_value, size, is_present, _json(holders), _now()))
    if _conn is not None:
        _apply(_conn.cursor())
        return
    with get_db() as conn:
        _apply(conn.cursor())


def get_blob(hash_value: str) -> Optional[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sync_blobs WHERE hash = %s", (hash_value,))
        row = cursor.fetchone()
    if not row:
        return None
    blob = row_to_dict(row)
    blob["holders"] = _load(blob.get("holders"), []) or []
    blob["present"] = bool(blob.get("present"))
    return blob


def missing_blobs(limit: int = 50) -> List[dict]:
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM sync_blobs WHERE present = FALSE ORDER BY updated_at LIMIT %s", (limit,))
        rows = rows_to_list(cursor.fetchall())
    for row in rows:
        row["holders"] = _load(row.get("holders"), []) or []
    return rows


# ---------------------------------------------------------------------------
# Catalog cursors (owner-ordered transcript publishing)
# ---------------------------------------------------------------------------

def catalog_cursor(cursor, session_id: str) -> int:
    cursor.execute("SELECT published_seq FROM network_catalog_cursors WHERE session_id = %s", (session_id,))
    row = cursor.fetchone()
    return int(row["published_seq"]) if row else 0


def set_catalog_cursor(cursor, session_id: str, published_seq: int) -> None:
    cursor.execute(
        """INSERT INTO network_catalog_cursors (session_id, published_seq, updated_at) VALUES (%s, %s, %s)
           ON CONFLICT (session_id) DO UPDATE SET published_seq = EXCLUDED.published_seq, updated_at = EXCLUDED.updated_at""",
        (session_id, published_seq, _now()))
