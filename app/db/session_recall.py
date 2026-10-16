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
Pieces are cut in one linear pass (``recall_pieces``): ``substr`` at an offset
walks a UTF-8 string from its start, which made a megabyte message quadratic.
A row whose text exceeds ``MAX_ROW_CHARS`` gets a single ``part = -1`` marker
instead of pieces, and search reads that row's text from its source table
(the legacy way) — so no single write ever indexes more than that much text.

Maintenance:

- Row triggers on the three source tables keep the projection exact on every
  insert, update and delete, whichever writer (or app version) made it. A
  projection error never fails the user's write: the trigger logs a warning
  and marks its source for a rebuild, and search falls back to the legacy
  queries until :func:`run_backfill` has copied that source again.
- The triggers are installed one table per short transaction, waiting at most
  ``LOCK_TIMEOUT_MS`` for the table, so writers never queue behind them for
  long; a busy table just means a later pass. Creating a trigger restarts that
  source's backfill, so rows written while it was missing are copied too.
- Existing rows are copied by :func:`run_backfill` in primary-key-ordered
  batches bounded by row count and by stored bytes, one short transaction
  each, resumable from ``session_recall_backfill``. Each batch share-locks its
  source rows (``NOWAIT``: a busy row makes the batch retry rather than wait),
  so a concurrent delete or update cannot interleave with the copy.
- SessionSearch reads the projection only while :func:`ready` holds: current
  version, every trigger present, every source backfilled.
- ``VERSION`` names the triggers and tags the table. Changing the projection's
  shape bumps it: the old table is dropped and rebuilt, and the old triggers
  are replaced table by table.

An older app version ignores these objects, but the triggers keep running
(and the table keeps growing with transcripts) until they are removed with
``python -m app.maintenance.session_recall_projection --remove``.

