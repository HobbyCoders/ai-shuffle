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
Each source has its own partial trigram index; one newest-first index
serves all three.

Consecutive pieces overlap by ``CHUNK_OVERLAP`` characters, so any substring of
up to ``CHUNK_OVERLAP + 1`` characters lies whole inside one piece; searches
match longer terms by that prefix (a superset) and the Python test stays exact.
Pieces are cut in one linear pass (``recall_pieces``): ``substr`` at an offset
walks a UTF-8 string from its start, which made a megabyte message quadratic.

Long rows. A user's write never indexes more than ``MAX_ROW_CHARS``
characters: a longer row gets one marker row instead (``part`` = ``PENDING``,
with its length in ``chars``), and :func:`promote_overflow` cuts it into
pieces shortly afterwards in the background. Until then search reads marked
rows from their source table, newest first and within a character budget.
The background (backfill included) indexes at most the first
``INDEX_MAX_CHARS`` characters of a row; text past that in one message is not
searchable.

Maintenance:

- Row triggers on the three source tables keep the projection exact on every
  insert, update and delete, whichever writer (or app version) made it. They
  run no subtransaction (bulk writes stay as fast as they were) and skip the
  projection while any of its objects is missing; :func:`installed` is then
  false, search keeps the legacy queries, and the startup task's next pass
  recreates the objects and copies every source again.
- The triggers are installed one table per short transaction, waiting at most
  ``LOCK_TIMEOUT_MS`` for the table, so writers never queue behind them for
  long; a busy table just means a later pass. Creating a trigger restarts that
  source's backfill, so rows written while it was missing are copied too.
- Existing rows are copied by :func:`run_backfill` in primary-key-ordered
  batches bounded by row count, by stored bytes and by extracted characters,
  one short transaction each, resumable from ``session_recall_backfill``. Each
  batch share-locks just the rows it copies (``NOWAIT``: a busy row makes the
  batch retry rather than wait), so a concurrent delete or update cannot
  interleave with the copy and waits at most for that batch.
- SessionSearch reads the projection only while :func:`ready` holds: current
  version, every function and trigger present, every source backfilled.
- ``VERSION`` names the triggers and tags the table. Changing the projection's
  shape bumps it: the old table is dropped and rebuilt, and the old triggers
  are replaced table by table.

An older app version ignores these objects, but the triggers keep running
(and the table keeps growing with transcripts) until they are removed with
``python -m app.maintenance.session_recall_projection --remove``.

