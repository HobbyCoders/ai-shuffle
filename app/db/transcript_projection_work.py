"""Bounded projection work queues/index maintenance; never source-log writes."""
from __future__ import annotations

import json
from typing import Any

from app.db.base import get_db, rows_to_list
from app.db import session_events as source
from app.db import transcript_projections as store


SCAN_ROWS = 128
CLEANUP_ROWS = 512
GARBAGE_ROWS = 128
# UTF-8 needs at most four bytes per character. Select a prefix by size in
# the same statement/snapshot that fetches bodies; LIMIT alone is not a byte
# bound for historical tool results. Never skip an oversized first event.
SOURCE_CHARS = 2 * 1024 * 1024


def source_sessions(after: str = "", limit: int = 32) -> list[str]:
    with get_db() as connection:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT c.session_id FROM session_event_counters c JOIN sessions s ON s.id=c.session_id "
            "WHERE c.session_id>%s ORDER BY c.session_id LIMIT %s", (after, min(limit, 32)),
        )
        return [row['session_id'] for row in cursor.fetchall()]


def source_index(session_id: str, after: int, through: int) -> list[dict[str, Any]]:
    # SQL ->> is exactly the serving get_tool_use_seqs coercion. Do not infer
    # JSON scalar identity from Python payloads (booleans are not strings).
    # Narrow projection avoids fetching progress/tool-result TOAST bodies.
    with get_db() as connection:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT id,seq,turn_id,event_type,CASE WHEN event_type='tool.use' "
            "THEN payload->>'tool_use_id' END AS tool_use_id FROM session_events "
            "WHERE session_id=%s AND seq>%s AND seq<=%s ORDER BY seq LIMIT %s",
            (session_id, after, through, SCAN_ROWS),
        )
        return [dict(row) for row in cursor.fetchall()]


def source_rows(session_id: str, after: int, through: int) -> list[dict[str, Any]]:
    """A bounded contiguous source prefix, with the serving DAO's decoding."""
    with get_db() as connection:
        cursor = connection.cursor()
        cursor.execute(
            "WITH bounded AS (SELECT id,seq,COALESCE(length(CAST(payload AS TEXT)),0) AS chars "
            "FROM session_events WHERE session_id=%s AND seq>%s AND seq<=%s ORDER BY seq LIMIT %s), "
            "budget AS (SELECT id,seq,SUM(chars) OVER (ORDER BY seq) AS used,"
            "ROW_NUMBER() OVER (ORDER BY seq) AS ordinal FROM bounded) "
            "SELECT s.id,s.session_id,s.seq,s.run_id,s.turn_id,s.event_type,"
            "CASE WHEN b.used<=%s THEN s.payload ELSE NULL END AS payload,s.command_id,s.created_at,"
            "(b.used<=%s) AS readable FROM budget b JOIN session_events s ON s.id=b.id "
            "WHERE b.used<=%s OR b.ordinal=1 ORDER BY s.seq",
            (session_id, after, through, SCAN_ROWS, SOURCE_CHARS, SOURCE_CHARS, SOURCE_CHARS),
        )
        result = rows_to_list(cursor.fetchall())
    for row in result:
        if not row.pop('readable'):
            raise store.ProjectionStoreUnavailable('source_row_limit')
        row['payload'] = source._parse_payload(row['payload'])
    return result


def cleanup(state: store.ProjectionState, previous: store.ProjectionState) -> bool:
    """Apply Node-authored range invalidations in finite derived-only batches."""
    ranges = json.loads(state.cleanup_json)
    if not ranges or len(ranges) > 256:
        raise store.ProjectionStoreUnavailable('cleanup_state')
    clauses, bounds = [], []
    for item in ranges:
        clauses.append('(origin_seq>=%s AND folded_through<%s)')
        bounds.extend((item['originAtLeast'], item['foldedBefore']))
    scope = (state.session_id, state.generation)
    with get_db() as connection:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT id FROM session_transcript_projection_records WHERE session_id=%s AND generation=%s AND (" +
            ' OR '.join(clauses) + ") ORDER BY order_key LIMIT %s", (*scope, *bounds, CLEANUP_ROWS),
        )
        identifiers = [row['id'] for row in cursor.fetchall()]
        more = len(identifiers) == CLEANUP_ROWS
        if not more:
            state.cleanup_seq = max(item['foldedBefore'] for item in ranges)
            state.cleanup_json = '[]'
            state.stage = 'fold'
        state.ready = 0
        store._cas(cursor, state, previous)
        if identifiers:
            params = (*scope, *identifiers)
            placeholders = ','.join('%s' for _ in identifiers)
            cursor.execute(
                "DELETE FROM session_transcript_projection_aliases WHERE session_id=%s AND generation=%s "
                f"AND record_id IN ({placeholders})", params,
            )
            cursor.execute(
                "DELETE FROM session_transcript_projection_records WHERE session_id=%s AND generation=%s "
                f"AND id IN ({placeholders})", params,
            )
        return more


def retire_missing(previous: store.ProjectionState) -> None:
    """Called only after an actual source-session absence witness."""
    with get_db() as connection:
        cursor = connection.cursor()
        cursor.execute(
            "DELETE FROM session_transcript_projection_state WHERE session_id=%s AND generation=%s AND revision=%s",
            (previous.session_id, previous.generation, previous.revision),
        )
        if cursor.rowcount:
            cursor.execute(
                "INSERT INTO session_transcript_projection_retired (session_id,generation) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING", (previous.session_id, previous.generation),
            )


def collect_garbage() -> bool:
    """Finite cleanup of retired generations, without scanning current history."""
    with get_db() as connection:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT r.session_id,r.generation FROM session_transcript_projection_retired r "
            "LEFT JOIN session_transcript_projection_state s ON s.session_id=r.session_id "
            "WHERE s.generation IS NULL OR s.generation<>r.generation ORDER BY r.session_id,r.generation LIMIT 1"
        )
        retired = cursor.fetchone()
        if not retired:
            return False
        scope = (retired['session_id'], retired['generation'])
        cursor.execute(
            "SELECT id FROM session_transcript_projection_records WHERE session_id=%s AND generation=%s "
            "ORDER BY order_key LIMIT %s", (*scope, GARBAGE_ROWS),
        )
        ids = [row['id'] for row in cursor.fetchall()]
        if ids:
            placeholders = ','.join('%s' for _ in ids)
            cursor.execute(
                "DELETE FROM session_transcript_projection_aliases WHERE session_id=%s AND generation=%s "
                f"AND record_id IN ({placeholders})", (*scope, *ids),
            )
            cursor.execute(
                "DELETE FROM session_transcript_projection_records WHERE session_id=%s AND generation=%s "
                f"AND id IN ({placeholders})", (*scope, *ids),
            )
            return True
        for table in ('session_transcript_projection_tools', 'session_transcript_projection_inputs'):
            cursor.execute(
                f"SELECT lookup_key FROM {table} WHERE session_id=%s AND generation=%s LIMIT %s",
                (*scope, GARBAGE_ROWS),
            )
            keys = [row['lookup_key'] for row in cursor.fetchall()]
            if keys:
                cursor.execute(
                    f"DELETE FROM {table} WHERE session_id=%s AND generation=%s "
                    f"AND lookup_key IN ({','.join('%s' for _ in keys)})", (*scope, *keys),
                )
                return True
        cursor.execute(
            "DELETE FROM session_transcript_projection_retired WHERE session_id=%s AND generation=%s", scope,
        )
        return True
