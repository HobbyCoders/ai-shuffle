"""Installation-local projection durability and bounded indexes.

Opaque Node checkpoints stay TEXT: JSONB must not reinterpret JS string/number
representations. No source event, replicated collection or message rule is
written here. Generation/revision CAS makes cancelled or stale folds harmless.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from typing import Any, Sequence

from app.db.base import get_db

MAX_REFERENCED_ROWS = 128
MAX_REFERENCE_BYTES = 12 * 1024 * 1024
MAX_PAGE_ROWS = 96
MAX_PAGE_BYTES = 4 * 1024 * 1024


class ProjectionStoreUnavailable(RuntimeError):
    pass


@dataclass
class ProjectionState:
    session_id: str
    generation: str
    pipeline: str
    epoch: str
    target_seq: int
    target_id: str | None
    revision: int = 0
    index_seq: int = 0
    index_id: str | None = None
    scan_seq: int = 0
    scan_id: str | None = None
    base_seq: int = 0
    base_json: str | None = None
    preview_json: str | None = None
    preprocess_json: str = ""
    stage: str = "index"
    ready: int = 0
    error_code: str | None = None
    cleanup_json: str = "[]"
    cleanup_seq: int = 0


_FIELDS = tuple(ProjectionState.__dataclass_fields__)
_RECORD_FIELDS = ('id', 'order_key', 'origin_seq', 'folded_through', 'body', 'body_sha', 'body_bytes')


def initialize() -> None:
    """Only new local tables; no raw-log migration or write-path hook."""
    definitions = (
        """CREATE TABLE IF NOT EXISTS session_transcript_projection_state (
            session_id TEXT PRIMARY KEY, generation TEXT NOT NULL, pipeline TEXT NOT NULL,
            epoch TEXT NOT NULL, target_seq BIGINT NOT NULL, target_id TEXT,
            revision BIGINT NOT NULL DEFAULT 0, index_seq BIGINT NOT NULL DEFAULT 0, index_id TEXT,
            scan_seq BIGINT NOT NULL DEFAULT 0, scan_id TEXT, base_seq BIGINT NOT NULL DEFAULT 0,
            base_json TEXT, preview_json TEXT, preprocess_json TEXT NOT NULL DEFAULT '',
            stage TEXT NOT NULL DEFAULT 'index', ready INTEGER NOT NULL DEFAULT 0, error_code TEXT,
            cleanup_json TEXT NOT NULL DEFAULT '[]', cleanup_seq BIGINT NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS session_transcript_projection_tools (
            session_id TEXT NOT NULL, generation TEXT NOT NULL, lookup_key TEXT NOT NULL,
            seq BIGINT NOT NULL, PRIMARY KEY (session_id, generation, lookup_key)
        )""",
        """CREATE TABLE IF NOT EXISTS session_transcript_projection_inputs (
            session_id TEXT NOT NULL, generation TEXT NOT NULL, lookup_key TEXT NOT NULL,
            first_seq BIGINT NOT NULL, PRIMARY KEY (session_id, generation, lookup_key)
        )""",
        """CREATE TABLE IF NOT EXISTS session_transcript_projection_records (
            session_id TEXT NOT NULL, generation TEXT NOT NULL, id TEXT NOT NULL,
            order_key BIGINT NOT NULL, origin_seq BIGINT NOT NULL, folded_through BIGINT NOT NULL,
            body TEXT NOT NULL, body_sha TEXT NOT NULL, body_bytes BIGINT NOT NULL,
            PRIMARY KEY (session_id, generation, id), UNIQUE (session_id, generation, order_key)
        )""",
        """CREATE TABLE IF NOT EXISTS session_transcript_projection_aliases (
            session_id TEXT NOT NULL, generation TEXT NOT NULL, alias TEXT NOT NULL, record_id TEXT NOT NULL,
            PRIMARY KEY (session_id, generation, alias, record_id),
            FOREIGN KEY (session_id, generation, record_id) REFERENCES
                session_transcript_projection_records (session_id, generation, id) ON DELETE CASCADE
        )""",
        """CREATE INDEX IF NOT EXISTS idx_session_transcript_projection_origin ON
            session_transcript_projection_records (session_id, generation, origin_seq)""",
        """CREATE INDEX IF NOT EXISTS idx_session_transcript_projection_alias_record ON
            session_transcript_projection_aliases (session_id, generation, record_id)""",
        """CREATE TABLE IF NOT EXISTS session_transcript_projection_retired (
            session_id TEXT NOT NULL, generation TEXT NOT NULL,
            PRIMARY KEY (session_id, generation)
        )""",
    )
    with get_db() as connection:
        cursor = connection.cursor()
        for statement in definitions:
            cursor.execute(statement)


def load(session_id: str) -> ProjectionState | None:
    with get_db() as connection:
        cursor = connection.cursor()
        cursor.execute("SELECT * FROM session_transcript_projection_state WHERE session_id = %s", (session_id,))
        row = cursor.fetchone()
        return ProjectionState(**dict(row)) if row else None


def _cas(cursor, state: ProjectionState, previous: ProjectionState | None) -> None:
    values = asdict(state)
    values['revision'] = previous.revision + 1 if previous else 0
    if previous is None:
        placeholders = ','.join('%s' for _ in _FIELDS)
        cursor.execute(
            f"INSERT INTO session_transcript_projection_state ({','.join(_FIELDS)}) VALUES ({placeholders}) "
            "ON CONFLICT (session_id) DO NOTHING", tuple(values[name] for name in _FIELDS),
        )
    else:
        fields = tuple(name for name in _FIELDS if name != 'session_id')
        cursor.execute(
            "UPDATE session_transcript_projection_state SET " + ','.join(f'{name} = %s' for name in fields) +
            " WHERE session_id = %s AND generation = %s AND revision = %s",
            tuple(values[name] for name in fields) + (state.session_id, previous.generation, previous.revision),
        )
    if cursor.rowcount != 1:
        raise ProjectionStoreUnavailable('stale_projection')
    state.revision = values['revision']
    if previous is not None and previous.generation != state.generation:
        cursor.execute(
            "INSERT INTO session_transcript_projection_retired (session_id,generation) VALUES (%s,%s) "
            "ON CONFLICT DO NOTHING", (previous.session_id, previous.generation),
        )


def commit(state: ProjectionState, previous: ProjectionState | None, *,
           tool_uses: Sequence[tuple[str, int]] = (),
           emitted_inputs: Sequence[tuple[str, int]] = (),
           upserts: Sequence[dict[str, Any]] = (), removed: Sequence[str] = ()) -> None:
    """All checkpoint/index changes commit together, after a source witness check."""
    if len(upserts) > 2048 or len(removed) > 2048 or len(tool_uses) > 128:
        raise ProjectionStoreUnavailable('commit_limit')
    prepared = []
    total_bytes = 0
    for row in upserts:
        data = row['body'].encode('utf-8')
        total_bytes += len(data)
        if len(data) > 512 * 1024 or len(row['aliases']) > 1024:
            raise ProjectionStoreUnavailable('record_limit')
        if any(not isinstance(alias, str) or '\x00' in alias or len(alias) > 2048 for alias in row['aliases']):
            raise ProjectionStoreUnavailable('alias_limit')
        prepared.append((row, len(data), hashlib.sha256(data).hexdigest()))
    if total_bytes > MAX_REFERENCE_BYTES:
        raise ProjectionStoreUnavailable('commit_limit')
    with get_db() as connection:
        cursor = connection.cursor()
        _cas(cursor, state, previous)
        scope = (state.session_id, state.generation)
        for key, seq in tool_uses:
            cursor.execute(
                "INSERT INTO session_transcript_projection_tools (session_id,generation,lookup_key,seq) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (session_id,generation,lookup_key) "
                "DO UPDATE SET seq = excluded.seq", (*scope, key, seq),
            )
        for key, seq in emitted_inputs:
            cursor.execute(
                "INSERT INTO session_transcript_projection_inputs (session_id,generation,lookup_key,first_seq) "
                "VALUES (%s,%s,%s,%s) ON CONFLICT (session_id,generation,lookup_key) DO NOTHING",
                (*scope, key, seq),
            )
        for record_id in set(removed) | {row['id'] for row, _, _ in prepared}:
            cursor.execute(
                "DELETE FROM session_transcript_projection_aliases WHERE session_id=%s AND generation=%s AND record_id=%s",
                (*scope, record_id),
            )
        for record_id in removed:
            cursor.execute(
                "DELETE FROM session_transcript_projection_records WHERE session_id=%s AND generation=%s AND id=%s",
                (*scope, record_id),
            )
        for row, size, digest in prepared:
            cursor.execute(
                "INSERT INTO session_transcript_projection_records "
                "(session_id,generation,id,order_key,origin_seq,folded_through,body,body_sha,body_bytes) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (session_id,generation,id) DO UPDATE SET "
                "order_key=excluded.order_key,origin_seq=excluded.origin_seq,folded_through=excluded.folded_through,"
                "body=excluded.body,body_sha=excluded.body_sha,body_bytes=excluded.body_bytes",
                (*scope, row['id'], row['order'], row['origin'], row['foldedThrough'], row['body'], digest, size),
            )
            for alias in set(row['aliases']):
                cursor.execute(
                    "INSERT INTO session_transcript_projection_aliases (session_id,generation,alias,record_id) "
                    "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING", (*scope, alias, row['id']),
                )


def lookups(state: ProjectionState, keys: list[str]) -> dict[str, int]:
    if not keys:
        return {}
    if len(keys) > 1024:
        raise ProjectionStoreUnavailable('lookup_limit')
    with get_db() as connection:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT lookup_key,seq FROM session_transcript_projection_tools WHERE session_id=%s AND generation=%s "
            f"AND lookup_key IN ({','.join('%s' for _ in keys)})", (state.session_id, state.generation, *keys),
        )
        return {row['lookup_key']: int(row['seq']) for row in cursor.fetchall()}


def changes_committed_input(state: ProjectionState, tools: list[tuple[str, int]]) -> bool:
    if not tools:
        return False
    with get_db() as connection:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT lookup_key,first_seq FROM session_transcript_projection_inputs WHERE session_id=%s AND generation=%s "
            f"AND lookup_key IN ({','.join('%s' for _ in tools)})",
            (state.session_id, state.generation, *(key for key, _ in tools)),
        )
        committed = {row['lookup_key']: int(row['first_seq']) for row in cursor.fetchall()}
        return any(key in committed and committed[key] < seq for key, seq in tools)


def _records(cursor, state: ProjectionState, identifiers: list[str]) -> list[dict[str, Any]]:
    if not identifiers:
        return []
    cursor.execute(
        "SELECT " + ','.join(_RECORD_FIELDS) + " FROM session_transcript_projection_records "
        f"WHERE session_id=%s AND generation=%s AND id IN ({','.join('%s' for _ in identifiers)})",
        (state.session_id, state.generation, *identifiers),
    )
    result = []
    for row in cursor.fetchall():
        if hashlib.sha256(row['body'].encode('utf-8')).hexdigest() != row['body_sha']:
            raise ProjectionStoreUnavailable('record_changed')
        result.append({'id': row['id'], 'order': int(row['order_key']), 'origin': int(row['origin_seq']),
                       'foldedThrough': int(row['folded_through']), 'body': row['body'], 'aliases': []})
    return result


def referenced_records(state: ProjectionState, aliases: list[str]) -> list[dict[str, Any]]:
    """Two queries maximum, irrespective of transcript size or alias overlap."""
    if not aliases:
        return []
    if len(aliases) > 512:
        raise ProjectionStoreUnavailable('reference_limit')
    with get_db() as connection:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT id,body_bytes FROM session_transcript_projection_records WHERE session_id=%s AND generation=%s "
            "AND id IN (SELECT record_id FROM session_transcript_projection_aliases WHERE session_id=%s AND generation=%s "
            f"AND alias IN ({','.join('%s' for _ in aliases)})) LIMIT %s",
            (state.session_id, state.generation, state.session_id, state.generation, *aliases, MAX_REFERENCED_ROWS + 1),
        )
        candidates = cursor.fetchall()
        if len(candidates) > MAX_REFERENCED_ROWS or sum(row['body_bytes'] for row in candidates) > MAX_REFERENCE_BYTES:
            raise ProjectionStoreUnavailable('reference_limit')
        return _records(cursor, state, [row['id'] for row in candidates])


def page(session_id: str, generation: str, revision: int, *, before: int,
         limit: int = MAX_PAGE_ROWS) -> dict[str, Any]:
    """Private read adapter only; no HTTP route/capability enables view reads.

    Four queries maximum: header, bounded keys/sizes, opaque bodies, revision
    witness. A source watermark check belongs to the future serving adapter.
    Never report an empty page when a concurrent writer changed its snapshot.
    """
    if limit < 1 or limit > MAX_PAGE_ROWS:
        raise ProjectionStoreUnavailable('page_limit')
    with get_db() as connection:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT generation,revision,ready,preview_json,epoch,target_seq,target_id FROM "
            "session_transcript_projection_state WHERE session_id=%s", (session_id,),
        )
        header = cursor.fetchone()
        if not header or header['generation'] != generation or header['revision'] != revision or not header['ready']:
            raise ProjectionStoreUnavailable('stale_projection')
        cursor.execute(
            "SELECT id,order_key,body_bytes FROM session_transcript_projection_records "
            "WHERE session_id=%s AND generation=%s AND order_key<%s ORDER BY order_key DESC LIMIT %s",
            (session_id, generation, before, limit + 1),
        )
        candidates = cursor.fetchall()
        selected, used = [], 0
        for row in candidates[:limit]:
            if used + row['body_bytes'] > MAX_PAGE_BYTES:
                break
            selected.append(row)
            used += row['body_bytes']
        # Only scope is used by _records; do not load raw/pending history.
        scope = ProjectionState(session_id, generation, '', header['epoch'], header['target_seq'], header['target_id'])
        records = _records(cursor, scope, [row['id'] for row in selected])
        cursor.execute(
            "SELECT generation,revision,ready FROM session_transcript_projection_state WHERE session_id=%s", (session_id,),
        )
        after = cursor.fetchone()
        if not after or after['generation'] != generation or after['revision'] != revision or not after['ready']:
            raise ProjectionStoreUnavailable('stale_projection')
        records.sort(key=lambda row: row['order'])
        return {'header': dict(header), 'records': records, 'hasOlder': len(candidates) > len(selected),
                'before': min((row['order'] for row in records), default=before)}
