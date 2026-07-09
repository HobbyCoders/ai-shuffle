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


def _now_epoch() -> float:
    """Current UTC time as epoch seconds (for retention-window math)."""
    return datetime.now(timezone.utc).timestamp()


# How long a FAILED background task (status "error"/"killed") stays surfaced on
# ``/live`` after it dies, so the taskbar shows the failure instead of the row
# silently vanishing. Bounded so a dead session's poller park-polls for at most
# this long before going idle. Kept short enough to limit waste, long enough that
# a glancing user sees the failure (and survives a reload, since retention is
# server-side). See ``_recent_terminal`` / ``get_visible``.
_RECENT_TERMINAL_WINDOW_SECONDS = 120.0

# How long a cleanly COMPLETED background task stays surfaced. Much shorter than
# the failure window — it exists to bridge the completion → unpark hand-off, not
# to display a result. ``complete_task`` drops a task from ``get_active`` the
# moment its completion is consumed (for Monitor shells: the moment the process
# exits), but the ``unpark_pending`` latch is only set a beat later — after the
# drain pump's dispatch awaits, or, for shells, after the SDK's own terminal
# notification arrives seconds behind the process exit (and the deliver-time
# ``end_streaming`` briefly clears the latch before re-asserting it). In every
# one of those gaps a ``/live`` response read (is_streaming=false,
# activeTasks=[], unpark_pending=false) — indistinguishable from idle — so a
# poll landing there STOPPED the poller and a session LOAD landing there
# classified the session idle and never armed one; the unpark follow-up then
# streamed into a dead UI until a manual refresh. Retaining the completed row
# keeps park-polling and load classification alive across the gap; the
# ``unpark_pending`` latch takes over well within the window. Self-limiting:
# with no follow-up coming, the poller goes idle when the window expires.
_RECENT_COMPLETED_WINDOW_SECONDS = 15.0