The SQL extractors mirror the Python scorers (``message_texts``,
``_sse_entry_texts``, ``_fold_events``) and may only ever return a SUPERSET of
what those score: the projection is a prefilter and the Python substring test
stays the real match.
"""

from __future__ import annotations

import json
import logging
import time

import psycopg2.errors

from app.db.base import get_db, table_exists

logger = logging.getLogger(__name__)

VERSION = 1
TABLE = "session_recall_text"
NATIVE, ENTRY, EVENT = "n", "s", "e"
SOURCES = (NATIVE, ENTRY, EVENT)
TEXT_EVENT_TYPES = ("message.user", "message.assistant", "assistant.text_delta")
_TEXT_EVENTS_SQL = "('message.user', 'message.assistant', 'assistant.text_delta')"
CHUNK_CHARS = 1000
CHUNK_OVERLAP = 100
_STRIDE = CHUNK_CHARS - CHUNK_OVERLAP
# PostgreSQL caps a regex bound at 255, so pieces are assembled from
# quarter-stride runs.
_RUN = _STRIDE // 4
# Larger rows are matched in their source table instead (see the docstring).
MAX_ROW_CHARS = 200_000
LOCK_TIMEOUT_MS = 250
OVERFLOW_PART = -1

_TABLE_TAG = f"session_recall v{VERSION}"

# source -> (source table, trigger name suffixes)
_SOURCE_TABLES = {
    NATIVE: ("fluid_session_nodes", ("nodes",)),
    ENTRY: ("session_store_entries", ("entries",)),
    EVENT: ("session_events", ("events_ins", "events_del", "events_upd")),
}


def _trigger_name(suffix: str) -> str:
    return f"trg_session_recall_v{VERSION}_{suffix}"


_TRIGGERS = tuple(_trigger_name(s) for _, suffixes in _SOURCE_TABLES.values() for s in suffixes)

# Per backfill transaction: at most this many rows, and at most this many
# stored (possibly compressed) bytes of the column the extractor reads.
_BATCH = {NATIVE: 200, ENTRY: 200, EVENT: 2000}
_BATCH_BYTES = 2_000_000

# The table and its indexes: run only when the table is (re)created, so a
# repair pass never takes a lock on a table the triggers are writing to.
_TABLE_DDL = [
    f"""
    CREATE TABLE {TABLE} (
        source TEXT NOT NULL,
        session_id TEXT NOT NULL,
        row_key TEXT NOT NULL,
        part INTEGER NOT NULL,
        ts DOUBLE PRECISION NOT NULL,
        body TEXT NOT NULL,
        -- An event's first row only (part 0, or its overflow marker): what
        -- _fold_events reads, so recall folds a mirrored session without
        -- decoding its JSON payloads.
        seq BIGINT,
        event_type TEXT,
        raw TEXT,
        stamp TEXT,
        PRIMARY KEY (source, session_id, row_key, part)
    )
    """,
    f"COMMENT ON TABLE {TABLE} IS '{_TABLE_TAG}'",
    *[f"CREATE INDEX idx_session_recall_text_ts_{s} ON {TABLE} (ts DESC) WHERE source = '{s}'"
      for s in SOURCES],
    *[f"CREATE INDEX idx_session_recall_text_trgm_{s} ON {TABLE} "
      f"USING gin (body gin_trgm_ops) WHERE source = '{s}'" for s in SOURCES],
    (f"CREATE INDEX idx_session_recall_text_overflow ON {TABLE} (source, session_id) "
     f"WHERE part = {OVERFLOW_PART}"),
]

# Progress table and functions: idempotent, no lock on any table in use.
_DDL = [
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
    # Piece k is characters [k*STRIDE, k*STRIDE + CHUNK) of ``t``: STRIDE-long
    # runs (each built from four regex matches, in one pass over the string)
    # plus the next run's first OVERLAP characters. A trailing run no longer
    # than the overlap already lies inside the previous piece. '' is one piece.
    f"""
    CREATE OR REPLACE FUNCTION recall_pieces(t text) RETURNS TABLE (part integer, piece text)
    LANGUAGE sql IMMUTABLE AS $fn$
        SELECT z.g, z.piece FROM (
            SELECT q.g, q.c || COALESCE(left(lead(q.c) OVER (ORDER BY q.g), {CHUNK_OVERLAP}), '') AS piece,
                   length(q.c) AS n
            FROM (SELECT ((r.o - 1) / 4)::integer AS g, string_agg(r.m[1], '' ORDER BY r.o) AS c
                  FROM regexp_matches(t, '.{{1,{_RUN}}}', 'g') WITH ORDINALITY AS r(m, o)
                  GROUP BY 1) q
        ) z
        WHERE z.g = 0 OR z.n > {CHUNK_OVERLAP}
        UNION ALL
        SELECT 0, '' WHERE t = ''
    $fn$
    """,
    # One source row's projection: its pieces, or one overflow marker when
    # the text is too long to index. ``body`` NULL = nothing recall scores.
    f"""
    CREATE OR REPLACE FUNCTION session_recall_put(
        src text, sid text, rkey text, rts double precision, body text,
        eseq bigint, etype text, eraw text, estamp text) RETURNS void
    LANGUAGE plpgsql AS $fn$
    BEGIN
        IF body IS NULL THEN
            RETURN;
        END IF;
        IF length(body) > {MAX_ROW_CHARS} THEN
            INSERT INTO {TABLE} (source, session_id, row_key, part, ts, body, seq, event_type, raw, stamp)
            VALUES (src, sid, rkey, {OVERFLOW_PART}, rts, '', eseq, etype, eraw, estamp)
            ON CONFLICT (source, session_id, row_key, part) DO UPDATE SET ts = EXCLUDED.ts,
                seq = EXCLUDED.seq, event_type = EXCLUDED.event_type, raw = EXCLUDED.raw,
                stamp = EXCLUDED.stamp;
        ELSE
            INSERT INTO {TABLE} (source, session_id, row_key, part, ts, body, seq, event_type, raw, stamp)
            SELECT src, sid, rkey, p.part, rts, p.piece,
                   CASE WHEN p.part = 0 THEN eseq END, CASE WHEN p.part = 0 THEN etype END,
                   CASE WHEN p.part = 0 THEN eraw END, CASE WHEN p.part = 0 THEN estamp END
            FROM recall_pieces(body) p
            ON CONFLICT (source, session_id, row_key, part) DO UPDATE SET ts = EXCLUDED.ts,
                body = EXCLUDED.body, seq = EXCLUDED.seq, event_type = EXCLUDED.event_type,
                raw = EXCLUDED.raw, stamp = EXCLUDED.stamp;
        END IF;
    END
    $fn$
    """,
]


def _sync_function(source: str, name: str, old_key: str, put: str, put_when: str = "TRUE") -> str:
    """A trigger function that can never fail the write it runs in.

    Any error rolls back just the projection change (the block is a
    subtransaction), logs a warning and restarts that source's backfill, so
    :func:`ready` turns false and search keeps the legacy queries until the
    source has been copied again. Everything it calls is resolved at run
    time, so even a dropped table or function only lands in the handler.
    """
    return f"""
    CREATE OR REPLACE FUNCTION {name}() RETURNS trigger
    LANGUAGE plpgsql AS $fn$
    BEGIN
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                DELETE FROM {TABLE} WHERE source = '{source}' AND session_id = OLD.session_id
                    AND row_key = {old_key};
            END IF;
            IF TG_OP <> 'DELETE' AND ({put_when}) THEN
                PERFORM {put};
            END IF;
        EXCEPTION WHEN OTHERS THEN
            RAISE WARNING 'SessionSearch projection (%) not updated: % %; search uses the legacy queries '
                          'until it is rebuilt', '{source}', SQLSTATE, SQLERRM;
            BEGIN
                UPDATE session_recall_backfill SET done = FALSE, last_key = NULL WHERE source = '{source}';
            EXCEPTION WHEN OTHERS THEN
                NULL;
            END;
        END;
        RETURN NULL;
    END
    $fn$
    """


_DDL += [
    _sync_function(
        NATIVE, "session_recall_nodes_sync", "OLD.node_id",
        "session_recall_put('n', NEW.session_id, NEW.node_id, NEW.ts, recall_node_text(NEW.message), "
        "NULL, NULL, NULL, NULL)",
    ),
    _sync_function(
        ENTRY, "session_recall_entries_sync", "OLD.id::text",
        "session_recall_put('s', NEW.session_id, NEW.id::text, EXTRACT(EPOCH FROM NEW.created_at), "
        "recall_entry_text(NEW.entry), NULL, NULL, NULL, NULL)",
        put_when="NEW.subpath IS NULL",
    ),
    _sync_function(
        EVENT, "session_recall_events_sync", "OLD.id",
        "session_recall_put('e', NEW.session_id, NEW.id, COALESCE(EXTRACT(EPOCH FROM NEW.created_at), 0), "
        "lower(recall_event_raw(NEW.event_type, NEW.payload)), NEW.seq, NEW.event_type, "
        "recall_event_raw(NEW.event_type, NEW.payload), NEW.payload->>'timestamp')",
    ),
]

_TRIGGER_DDL = {
    "nodes": (
        "AFTER INSERT OR DELETE OR UPDATE OF session_id, node_id, ts, message ON fluid_session_nodes "
        "FOR EACH ROW EXECUTE FUNCTION session_recall_nodes_sync()"
    ),
    "entries": (
        "AFTER INSERT OR DELETE OR UPDATE OF id, session_id, subpath, entry, created_at ON session_store_entries "
        "FOR EACH ROW EXECUTE FUNCTION session_recall_entries_sync()"
    ),
    # Most events are tool deltas: the WHEN clauses keep them off the trigger.
    "events_ins": (
        f"AFTER INSERT ON session_events FOR EACH ROW WHEN (NEW.event_type IN {_TEXT_EVENTS_SQL}) "
        "EXECUTE FUNCTION session_recall_events_sync()"
    ),
    "events_del": (
        f"AFTER DELETE ON session_events FOR EACH ROW WHEN (OLD.event_type IN {_TEXT_EVENTS_SQL}) "
        "EXECUTE FUNCTION session_recall_events_sync()"
    ),
    "events_upd": (
        "AFTER UPDATE ON session_events FOR EACH ROW "
        f"WHEN (OLD.event_type IN {_TEXT_EVENTS_SQL} OR NEW.event_type IN {_TEXT_EVENTS_SQL}) "
        "EXECUTE FUNCTION session_recall_events_sync()"
    ),
}


def _set_lock_timeout(cursor, ms: int) -> None:
    cursor.execute("SELECT set_config('lock_timeout', %s, true)", (f"{int(ms)}ms",))


def _table_tag(cursor):
    cursor.execute("SELECT obj_description(to_regclass(%s), 'pg_class') AS tag", (TABLE,))
    row = cursor.fetchone()
    return row["tag"] if row else None


def install_objects(cursor, *, lock_timeout_ms: int = LOCK_TIMEOUT_MS) -> bool:
    """Create (or rebuild for a new ``VERSION``) the table, indexes and functions.

    Takes no lock on a source table. Savepoint-fenced: without pg_trgm (or on
    the SQLite test shim) recall keeps its legacy queries. A newly created
    table starts every source's backfill over. Returns True on success.
    """
    cursor.execute("SAVEPOINT session_recall_objects")
    try:
        cursor.execute("SELECT current_setting('lock_timeout') AS v")
        previous = cursor.fetchone()["v"]
        _set_lock_timeout(cursor, lock_timeout_ms)
        cursor.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
        cursor.execute("SELECT to_regclass(%s) IS NOT NULL AS present", (TABLE,))
        present = cursor.fetchone()["present"]
        if present and _table_tag(cursor) != _TABLE_TAG:
            cursor.execute(f"DROP TABLE {TABLE}")
            present = False
        for statement in _DDL if present else [*_TABLE_DDL, *_DDL]:
            cursor.execute(statement)
        if not present:
            cursor.execute("DELETE FROM session_recall_backfill")
        cursor.execute("SELECT set_config('lock_timeout', %s, true)", (previous,))
        cursor.execute("RELEASE SAVEPOINT session_recall_objects")
    except Exception:  # recall degrades to the legacy queries
        cursor.execute("ROLLBACK TO SAVEPOINT session_recall_objects")
        cursor.execute("RELEASE SAVEPOINT session_recall_objects")
        logger.info("SessionSearch projection not installed yet; recall keeps the legacy queries",
                    exc_info=True)
        return False
    return True


def _source_triggers(cursor, table: str) -> set:
    cursor.execute(
        "SELECT t.tgname FROM pg_trigger t WHERE NOT t.tgisinternal AND t.tgrelid = to_regclass(%s) "
        "AND t.tgname LIKE 'trg\\_session\\_recall\\_%%'",
        (table,),
    )
    return {r["tgname"] for r in cursor.fetchall()}


def install_triggers(*, lock_timeout_ms: int = LOCK_TIMEOUT_MS) -> bool:
    """Install this version's triggers, one source table per short transaction.

    Each transaction waits at most ``lock_timeout_ms`` for its table, so
    writers never queue behind it for long. Triggers of another version are
    replaced. A source whose trigger had to be created is backfilled again
    from the start. Returns True when every trigger is in place.
    """
    complete = True
    for source, (table, suffixes) in _SOURCE_TABLES.items():
        wanted = {_trigger_name(s): s for s in suffixes}
        try:
            with get_db() as conn:
                cur = conn.cursor()
                present = _source_triggers(cur, table)
                if present == set(wanted):
                    continue
                _set_lock_timeout(cur, lock_timeout_ms)
                for stale in sorted(present - set(wanted)):
                    cur.execute(f'DROP TRIGGER "{stale}" ON {table}')
                for name, suffix in wanted.items():
                    if name not in present:
                        cur.execute(f"CREATE TRIGGER {name} {_TRIGGER_DDL[suffix]}")
                cur.execute(
                    "UPDATE session_recall_backfill SET done = FALSE, last_key = NULL WHERE source = %s",
                    (source,),
                )
        except Exception:  # a busy table is retried on a later pass
            logger.info("SessionSearch projection trigger on %s not installed yet", table, exc_info=True)
            complete = False
    return complete


def uninstall(*, lock_timeout_ms: int = 2000) -> None:
    """Remove every projection object (triggers first, one table at a time)."""
    for table, _ in _SOURCE_TABLES.values():
        with get_db() as conn:
            cur = conn.cursor()
            names = _source_triggers(cur, table)
            if names:
                _set_lock_timeout(cur, lock_timeout_ms)
                for name in sorted(names):
                    cur.execute(f'DROP TRIGGER "{name}" ON {table}')
    with get_db() as conn:
        cur = conn.cursor()
        _set_lock_timeout(cur, lock_timeout_ms)
        cur.execute(f"DROP TABLE IF EXISTS {TABLE}")
        cur.execute("DROP TABLE IF EXISTS session_recall_backfill")
        for fn in ("session_recall_nodes_sync()", "session_recall_entries_sync()", "session_recall_events_sync()",
                   "session_recall_put(text, text, text, double precision, text, bigint, text, text, text)",
                   "recall_pieces(text)", "recall_node_text(jsonb)", "recall_entry_text(jsonb)",
                   "recall_event_raw(text, jsonb)"):
            cur.execute(f"DROP FUNCTION IF EXISTS {fn}")


def installed(cursor) -> bool:
    """This version's table and every one of its triggers exist."""
    if not table_exists(cursor, TABLE):
        return False
    if _table_tag(cursor) != _TABLE_TAG:
        return False
    cursor.execute(
        "SELECT COUNT(*) AS n FROM pg_trigger WHERE NOT tgisinternal AND tgname = ANY(%s)", (list(_TRIGGERS),)
    )
    return int(cursor.fetchone()["n"]) == len(_TRIGGERS)


