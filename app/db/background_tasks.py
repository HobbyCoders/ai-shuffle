"""Durable background-task snapshots.

``task_registry`` (in-memory, rebuilt empty on restart) mirrors every task
transition into the ``background_tasks`` table through
``app.core.task_persistence``. Two recovery readers depend on these rows —
both keyed off the fact that the in-memory registry is empty after a restart:

- boot reconcile marks this install's stale non-terminal rows killed
  (``mark_stale_background_tasks_killed``);
- the stop-task durable force-settle fallback reads a single row
  (``get_background_task`` / ``mark_background_task_killed``) to settle a task
  the UI still shows "running" when nothing survives in memory.

Sidebar list/search reads and user-scoped updates use shared running-task
presence.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.db.base import get_db, rows_to_list
from app.db.compat import ph

logger = logging.getLogger(__name__)

# Terminal task statuses — a row in any of these needs no further reconcile.
# Single source of truth shared with ``query_session_lifecycle`` (import it,
# do not re-list) and used to build the stale-sweep SQL ``NOT IN`` predicate.
TERMINAL_STATUSES: tuple = ("completed", "error", "killed")


def _task_text(text: Optional[str]) -> Optional[str]:
    """Render NUL in free-form task text for PostgreSQL without hiding byte loss.

    Monitor stdout (including binary output) reaches terminal summaries verbatim.
    PostgreSQL rejects U+0000 in text, aborting even unrelated rows in the batch.
    Show each NUL as U+2400 SYMBOL FOR NULL in this durable mirror only; this is
    a lossy display representation, not an encoding to decode on restore. Live
    output, ordinary Unicode, literal escapes and generated identifiers stay
    unchanged. One-for-one replacement also preserves the existing text bounds.
    """
    return text.replace("\x00", "␀") if text is not None else None


def upsert_background_task(snapshot: Dict[str, Any]) -> None:
    """Insert-or-update one task snapshot (latest state wins).

    ``snapshot`` is the coalesced record built by ``task_registry`` (see
    ``TaskRegistry._persist_task_state``). The row is stamped with THIS
    install's instance id — several installs share one Postgres and each
    mirrors only its own registry.
    """
    upsert_background_tasks([snapshot])


def upsert_background_tasks(snapshots: List[Dict[str, Any]]) -> None:
    """Persist a coalesced batch in one transaction."""
    if not snapshots:
        return
    def bounded_summary(snapshot: Dict[str, Any]) -> Optional[str]:
        text = _task_text(snapshot.get("summary")) or ""
        if len(text) <= 20_000:
            return text or None
        marker = "\n… [middle of terminal summary clipped] …\n"
        keep = 20_000 - len(marker)
        return text[:keep // 2] + marker + text[-(keep - keep // 2):]

    _ph = ph()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.executemany(
            f"""INSERT INTO background_tasks (
                    task_id, session_id, instance_id, kind, is_sdk_background,
                    tool_use_id, title, status, terminal_notified,
                    started_at, updated_at, summary, reason, report_path, report_chars,
                    agent, roster_name, model
                ) VALUES ({", ".join([_ph] * 18)})
                ON CONFLICT (task_id, session_id) DO UPDATE SET
                    instance_id = EXCLUDED.instance_id,
                    kind = EXCLUDED.kind,
                    is_sdk_background = EXCLUDED.is_sdk_background,
                    tool_use_id = EXCLUDED.tool_use_id,
                    title = EXCLUDED.title,
                    status = EXCLUDED.status,
                    terminal_notified = EXCLUDED.terminal_notified,
                    started_at = EXCLUDED.started_at,
                    updated_at = EXCLUDED.updated_at,
                    summary = EXCLUDED.summary,
                    reason = EXCLUDED.reason,
                    report_path = EXCLUDED.report_path,
                    report_chars = EXCLUDED.report_chars,
                    agent = EXCLUDED.agent,
                    roster_name = EXCLUDED.roster_name,
                    model = EXCLUDED.model""",
            [(
                snapshot.get("task_id"),
                snapshot.get("session_id"),
                settings.effective_instance_id,
                snapshot.get("kind"),
                bool(snapshot.get("is_sdk_background")),
                snapshot.get("tool_use_id"),
                _task_text(snapshot.get("title")),
                snapshot.get("status"),
                bool(snapshot.get("terminal_notified")),
                snapshot.get("started_at"),
                snapshot.get("updated_at"),
                bounded_summary(snapshot),
                (_task_text(snapshot.get("reason")) or "")[:256] or None,
                (_task_text(snapshot.get("report_path")) or "")[:4096] or None,
                snapshot.get("report_chars"),
                (_task_text(snapshot.get("agent")) or "")[:256] or None,
                (_task_text(snapshot.get("name")) or "")[:256] or None,
                (_task_text(snapshot.get("model")) or "")[:256] or None,
            ) for snapshot in snapshots],
        )


def get_running_task_presence(session_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    """Shared durable presence and DB owner, including sessions with no work.

    No instance filter: a peer's work is just as visible as this install's.
    Only running rows count; retained terminal history never pins the sidebar.
    """
    ids = list(dict.fromkeys(session_ids))
    if not ids:
        return {}
    _ph = ph()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT s.id, s.api_user_id,
                       EXISTS (SELECT 1 FROM background_tasks b
                               WHERE b.session_id = s.id AND b.status = 'running')
                           AS has_running_tasks
                  FROM sessions s WHERE s.id IN ({', '.join([_ph] * len(ids))})""",
            ids,
        )
        return {
            row["id"]: {"api_user_id": row["api_user_id"],
                        "has_running_tasks": bool(row["has_running_tasks"])}
            for row in rows_to_list(cursor.fetchall())
        }


