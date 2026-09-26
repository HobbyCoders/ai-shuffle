"""
Postgres tables for Claude-format transcript entries and their folded summaries.

:class:`PgSessionStore` keeps the ``SessionStore`` protocol shape of the retired
Claude Agent SDK. ``query_options`` still attaches it as
``AgentOptions.session_store``, but ``HarnessAdapter`` ignores that option, so
:meth:`PgSessionStore.append` has no live caller. Rows are written by the lazy
JSONL backfill (:func:`lazy_backfill_session_from_jsonl`, called on a store
miss from the history read path), and read for session history, export,
rewind and deletion. Native fluid sessions persist separately
(``app.core.provider.fluid_pg_store``).

Tables
------
``session_store_entries``
    Append-only log of transcript entries. ``subpath IS NULL`` means main
    transcript; non-NULL is a subagent transcript (e.g. ``"subagents/agent-1"``).
    Per-session order is by the ``BIGSERIAL`` ``id`` column, which preserves
    append-call order within a single process and storage commit order across
    concurrent processes — exactly what the ``SessionStore`` contract requires.

``session_store_summaries``
    Incrementally-folded ``SessionSummaryEntry`` sidecar, one row per
    (project_key, session_id), plus a ``folded_through_id`` watermark naming
    the highest ``session_store_entries.id`` already folded into ``data``.
    Updated by :func:`_fold_summary_sync` under a per-session
    ``SELECT ... FOR UPDATE``.

    The fold is COALESCED: it runs at most once per
    ``_SUMMARY_FOLD_INTERVAL_S`` inside the append path, and on demand from the
    two readers that actually consume summaries (``list_sessions`` /
    ``list_session_summaries``) and from :func:`flush_summary_folds`.

    Correctness does not depend on the in-process timer: the summary is a pure
    fold of the entries and the watermark lives in the DATABASE, so whatever a
    crash (or a rollback, or another install) left unfolded is folded by the
    next append or read from exactly where the last one stopped. The in-memory
    state is a rate limiter, nothing more.

Blocking I/O
------------
The :class:`PgSessionStore` methods are coroutines. psycopg2 is synchronous,
so every DB operation is dispatched to a worker thread via
``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from contextlib import nullcontext
from typing import Any

from app.core.cli_sessions import (
    SessionKey,
    SessionListSubkeysKey,
    SessionStoreEntry,
    SessionStoreListEntry,
    SessionSummaryEntry,
)
from app.core.cli_sessions import fold_session_summary
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
# aborts the whole transaction. Transcript entries occasionally contain
# these — e.g. a tool result that echoed a binary or UTF-16-encoded file — and
# ``psycopg2.extras.Json`` serializes them verbatim, so the INSERT would fail.
# JSON itself permits these code points, so the on-disk JSONL transcript is
# unaffected; we sanitize only at the Postgres boundary. uuids (and every other
# id field) never contain these code points, so uuid lookups are unaffected.
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


# ---------------------------------------------------------------------------
# Coalesced summary fold
# ---------------------------------------------------------------------------

#: Minimum wall-clock gap between two summary folds for the same session inside
#: the append path. A streaming mirror appends many times a second, and folding
#: on each append would rewrite the whole summary JSONB row every time. Readers
#: flush on demand, so the delay is never visible to them.
_SUMMARY_FOLD_INTERVAL_S = 2.0

#: session_id -> {"project_key": str, "last_fold": monotonic, "pending": bool}.
#: Purely an in-process rate limiter + "who owes a fold" list: the authoritative
#: watermark is ``session_store_summaries.folded_through_id`` in the database,
#: so losing this dict (crash, restart, another install) costs nothing but a
#: slightly later fold.
_fold_state: dict[str, dict[str, Any]] = {}
_fold_state_lock = threading.Lock()

#: Bound on the rate-limiter dict. A long-lived process would otherwise keep one
#: small entry per session it has ever mirrored; above this many entries the
#: settled (non-pending) ones older than the interval are dropped, which costs
#: nothing but an extra first fold if such a session streams again.
_FOLD_STATE_SOFT_CAP = 512


def _forget_fold_state(session_id: str) -> None:
    """Drop a session's fold bookkeeping (called when its store rows go away).

    Without this, a pending fold recorded before a delete/truncate would be
    replayed by the next flush against a row that no longer exists. The fold
    itself refuses to recreate a missing summary row, so this is belt and
    braces — but it also stops the flush list growing without bound.
    """
    with _fold_state_lock:
        _fold_state.pop(session_id, None)


def _reset_fold_state() -> None:
    """Test helper — drop every session's fold bookkeeping."""
    with _fold_state_lock:
        _fold_state.clear()
        _last_repair.clear()