def ready(cursor) -> bool:
    """True while the projection is installed and every source backfilled."""
    if not table_exists(cursor, "session_recall_backfill"):
        return False  # also the SQLite test shim, which never gets the projection
    if not installed(cursor):
        return False
    cursor.execute("SELECT COUNT(*) AS n FROM session_recall_backfill WHERE done")
    return int(cursor.fetchone()["n"]) == len(SOURCES)


# Per source: the batch's last key (bounded by rows and by stored bytes), then
# the copy of every row up to that key, share-locked so a concurrent delete or
# update waits for this short transaction instead of racing it.
_KEY_COLUMNS = {NATIVE: ("session_id", "node_id"), ENTRY: ("id",), EVENT: ("session_id", "seq")}
_SIZE = {
    NATIVE: "pg_column_size(message)",
    ENTRY: "pg_column_size(entry)",
    EVENT: f"CASE WHEN event_type IN {_TEXT_EVENTS_SQL} THEN pg_column_size(payload) ELSE 0 END",
}
_FILTER = {NATIVE: "TRUE", ENTRY: "subpath IS NULL", EVENT: "TRUE"}
_SOURCE_TABLE = {s: t for s, (t, _) in _SOURCE_TABLES.items()}
_PUT = {
    NATIVE: ("SELECT COUNT(session_recall_put('n', session_id, node_id, ts, recall_node_text(message), "
             "NULL, NULL, NULL, NULL)) AS n FROM ({rows}) b"),
    ENTRY: ("SELECT COUNT(session_recall_put('s', session_id, id::text, EXTRACT(EPOCH FROM created_at), "
            "recall_entry_text(entry), NULL, NULL, NULL, NULL)) AS n FROM ({rows}) b"),
    EVENT: ("SELECT COUNT(session_recall_put('e', session_id, id, COALESCE(EXTRACT(EPOCH FROM created_at), 0), "
            "lower(r), seq, event_type, r, stamp)) AS n FROM ("
            "SELECT session_id, id, created_at, seq, event_type, recall_event_raw(event_type, payload) AS r, "
            "payload->>'timestamp' AS stamp FROM ({rows}) l) b"),
}
_ROWS = {
    NATIVE: "SELECT session_id, node_id, ts, message FROM fluid_session_nodes WHERE {range} FOR SHARE NOWAIT",
    ENTRY: ("SELECT id, session_id, created_at, entry FROM session_store_entries "
            "WHERE {range} AND subpath IS NULL FOR SHARE NOWAIT"),
    EVENT: ("SELECT id, session_id, seq, event_type, payload, created_at FROM session_events "
            f"WHERE {{range}} AND event_type IN {_TEXT_EVENTS_SQL} FOR SHARE NOWAIT"),
}
_START_KEY = {NATIVE: ["", ""], ENTRY: [0], EVENT: ["", -1]}


