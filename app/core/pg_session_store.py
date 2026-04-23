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
            [(project_key, session_id, subpath, Json(dict(e))) for e in entries],
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
            (Json(folded["data"]), project_key, session_id),
        )


def _load_sync(key: SessionKey) -> list[SessionStoreEntry] | None:
    project_key = key["project_key"]
    session_id = key["session_id"]
    subpath = _subpath_of(key)

    with get_db() as conn:
        cur = conn.cursor()
        if subpath is None:
            cur.execute(
                "SELECT entry FROM session_store_entries "
                "WHERE project_key = %s AND session_id = %s AND subpath IS NULL "
                "ORDER BY id ASC",
                (project_key, session_id),
            )
        else:
            cur.execute(
                "SELECT entry FROM session_store_entries "
                "WHERE project_key = %s AND session_id = %s AND subpath = %s "
                "ORDER BY id ASC",
                (project_key, session_id, subpath),
            )
        rows = cur.fetchall()

    if not rows:
        return None
    # psycopg2 JSONB returns dicts already; SDK contract is deep-equal not
    # byte-equal so no round-trip normalization is required.
    return [row["entry"] for row in rows]


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
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT subpath FROM session_store_entries "
            "WHERE project_key = %s AND session_id = %s AND subpath IS NOT NULL",
            (key["project_key"], key["session_id"]),
        )
        rows = cur.fetchall()
    return [r["subpath"] for r in rows]


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


__all__ = ["PgSessionStore"]
