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

from app.db.base import get_db, row_to_dict, rows_to_list, utc_now_iso as _now

logger = logging.getLogger(__name__)

CONFLICT_RETENTION_DAYS = 30
LANE_SETTINGS = "settings"
LANE_HISTORY = "history"


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
    # Owner-produced display state only: never runner leases or executable work.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS network_session_runtime (
            session_id TEXT PRIMARY KEY,
            owner_device_id TEXT NOT NULL,
            state TEXT NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
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
            last_incarnation_accepted INTEGER,
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
        CREATE TABLE IF NOT EXISTS network_session_active (
            session_id TEXT PRIMARY KEY,
            is_active BOOLEAN NOT NULL,
            local_pending BOOLEAN NOT NULL DEFAULT FALSE,
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
        CREATE TABLE IF NOT EXISTS network_member_names (
            device_id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS network_presence (
            device_id TEXT PRIMARY KEY,
            endpoints JSONB NOT NULL DEFAULT '[]',
            capabilities JSONB NOT NULL DEFAULT '{}',
            version TEXT,
            source_revision TEXT,
            platform TEXT,
            incarnation INTEGER NOT NULL DEFAULT 1,
            instance_id TEXT,
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
    _ensure_columns(cursor)


def _ensure_columns(cursor) -> None:
    """Additive columns for databases created before they existed (idempotent).

    PostgreSQL does this natively and in whatever schema ``search_path``
    names; the SQLite shim has no ``IF NOT EXISTS`` for columns, so it asks
    the catalog first.
    """
    additive = (("network_members", "last_incarnation_accepted", "INTEGER"),
                ("network_presence", "instance_id", "TEXT"),
                ("network_presence", "source_revision", "TEXT"))
    if type(cursor).__module__.startswith("psycopg2"):
        for table, column, kind in additive:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {column} {kind}")
        return
    from app.db.base import column_exists
    for table, column, kind in additive:
        if not column_exists(cursor, table, column):
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")


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
    # One round trip: this runs once per published op.
    cursor.execute("UPDATE network_state SET clock = CASE WHEN clock > %s THEN clock ELSE %s END + 1 WHERE id = 1 "
                   "RETURNING clock", (observed, observed))
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
    shared_name = member.pop("shared_name", None)
    if shared_name is not None:
        member["name"] = shared_name
    member["capabilities"] = _load(member.get("capabilities"), {}) or {}
    member["endpoints"] = _load(member.get("endpoints"), []) or []
    return member


def upsert_member(*, device_id: str, name: str, kind: str, signing_public: str, encryption_public: str,
                  generation: int, status: str = "active", platform: Optional[str] = None,
                  added_by: Optional[str] = None, legacy_instance_id: Optional[str] = None,
                  last_seq_accepted: Optional[int] = None, last_incarnation_accepted: Optional[int] = None,
                  _conn=None) -> dict:
    sql = """INSERT INTO network_members (device_id, name, kind, platform, signing_public, encryption_public,
                                          generation, status, last_seq_accepted, last_incarnation_accepted,
                                          legacy_instance_id, added_by, created_at, updated_at)
             VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
             ON CONFLICT (device_id) DO UPDATE SET
                 name = EXCLUDED.name, kind = EXCLUDED.kind,
                 platform = COALESCE(EXCLUDED.platform, network_members.platform),
                 signing_public = EXCLUDED.signing_public, encryption_public = EXCLUDED.encryption_public,
                 generation = EXCLUDED.generation, status = EXCLUDED.status,
                 last_seq_accepted = EXCLUDED.last_seq_accepted,
                 last_incarnation_accepted = EXCLUDED.last_incarnation_accepted,
                 legacy_instance_id = COALESCE(EXCLUDED.legacy_instance_id, network_members.legacy_instance_id),
                 added_by = COALESCE(EXCLUDED.added_by, network_members.added_by),
                 updated_at = EXCLUDED.updated_at"""
    now = _now()
    params = (device_id, name, kind, platform, signing_public, encryption_public, generation, status,
              last_seq_accepted, last_incarnation_accepted, legacy_instance_id, added_by, now, now)
    if _conn is not None:
        _conn.cursor().execute(sql, params)
        return get_member(device_id, _conn=_conn)
    with get_db() as conn:
        conn.cursor().execute(sql, params)
    return get_member(device_id)


def get_member(device_id: str, _conn=None) -> Optional[dict]:
    sql = ("SELECT m.*, n.name AS shared_name FROM network_members m "
           "LEFT JOIN network_member_names n ON n.device_id = m.device_id WHERE m.device_id = %s")
    if _conn is not None:
        cursor = _conn.cursor()
        cursor.execute(sql, (device_id,))
        return _member_row(cursor.fetchone())
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(sql, (device_id,))
        return _member_row(cursor.fetchone())


def list_members(_conn=None, *, include_revoked: bool = True) -> List[dict]:
    sql = ("SELECT m.*, n.name AS shared_name FROM network_members m "
           "LEFT JOIN network_member_names n ON n.device_id = m.device_id"
           + ("" if include_revoked else " WHERE m.status = 'active'")
           + " ORDER BY m.created_at, m.device_id")
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
    """Persist a display-name edit and its signed sync operation together."""
    from app.network import engine

    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE network_members SET name = %s, updated_at = %s WHERE device_id = %s",
                       (name, _now(), device_id))
        if not cursor.rowcount:
            return None
        row = {"device_id": device_id, "name": name, "updated_at": _now()}
        cursor.execute("""INSERT INTO network_member_names (device_id, name, updated_at) VALUES (%s, %s, %s)
                          ON CONFLICT (device_id) DO UPDATE SET name = EXCLUDED.name, updated_at = EXCLUDED.updated_at""",
                       (device_id, name, row["updated_at"]))
        engine.record(cursor, "member_names", device_id, row)
    engine.kick()
    return get_member(device_id)


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
        # Catch-up arrival times (or tied host timestamps) are not trust-chain
        # order. Certificates must precede the generations they authorize.
        out.sort(key=lambda item: int(item["record"].get("generation", 0)))
        return out
    if _conn is not None:
        return _rows(_conn.cursor())
    with get_db() as conn:
        return _rows(conn.cursor())


# ---------------------------------------------------------------------------
# Sync ops log
# ---------------------------------------------------------------------------

def next_origin_seq(cursor, device_id: str, incarnation: int) -> int:
    # ORDER BY ... LIMIT 1, not MAX(seq): while a stream grows faster than
    # autovacuum re-analyzes (a first publish of a large backlog), PostgreSQL
    # can plan MAX as an aggregate over the whole stream, one heap fetch per
    # op, for every op it allocates (#1978). This form always reads one entry.
    cursor.execute("SELECT seq AS s FROM sync_ops WHERE origin_device_id = %s AND incarnation = %s "
                   "ORDER BY seq DESC LIMIT 1", (device_id, incarnation))
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


# Most retained logs have many operations per origin/incarnation. Seek between
# streams in the existing unique index, then read each stream's last entry.
# Bound the walk: an unusually wide log falls back to the aggregate instead of
# doing an unbounded number of index probes. No separately maintained head can
# drift from the log after pruning, restore, rollback or direct maintenance.
HEAD_PROBE_LIMIT = 256
_LOG_HEADS_QUERY = """
    WITH RECURSIVE sync_streams(origin_device_id, incarnation, depth) AS (
        SELECT origin_device_id, incarnation, 1
        FROM (SELECT origin_device_id, incarnation FROM sync_ops
              ORDER BY origin_device_id, incarnation LIMIT 1) first_stream
        UNION ALL
        SELECT
            (SELECT op.origin_device_id FROM sync_ops op
             WHERE (op.origin_device_id, op.incarnation) > (previous.origin_device_id, previous.incarnation)
             ORDER BY op.origin_device_id, op.incarnation LIMIT 1),
            (SELECT op.incarnation FROM sync_ops op
             WHERE (op.origin_device_id, op.incarnation) > (previous.origin_device_id, previous.incarnation)
             ORDER BY op.origin_device_id, op.incarnation LIMIT 1),
            previous.depth + 1
        FROM sync_streams previous
        WHERE previous.origin_device_id IS NOT NULL AND previous.depth < %s
    )
    SELECT origin_device_id, incarnation,
           (SELECT MAX(seq) FROM sync_ops op
            WHERE op.origin_device_id = sync_streams.origin_device_id
              AND op.incarnation = sync_streams.incarnation) AS s
    FROM sync_streams WHERE origin_device_id IS NOT NULL
"""


def _log_heads(cursor) -> list[dict]:
    cursor.execute(_LOG_HEADS_QUERY, (HEAD_PROBE_LIMIT,))
    rows = cursor.fetchall()
    if len(rows) >= HEAD_PROBE_LIMIT:
        # The bounded probe may not have reached the final stream. Re-read a
        # complete frontier in one statement, never advertise a partial one.
        cursor.execute("SELECT origin_device_id, incarnation, MAX(seq) AS s FROM sync_ops "
                       "GROUP BY origin_device_id, incarnation")
        rows = cursor.fetchall()
    return rows


def ops_since(watermarks: Dict[str, Dict[str, int]], *, limit: int = 500, lane: Optional[str] = None,
              exclude_origin: Optional[str] = None, _conn=None,
              _heads: Optional[Dict[str, Dict[str, int]]] = None) -> List[dict]:
    """Ops the caller lacks: per origin/incarnation, everything past its watermark.

    ``watermarks`` is ``{origin_device_id: {incarnation: last_seq}}``. Origins
    the caller has never seen come back from seq 1. Results are ordered per
    origin by (incarnation, seq) so the receiver can apply them in order.

    The budget is shared fairly: every lagging stream first gets an equal
    slice, then the leftovers go to streams that still have more. Handing the
    whole budget to the first stream let one stream the receiver cannot apply
    yet (a child whose parent comes from another origin) be resent forever
    while the parent's stream never got a turn. Each stream's slice is still
    a contiguous prefix past the caller's watermark.
    """
    def _fetch(cursor, origin: str, incarnation: int, after: int, head: int, count: int) -> List[dict]:
        # A concurrent append belongs to the next exchange, not beyond
        # the frontier advertised with this batch.
        params: List[Any] = [origin, incarnation, after, head]
        sql = ("SELECT seq, envelope FROM sync_ops WHERE origin_device_id = %s AND incarnation = %s "
               "AND seq > %s AND seq <= %s")
        if lane:
            sql += " AND lane = %s"
            params.append(lane)
        sql += " ORDER BY seq LIMIT %s"
        params.append(count)
        cursor.execute(sql, tuple(params))
        return [dict(r) for r in cursor.fetchall()]

    def _query(cursor):
        if _heads is None:
            streams = sorted((r["origin_device_id"], int(r["incarnation"]), int(r["s"]))
                             for r in _log_heads(cursor))
        else:
            streams = sorted((origin, int(inc), int(head))
                             for origin, incarnations in _heads.items() for inc, head in incarnations.items())
        lagging = []
        for origin, incarnation, head in streams:
            if origin == exclude_origin:
                continue
            last = int((watermarks.get(origin) or {}).get(str(incarnation), (watermarks.get(origin) or {}).get(incarnation, 0)) or 0)
            if last < head:
                lagging.append((origin, incarnation, last, head))
        if not lagging or limit <= 0:
            return []
        share = max(1, limit // len(lagging))
        remaining = limit
        picked: List[List[dict]] = []
        for origin, incarnation, last, head in lagging:
            if remaining <= 0:
                picked.append([])
                continue
            rows = _fetch(cursor, origin, incarnation, last, head, min(remaining, share))
            remaining -= len(rows)
            picked.append(rows)
        for index, (origin, incarnation, last, head) in enumerate(lagging):
            if remaining <= 0:
                break
            rows = picked[index]
            if len(rows) < share:
                continue  # this stream had no more to give
            more = _fetch(cursor, origin, incarnation, int(rows[-1]["seq"]), head, remaining)
            remaining -= len(more)
            rows.extend(more)
        return [_load(r["envelope"], {}) for rows in picked for r in rows]
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
        out: Dict[str, Dict[str, int]] = {}
        for row in _log_heads(cursor):
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

def insert_event_batch(cursor, coll, rows: List[dict], envelopes: List[dict], hashes: List[str]) -> None:
    """The append-only transcript fast path; caller verified ordering and ownership."""
    now = _now()

    def insert(table, columns, values, conflict):
        placeholders = ", ".join(["(" + ", ".join(["%s"] * len(columns)) + ")"] * len(values))
        cursor.execute(f"INSERT INTO {table} ({', '.join(columns)}) VALUES {placeholders} {conflict}",
                       tuple(value for row in values for value in row))

    insert("session_events", coll.fields,
           [[_json(row.get(name)) if name in coll.json_fields else row.get(name) for name in coll.fields]
            for row in rows], "")
    insert("sync_records", ("collection", "record_id", "clock", "origin_device_id", "content_hash",
                            "deleted", "order_seq", "updated_at"),
           [(env["collection"], env["record_id"], env["clock"], env["origin_device_id"], digest,
             False, env.get("order_seq"), now) for env, digest in zip(envelopes, hashes)], "")
    insert("sync_ops", ("origin_device_id", "incarnation", "seq", "collection", "record_id", "op",
                        "clock", "lane", "envelope", "created_at"),
           [(env["origin_device_id"], env["origin_incarnation"], env["origin_seq"], env["collection"],
             env["record_id"], env["op"], env["clock"], LANE_HISTORY, _json(env), now) for env in envelopes],
           "ON CONFLICT (origin_device_id, incarnation, seq) DO NOTHING")


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


def records_for_ids(cursor, collection: str, record_ids: List[str]) -> Dict[str, dict]:
    """The records among ``record_ids`` (primary-key lookups, not a collection walk)."""
    if not record_ids:
        return {}
    placeholders = ", ".join(["%s"] * len(record_ids))
    cursor.execute(f"SELECT * FROM sync_records WHERE collection = %s AND record_id IN ({placeholders})",
                   (collection, *record_ids))
    return {r["record_id"]: row_to_dict(r) for r in cursor.fetchall()}


def records_with_prefixes(cursor, collection: str, prefixes: List[str]) -> Dict[str, dict]:
    """Records whose id starts with one of ``prefixes`` (``session_id|`` for session children)."""
    if not prefixes:
        return {}
    likes = " OR ".join(["record_id LIKE %s ESCAPE '!'"] * len(prefixes))
    patterns = [prefix.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%" for prefix in prefixes]
    cursor.execute(f"SELECT * FROM sync_records WHERE collection = %s AND ({likes})", (collection, *patterns))
    return {r["record_id"]: row_to_dict(r) for r in cursor.fetchall()
            if any(r["record_id"].startswith(prefix) for prefix in prefixes)}


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


def drop_pending_upto(cursor, origin: str, incarnation: int, seq: int) -> None:
    """Forget buffered copies of ops the stream has already moved past."""
    cursor.execute("DELETE FROM sync_pending WHERE origin_device_id = %s AND incarnation = %s AND seq <= %s",
                   (origin, incarnation, seq))


def clear_pending_origin(cursor, origin: str) -> None:
    """Drop every buffered op of an origin (it was revoked; nothing of it will apply)."""
    cursor.execute("DELETE FROM sync_pending WHERE origin_device_id = %s", (origin,))


def count_pending(cursor, origin: str, incarnation: int) -> int:
    cursor.execute("SELECT COUNT(*) AS n FROM sync_pending WHERE origin_device_id = %s AND incarnation = %s",
                   (origin, incarnation))
    row = cursor.fetchone()
    return int(row["n"]) if row else 0


def get_op(cursor, origin: str, incarnation: int, seq: int, *, at_or_after: bool = False) -> Optional[dict]:
    """One stored envelope, optionally the first retained op at or after seq."""
    comparison = ">=" if at_or_after else "="
    cursor.execute("SELECT envelope FROM sync_ops WHERE origin_device_id = %s AND incarnation = %s "
                   f"AND seq {comparison} %s ORDER BY seq LIMIT 1", (origin, incarnation, seq))
    row = cursor.fetchone()
    return _load(row["envelope"], {}) if row else None


def membership_ops_after(cursor, origin: str, incarnation: int, seq: int) -> List[dict]:
    """Membership records this worker applied from ``origin`` past ``(incarnation, seq)``."""
    cursor.execute("""SELECT record_id, incarnation, seq FROM sync_ops
                      WHERE origin_device_id = %s AND collection = 'membership'
                        AND (incarnation > %s OR (incarnation = %s AND seq > %s))
                      ORDER BY incarnation, seq""", (origin, incarnation, incarnation, seq))
    return [dict(r) for r in cursor.fetchall()]


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
        cursor.execute("SELECT * FROM sync_conflicts WHERE expires_at > %s ORDER BY created_at DESC, id DESC LIMIT %s",
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
                _conn=None, _cursor=None) -> None:
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
    if _cursor is not None:
        _apply(_cursor)
        return
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


def exchange_batch(watermarks: dict, *, limit: int = 500, lane: Optional[str] = None,
                   exclude_origin: str | None = None) -> tuple[List[dict], dict, bool]:
    """Read one frontier for a bounded batch and its snapshot-gap check."""
    with get_db() as conn:
        heads = local_watermarks(conn)
        snapshot = requires_snapshot(watermarks, exclude_origin=exclude_origin, _conn=conn, _heads=heads)
        ops = ops_since(watermarks, limit=limit, lane=lane, exclude_origin=exclude_origin,
                        _conn=conn, _heads=heads)
        return ops, heads, snapshot


def requires_snapshot(watermarks: dict, *, exclude_origin: str | None = None, _conn=None,
                      _heads: Optional[Dict[str, Dict[str, int]]] = None) -> bool:
    """A state-copy recipient can forward state even without original log rows."""
    def _query(conn):
        cursor = conn.cursor()
        heads = local_watermarks(conn) if _heads is None else _heads
        for origin, streams in heads.items():
            if origin == exclude_origin:
                continue
            for incarnation, head in streams.items():
                last = int((watermarks.get(origin) or {}).get(str(incarnation), 0))
                if last >= head:
                    continue
                cursor.execute("""SELECT COUNT(*) AS n FROM sync_ops WHERE origin_device_id=%s
                                  AND incarnation=%s AND seq>%s AND seq<=%s""",
                               (origin, int(incarnation), last, head))
                if int(cursor.fetchone()["n"]) != head - last:
                    return True
        return False
    if _conn is not None:
        return _query(_conn)
    with get_db() as conn:
        return _query(conn)
