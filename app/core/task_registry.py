"""
Task Registry - In-memory store for the in-app agent's in-flight background tasks.

This unifies two sources of "background work" into a single per-session view so
the agent can query what it has running and the UI can surface it on every poll:

1. SDK background AGENT tasks — TaskStartedMessage / TaskProgressMessage /
   TaskNotificationMessage events yielded inside ``app/core/query_engine.py``.
2. App shell subprocesses — the ``Monitor`` tool family in
   ``app/mcp/builtin_tools.py``.

Key principles (mirrors ``app/core/live_session_store.py``):
1. In-memory only — no DB table, no migration. State is rebuilt on restart.
2. Keyed by AI Shuffle ``session_id`` then ``task_id``.
3. Tasks intentionally OUTLIVE a single turn — the registry is only cleared when
   the live session is finally removed (see ``cleanup_ended_session``).
4. One wire shape (``to_wire``) is shared by the ``TaskList`` MCP tool result and
   the ``/live`` poll response so the frontend has a single contract.

Runs on a single asyncio loop, so a plain dict is sufficient; methods still
guard against missing keys so a stray update can never raise.
"""

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    """Current UTC time as an ISO-8601 string (matches _BackgroundTask.started_at)."""
    return datetime.now(timezone.utc).isoformat()


# Value vocabulary (documented, not enforced): the registry deliberately does NOT
# reject unknown values, so a new SDK kind/status can flow through without a code
# change. kind ∈ {"agent", "shell"}; status ∈ {"running", "completed", "error",
# "killed"} (the last three terminal).


def to_wire(record: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an internal (snake_case) task record into the camelCase wire shape.

    Mirrors native Claude Code ``TaskList`` (id/subject/status) plus AI Shuffle
    extras. Used by BOTH the ``TaskList`` tool result and the ``/live`` response.
    """
    return {
        "id": record.get("task_id"),
        "subject": record.get("title"),
        "status": record.get("status"),
        "kind": record.get("kind"),
        "summary": record.get("summary"),
        "startedAt": record.get("started_at"),
        "updatedAt": record.get("updated_at"),
    }


class TaskRegistry:
    """In-memory per-session registry of in-flight background tasks."""

    def __init__(self) -> None:
        # session_id -> {task_id -> record}
        self._tasks: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def start_task(
        self,
        session_id: str,
        task_id: str,
        kind: str,
        title: Optional[str],
        *,
        summary: Optional[str] = None,
    ) -> None:
        """Register a new task (idempotent — if it already exists, update it).

        Re-registering preserves the original ``started_at`` and any existing
        terminal status: a late ``start`` for an already-finished task must not
        flip it back to running.
        """
        if not session_id or not task_id:
            return
        now = _now_iso()
        bucket = self._tasks.setdefault(session_id, {})
        existing = bucket.get(task_id)
        if existing is not None:
            # Treat as an update — refresh mutable fields, keep started_at.
            if title is not None:
                existing["title"] = title
            if summary is not None:
                existing["summary"] = summary
            existing["kind"] = kind or existing.get("kind")
            existing["updated_at"] = now
            return
        bucket[task_id] = {
            "task_id": task_id,
            "kind": kind,
            "title": title,
            "status": "running",
            "summary": summary,
            "started_at": now,
            "updated_at": now,
        }

    def update_task(
        self,
        session_id: str,
        task_id: str,
        *,
        status: Optional[str] = None,
        summary: Optional[str] = None,
        title: Optional[str] = None,
    ) -> None:
        """Update an existing task. No-op if the task is unknown."""
        record = self._tasks.get(session_id, {}).get(task_id)
        if record is None:
            return
        if status is not None:
            record["status"] = status
        if summary is not None:
            record["summary"] = summary
        if title is not None:
            record["title"] = title
        record["updated_at"] = _now_iso()

    def complete_task(self, session_id: str, task_id: str, status: str) -> None:
        """Set a terminal status on a task. No-op if the task is unknown."""
        record = self._tasks.get(session_id, {}).get(task_id)
        if record is None:
            return
        record["status"] = status
        record["updated_at"] = _now_iso()

    def get_active(self, session_id: str) -> List[Dict[str, Any]]:
        """Running tasks only, newest-first, each as a wire dict."""
        bucket = self._tasks.get(session_id)
        if not bucket:
            return []
        records = [r for r in bucket.values() if r.get("status") == "running"]
        records.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return [to_wire(r) for r in records]

    def get_all(self, session_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Running + recently-finished tasks, newest-first, each as a wire dict.

        Ordered by ``updated_at`` so just-finished tasks surface near the top,
        then truncated to ``limit``.
        """
        bucket = self._tasks.get(session_id)
        if not bucket:
            return []
        records = list(bucket.values())
        records.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
        return [to_wire(r) for r in records[:limit]]

    def clear_session(self, session_id: str) -> None:
        """Drop all tasks for a session (called when the live session is removed)."""
        self._tasks.pop(session_id, None)

    def reset_all(self) -> None:
        """Drop all task state. Used on shutdown and between tests."""
        self._tasks.clear()


# Global instance
task_registry = TaskRegistry()
