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
(see ``app/api/search.py``, ``app/core/startup.py``).

Feature flag
------------
``settings.session_store_mode`` gates the whole thing:

* ``"disabled"`` — adapter never attached (PR A default; zero behavior change).
* ``"mirror"``   — attach to ``ClaudeAgentOptions.session_store`` alongside the
                   CLI's JSONL writer. Used to backfill + verify in PR B.
* ``"postgres_only"`` — Postgres is the only source of truth; JSONL readers
                       removed. PR C cutover target.
"""

from __future__ import annotations

import asyncio
import logging
import re
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

from app.db.base import get_db

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


def _count_entries_sync(key: SessionKey) -> int:
    """Return how many rows already exist for ``key`` (incl. its subpath).

    Used by the backfill's idempotency pre-check: ``session_store_entries`` has
    a ``BIGSERIAL`` primary key and **no** unique constraint on
    ``(project_key, session_id, subpath)``, so a plain re-INSERT would duplicate
    rows. There is no ``ON CONFLICT`` target to lean on. The backfill therefore
    skips any (project_key, session_id, subpath) that already has rows — the
    on-disk JSONL transcript is the complete, append-only history, so "any rows
    present" means "already backfilled (or live-mirrored)".
    """
    project_key = key["project_key"]
    session_id = key["session_id"]
    subpath = _subpath_of(key)

    with get_db() as conn:
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
    """
    if not entries:
        return 0

    project_key = key["project_key"]
    session_id = key["session_id"]
    subpath = _subpath_of(key)

    written = 0
    with get_db() as conn:
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


def _list_subkeys_sync(key: SessionListSubkeysKey) -> list[str]:
    # Match on session_id ONLY (drop the project_key predicate) so the SDK's
    # resume subkey materialization works regardless of the cwd-derived
    # project_key it passes in. See _load_entries_by_session_id_sync.
    return _list_subpaths_by_session_id_sync(key["session_id"])


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


__all__ = ["PgSessionStore"]