def _row(columns) -> str:
    return f"({', '.join(columns)})"


def _params(key) -> str:
    return f"({', '.join(['%s'] * len(key))})"


def backfill_step(cursor, source: str, batch: int | None = None, max_bytes: int | None = None) -> bool:
    """Copy one batch of ``source``; True once that source is complete.

    Caller owns the (short) transaction: rows and progress commit together, so
    an interrupted pass resumes exactly where it stopped. Raises
    ``psycopg2.errors.LockNotAvailable`` when a row of the batch is being
    written; the caller rolls back and retries later.
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
    columns = _KEY_COLUMNS[source]
    table = _SOURCE_TABLE[source]
    order = ", ".join(columns)
    size = _SIZE[source]
    # The first row always fits, however large: every batch makes progress.
    cursor.execute(
        f"SELECT {', '.join(f'{c} AS k{i}' for i, c in enumerate(columns))} FROM ("
        f"SELECT {order}, SUM({size}) OVER (ORDER BY {order}) - {size} AS before FROM {table} "
        f"WHERE {_row(columns)} > {_params(key)} AND {_FILTER[source]} ORDER BY {order} LIMIT %s"
        f") w WHERE before < %s ORDER BY {', '.join(f'{c} DESC' for c in columns)} LIMIT 1",
        [*key, int(batch or _BATCH[source]), int(max_bytes or _BATCH_BYTES)],
    )
    last = cursor.fetchone()
    if last is None:
        cursor.execute("UPDATE session_recall_backfill SET done = TRUE WHERE source = %s", (source,))
        return True
    new_key = [last[f"k{i}"] for i in range(len(columns))]
    span = f"{_row(columns)} > {_params(key)} AND {_row(columns)} <= {_params(new_key)}"
    cursor.execute(_PUT[source].format(rows=_ROWS[source].format(range=span)), [*key, *new_key])
    cursor.execute(
        "UPDATE session_recall_backfill SET last_key = %s WHERE source = %s",
        (json.dumps(new_key), source),
    )
    return False


def run_backfill(*, max_seconds: float = 20.0, pause_seconds: float = 0.1) -> bool:
    """One bounded pass of installation plus backfill; True when recall is ready.

    Raises when the projection cannot be installed yet, so the caller backs off.

    Every batch is its own short transaction (bounded by rows and bytes),
    separated by ``pause_seconds`` so foreground writers never queue behind
    it. Called repeatedly by the startup task; once ready, a pass only checks
    (and repairs) the installation.
    """
    deadline = time.monotonic() + max_seconds
    with get_db() as conn:
        cur = conn.cursor()
        if ready(cur):
            return True
        objects = installed(cur) or install_objects(cur)
    if not objects or not install_triggers():
        raise RuntimeError("projection not installed (pg_trgm unavailable or a source table busy)")
    pending = list(SOURCES)
    while pending and time.monotonic() < deadline:
        source = pending[0]
        try:
            with get_db() as conn:
                if backfill_step(conn.cursor(), source):
                    pending.pop(0)
                    continue
        except psycopg2.errors.LockNotAvailable:
            pass  # a writer holds a row of this batch; retry after the pause
        time.sleep(pause_seconds)
    with get_db() as conn:
        cur = conn.cursor()
        if not ready(cur):
            return False
        # Fresh planner statistics before the first search reads the table.
        cur.execute(f"ANALYZE {TABLE}")
    logger.info("SessionSearch projection backfilled; recall now uses it")
    return True