#: Kill attribution the boot reconcile stamps on a row the restart stranded.
#: ``task_registry`` reads a ``killed`` row carrying it back as "interrupted".
SERVER_RESTART_REASON = "server_restart"


def mark_stale_background_tasks_killed(
    summary: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Boot reconcile: mark THIS install's non-terminal rows killed.

    Background work never survives a server restart, so any own-instance row
    still "running" at boot was stranded by the previous process. Mirrors
    ``startup_reconcile._clear_stale_parked_markers`` (instance-scoped; peer
    installs' rows are never touched). Each flipped row is stamped with reason
    ``server_restart`` (and ``summary`` when given) so a later read can tell a
    restart casualty from an ordinary kill. Returns the flipped rows (dicts with
    ``task_id``, ``session_id``, ``tool_use_id``, ``title``, ``kind``, ``agent``,
    ``roster_name``, ``model``, ``started_at``) so the caller can emit a terminal
    ``task.updated`` frame per row, settle the UI chip, rebuild the registry
    and tell each parent what was interrupted.
    """
    _ph = ph()
    instance_id = settings.effective_instance_id
    # Safe literal join: TERMINAL_STATUSES is a module constant of trusted
    # string literals, never user input.
    terminal_list = ", ".join(f"'{s}'" for s in TERMINAL_STATUSES)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""UPDATE background_tasks
                   SET status = 'killed', updated_at = CURRENT_TIMESTAMP,
                       reason = {_ph}, summary = COALESCE({_ph}, summary)
                 WHERE instance_id = {_ph}
                   AND status NOT IN ({terminal_list})
             RETURNING task_id, session_id, tool_use_id, title, kind, agent,
                       roster_name, model, started_at""",
            (SERVER_RESTART_REASON, summary, instance_id),
        )
        return rows_to_list(cursor.fetchall())


def list_session_terminal_background_tasks(
    session_id: str, limit: int = 200,
) -> List[Dict[str, Any]]:
    """A session's finished task rows, newest first, for rebuilding its registry.

    After a restart the in-memory registry is empty, so a parent asking for its
    workers (``TaskList``, ``ListAgents``, ``SendMessage``) has only these rows
    left. Only terminal rows: a non-terminal one belongs to live work (another
    install's, or this process's own, which is already in memory). Bounded so a
    long-lived session cannot pull its whole history into memory.
    """
    _ph = ph()
    terminal_list = ", ".join(f"'{s}'" for s in TERMINAL_STATUSES)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT task_id, session_id, kind, tool_use_id, title, status,
                       started_at, updated_at, summary, reason, report_path,
                       report_chars, agent, roster_name, model
                  FROM background_tasks
                 WHERE session_id = {_ph}
                   AND status IN ({terminal_list})
                 ORDER BY updated_at DESC
                 LIMIT {_ph}""",
            (session_id, int(limit)),
        )
        return rows_to_list(cursor.fetchall())


def list_stale_nonterminal_background_tasks(
    older_than_seconds: int,
) -> List[Dict[str, Any]]:
    """THIS install's non-terminal rows not updated for ``older_than_seconds``.

    Feed for the periodic stale-task sweep (``startup_reconcile.
    settle_stale_background_task_rows``): a non-terminal row this old whose
    task the in-memory registry no longer knows can never settle on its own —
    the boot reconcile only runs at boot, and an install can stay up for weeks.
    Read-only; instance-scoped like the boot sweep. Returns dicts with
    ``task_id``, ``session_id``, ``tool_use_id``, ``title``, ``status``.
    """
    _ph = ph()
    instance_id = settings.effective_instance_id
    # Safe literal join: TERMINAL_STATUSES is a module constant of trusted
    # string literals, never user input.
    terminal_list = ", ".join(f"'{s}'" for s in TERMINAL_STATUSES)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT task_id, session_id, tool_use_id, title, status
                  FROM background_tasks
                 WHERE instance_id = {_ph}
                   AND status NOT IN ({terminal_list})
                   AND updated_at < NOW() - make_interval(secs => {_ph})""",
            (instance_id, older_than_seconds),
        )
        return rows_to_list(cursor.fetchall())


