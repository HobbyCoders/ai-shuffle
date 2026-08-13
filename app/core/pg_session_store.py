"""
Postgres-backed implementation of the Claude Agent SDK ``SessionStore`` protocol.

This adapter mirrors every session transcript line the SDK subprocess produces
into a pair of Postgres tables so that Postgres becomes the single source of
truth for session history — replacing the JSONL files under
``~/.claude/projects``. The subprocess still writes locally (to a tmpfs
``CLAUDE_CONFIG_DIR``), but the durable copy lives in Postgres and is the only
thing the backend reads back for resume, listing, and summaries.

Tables
------
``session_store_entries``
    Append-only log of transcript entries. ``subpath IS NULL`` means main
    transcript; non-NULL is a subagent transcript (e.g. ``"subagents/agent-1"``).
    Per-session order is by the ``BIGSERIAL`` ``id`` column, which preserves
    append-call order within a single process and storage commit order across
    concurrent processes — exactly what the SDK contract requires.

``session_store_summaries``
    Incrementally-folded ``SessionSummaryEntry`` sidecar, one row per
    (project_key, session_id). Updated inside :meth:`PgSessionStore.append`
    via :func:`fold_session_summary`, guarded by a per-session
    ``SELECT ... FOR UPDATE`` to serialize concurrent appends.

Blocking I/O
------------
The SDK calls :meth:`append`/:meth:`load` from an event loop. psycopg2 is
synchronous, so every DB operation is dispatched to a worker thread via
``asyncio.to_thread``. This matches the pattern used elsewhere in the backend
(see ``app/core/startup.py``).

Postgres is the only source of truth for session transcripts; the old
SESSION_STORE_MODE feature flag ("disabled"/"mirror"/"postgres_only") was
removed after the cutover.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import nullcontext
from typing import Any

from claude_agent_sdk.types import (
    SessionKey,
    SessionListSubkeysKey,
    SessionStoreEntry,
    SessionStoreListEntry,
    SessionSummaryEntry,
)
from claude_agent_sdk import fold_session_summary
from psycopg2.extras import Json

from app.db.base import get_db, transaction

logger = logging.getLogger(__name__)


def _subpath_of(key: SessionKey | SessionListSubkeysKey) -> str | None:
    """Return the subpath field or ``None`` for main transcripts.

    Empty string is treated as invalid per the Protocol contract but we
    normalize defensively to ``None`` rather than raising so a misconfigured
    caller does not crash the subprocess.
    """
    sp = key.get("subpath") if isinstance(key, dict) else None  # type: ignore[arg-type]
    if sp is None or sp == "":
        return None
    return sp


# Postgres ``text``/``json``/``jsonb`` cannot store U+0000 (NUL) or lone UTF-16
# surrogates (U+D800–U+DFFF): an INSERT carrying either fails with
# "unsupported Unicode escape sequence: <0x00> cannot be converted to text" and
# aborts the whole transaction. SDK transcript entries occasionally contain
# these — e.g. a tool result that echoed a binary or UTF-16-encoded file — and
# ``psycopg2.extras.Json`` serializes them verbatim, so the mirror ``append``
# raised and the SDK surfaced a ``mirror_error`` system message to the chat.
# JSON itself permits these code points, so the on-disk JSONL transcript is
# unaffected; we sanitize only at the Postgres boundary. uuids (and every other
# id field) never contain these code points, so the drift verifier — which
# compares ``entry->>'uuid'`` — stays green.
_PG_ILLEGAL_RE = re.compile("[\x00\ud800-\udfff]")


def _scrub_pg_illegal(value: Any) -> Any:
    """Recursively replace Postgres-illegal code points (NUL, lone surrogates)
    in every string within ``value`` with U+FFFD, returning a cleaned copy.

    Dict keys are scrubbed too (a NUL in a key would break the INSERT just the
    same). Non-string scalars pass through untouched. The common case — content
    with no illegal code points — returns the original object without copying
    its strings, so the hot path stays cheap.
    """
    if isinstance(value, str):
        if _PG_ILLEGAL_RE.search(value) is None:
            return value
        return _PG_ILLEGAL_RE.sub("�", value)
    if isinstance(value, dict):
        return {_scrub_pg_illegal(k): _scrub_pg_illegal(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub_pg_illegal(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Synchronous DB primitives — each one owns its own connection/transaction.
# ---------------------------------------------------------------------------


def _append_sync(key: SessionKey, entries: list[SessionStoreEntry]) -> None:
    """Insert entries and (for main transcripts) update the summary sidecar.

    Summary fold is serialized per (project_key, session_id) via
    ``SELECT ... FOR UPDATE`` on the summaries row. The row is created on
    first append (INSERT ... ON CONFLICT DO NOTHING) so the lock target
    always exists before we read it.
    """
    if not entries:
        return

    project_key = key["project_key"]
    session_id = key["session_id"]
    subpath = _subpath_of(key)

    with get_db() as conn:
        cur = conn.cursor()

        # Bulk insert entries. executemany is fine here — batches are ~100ms
        # cadence with a handful of rows each.
        cur.executemany(
            "INSERT INTO session_store_entries "
            "(project_key, session_id, subpath, entry) "
            "VALUES (%s, %s, %s, %s)",
            [
                (project_key, session_id, subpath, Json(_scrub_pg_illegal(dict(e))))
                for e in entries
            ],
        )

        # Subagent appends MUST NOT touch the main session's summary.
        if subpath is not None:
            return

        # Ensure the summary row exists so FOR UPDATE has something to lock.
        # mtime_ms=0 is the fold's placeholder; we overwrite below.
        cur.execute(
            "INSERT INTO session_store_summaries "
            "(project_key, session_id, data, mtime_ms) "
            "VALUES (%s, %s, %s, 0) "
            "ON CONFLICT (project_key, session_id) DO NOTHING",
            (project_key, session_id, Json({})),
        )
        cur.execute(
            "SELECT data, mtime_ms FROM session_store_summaries "
            "WHERE project_key = %s AND session_id = %s FOR UPDATE",
            (project_key, session_id),
        )
        row = cur.fetchone()
        prev_data = row["data"] if row and row["data"] else {}
        prev_mtime = row["mtime_ms"] if row else 0

        # If this is the first real fold on this row (we just inserted it),
        # pass prev=None so fold seeds created_at / is_sidechain from the
        # first entry rather than leaving them off.
        if prev_data or prev_mtime:
            prev_summary: SessionSummaryEntry | None = {
                "session_id": session_id,
                "mtime": int(prev_mtime),
                "data": dict(prev_data),
            }
        else:
            prev_summary = None

        folded = fold_session_summary(prev_summary, key, list(entries))

        # mtime_ms is generated Postgres-side (same formula as
        # _list_sessions_sync's MAX(created_at)) so the SDK's fast-path
        # staleness check sees a single shared clock — otherwise app↔DB
        # drift breaks list_sessions_from_store.
        cur.execute(
            "UPDATE session_store_summaries "
            "SET data = %s, "
            "    mtime_ms = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT "
            "WHERE project_key = %s AND session_id = %s",
            (Json(_scrub_pg_illegal(folded["data"])), project_key, session_id),
        )


def _count_entries_sync(key: SessionKey, _conn=None) -> int:
    """Return how many rows already exist for ``key`` (incl. its subpath).

    Used by the backfill's idempotency pre-check: ``session_store_entries`` has
    a ``BIGSERIAL`` primary key and **no** unique constraint on
    ``(project_key, session_id, subpath)``, so a plain re-INSERT would duplicate
    rows. There is no ``ON CONFLICT`` target to lean on. The backfill therefore
    skips any (project_key, session_id, subpath) that already has rows — the
    on-disk JSONL transcript is the complete, append-only history, so "any rows
    present" means "already backfilled (or live-mirrored)".

    ``_conn``: optional open connection (the app's ``transaction()`` idiom) so
    the lazy backfill's in-lock re-check runs on the SAME connection that holds
    the advisory xact lock.
    """
    project_key = key["project_key"]
    session_id = key["session_id"]
    subpath = _subpath_of(key)

    with (nullcontext(_conn) if _conn is not None else get_db()) as conn:
        cur = conn.cursor()
        if subpath is None:
            cur.execute(
                "SELECT COUNT(*) AS n FROM session_store_entries "
                "WHERE project_key = %s AND session_id = %s AND subpath IS NULL",
                (project_key, session_id),
            )
        else:
            cur.execute(
                "SELECT COUNT(*) AS n FROM session_store_entries "
                "WHERE project_key = %s AND session_id = %s AND subpath = %s",
                (project_key, session_id, subpath),
            )
        row = cur.fetchone()
    return int(row["n"]) if row else 0


def _backfill_entries_sync(
    key: SessionKey,
    entries: list[SessionStoreEntry],
    batch_size: int = 500,
    _conn=None,
) -> int:
    """Insert ``entries`` for ``key`` using the SAME row shape as
    :func:`_append_sync`, batched, and update the summary sidecar for main
    transcripts.

    Returns the number of rows written. This deliberately reuses the exact
    column set / ``Json(dict(e))`` encoding ``_append_sync`` uses so backfilled
    rows are shape-identical to live-mirrored rows and the drift verifier
    (which compares ``entry->>'uuid'`` ordered by ``id``) passes unchanged.

    Caller is responsible for the idempotency pre-check (see
    :func:`_count_entries_sync`) — this function unconditionally inserts.
    Insertion order is preserved by the ``BIGSERIAL id`` exactly as live
    appends are, because rows are inserted in list order within one
    transaction.

    ``_conn``: optional open connection (the app's ``transaction()`` idiom).
    The lazy backfill passes the connection holding its advisory xact lock so
    the count re-check and the inserts share one serialized transaction.
    """
    if not entries:
        return 0

    project_key = key["project_key"]
    session_id = key["session_id"]
    subpath = _subpath_of(key)

    written = 0
    with (nullcontext(_conn) if _conn is not None else get_db()) as conn:
        cur = conn.cursor()
        # Batched inserts keep a single session's (possibly large) transcript
        # from building one giant executemany / huge transaction snapshot.
        for start in range(0, len(entries), batch_size):
            chunk = entries[start : start + batch_size]
            cur.executemany(
                "INSERT INTO session_store_entries "
                "(project_key, session_id, subpath, entry) "
                "VALUES (%s, %s, %s, %s)",
                [
                    (project_key, session_id, subpath, Json(_scrub_pg_illegal(dict(e))))
                    for e in chunk
                ],
            )
            written += len(chunk)

        # Subagent transcripts MUST NOT touch the main session's summary —
        # identical guard to _append_sync.
        if subpath is not None:
            return written

        # Fold the whole transcript into the summary sidecar in one shot,
        # mirroring _append_sync's create-then-fold flow. prev=None seeds
        # created_at / is_sidechain from the first entry. mtime_ms is stamped
        # Postgres-side with the same NOW()-epoch-ms formula as live appends.
        cur.execute(
            "INSERT INTO session_store_summaries "
            "(project_key, session_id, data, mtime_ms) "
            "VALUES (%s, %s, %s, 0) "
            "ON CONFLICT (project_key, session_id) DO NOTHING",
            (project_key, session_id, Json({})),
        )
        cur.execute(
            "SELECT data, mtime_ms FROM session_store_summaries "
            "WHERE project_key = %s AND session_id = %s FOR UPDATE",
            (project_key, session_id),
        )
        row = cur.fetchone()
        prev_data = row["data"] if row and row["data"] else {}
        prev_mtime = row["mtime_ms"] if row else 0
        if prev_data or prev_mtime:
            prev_summary: SessionSummaryEntry | None = {
                "session_id": session_id,
                "mtime": int(prev_mtime),
                "data": dict(prev_data),
            }
        else:
            prev_summary = None

        folded = fold_session_summary(prev_summary, key, list(entries))
        cur.execute(
            "UPDATE session_store_summaries "
            "SET data = %s, "
            "    mtime_ms = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT "
            "WHERE project_key = %s AND session_id = %s",
            (Json(_scrub_pg_illegal(folded["data"])), project_key, session_id),
        )

    return written


def _load_entries_by_session_id_sync(
    session_id: str, subpath: str | None = None
) -> list[SessionStoreEntry]:
    """Return the raw ``entry`` dicts for ``session_id`` (+``subpath``), id ASC.

    Matched by ``session_id`` (and ``subpath``) ALONE — the ``project_key``
    predicate is deliberately dropped. The ``project_key`` stored at write time
    is the on-disk folder name, but the SDK's read helpers derive a project key
    via ``_sanitize_path(realpath(cwd))`` that DIVERGES from it (Windows drives,
    ``/workspace`` vs worktree paths, symlinks). SDK session UUIDs are globally
    unique (a live DB check confirmed 0 sessions span multiple project_keys),
    so matching on ``session_id`` alone is both correct and the only way to make
    resume work regardless of which (possibly-wrong) project_key the caller
    reconstructs. This is the same strategy the drift verifier uses at 0 drift.
    """
    with get_db() as conn:
        cur = conn.cursor()
        if subpath is None:
            cur.execute(
                "SELECT entry FROM session_store_entries "
                "WHERE session_id = %s AND subpath IS NULL "
                "ORDER BY id ASC",
                (session_id,),
            )
        else:
            cur.execute(
                "SELECT entry FROM session_store_entries "
                "WHERE session_id = %s AND subpath = %s "
                "ORDER BY id ASC",
                (session_id, subpath),
            )
        rows = cur.fetchall()

    # psycopg2 JSONB returns dicts already; SDK contract is deep-equal not
    # byte-equal so no round-trip normalization is required.
    return [row["entry"] for row in rows]


def _store_version_by_session_id_sync(session_id: str, _conn=None) -> tuple[int, int]:
    """Return a cheap ``(count, max_id)`` version stamp for ``session_id``'s main
    transcript (``subpath IS NULL``), matched by ``session_id`` ALONE.

    One indexed row. Used to validate ``_store_parse_cache`` entries on read:
    the append-only ``session_store_entries`` log means the pair
    ``(COUNT(*), MAX(id))`` changes on ANY insert (count grows, max_id grows) and
    on ANY delete such as a rewind (count shrinks). A rewind on one install
    DELETEs shared rows; other installs holding a cache stamped with the
    pre-rewind ``(count, max_id)`` will see the mismatch and drop the stale entry
    instead of serving the pre-rewind transcript verbatim (the cross-install
    "rewind still broken" symptom).

    ``max_id`` is 0 when the session has no rows (``MAX(id)`` is NULL). Matched by
    ``session_id`` alone for the same reason as
    :func:`_load_entries_by_session_id_sync` — the stored ``project_key`` diverges
    from any cwd-derived key.

    ``_conn``: optional open connection (the app's ``transaction()`` idiom) so
    the lazy backfill's in-lock re-check runs on the SAME connection that holds
    the advisory xact lock.
    """
    with (nullcontext(_conn) if _conn is not None else get_db()) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n, MAX(id) AS max_id FROM session_store_entries "
            "WHERE session_id = %s AND subpath IS NULL",
            (session_id,),
        )
        row = cur.fetchone()
    if not row:
        return (0, 0)
    return (int(row["n"]), int(row["max_id"]) if row["max_id"] is not None else 0)


def _has_summary_by_session_id_sync(session_id: str) -> bool:
    """True when a ``session_store_summaries`` row exists for ``session_id``.

    The summary row is created on the FIRST store append/backfill and survives
    rewind truncation (which deletes only entries), so it acts as a tombstone:
    "this session WAS store-recorded". The lazy backfill uses it to distinguish
    a never-backfilled pre-cutover session (no row → safe to backfill) from a
    session whose transcript was deliberately emptied, e.g. rewound to the
    start (row present → a stale on-disk JSONL must NOT resurrect it).
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 AS present FROM session_store_summaries "
            "WHERE session_id = %s LIMIT 1",
            (session_id,),
        )
        return cur.fetchone() is not None


# A main JSONL modified more recently than this is treated as live: during a
# session's first turn the CLI is still writing the transcript locally while
# the eager store flush is in flight, and backfilling that window would insert
# the same entries the flush is about to append (no unique constraint exists
# to dedupe them). Pre-cutover transcripts are days old, so the window costs
# nothing real.
_LAZY_BACKFILL_MIN_JSONL_AGE_S = 60.0


def _lazy_backfill_from_jsonl_sync(
    sdk_session_id: str, working_dir: str = "/workspace"
) -> int:
    """Backfill a session's store transcript from its local JSONL on a miss.

    The postgres_only cutover assumed ``scripts/backfill_session_store.py`` had
    populated the store first. Where it wasn't run, every pre-cutover session
    has ZERO store rows, so reads render empty and SDK resume seeds a blank
    context. This self-heals the store one session at a time, at read/resume
    time, from the same JSONL the bulk script would have used.

    Guards (cheapest first — this sits on read paths):
      1. any main-transcript store rows → already recorded, skip;
      2. a summaries row → session WAS store-recorded (tombstone; e.g. rewound
         to empty) — do not resurrect from a stale JSONL;
      3. no local JSONL / no entries → nothing to heal (the caller's legacy-DB
         read fallback covers cross-install sessions);
      4. JSONL modified < ``_LAZY_BACKFILL_MIN_JSONL_AGE_S`` ago → live first
         turn, skip (see the constant);
      5. a ``pg_advisory_xact_lock`` on ``hashtext(session_id)`` plus a
         SESSION-WIDE in-lock count re-check serializes concurrent
         backfillers — the table has NO unique constraint, so exclusion is
         the only dedupe (and installs derive different project_keys, so the
         re-check must ignore the key).

    Returns rows written (0 = nothing to do). Raises on DB errors — use
    :func:`lazy_backfill_session_from_jsonl` from read paths.
    """
    count, _ = _store_version_by_session_id_sync(sdk_session_id)
    if count:
        return 0
    if _has_summary_by_session_id_sync(sdk_session_id):
        return 0

    from app.core.jsonl_parser import (
        get_agent_jsonl_paths,
        get_session_jsonl_path,
        parse_jsonl_file,
    )

    jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
    if jsonl_path is None:
        return 0
    if time.time() - jsonl_path.stat().st_mtime < _LAZY_BACKFILL_MIN_JSONL_AGE_S:
        return 0
    entries = list(parse_jsonl_file(jsonl_path))
    if not entries:
        return 0

    # Same project_key convention as the bulk backfill script: the on-disk
    # folder name (NOT a cwd-derived key, which diverges — see
    # _load_entries_by_session_id_sync).
    project_key = jsonl_path.parent.name
    main_key: SessionKey = {"project_key": project_key, "session_id": sdk_session_id}

    written = 0
    with transaction() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT pg_advisory_xact_lock(hashtext(%s))", (sdk_session_id,)
        )
        # The in-lock re-check must be SESSION-WIDE (session_id alone, like
        # the pre-lock guard), not keyed on this install's project_key: two
        # installs derive different project_keys for the same session, so a
        # per-key recheck would let the second backfiller insert the whole
        # transcript again under its own key — and reads merge by session_id,
        # serving a doubled history.
        in_lock_count, _ = _store_version_by_session_id_sync(
            sdk_session_id, _conn=conn
        )
        if in_lock_count == 0:
            written += _backfill_entries_sync(main_key, entries, _conn=conn)
            for agent_id, agent_path in get_agent_jsonl_paths(
                sdk_session_id, working_dir
            ).items():
                sub_entries = list(parse_jsonl_file(agent_path))
                if not sub_entries:
                    continue
                sub_key: SessionKey = {
                    "project_key": project_key,
                    "session_id": sdk_session_id,
                    "subpath": f"subagents/agent-{agent_id}",
                }
                if _count_entries_sync(sub_key, _conn=conn) == 0:
                    written += _backfill_entries_sync(
                        sub_key, sub_entries, _conn=conn
                    )
    return written


def lazy_backfill_session_from_jsonl(
    sdk_session_id: str, working_dir: str = "/workspace"
) -> int:
    """Read-path wrapper around :func:`_lazy_backfill_from_jsonl_sync`.

    Never raises: a failed heal must degrade to "store still empty" (the
    caller then falls back to the legacy DB or serves what it has), not break
    the read. Returns rows written.
    """
    try:
        written = _lazy_backfill_from_jsonl_sync(sdk_session_id, working_dir)
    except Exception:
        logger.exception(
            "Lazy store backfill failed for session %s", sdk_session_id
        )
        return 0
    if written:
        logger.info(
            "Lazily backfilled %d store entries for session %s from local JSONL",
            written,
            sdk_session_id,
        )
    return written


def _list_subpaths_by_session_id_sync(session_id: str) -> list[str]:
    """Distinct non-null subpaths for ``session_id`` (matched by id alone).

    Like :func:`_load_entries_by_session_id_sync`, this drops the ``project_key``
    predicate — see that function for the rationale.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT subpath FROM session_store_entries "
            "WHERE session_id = %s AND subpath IS NOT NULL",
            (session_id,),
        )
        rows = cur.fetchall()
    return [r["subpath"] for r in rows]


def _load_sync(key: SessionKey) -> list[SessionStoreEntry] | None:
    # Match on (session_id, subpath) ONLY — the project_key the SDK passes in
    # via load(key) is derived from cwd and DIVERGES from the on-disk folder
    # name stored at write time (Windows, /workspace, worktrees, symlinks).
    # SDK session UUIDs are globally unique, so this is correct and makes resume
    # robust. See _load_entries_by_session_id_sync for the full rationale.
    session_id = key["session_id"]
    subpath = _subpath_of(key)

    rows = _load_entries_by_session_id_sync(session_id, subpath)
    if not rows and subpath is None:
        # Main-transcript miss: almost always a pre-cutover session whose JSONL
        # was never bulk-backfilled. Returning None here would seed the SDK
        # resume with a BLANK context — heal from the local JSONL first (guarded
        # inside; a no-op for genuinely new sessions).
        if lazy_backfill_session_from_jsonl(session_id):
            rows = _load_entries_by_session_id_sync(session_id, subpath)
    if not rows:
        return None
    return rows


def _list_sessions_sync(project_key: str) -> list[SessionStoreListEntry]:
    """Return main-transcript session IDs with storage-time mtime (epoch ms).

    ``mtime`` is derived from MAX(created_at) of the entries table, converted
    to epoch milliseconds. This shares a clock with the summary sidecar's
    ``mtime_ms`` (both are Postgres-side NOW() timestamps) so the fast-path
    staleness check in ``list_sessions_from_store`` sees sidecars as fresh.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT session_id, "
            "       (EXTRACT(EPOCH FROM MAX(created_at)) * 1000)::BIGINT AS mtime "
            "FROM session_store_entries "
            "WHERE project_key = %s AND subpath IS NULL "
            "GROUP BY session_id",
            (project_key,),
        )
        rows = cur.fetchall()
    return [{"session_id": r["session_id"], "mtime": int(r["mtime"])} for r in rows]


