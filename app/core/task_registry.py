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
        # session_id -> set of (task_id, signature) already delivered to the
        # parent as an "unpark" notification. Guards the post-loop follow-up turn
        # against re-triggering on a duplicate TaskNotificationMessage.
        self._notified: Dict[str, set] = {}
        # session_id -> set of task_ids whose TaskStartedMessage arrived during
        # the CURRENT turn. Reset at the start of every turn (see
        # ``reset_started_this_turn``). This scopes the in-stream "unpark" stash
        # to the genuine FAST path — a background agent SPAWNED and COMPLETED
        # before the parent's ResultMessage. A terminal notification for a task
        # NOT in this set is a prior-turn orphan leaking from the SDK's pooled-
        # client buffer; it must NOT fire a spurious in-stream unpark in this
        # later, unrelated turn. (The slow path — a genuinely in-flight agent that
        # completes AFTER its spawning turn — is handled by the between-turns
        # drain pump, which is unaffected by this set.)
        self._started_this_turn: Dict[str, set] = {}
        # session_id -> set of PARENT launch tool_use_ids that the parent issued
        # with run_in_background=true. This is the gate that distinguishes a
        # GENUINELY-backgrounded sub-agent from two look-alikes that must NEVER
        # fire a spurious follow-up turn:
        #   1. A SYNCHRONOUS sub-agent (run_in_background=false): the parent already
        #      received its FULL result INLINE as a tool_result during the turn (the
        #      node was completed at query_engine yield "subagent_done"). The SDK
        #      STILL emits an at-rest TaskNotificationMessage for it AFTER the turn's
        #      receive_response() returns; that notification carries no NEW info and
        #      must not wake the model to say "nothing to act on."
        #   2. A grandchild background task launched BY a sub-agent: the parent never
        #      processed that launch tool_use, so its tool_use_id is absent here.
        # The terminal notification's tool_use_id equals the parent's launch
        # tool_use block id (see query_engine._task_notification_frame), so a simple
        # membership test gates the unpark. UNLIKE _started_this_turn, this set is
        # NOT reset per-turn — only cleared on clear_session — because the legit
        # slow path is a background agent launched in turn N whose notification
        # arrives between turn N and N+1.
        self._background_launches: Dict[str, set] = {}

    def mark_notified(self, session_id: str, task_id: str, signature: str) -> bool:
        """Idempotency latch for the parent "unpark" follow-up turn.

        Returns ``True`` only the FIRST time a given ``(task_id, signature)`` is
        seen for ``session_id``; every subsequent call with the same pair returns
        ``False``. Callers pass ``signature = uuid or f"{output_file}:{status}"``,
        so a NEW completion of the same task (fresh uuid — the agent can be
        resumed and may notify again) RE-ARMS the latch and yields ``True`` again.

        Defensive: a missing ``session_id``/``task_id`` never latches (returns
        ``False``) so a stray notification can't strand state.
        """
        if not session_id or not task_id:
            return False
        key = (task_id, signature)
        seen = self._notified.setdefault(session_id, set())
        if key in seen:
            return False
        seen.add(key)
        return True

    def reset_started_this_turn(self, session_id: str) -> None:
        """Clear the "started this turn" set for a session.

        Called at the START of every turn (right after ``stop_drain_pump``) so the
        in-stream unpark stash only ever fires for a background agent that both
        started AND finished within the turn currently streaming. Defensive: a
        missing ``session_id`` is a no-op.
        """
        if not session_id:
            return
        self._started_this_turn.pop(session_id, None)

    def mark_started_this_turn(self, session_id: str, task_id: str) -> None:
        """Record that ``task_id`` was started (TaskStartedMessage) this turn.

        Defensive: a missing ``session_id``/``task_id`` never records, so a stray
        message can't strand state.
        """
        if not session_id or not task_id:
            return
        self._started_this_turn.setdefault(session_id, set()).add(task_id)

    def was_started_this_turn(self, session_id: str, task_id: str) -> bool:
        """True iff ``task_id``'s TaskStartedMessage arrived during the current turn.

        Used by the in-stream stash to distinguish a genuine FAST sub-agent
        (spawned+completed this turn → unpark) from a prior-turn orphan leaking
        from the pooled-client buffer (→ suppress, no spurious unpark).
        """
        if not session_id or not task_id:
            return False
        return task_id in self._started_this_turn.get(session_id, set())

    def mark_background_launch(self, session_id: str, tool_use_id: str) -> None:
        """Record that the PARENT launched ``tool_use_id`` with run_in_background=true.

        Called from the streaming loop the moment a Task/Agent tool_use carrying the
        backgrounded flag is seen. ``tool_use_id`` is the parent's launch tool_use
        block id — the SAME id the SDK puts on the eventual terminal
        TaskNotificationMessage — so the unpark gate can later confirm a
        notification belongs to a genuinely-backgrounded sub-agent (and NOT a
        synchronous one whose result already returned inline, nor a sub-agent's own
        grandchild the parent never tracked).

        Defensive: a missing ``session_id``/``tool_use_id`` never records, so a stray
        message can't strand state.
        """
        if not session_id or not tool_use_id:
            return
        self._background_launches.setdefault(session_id, set()).add(tool_use_id)

    def was_background_launch(self, session_id: str, tool_use_id: str) -> bool:
        """True iff the parent launched ``tool_use_id`` with run_in_background=true.

        The unpark gate (both the in-stream stash and the between-turns drain pump)
        uses this to suppress the spurious at-rest follow-up turn for synchronous
        sub-agents (result already inline) and sub-agent-launched grandchildren
        (never recorded → False).
        """
        if not session_id or not tool_use_id:
            return False
        return tool_use_id in self._background_launches.get(session_id, set())

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

    def get_task(self, session_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        """A single task as a wire dict, or None if the task is unknown.

        Lets the ``TaskGet`` tool (and registry-aware ``TaskOutput``/``TaskStop``)
        resolve one task by id across BOTH sources without reaching into the
        private ``_tasks`` map.
        """
        record = self._tasks.get(session_id, {}).get(task_id)
        return to_wire(record) if record is not None else None

    def clear_session(self, session_id: str) -> None:
        """Drop all tasks for a session (called when the live session is removed)."""
        self._tasks.pop(session_id, None)
        self._notified.pop(session_id, None)
        self._started_this_turn.pop(session_id, None)
        self._background_launches.pop(session_id, None)

    def reset_all(self) -> None:
        """Drop all task state. Used on shutdown and between tests."""
        self._tasks.clear()
        self._notified.clear()
        self._started_this_turn.clear()
        self._background_launches.clear()


# Global instance
task_registry = TaskRegistry()