def _claim_fold(session_id: str, project_key: str, now: float) -> bool:
    """Decide whether THIS append should fold, marking the session pending if not.

    Returns True when the caller should fold now (first append for the session
    in this process, or the interval has elapsed). The claim is recorded
    optimistically: if the surrounding transaction later rolls back, the DB
    watermark is unchanged and the next fold simply picks the same entries up
    again, so the only cost of a wrong guess is a slightly later fold.
    """
    with _fold_state_lock:
        if len(_fold_state) > _FOLD_STATE_SOFT_CAP:
            for sid in [
                sid
                for sid, st in _fold_state.items()
                if not st["pending"]
                and now - st["last_fold"] > _SUMMARY_FOLD_INTERVAL_S
                and sid != session_id
            ]:
                _fold_state.pop(sid, None)
        state = _fold_state.get(session_id)
        if state is None:
            _fold_state[session_id] = {
                "project_key": project_key,
                "last_fold": now,
                "pending": False,
            }
            return True
        state["project_key"] = project_key
        if now - state["last_fold"] >= _SUMMARY_FOLD_INTERVAL_S:
            state["last_fold"] = now
            state["pending"] = False
            return True
        state["pending"] = True
        return False


def _fold_summary_sync(cur, project_key: str, session_id: str) -> bool:
    """Fold every not-yet-folded main-transcript entry into the summary sidecar.

    Reads the row ``FOR UPDATE`` (serializing concurrent folders exactly as the
    old per-append fold did), then folds the entries with
    ``id > folded_through_id`` and advances the watermark to the last one.

    Deliberately does NOT create the summary row: the row is a tombstone meaning
    "this session WAS store-recorded" (see :func:`_has_summary_by_session_id_sync`)
    and recreating it here would resurrect a session that was just deleted or
    rewound to empty. The append path creates it before folding.

    Entries are matched by ``session_id`` alone, like every other read in this
    module — the stored ``project_key`` is the on-disk folder name and diverges
    from any cwd-derived key. Returns True when the summary was rewritten.
    """
    cur.execute(
        "SELECT data, mtime_ms, folded_through_id FROM session_store_summaries "
        "WHERE project_key = %s AND session_id = %s FOR UPDATE",
        (project_key, session_id),
    )
    row = cur.fetchone()
    if row is None:
        return False

    prev_data = row["data"] if row["data"] else {}
    prev_mtime = row["mtime_ms"] or 0
    watermark = int(row["folded_through_id"] or 0)

    cur.execute(
        "SELECT id, entry FROM session_store_entries "
        "WHERE session_id = %s AND subpath IS NULL AND id > %s "
        "ORDER BY id ASC",
        (session_id, watermark),
    )
    rows = cur.fetchall() or []
    if not rows:
        return False

    entries = [r["entry"] for r in rows]
    last_id = int(rows[-1]["id"])

    # A first fold (row just created, nothing folded yet) passes prev=None so
    # the fold seeds created_at / is_sidechain from the first entry.
    if prev_data or prev_mtime:
        prev_summary: SessionSummaryEntry | None = {
            "session_id": session_id,
            "mtime": int(prev_mtime),
            "data": dict(prev_data),
        }
    else:
        prev_summary = None

    key: SessionKey = {"project_key": project_key, "session_id": session_id}
    folded = fold_session_summary(prev_summary, key, entries)

    # mtime_ms is generated Postgres-side (same formula as _list_sessions_sync's
    # MAX(created_at)) so the SDK's fast-path staleness check sees a single
    # shared clock — otherwise app↔DB drift breaks list_sessions_from_store.
    cur.execute(
        "UPDATE session_store_summaries "
        "SET data = %s, "
        "    mtime_ms = (EXTRACT(EPOCH FROM NOW()) * 1000)::BIGINT, "
        "    folded_through_id = %s "
        "WHERE project_key = %s AND session_id = %s",
        (
            Json(_scrub_pg_illegal(folded["data"])),
            last_id,
            project_key,
            session_id,
        ),
    )
    return True