def _list_session_summaries_sync(project_key: str) -> list[SessionSummaryEntry]:
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT session_id, data, mtime_ms "
            "FROM session_store_summaries "
            "WHERE project_key = %s",
            (project_key,),
        )
        rows = cur.fetchall()
    return [
        {
            "session_id": r["session_id"],
            "mtime": int(r["mtime_ms"]),
            "data": dict(r["data"]) if r["data"] else {},
        }
        for r in rows
    ]


def _delete_sync(key: SessionKey) -> None:
    """Delete one session or subkey.

    Main-key delete (no subpath) cascades to every subpath + the summary
    sidecar. Subpath delete removes only that one transcript.
    """
    project_key = key["project_key"]
    session_id = key["session_id"]
    subpath = _subpath_of(key)

    with get_db() as conn:
        cur = conn.cursor()
        if subpath is None:
            cur.execute(
                "DELETE FROM session_store_entries "
                "WHERE project_key = %s AND session_id = %s",
                (project_key, session_id),
            )
            cur.execute(
                "DELETE FROM session_store_summaries "
                "WHERE project_key = %s AND session_id = %s",
                (project_key, session_id),
            )
        else:
            cur.execute(
                "DELETE FROM session_store_entries "
                "WHERE project_key = %s AND session_id = %s AND subpath = %s",
                (project_key, session_id, subpath),
            )


