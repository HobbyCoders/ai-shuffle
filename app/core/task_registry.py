"""
Task Registry - In-memory store for the in-app agent's in-flight background tasks.

This unifies two sources of "background work" into a single per-session view so
the agent can query what it has running and the UI can surface it on every poll:

1. SDK background AGENT tasks — TaskStartedMessage / TaskProgressMessage /
   TaskNotificationMessage events yielded inside ``app/core/query_engine.py``.
2. App shell subprocesses — the ``Monitor`` tool family in
   ``app/mcp/builtin_tools.py``.

Key principles:
1. In-memory only — no DB table, no migration. State is rebuilt on restart.
2. Keyed by AI Shuffle ``session_id`` then ``task_id``.
3. Tasks intentionally OUTLIVE a single turn — the registry is only cleared when
   the session itself is torn down (clear, eviction, worktree termination).
4. One wire shape (``to_wire``) is shared by the ``TaskList`` MCP tool result and
   the ``/live`` poll response so the frontend has a single contract.

Runs on a single asyncio loop, so a plain dict is sufficient; methods still
guard against missing keys so a stray update can never raise.
"""

import logging
import time
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

# How long a cleanly COMPLETED background task stays READABLE after it finishes.
#
# This is the parent's hand-off window, and it used to be fifteen seconds — sized
# for the poller gap below and nothing else. That is far shorter than the thing
# it actually has to cover: a parent woken about its worker reads the result on
# its wake TURN, which starts whenever the session next gets a run lease. The
# audit caught a ``TaskGet`` thirty-five seconds after the worker finished
# answering "task not found", with ``TaskList(include_finished=True)`` returning
# an empty list beside it — the row had aged out before the parent it was written
# for ever looked. Minutes, not seconds: a wake can queue behind a long turn, and
# a stale record costs nothing but a little memory.
_RECENT_COMPLETED_WINDOW_SECONDS = 600.0

# How long a cleanly completed task stays SURFACED on ``/live`` (``get_visible``).
# Deliberately separate from — and much shorter than — the readability window
# above: this one exists to bridge the completion → unpark hand-off, not to
# display a result. ``complete_task`` drops a task from ``get_active`` the
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
# with no follow-up coming, the poller goes idle when the window expires — which
# is why the taskbar keeps the SHORT window while the record itself lives on.
_COMPLETED_VISIBLE_WINDOW_SECONDS = 15.0

# Statuses that mean a task DIED rather than finished cleanly. These get the
# long retention window above (the user must SEE the failure); a clean
# "completed" is retained only for the short hand-off window.
_FAILURE_STATUSES = frozenset({"error", "killed"})

# Terminal statuses that mean a task was STOPPED rather than that it failed on
# its own. ``killed`` is in both sets on purpose: it is a failure as far as
# RETENTION is concerned (the user must see the row) and a cancellation as far
# as WAKES are concerned (see ``is_unattributed_cancellation``). The fluid
# adapter's wire vocabulary adds "cancelled"/"stopped" for the same state.
_CANCELLATION_STATUSES = frozenset({"killed", "cancelled", "stopped"})

# Kill attribution for shutdown-origin reaps (lifecycle batch, item 4a): a
# background task killed because the SERVER is restarting must say so instead
# of presenting as a mysterious task failure. Every kill site that reaps while
# ``startup.is_shutting_down`` stamps this summary + reason onto the terminal
# record / unpark payload.
SERVER_SHUTDOWN_SUMMARY = "Server restarted — background task stopped"
SERVER_SHUTDOWN_REASON = "server_shutdown"

# Kill attribution for an EXPLICIT user-initiated stop (the "x" on the live
# background-tasks strip, or "Stop, and also stop background agents"). The stop
# path records the terminal transition on the registry row ITSELF — it must not
# rely on a CLI "stopped" frame flowing back (a parked session has no reader for
# it, and the scoped stop may emit none) or the row stays "running" forever.
# These stamp WHO stopped it so the terminal chip / retention row reads as a
# deliberate user stop, not a mysterious failure.
USER_STOP_SUMMARY = "Stopped by user"
USER_STOP_REASON = "user_stopped"

# Kill attribution for the Stop button's session-wide sweep. Distinct from the
# per-task "x" above only in wording — both are the user, but a sweep row wants
# to say WHY a task the user never singled out died.
STOP_ALL_SUMMARY = "Stopped by user (Stop ended all background work)"

# The wire status a DELIBERATE stop is chipped with. ``killed`` is the registry
# vocabulary and stays that way on the row (/live, TaskGet, retention), but the
# chat chip built from it used to render a red cross for work an agent stopped
# on purpose seconds earlier — an agent's own ``TaskStop`` of a healthy dev
# server read as a failure. ``cancelled`` folds back to ``killed`` in the
# frontend's task-summary reducer (TASK_STATE_MAP), so the chip settles cards
# and drops off the strip exactly as before; only its wording and colour change.
CANCELLED_CHIP_STATUS = "cancelled"

# Kill attribution for the session's OWN ``TaskStop`` — a model stopping a
# watcher or dev server it no longer needs. Stamped by
# ``stop_background_shell_task`` when no caller supplies an attribution, so the
# terminal chip can tell "an agent stopped this on purpose" apart from a task
# that was reaped because something died underneath it. Both leave a ``killed``
# row; only one of them is worth a red cross in the transcript.
AGENT_STOP_SUMMARY = "Stopped by the agent"
AGENT_STOP_REASON = "agent_stopped"

# Ceiling on remembered user-stop latches (see ``TaskRegistry._user_stopped``).
# Generous: the latch normally lives for seconds, until the user's next message.
_USER_STOPPED_MAX = 5000

# Kill reasons that mean "somebody decided to stop this", as opposed to "it
# died". Chips built for these read as cancelled rather than failed. An
# EXPLICIT reason is required: a reason-less ``killed`` is what the ghost reap
# and the force-eviction sweep write, and those really are failures from the
# user's point of view — the work cannot finish.
DELIBERATE_STOP_REASONS = frozenset({
    USER_STOP_REASON,
    AGENT_STOP_REASON,
    "user_stopped_all",
    "parent_turn_ended",
})


def chip_status(status: Optional[str], reason: Optional[str]) -> str:
    """The status a terminal CHIP should carry for a registry ``status``.

    Only ``killed`` is rewritten, and only when the record says WHO stopped it
    and that answer is a person or an agent. Everything else — ``error``,
    ``completed``, a reason-less reap, a ``killed`` carrying a failure-ish
    reason like ``monitor_timeout``, ``server_shutdown`` or
    ``provider_rate_limited`` — is passed through untouched.
    """
    if status != "killed":
        return status or ""
    if reason in DELIBERATE_STOP_REASONS:
        return CANCELLED_CHIP_STATUS
    return status


# Value vocabulary (documented, not enforced): the registry deliberately does NOT
# reject unknown values, so a new SDK kind/status can flow through without a code
# change. kind ∈ {"agent", "shell"}; status ∈ {"running", "completed", "error",
# "killed"} (the last three terminal).


