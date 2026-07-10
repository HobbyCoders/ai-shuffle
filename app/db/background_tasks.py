"""Durable background-task snapshots (v55) — WRITE-ONLY in Phase 1.

``task_registry`` (in-memory, rebuilt empty on restart) mirrors every task
transition into the ``background_tasks`` table through
``app.core.task_persistence``. NO reader depends on these rows yet — Phase 1
is derivation/durability only; flipping readers to this table is a later
phase. The single consumer today is boot reconcile, which marks this
install's stale non-terminal rows killed (background work never survives a
restart).
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from app.core.config import settings
from app.db.base import get_db
from app.db.compat import ph

logger = logging.getLogger(__name__)


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
                    tool_use_id, title, status, unparked, terminal_notified,
                    started_at, updated_at
                ) VALUES ({_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph}, {_ph})
                ON CONFLICT (task_id, session_id) DO UPDATE SET
                    instance_id = EXCLUDED.instance_id,
                    kind = EXCLUDED.kind,
                    is_sdk_background = EXCLUDED.is_sdk_background,
                    tool_use_id = EXCLUDED.tool_use_id,
                    title = EXCLUDED.title,
                    status = EXCLUDED.status,
                    unparked = EXCLUDED.unparked,
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
                bool(snapshot.get("unparked")),
                bool(snapshot.get("terminal_notified")),
                snapshot.get("started_at"),
                snapshot.get("updated_at"),
            ),
        )


def mark_stale_background_tasks_killed() -> int:
    """Boot reconcile: mark THIS install's non-terminal rows killed.

    Background work never survives a server restart, so any own-instance row
    still "running" at boot was stranded by the previous process. Mirrors
    ``startup_reconcile._clear_stale_parked_markers`` (instance-scoped; peer
    installs' rows are never touched). Returns the number of rows marked.
    """
    _ph = ph()
    instance_id = settings.effective_instance_id
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            f"""UPDATE background_tasks
                   SET status = 'killed', updated_at = CURRENT_TIMESTAMP
                 WHERE instance_id = {_ph}
                   AND status NOT IN ('completed', 'error', 'killed')""",
            (instance_id,),
        )
        return cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