def flush_summary_folds(session_id: str | None = None) -> int:
    """Fold every summary this process still owes, now.

    Call this wherever a reader needs an up-to-date sidecar (the two list
    readers below do) or at turn end. Never raises: a failed flush leaves the
    work pending, and the DB watermark means the next attempt picks up exactly
    where this one stopped. Returns the number of summaries rewritten.
    """
    with _fold_state_lock:
        if session_id is not None:
            state = _fold_state.get(session_id)
            targets = (
                [(session_id, state["project_key"])]
                if state is not None and state["pending"]
                else []
            )
        else:
            targets = [
                (sid, st["project_key"])
                for sid, st in _fold_state.items()
                if st["pending"]
            ]
        now = time.monotonic()
        for sid, _pk in targets:
            st = _fold_state.get(sid)
            if st is not None:
                st["pending"] = False
                st["last_fold"] = now

    folded = 0
    for sid, pk in targets:
        try:
            with get_db() as conn:
                if _fold_summary_sync(conn.cursor(), pk, sid):
                    folded += 1
        except Exception:
            # Re-arm so the next flush retries; the watermark makes the retry
            # idempotent.
            with _fold_state_lock:
                st = _fold_state.get(sid)
                if st is not None:
                    st["pending"] = True
            logger.warning(
                "Deferred session-summary fold failed for %s", sid, exc_info=True
            )
    return folded


#: How often the crash-recovery sweep may run per project_key. The sweep probes
#: the entries table once per summary row, so it must not ride on every read —
#: but it MUST run the first time a process reads a project (that is the
#: post-restart case it exists for), which a monotonic "never run yet" naturally
#: gives.
_SUMMARY_REPAIR_INTERVAL_S = 300.0
_last_repair: dict[str, float] = {}


def _claim_repair(project_key: str, now: float) -> bool:
    with _fold_state_lock:
        last = _last_repair.get(project_key)
        if last is not None and now - last < _SUMMARY_REPAIR_INTERVAL_S:
            return False
        _last_repair[project_key] = now
        return True


def _repair_stale_summaries_sync(project_key: str, limit: int = 100) -> int:
    """Fold summaries left behind by a process that died mid-turn.

    The in-process pending list does not survive a restart, so a summary whose
    session is never appended to again could otherwise stay at its last
    watermark forever. This finds rows in ``project_key`` that have entries
    beyond ``folded_through_id`` and folds them — the crash-recovery half of
    the coalesced fold (the summary is a pure function of the entries, so
    nothing is lost, only late). Bounded by ``limit`` so a first run against a
    large project cannot turn one read into an unbounded repair loop, and
    throttled per project so a steady-state reader does not pay for it.
    """
    if not _claim_repair(project_key, time.monotonic()):
        return 0
    repaired = 0
    try:
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT s.session_id FROM session_store_summaries s "
                "WHERE s.project_key = %s AND EXISTS ("
                "    SELECT 1 FROM session_store_entries e "
                "    WHERE e.session_id = s.session_id AND e.subpath IS NULL "
                "    AND e.id > s.folded_through_id) "
                "LIMIT %s",
                (project_key, limit),
            )
            stale = [r["session_id"] for r in (cur.fetchall() or [])]
            for sid in stale:
                if _fold_summary_sync(cur, project_key, sid):
                    repaired += 1
    except Exception:
        logger.warning(
            "Stale session-summary repair failed for project %s",
            project_key,
            exc_info=True,
        )
    return repaired