def list_session_nonterminal_background_tasks(
    session_id: str,
) -> List[Dict[str, Any]]:
    """THIS install's still-running rows for ONE session.

    Feed for the park watchdog (``startup_reconcile.sweep_stalled_parks``):
    when a session has been parked past the hard ceiling with no wake, these
    are the tasks it is parked ON, and forcing their terminal frames is what
    wakes it. Read-only; instance-scoped like the other sweeps.
    """
    _ph = ph()
    instance_id = settings.effective_instance_id
    # Safe literal join: TERMINAL_STATUSES is a module constant of trusted
    # string literals, never user input.
    terminal_list = ", ".join(f"'{s}'" for s in TERMINAL_STATUSES)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT task_id, session_id, tool_use_id, title, status, kind
                  FROM background_tasks
                 WHERE instance_id = {_ph}
                   AND session_id = {_ph}
                   AND status NOT IN ({terminal_list})""",
            (instance_id, session_id),
        )
        return rows_to_list(cursor.fetchall())


def get_background_task(task_id: str, session_id: str) -> Optional[Dict[str, Any]]:
    """Read one durable task row (or ``None``) by its (task_id, session_id) key.

    Used by the ``stop_session_task`` durable force-settle fallback: post-restart
    the in-memory registry is empty, so the only remaining truth for a task the
    UI still shows "running" is this row. Returns the columns needed to settle
    it: ``task_id``, ``session_id``, ``kind``, ``tool_use_id``, ``title``,
    ``status``, ``started_at``, ``updated_at``.

    The two timestamps are also what ``TaskRegistry._durable_task`` needs: when
    nothing survives in memory this row is the only answer ``TaskGet`` can give
    a parent asking about its own finished worker, and a record with no times on
    it reads as a task that never ran.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT task_id, session_id, kind, tool_use_id, title, status,
                       started_at, updated_at, summary, reason, report_path, report_chars,
                       agent, roster_name, model
                  FROM background_tasks
                 WHERE task_id = {_ph} AND session_id = {_ph}""",
            (task_id, session_id),
        )
        row = cursor.fetchone()
    return dict(row) if row is not None else None


def mark_background_task_killed(task_id: str, session_id: str) -> None:
    """Flip ONE durable task row to ``killed`` by its (task_id, session_id) key.

    Single-row counterpart to ``mark_stale_background_tasks_killed`` used by the
    ``stop_session_task`` durable force-settle fallback when it reaps a
    still-non-terminal row.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""UPDATE background_tasks
                   SET status = 'killed', updated_at = CURRENT_TIMESTAMP
                 WHERE task_id = {_ph} AND session_id = {_ph}""",
            (task_id, session_id),
        )