The SQL extractors mirror the Python scorers (``message_texts``,
``_sse_entry_texts``, ``_fold_events``) and may only ever return a SUPERSET of
what those score: the projection is a prefilter and the Python substring test
stays the real match. They cannot raise on any JSON shape.
"""

from __future__ import annotations

import json
import logging
import time

import psycopg2.errors

from app.db.base import get_db, table_exists

logger = logging.getLogger(__name__)

VERSION = 3
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
# Most characters a user's write indexes; most the background indexes per row.
MAX_ROW_CHARS = 200_000
INDEX_MAX_CHARS = 5_000_000
# Marker ``part`` of a row waiting to be cut into pieces (see the docstring).
PENDING = -1
LOCK_TIMEOUT_MS = 250

_TABLE_TAG = f"session_recall v{VERSION}"
# Stands for the install schema in what the triggers run: names there are
# schema-qualified at install, so a writer whose search_path lacks the schema
# (``pg_restore`` sets it empty) is projected like any other.
_S = "@schema@."

# source -> (source table, trigger name suffixes)
_SOURCE_TABLES = {
    NATIVE: ("fluid_session_nodes", ("nodes",)),
    ENTRY: ("session_store_entries", ("entries",)),
    EVENT: ("session_events", ("events_ins", "events_del", "events_upd")),
}


def _trigger_name(suffix: str) -> str:
    return f"trg_session_recall_v{VERSION}_{suffix}"


_TRIGGERS = tuple(_trigger_name(s) for _, suffixes in _SOURCE_TABLES.values() for s in suffixes)

# Per backfill transaction: at most this many rows, this many stored (possibly
# compressed) bytes of the column the extractor reads, and this many
# extracted characters cut into pieces.
_BATCH = {NATIVE: 200, ENTRY: 200, EVENT: 2000}
_BATCH_BYTES = 2_000_000
_BATCH_CHARS = 1_000_000

_PUT_SIGNATURE = "session_recall_put(text, text, text, double precision, text, bigint, text, text, text, integer)"
_FUNCTIONS = (
    "recall_node_text(jsonb)", "recall_entry_text(jsonb)", "recall_event_raw(text, jsonb)",
    "recall_pieces(text)", _PUT_SIGNATURE,
    "session_recall_background_put(text,text,text,double precision,text,"
    "bigint,text,text,text,integer)",
    "session_recall_nodes_sync()", "session_recall_entries_sync()", "session_recall_events_sync()",
)

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
        -- An event's first row only (part 0, or its marker): what
        -- _fold_events reads, so recall folds a mirrored session without
        -- decoding its JSON payloads.
        seq BIGINT,
        event_type TEXT,
        raw TEXT,
        stamp TEXT,
        -- A marker row only: the length of the text it stands for.
        chars INTEGER,
        PRIMARY KEY (source, session_id, row_key, part)
    )
    """,
    f"COMMENT ON TABLE {TABLE} IS '{_TABLE_TAG}'",
    # Search reads pieces newest first. The predicate keeps this index away
    # from the triggers' per-row lookups (source, session, key), which cannot
    # prove it: while statistics showed few rows of a source, the planner
    # preferred a newest-first index over the primary key there and scanned
    # all of it for every row of a bulk delete.
    f"CREATE INDEX idx_session_recall_text_ts ON {TABLE} (source, ts DESC) WHERE part >= 0",
    *[f"CREATE INDEX idx_session_recall_text_trgm_{s} ON {TABLE} "
      f"USING gin (body gin_trgm_ops) WHERE source = '{s}'" for s in SOURCES],
    f"CREATE INDEX idx_session_recall_text_marker ON {TABLE} (source, ts DESC) WHERE part = {PENDING}",
]