def _append_sync(key: SessionKey, entries: list[SessionStoreEntry]) -> None:
    """Insert entries and (for main transcripts) keep the summary sidecar fresh.

    The insert always happens. The summary fold is coalesced to at most one per
    ``_SUMMARY_FOLD_INTERVAL_S`` per session — see the module
    docstring for why that is safe — and is serialized per
    (project_key, session_id) via ``SELECT ... FOR UPDATE`` on the summaries
    row, which is created on first append (INSERT ... ON CONFLICT DO NOTHING)
    so the lock target always exists.
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

        should_fold = _claim_fold(session_id, project_key, time.monotonic())

        # Ensure the summary row exists. Cheap (one ON CONFLICT DO NOTHING) and
        # kept on EVERY append rather than only on the folding ones: the row
        # doubles as the "this session was store-recorded" tombstone the lazy
        # JSONL backfill checks, so it must not wait for the first fold.
        cur.execute(
            "INSERT INTO session_store_summaries "
            "(project_key, session_id, data, mtime_ms, folded_through_id) "
            "VALUES (%s, %s, %s, 0, 0) "
            "ON CONFLICT (project_key, session_id) DO NOTHING",
            (project_key, session_id, Json({})),
        )

        if should_fold:
            _fold_summary_sync(cur, project_key, session_id)


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
    rows are shape-identical to appended rows.

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
        # mirroring _append_sync's create-then-fold flow. The shared fold reads
        # the rows back from this same transaction (id > folded_through_id, so
        # exactly what was just inserted), seeds created_at / is_sidechain from
        # the first entry, and stamps mtime_ms Postgres-side with the same
        # NOW()-epoch-ms formula as live appends.
        cur.execute(
            "INSERT INTO session_store_summaries "
            "(project_key, session_id, data, mtime_ms, folded_through_id) "
            "VALUES (%s, %s, %s, 0, 0) "
            "ON CONFLICT (project_key, session_id) DO NOTHING",
            (project_key, session_id, Json({})),
        )
        _fold_summary_sync(cur, project_key, session_id)

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
    reconstructs.
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


# ---------------------------------------------------------------------------
# Windowed transcript reads (store-side pagination)
# ---------------------------------------------------------------------------

#: Entries loaded on EACH side of a requested window purely so that entries
#: which merge across the boundary still merge the way a full parse merges
#: them. A ``tool_result`` entry attaches itself to the ``tool_use`` message of
#: an EARLIER entry; without a prefix of context the transform cannot find that
#: message and emits a free-standing ``tool_result`` card the full parse never
#: produces. The mirror case (a ``tool_use`` at the newest edge of an OLDER page
#: whose result lands in the next page) is why the context is symmetric.
#:
#: Two hundred entries is far more than the 1–3 that separate a tool_use from
#: its result in practice, and it is a constant — it does not grow with the
#: transcript, which is the entire point of windowing.
_WINDOW_MERGE_CONTEXT_ENTRIES = 200


def _count_entries_by_session_id_sync(
    session_id: str, subpath: str | None = None, _conn=None
) -> tuple[int, int]:
    """``(count, max_id)`` for ``session_id`` (+``subpath``), matched by id alone.

    The ``subpath IS NULL`` case is what :func:`_store_version_by_session_id_sync`
    computes; this generalises it to subagent transcripts and takes an optional
    open connection so a windowed read can count and page inside ONE connection.
    ``max_id`` is 0 for an empty transcript.
    """
    with (nullcontext(_conn) if _conn is not None else get_db()) as conn:
        cur = conn.cursor()
        if subpath is None:
            cur.execute(
                "SELECT COUNT(*) AS n, MAX(id) AS max_id FROM session_store_entries "
                "WHERE session_id = %s AND subpath IS NULL",
                (session_id,),
            )
        else:
            cur.execute(
                "SELECT COUNT(*) AS n, MAX(id) AS max_id FROM session_store_entries "
                "WHERE session_id = %s AND subpath = %s",
                (session_id, subpath),
            )
        row = cur.fetchone()
    if not row:
        return (0, 0)
    return (int(row["n"]), int(row["max_id"]) if row["max_id"] is not None else 0)


def _load_entries_window_by_session_id_sync(
    session_id: str,
    subpath: str | None = None,
    *,
    limit: int,
    before_seq: int | None = None,
    context: int | None = None,
) -> dict:
    """Read ONE window of a transcript instead of the whole thing.

    This is the store half of transcript pagination. Until it existed, every
    session open read and transformed the ENTIRE transcript and the API layer
    then sliced a window off the end — so a long session paid a full table read,
    a full parse and a full transform to display its newest 2000 messages.

    The window is expressed in ENTRY SEQUENCE space: ``seq`` is a row's 0-based
    position within this session's ``(session_id, subpath)`` log ordered by
    ``id ASC``. The log is append-only, so a position is a stable cursor — the
    same property the API's message-space cursor relies on.

    Newest-window-first, walking backwards: with ``before_seq=None`` the NEWEST
    ``limit`` entries are returned (what opening a session wants), and the
    returned ``window_start`` is the cursor for the next, older page.

    Args:
        session_id: SDK session UUID. Matched ALONE — no ``project_key``
            predicate, for the reason spelled out in
            :func:`_load_entries_by_session_id_sync`.
        subpath: ``None`` for the main transcript, else an exact subpath.
        limit: Maximum entries in the window (>= 1).
        before_seq: Keyset cursor — return the window ENDING before this entry
            seq. ``None`` means "from the newest end". Clamped into range, so a
            stale cursor from before a rewind degrades to a valid window rather
            than to an error.
        context: Entries to load either side of the window for boundary-correct
            merging (default :data:`_WINDOW_MERGE_CONTEXT_ENTRIES`). These are
            RETURNED — the parse layer transforms them and then drops the
            messages they produced; it needs them to reproduce the merges a full
            parse performs.

    Returns:
        A dict with ``entries`` (raw ``entry`` dicts, id ASC, INCLUDING the
        context on both sides), ``total`` (entries in the whole transcript),
        ``start`` (seq of ``entries[0]``), ``window_start`` and ``window_end``
        (the half-open seq range of the window proper).

    Consistency: the count and the page run on ONE connection, and the page is
    pinned with ``id <= max_id`` from the count. A concurrent append therefore
    cannot shift the offset out from under the read and silently drop the newest
    entry — the classic symptom of paginating an append-only log with a bare
    ``OFFSET``. A concurrent REWIND (a DELETE) can still shift it; that already
    invalidates every cached parse of this session, and the read simply returns
    a window of the post-rewind transcript.
    """
    page_limit = max(1, int(limit))
    ctx = _WINDOW_MERGE_CONTEXT_ENTRIES if context is None else max(0, int(context))

    with get_db() as conn:
        total, max_id = _count_entries_by_session_id_sync(session_id, subpath, conn)
        if total <= 0:
            return {
                "entries": [],
                "total": 0,
                "start": 0,
                "window_start": 0,
                "window_end": 0,
            }

        # Clamp the cursor: anything not a usable int means "from the newest
        # end", matching the API layer's leniency about a garbage ?before_seq=.
        if isinstance(before_seq, bool) or not isinstance(before_seq, int):
            end = total
        else:
            end = max(0, min(before_seq, total))
        window_start = max(0, end - page_limit)

        load_start = max(0, window_start - ctx)
        load_end = min(total, end + ctx)
        count = load_end - load_start
        if count <= 0:
            return {
                "entries": [],
                "total": total,
                "start": load_start,
                "window_start": window_start,
                "window_end": end,
            }

        # Read from the NEWEST end (id DESC) so the common "newest page" case
        # has OFFSET 0 and Postgres walks exactly the rows it returns. Only the
        # returned rows have their (frequently TOASTed) ``entry`` JSONB
        # detoasted, transferred and built into Python dicts — which is where
        # the whole-transcript cost used to be.
        offset = total - load_end
        cur = conn.cursor()
        if subpath is None:
            cur.execute(
                "SELECT entry FROM session_store_entries "
                "WHERE session_id = %s AND subpath IS NULL AND id <= %s "
                "ORDER BY id DESC "
                "LIMIT %s OFFSET %s",
                (session_id, max_id, count, offset),
            )
        else:
            cur.execute(
                "SELECT entry FROM session_store_entries "
                "WHERE session_id = %s AND subpath = %s AND id <= %s "
                "ORDER BY id DESC "
                "LIMIT %s OFFSET %s",
                (session_id, subpath, max_id, count, offset),
            )
        rows = cur.fetchall()

    # id DESC on the wire, oldest-first to the caller (the transform's contract).
    entries = [row["entry"] for row in reversed(rows)]
    return {
        "entries": entries,
        "total": total,
        "start": load_start,
        "window_start": window_start,
        "window_end": end,
    }


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

    A session whose transcript was never copied into the store has ZERO store
    rows, so reads render empty and resume seeds a blank context. This
    self-heals the store one session at a time, at read/resume time, from the
    session's local JSONL.

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

    Flushes any deferred summary fold first: this listing is half of the SDK's
    freshness comparison, so it must not report entry mtimes newer than the
    sidecars it is about to be compared against.
    """
    flush_summary_folds()
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
    """Return every folded summary for ``project_key``.

    The summary sidecar is folded lazily, so this — the only place
    the folded ``data`` is actually consumed — brings it current first: it
    flushes what this process still owes, then repairs anything a previous
    process left unfolded when it died mid-turn.
    """
    flush_summary_folds()
    _repair_stale_summaries_sync(project_key)
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

    if subpath is None:
        _forget_fold_state(session_id)

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
    session leaked its full transcript forever.
    """
    _forget_fold_state(session_id)
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

    Drops any deferred summary fold for the session first: the entries it was
    going to fold may be the ones being deleted here. The summary ``data``
    itself is left exactly as the pre-rewind behaviour left it (it is advisory
    metadata, and the watermark only ever moves forward — BIGSERIAL never
    reissues an id, so entries appended after a rewind are still strictly above
    it and cannot be skipped).
    """
    _forget_fold_state(session_id)
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
    """Postgres-backed app-owned CLI transcript store implementation.

    See module docstring for the full design. Implements all four optional
    methods (``list_sessions``, ``list_session_summaries``, ``delete``,
    ``list_subkeys``) for fast-path resume and summary reads without falling
    back to per-session ``load()``.

    All methods are coroutines wrapping synchronous psycopg2 work via
    ``asyncio.to_thread``. Exceptions raised by ``append`` are logged and
    re-raised to the caller; persistence failures are never silently accepted.
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
    # App-facing read API.
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

    async def load_entries_window_by_session_id(
        self,
        session_id: str,
        subpath: str | None = None,
        *,
        limit: int,
        before_seq: int | None = None,
        context: int | None = None,
    ) -> dict:
        """One WINDOW of ``session_id``'s transcript, newest-first by default.

        See :func:`_load_entries_window_by_session_id_sync` for the seq-space
        contract and the returned dict. Use this instead of
        :meth:`load_entries_by_session_id` whenever the caller only intends to
        display a page: it reads the window's rows and nothing else, where the
        full loader materialises the entire transcript.
        """
        return await asyncio.to_thread(
            _load_entries_window_by_session_id_sync,
            session_id,
            subpath,
            limit=limit,
            before_seq=before_seq,
            context=context,
        )

    async def list_subpaths_by_session_id(self, session_id: str) -> list[str]:
        """Distinct non-null subpaths for ``session_id`` (subagent transcripts).

        Matched by ``session_id`` alone — see
        :func:`_list_subpaths_by_session_id_sync`.
        """
        return await asyncio.to_thread(
            _list_subpaths_by_session_id_sync, session_id
        )


__all__ = [
    "PgSessionStore",
    "RewindTargetNotFound",
    "flush_summary_folds",
    "truncate_session_store_to_checkpoint",
]