# Statuses that mean a task DIED rather than finished cleanly. These get the
# long retention window above (the user must SEE the failure); a clean
# "completed" is retained only for the short hand-off window.
_FAILURE_STATUSES = frozenset({"error", "killed"})


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
        # Parent launch tool_use_id — lets /live match a running task to the
        # subagent CARD (whose toolId is that same launch tool_use id) so a
        # store-loaded card can be reconciled back to "running" on a fresh load.
        "toolUseId": record.get("tool_use_id"),
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
        # session_ids with an "unpark" follow-up turn COMMITTED but not yet
        # streaming. Set the instant the drain pump (or in-stream stash) hands a
        # background sub-agent's completion off to a follow-up ``run_query``, and
        # cleared the moment that turn calls ``live_session_store.start_streaming``
        # (or it tears down). It bridges the gap between a background task
        # completing — ``complete_task`` removes it from ``get_active`` so the poll
        # reports ``activeTasks=[]`` — and the follow-up turn flipping
        # ``is_streaming`` back to ``true``. ``/live`` surfaces it so a polling
        # client keeps its park-poll alive across that gap instead of seeing a
        # transient ``(is_streaming=false, activeTasks=[])`` and STOPPING — which
        # stranded the parked UI (the origin socket is gone, so nothing restarts
        # polling and the follow-up streamed unseen until a full reload).
        self._unpark_pending: set = set()
        # session_id -> {task_id, ...} for SDK-managed BACKGROUND tasks that PARK
        # the parent and unpark via a terminal TaskNotificationMessage on the
        # pooled SDK client — i.e. backgrounded shell commands (Bash/PowerShell
        # ``run_in_background``, SDK ``task_type=="local_bash"``). These register
        # in the registry as ``kind="shell"`` (for the SHELL badge), SAME as the
        # app's own Monitor-tool background shells (see ``mcp/builtin_tools.py``) —
        # but ONLY the SDK ones depend on the pooled client's lifecycle: evicting
        # the session would kill the subprocess and drop the completion
        # notification the parent is parked awaiting. This set lets the pool busy
        # signal + the unpark drain pump tell the two apart (Monitor shells are
        # app-managed, independent of the SDK client, and must NOT pin a session).
        # Populated from ``query_engine._record_task_message`` on the shell's
        # TaskStartedMessage; cleared with the session (status is checked live, so
        # a completed task in the set is simply ignored — no per-completion prune).
        self._sdk_background_tasks: Dict[str, set] = {}
        # session_id -> set of task_ids that have ALREADY driven an unpark
        # follow-up turn. Unlike ``_notified`` (keyed on (task_id, signature) and
        # therefore RE-ARMED by a fresh per-rest uuid), this is keyed on the
        # ``task_id`` ALONE: a given background task drives AT MOST ONE unpark for
        # its whole life. The newer Claude CLI emits a TaskNotificationMessage
        # EACH TIME an agent comes to rest with no live background children — a
        # repeatable resumable rest-signal with a fresh uuid every time, NOT a
        # one-shot completion — so the old signature latch let the same task
        # re-notify on every rest and storm the session with "nothing to act on"
        # follow-ups. Cleared only with the session.
        self._unparked: Dict[str, set] = {}
        # session_id -> {task_id -> {"task": <wire dict>, "recorded_at": <epoch>}}
        # for tasks that reached a FAILURE terminal status ("error"/"killed").
        # This is the retention store that fixes the "background task vanished
        # from the UI with no failed indication" bug: when the pooled SDK client
        # dies, force-cleanup reaps the session's running tasks to "killed"
        # (``complete_task``) and then immediately ``clear_session`` wipes
        # ``_tasks`` — so the very next ``/live`` poll saw no tasks and the
        # taskbar row dropped silently. This store deliberately SURVIVES
        # ``clear_session`` for ``_RECENT_TERMINAL_WINDOW_SECONDS`` so ``/live``
        # (via ``get_visible``) keeps surfacing the dead task as failed for a
        # window. Self-pruning on read/write; rebuilt on restart. Keyed by
        # task_id so a re-reaped task replaces rather than duplicates its row.
        self._recent_terminal: Dict[str, Dict[str, Dict[str, Any]]] = {}

    def _prune_recent_terminal(self, session_id: str, *, now: Optional[float] = None) -> None:
        """Drop expired terminal-retention entries for a session; drop the bucket
        if it empties. Each entry carries its own ``ttl`` (failures get the long
        window, clean completions the short hand-off window; a missing ttl —
        pre-existing entry — defaults to the long window). Defensive: a missing
        session is a no-op."""
        bucket = self._recent_terminal.get(session_id)
        if not bucket:
            return
        now_val = now if now is not None else _now_epoch()
        expired = [
            tid
            for tid, e in bucket.items()
            if now_val - e.get("recorded_at", 0.0)
            > e.get("ttl", _RECENT_TERMINAL_WINDOW_SECONDS)
        ]
        for tid in expired:
            bucket.pop(tid, None)
        if not bucket:
            self._recent_terminal.pop(session_id, None)

    def mark_sdk_background_task(self, session_id: str, task_id: str) -> None:
        """Record that ``task_id`` is an SDK-managed background task the parent
        parks on (currently a backgrounded ``local_bash`` shell command).

        Distinguishes SDK-backgrounded shells (which DO pin a parked session and
        DO drive an unpark) from the app's Monitor-tool background shells (which
        share ``kind="shell"`` but are independent of the pooled SDK client and
        must NOT pin). Defensive: a missing id is a no-op.
        """
        if not session_id or not task_id:
            return
        self._sdk_background_tasks.setdefault(session_id, set()).add(task_id)

    def is_sdk_background_task(self, session_id: str, task_id: str) -> bool:
        """True iff ``task_id`` was recorded as an SDK-managed background task.

        Used as the unpark gate's robust fallback for backgrounded ``local_bash``
        shells. The primary gate (``was_background_launch``) keys on the parent's
        launch ``tool_use_id`` — but the SDK does not reliably populate
        ``tool_use_id`` on a shell's terminal notification, whereas ``task_id`` is
        always present. A task in this set is, by construction, a genuinely
        backgrounded shell the parent parked on (there is no "synchronous
        local_bash" notification to confuse it with — only ``run_in_background``
        shells emit a TaskStartedMessage), so its terminal notification always
        warrants the unpark. Defensive: missing ids return ``False``.
        """
        if not session_id or not task_id:
            return False
        return task_id in self._sdk_background_tasks.get(session_id, set())

    def mark_unpark_pending(self, session_id: str) -> None:
        """Latch that an unpark follow-up turn is committed but not yet streaming.

        Defensive: a missing ``session_id`` is a no-op.
        """
        if not session_id:
            return
        self._unpark_pending.add(session_id)

    def clear_unpark_pending(self, session_id: str) -> None:
        """Drop the unpark-pending latch (follow-up started streaming or failed)."""
        if not session_id:
            return
        self._unpark_pending.discard(session_id)

    def is_unpark_pending(self, session_id: str) -> bool:
        """True iff an unpark follow-up turn is committed but not yet streaming."""
        return session_id in self._unpark_pending

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

    def mark_unparked(self, session_id: str, task_id: str) -> bool:
        """One-unpark-per-task latch, keyed on ``task_id`` ALONE.

        Returns ``True`` only the FIRST time ``task_id`` is seen for
        ``session_id``; every later call returns ``False`` regardless of the
        notification's uuid/signature. This is the dedup gate for the unpark
        follow-up turn: a background task drives at most ONE follow-up for its
        whole life. It deliberately ignores the per-rest uuid that
        ``mark_notified`` keyed on, because the newer CLI re-emits a terminal
        TaskNotificationMessage with a FRESH uuid each time the agent comes to
        rest — which RE-ARMED the old latch and let one task storm the session
        with repeated "nothing to act on" follow-ups.

        Defensive: a missing ``session_id``/``task_id`` never latches (returns
        ``False``) so a stray notification can't strand state.
        """
        if not session_id or not task_id:
            return False
        seen = self._unparked.setdefault(session_id, set())
        if task_id in seen:
            return False
        seen.add(task_id)
        return True

    def was_unparked(self, session_id: str, task_id: str) -> bool:
        """True iff ``task_id`` has already driven an unpark for ``session_id``."""
        if not session_id or not task_id:
            return False
        return task_id in self._unparked.get(session_id, set())

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

    def get_background_launches(self, session_id: str) -> set:
        """Return a COPY of the parent launch tool_use_ids the parent issued with
        run_in_background=true for ``session_id``.

        These outlive the turn that launched them (the set is only cleared with the
        session), so a FOLLOW-UP turn's streaming loop can seed its per-turn
        ``task_tool_uses`` map with them — otherwise a background sub-agent's
        StreamEvents (which still carry ``parent_tool_use_id`` in the later turn)
        fail the ``parent_tool_id in task_tool_uses`` gate and leak into the
        MAIN-agent text/thinking/tool handlers.

        Returns a copy so callers can't mutate the registry's internal set.
        Defensive: a missing ``session_id`` yields an empty set.
        """
        if not session_id:
            return set()
        return set(self._background_launches.get(session_id, set()))

    def start_task(
        self,
        session_id: str,
        task_id: str,
        kind: str,
        title: Optional[str],
        *,
        summary: Optional[str] = None,
        tool_use_id: Optional[str] = None,
    ) -> None:
        """Register a new task (idempotent — if it already exists, update it).

        Re-registering preserves the original ``started_at``. A fresh
        ``TaskStartedMessage`` for a CLEANLY-COMPLETED task is a genuine RESUME
        (the SDK re-emits TaskStartedMessage when a background sub-agent is
        continued via the SendMessage tool under the SAME task_id), so its status
        is flipped BACK to "running" — otherwise the resumed run stayed
        "completed" and vanished from ``get_active``/``get_visible`` (the poll's
        ``activeTasks``), leaving a genuinely-running agent invisible in the UI
        while a chat Stop still killed the live SDK query. On revival the stale
        completed snapshot in ``_recent_terminal`` is also dropped so the row
        can't double-count or resurrect as "completed".

        A FAILURE terminal status ("error"/"killed") is NOT revived: a re-emitted
        start for such a task is the force-cleanup / dead-pooled-client stale
        leak (see ``_recent_terminal`` / ``test_reaped_failure_resnapshots_status``),
        and reviving it would falsely show a dead task as running.
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
            # Bind the parent launch tool_use_id once — a later re-register (e.g.
            # a resume) may carry it when the first start did not; never clobber
            # an id already recorded.
            if tool_use_id and not existing.get("tool_use_id"):
                existing["tool_use_id"] = tool_use_id
            # Resume revival: a fresh start for a CLEANLY-COMPLETED task flips it
            # back to running (failures stay terminal — that's a stale leak).
            if existing.get("status") == "completed":
                existing["status"] = "running"
                retained = self._recent_terminal.get(session_id)
                if retained is not None:
                    retained.pop(task_id, None)
                    if not retained:
                        self._recent_terminal.pop(session_id, None)
            existing["updated_at"] = now
            return
        bucket[task_id] = {
            "task_id": task_id,
            "kind": kind,
            "title": title,
            "status": "running",
            "summary": summary,
            "tool_use_id": tool_use_id,
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
        """Set a terminal status on a task. No-op if the task is unknown.

        A snapshot of the task is also stashed in the retention store so ``/live``
        (``get_visible``) keeps surfacing it after ``clear_session`` wipes the live
        ``_tasks`` map. FAILURES ("error"/"killed") are retained for
        ``_RECENT_TERMINAL_WINDOW_SECONDS`` so a force-cleanup reap (dead SDK
        client) doesn't drop the row with no failed indication; clean completions
        get the much shorter ``_RECENT_COMPLETED_WINDOW_SECONDS`` purely to keep
        park-polling alive across the completion → ``unpark_pending`` hand-off gap.
        """
        record = self._tasks.get(session_id, {}).get(task_id)
        if record is None:
            return
        record["status"] = status
        record["updated_at"] = _now_iso()
        now = _now_epoch()
        self._prune_recent_terminal(session_id, now=now)
        bucket = self._recent_terminal.setdefault(session_id, {})
        ttl = (
            _RECENT_TERMINAL_WINDOW_SECONDS
            if status in _FAILURE_STATUSES
            else _RECENT_COMPLETED_WINDOW_SECONDS
        )
        bucket[task_id] = {"task": to_wire(record), "recorded_at": now, "ttl": ttl}

    def get_active(self, session_id: str) -> List[Dict[str, Any]]:
        """Running tasks only, newest-first, each as a wire dict.

        This is the running-only view used by every CONTROL-FLOW predicate (pool
        busy signal, unpark gating). It must stay running-only — the ``/live``
        DISPLAY view (which also surfaces recently-failed tasks) is ``get_visible``.
        """
        bucket = self._tasks.get(session_id)
        if not bucket:
            return []
        records = [r for r in bucket.values() if r.get("status") == "running"]
        records.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return [to_wire(r) for r in records]

    def get_visible(self, session_id: str) -> List[Dict[str, Any]]:
        """Tasks to SHOW on the taskbar: running tasks PLUS recently-failed ones.

        This is the ``/live`` DISPLAY view — distinct from ``get_active`` so the
        running-only control-flow predicates are unaffected. A failed task
        (status "error"/"killed") that was reaped on force-cleanup is surfaced for
        ``_RECENT_TERMINAL_WINDOW_SECONDS`` so the row shows the failure instead of
        vanishing silently, then ages out (the poller park-polls during the window
        and goes idle once it empties). Deduped by id (a live record wins over a
        retained snapshot), newest-first by ``startedAt``.

        NOTE: the poller treats a non-empty ``activeTasks`` as "keep park-polling".
        Including a retained failure here therefore keeps a dead session's poller
        alive for the window — that is intentional and self-limiting (it stops the
        moment the window expires).
        """
        active = self.get_active(session_id)
        self._prune_recent_terminal(session_id)
        retained = self._recent_terminal.get(session_id)
        if not retained:
            return active
        seen = {t.get("id") for t in active}
        merged = list(active)
        for entry in retained.values():
            wire = entry.get("task") or {}
            if wire.get("id") in seen:
                continue
            merged.append(wire)
        merged.sort(key=lambda t: t.get("startedAt") or "", reverse=True)
        return merged

    def has_active_background_agent(self, session_id: str) -> bool:
        """True iff a background sub-agent (kind=="agent") is still running.

        This is the single source of truth for "this session has in-flight
        background work" — the busy signal the session pool honors so it never
        evicts/disconnects a PARKED session out from under a live sub-agent.

        A parked session looks idle to every other signal: its parent turn's
        ``receive_response()`` already returned at the parent ResultMessage, so
        ``is_streaming`` is False and there are no team members — yet a backgrounded
        sub-agent is still executing and its terminal TaskNotificationMessage has
        not yet arrived (it is caught by the between-turns drain pump). Evicting the
        pooled SDK client now would kill the sub-agent mid-run, strand its live-store
        node "running", and wipe the unpark gate so the parent never resumes. Gating
        eviction on this predicate closes that hole.

        Only ``kind=="agent"`` counts: shell subprocesses (the Monitor tool family)
        are independent of the pooled SDK client's lifecycle and must not pin a
        session in the pool.

        Defensive: a missing ``session_id`` returns ``False``.
        """
        bucket = self._tasks.get(session_id)
        if not bucket:
            return False
        return any(
            r.get("kind") == "agent" and r.get("status") == "running"
            for r in bucket.values()
        )

    def get_active_background_work(self, session_id: str) -> List[Dict[str, Any]]:
        """Running tasks that PARK the parent, newest-first, each as a wire dict.

        Superset of the agent-only ``has_active_background_agent`` view: includes
        BOTH background sub-agents (``kind=="agent"``) AND SDK-backgrounded shell
        commands (``kind=="shell"`` AND in ``_sdk_background_tasks`` — i.e. a
        ``local_bash`` ``run_in_background`` Bash/PowerShell command, NOT an
        app-managed Monitor shell). Both flavours are in-flight work whose terminal
        TaskNotificationMessage lands LATER on the pooled SDK client and which the
        parent turn has parked awaiting — so both must pin the session in the pool
        and warrant a between-turns unpark drain pump. The session pool busy signal
        and the pump-start guard share this view.
        """
        bucket = self._tasks.get(session_id)
        if not bucket:
            return []
        sdk_bg = self._sdk_background_tasks.get(session_id, set())
        records = [
            r for tid, r in bucket.items()
            if r.get("status") == "running"
            and (r.get("kind") == "agent" or tid in sdk_bg)
        ]
        records.sort(key=lambda r: r.get("started_at") or "", reverse=True)
        return [to_wire(r) for r in records]

    def has_active_background_work(self, session_id: str) -> bool:
        """True iff a background sub-agent OR an SDK-backgrounded shell is running.

        The session-pool busy predicate (``_has_background_work``) honors this so it
        never evicts/disconnects a PARKED session out from under in-flight work —
        whether that work is a sub-agent (``kind=="agent"``) or a backgrounded shell
        command (``local_bash``, tracked in ``_sdk_background_tasks``). The original
        ``has_active_background_agent`` covered only sub-agents, which let a session
        parked on a backgrounded Bash/PowerShell command get idle-evicted mid-run —
        killing the pooled SDK client that owns the shell's completion notification,
        so the parent never unparked. See ``get_active_background_work``.
        """
        return bool(self.get_active_background_work(session_id))

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
        """Drop all tasks for a session (called when the live session is removed).

        The failure-retention store (``_recent_terminal``) is deliberately NOT
        wiped here: force-cleanup reaps running tasks to "killed" and then calls
        this, so wiping it would re-introduce the silent-vanish bug. Retained
        failures age out on their own window (pruned here so a cleared session's
        already-expired entries don't linger).
        """
        self._tasks.pop(session_id, None)
        self._notified.pop(session_id, None)
        self._started_this_turn.pop(session_id, None)
        self._background_launches.pop(session_id, None)
        self._unpark_pending.discard(session_id)
        self._sdk_background_tasks.pop(session_id, None)
        self._unparked.pop(session_id, None)
        self._prune_recent_terminal(session_id)

    def reset_all(self) -> None:
        """Drop all task state. Used on shutdown and between tests."""
        self._tasks.clear()
        self._notified.clear()
        self._started_this_turn.clear()
        self._background_launches.clear()
        self._unpark_pending.clear()
        self._sdk_background_tasks.clear()
        self._unparked.clear()
        self._recent_terminal.clear()


# Global instance
task_registry = TaskRegistry()