def to_wire(record: Dict[str, Any]) -> Dict[str, Any]:
    """Convert an internal (snake_case) task record into the camelCase wire shape.

    Mirrors native Claude Code ``TaskList`` (id/subject/status) plus AI Shuffle
    extras. Used by BOTH the ``TaskList`` tool result and the ``/live`` response.
    """
    wire = {
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
    # Kill attribution (e.g. "server_shutdown"): additive — only present when a
    # reap stamped one, so the pinned wire contract is unchanged otherwise.
    if record.get("reason"):
        wire["reason"] = record.get("reason")
    # The dispatched agent's display NAME, for Task-tool workers. Additive for
    # the same reason: only ``dispatch_worker`` sets it, so every other task
    # kind keeps the exact shape it had.
    if record.get("agent"):
        wire["agent"] = record.get("agent")
    # The worker's provider/model label. It used to BE ``summary``, which made
    # every agent row in TaskList/TaskGet answer "OpenRouter · openai/gpt-5.6"
    # to the question "what is this task". The label is still worth having —
    # just not in the field that is supposed to say what happened.
    if record.get("model"):
        wire["model"] = record.get("model")
    # Where a completed agent worker's full report was persisted, and how long
    # it is. Additive: only ``complete_task`` on a successful agent dispatch
    # stamps them, so every other task kind keeps its exact shape.
    if record.get("report_path"):
        wire["reportPath"] = record.get("report_path")
    if record.get("report_chars") is not None:
        wire["reportChars"] = record.get("report_chars")
    return wire


class TaskRegistry:
    """In-memory per-session registry of in-flight background tasks."""

    def __init__(self) -> None:
        # session_id -> {task_id -> record}
        self._tasks: Dict[str, Dict[str, Dict[str, Any]]] = {}
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
        # cleared the moment that turn starts streaming (or it tears down).
        # It bridges the gap between a background task
        # completing — ``complete_task`` removes it from ``get_active`` so the poll
        # reports ``activeTasks=[]`` — and the follow-up turn flipping
        # ``is_streaming`` back to ``true``. ``/live`` surfaces it so a polling
        # client keeps its park-poll alive across that gap instead of seeing a
        # transient ``(is_streaming=false, activeTasks=[])`` and STOPPING — which
        # stranded the parked UI (the origin socket is gone, so nothing restarts
        # polling and the follow-up streamed unseen until a full reload).
        self._unpark_pending: set = set()
        # session_id -> monotonic timestamp of when the unpark-pending latch
        # was last set. Lets the runtime reconciler age-gate its "stale latch"
        # repair (a latch is legal for the brief commit -> start_streaming
        # hand-off; one older than the grace window with no pump and nothing
        # streaming is stranded). Kept in lock-step with ``_unpark_pending``.
        self._unpark_pending_since: Dict[str, float] = {}
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
        # session_id -> set of SUBAGENT-lane launch tool_use_ids: tool_use
        # blocks a SUB-AGENT sidechain (parent_tool_use_id set) issued with
        # run_in_background=true. Kept SEPARATE from ``_background_launches``
        # on purpose — that set means "the PARENT's own backgrounded work" and
        # feeds pool pinning + chip scoping (``is_own_background_task``), which
        # a grandchild must never do. This set exists ONLY so the unpark drain
        # pump can recognize the grandchild's terminal notification as a real
        # wake (2026-07-10 incident: a Monitor-pattern task launched from a
        # subagent sidechain completed between turns, both unpark predicates
        # missed it — decision=suppress-foreground — and the subagent parked
        # awaiting it was stranded forever; the #470 top-level-only gate on
        # ``mark_background_launch`` is what kept both predicates false).
        self._subagent_background_launches: Dict[str, set] = {}
        # session_id -> {task_id, ...} for background tasks whose launch was
        # recorded in ``_subagent_background_launches`` (armed at the SDK
        # TaskStarted, mirroring ``_sdk_background_tasks``). Consumed ONLY by
        # the unpark gates (drain pump + in-stream latch) — never by pool
        # pinning (the launching sub-agent's own kind=="agent" running record
        # already pins) or chip eligibility. Cleared with the session.
        self._subagent_background_tasks: Dict[str, set] = {}
        # session_id -> {task_id, ...} for APP-spawned Monitor shells (the MCP
        # Monitor tool and the fluid monitor_spawn spawner both land here). The
        # third lane of ``is_own_background_task``: these shells are the parent's
        # own background work — their completion must drive the unpark wake —
        # but they are independent OS processes, NOT SDK tasks, so they stay out
        # of ``_sdk_background_tasks`` and deliberately do NOT pin the session in
        # the pool (``get_active_background_work`` keeps excluding them).
        self._monitor_shell_tasks: Dict[str, set] = {}
        self._serving_monitor_tasks: Dict[str, set] = {}
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
        # session_id -> {task_id -> set(phase)} one-shot chip latches (see
        # ``mark_chipped``). Caps a task at MAX TWO "Task Updated" chips for
        # its whole lifetime: one on first sighting ("start"), one on its
        # terminal transition ("terminal"). Lives here — NOT in the drain
        # pump's per-run local set — so the cap holds across pump restarts
        # (each run used to reset its dedup and re-chip the same task).
        self._chipped: Dict[str, Dict[str, set]] = {}
        # session_id -> set of task_ids that have ALREADY produced an unpark/pump
        # wake turn (the late-frame latch, ``mark_woken``). Keyed on task_id ALONE:
        # a given background task drives AT MOST ONE wake for its whole life, so a
        # frame that arrives AFTER its wake already left the queue (the guarded
        # UPDATE merge lost the race, or the wake already ran) must not spawn a
        # fresh duplicate. Set at every OWN/PUMP wake enqueue/merge; checked on the
        # own-lane fresh-enqueue fall-through. Cleared with the session AND flipped
        # OFF on a resume (``start_task`` revival) so a re-run agent can wake again.
        self._woken: Dict[str, set] = {}
        # session_id -> set of task_ids explicitly canceled by the user. Unlike
        # ``_woken`` this is not a duplicate-delivery latch: it is checked BEFORE
        # every wake ownership path so a completion racing Stop cannot enqueue an
        # autonomous follow-up turn. Cleared with the session and on a genuine
        # completed-task revival under the same id.
        self._wake_suppressed: Dict[str, set] = {}
        # Sessions the USER stopped with the Stop button, until their next
        # message. Per-TASK suppression (``_wake_suppressed`` above) only covers
        # the tasks that existed at the moment Stop ran; this session-wide latch
        # covers everything else that could restart a stopped session — a nested
        # worker registered a beat later, a scheduled wakeup row, an idle-
        # watchdog nudge, a late terminal frame for a task the registry never
        # saw. Set by ``query_session_lifecycle.stop_all_background_tasks`` on
        # the user-stop path (which BOTH the WebSocket runner and the owning
        # runner execute, so it latches wherever the work actually lives) and
        # cleared by the next user-authored turn submit
        # (``turn_manager._submit_blocking``). Deliberately NOT cleared by
        # ``clear_session``: an idle eviction after a Stop must not un-stop the
        # session (that is exactly how the audited "stopped sessions come back
        # to life" reports were produced). A dict, not a set, purely so the
        # insertion order gives the cap below something to evict.
        self._user_stopped: Dict[str, float] = {}
        self._stop_generations: Dict[str, int] = {}
        self._stop_supervisors: Dict[str, set] = {}
        # session_id -> {child_launch_tuid -> owner_launch_tuid} (B-1). A grand-
        # child background task's launch tool_use_id mapped to the launch
        # tool_use_id of the SUB-AGENT that spawned it (its owner). Recorded when
        # a subagent-lane run_in_background launch is seen (query_engine, both the
        # in-stream ToolUseBlock branch and the between-turns drain). Lets the
        # PUMP lane resolve which running sub-agent a terminal child belongs to,
        # so a child step can be deferred against ITS owner's quiescence rather
        # than pumped blindly. Cleared with the session.
        self._child_owner: Dict[str, Dict[str, str]] = {}
        # session_id -> {owner_launch_tuid -> monotonic ts} (B-prime item b). The
        # last time a running sub-agent (an owner) emitted inner activity on the
        # main session's between-turns drained stream (idle_reader inner-message
        # dispatch calls ``note_owner_activity``). A parked/at-rest sub-agent is
        # SILENT there; a still-running one continuously emits. The deferred-pump
        # discriminator compares this against a child terminal's timestamp:
        # activity AFTER the child terminal ⇒ owner still running (the CLI steered
        # the child result to it for free) ⇒ suppress; silence ⇒ at rest ⇒ pump.
        # Cleared with the session.
        self._owner_activity_ts: Dict[str, Dict[str, float]] = {}
        # Deferred-pump cleanup callback (B-prime item f). The scheduler registers
        # ONE sync callback at install(); ``clear_session`` fires it with the
        # session id and ``reset_all`` with ``None`` (clear-all) so the scheduler's
        # per-owner deferred timers are cancelled with the session/registry rather
        # than leaking. Fully defensive — a raising callback is swallowed.
        self._pump_cleanup_callback: Optional[Any] = None
        # Guaranteed terminal notification (lifecycle batch, item 4b): a
        # callback the chat layer registers ONCE at startup. ``complete_task``
        # fires it exactly once per task (see ``mark_terminal_notified``) when
        # the parent's OWN background work transitions to error/killed, so a
        # kill with no reader (force-eviction reap, ghost reap, pool teardown)
        # still surfaces a durable terminal chip instead of silence. The
        # callback is SYNC and must never raise into complete_task; the chat
        # layer schedules its async surface work internally.
        self._terminal_notification_callback: Optional[Any] = None
        # session_id -> set of task_ids whose terminal callback already fired.
        self._terminal_notified: Dict[str, set] = {}

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

    def mark_monitor_shell_task(self, session_id: Optional[str], task_id: str, *, serves: bool = False) -> None:
        """Record that ``task_id`` is an APP-spawned Monitor shell of ``session_id``.

        Admits the shell to the OWN lane of the unpark gate so its terminal frame
        wakes the parked parent (bdf60782 wired the spawner's handoff; the gate
        still dropped every such frame as not-own work). Defensive: a missing
        id is a no-op (a module-level spawn has no parent to wake).
        """
        if not session_id or not task_id:
            return
        self._monitor_shell_tasks.setdefault(session_id, set()).add(task_id)
        if serves:
            self._serving_monitor_tasks.setdefault(session_id, set()).add(task_id)

    def is_serving_monitor(self, session_id: str, task_id: str) -> bool:
        return task_id in self._serving_monitor_tasks.get(session_id, set())

    def is_monitor_shell_task(self, session_id: str, task_id: str) -> bool:
        """True iff ``task_id`` was recorded as an app-spawned Monitor shell."""
        if not session_id or not task_id:
            return False
        return task_id in self._monitor_shell_tasks.get(session_id, set())

    def has_running_monitor_shell(self, session_id: str) -> bool:
        """True iff an app-spawned Monitor shell of this session is still running.

        The routine (non-force) eviction cleanup consults this before tearing
        down a session's registry state. A Monitor shell is an OS process
        independent of the pooled SDK client, and its completion watcher wakes
        the parked parent through a durable turn — but only while the registry
        still holds the task's OWN-lane mark. Clearing the bucket mid-run both
        suppressed that wake (the terminal frame failed
        ``is_own_background_task``) and stranded the task "running" forever
        (live 2026-08-29, task a0270e18: a CI-watch loop killed by an idle
        eviction 3.5 minutes in, its chip stuck running for 45+ minutes).
        Defensive: a missing ``session_id`` returns ``False``.
        """
        if not session_id:
            return False
        bucket = self._tasks.get(session_id)
        if not bucket:
            return False
        return any(
            (bucket.get(tid) or {}).get("status") == "running"
            for tid in self._monitor_shell_tasks.get(session_id, set())
        )

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

    def mark_subagent_background_launch(self, session_id: str, tool_use_id: str) -> None:
        """Record that a SUB-AGENT sidechain launched ``tool_use_id`` with
        run_in_background=true.

        The subagent-lane sibling of ``mark_background_launch``: the #470 gate
        deliberately keeps grandchild launches out of ``_background_launches``
        (they must not pin the pool or chip as the parent's own work), but that
        also made the drain pump drop the grandchild's terminal notification as
        ``suppress-foreground`` — stranding a sub-agent parked awaiting it
        (2026-07-10 incident). Recording the launch here lets
        ``_record_task_message`` arm the task_id-keyed marker below, which the
        unpark gates alone consume. Defensive: missing ids are a no-op.
        """
        if not session_id or not tool_use_id:
            return
        self._subagent_background_launches.setdefault(session_id, set()).add(tool_use_id)

    def was_subagent_background_launch(self, session_id: str, tool_use_id: str) -> bool:
        """True iff a sub-agent sidechain launched ``tool_use_id`` backgrounded."""
        if not session_id or not tool_use_id:
            return False
        return tool_use_id in self._subagent_background_launches.get(session_id, set())

    def mark_subagent_background_task(self, session_id: str, task_id: str) -> None:
        """Record that ``task_id`` is a background task launched FROM a sub-agent
        sidechain (its TaskStarted's tool_use_id was in
        ``_subagent_background_launches``).

        Consumed ONLY by the unpark gates (drain pump foreground gate + the
        in-stream latch) so the grandchild's terminal notification wakes the
        parent instead of being suppressed. Deliberately NOT part of
        ``is_own_background_task`` / ``get_active_background_work``: no pool
        pinning (the launching sub-agent's own kind=="agent" record pins) and
        no chip-scoping change. Defensive: missing ids are a no-op.
        """
        if not session_id or not task_id:
            return
        self._subagent_background_tasks.setdefault(session_id, set()).add(task_id)

    def is_subagent_background_task(self, session_id: str, task_id: str) -> bool:
        """True iff ``task_id`` is a recorded subagent-lane background task."""
        if not session_id or not task_id:
            return False
        return task_id in self._subagent_background_tasks.get(session_id, set())

    def is_own_background_task(self, session_id: str, task_id: str) -> bool:
        """True iff ``task_id`` is background work the PARENT itself launched.

        Mirrors the unpark admission set (chip-flood scoping): a task is "own"
        when it is a recorded SDK-backgrounded shell (``is_sdk_background_task``,
        keyed on the always-present task_id) OR its registry record's launch
        ``tool_use_id`` was issued by the parent with run_in_background=true
        (``was_background_launch``). A GRANDCHILD task — launched BY a sub-agent
        — has a registry record (every TaskStarted feeds the registry) but its
        launch tool_use_id belongs to the sub-agent, so it fails both tests and
        must never surface "Task Updated" chips in the parent chat.

        Defensive: missing ids return ``False``.
        """
        if not session_id or not task_id:
            return False
        if (
            self.is_sdk_background_task(session_id, task_id)
            or self.is_monitor_shell_task(session_id, task_id)
        ):
            return True
        record = self._tasks.get(session_id, {}).get(task_id)
        tool_use_id = record.get("tool_use_id") if record else None
        return bool(
            tool_use_id and self.was_background_launch(session_id, tool_use_id)
        )

    def mark_chipped(self, session_id: str, task_id: str, phase: str) -> bool:
        """One-shot "Task Updated" chip latch per ``(task_id, phase)``.

        Returns ``True`` only the FIRST time a given phase is latched for a
        task; every later call returns ``False``. Two phases exist:

        - ``"start"``: the task's first sighting chip (typically its first
          non-terminal status patch while the parent is parked);
        - ``"terminal"``: the task's terminal chip (killed/completed/error).

        Together they cap a task at MAX TWO chips for its whole lifetime —
        intermediate running/paused patches render nothing. Registry-level (not
        per-pump-run) so the cap survives pump restarts; cleared only with the
        session. Defensive: missing ids/phase never latch (return ``False``).
        """
        if not session_id or not task_id or not phase:
            return False
        phases = self._chipped.setdefault(session_id, {}).setdefault(task_id, set())
        if phase in phases:
            return False
        phases.add(phase)
        return True

    def set_terminal_notification_callback(self, callback: Optional[Any]) -> None:
        """Register the terminal-notification callback (chat layer, once at
        startup). ``callback(session_id, task_wire_dict)`` — SYNC, fired from
        ``complete_task`` exactly once per task on its error/killed transition
        for the parent's OWN background work. ``None`` deregisters."""
        self._terminal_notification_callback = callback

    def mark_terminal_notified(self, session_id: str, task_id: str) -> bool:
        """One-shot latch: True only the FIRST time a task's terminal
        notification callback fires. Defensive: missing ids never latch."""
        if not session_id or not task_id:
            return False
        seen = self._terminal_notified.setdefault(session_id, set())
        if task_id in seen:
            return False
        seen.add(task_id)
        return True

    def mark_woken(self, session_id: str, task_id: str) -> bool:
        """One-shot late-frame latch: a background task drives AT MOST ONE
        unpark/pump wake for its whole life.

        Returns ``True`` only the FIRST time ``task_id`` is latched for
        ``session_id`` (and records it); every later call returns ``False``.
        The scheduler sets it at each OWN/PUMP wake enqueue/merge and reads it on
        the own-lane fresh-enqueue fall-through: a frame arriving AFTER the wake
        already left the queue (a lost merge race, or the wake already ran) finds
        the latch set and is dropped instead of spawning a duplicate wake. Keyed
        on ``task_id`` ALONE (mirrors the retired ``_unparked`` intent) — cleared
        only with the session, and flipped OFF on a genuine resume (``start_task``
        revival) so a re-run agent can wake again. Defensive: missing ids never
        latch (return ``False``).
        """
        if not session_id or not task_id:
            return False
        seen = self._woken.setdefault(session_id, set())
        if task_id in seen:
            return False
        seen.add(task_id)
        return True

    def is_woken(self, session_id: str, task_id: str) -> bool:
        """True iff ``task_id`` has already produced a wake (``mark_woken``)."""
        if not session_id or not task_id:
            return False
        return task_id in self._woken.get(session_id, set())

    def suppress_wake(self, session_id: str, task_id: str) -> bool:
        """Prevent a user-canceled task from waking its parent.

        Returns ``True`` only for the first suppression. The separate latch lets
        normal completion frames still use ``_woken`` for rich-frame upgrades and
        duplicate detection while Stop can reject every racing terminal frame at
        the scheduler's entry point.
        """
        if not session_id or not task_id:
            return False
        suppressed = self._wake_suppressed.setdefault(session_id, set())
        if task_id in suppressed:
            return False
        suppressed.add(task_id)
        return True

    def is_wake_suppressed(self, session_id: str, task_id: str) -> bool:
        """True iff an explicit user cancellation suppressed this task's wake."""
        if not session_id or not task_id:
            return False
        return task_id in self._wake_suppressed.get(session_id, set())

    # ------------------------------------------------------------------
    # Session-wide user-stop latch
    # ------------------------------------------------------------------

    def mark_user_stopped(self, session_id: str) -> bool:
        """Latch "the user pressed Stop on this session". First call wins.

        The Stop button's contract is that EVERYTHING this session is doing
        ends now and NOTHING restarts it until the user speaks again. The
        per-task ``suppress_wake`` latch cannot carry that on its own: it only
        knows the tasks that were active at the instant Stop ran, so a nested
        worker that registers a beat later, a wakeup row, an idle-watchdog
        nudge or a terminal frame for a task the registry never saw all sailed
        past it and started a fresh turn. Every wake producer checks this latch
        at its entry point instead.

        Cleared ONLY by :meth:`clear_user_stop` (the next user-authored turn)
        or a full ``reset_all`` — notably not by ``clear_session``, so an idle
        eviction between the Stop and the user's next message cannot resurrect
        the session.
        """
        if not session_id:
            return False
        if session_id in self._user_stopped:
            return False
        self._stop_generations[session_id] = self.stop_generation(session_id) + 1
        # Bounded: the latch is normally released by the user's very next
        # message, but a session stopped and never returned to would otherwise
        # hold its entry for the life of the process. Evicting the oldest is
        # safe — by then every task it named is dead and every wakeup row it
        # had is deleted, so there is nothing left for the latch to block.
        while len(self._user_stopped) >= _USER_STOPPED_MAX:
            self._user_stopped.pop(next(iter(self._user_stopped)), None)
        self._user_stopped[session_id] = _now_epoch()
        for task in tuple(self._stop_supervisors.get(session_id, ())):
            if not task.done():
                try:
                    task.get_loop().call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    logger.debug("Stopped supervisor's loop is already closed", exc_info=True)
        return True

    def is_user_stopped(self, session_id: str) -> bool:
        """True while ``session_id`` is under a user Stop with no newer message."""
        if not session_id:
            return False
        return session_id in self._user_stopped

    def stop_generation(self, session_id: str) -> int:
        """An operation's generation survives resume, so old retries stay dead."""
        return self._stop_generations.get(session_id, 0)

    def stop_invalidated(self, session_id: str, generation: int) -> bool:
        return self.is_user_stopped(session_id) or self.stop_generation(session_id) != generation

    def supervise_until_stop(self, session_id: str, task: Any) -> None:
        """Keep detached dispatch supervisors cancellable between attempts."""
        self._stop_supervisors.setdefault(session_id, set()).add(task)
        def finished(done):
            bucket = self._stop_supervisors.get(session_id)
            if bucket is not None:
                bucket.discard(done)
                if not bucket:
                    self._stop_supervisors.pop(session_id, None)
        task.add_done_callback(finished)
        if self.is_user_stopped(session_id):
            task.cancel()

    def clear_user_stop(self, session_id: str) -> bool:
        """Release the Stop latch — the user addressed the session again.

        Returns True only when a latch was actually released, so the caller can
        log the transition. Called from ``turn_manager._submit_blocking`` for
        the person-authored turn sources (user / queue / resume), the same set
        that supersedes armed wakeups.
        """
        if not session_id:
            return False
        return self._user_stopped.pop(session_id, None) is not None

    def force_terminal_surface(self, session_id: str, task_id: str) -> bool:
        """Fire the terminal-surface callback for a task the APP killed.

        ``complete_task`` fires that callback only for the parent's OWN
        background work and only when ``notify`` is left on, which left two
        real kills silent: a ProviderAgent worker torn down through
        ``teardown_worker`` (``notify=False``, so the per-worker chip is
        suppressed in favour of a coalesced note that only the bulk path emits)
        and any task that is not in the unpark admission set. The audited
        consequence is 087d3809 F2 — an interrupt-killed agent worker produced
        no terminal event at all while its sibling shell got a proper
        ``cancelled`` one, so the model went on believing the corpse was
        working and redid its output by hand.

        Stop and interrupt call this for every task they killed. It is bounded
        by the SAME ``mark_terminal_notified`` one-shot latch the normal path
        uses, so a task that already surfaced its terminal is untouched and
        nothing is ever chipped twice. Returns True iff the callback fired.
        """
        if not session_id or not task_id:
            return False
        record = self._tasks.get(session_id, {}).get(task_id)
        if record is None:
            return False
        if self._terminal_notification_callback is None:
            return False
        if not self.mark_terminal_notified(session_id, task_id):
            return False
        try:
            self._terminal_notification_callback(session_id, to_wire(record))
        except Exception:
            logger.debug(
                "forced terminal notification callback failed", exc_info=True
            )
            return False
        return True

    def is_unattributed_cancellation(self, session_id: str, task_id: str) -> bool:
        """True iff this task was STOPPED and nobody claimed the stop.

        The discriminator every kill site already writes: a stop with an outside
        cause stamps a ``reason`` on the record — ``user_stopped``,
        ``server_shutdown``, ``monitor_timeout``, ``parent_turn_ended``,
        ``provider_rate_limited`` — and a stop with none is the one the SESSION
        ITSELF asked for, the ``TaskStop`` a model fires at its own stale
        watcher. (``monitor_spawn._cause_line`` draws the same line from the same
        field, which is where "Stopped deliberately by the app or by an agent,
        not by a crash" comes from.)

        The scheduler uses it to retire such a terminal without waking a turn:
        the model stopped the thing on purpose seconds ago, and a whole wake turn
        whose only output is "noted" is pure churn. An ATTRIBUTED stop keeps its
        wake — a task killed by a shutdown, a timeout or a rate limit is news the
        parent did not already have.
        """
        record = self.get_task(session_id, task_id)
        if record is None:
            return False
        return (
            record.get("status") in _CANCELLATION_STATUSES
            and not record.get("reason")
        )

    def link_child_to_owner(
        self, session_id: str, child_launch_tuid: str, owner_launch_tuid: str
    ) -> None:
        """Record that a grandchild task launched by ``child_launch_tuid`` belongs
        to the sub-agent whose launch is ``owner_launch_tuid`` (B-1).

        Wired at every subagent-lane run_in_background launch site — a sub-agent's
        inner ToolUseBlock carries the launched task's id (the child) while
        ``parent_tool_use_id`` (in-stream ``tool_entry["parent_tool_id"]``) is the
        sub-agent's OWN launch id (the owner). Defensive: missing ids are a no-op,
        and self-links (child == owner) are ignored.
        """
        if not session_id or not child_launch_tuid or not owner_launch_tuid:
            return
        if child_launch_tuid == owner_launch_tuid:
            return
        self._child_owner.setdefault(session_id, {})[
            child_launch_tuid
        ] = owner_launch_tuid

    def owner_task_for_child(
        self, session_id: str, child_task_id: str
    ) -> Optional[str]:
        """Resolve a terminal child task to its OWNER's task_id, or ``None``.

        child task → its registry record's launch ``tool_use_id`` → ``_child_owner``
        → the owner's launch ``tool_use_id`` → reverse-lookup the owner's registry
        record by that launch id → its task_id. Any missing hop (no child record,
        no linkage, no owner record) yields ``None`` — the PUMP lane treats an
        unresolvable child as "fire immediately" (never suppress blind). Read-only
        and exception-free.
        """
        if not session_id or not child_task_id:
            return None
        try:
            bucket = self._tasks.get(session_id, {})
            child = bucket.get(child_task_id)
            child_tuid = child.get("tool_use_id") if child else None
            if not child_tuid:
                return None
            owner_tuid = self._child_owner.get(session_id, {}).get(child_tuid)
            if not owner_tuid:
                return None
            for tid, rec in bucket.items():
                if rec.get("tool_use_id") == owner_tuid:
                    return tid
        except Exception:  # noqa: BLE001 — a stray shape must never raise.
            return None
        return None

    def note_owner_activity(self, session_id: str, owner_launch_tuid: str) -> None:
        """Stamp the monotonic time a running sub-agent (owner) emitted inner
        activity on the drained stream (B-prime item b). Called from the idle
        reader's inner-message dispatch for every drained message whose
        ``parent_tool_use_id`` is one of the parent's own background launches.
        Defensive: missing ids are a no-op."""
        if not session_id or not owner_launch_tuid:
            return
        self._owner_activity_ts.setdefault(session_id, {})[
            owner_launch_tuid
        ] = time.monotonic()

    def owner_activity_ts(
        self, session_id: str, owner_launch_tuid: str
    ) -> Optional[float]:
        """The monotonic ts of the owner's last inner activity, or ``None`` if the
        owner has never been seen active. Defensive: missing ids return ``None``."""
        if not session_id or not owner_launch_tuid:
            return None
        return self._owner_activity_ts.get(session_id, {}).get(owner_launch_tuid)

    def set_pump_cleanup_callback(self, callback: Optional[Any]) -> None:
        """Register the deferred-pump cleanup callback (scheduler, once at
        install()). ``callback(session_id_or_None)`` — SYNC, fired from
        ``clear_session`` (with the session id) and ``reset_all`` (with ``None``)
        so the scheduler cancels the session's/all deferred-pump timers. ``None``
        deregisters."""
        self._pump_cleanup_callback = callback

    def _fire_pump_cleanup(self, session_id: Optional[str]) -> None:
        """Invoke the pump-cleanup callback defensively (never raises)."""
        if self._pump_cleanup_callback is None:
            return
        try:
            self._pump_cleanup_callback(session_id)
        except Exception:  # noqa: BLE001 — cleanup must never break teardown.
            logger.debug("pump cleanup callback failed", exc_info=True)

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
        ``task_tool_uses`` map with them. Lane routing itself is parent-null-based
        and does not depend on this seed; the seeded entries let a cross-turn
        launch-ACK ToolResult (matched via ``tool_use_id in task_tool_uses``) skip
        subagent_done, and feed the SystemMessage ``len(task_tool_uses) > 0``
        suppression heuristic.

        Returns a copy so callers can't mutate the registry's internal set.
        Defensive: a missing ``session_id`` yields an empty set.
        """
        if not session_id:
            return set()
        return set(self._background_launches.get(session_id, set()))

    def discard_background_launch(self, session_id: str, tool_use_id: str) -> bool:
        """Forget ONE launch id from the cumulative background-launch sets.

        The only surgical way to undo an entry in ``_background_launches`` /
        ``_subagent_background_launches``. Both sets are deliberately CUMULATIVE
        — never reset per turn, cleared only by ``clear_session`` — and the
        ``_finalize`` park gate ORs the first of them
        (``app/core/session_runtime.py:674``), so any id left behind parks the
        turn that dropped it AND every later turn.

        That is exactly the trap ProviderAgent worker teardown has to avoid
        (multi-provider phase 3, plan §2 C.4): the worker is gone, but its
        launch id would keep the session parked forever. Discarding ONLY the
        torn-down worker's own id leaves ordinary CLI-native subagent launches
        untouched, so their parking — a deliberate feature — is unchanged.

        Returns True iff an id was actually removed from either set. Defensive:
        missing ids and unknown sessions are no-ops.
        """
        if not session_id or not tool_use_id:
            return False
        removed = False
        for store in (self._background_launches, self._subagent_background_launches):
            bucket = store.get(session_id)
            if bucket is None:
                continue
            if tool_use_id in bucket:
                bucket.discard(tool_use_id)
                removed = True
            if not bucket:
                store.pop(session_id, None)
        return removed

    def start_task(
        self,
        session_id: str,
        task_id: str,
        kind: str,
        title: Optional[str],
        *,
        summary: Optional[str] = None,
        tool_use_id: Optional[str] = None,
        agent: Optional[str] = None,
        model: Optional[str] = None,
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
            if agent and not existing.get("agent"):
                existing["agent"] = agent
            if model and not existing.get("model"):
                existing["model"] = model
            # Resume revival: a fresh start for a CLEANLY-COMPLETED task flips it
            # back to running (failures stay terminal — that's a stale leak).
            if existing.get("status") == "completed":
                existing["status"] = "running"
                existing.pop("notification_delivered", None)
                retained = self._recent_terminal.get(session_id)
                if retained is not None:
                    retained.pop(task_id, None)
                    if not retained:
                        self._recent_terminal.pop(session_id, None)
                # A genuine resume re-arms the late-frame wake latch: the revived
                # run may come to rest again and must be able to drive a fresh
                # unpark/pump wake (A-3 clear-on-revival).
                self._woken.get(session_id, set()).discard(task_id)
                self._wake_suppressed.get(session_id, set()).discard(task_id)
                # ...and the chip latches, so the revived run paints a fresh
                # running chip (the durable start row is deduped on this latch —
                # see turn_recorder._translate_shared) and can settle again.
                self._chipped.get(session_id, {}).pop(task_id, None)
            existing["updated_at"] = now
            self._persist_task_state(session_id, task_id)
            return
        bucket[task_id] = {
            "task_id": task_id,
            "kind": kind,
            "title": title,
            "status": "running",
            "summary": summary,
            "tool_use_id": tool_use_id,
            # Task-tool workers only: the agent's display name (see to_wire).
            "agent": agent,
            # Task-tool workers only: the "<provider> · <model>" label, kept out
            # of ``summary`` so summary can say what the task IS.
            "model": model,
            "started_at": now,
            "updated_at": now,
        }
        self._persist_task_state(session_id, task_id)

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
        self._persist_task_state(session_id, task_id)

    def complete_task(
        self,
        session_id: str,
        task_id: str,
        status: str,
        *,
        summary: Optional[str] = None,
        reason: Optional[str] = None,
        report_path: Optional[str] = None,
        report_chars: Optional[int] = None,
        notify: bool = True,
    ) -> None:
        """Set a terminal status on a task. No-op if the task is unknown.

        ``report_path``/``report_chars`` are the retrieval handle for a finished
        BACKGROUND agent worker: by the time it completes there is no caller
        frame left to return a report to, so the file it was written to is
        recorded HERE and served by ``TaskGet``. Without them the woken parent
        had only id/status/summary and no way at all to reach the work.

        A snapshot of the task is also stashed in the retention store so ``/live``
        (``get_visible``) keeps surfacing it after ``clear_session`` wipes the live
        ``_tasks`` map. FAILURES ("error"/"killed") are retained for
        ``_RECENT_TERMINAL_WINDOW_SECONDS`` so a force-cleanup reap (dead SDK
        client) doesn't drop the row with no failed indication; clean completions
        get the much shorter ``_RECENT_COMPLETED_WINDOW_SECONDS`` purely to keep
        park-polling alive across the completion → ``unpark_pending`` hand-off gap.

        ``summary``/``reason`` attribute the kill (item 4a): shutdown-origin
        reaps stamp ``SERVER_SHUTDOWN_SUMMARY``/``SERVER_SHUTDOWN_REASON`` so
        the terminal record says WHY the task died.

        GUARANTEED TERMINAL NOTIFICATION (item 4b): an error/killed transition
        of the parent's OWN background work (``is_own_background_task`` — the
        unpark admission set, so foreground tasks whose failure already
        returned inline never fire) invokes the registered callback exactly
        once per task (``mark_terminal_notified``). Fired AFTER the record and
        retention snapshot are final so the callback sees the attributed wire
        shape. Fully defensive: a raising callback is swallowed.

        ``notify=False`` suppresses that chip for THIS transition only. Its one
        caller is ProviderAgent worker teardown
        (:mod:`app.core.provider_agent_supervisor`), where fan-out is uncapped
        by ruling P12: a wide turn ending would otherwise fire one durable chip
        per killed worker in a single burst. Teardown emits ONE coalesced note
        instead. The terminal record, the retention snapshot and the durable
        mirror are all still written — only the chip is skipped, and the
        one-shot ``mark_terminal_notified`` latch is deliberately left UNSET so
        a genuine later surfacing of the same task is still possible.
        """
        record = self._tasks.get(session_id, {}).get(task_id)
        if record is None:
            return
        record["status"] = status
        if summary is not None:
            record["summary"] = summary
        if reason is not None:
            record["reason"] = reason
        if report_path is not None:
            record["report_path"] = report_path
        if report_chars is not None:
            record["report_chars"] = report_chars
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

        if (
            notify
            and status in _FAILURE_STATUSES
            and self._terminal_notification_callback is not None
            and self.is_own_background_task(session_id, task_id)
            and self.mark_terminal_notified(session_id, task_id)
        ):
            try:
                self._terminal_notification_callback(session_id, to_wire(record))
            except Exception:
                logger.debug(
                    "terminal notification callback failed", exc_info=True
                )

        # Durable mirror AFTER the guaranteed-notification block (the #514
        # callback point) so the snapshot carries the final terminal_notified
        # latch state alongside the terminal status.
        self._persist_task_state(session_id, task_id)

    def _persist_task_state(self, session_id: str, task_id: str) -> None:
        """Mirror one task's current state into the durable queue (Phase 1).

        Fire-and-forget and fully defensive — the registry hot path (called
        inside streaming loops) must never gain latency or a failure mode from
        persistence. A missing record is a no-op (nothing to mirror). The
        snapshot is COALESCED downstream (latest per (session, task) wins);
        see ``app.core.task_persistence``. WRITE-ONLY: nothing reads the rows
        back into the registry.
        """
        try:
            record = self._tasks.get(session_id, {}).get(task_id)
            if record is None:
                return
            from app.core.task_persistence import enqueue_task_snapshot

            enqueue_task_snapshot(
                session_id,
                task_id,
                {
                    "task_id": task_id,
                    "session_id": session_id,
                    "kind": record.get("kind"),
                    "is_sdk_background": task_id
                    in self._sdk_background_tasks.get(session_id, set()),
                    "tool_use_id": record.get("tool_use_id"),
                    "title": record.get("title"),
                    "status": record.get("status"),
                    "terminal_notified": task_id
                    in self._terminal_notified.get(session_id, set()),
                    "started_at": record.get("started_at"),
                    "updated_at": record.get("updated_at"),
                },
            )
        except Exception:
            logger.debug("task persistence snapshot failed", exc_info=True)

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
        now = _now_epoch()
        for entry in retained.values():
            wire = entry.get("task") or {}
            if wire.get("id") in seen:
                continue
            # A clean completion is now RETAINED for minutes (the parent's
            # hand-off window) but DISPLAYED for seconds — long enough to bridge
            # the poller gap, short enough that the taskbar does not carry a
            # finished row (and keep a dead session park-polling) for ten
            # minutes. Failures keep showing for their whole retention.
            if wire.get("status") not in _FAILURE_STATUSES and (
                now - entry.get("recorded_at", 0.0)
                > _COMPLETED_VISIBLE_WINDOW_SECONDS
            ):
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

    def has_unresolved_background_launches(self, session_id: str) -> bool:
        """True iff any recorded background-launch tool_use_id has NO registry
        task record that has reached a TERMINAL status.

        ``_background_launches`` is CUMULATIVE (cleared only on ``clear_session``)
        and exists to cover the launch-before-registration race — a Task launched
        but not yet given a registry row. The ``_finalize`` park GATE reads this
        so it stops counting launches whose task already FINISHED (a terminal
        record) while still honoring the race a bare launch id exists for.

        A launch with NO matching record at all is UNRESOLVED (True) — that is
        exactly the launched-but-not-yet-registered case. A launch whose only
        record is still ``running`` is also UNRESOLVED (True), but that is
        irrelevant to the gate — ``has_active_background_work`` already arms it
        for running work. Terminal = any status other than ``running`` (the
        registry's value vocabulary makes ``running`` the sole live status; every
        other status — ``completed``/``error``/``killed``/``stopped``/… — is
        terminal). Read-only, no locks, exception-free; mirrors the iteration
        convention of the other registry predicates.
        """
        if not session_id:
            return False
        launches = self._background_launches.get(session_id)
        if not launches:
            return False
        try:
            resolved: set = set()
            for record in self._tasks.get(session_id, {}).values():
                tuid = record.get("tool_use_id")
                if tuid and record.get("status") != "running":
                    resolved.add(tuid)
            return any(tuid not in resolved for tuid in launches)
        except Exception:  # noqa: BLE001 — a stray shape must never raise.
            return bool(launches)

    def mark_background_notification_delivered(self, session_id: str, task_id: str) -> None:
        """Model receipt, distinct from the terminal UI-chip callback latch."""
        record = self._tasks.get(session_id, {}).get(task_id)
        if record is not None:
            record["notification_delivered"] = True

    def has_pending_composer_work(self, session_id: str) -> bool:
        """Completion hand-offs still awaiting delivery, including missing rows.

        Unlike park/pool gates this is conservative: serving jobs also block
        composer hints (the caller checks get_active). Never truncate to the
        taskbar's retained/display limit or treat wake submission as delivery.
        """
        if session_id in self._unpark_pending:
            return True
        # Nested task results go to their owning agent, not this session.
        # Match on_task_terminal's OWN lane: only parent-owned completions can
        # produce a parent delivery receipt. Active nested work still blocks
        # through get_active until its owner and the nested work finish.
        ids = (self._sdk_background_tasks.get(session_id, set())
               | self._monitor_shell_tasks.get(session_id, set()))
        ids = ids | {
            task_id for task_id, record in self._tasks.get(session_id, {}).items()
            if record.get("tool_use_id") in self._background_launches.get(session_id, set())
        }
        return any(
            not self._tasks.get(session_id, {}).get(task_id, {}).get("notification_delivered")
            for task_id in ids - self._wake_suppressed.get(session_id, set())
        )

    def get_all(self, session_id: str, *, limit: int = 50) -> List[Dict[str, Any]]:
        """Running + recently-finished tasks, newest-first, each as a wire dict.

        Ordered by ``updated_at`` so just-finished tasks surface near the top,
        then truncated to ``limit``.

        The retention store is merged in behind the live map. ``TaskList`` is how
        a woken parent finds the id of the worker it was woken about, and it used
        to read ``_tasks`` alone — so a session whose pooled client had been
        evicted (``clear_session`` wipes ``_tasks``, deliberately NOT the
        retention store) answered an empty list to ``include_finished=True``
        seconds after a worker finished. A live record always wins over its
        retained snapshot.
        """
        records = list((self._tasks.get(session_id) or {}).values())
        wire = [to_wire(r) for r in records]
        seen = {w.get("id") for w in wire}
        self._prune_recent_terminal(session_id)
        for task_id, entry in (self._recent_terminal.get(session_id) or {}).items():
            if task_id in seen:
                continue
            snapshot = entry.get("task")
            if isinstance(snapshot, dict):
                wire.append(dict(snapshot))
        wire.sort(key=lambda w: w.get("updatedAt") or "", reverse=True)
        return wire[:limit]

    def sessions_with_task(self, task_id: str) -> List[str]:
        """Every session whose registry knows ``task_id``, live map first.

        Additive and read-only, for the task tools' SCOPING check: they used to
        resolve a task id against a process-global dict with no session in it,
        so one session's shell answered another session's ``TaskGet`` (and told
        it to park on a completion that would never wake it). Knowing WHICH
        session owns an id is what lets the tools allow a parent to read its
        own worker's task and refuse everybody else's by name.

        Normally 0 or 1 entries; ids are uuid4-derived, so a collision across
        two sessions is theoretical rather than real.
        """
        owners: List[str] = []
        for session_id, tasks in list(self._tasks.items()):
            if task_id in tasks:
                owners.append(session_id)
        for session_id, entries in list(self._recent_terminal.items()):
            if task_id in entries and session_id not in owners:
                owners.append(session_id)
        return owners

    def get_task(
        self, session_id: str, task_id: str, *, durable: bool = False
    ) -> Optional[Dict[str, Any]]:
        """A single task as a wire dict, or None if the task is unknown.

        Three sources, in order of fidelity:

        1. the LIVE ``_tasks`` record — everything, including the agent-report
           handles ``complete_task`` stamped on it;
        2. the ``_recent_terminal`` retention snapshot, which SURVIVES
           ``clear_session`` — a full wire dict taken at the terminal transition,
           so it still carries ``summary``/``reportPath``/``reportChars``;
        3. with ``durable=True``, the ``background_tasks`` row.

        Only the live map was ever consulted, which is how a parent woken about
        its own worker got ``{"success": false, "error": "task not found."}`` from
        ``TaskGet`` thirty-five seconds after that worker finished. The durable
        read is opt-in because the callers that poll this (the idle reader's
        phantom-task filter, ``running_background_task_for_tool_use``) want a
        cheap in-memory miss, not a DB round trip; the task TOOLS pass it.
        """
        record = self._tasks.get(session_id, {}).get(task_id)
        if record is not None:
            return to_wire(record)
        self._prune_recent_terminal(session_id)
        entry = self._recent_terminal.get(session_id, {}).get(task_id)
        snapshot = entry.get("task") if isinstance(entry, dict) else None
        if isinstance(snapshot, dict):
            return dict(snapshot)
        if durable:
            return self._durable_task(session_id, task_id)
        return None

    def _durable_task(
        self, session_id: str, task_id: str
    ) -> Optional[Dict[str, Any]]:
        """One task read back out of the durable ``background_tasks`` mirror.

        The last resort behind ``get_task``: a restart, an eviction, or simply a
        retention window that finally expired leaves nothing in memory, and the
        row is then the only surviving record that the task existed and how it
        ended. It carries less than a live record (the table stores identity and
        lifecycle, not the summary or the report handles), which is exactly why
        it is last — but "completed, kind=agent" is enough for ``TaskGet`` to
        reach the report through the worker's own event log.

        Never raises: any failure is an unknown task, as before.
        """
        if not session_id or not task_id:
            return None
        try:
            from app.db import background_tasks as background_tasks_db

            row = background_tasks_db.get_background_task(task_id, session_id)
        except Exception:  # noqa: BLE001 — a durable miss is just a miss.
            logger.debug("durable task read failed", exc_info=True)
            return None
        if not row:
            return None
        def _stamp(value: Any) -> Optional[str]:
            # The column is a timestamptz on Postgres and a string on the test
            # shim; the wire contract is an ISO-8601 string either way.
            if value is None or isinstance(value, str):
                return value
            iso = getattr(value, "isoformat", None)
            return iso() if callable(iso) else str(value)

        return to_wire({
            "task_id": row.get("task_id"),
            "title": row.get("title"),
            "status": row.get("status"),
            "kind": row.get("kind"),
            "tool_use_id": row.get("tool_use_id"),
            "started_at": _stamp(row.get("started_at")),
            "updated_at": _stamp(row.get("updated_at")),
        })

    def get_task_tool_use_id(self, session_id: str, task_id: str) -> Optional[str]:
        """The launch ``tool_use_id`` for a task, or None if it can't be resolved.

        The card join key: terminal ``task_updated`` chips settle a subagent card
        only when they carry the launch ``tool_use_id`` (the card's ``toolId``).
        The SDK's ``TaskUpdatedMessage`` never carries it, so the chip path enriches
        from the registry — and it runs AFTER ``complete_task``, so this must still
        resolve for a JUST-completed task. ``complete_task`` keeps the live record
        (it only flips status + snapshots), so the live ``_tasks`` map resolves the
        common case; the ``_recent_terminal`` retention snapshot (which SURVIVES
        ``clear_session``) is the fallback for a force-reaped task whose live record
        was already wiped. Read-only and exception-free: any miss returns None.
        """
        try:
            record = self._tasks.get(session_id, {}).get(task_id)
            tool_use_id = record.get("tool_use_id") if record is not None else None
            if tool_use_id:
                return tool_use_id
            entry = self._recent_terminal.get(session_id, {}).get(task_id)
            if entry is not None:
                return (entry.get("task") or {}).get("toolUseId")
        except Exception:  # noqa: BLE001 — a stray shape must never raise.
            return None
        return None

    def running_background_task_for_tool_use(
        self, session_id: str, tool_use_id: str
    ) -> bool:
        """True iff a RUNNING background task was launched by ``tool_use_id``.

        The reverse of ``get_task_tool_use_id`` (which maps task_id → launch
        tool_use_id): given the parent's launch ``tool_use_id`` — the subagent
        card's join key — is there a live registry task that (a) was launched by
        that exact tool_use, (b) is still ``running``, and (c) is background per
        the existing predicates (``is_own_background_task`` OR
        ``is_subagent_background_task``)? Consumed by the query_engine defer
        classifier so a launch-ACK ToolResult whose task is ALREADY a registered
        running background task suppresses ``subagent_done`` regardless of the ACK
        wording. Note the launch-ACK ToolResult is usually processed BEFORE the
        task's TaskStartedMessage registers it, so this mainly catches a
        CROSS-TURN launch still in flight (registered in a prior turn).

        Read-only and exception-free (mirrors ``get_task_tool_use_id``): no
        locks, and any stray shape returns ``False``.
        """
        if not session_id or not tool_use_id:
            return False
        try:
            for task_id, record in self._tasks.get(session_id, {}).items():
                if record.get("tool_use_id") != tool_use_id:
                    continue
                if record.get("status") != "running":
                    continue
                if self.is_own_background_task(
                    session_id, task_id
                ) or self.is_subagent_background_task(session_id, task_id):
                    return True
        except Exception:  # noqa: BLE001 — a stray shape must never raise.
            return False
        return False

    def clear_session(self, session_id: str) -> None:
        """Drop all tasks for a session (called when the live session is removed).

        The failure-retention store (``_recent_terminal``) is deliberately NOT
        wiped here: force-cleanup reaps running tasks to "killed" and then calls
        this, so wiping it would re-introduce the silent-vanish bug. Retained
        failures age out on their own window (pruned here so a cleared session's
        already-expired entries don't linger).
        """
        self._tasks.pop(session_id, None)
        self._started_this_turn.pop(session_id, None)
        self._background_launches.pop(session_id, None)
        self._unpark_pending.discard(session_id)
        self._unpark_pending_since.pop(session_id, None)
        self._sdk_background_tasks.pop(session_id, None)
        self._monitor_shell_tasks.pop(session_id, None)
        self._serving_monitor_tasks.pop(session_id, None)
        self._subagent_background_launches.pop(session_id, None)
        self._subagent_background_tasks.pop(session_id, None)
        self._chipped.pop(session_id, None)
        self._woken.pop(session_id, None)
        self._wake_suppressed.pop(session_id, None)
        # ``_user_stopped`` is deliberately NOT cleared here: eviction/teardown
        # must not un-stop a session the user stopped (see mark_user_stopped).
        self._child_owner.pop(session_id, None)
        self._owner_activity_ts.pop(session_id, None)
        self._terminal_notified.pop(session_id, None)
        # Cancel the scheduler's deferred-pump timers for this session (item f).
        self._fire_pump_cleanup(session_id)
        self._prune_recent_terminal(session_id)

    def reset_all(self) -> None:
        """Drop all task state. Used on shutdown and between tests."""
        self._tasks.clear()
        self._started_this_turn.clear()
        self._background_launches.clear()
        self._unpark_pending.clear()
        self._unpark_pending_since.clear()
        self._sdk_background_tasks.clear()
        self._monitor_shell_tasks.clear()
        self._serving_monitor_tasks.clear()
        self._subagent_background_launches.clear()
        self._subagent_background_tasks.clear()
        self._chipped.clear()
        self._woken.clear()
        self._wake_suppressed.clear()
        self._user_stopped.clear()
        self._stop_generations.clear()
        self._stop_supervisors.clear()
        self._child_owner.clear()
        self._owner_activity_ts.clear()
        self._terminal_notified.clear()
        self._terminal_notification_callback = None
        # Cancel ALL deferred-pump timers, then deregister (item f).
        self._fire_pump_cleanup(None)
        self._pump_cleanup_callback = None
        self._recent_terminal.clear()


# Global instance
task_registry = TaskRegistry()