def _delete_session_data_by_session_id_sync(session_id: str) -> int:
    """Delete EVERY store row for ``session_id``: all transcript entries (main
    + every subagent subpath) and the summary sidecar, matched by
    ``session_id`` ALONE — the stored ``project_key`` is the on-disk folder
    name and cannot be reconstructed by the app (see
    :func:`_load_entries_by_session_id_sync`), so the SDK-key
    :func:`_delete_sync` is unusable here. Returns rows removed.

    Used by the session-delete endpoint's background cleanup: ``sessions`` has
    NO foreign key into the store tables, so without this every deleted
    session leaked its full transcript forever (Bug E).
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM session_store_entries WHERE session_id = %s",
            (session_id,),
        )
        removed = cur.rowcount or 0
        cur.execute(
            "DELETE FROM session_store_summaries WHERE session_id = %s",
            (session_id,),
        )
        removed += cur.rowcount or 0
    return removed


async def delete_session_data_by_session_id(session_id: str) -> int:
    """Async wrapper around :func:`_delete_session_data_by_session_id_sync`."""
    return await asyncio.to_thread(
        _delete_session_data_by_session_id_sync, session_id
    )


def _list_subkeys_sync(key: SessionListSubkeysKey) -> list[str]:
    # Match on session_id ONLY (drop the project_key predicate) so the SDK's
    # resume subkey materialization works regardless of the cwd-derived
    # project_key it passes in. See _load_entries_by_session_id_sync.
    return _list_subpaths_by_session_id_sync(key["session_id"])


class RewindTargetNotFound(Exception):
    """Raised when a rewind target uuid is absent from the session store."""


def _truncate_main_transcript_to_keep_count_sync(
    session_id: str, keep_count: int
) -> int:
    """Delete main-transcript (``subpath IS NULL``) rows beyond the first
    ``keep_count``, ordered by ``id ASC``. Returns the number of rows deleted.

    ``keep_count <= 0`` removes every main-transcript row. The boundary is
    resolved by ``id`` (the per-session order column) so deletion is exact even
    though ``id`` values are not contiguous. Subagent transcripts
    (``subpath IS NOT NULL``) are intentionally left untouched: a truncated turn
    no longer references them, so they are inert, and mapping each subagent
    transcript back to the turn that spawned it is not worth the risk here.
    """
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id FROM session_store_entries "
            "WHERE session_id = %s AND subpath IS NULL ORDER BY id ASC",
            (session_id,),
        )
        ids = [row["id"] for row in cur.fetchall()]
        total = len(ids)
        if keep_count >= total:
            return 0
        if keep_count <= 0:
            cur.execute(
                "DELETE FROM session_store_entries "
                "WHERE session_id = %s AND subpath IS NULL",
                (session_id,),
            )
            return cur.rowcount or 0
        boundary_id = ids[keep_count - 1]
        cur.execute(
            "DELETE FROM session_store_entries "
            "WHERE session_id = %s AND subpath IS NULL AND id > %s",
            (session_id, boundary_id),
        )
        return cur.rowcount or 0


def _truncate_session_store_to_checkpoint_sync(
    sdk_session_id: str,
    target_uuid: str,
    include_response: bool,
    target_role: str,
) -> int:
    """Truncate the stored main transcript at a rewind checkpoint.

    Loads the session's main-transcript entries, resolves the keep-count with
    the shared :func:`app.core.jsonl_rewind.compute_rewind_keep_count` helper
    (so the store cuts at the exact same point as the JSONL), and deletes the
    rows after it. Returns the number of rows removed.

    Raises :class:`RewindTargetNotFound` when there are no entries for the
    session or ``target_uuid`` is not present.
    """
    entries = _load_entries_by_session_id_sync(sdk_session_id, None)
    if not entries:
        raise RewindTargetNotFound(
            f"No session store entries for session {sdk_session_id}"
        )

    # Lazy import keeps the module import graph one-directional (jsonl_rewind
    # already imports app.core.* freely; importing it at module top here would
    # risk a future cycle).
    from app.core.jsonl_rewind import compute_rewind_keep_count

    keep_count, slice_error = compute_rewind_keep_count(
        entries, target_uuid, include_response, target_role
    )
    if slice_error is not None or keep_count is None:
        raise RewindTargetNotFound(slice_error or "Rewind target could not be resolved")

    return _truncate_main_transcript_to_keep_count_sync(sdk_session_id, keep_count)


async def truncate_session_store_to_checkpoint(
    sdk_session_id: str,
    target_uuid: str,
    include_response: bool,
    target_role: str,
) -> int:
    """Async wrapper around :func:`_truncate_session_store_to_checkpoint_sync`.

    Used by the rewind endpoint to make the Postgres session store — the source
    of truth the UI reads in ``postgres_only`` mode — actually reflect the
    rewind. Without this the store keeps the truncated messages and they
    reappear on the next reload.
    """
    return await asyncio.to_thread(
        _truncate_session_store_to_checkpoint_sync,
        sdk_session_id,
        target_uuid,
        include_response,
        target_role,
    )


async def main_transcript_is_empty(session_id: str) -> bool:
    """True when ``session_id`` has no main-transcript rows in the store.

    Used by the rewind endpoint to detect a full rewind (store truncated to
    nothing): with the store empty, the read path falls back to legacy
    ``session_messages`` rows, so any leftovers there must be purged too.
    Reuses the indexed ``(count, max_id)`` version query.
    """
    count, _ = await asyncio.to_thread(_store_version_by_session_id_sync, session_id)
    return count == 0


# ---------------------------------------------------------------------------
# Async SessionStore adapter (duck-typed; no Protocol subclass needed).
# ---------------------------------------------------------------------------


class PgSessionStore:
    """Postgres-backed :class:`claude_agent_sdk.SessionStore` implementation.

    See module docstring for the full design. Implements all four optional
    methods (``list_sessions``, ``list_session_summaries``, ``delete``,
    ``list_subkeys``) so the SDK can use the fast-path resume + summary APIs
    without falling back to per-session ``load()``.

    All methods are coroutines wrapping synchronous psycopg2 work via
    ``asyncio.to_thread``. Exceptions raised by ``append`` are logged but
    re-raised per the Protocol contract so the SDK's own error handling
    (swallow + continue subprocess) applies.
    """

    async def append(
        self, key: SessionKey, entries: list[SessionStoreEntry]
    ) -> None:
        await asyncio.to_thread(_append_sync, key, entries)

    async def load(self, key: SessionKey) -> list[SessionStoreEntry] | None:
        return await asyncio.to_thread(_load_sync, key)

    async def list_sessions(self, project_key: str) -> list[SessionStoreListEntry]:
        return await asyncio.to_thread(_list_sessions_sync, project_key)

    async def list_session_summaries(
        self, project_key: str
    ) -> list[SessionSummaryEntry]:
        return await asyncio.to_thread(_list_session_summaries_sync, project_key)

    async def delete(self, key: SessionKey) -> None:
        await asyncio.to_thread(_delete_sync, key)

    async def list_subkeys(self, key: SessionListSubkeysKey) -> list[str]:
        return await asyncio.to_thread(_list_subkeys_sync, key)

    # ------------------------------------------------------------------
    # App-facing read API (PR C.1 — postgres_only read cutover).
    #
    # NOT part of the SDK SessionStore protocol. The backend has a
    # ``session_id`` (the SDK session UUID), not an SDK key dict, and must NOT
    # reconstruct a project_key from cwd (it diverges from the on-disk folder
    # name stored at write time). These methods match on ``session_id`` (+
    # ``subpath``) alone so reads are correct regardless of cwd encoding.
    # ------------------------------------------------------------------

    async def load_entries_by_session_id(
        self, session_id: str, subpath: str | None = None
    ) -> list[dict]:
        """Return raw ``entry`` JSONB dicts for ``session_id`` (+``subpath``).

        Ordered by ``id ASC``. ``subpath IS NULL`` when ``subpath is None``;
        otherwise an exact subpath match. Matched by ``session_id`` alone (no
        project_key predicate) — see :func:`_load_entries_by_session_id_sync`.
        """
        return await asyncio.to_thread(
            _load_entries_by_session_id_sync, session_id, subpath
        )

    async def list_subpaths_by_session_id(self, session_id: str) -> list[str]:
        """Distinct non-null subpaths for ``session_id`` (subagent transcripts).

        Matched by ``session_id`` alone — see
        :func:`_list_subpaths_by_session_id_sync`.
        """
        return await asyncio.to_thread(
            _list_subpaths_by_session_id_sync, session_id
        )

    # ------------------------------------------------------------------
    # Backfill API (operational; used by scripts.backfill_session_store).
    #
    # NOT part of the SDK SessionStore protocol. The live mirror appends
    # incrementally; these methods bulk-load a complete on-disk transcript
    # into the SAME tables, in the SAME row shape, so backfilled history is
    # indistinguishable from mirrored history to readers and the verifier.
    # ------------------------------------------------------------------

    async def count_entries(self, key: SessionKey) -> int:
        """Rows already present for ``key`` (used for the idempotency check)."""
        return await asyncio.to_thread(_count_entries_sync, key)

    async def backfill(
        self,
        key: SessionKey,
        entries: list[SessionStoreEntry],
        *,
        skip_if_present: bool = True,
        batch_size: int = 500,
    ) -> int:
        """Bulk-insert a full transcript for ``key``, idempotently.

        Idempotency: ``session_store_entries`` has no unique constraint
        (BIGSERIAL PK only), so there is no ``ON CONFLICT`` target. When
        ``skip_if_present`` is True (default) this first counts existing rows
        for the exact (project_key, session_id, subpath) and returns ``0``
        without writing if any exist — the on-disk JSONL is the complete,
        append-only history, so partial overlap is not expected. Pass
        ``skip_if_present=False`` only when you have already truncated the
        target rows yourself.

        Returns the number of rows written (``0`` when skipped or empty).
        """
        if not entries:
            return 0
        if skip_if_present:
            existing = await asyncio.to_thread(_count_entries_sync, key)
            if existing > 0:
                return 0
        return await asyncio.to_thread(
            _backfill_entries_sync, key, entries, batch_size
        )


__all__ = [
    "PgSessionStore",
    "RewindTargetNotFound",
    "truncate_session_store_to_checkpoint",
]
