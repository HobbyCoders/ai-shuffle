"""Compact, trigger-maintained text projection behind agent SessionSearch.

SessionSearch used to run its substring prefilter over the raw JSON of the
transcript tables (``message::text`` and friends, or the optional stored
``recall_text`` copy of the same JSON). On a long-lived worker almost all of
those bytes are things recall never scores — reasoning blobs, tool inputs,
image data, JSON keys — so every trigram candidate meant detoasting and
case-folding tens of kilobytes, and a broad term (``the``) meant reading the
whole transcript store. ``session_recall_text`` keeps ONLY the text recall
scores, lowercased once at write time, cut into overlapping pieces of at most
``CHUNK_CHARS`` characters (one row per piece, ``part`` 0, 1, ...). Pieces stay
inline in the heap, so a trigram recheck reads about a kilobyte instead of
detoasting a whole tool output, and a common term fails far fewer rechecks.
Each source has its own partial trigram and newest-first index.

Consecutive pieces overlap by ``CHUNK_OVERLAP`` characters, so any substring of
up to ``CHUNK_OVERLAP + 1`` characters lies whole inside one piece; searches
match longer terms by that prefix (a superset) and the Python test stays exact.

Maintenance:

- Row triggers on the three source tables keep the projection exact on every
  insert, update and delete, whichever writer (or app version) made it.
- Existing rows are copied by :func:`run_backfill` in small primary-key-ordered
  batches, one short transaction each, resumable from
  ``session_recall_backfill``. Nothing here rewrites or long-locks a source
  table: installing the triggers waits at most ``lock_timeout`` and simply
  retries on a later pass.
- SessionSearch reads the projection only once :func:`ready` reports every
  source backfilled; until then it keeps the legacy queries.

The SQL extractors mirror the Python scorers (``message_texts``,
``_sse_entry_texts``, ``_fold_events``) and may only ever return a SUPERSET of
what those score: the projection is a prefilter and the Python substring test
stays the real match.
"""

from __future__ import annotations

import json
import logging
import time

from app.db.base import get_db, table_exists

logger = logging.getLogger(__name__)

TABLE = "session_recall_text"
NATIVE, ENTRY, EVENT = "n", "s", "e"
SOURCES = (NATIVE, ENTRY, EVENT)
TEXT_EVENT_TYPES = ("message.user", "message.assistant", "assistant.text_delta")
_TEXT_EVENTS_SQL = "('message.user', 'message.assistant', 'assistant.text_delta')"
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 100
_STRIDE = CHUNK_CHARS - CHUNK_OVERLAP
# One source row's text as (part, piece) rows; an empty text is one '' piece.
_PIECES = (f"SELECT k AS part, substr(%s, k * {_STRIDE} + 1, {CHUNK_CHARS}) AS piece "
           f"FROM generate_series(0, GREATEST(0, CEIL((length(%s) - {CHUNK_CHARS})::numeric / {_STRIDE})::int)) k")


def _pieces(expr: str) -> str:
    return _PIECES.replace("%s", expr)


def _event_part0(row: str) -> str:
    """The fold columns, set on an event's first piece only."""
    return ", ".join(
        f"CASE WHEN p.part = 0 THEN {value} END"
        for value in (f"{row}seq", f"{row}event_type", "x.r", f"{row}payload->>'timestamp'")
    )

_TRIGGERS = (
    "trg_session_recall_nodes",
    "trg_session_recall_entries",
    "trg_session_recall_events_ins",
    "trg_session_recall_events_del",
    "trg_session_recall_events_upd",
)

# Rows read per backfill transaction. Native rows carry whole messages (tens of
# KB at the tail); most events are small and not text, so they batch wider.
_BATCH = {NATIVE: 200, ENTRY: 200, EVENT: 2000}