# Progress table and functions: idempotent, no lock on any table in use.
_DDL = [
    # Version 1's signature, before ``cap``.
    "DROP FUNCTION IF EXISTS session_recall_put(text, text, text, double precision, text, bigint, text, text, text)",
    """
    CREATE TABLE IF NOT EXISTS session_recall_backfill (
        source TEXT PRIMARY KEY,
        last_key TEXT,
        done BOOLEAN NOT NULL DEFAULT FALSE
    )
    """,
    # fluid's ``message_texts``: text blocks plus the text parts of tool results.
    # PL/pgSQL rather than SQL: a SQL function with a FROM clause is not
    # inlined, and the trigger would plan it again for every row.
    """
    CREATE OR REPLACE FUNCTION recall_node_text(message jsonb) RETURNS text
    LANGUAGE plpgsql IMMUTABLE AS $fn$
    BEGIN
        RETURN (
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
            WHERE COALESCE(x.t, '') <> '');
    END
    $fn$
    """,
    # ``_sse_entry_texts``. Only an explicit ``true`` meta/sidechain flag is
    # skipped here; Python drops any other truthy value afterwards.
    """
    CREATE OR REPLACE FUNCTION recall_entry_text(entry jsonb) RETURNS text
    LANGUAGE plpgsql IMMUTABLE AS $fn$
    BEGIN
        RETURN CASE
            WHEN COALESCE(entry->>'type', '') NOT IN ('user', 'assistant') THEN NULL
            WHEN entry->'isMeta' = 'true'::jsonb OR entry->'isSidechain' = 'true'::jsonb THEN NULL
            WHEN jsonb_typeof(entry #> '{message,content}') = 'string'
                THEN NULLIF(lower(entry #>> '{message,content}'), '')
            WHEN jsonb_typeof(entry #> '{message,content}') = 'array' THEN (
                SELECT lower(string_agg(b->>'text', E'\\n'))
                FROM jsonb_array_elements(entry #> '{message,content}') b
                WHERE b->>'type' = 'text' AND COALESCE(b->>'text', '') <> '')
        END;
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
    # than the overlap already lies inside the previous piece. Text of at
    # most CHUNK characters ('' included) is one piece.
    f"""
    CREATE OR REPLACE FUNCTION recall_pieces(t text) RETURNS TABLE (part integer, piece text)
    LANGUAGE plpgsql IMMUTABLE AS $fn$
    BEGIN
        IF length(t) <= {CHUNK_CHARS} THEN
            part := 0;
            piece := t;
            RETURN NEXT;
            RETURN;
        END IF;
        RETURN QUERY
        SELECT z.g, z.pc FROM (
            SELECT q.g, q.c || COALESCE(left(lead(q.c) OVER (ORDER BY q.g), {CHUNK_OVERLAP}), '') AS pc,
                   length(q.c) AS n
            FROM (SELECT ((r.o - 1) / 4)::integer AS g, string_agg(r.m[1], '' ORDER BY r.o) AS c
                  FROM regexp_matches(t, '.{{1,{_RUN}}}', 'g') WITH ORDINALITY AS r(m, o)
                  GROUP BY 1) q
        ) z
        WHERE z.g = 0 OR z.n > {CHUNK_OVERLAP};
    END
    $fn$
    """,
    # One source row's projection. A user's write passes cap = MAX_ROW_CHARS
    # and writes a marker for longer text; the background passes
    # INDEX_MAX_CHARS, replaces whatever the row had (its marker included)
    # and indexes at most that many characters. ``body`` NULL = nothing
    # recall scores.
    f"""
    CREATE OR REPLACE FUNCTION session_recall_put(
        src text, sid text, rkey text, rts double precision, body text,
        eseq bigint, etype text, eraw text, estamp text, cap integer) RETURNS void
    LANGUAGE plpgsql AS $fn$
    DECLARE
        n integer;
    BEGIN
        IF body IS NULL THEN
            RETURN;
        END IF;
        n := length(body);
        IF cap > {MAX_ROW_CHARS} THEN
            -- The background holds the source row's share lock.
            DELETE FROM {_S}{TABLE} WHERE source = src AND session_id = sid AND row_key = rkey;
            IF n > cap THEN
                body := left(body, cap);
            END IF;
        ELSIF n > cap THEN
            INSERT INTO {_S}{TABLE} (source, session_id, row_key, part, ts, body, seq, event_type, raw, stamp, chars)
            VALUES (src, sid, rkey, {PENDING}, rts, '', eseq, etype, eraw, estamp, n)
            ON CONFLICT (source, session_id, row_key, part) DO UPDATE SET ts = EXCLUDED.ts,
                seq = EXCLUDED.seq, event_type = EXCLUDED.event_type, raw = EXCLUDED.raw,
                stamp = EXCLUDED.stamp, chars = EXCLUDED.chars;
            RETURN;
        END IF;
        INSERT INTO {_S}{TABLE} (source, session_id, row_key, part, ts, body, seq, event_type, raw, stamp)
        SELECT src, sid, rkey, p.part, rts, p.piece,
               CASE WHEN p.part = 0 THEN eseq END, CASE WHEN p.part = 0 THEN etype END,
               CASE WHEN p.part = 0 THEN eraw END, CASE WHEN p.part = 0 THEN estamp END
        FROM {_S}recall_pieces(body) p
        ON CONFLICT (source, session_id, row_key, part) DO UPDATE SET ts = EXCLUDED.ts,
            body = EXCLUDED.body, seq = EXCLUDED.seq, event_type = EXCLUDED.event_type,
            raw = EXCLUDED.raw, stamp = EXCLUDED.stamp;
    END
    $fn$
    """,
    f"""
    CREATE OR REPLACE FUNCTION session_recall_background_put(
        src text, sid text, rkey text, rts double precision, body text,
        eseq bigint, etype text, eraw text, estamp text, history_state integer) RETURNS void
    LANGUAGE plpgsql AS $fn$
    DECLARE previous text;
    BEGIN
        previous := COALESCE(current_setting('aishuffle.history_index_state', true), '0');
        PERFORM set_config('aishuffle.history_index_state', history_state::text, true);
        PERFORM {_S}session_recall_put(src, sid, rkey, rts, body, eseq, etype,
                                     eraw, estamp, {INDEX_MAX_CHARS});
        PERFORM set_config('aishuffle.history_index_state', previous, true);
    END
    $fn$
    """,
]


def _sync_function(source: str, name: str, old_key: str, put: str, needs: tuple,
                   put_when: str = "TRUE") -> str:
    """A row trigger function. No exception block: a subtransaction per row
    would overflow every bulk write's subtransaction cache. It skips the
    projection while the table or a function it calls is missing (dropped to
    free disk, a partial restore); :func:`installed` is false then, so search
    keeps the legacy queries until the startup task rebuilds everything.
    """
    present = " AND ".join(
        [f"to_regclass('{_S}{TABLE}') IS NOT NULL", *[f"to_regprocedure('{_S}{fn}') IS NOT NULL" for fn in needs]])
    # Event triggers see every UPDATE (unlike nodes/entries' column-specific
    # triggers). State-only promotion must not delete/reinsert the text pieces:
    # they already have their own durable queue. Do not skip ordinary live edits,
    # including a no-op edit that moves pending history back to the live index.
    promotion_guard = ""
    if source == EVENT:
        fields = ["id", "session_id", "seq", "event_type", "payload", "created_at"]
        old = ", ".join(f"OLD.{field}" for field in fields)
        new = ", ".join(f"NEW.{field}" for field in fields)
        promotion_guard = f"""
        IF TG_OP = 'UPDATE' THEN
            IF current_setting('aishuffle.history_index_state', true) = '2'
                AND ROW({old}) IS NOT DISTINCT FROM ROW({new}) THEN
                RETURN NULL;
            END IF;
        END IF;
        """
    return f"""
    CREATE OR REPLACE FUNCTION {name}() RETURNS trigger
    LANGUAGE plpgsql AS $fn$
    BEGIN
        {promotion_guard}
        IF NOT ({present}) THEN
            RETURN NULL;
        END IF;
        IF TG_OP <> 'INSERT' THEN
            DELETE FROM {_S}{TABLE} WHERE source = '{source}' AND session_id = OLD.session_id
                AND row_key = {old_key};
        END IF;
        IF TG_OP <> 'DELETE' AND ({put_when}) THEN
            PERFORM {put};
        END IF;
        RETURN NULL;
    END
    $fn$
    """


_DDL += [
    _sync_function(
        NATIVE, "session_recall_nodes_sync", "OLD.node_id",
        f"{_S}session_recall_put('n', NEW.session_id, NEW.node_id, NEW.ts, {_S}recall_node_text(NEW.message), "
        f"NULL, NULL, NULL, NULL, {MAX_ROW_CHARS})",
        ("recall_node_text(jsonb)", "recall_pieces(text)", _PUT_SIGNATURE),
    ),
    _sync_function(
        ENTRY, "session_recall_entries_sync", "OLD.id::text",
        f"{_S}session_recall_put('s', NEW.session_id, NEW.id::text, EXTRACT(EPOCH FROM NEW.created_at), "
        f"{_S}recall_entry_text(NEW.entry), NULL, NULL, NULL, NULL, {MAX_ROW_CHARS})",
        ("recall_entry_text(jsonb)", "recall_pieces(text)", _PUT_SIGNATURE),
        put_when="NEW.subpath IS NULL",
    ),
    _sync_function(
        EVENT, "session_recall_events_sync", "OLD.id",
        f"{_S}session_recall_put('e', NEW.session_id, NEW.id, COALESCE(EXTRACT(EPOCH FROM NEW.created_at), 0), "
        f"lower({_S}recall_event_raw(NEW.event_type, NEW.payload)), NEW.seq, NEW.event_type, "
        f"{_S}recall_event_raw(NEW.event_type, NEW.payload), NEW.payload->>'timestamp', {MAX_ROW_CHARS})",
        ("recall_event_raw(text, jsonb)", "recall_pieces(text)", _PUT_SIGNATURE),
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


def _missing_functions(cursor) -> list:
    cursor.execute(
        "SELECT f FROM unnest(%s::text[]) AS f WHERE to_regprocedure(f) IS NULL", (list(_FUNCTIONS),)
    )
    return [r["f"] for r in cursor.fetchall()]


def install_objects(cursor, *, lock_timeout_ms: int = LOCK_TIMEOUT_MS) -> bool:
    """Create (or rebuild for a new ``VERSION``) the table, indexes and functions.

    Takes no lock on a source table. Savepoint-fenced: without pg_trgm (or on
    the SQLite test shim) recall keeps its legacy queries. When the table or
    any function was missing, the triggers may have skipped writes, so every
    source's backfill starts over. Returns True on success.
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
        missing = _missing_functions(cursor)
        # Adding the background state wrapper does not change extracted text;
        # install it without replaying every existing transcript.
        intact = present and not [f for f in missing if not f.startswith(
            "session_recall_background_put(")]
        cursor.execute("SELECT quote_ident(current_schema()) AS s")
        schema = cursor.fetchone()["s"]
        if not schema or "'" in schema:
            raise ValueError("no usable current schema")
        if not present:
            # Preserve existing opt-in so automatic maintenance rebuilds the
            # indexes and resumes pending imports. Never opt in a new worker.
            cursor.execute("UPDATE system_settings SET value = 'building' WHERE key = %s",
                           ("migration:history_indexing_v1",))
        for statement in _DDL if present else [*_TABLE_DDL, *_DDL]:
            cursor.execute(statement.replace(_S, f"{schema}."))
        if not intact:
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
        for fn in reversed(_FUNCTIONS):
            cur.execute(f"DROP FUNCTION IF EXISTS {fn}")


def installed(cursor) -> bool:
    """This version's table, every function and every trigger exist."""
    if not table_exists(cursor, TABLE):
        return False
    if _table_tag(cursor) != _TABLE_TAG or _missing_functions(cursor):
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


# Per source: its primary key, the stored size of the column its extractor
# reads, and session_recall_put's arguments computed from one of its rows
# (``x``). Rows are share-locked while copied, so a concurrent delete or update
# waits for the short copy instead of racing it.
_KEY_COLUMNS = {NATIVE: ("session_id", "node_id"), ENTRY: ("id",), EVENT: ("session_id", "seq")}
_SIZE = {
    NATIVE: "pg_column_size(message)",
    ENTRY: "pg_column_size(entry)",
    EVENT: f"CASE WHEN event_type IN {_TEXT_EVENTS_SQL} THEN pg_column_size(payload) ELSE 0 END",
}
_FILTER = {NATIVE: "TRUE", ENTRY: "subpath IS NULL", EVENT: "TRUE"}
# The rows that are copied.
_COPY_FILTER = {**_FILTER, EVENT: f"event_type IN {_TEXT_EVENTS_SQL}"}
_SOURCE_TABLE = {s: t for s, (t, _) in _SOURCE_TABLES.items()}
_NO_FOLD = "NULL::bigint AS eseq, NULL::text AS etype, NULL::text AS eraw, NULL::text AS estamp"
_ARGS = {
    NATIVE: ("x.session_id AS sid, x.node_id AS rkey, x.ts::float8 AS rts, "
             f"recall_node_text(x.message) AS t, {_NO_FOLD}"),
    ENTRY: ("x.session_id AS sid, x.id::text AS rkey, EXTRACT(EPOCH FROM x.created_at)::float8 AS rts, "
            f"recall_entry_text(x.entry) AS t, {_NO_FOLD}"),
    EVENT: ("x.session_id AS sid, x.id AS rkey, COALESCE(EXTRACT(EPOCH FROM x.created_at), 0)::float8 AS rts, "
            "lower(recall_event_raw(x.event_type, x.payload)) AS t, x.seq AS eseq, x.event_type AS etype, "
            "recall_event_raw(x.event_type, x.payload) AS eraw, x.payload->>'timestamp' AS estamp"),
}
_ARG_NAMES = ("sid", "rkey", "rts", "t", "eseq", "etype", "eraw", "estamp")


def _rows(source: str, where: str, tail: str = "", *, history: bool = False) -> str:
    """The share-locked rows matching ``where`` (plus ``tail``: ORDER BY,
    LIMIT), with their key columns and session_recall_put's arguments. The
    text is extracted only from the rows returned."""
    keys = ", ".join(f"x.{c}" for c in _KEY_COLUMNS[source])
    state = "x.history_index_state" if history else "0"
    return (f"SELECT {keys}, {_ARGS[source]}, {state} AS history_state "
            f"FROM (SELECT * FROM {_SOURCE_TABLE[source]} "
            f"WHERE {where} AND {_COPY_FILTER[source]} {tail} FOR SHARE NOWAIT) x")


# One row by its projection key: (where, params from session_id and row_key).
_ROW_MATCH = {
    NATIVE: ("session_id = %s AND node_id = %s", lambda sid, key: [sid, key]),
    ENTRY: ("id = %s::bigint", lambda sid, key: [key]),
    EVENT: ("id = %s", lambda sid, key: [key]),
}
_START_KEY = {NATIVE: ["", ""], ENTRY: [0], EVENT: ["", -1]}


def _put(source: str) -> str:
    return (f"session_recall_background_put('{source}', b.sid, b.rkey, b.rts, b.t, "
            "b.eseq, b.etype, b.eraw, b.estamp, b.history_state::integer)")


def _row(columns) -> str:
    return f"({', '.join(columns)})"


def _params(key) -> str:
    return f"({', '.join(['%s'] * len(key))})"


def backfill_step(cursor, source: str, batch: int | None = None, max_bytes: int | None = None,
                  max_chars: int | None = None) -> bool:
    """Copy one batch of ``source``; True once that source is complete.

    The window is the next rows in key order up to ``batch`` rows and
    ``max_bytes`` stored bytes; of those, only the leading rows up to the first
    whose text reaches ``max_chars`` in total are locked, extracted (once) and
    cut into pieces this time (a compressible window stays short). The first
    row always goes, so every batch makes progress.

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
    order = ", ".join(columns)
    keys_desc = ", ".join(f"{c} DESC" for c in columns)
    aliases = ", ".join(f"{c} AS k{i}" for i, c in enumerate(columns))
    size = _SIZE[source]
    cursor.execute(
        f"SELECT {aliases} FROM ("
        f"SELECT {order}, SUM({size}) OVER (ORDER BY {order}) - {size} AS before FROM {_SOURCE_TABLE[source]} "
        f"WHERE {_row(columns)} > {_params(key)} AND {_FILTER[source]} ORDER BY {order} LIMIT %s"
        f") w WHERE before < %s ORDER BY {keys_desc} LIMIT 1",
        [*key, int(batch or _BATCH[source]), int(max_bytes or _BATCH_BYTES)],
    )
    last = cursor.fetchone()
    if last is None:
        cursor.execute("UPDATE session_recall_backfill SET done = TRUE WHERE source = %s", (source,))
        return True
    new_key = [last[f"k{i}"] for i in range(len(columns))]
    # Stored bytes say little about a compressible row's text. The batch walks
    # the window one row at a time in key order, share-locking and extracting
    # each row once, and stops after the first row that brings its text to the
    # character budget; the rows walked are then cut up from what was
    # extracted. Rows past the stop are neither read nor locked.
    from app.db.base import column_exists
    history = column_exists(cursor, _SOURCE_TABLE[source], "history_index_state")
    weight = f"LEAST(COALESCE(length(n.t), 0), {INDEX_MAX_CHARS})"

    def next_row(after: str) -> str:
        return _rows(source, f"{_row(columns)} > {after} AND {_row(columns)} <= {_params(new_key)}",
                     f"ORDER BY {order} LIMIT 1", history=history)

    cursor.execute(
        f"WITH RECURSIVE c({order}, {', '.join(_ARG_NAMES)}, history_state, total) AS ("
        f"SELECT n.*, {weight} FROM ({next_row(_params(key))}) n "
        f"UNION ALL SELECT n.*, c.total + {weight} FROM c CROSS JOIN LATERAL ("
        f"{next_row(_row([f'c.{col}' for col in columns]))}"
        ") n WHERE c.total < %s), "
        f"w AS MATERIALIZED (SELECT COUNT({_put(source)}) AS n FROM c b) "
        f"SELECT {aliases}, (SELECT n FROM w) AS written FROM c ORDER BY {keys_desc} LIMIT 1",
        [*key, *new_key, *new_key, int(max_chars or _BATCH_CHARS)],
    )
    done = cursor.fetchone()
    if done is not None:
        new_key = [done[f"k{i}"] for i in range(len(columns))]  # the rest next time
    cursor.execute(
        "UPDATE session_recall_backfill SET last_key = %s WHERE source = %s",
        (json.dumps(new_key), source),
    )
    return False


def promote_overflow(*, max_seconds: float = 10.0, pause_seconds: float = 0.1) -> int:
    """Cut rows that a user's write only marked (too long to index inline)
    into pieces, newest first, one short transaction per row. Returns how many
    rows it promoted."""
    deadline = time.monotonic() + max_seconds
    promoted = 0
    skipped: set = set()
    while time.monotonic() < deadline:
        try:
            with get_db() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"SELECT source, session_id, row_key FROM {TABLE} WHERE part = {PENDING} "
                    "ORDER BY ts DESC LIMIT 64"
                )
                todo = [(r["source"], r["session_id"], r["row_key"]) for r in cur.fetchall()]
                todo = [t for t in todo if t not in skipped]
                if not todo:
                    break
                source, sid, key = todo[0]
                where, params = _ROW_MATCH[source]
                from app.db.base import column_exists
                history = column_exists(cur, _SOURCE_TABLE[source], "history_index_state")
                sql = _rows(source, where, history=history)
                cur.execute(f"SELECT COUNT({_put(source)}) AS n FROM ({sql}) b", params(sid, key))
                if not cur.fetchone()["n"]:
                    # Its row is gone or no longer scored: the marker is stale.
                    cur.execute(f"DELETE FROM {TABLE} WHERE source = %s AND session_id = %s AND row_key = %s",
                                (source, sid, key))
                promoted += 1
        except psycopg2.errors.LockNotAvailable:
            skipped.add((source, sid, key))  # being written; a later pass takes it
        time.sleep(pause_seconds)
    return promoted


def run_backfill(*, max_seconds: float = 20.0, pause_seconds: float = 0.1) -> bool:
    """One bounded pass of installation, backfill and promotion of marked rows;
    True when recall is ready.

    Raises when the projection cannot be installed yet, so the caller backs off.

    Every batch is its own short transaction (bounded by rows, bytes and
    characters), separated by ``pause_seconds`` so foreground writers never
    queue behind it. Called repeatedly by the startup task; once ready, a pass
    only checks (and repairs) the installation and promotes marked rows.
    """
    deadline = time.monotonic() + max_seconds
    with get_db() as conn:
        cur = conn.cursor()
        was_ready = ready(cur)
        objects = was_ready or installed(cur) or install_objects(cur)
    if not was_ready:
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
    promote_overflow(max_seconds=max(0.0, deadline - time.monotonic()), pause_seconds=pause_seconds)
    if was_ready:
        return True
    with get_db() as conn:
        cur = conn.cursor()
        if not ready(cur):
            return False
        # Fresh planner statistics before the first search reads the table.
        cur.execute(f"ANALYZE {TABLE}")
    logger.info("SessionSearch projection backfilled; recall now uses it")
    return True
