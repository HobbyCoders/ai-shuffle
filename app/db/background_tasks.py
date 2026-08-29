"""Durable background-task snapshots (v55).

``task_registry`` (in-memory, rebuilt empty on restart) mirrors every task
transition into the ``background_tasks`` table through
``app.core.task_persistence``. These rows started life WRITE-ONLY, but two
recovery readers now depend on them — both keyed off the fact that the
in-memory registry is empty after a restart:

- boot reconcile marks this install's stale non-terminal rows killed
  (``mark_stale_background_tasks_killed``);
- the stop-task durable force-settle fallback reads a single row
  (``get_background_task`` / ``mark_background_task_killed``) to settle a task
  the UI still shows "running" when nothing survives in memory.

The general hot-path read cutover (flipping the live views to this table) is
still a later phase.
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


def upsert_background_task(snapshot: Dict[str, Any]) -> None:
    """Insert-or-update one task snapshot (latest state wins).

    ``snapshot`` is the coalesced record built by ``task_registry`` (see
    ``TaskRegistry._persist_task_state``). The row is stamped with THIS
    install's instance id — several installs share one Postgres and each
    mirrors only its own registry.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""INSERT INTO background_tasks (
                    task_id, session_id, instance_id, kind, is_sdk_background,
                    tool_use_id, title, status, terminal_notified,
                    started_at, updated_at
                ) VALUES ({_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph})
                ON CONFLICT (task_id, session_id) DO UPDATE SET
                    instance_id = EXCLUDED.instance_id,
                    kind = EXCLUDED.kind,
                    is_sdk_background = EXCLUDED.is_sdk_background,
                    tool_use_id = EXCLUDED.tool_use_id,
                    title = EXCLUDED.title,
                    status = EXCLUDED.status,
                    terminal_notified = EXCLUDED.terminal_notified,
                    started_at = EXCLUDED.started_at,
                    updated_at = EXCLUDED.updated_at""",
            (
                snapshot.get("task_id"),
                snapshot.get("session_id"),
                settings.effective_instance_id,
                snapshot.get("kind"),
                bool(snapshot.get("is_sdk_background")),
                snapshot.get("tool_use_id"),
                snapshot.get("title"),
                snapshot.get("status"),
                bool(snapshot.get("terminal_notified")),
                snapshot.get("started_at"),
                snapshot.get("updated_at"),
            ),
        )


def mark_stale_background_tasks_killed() -> List[Dict[str, Any]]:
    """Boot reconcile: mark THIS install's non-terminal rows killed.

    Background work never survives a server restart, so any own-instance row
    still "running" at boot was stranded by the previous process. Mirrors
    ``startup_reconcile._clear_stale_parked_markers`` (instance-scoped; peer
    installs' rows are never touched). Returns the flipped rows (dicts with
    ``task_id``, ``session_id``, ``tool_use_id``, ``title``) so the caller can
    emit a terminal ``task.updated`` frame per row and settle the UI chip.
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
                   SET status = 'killed', updated_at = CURRENT_TIMESTAMP
                 WHERE instance_id = {_ph}
                   AND status NOT IN ({terminal_list})
             RETURNING task_id, session_id, tool_use_id, title""",
            (instance_id,),
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


def get_background_task(task_id: str, session_id: str) -> Optional[Dict[str, Any]]:
    """Read one durable task row (or ``None``) by its (task_id, session_id) key.

    Used by the ``stop_session_task`` durable force-settle fallback: post-restart
    the in-memory registry is empty, so the only remaining truth for a task the
    UI still shows "running" is this row. Returns the columns needed to settle
    it: ``task_id``, ``session_id``, ``kind``, ``tool_use_id``, ``title``,
    ``status``.
    """
    _ph = ph()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""SELECT task_id, session_id, kind, tool_use_id, title, status
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