_DDL = [
    f"""
    CREATE TABLE IF NOT EXISTS {TABLE} (
        source TEXT NOT NULL,
        session_id TEXT NOT NULL,
        row_key TEXT NOT NULL,
        part INTEGER NOT NULL,
        ts DOUBLE PRECISION NOT NULL,
        body TEXT NOT NULL,
        -- Part 0 of an event only: what _fold_events reads, so recall folds
        -- a mirrored session without decoding its JSON payloads.
        seq BIGINT,
        event_type TEXT,
        raw TEXT,
        stamp TEXT,
        PRIMARY KEY (source, session_id, row_key, part)
    )
    """,
    *[f"CREATE INDEX IF NOT EXISTS idx_session_recall_text_ts_{s} ON {TABLE} (ts DESC) WHERE source = '{s}'"
      for s in SOURCES],
    *[f"CREATE INDEX IF NOT EXISTS idx_session_recall_text_trgm_{s} ON {TABLE} "
      f"USING gin (body gin_trgm_ops) WHERE source = '{s}'" for s in SOURCES],
    """
    CREATE TABLE IF NOT EXISTS session_recall_backfill (
        source TEXT PRIMARY KEY,
        last_key TEXT,
        done BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    # fluid's ``message_texts``: text blocks plus the text parts of tool results.
    """
    CREATE OR REPLACE FUNCTION recall_node_text(message jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE AS $fn$
        SELECT lower(string_agg(x.t, E'\\n'))
        FROM (SELECT CASE WHEN jsonb_typeof(message->'content') = 'array'
                     THEN message->'content' ELSE '[]'::jsonb END AS c) s0
        CROSS JOIN LATERAL (
            SELECT b->>'text' AS t FROM jsonb_array_elements(s0.c) b
            WHERE b->>'kind' = 'text'
            UNION ALL
            SELECT i->>'text' FROM jsonb_array_elements(s0.c) b
            CROSS JOIN LATERAL jsonb_array_elements(
                CASE WHEN jsonb_typeof(b->'content') = 'array' THEN b->'content' ELSE '[]'::jsonb END) i
            WHERE b->>'kind' = 'toolresultblock' AND i->>'kind' = 'text'
        ) x
        WHERE COALESCE(x.t, '') <> ''
    $fn$
    """,
    # ``_sse_entry_texts``. Only an explicit ``true`` meta/sidechain flag is
    # skipped here; Python drops any other truthy value afterwards.
    """
    CREATE OR REPLACE FUNCTION recall_entry_text(entry jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE AS $fn$
        SELECT CASE
            WHEN COALESCE(entry->>'type', '') NOT IN ('user', 'assistant') THEN NULL
            WHEN entry->'isMeta' = 'true'::jsonb OR entry->'isSidechain' = 'true'::jsonb THEN NULL
            WHEN jsonb_typeof(entry #> '{message,content}') = 'string'
                THEN NULLIF(lower(entry #>> '{message,content}'), '')
            WHEN jsonb_typeof(entry #> '{message,content}') = 'array' THEN (
                SELECT lower(string_agg(b->>'text', E'\\n'))
                FROM jsonb_array_elements(entry #> '{message,content}') b
                WHERE b->>'type' = 'text' AND COALESCE(b->>'text', '') <> '')
        END
    $fn$
    """,
    # ``_fold_events``: every event it folds keeps a row (an empty text too, so
    # the fold sees the same message boundaries); NULL = never folded.
    """
    CREATE OR REPLACE FUNCTION recall_event_raw(event_type text, payload jsonb) RETURNS text
    LANGUAGE sql IMMUTABLE AS $fn$
        SELECT CASE
            WHEN event_type = 'assistant.text_delta' THEN COALESCE(payload->>'text', '')
            WHEN event_type = 'message.user' THEN COALESCE(payload->>'content', '')
            WHEN event_type = 'message.assistant'
                 AND (NOT payload ? 'kind' OR payload->>'kind' = 'text')
                THEN COALESCE(payload->>'content', '')
        END
    $fn$
    """,
    f"""
    CREATE OR REPLACE FUNCTION session_recall_nodes_sync() RETURNS trigger
    LANGUAGE plpgsql AS $fn$
    BEGIN
        IF TG_OP <> 'INSERT' THEN
            DELETE FROM {TABLE} WHERE source = 'n' AND session_id = OLD.session_id AND row_key = OLD.node_id;
        END IF;
        IF TG_OP <> 'DELETE' THEN
            INSERT INTO {TABLE} (source, session_id, row_key, part, ts, body)
            SELECT 'n', NEW.session_id, NEW.node_id, p.part, NEW.ts, p.piece
            FROM (SELECT recall_node_text(NEW.message) AS t) x CROSS JOIN LATERAL ({_pieces("x.t")}) p WHERE x.t IS NOT NULL
            ON CONFLICT (source, session_id, row_key, part) DO UPDATE SET ts = EXCLUDED.ts, body = EXCLUDED.body;
        END IF;
        RETURN NULL;
    END
    $fn$
    """,
    f"""
    CREATE OR REPLACE FUNCTION session_recall_entries_sync() RETURNS trigger
    LANGUAGE plpgsql AS $fn$
    BEGIN
        IF TG_OP <> 'INSERT' THEN
            DELETE FROM {TABLE} WHERE source = 's' AND session_id = OLD.session_id AND row_key = OLD.id::text;
        END IF;
        IF TG_OP <> 'DELETE' AND NEW.subpath IS NULL THEN
            INSERT INTO {TABLE} (source, session_id, row_key, part, ts, body)
            SELECT 's', NEW.session_id, NEW.id::text, p.part, EXTRACT(EPOCH FROM NEW.created_at), p.piece
            FROM (SELECT recall_entry_text(NEW.entry) AS t) x CROSS JOIN LATERAL ({_pieces("x.t")}) p WHERE x.t IS NOT NULL
            ON CONFLICT (source, session_id, row_key, part) DO UPDATE SET ts = EXCLUDED.ts, body = EXCLUDED.body;
        END IF;
        RETURN NULL;
    END
    $fn$
    """,
    f"""
    CREATE OR REPLACE FUNCTION session_recall_events_sync() RETURNS trigger
    LANGUAGE plpgsql AS $fn$
    BEGIN
        IF TG_OP <> 'INSERT' THEN
            DELETE FROM {TABLE} WHERE source = 'e' AND session_id = OLD.session_id AND row_key = OLD.id;
        END IF;
        IF TG_OP <> 'DELETE' THEN
            INSERT INTO {TABLE} (source, session_id, row_key, part, ts, body, seq, event_type, raw, stamp)
            SELECT 'e', NEW.session_id, NEW.id, p.part, COALESCE(EXTRACT(EPOCH FROM NEW.created_at), 0), p.piece,
                   {_event_part0("NEW.")}
            FROM (SELECT recall_event_raw(NEW.event_type, NEW.payload) AS r) x
            CROSS JOIN LATERAL ({_pieces("lower(x.r)")}) p WHERE x.r IS NOT NULL
            ON CONFLICT (source, session_id, row_key, part) DO UPDATE SET ts = EXCLUDED.ts, body = EXCLUDED.body,
                seq = EXCLUDED.seq, event_type = EXCLUDED.event_type, raw = EXCLUDED.raw, stamp = EXCLUDED.stamp;
        END IF;
        RETURN NULL;
    END
    $fn$
    """,
]

_TRIGGER_DDL = [
    (
        "CREATE TRIGGER trg_session_recall_nodes "
        "AFTER INSERT OR DELETE OR UPDATE OF session_id, node_id, ts, message ON fluid_session_nodes "
        "FOR EACH ROW EXECUTE FUNCTION session_recall_nodes_sync()"
    ),
    (
        "CREATE TRIGGER trg_session_recall_entries "
        "AFTER INSERT OR DELETE OR UPDATE OF id, session_id, subpath, entry, created_at ON session_store_entries "
        "FOR EACH ROW EXECUTE FUNCTION session_recall_entries_sync()"
    ),
    # Most events are tool deltas: the WHEN clauses keep them off the trigger.
    (
        "CREATE TRIGGER trg_session_recall_events_ins AFTER INSERT ON session_events "
        f"FOR EACH ROW WHEN (NEW.event_type IN {_TEXT_EVENTS_SQL}) "
        "EXECUTE FUNCTION session_recall_events_sync()"
    ),
    (
        "CREATE TRIGGER trg_session_recall_events_del AFTER DELETE ON session_events "
        f"FOR EACH ROW WHEN (OLD.event_type IN {_TEXT_EVENTS_SQL}) "
        "EXECUTE FUNCTION session_recall_events_sync()"
    ),
    (
        "CREATE TRIGGER trg_session_recall_events_upd AFTER UPDATE ON session_events "
        f"FOR EACH ROW WHEN (OLD.event_type IN {_TEXT_EVENTS_SQL} OR NEW.event_type IN {_TEXT_EVENTS_SQL}) "
        "EXECUTE FUNCTION session_recall_events_sync()"
    ),
]


def _triggers_present(cursor) -> set:
    cursor.execute(
        "SELECT tgname FROM pg_trigger WHERE NOT tgisinternal AND tgname IN ("
        + ", ".join(["%s"] * len(_TRIGGERS)) + ")",
        list(_TRIGGERS),
    )
    return {r["tgname"] for r in cursor.fetchall()}


def install(cursor, *, lock_timeout_ms: int = 2000) -> bool:
    """Create the projection table, its indexes, extractors and triggers.

    Idempotent and savepoint-fenced: without pg_trgm (or on the SQLite test
    shim) recall keeps its legacy queries. The table is created empty, so its
    indexes cost nothing to build; the only lock on a source table is the brief
    one CREATE TRIGGER takes, bounded by ``lock_timeout_ms`` (a busy table just
    means a later pass installs it). Returns True when every trigger exists.
    """
    cursor.execute("SAVEPOINT session_recall_install")
    try:
        cursor.execute("SELECT current_setting('lock_timeout') AS v")
        previous = cursor.fetchone()["v"]
        cursor.execute("SELECT set_config('lock_timeout', %s, true)", (f"{int(lock_timeout_ms)}ms",))
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        for statement in _DDL:
            cursor.execute(statement)
        present = _triggers_present(cursor)
        for name, statement in zip(_TRIGGERS, _TRIGGER_DDL):
            if name not in present:
                cursor.execute(statement)
        cursor.execute("SELECT set_config('lock_timeout', %s, true)", (previous,))
        cursor.execute("RELEASE SAVEPOINT session_recall_install")
    except Exception:  # recall degrades to the legacy queries
        cursor.execute("ROLLBACK TO SAVEPOINT session_recall_install")
        cursor.execute("RELEASE SAVEPOINT session_recall_install")
        logger.info("SessionSearch projection not installed yet; recall keeps the legacy queries",
                    exc_info=True)
        return False
    return True


def installed(cursor) -> bool:
    try:
        return _triggers_present(cursor) == set(_TRIGGERS)
    except Exception:  # noqa: BLE001 — no pg_catalog (test shim)
        return False


def ready(cursor) -> bool:
    """True once every source is backfilled (the triggers keep it current)."""
    if not table_exists(cursor, "session_recall_backfill"):
        return False
    cursor.execute("SELECT COUNT(*) AS n FROM session_recall_backfill WHERE done")
    return int(cursor.fetchone()["n"]) == len(SOURCES)


# Each statement copies one primary-key-ordered batch after ``last_key`` and
# returns that batch's last key, whether or not its rows had searchable text.
_STEP_SQL = {
    NATIVE: f"""
        WITH b AS (
            SELECT session_id, node_id, ts, message FROM fluid_session_nodes
            WHERE (session_id, node_id) > (%s, %s) ORDER BY session_id, node_id LIMIT %s
        ), ins AS (
            INSERT INTO {TABLE} (source, session_id, row_key, part, ts, body)
            SELECT 'n', session_id, node_id, p.part, ts, p.piece
            FROM (SELECT session_id, node_id, ts, recall_node_text(message) AS t FROM b) x
            CROSS JOIN LATERAL ({_pieces("x.t")}) p
            WHERE x.t IS NOT NULL
            ON CONFLICT DO NOTHING
        )
        SELECT session_id AS k1, node_id AS k2 FROM b ORDER BY session_id DESC, node_id DESC LIMIT 1
    """,
    ENTRY: f"""
        WITH b AS (
            SELECT id, session_id, entry, created_at FROM session_store_entries
            WHERE id > %s AND subpath IS NULL ORDER BY id LIMIT %s
        ), ins AS (
            INSERT INTO {TABLE} (source, session_id, row_key, part, ts, body)
            SELECT 's', session_id, id::text, p.part, EXTRACT(EPOCH FROM created_at), p.piece
            FROM (SELECT id, session_id, created_at, recall_entry_text(entry) AS t FROM b) x
            CROSS JOIN LATERAL ({_pieces("x.t")}) p
            WHERE x.t IS NOT NULL
            ON CONFLICT DO NOTHING
        )
        SELECT id AS k1, NULL AS k2 FROM b ORDER BY id DESC LIMIT 1
    """,
    EVENT: f"""
        WITH b AS (
            SELECT id, session_id, seq, event_type, payload, created_at FROM session_events
            WHERE (session_id, seq) > (%s, %s) ORDER BY session_id, seq LIMIT %s
        ), ins AS (
            INSERT INTO {TABLE} (source, session_id, row_key, part, ts, body, seq, event_type, raw, stamp)
            SELECT 'e', x.session_id, x.id, p.part, COALESCE(EXTRACT(EPOCH FROM x.created_at), 0), p.piece,
                   {_event_part0("x.")}
            FROM (SELECT id, session_id, seq, event_type, payload, created_at,
                         recall_event_raw(event_type, payload) AS r
                  FROM b WHERE event_type IN {_TEXT_EVENTS_SQL}) x
            CROSS JOIN LATERAL ({_pieces("lower(x.r)")}) p
            WHERE x.r IS NOT NULL
            ON CONFLICT DO NOTHING
        )
        SELECT session_id AS k1, seq AS k2 FROM b ORDER BY session_id DESC, seq DESC LIMIT 1
    """,
}
_START_KEY = {NATIVE: ["", ""], ENTRY: [0], EVENT: ["", -1]}


def backfill_step(cursor, source: str, batch: int | None = None) -> bool:
    """Copy one batch of ``source``; True once that source is complete.

    Caller owns the (short) transaction: rows and progress commit together, so
    an interrupted pass resumes exactly where it stopped. Rows the triggers
    already wrote win (``ON CONFLICT DO NOTHING``).
    """
    cursor.execute(
        "INSERT INTO session_recall_backfill (source) VALUES (%s) ON CONFLICT DO NOTHING", (source,)
    )
    cursor.execute(
        "SELECT last_key, done FROM session_recall_backfill WHERE source = %s FOR UPDATE", (source,)
    )
    row = cursor.fetchone()
    if row["done"]:
        return True
    key = json.loads(row["last_key"]) if row["last_key"] else _START_KEY[source]
    cursor.execute(_STEP_SQL[source], [*key, int(batch or _BATCH[source])])
    last = cursor.fetchone()
    if last is None:
        cursor.execute("UPDATE session_recall_backfill SET done = TRUE WHERE source = %s", (source,))
        return True
    new_key = [last["k1"]] if source == ENTRY else [last["k1"], last["k2"]]
    cursor.execute(
        "UPDATE session_recall_backfill SET last_key = %s WHERE source = %s",
        (json.dumps(new_key), source),
    )
    return False


def run_backfill(*, max_seconds: float = 20.0, pause_seconds: float = 0.1) -> bool:
    """One bounded pass of installation plus backfill; True when recall is ready.

    Raises when the projection cannot be installed yet, so the caller backs off.

    Every batch is its own transaction, separated by ``pause_seconds`` so
    foreground writers never queue behind it. Called repeatedly by the startup
    task until it returns True.
    """
    deadline = time.monotonic() + max_seconds
    with get_db() as conn:
        cur = conn.cursor()
        if not installed(cur) and not install(cur):
            raise RuntimeError("projection not installed (pg_trgm unavailable or a source table busy)")
        if ready(cur):
            return True
    pending = list(SOURCES)
    while pending and time.monotonic() < deadline:
        source = pending[0]
        with get_db() as conn:
            if backfill_step(conn.cursor(), source):
                pending.pop(0)
                continue
        time.sleep(pause_seconds)
    with get_db() as conn:
        cur = conn.cursor()
        if not ready(cur):
            return False
        # Fresh planner statistics before the first search reads the table.
        cur.execute(f"ANALYZE {TABLE}")
    logger.info("SessionSearch projection backfilled; recall now uses it")
    return True
