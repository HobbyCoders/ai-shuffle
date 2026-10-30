"""Bounded source reads for the existing REST event fold (not the dark TS view).

Only summarize *boundaries*, never message payloads. PostgreSQL counts the cold
prefix's bubbles/assistant turns; Python runs the SAME fold over a complete
suffix. Rewinds and approval retry cards need nonlocal reducer state and fail
closed to full replay. No derived state, source-rewrite assumptions or rollout
of the default-off canonical transcript producer is involved.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.db.base import get_db
from app.db.session_events import _parse_payload

MAX_WINDOW_SOURCE_ROWS = 8192
# Match str.strip(), including the non-ASCII whitespace Python recognizes.
_WHITESPACE = "".join(chr(i) for i in range(0x3001) if chr(i).isspace())
_FOLD_TYPES = (
    "turn.started", "turn.completed", "turn.aborted", "turn.parked",
    "message.user", "message.assistant", "assistant.text_delta", "assistant.thinking_delta",
    "tool.use", "tool.input_delta", "tool.result", "system.note", "system.compaction_boundary",
    "interaction.requested", "interaction.answered", "interaction.expired", "transcript.rewound",
)


@dataclass
class EventMessageWindow:
    rows: list[dict]
    epoch: str | None
    total: int
    start: int
    end: int
    offset: int
    source_start: int
    turn_offset: int
    total_turns: int
    ids: list[str]


def read_event_message_window(session_id: str, limit: int, before: int | None) -> EventMessageWindow | None:
    """One statement/snapshot: scalar prefix aggregates + a capped complete suffix.

    A message's ordinal is NOT its last event seq. LAG runs only over events
    that affect the REST fold's open-message boundary. Existing tool updates
    don't create cards; a new input-only tool does. The last preceding user
    and its turn.started barrier complete both streamed messages and cost turns.
    Later results/edits are read through the captured tail, even on older pages.

    Returning None means use the unmodified full path. In particular we don't
    guess through rewinds, approval retries, stripped reminder matching, an
    oversized suffix, or a tool re-opened across the chosen boundary.
    """
    with get_db() as connection:
        cursor = connection.cursor()
        cursor.execute("""
            WITH e AS MATERIALIZED (
                SELECT id, seq, event_type AS kind,
                    CASE WHEN event_type IN ('tool.use','tool.input_delta')
                         THEN NULLIF(payload->>'tool_use_id','') END AS tool,
                    CASE WHEN event_type = 'assistant.thinking_delta'
                         THEN NULLIF(payload->>'subtype','') END AS subtype,
                    CASE WHEN event_type = 'message.assistant'
                         THEN COALESCE(payload->>'kind','') <> 'thinking'
                              AND LENGTH(BTRIM(COALESCE(payload->>'content',''), %s)) > 0
                         WHEN event_type = 'assistant.text_delta'
                         THEN LENGTH(BTRIM(COALESCE(payload->>'text',''), %s)) > 0
                         ELSE FALSE END AS has_text,
                    -- Prefix rows are not folded in Python. Decline every shape
                    -- that can fail its .get/concat/regex/hash/iteration operations.
                    -- A JSON string can also decode to an object in _parse_payload,
                    -- which is not the SQL extraction's interpretation.
                    jsonb_typeof(payload) IS DISTINCT FROM 'object'
                        OR event_type = 'transcript.rewound'
                        OR (event_type = 'interaction.requested'
                            AND payload->>'kind' = 'permission'
                            AND jsonb_typeof(payload->'request'->'assistant_confirm') = 'object')
                        OR (event_type = 'message.user'
                            AND POSITION('<system•reminder>' IN COALESCE(payload->>'content','')) > 0)
                        OR (event_type IN ('message.user','message.assistant')
                            AND jsonb_typeof(payload->'content') NOT IN ('string','null'))
                        OR (event_type IN ('assistant.text_delta','assistant.thinking_delta')
                            AND jsonb_typeof(payload->'text') NOT IN ('string','null'))
                        OR (event_type IN ('tool.use','tool.input_delta','tool.result')
                            AND jsonb_typeof(payload->'tool_use_id') NOT IN ('string','null'))
                        OR (event_type IN ('system.note','system.compaction_boundary')
                            AND jsonb_typeof(payload->'data') NOT IN ('object','null'))
                        OR (event_type = 'tool.use'
                            AND jsonb_typeof(payload->'screenshot') NOT IN ('string','null'))
                        OR (event_type = 'tool.input_delta'
                            AND jsonb_typeof(payload->'partial_json') NOT IN ('string','null'))
                        OR (event_type = 'tool.result'
                            AND jsonb_typeof(payload->'artifact_hashes') NOT IN ('array','null'))
                        OR (event_type = 'assistant.thinking_delta'
                            AND jsonb_typeof(payload->'subtype') NOT IN ('string','null'))
                        AS unsafe
                FROM session_events WHERE session_id = %s AND event_type = ANY(%s)
                    AND (event_type <> 'system.note'
                         OR COALESCE(payload->>'subtype','') NOT IN ('background_tasks_changed','model_info'))
            ), tools AS (
                SELECT tool, MIN(seq) AS first_seq FROM e WHERE tool IS NOT NULL GROUP BY tool
            ), typed AS MATERIALIZED (
                SELECT e.*, tools.first_seq FROM e LEFT JOIN tools USING (tool)
            ), markers AS (
                SELECT * FROM typed WHERE kind IN ('turn.started','turn.completed','turn.aborted','turn.parked',
                    'message.user','message.assistant','assistant.text_delta','assistant.thinking_delta',
                    'system.note','system.compaction_boundary')
                    OR (kind = 'tool.use' AND tool IS NOT NULL)
                    OR (kind = 'tool.input_delta' AND seq = first_seq)
            ), ordered AS (
                SELECT *, LAG(kind) OVER (ORDER BY seq) AS previous_kind,
                    LAG(subtype) OVER (ORDER BY seq) AS previous_subtype,
                    SUM(CASE WHEN kind = 'message.user' THEN 1 ELSE 0 END)
                        OVER (ORDER BY seq) AS user_group
                FROM markers
            ), bubbles AS MATERIALIZED (
                SELECT seq, CASE WHEN tool IS NOT NULL THEN 'tooluse-' || tool
                    WHEN kind = 'assistant.text_delta' THEN 'text-' || id
                    WHEN kind = 'assistant.thinking_delta' THEN 'thinking-' || id
                    ELSE id END AS message_id
                FROM ordered WHERE kind IN ('message.user','message.assistant',
                        'system.note','system.compaction_boundary')
                    OR (tool IS NOT NULL AND seq = first_seq)
                    OR (kind IN ('assistant.text_delta','assistant.thinking_delta')
                        AND (kind IS DISTINCT FROM previous_kind
                             OR subtype IS DISTINCT FROM previous_subtype))
            ), finals AS MATERIALIZED (
                SELECT MAX(seq) AS seq FROM ordered WHERE has_text GROUP BY user_group
            ), totals AS (
                SELECT COUNT(*) AS total FROM bubbles
            ), page AS (
                SELECT total, LEAST(total, GREATEST(0, COALESCE(%s, total))) AS page_end FROM totals
            ), bounds AS (
                SELECT *, GREATEST(0, page_end - %s) AS page_start FROM page
            ), oldest AS MATERIALIZED (
                SELECT bounds.*, (SELECT seq FROM bubbles ORDER BY seq
                    OFFSET bounds.page_start LIMIT 1) AS seq FROM bounds
            ), anchor AS MATERIALIZED (
                SELECT oldest.*, COALESCE((SELECT MAX(seq) FROM e WHERE kind = 'turn.started'
                    AND seq <= COALESCE((SELECT MAX(seq) FROM e WHERE kind = 'message.user'
                                         AND seq <= oldest.seq), oldest.seq)), 0) AS source_start
                FROM oldest
            ), summary AS MATERIALIZED (
                SELECT anchor.*,
                    (SELECT COUNT(*) FROM bubbles WHERE seq < source_start) AS message_offset,
                    (SELECT COUNT(*) FROM finals WHERE seq < source_start) AS turn_offset,
                    (SELECT COUNT(*) FROM finals) AS total_turns,
                    ARRAY(SELECT message_id FROM bubbles WHERE seq >= source_start
                          ORDER BY seq LIMIT %s) AS ids,
                    (SELECT id FROM session_events WHERE session_id = %s ORDER BY seq LIMIT 1) AS epoch,
                    COALESCE((SELECT BOOL_OR(unsafe) FROM e), FALSE)
                        OR EXISTS (SELECT 1 FROM typed
                                   WHERE seq >= source_start AND first_seq < source_start) AS unsafe
                FROM anchor
            )
            SELECT summary.total, page_start, page_end, message_offset, source_start,
                   turn_offset, total_turns,
                   CASE WHEN ROW_NUMBER() OVER (ORDER BY source.seq) = 1 THEN ids END AS ids,
                   epoch, unsafe, source.*
            FROM summary LEFT JOIN LATERAL (
                SELECT id, session_id, seq, run_id, turn_id, event_type, payload, command_id, created_at
                FROM session_events WHERE session_id = %s AND seq >= source_start
                    AND event_type = ANY(%s) AND NOT summary.unsafe
                ORDER BY seq LIMIT %s
            ) AS source ON TRUE ORDER BY source.seq
        """, (_WHITESPACE, _WHITESPACE, session_id, list(_FOLD_TYPES), before, limit,
              MAX_WINDOW_SOURCE_ROWS + 1,
              session_id, session_id, list(_FOLD_TYPES), MAX_WINDOW_SOURCE_ROWS + 1))
        rows = [dict(row) for row in cursor.fetchall()]
    summary = rows[0]
    if summary['unsafe'] or len(rows) > MAX_WINDOW_SOURCE_ROWS:
        return None
    source = []
    for row in rows:
        if row['seq'] is not None:
            source.append({key: row[key] for key in (
                'id', 'session_id', 'seq', 'run_id', 'turn_id', 'event_type',
                'payload', 'command_id', 'created_at')})
            source[-1]['payload'] = _parse_payload(source[-1]['payload'])
    return EventMessageWindow(source, summary['epoch'], summary['total'], summary['page_start'],
                              summary['page_end'], summary['message_offset'], summary['source_start'],
                              summary['turn_offset'], summary['total_turns'], summary['ids'] or [])


def read_window_usage(session_id: str, offset: int, count: int) -> list[dict]:
    """Only transfer the relevant usage rows; running cost stays a SQL aggregate."""
    with get_db() as connection:
        cursor = connection.cursor()
        cursor.execute("""
            SELECT COALESCE(cost_usd, 0) AS cost_usd, duration_ms, tokens_out, usage_estimated,
                   SUM(COALESCE(cost_usd, 0)) OVER (ORDER BY id ROWS UNBOUNDED PRECEDING)
                       AS cumulative_cost_usd
            FROM usage_log WHERE session_id = %s ORDER BY id OFFSET %s LIMIT %s
        """, (session_id, offset, count))
        return [dict(row) for row in cursor.fetchall()]


def read_window_user_metadata(session_id: str, source_start: int, contents: dict[str, int]) -> dict:
    """Same per-content occurrence pairing as the full path, without prefix rows.

    Reminder-bearing user events decline the bounded path, so raw content here
    is exactly _wire's rendered key. None metadata entries still consume a slot.
    """
    if not contents:
        return {}
    from collections import defaultdict
    import json

    keys = list(contents)
    with get_db() as connection:
        cursor = connection.cursor()
        cursor.execute("""
            SELECT COALESCE(payload->>'content','') AS content, COUNT(*) AS skipped FROM session_events
            WHERE session_id = %s AND seq < %s AND event_type = 'message.user'
                AND COALESCE(payload->>'content','') = ANY(%s) GROUP BY COALESCE(payload->>'content','')
        """, (session_id, source_start, keys))
        skipped = {row['content']: row['skipped'] for row in cursor.fetchall()}
        cursor.execute("""
            SELECT m.content, m.metadata FROM (
                SELECT content, metadata, ROW_NUMBER() OVER (
                    PARTITION BY content ORDER BY created_at ASC, id ASC) AS occurrence
                FROM session_messages WHERE session_id = %s AND role = 'user' AND content = ANY(%s)
            ) AS m JOIN UNNEST(%s::text[], %s::bigint[], %s::bigint[]) AS wanted(content, skipped, take)
                ON m.content = wanted.content
            WHERE occurrence > skipped AND occurrence <= skipped + take
            ORDER BY m.content, occurrence
        """, (session_id, keys, keys, [skipped.get(key, 0) for key in keys],
              [contents[key] for key in keys]))
        result = defaultdict(list)
        for row in cursor.fetchall():
            metadata = row['metadata']
            if isinstance(metadata, str):
                try:
                    metadata = json.loads(metadata)
                except (ValueError, TypeError):
                    metadata = None
            result[row['content']].append(metadata if isinstance(metadata, dict) else None)
        return dict(result)

