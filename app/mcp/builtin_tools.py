"""
Claude Code builtin tools MCP server — adapts harness-level tools for AI Shuffle.

Claude Code CLI ships several "builtin" tools that normally require harness
cooperation (ScheduleWakeup, Monitor, CronCreate, EnterWorktree, etc). When
running under the Claude Agent SDK those tools don't exist natively, so we
expose equivalent implementations as MCP tools that bridge to AI Shuffle's
existing infrastructure (session pool, worktree manager, automation manager,
push notification service).

Tools are registered per-session so that session-scoped actions
(ScheduleWakeup firing a new prompt, EnterWorktree tying a worktree to the
calling session) have the context they need.

Tool naming convention: ``mcp__builtins__<toolName>``
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional

from app.core.monitor_spawn import MAX_WAIT_SECONDS, WAIT_SECONDS_PARAM
from app.mcp.config_helpers import _json_default

logger = logging.getLogger(__name__)


# ============================================================================
# Helpers
# ============================================================================

def _ok(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Format a successful tool result for the SDK MCP envelope.

    Uses ``_json_default`` so DB-sourced datetime/date values (e.g. an
    automation's ``last_run_at``) ISO-normalize instead of crashing the tool
    with 'Object of type datetime is not JSON serializable'.
    """
    import json as _json
    return {"content": [{"type": "text", "text": _json.dumps({"success": True, **payload}, default=_json_default)}]}


def _park_doctrine() -> str:
    """Fluid's canonical park sentence, so the two legs cannot drift.

    Imported lazily and with a literal fallback: the delegated leg must build
    its tool surface even where the vendored harness is not importable.
    """
    try:
        from fluid.tools.fragments import END_TURN_ON_BACKGROUND

        return END_TURN_ON_BACKGROUND
    except Exception:  # noqa: BLE001 — a description must never fail a build
        return (
            "When background work is all that remains, end your turn — the "
            "completion wakes the session; do not sleep, poll, or hold the "
            "turn open waiting."
        )


def _monitor_description() -> str:
    """Monitor's description on the DELEGATED (Claude Code CLI) leg.

    The native leg splices fluid's park doctrine into Monitor's description;
    this copy carried none of it and read as a tool for dev servers ("a server
    started here keeps running…"). The audited consequence: an Opus session on
    this lane never called Monitor once and spent 26.7 of its 45 minutes in
    blocking Bash sleep-polls, one of which looped for ten minutes and returned
    nothing. So the trigger is stated outright — anything expected to outlast a
    Bash call — and the doctrine comes from fluid's own fragment.
    """
    return (
        "Run a long-running shell command in the background and be woken when "
        "it finishes. Returns a task_id immediately; when the command exits "
        "you receive its exit code and an output summary as a notification in "
        "a later turn. Read the buffer with TaskOutput, cancel with TaskStop.\n"
        "\n"
        "USE IT FOR ANY COMMAND EXPECTED TO OUTLAST A BASH CALL — a test "
        "suite, a build or type-check, an install, a CI wait, a deploy — not "
        "just for servers. Waiting happens inside the Monitor command, never "
        "in a foreground Bash call: no sleep loops, no polling with `wait`, no "
        "re-running the check to see if it is done yet. "
        f"{_park_doctrine()}\n"
        "\n"
        "The command must exit for a completion to fire, so give a watch its "
        "own end condition and time budget — for GitHub CI that is "
        "`gh pr checks <PR> --watch --fail-fast`, which blocks until the "
        "checks settle. Pass wait_seconds for work that will probably be "
        "quick: if it finishes inside the wait you get the result right here "
        "and nothing parks at all.\n"
        "\n"
        "The shell survives the user pressing Stop — a server started here "
        "keeps running until TaskStop, the task's x button, or the session "
        "ending."
    )


def _err(message: str, **extra: Any) -> Dict[str, Any]:
    """Format an error tool result for the SDK MCP envelope."""
    import json as _json
    return {
        "content": [{
            "type": "text",
            "text": _json.dumps(
                {"success": False, "error": message, **extra},
                default=_json_default,
            ),
        }],
        "isError": True,
    }


#: Prepended to a TaskList/TaskGet result that carries no news. Polling these
#: tools is the failure mode they invite: an audited parent called TaskList
#: three times and then re-did its own child's work. Completions wake the
#: session, so a result where everything is still running has nothing in it.
_PARK_NUDGE = "all tasks still running — park; you will be woken on completion."

#: Attached when the task belongs to a worker this session dispatched. It is
#: readable — that is the point — but its completion wakes the WORKER, not the
#: reader, so the park nudge above must never ride along with it.
_WORKER_TASK_SCOPE_NOTE = (
    "this task belongs to a worker session you dispatched. You can read it, "
    "but its completion does NOT wake you — do not park waiting for it."
)


# Bound GetAppErrors output on the server, including repeated stack traces and the serialized envelope. Tool-
# description guidance alone cannot guarantee the response size.
_APP_ERRORS_MAX_LIMIT = 50
#: Whole-payload ceiling. Entries are emitted until it is reached.
_APP_ERRORS_MAX_CHARS = 20_000
#: Per-entry ceiling on the stack trace, applied before the payload budget so
#: one enormous traceback cannot crowd out every other entry.
_APP_ERRORS_MAX_DETAIL_CHARS = 2_000

#: ``min_level`` → the level set it selects. A "minimum" only reads as one
#: because the table stores level names, not ordinals.
_APP_ERROR_LEVELS = {
    "ERROR": ["ERROR", "CRITICAL"],
    "WARNING": ["WARNING", "ERROR", "CRITICAL"],
}


# ── GetAppLogs output bounds ─────────────────────────────────────────────────
#
# The ring buffer behind ``GET /api/v1/logs`` holds ~5000 formatted lines. The
# endpoint is admin-cookie-only, which a Bearer key does not satisfy, so an
# audited session that needed INFO logs spent user round-trips and needless money
# driving a browser through a Cloudflare login to read them. Same buffer, same
# filters, in-process — with the same server-side bounds GetAppErrors uses,
# because a full-text log is the easiest way there is to fill a context window.
_APP_LOGS_DEFAULT_LIMIT = 50
_APP_LOGS_MAX_LIMIT = 300
_APP_LOGS_MAX_CHARS = 20_000
_APP_LOGS_MAX_LINE_CHARS = 2_000

#: Levels ``GetAppLogs`` will filter on. Exact match, not a floor: the buffer
#: stores level names, and "show me the INFO lines" is the actual request.
_APP_LOG_LEVELS = frozenset(
    {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
)


def _log_record_after(record: Any, cutoff: datetime) -> bool:
    """Whether a ring-buffer record is newer than ``cutoff``.

    Unparseable timestamps are KEPT: dropping a line because its stamp did not
    parse would silently narrow an investigation.
    """
    try:
        stamp = datetime.fromisoformat(str(record.timestamp).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp >= cutoff


def _clip(text: Any, limit: int) -> Any:
    """Clip a string to ``limit`` chars with a visible marker; pass anything
    else through untouched."""
    if not isinstance(text, str) or len(text) <= limit:
        return text
    dropped = len(text) - limit
    return f"{text[:limit]}\n… [{dropped} more characters clipped]"


def _cap_app_errors(rows: List[Dict[str, Any]]) -> tuple:
    """Bound a GetAppErrors payload. Returns ``(rows, truncation_note)``.

    Two passes: clip every stack trace to
    ``_APP_ERRORS_MAX_DETAIL_CHARS``, then emit entries until the running
    character total would exceed ``_APP_ERRORS_MAX_CHARS``. At least one entry
    always comes back — a single oversized row is clipped, never dropped, or
    the tool would answer a real error with an empty list.
    """
    import json as _json

    capped: List[Dict[str, Any]] = []
    used = 0
    detail_clipped = 0
    for row in rows:
        entry = dict(row or {})
        detail = entry.get("detail")
        if isinstance(detail, str) and len(detail) > _APP_ERRORS_MAX_DETAIL_CHARS:
            entry["detail"] = _clip(detail, _APP_ERRORS_MAX_DETAIL_CHARS)
            detail_clipped += 1
        try:
            size = len(_json.dumps(entry, default=_json_default))
        except Exception:  # noqa: BLE001 — sizing must never fail the tool
            size = _APP_ERRORS_MAX_DETAIL_CHARS
        if capped and used + size > _APP_ERRORS_MAX_CHARS:
            break
        capped.append(entry)
        used += size

    notes = []
    if len(capped) < len(rows):
        notes.append(
            f"{len(rows) - len(capped)} more matching entries omitted to stay "
            f"under the {_APP_ERRORS_MAX_CHARS}-character output cap"
        )
    if detail_clipped:
        notes.append(
            f"{detail_clipped} stack trace(s) clipped to "
            f"{_APP_ERRORS_MAX_DETAIL_CHARS} characters"
        )
    if not notes:
        return capped, None
    return capped, (
        "; ".join(notes)
        + ". Narrow the query (source, since_minutes, this_session_only) "
        "rather than raising limit."
    )


def _strip_ansi(text: str) -> str:
    """Fluid's ANSI scrubber, imported lazily and never fatal."""
    try:
        from fluid.tools.builtins._util import strip_ansi
    except Exception:  # noqa: BLE001 — cosmetics must never break a read
        return text
    return strip_ansi(text)


def _all_still_running(tasks: Any) -> bool:
    """True when ``tasks`` is non-empty and every entry is still running."""
    records = [t for t in (tasks or []) if isinstance(t, dict)]
    if not records:
        return False
    return all(t.get("status") == "running" for t in records)


def _agent_report_fields(task_id: str, record: Dict[str, Any]) -> Dict[str, Any]:
    """``report_head``/``report_path``/``report_chars`` for a finished worker.

    The retrieval seam TaskGet was missing. A background Task worker finishes
    long after the call that launched it returned, so its report reaches the
    parent only through this record — and until now TaskGet answered with
    id/status/summary and nothing else. The audited parent guessed the sync
    path, found no file (background never wrote one), searched the filesystem,
    and redid half an hour of its child's research.

    The stored path is preferred; ``final_turn_outcome`` is the fallback for
    workers that completed BEFORE the path was recorded. Never raises — a
    failure here degrades TaskGet to what it already returned.

    ``report_path`` is ALWAYS in the returned dict, ``None`` when there is no
    readable file. It used to appear only when the file read succeeded, so a
    report whose worktree had been removed came back with ``report_head`` and
    no path at all — and the caller's ``j['report_path']`` raised a KeyError
    that aborted its whole code cell.
    """
    fields: Dict[str, Any] = {"report_path": None}
    try:
        from app.core.task_dispatch import SYNC_REPORT_HEAD_CHARS

        path = record.get("reportPath") or record.get("report_path")
        chars = record.get("reportChars")
        if chars is None:
            chars = record.get("report_chars")
        head: Optional[str] = None
        if path:
            try:
                from pathlib import Path

                text = Path(path).read_text(encoding="utf-8")
                head = text[:SYNC_REPORT_HEAD_CHARS]
                chars = len(text)
                fields["report_path"] = str(path)
            except OSError:
                # The row remembers a file that is gone (worktree removed,
                # different runner). Fall through to the event log rather than
                # handing back a path nothing can read.
                logger.debug("TaskGet: report file unreadable at %s", path)
        if head is None:
            from app.core import provider_agent as pa

            outcome = pa.final_turn_outcome(task_id)
            if outcome.get("terminal_found") and not outcome.get("error"):
                report, _ = pa.bound_report(pa.final_assistant_text(task_id))
                if report:
                    head = report[:SYNC_REPORT_HEAD_CHARS]
                    chars = len(report)
        if head is None:
            return {}
        fields["report_head"] = head
        if chars is not None:
            fields["report_chars"] = chars
        fields["note"] = (
            "report_head is the complete report; nothing further to read."
            if chars is not None and chars <= len(head)
            else (
                f"report_head is the first {len(head)} of {chars} characters."
                + (
                    " The rest is at report_path — read it with "
                    "Read(offset=..., limit=...) only if the head does not "
                    "already answer your question."
                    if fields.get("report_path")
                    else " The rest is in the worker session transcript."
                )
            )
        )
    except Exception:  # noqa: BLE001 — inspection must never fail the tool
        logger.debug("TaskGet: report enrichment failed", exc_info=True)
        return {}
    return fields


# ============================================================================
# Background task registry (Monitor / TaskOutput / TaskStop)
# ============================================================================

class _BackgroundTask:
    """A spawned subprocess whose stdout is buffered for later retrieval."""

    def __init__(
        self, task_id: str, command: str, cwd: Optional[str],
        session_id: Optional[str] = None,
    ) -> None:
        self.task_id = task_id
        self.command = command
        self.cwd = cwd
        # The AI Shuffle session that spawned this shell, so its subprocess +
        # reader task can be reaped when that session is finally removed (a
        # Monitor shell deliberately does NOT pin the session open). None for
        # module-level spawns with no session context.
        self.session_id = session_id
        self.lines: List[str] = []
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.exit_code: Optional[int] = None
        self.process: Optional[asyncio.subprocess.Process] = None
        self._reader_task: Optional[asyncio.Task] = None
        # True once the subprocess has been spawned into a process group of
        # its own (see app.core.process_tree.spawn_kwargs). Group-addressed
        # signals — CTRL_BREAK on Windows, killpg on POSIX — are only safe
        # when this holds; otherwise they land on the SERVER's own group.
        self.own_process_group: bool = False

    def is_running(self) -> bool:
        return self.process is not None and self.process.returncode is None


_background_tasks: Dict[str, _BackgroundTask] = {}
_BACKGROUND_LINE_LIMIT = 5000  # avoid unbounded growth

#: How much the output reader pulls off the pipe per read. Chunked on purpose:
#: ``StreamReader.readline()`` raises ``ValueError`` (from
#: ``LimitOverrunError``) on a line longer than its 64 KiB limit AND discards
#: the buffered chunk, which killed the reader outright — a live production
#: occurrence, after which the task's output stalled while its process kept
#: running and the task looked stuck. Reading chunks and splitting lines here
#: has no such limit.
_READ_CHUNK_BYTES = 64 * 1024

#: Longest single line kept intact. A minified bundle, a one-line JSON dump or
#: a progress bar with no newline is chopped into pieces of this size rather
#: than buffered forever.
_MAX_LINE_BYTES = 256 * 1024

# Terminal statuses the output reader must not overwrite: each was written by a
# path that knows more than an exit code does (a deliberate stop, or the
# monitor watcher's outcome-derived settle).
_SETTLED_STATUSES = frozenset({"killed", "stopped", "cancelled", "error", "completed"})


def _registry_safe(fn) -> None:
    """Run a task-registry call, swallowing any error so it can never break a
    spawn/reader path. The registry is a best-effort observability surface."""
    try:
        fn()
    except Exception:
        logger.debug("task_registry update failed", exc_info=True)


def is_background_shell_running(task_id: str) -> bool:
    """True when ``task_id`` names a Monitor shell whose subprocess is alive.

    Lets the Stop path tell a healthy long-lived shell (spare it) from a ghost
    registry row whose process already exited (reap it).
    """
    task = _background_tasks.get(task_id)
    return bool(task and task.is_running())


def _session_in_caller_lineage(
    caller_session_id: Optional[str], owner_session_id: Optional[str]
) -> bool:
    """True when ``owner_session_id`` is a worker the caller dispatched.

    Directly or transitively: a grandchild's task is still the caller's work.
    Reuses ``task_dispatch.caller_chain`` — the same upward walk the dispatcher
    uses for depth and admission — so there is one definition of "my lineage".
    Never raises: an unresolvable chain is not a lineage.
    """
    if not caller_session_id or not owner_session_id:
        return False
    if caller_session_id == owner_session_id:
        return True
    try:
        from app.core.task_dispatch import caller_chain

        rows, root_id, _depth = caller_chain(owner_session_id)
    except Exception:  # noqa: BLE001 — scoping must never fail a read
        logger.debug("lineage check failed for %s", owner_session_id, exc_info=True)
        return False
    ancestors = {row.get("id") for row in (rows or []) if isinstance(row, dict)}
    ancestors.add(root_id)
    return caller_session_id in ancestors


def _resolve_task_scope(
    task_id: str, session_id: Optional[str]
) -> "tuple[Optional[_BackgroundTask], Optional[str], Optional[str]]":
    """``(shell, owner_session_id, relation)`` for a task id this session named.

    ``relation`` is ``"own"``, ``"lineage"`` (a worker this session dispatched,
    at any depth) or ``"foreign"``; ``None`` when the id is unknown here.

    The leak this closes: ``_background_tasks`` is a process-global dict, and
    both task tools read it with no session check. An audited parent asked
    ``TaskGet`` about a task id its WORKER had named, got another session's
    live shell back — with "all tasks still running — park; you will be woken
    on completion" attached — parked, was never woken (it was subscribed to
    nothing), and on its own unrelated wake found the task reaped and re-ran
    the build by hand. Reading a worker's task is legitimate; being told you
    will be woken by it is not.
    """
    shell = _background_tasks.get(task_id)
    owner = shell.session_id if shell is not None else None
    if shell is None:
        # No live shell. It may still be a task registered under a worker
        # session in this caller's lineage (an agent task, or a shell whose
        # process is gone but whose record survives).
        for candidate in _sessions_holding_task(task_id):
            if candidate == session_id:
                return None, candidate, "own"
            if _session_in_caller_lineage(session_id, candidate):
                return None, candidate, "lineage"
            owner = owner or candidate
        return None, owner, ("foreign" if owner else None)
    if owner is None or session_id is None or owner == session_id:
        # A module-level spawn (no session) has no scope to violate.
        return shell, owner, "own"
    if _session_in_caller_lineage(session_id, owner):
        return shell, owner, "lineage"
    return None, owner, "foreign"


def _sessions_holding_task(task_id: str) -> List[str]:
    """Session ids whose registry knows ``task_id`` (live or recently terminal)."""
    try:
        from app.core.task_registry import task_registry

        return list(task_registry.sessions_with_task(task_id))
    except Exception:  # noqa: BLE001 — a lookup miss is just a miss
        logger.debug("registry owner lookup failed for %s", task_id, exc_info=True)
        return []


def _foreign_task_error(task_id: str, owner_session_id: Optional[str]) -> Dict[str, Any]:
    """The "not yours" answer, which used to be an indistinguishable "not found".

    Two different failures wore the same message: an id that never existed, and
    an id that exists in another session. The second is recoverable — read the
    worker's own transcript, or ask the worker to report it — and the model
    cannot tell which it is looking at without being told.
    """
    return _err(
        "This task belongs to a different session, so it cannot be read from "
        "here. If it is a worker's Monitor task, its result reaches you in the "
        "worker's report; otherwise read the worker session directly.",
        task_id=task_id,
        owner_session_id=owner_session_id,
    )


async def stop_background_shell_task(
    task_id: str, session_id: Optional[str] = None
) -> Optional[Dict[str, Any]]:
    """Force-stop a Monitor shell background task by id.

    Kills the tracked subprocess AND its descendants
    (:func:`app.core.process_tree.kill_process_tree`: graceful group signal,
    escalating to SIGKILL / ``taskkill /F /T``), then VERIFIES the outcome
    before answering. Shared by the ``TaskStop`` MCP tool and the UI "stop
    task" endpoint.

    The verification is the point. Terminating the shell used to be reported
    as a stop even when the thing the shell had started — a dev server three
    processes down under Git Bash's nested bash — was still holding its
    listening socket, so ``TaskStop`` answered ``running: false`` while
    ``netstat`` showed the port bound and the agent had to ``taskkill`` by
    hand. When descendants survive, this says so: ``running`` stays true and
    ``warning`` names the pids (and the ports they still hold).

    Returns a small status dict when ``task_id`` is a known shell task, or
    ``None`` when no such shell task exists — letting callers fall through to
    SDK agent-task handling.
    """
    task = _background_tasks.get(task_id)
    if not task:
        return None
    from app.core.process_tree import kill_process_tree, survivor_warning

    survivors: List[int] = []
    if task.process is not None:
        survivors = await kill_process_tree(
            task.process, own_process_group=task.own_process_group
        )
    # Mark the task killed in the registry. The reader's completion path may also
    # fire (with exit_code from terminate); the last write wins and both are
    # terminal, so the surfaced status stays sane. Orphans do NOT keep the row
    # running — the shell itself is gone and nothing would ever settle it — but
    # they are reported to the caller.
    if session_id:
        from app.core.task_registry import task_registry
        _registry_safe(
            lambda: task_registry.complete_task(session_id, task_id, "killed")
        )
    result: Dict[str, Any] = {
        "task_id": task_id,
        "kind": "shell",
        "running": task.is_running() or bool(survivors),
        "exit_code": task.exit_code,
    }
    if survivors:
        result["survivors"] = survivors
        result["warning"] = survivor_warning(task_id, survivors)
        logger.warning(
            "TaskStop left orphans for shell %s: %s", task_id, survivors
        )
    return result


# Strong references to in-flight reap tasks so the fire-and-forget scheduler
# below does not let them be garbage-collected mid-run.
_reap_tasks: set = set()


async def _settle_reaped_shell(session_id: str, task_id: str) -> None:
    """Record the terminal transition for a shell the session reap just killed.

    The reap cancels the shell's ``_reader`` before terminating the process, so
    the reader's own ``complete_task`` never runs — without an explicit settle
    the task stays "running" forever: the in-memory registry row (when it still
    exists), the durable ``background_tasks`` mirror, and the UI chip built from
    it (live 2026-08-29, task a0270e18 — stuck 45+ minutes after its process
    died).

    Two shapes, matching teardown ordering at the call sites:

    - registry row still present and running → ``complete_task("killed")``
      (persists the durable mirror and fires the guaranteed terminal chip);
    - registry already cleared (callers run ``clear_session`` BEFORE scheduling
      this reap) → settle the durable row directly and emit the terminal
      ``task.updated`` frame, mirroring the stop-click durable force-settle.

    Best-effort in every direction: a settle failure must never block the reap.
    """
    from app.core.task_registry import task_registry

    record = task_registry.get_task(session_id, task_id)
    if record is not None:
        if record.get("status") == "running":
            _registry_safe(
                lambda: task_registry.complete_task(session_id, task_id, "killed")
            )
        return
    try:
        from app.db.background_tasks import (
            TERMINAL_STATUSES,
            get_background_task,
            mark_background_task_killed,
        )
        from app.core.turn_recorder import record_task_settle_frame

        row = await asyncio.to_thread(get_background_task, task_id, session_id)
        if row is None or row.get("status") in TERMINAL_STATUSES:
            return
        await asyncio.to_thread(mark_background_task_killed, task_id, session_id)
        from app.core.task_persistence import broadcast_running_task_presence
        await broadcast_running_task_presence([session_id])
        if task_registry.mark_chipped(session_id, task_id, "terminal"):
            await record_task_settle_frame(
                session_id, task_id, "killed",
                "Background task stopped with its session",
                reason="session_teardown", tool_use_id=row.get("tool_use_id"),
            )
    except Exception:  # noqa: BLE001 — settle is best-effort observability
        logger.debug(
            f"Durable settle for reaped shell {task_id} failed", exc_info=True
        )


async def stop_session_background_shells(session_id: str) -> int:
    """Terminate + reap every Monitor shell spawned by a session (U2).

    Monitor shells deliberately do NOT pin a live session open (see
    task_registry — they are excluded from the background-work guard), so when the
    session is finally removed its shells would otherwise STRAND: the subprocess
    keeps running, its ``_reader`` task awaits ``readline()`` forever (for a
    long-running command like ``tail -f``), and the ``_background_tasks`` entry —
    with its line buffer — leaks for the whole process lifetime. This stops each
    subprocess (escalating to kill after 5s), cancels the reader, drops the
    registry entry, and settles the task's registry/durable row to ``killed``
    (see ``_settle_reaped_shell`` — the cancelled reader can no longer do it).
    Also reaps already-finished shells, bounding the dict's growth to session
    lifetime.

    Returns the number of shell tasks reaped.
    """
    victims = [
        (tid, t) for tid, t in list(_background_tasks.items())
        if t.session_id == session_id
    ]
    reaped = 0
    for tid, task in victims:
        # Cancel the reader first so it stops touching task/registry state.
        rt = task._reader_task
        if rt is not None and not rt.done():
            rt.cancel()
        # Terminate the subprocess AND its descendants, escalating to a forced
        # tree kill after 5s. Killing only the shell would leave whatever it
        # started (a dev server, typically) running for the life of the host.
        proc = task.process
        if proc is not None and proc.returncode is None:
            from app.core.process_tree import kill_process_tree

            try:
                survivors = await kill_process_tree(
                    proc, own_process_group=task.own_process_group
                )
                if survivors:
                    logger.warning(
                        "Session reap left orphan pids for shell %s: %s",
                        tid, survivors,
                    )
            except Exception:
                logger.debug(f"Error terminating background shell {tid}", exc_info=True)
        _background_tasks.pop(tid, None)
        await _settle_reaped_shell(session_id, tid)
        reaped += 1
    if reaped:
        logger.info(
            f"Reaped {reaped} background shell(s) for ended session {session_id}"
        )
    return reaped


def reap_session_background_shells(session_id: str) -> None:
    """Schedule async reap of a session's Monitor shells (non-blocking).

    Fire-and-forget so callers (session teardown/eviction paths, which may
    hold locks) are never blocked by the up-to-5s terminate/kill escalation.
    A no-op when called with no running event loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    t = loop.create_task(stop_session_background_shells(session_id))
    _reap_tasks.add(t)
    t.add_done_callback(_reap_tasks.discard)


async def _emit_shell_start_frame(
    session_id: str, task_id: str, command: str, tool_use_id: Optional[str]
) -> None:
    """Durable RUNNING chip row for a freshly spawned Monitor shell.

    A Monitor shell is the one background task with NO durable start-phase
    surface of its own: the SDK/fluid ``TaskStarted`` mirror only exists on the
    native leg, and the terminal-surface guarantee
    (``chat_ws_handler._surface_terminal_notification``) fires on FAILURE only.
    So "I asked the agent to run the app" left nothing in the log to replay and
    the running server vanished from the chip strip on reload or session switch.

    Latched once per task lifetime, sharing the ``mark_chipped(..., "start")``
    latch with the in-stream mirror (``turn_recorder._translate_shared``), so
    the native leg — where both fire — still records exactly one row.
    """
    try:
        from app.core.turn_recorder import record_task_start_frame

        await record_task_start_frame(
            session_id, task_id, description=command, tool_use_id=tool_use_id,
        )
    except Exception:  # noqa: BLE001 — a chip must never break a spawn
        logger.debug(
            f"start frame for background shell {task_id} failed", exc_info=True
        )


async def _emit_shell_settle_frame(
    session_id: str, task_id: str, status: str
) -> None:
    """Durable terminal chip row for a Monitor shell that ran to completion.

    ``complete_task``'s guaranteed terminal notification covers error/killed
    only, so before this a CLEANLY completed shell settled in the registry and
    in ``background_tasks`` but appended nothing to the event log — and the
    reducer deliberately exempts ``kind === 'shell'`` summaries from the
    turn-completed staleness heal, so the chip this task's start row painted
    would have replayed as "running" forever.

    Shares the ``mark_chipped(..., "terminal")`` latch with every other terminal
    producer, so the failure path (which routes through the terminal-surface
    guarantee) still emits exactly one row and history shows one entry per task.
    """
    try:
        from app.core.task_registry import task_registry
        from app.core.turn_recorder import record_task_settle_frame

        if not task_registry.mark_chipped(session_id, task_id, "terminal"):
            return
        record = task_registry.get_task(session_id, task_id) or {}
        await record_task_settle_frame(
            session_id, task_id, status, record.get("summary"),
            tool_use_id=record.get("tool_use_id"),
        )
    except Exception:  # noqa: BLE001 — a chip must never break a completion
        logger.debug(
            f"settle frame for background shell {task_id} failed", exc_info=True
        )


async def _read_lines(stream: Any):
    """Yield newline-terminated chunks from ``stream``, with NO line limit.

    ``StreamReader.readline()`` raises ``ValueError`` on a line longer than the
    stream's 64 KiB limit — and clears the buffer on the way out, so the
    exception is not even recoverable in place. Nothing caught it: the reader
    task died, the task's output froze mid-run while the process kept going,
    and the shell looked hung (live production occurrence — a minified bundle
    on one line is enough). Reading fixed chunks and splitting on newlines here
    has no limit at all; an over-long line is emitted in ``_MAX_LINE_BYTES``
    pieces so the buffer cannot grow without bound either.

    Bytes are buffered, not text, so a multi-byte character is only ever split
    at a forced chop — never at a chunk boundary.
    """
    buffer = b""
    while True:
        chunk = await stream.read(_READ_CHUNK_BYTES)
        if not chunk:
            if buffer:
                yield buffer
            return
        buffer += chunk
        while True:
            index = buffer.find(b"\n")
            if index < 0:
                break
            yield buffer[: index + 1]
            buffer = buffer[index + 1:]
        while len(buffer) > _MAX_LINE_BYTES:
            yield buffer[:_MAX_LINE_BYTES]
            buffer = buffer[_MAX_LINE_BYTES:]


def sh_fallback_command(command: str, cwd: Optional[str]) -> str:
    """``command``, or ``sh <path>`` when it names a script with no exec bit.

    A launch script a worker wrote without ``chmod +x`` fails with
    ``/bin/sh: 1: …/launch-backend.sh: Permission denied`` and exit 126 — one
    audited session lost a whole Monitor cycle and a steer before prefixing
    ``sh`` by hand. Nothing has run at that point, so running it through the
    shell instead is safe and loses nothing.

    Deliberately narrow: the command must be exactly one token, that token must
    resolve to an existing regular file that is readable but not executable,
    and the platform must have an exec bit at all (never on Windows, where
    ``os.access(X_OK)`` answers about file extensions).
    """
    import os
    import shlex

    text = (command or "").strip()
    if os.name == "nt" or not text or text.startswith(("sh ", "bash ", "sudo ")):
        return command
    try:
        parts = shlex.split(text)
    except ValueError:
        return command
    if len(parts) != 1:
        return command
    path = parts[0]
    candidate = path if os.path.isabs(path) else os.path.join(cwd or "", path)
    try:
        if not os.path.isfile(candidate):
            return command
        if os.access(candidate, os.X_OK) or not os.access(candidate, os.R_OK):
            return command
    except OSError:
        return command
    logger.info(
        "Monitor: %s is not executable — running it as `sh %s`", path, path
    )
    return f"sh {shlex.quote(path)}"


async def _spawn_background(
    command: str,
    cwd: Optional[str],
    session_id: Optional[str] = None,
    tool_use_id: Optional[str] = None,
) -> _BackgroundTask:
    task_id = str(uuid.uuid4())[:8]
    # A script with no exec bit never runs; `sh <path>` does. Applied before the
    # task records its command so TaskList shows what actually ran.
    command = sh_fallback_command(command, cwd)
    task = _BackgroundTask(task_id=task_id, command=command, cwd=cwd, session_id=session_id)
    # Run through the SAME shell fluid's Bash resolves (Git Bash on Windows,
    # sh/bash on POSIX) — create_subprocess_shell would use cmd.exe on
    # Windows, breaking every POSIX-syntax command the model writes for the
    # shell the environment block declares. stdin is closed so a command
    # that prompts exits instead of inheriting the server's stdin.
    from fluid.tools.shell import resolve_shell
    from app.core.process_tree import child_env, spawn_kwargs

    # Its own process group on BOTH platforms (POSIX session / Windows
    # CREATE_NEW_PROCESS_GROUP) so the stop path can address the whole tree,
    # and a UTF-8 environment so a child Python printing anything outside
    # cp1252 doesn't die before it binds its port.
    proc = await asyncio.create_subprocess_exec(
        *resolve_shell().argv_prefix,
        command,
        cwd=cwd,
        env=child_env(),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **spawn_kwargs(),
    )
    task.process = proc
    task.own_process_group = True

    # Register the shell task so TaskList / /live can surface it. Keyed by the
    # AI Shuffle session that spawned it; module-level spawns (no session) skip
    # registration.
    if session_id:
        from app.core.task_registry import task_registry
        _registry_safe(
            lambda: task_registry.start_task(
                session_id, task_id, "shell", command, tool_use_id=tool_use_id,
            )
        )
        # Durable RUNNING chip. Awaited (not fire-and-forget) so the row is in
        # the log before the spawner hands its receipt back to the model — a
        # turn that ends immediately after must not race the frame that makes
        # its shell visible.
        await _emit_shell_start_frame(session_id, task_id, command, tool_use_id)

    async def _reader() -> None:
        assert proc.stdout is not None
        async for line in _read_lines(proc.stdout):
            decoded = line.decode("utf-8", errors="replace").rstrip("\n")
            # Scrub terminal control sequences at the SOURCE, so every consumer
            # of this buffer (TaskOutput, TaskGet, the completion summary the
            # parent is woken with) is clean. The app overrides fluid's
            # supervision here (ruling A13), so fluid's own supervisor-side
            # strip never runs for these lines and the model was being handed
            # colour codes — a baseline session burned a 34s vitest re-run on an
            # ANSI-mismatched regex. Line-at-a-time is safe: an escape sequence
            # never spans a newline.
            task.lines.append(_strip_ansi(decoded))
            if len(task.lines) > _BACKGROUND_LINE_LIMIT:
                # drop oldest
                del task.lines[: len(task.lines) - _BACKGROUND_LINE_LIMIT]
        task.exit_code = await proc.wait()
        if session_id:
            from app.core.task_registry import task_registry
            status = "completed" if task.exit_code == 0 else "error"

            settled: Dict[str, Any] = {}

            def _finish() -> None:
                # Never downgrade a status somebody else settled honestly:
                # a deliberate TaskStop "killed" (terminate() yields a non-zero
                # exit code, indistinguishable from a crash here), or the
                # monitor watcher's outcome-derived "error" for a command that
                # exited 0 while its own last word was `EXIT:1`. The exit code
                # is the WEAKEST of the three signals, so it only fills a gap.
                existing = (
                    task_registry._tasks.get(session_id, {}).get(task_id) or {}
                ).get("status")
                if existing in _SETTLED_STATUSES:
                    settled["status"] = existing
                    return
                task_registry.complete_task(session_id, task_id, status)
                settled["status"] = status

            _registry_safe(_finish)
            # Terminal chip row. complete_task's guaranteed notification covers
            # error/killed; this is what settles a shell that exited cleanly, so
            # the running chip its start row painted resolves on replay too.
            # Idempotent by ``mark_chipped``, so a status another path already
            # chipped costs nothing here.
            if settled.get("status"):
                await _emit_shell_settle_frame(session_id, task_id, settled["status"])

    task._reader_task = asyncio.create_task(_reader(), name=f"monitor-{task_id}")
    _background_tasks[task_id] = task
    return task


def _discover_agent_registry(cwd: Optional[str]) -> Optional[Dict[str, Any]]:
    """``{name: AgentDefinition}`` for the file/plugin agents visible from ``cwd``.

    The shape ``app.core.task_dispatch.dispatch_task_tool`` expects for its
    ``agent_registry`` seam — identical to the mapping ``NativeLoop`` publishes
    on ``ctx.extra["agent_registry"]``, built from the same
    ``fluid.context.agents.discover_agents`` walk (project ``.claude/agents``,
    the global dir, and enabled plugins, ruling CE6).

    This exists because the DELEGATED (claude CLI) leg runs no fluid engine:
    its Task tool is this MCP one, so without a registry a ``.claude/agents``
    definition is advertised in the prompt yet answers "Unknown agent" on
    dispatch. Best effort in every direction — no cwd, no fluid, or a bad
    definition returns None and dispatch falls back to DB agents only.
    """
    if not cwd:
        return None
    try:
        from fluid.context.agents import discover_agents

        return {d.name: d for d in discover_agents(cwd)}
    except Exception:  # noqa: BLE001 — discovery must never fail a dispatch
        logger.debug("agent discovery failed for cwd=%s", cwd, exc_info=True)
        return None


# ============================================================================
# App-memory bridge (delegated leg)
# ============================================================================

def _fluid_outcome_result(outcome: Any) -> Dict[str, Any]:
    """A fluid ``ToolOutcome`` as the SDK MCP envelope (plain text, not JSON —
    the fluid handlers already format for the model)."""
    result: Dict[str, Any] = {
        "content": [{"type": "text", "text": getattr(outcome, "text", "") or ""}]
    }
    if getattr(outcome, "is_error", False):
        result["isError"] = True
    return result


def _build_memory_bridge_tools(
    tool: Any, *, session_id: Optional[str], cwd: Optional[str]
) -> list:
    """The app-memory tools for the delegated (claude CLI) leg.

    2026-08-31 decision (docs/MEMORY_ARCHITECTURE.md): the delegated leg gets
    a BRIDGE to the app's memory rather than a note saying it has none. Each
    tool reuses the fluid builtin handler over the same stores the native leg
    wires through ``ctx.extra`` — Recall over the continual SkillStore,
    SessionSearch/SessionView over PgFluidStore's recall hooks, Remember into
    the same episodic journal. One difference is inherent to the leg: no
    engine means no live synthesizer, so a Remember journals the fact and the
    next NATIVE session's synthesis pass distills it into memories.json.

    Gating mirrors the native side's: Recall needs the continual store
    (code-mode or continual on), Remember needs continual learning, the
    session pair needs a database. The returned list may be empty.
    """
    from app.core.config import settings

    tools: list = []
    store_on = settings.fluid_code_mode or settings.fluid_continual
    continual_on = settings.fluid_continual
    db_on = bool(settings.database_url or settings.postgres_password)

    # The bridged surface itself, for fluid's describe() specialization —
    # cross-tool routing clauses collapse to the tools that actually exist
    # here (ruling T9 for descriptions), e.g. SessionSearch does not point at
    # HistorySearch, which the CLI leg does not have.
    surface = set()
    if store_on:
        surface.add("Recall")
    if continual_on:
        surface.add("Remember")
    if db_on:
        surface.update({"SessionSearch", "SessionView"})
    frozen = frozenset(surface)

    def _fluid_ctx() -> Any:
        from fluid.tools.registry import ToolCtx

        return ToolCtx(cwd=cwd or ".", session_id=session_id)

    if continual_on:
        # Description is the bridge's own: fluid's speaks of "the # Memory
        # list shown at the start of every future session", which only native
        # legs render — here the honest promise is Recall + future native
        # sessions' snapshots.
        @tool(
            "Remember",
            "Save a durable fact to the app's cross-session memory (shared by "
            "every session and model). Call it the moment the user asks you to "
            "remember something, states a lasting preference, or corrects you "
            "on something worth never repeating; also for durable facts you "
            "establish yourself. One call per fact; `text` is the fact itself "
            "in one or two plain sentences — a statement about the world, not "
            "an instruction to yourself. A background learner distills it into "
            "the shared memory shortly after; retrieve with Recall. Never "
            "store secrets: record where a credential lives, not its value.",
            {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": (
                            "The fact, in one or two plain sentences. State "
                            "what is true, not what to do."
                        ),
                    },
                    "scope": {
                        "type": "string",
                        "enum": ["global", "project"],
                        "description": (
                            "'project' when the fact is about this project's "
                            "code, layout, or conventions; 'global' (default) "
                            "when it is about the user or their machine in "
                            "general."
                        ),
                    },
                },
                "required": ["text"],
            },
        )
        async def remember_tool(args: dict) -> dict:
            try:
                from fluid.codemode.skills import SkillStore
                from fluid.tools.builtins import remember as remember_mod

                from app.core.fluid_memory import skills_root

                skills = SkillStore(skills_root())

                def _journal(text: str, scope: Optional[str] = None) -> None:
                    entry: Dict[str, Any] = {"kind": "remember", "text": text}
                    if scope:
                        entry["scope"] = scope
                    skills.journal_append(entry)

                ctx = _fluid_ctx()
                ctx.extra["continual_journal"] = _journal
                outcome = await remember_mod.handler(args, ctx)
                return _fluid_outcome_result(outcome)
            except Exception as e:  # noqa: BLE001
                logger.exception("Remember bridge failed")
                return _err(f"failed to record: {e}")

        tools.append(remember_tool)

    if store_on:
        @tool(
            "Recall",
            "Search the app's long-term memory: every fact the continual "
            "learner has kept (archived entries and other projects' entries "
            "included) plus the saved-skill index. Use it when the task "
            "touches something that may have been learned before — a "
            "preference, a convention, a port, a past correction. Returns the "
            "top matches per category with their source."
            + (
                " For past conversations and what happened in them, use "
                "SessionSearch instead — Recall covers learned facts and "
                "skills, not transcripts."
                if "SessionSearch" in frozen
                else ""
            ),
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": (
                            "Keywords (matched case-insensitively, best "
                            "matches first)."
                        ),
                    },
                    "limit": {
                        "type": "integer",
                        "description": (
                            "Maximum matches per category (default 3, max 10)."
                        ),
                    },
                },
                "required": ["query"],
            },
        )
        async def recall_tool(args: dict) -> dict:
            try:
                from fluid.codemode.skills import SkillStore
                from fluid.tools.builtins import recall as recall_mod

                from app.core.fluid_memory import skills_root

                ctx = _fluid_ctx()
                ctx.extra["recall_store"] = SkillStore(skills_root())
                outcome = await recall_mod.handler(args, ctx)
                return _fluid_outcome_result(outcome)
            except Exception as e:  # noqa: BLE001
                logger.exception("Recall bridge failed")
                return _err(f"recall failed: {e}")

        tools.append(recall_tool)

    if db_on:
        from fluid.tools.builtins import session_search as session_search_mod
        from fluid.tools.builtins import session_view as session_view_mod

        def _session_ctx() -> Any:
            from app.core.provider.fluid_pg_store import PgFluidStore

            store = PgFluidStore()
            ctx = _fluid_ctx()
            ctx.extra["session_search"] = store.search
            ctx.extra["session_recent"] = store.recent_sessions
            ctx.extra["session_view"] = store.session_slice
            return ctx

        @tool(
            "SessionSearch",
            session_search_mod.describe(frozen),
            session_search_mod.SPEC.input_schema,
        )
        async def session_search_tool(args: dict) -> dict:
            try:
                outcome = await session_search_mod.handler(args, _session_ctx())
                return _fluid_outcome_result(outcome)
            except Exception as e:  # noqa: BLE001
                logger.exception("SessionSearch bridge failed")
                return _err(f"session search failed: {e}")

        tools.append(session_search_tool)

        @tool(
            "SessionView",
            session_view_mod.describe(frozen),
            session_view_mod.SPEC.input_schema,
        )
        async def session_view_tool(args: dict) -> dict:
            try:
                outcome = await session_view_mod.handler(args, _session_ctx())
                return _fluid_outcome_result(outcome)
            except Exception as e:  # noqa: BLE001
                logger.exception("SessionView bridge failed")
                return _err(f"session view failed: {e}")

        tools.append(session_view_tool)

    return tools


# ============================================================================
# Tool factory
# ============================================================================

def create_builtin_tools_server(
    session_id: Optional[str] = None,
    profile_id: Optional[str] = None,
    project_id: Optional[str] = None,
    is_admin: bool = False,
    model: Optional[str] = None,
    provider_id: Optional[str] = None,
    cwd: Optional[str] = None,
) -> Optional[Any]:
    """
    Build the ``mcp__builtins__*`` MCP server for a session.

    Args:
        session_id: The current AI Shuffle session (used by ScheduleWakeup to
            target the wakeup prompt back at the correct session).
        profile_id: Profile to use when firing scheduled/wake prompts.
        project_id: Project context for worktree and cron tools.
        is_admin: Whether the caller has admin permissions (gated for
            destructive operations like CronDelete).
        model: The current turn's effective model, persisted by ScheduleWakeup
            so the wake replays it instead of running on the profile default.
        provider_id: The current turn's effective model-provider row. ``None``
            means native Claude. Used to build and enforce Task's subagent
            roster without exposing cross-provider targets.
        cwd: The session's resolved working directory. Task uses it to discover
            the file/plugin agent definitions this session can delegate to —
            the delegated (CLI) leg has no fluid engine to publish them, so
            without it those agents are advertised but not dispatchable.

    Returns:
        SDK MCP server config, or ``None`` if the SDK is unavailable.
    """
    try:
        from claude_agent_sdk import tool, create_sdk_mcp_server
    except ImportError:
        logger.debug("claude_agent_sdk not available — builtin tools disabled")
        return None

    from app.mcp.sdk_compat import patch_sdk_server

    all_tools: list = []

    # ------------------------------------------------------------------
    # ScheduleWakeup — pause + resume session after a delay
    # ------------------------------------------------------------------
    @tool(
        "ScheduleWakeup",
        "Schedule the session to resume after a delay by re-firing a prompt. "
        "Use when you want the agent to stop now and continue work later on a timer. "
        "delaySeconds is clamped to [60, 3600]. prompt is the /loop input to fire on wake-up.",
        {
            "type": "object",
            "properties": {
                "delaySeconds": {"type": "number", "description": "Seconds from now to wake up (60-3600)."},
                "prompt": {"type": "string", "description": "Prompt to fire on wake-up."},
                "reason": {"type": "string", "description": "Short explanation of the chosen delay."},
            },
            "required": ["delaySeconds", "prompt"],
        },
    )
    async def schedule_wakeup(args: dict) -> dict:
        if not session_id or not profile_id:
            return _err("ScheduleWakeup requires an active session context.")
        try:
            delay = float(args.get("delaySeconds") or 0)
        except (TypeError, ValueError):
            return _err("delaySeconds must be a number.")
        delay = max(60.0, min(3600.0, delay))
        prompt = (args.get("prompt") or "").strip()
        if not prompt:
            return _err("prompt is required.")
        reason = (args.get("reason") or "").strip()

        # #598: persist the wake BEFORE arming its timer so an app restart
        # rehydrates and re-arms it (the old bare in-process asyncio.sleep task
        # was dropped on restart). wakeup_service owns the claim-on-fire + boot
        # rehydration; the actual wake still re-fires through
        # execute_query_with_failover (a genuine CLI-owned new turn).
        from app.core import wakeup_service

        row = await wakeup_service.schedule(
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id,
            prompt=prompt,
            delay_seconds=delay,
            reason=reason or None,
            model=model,
            source="schedule_wakeup",
        )
        return _ok({
            "scheduled": True,
            "delay_seconds": delay,
            "fire_at_unix": row.get("fire_at"),
            "wakeup_id": row.get("id"),
            "reason": reason,
            "model": model,
        })

    all_tools.append(schedule_wakeup)

    # ------------------------------------------------------------------
    # Task — dispatch work to a subagent (P2c, rulings A6/A8/A9/A10).
    # Delegated-leg surface (mcp__builtins__Task); native legs get fluid's
    # bare-named Task builtin instead (the bridge filters this copy out).
    # ------------------------------------------------------------------
    from app.core.agent_provider_models import (
        build_task_contract,
        file_subagent_targets,
    )

    agent_registry = _discover_agent_registry(cwd)
    task_contract = build_task_contract(
        profile_id,
        provider_id,
        file_subagent_targets(agent_registry, model),
    )

    @tool("Task", task_contract.description, task_contract.input_schema)
    async def task_tool(args: dict) -> dict:
        if not session_id:
            payload = {
                "ok": False,
                "error": "Task requires an active session context.",
            }
        else:
            from app.core.task_dispatch import dispatch_task_tool

            payload = await dispatch_task_tool(
                args,
                caller_session_id=session_id,
                caller_profile_id=profile_id,
                caller_provider_id=provider_id,
                # Fluid file/plugin definitions have no DB provider map. They
                # remain a fallback and borrow this exact caller binding.
                agent_registry=agent_registry,
            )
        import json as _json

        return {
            "content": [{
                "type": "text",
                "text": _json.dumps(payload, default=_json_default),
            }],
            "isError": not bool(payload.get("ok")),
            "structuredContent": payload,
        }

    all_tools.append(task_tool)

    # ------------------------------------------------------------------
    # NotifyDevices (formerly PushNotification; renamed for issue #610)
    # ------------------------------------------------------------------
    @tool(
        "NotifyDevices",
        "Send a push notification to all registered mobile devices. "
        "Use for alerting the user to long-running task completion or important "
        "events.\n"
        "\n"
        "This install may have NO registered devices, in which case nothing is "
        "delivered and the result says so explicitly (sent: 0, delivered: "
        "false). Treat that as 'the user has not been told' — say it in chat "
        "instead. A non-zero `sent` with `successful: 0` means every device "
        "rejected the push; that is also undelivered.",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "body": {"type": "string"},
                "data": {"type": "object", "description": "Optional custom data payload."},
            },
            "required": ["title", "body"],
        },
    )
    async def push_notification(args: dict) -> dict:
        title = (args.get("title") or "").strip()
        body = (args.get("body") or "").strip()
        if not title or not body:
            return _err("title and body are required.")
        try:
            from app.services.push_notification import push_notification_service
            results = await push_notification_service.send_to_all_devices(
                title=title, body=body, data=args.get("data"),
            )
            successful = sum(1 for r in results if getattr(r, "success", False))
            payload: Dict[str, Any] = {
                "sent": len(results),
                "successful": successful,
                "delivered": successful > 0,
            }
            # A bare {"sent": 0, "successful": 0} reads as success. It is the
            # opposite: an audited monitoring session announced a release
            # through this tool, reached nobody, and moved on believing the
            # user had been told. Not an exception — there is nothing to
            # retry — but not a green light either.
            if not results:
                payload["note"] = (
                    "no registered devices — nothing was delivered; tell the "
                    "user in chat instead."
                )
            elif not successful:
                payload["note"] = (
                    f"all {len(results)} device(s) rejected the push — nothing "
                    "was delivered; tell the user in chat instead."
                )
            return _ok(payload)
        except Exception as e:
            logger.exception("NotifyDevices failed")
            return _err(f"send failed: {e}")

    all_tools.append(push_notification)

    # ------------------------------------------------------------------
    # EnterWorktree / ExitWorktree
    # ------------------------------------------------------------------
    @tool(
        "EnterWorktree",
        "Materialize (or resolve) the current project's persistent branch checkout and return its path. "
        "The checkout path can be used as cwd for subsequent tool calls. Requires an active project. "
        "Also records the branch on this session, so the sidebar and PR tracking follow the work you do there.",
        {
            "type": "object",
            "properties": {
                "branch_name": {"type": "string", "description": "Branch to check out."},
                "create_new_branch": {"type": "boolean", "description": "If true, create a new branch from base_branch."},
                "base_branch": {"type": "string", "description": "Base branch when creating a new one."},
            },
            "required": ["branch_name"],
        },
    )
    async def enter_worktree(args: dict) -> dict:
        if not project_id:
            return _err("EnterWorktree requires an active project context.")
        branch = (args.get("branch_name") or "").strip()
        if not branch:
            return _err("branch_name is required.")
        try:
            from app.core.project_resolver import resolve_project_v2
            from app.core import checkout_manager
            pv2 = await asyncio.to_thread(
                resolve_project_v2, {"project_id": project_id}
            )
            if not pv2:
                return _err(
                    "This project is not resolvable to a v2 checkout on this device."
                )
            checkout_path = await asyncio.to_thread(
                checkout_manager.ensure_checkout,
                pv2,
                branch,
                base_branch=args.get("base_branch"),
                create_new_branch=bool(args.get("create_new_branch")),
            )
            if not checkout_path:
                return _err(
                    f"Branch '{branch}' is not materializable on this device "
                    "(no base checkout here, or the branch exists nowhere and "
                    "create_new_branch was not set)."
                )
            # BIND THE SESSION TO THE BRANCH, exactly as the HTTP intake paths
            # do (app/api/sessions_worktree.py:329 and :521). Without this the
            # tool materialized a checkout and told nobody: `sessions.branch`
            # stayed NULL, the sidebar showed the session on no branch, the
            # turn-finalize resync could not repair it (with no branch it
            # resolves the BASE checkout, whose branch it deliberately refuses
            # to adopt), and PR reconciliation — which matches on session_id,
            # pr_number, or project_id+branch — could match nothing.
            bound = False
            if session_id:
                try:
                    from app.db import database

                    bound = bool(await asyncio.to_thread(
                        database.set_session_branch,
                        session_id, branch, pv2.get("id"),
                    ))
                except Exception:  # noqa: BLE001 — bookkeeping must never fail
                    logger.warning(  # a checkout that was really materialized
                        "EnterWorktree: binding session %s to %r failed",
                        session_id, branch, exc_info=True,
                    )
            return _ok({
                "branch_name": branch,
                "worktree_path": checkout_path,
                "status": "active",
                "session_branch_recorded": bound,
                # Audited sessions edited a file at its old path's worktree
                # twin and hit the read-before-edit gate as if it were a new
                # file — which, to the gate, it is.
                "note": (
                    "Paths under the worktree root are new paths: the "
                    "read-before-edit gate re-fires there, so Read a file at "
                    "its worktree path before the first Edit to it."
                ),
            })
        except Exception as e:
            logger.exception("EnterWorktree failed")
            return _err(f"enter checkout failed: {e}")

    all_tools.append(enter_worktree)

    @tool(
        "ExitWorktree",
        "Clean up a LEGACY git worktree by ID. Optionally keep the underlying branch. "
        "Applies only to legacy worktrees — v2 project checkouts are persistent and need no cleanup.",
        {
            "type": "object",
            "properties": {
                "worktree_id": {"type": "string"},
                "keep_branch": {"type": "boolean", "description": "Keep the branch after removing the worktree (default true)."},
            },
            "required": ["worktree_id"],
        },
    )
    async def exit_worktree(args: dict) -> dict:
        wt_id = (args.get("worktree_id") or "").strip()
        if not wt_id:
            return _err("worktree_id is required.")
        try:
            from app.core.worktree_manager import worktree_manager
            keep = args.get("keep_branch", True)
            ok = await worktree_manager.cleanup_worktree(wt_id, keep_branch=bool(keep))
            return _ok({"removed": ok, "worktree_id": wt_id})
        except Exception as e:
            logger.exception("ExitWorktree failed")
            return _err(f"cleanup worktree failed: {e}")

    all_tools.append(exit_worktree)

    # ------------------------------------------------------------------
    # CronCreate / CronList / CronDelete (bridge to automations)
    # ------------------------------------------------------------------
    @tool(
        "CronCreate",
        "Create a recurring scheduled prompt (backed by an AI Shuffle automation). "
        "Use 5-field cron expressions (e.g. '0 9 * * 1-5' = weekdays at 9am).",
        {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "expression": {"type": "string", "description": "5-field cron expression."},
                "prompt": {"type": "string", "description": "Prompt to run on each fire."},
                "timezone": {"type": "string", "description": "IANA timezone (default UTC)."},
                "max_cost_per_run": {"type": "number"},
                "max_runs_per_day": {"type": "integer"},
            },
            "required": ["name", "expression", "prompt"],
        },
    )
    async def cron_create(args: dict) -> dict:
        if not profile_id:
            return _err("CronCreate requires a profile context.")
        name = (args.get("name") or "").strip()
        expression = (args.get("expression") or "").strip()
        prompt = (args.get("prompt") or "").strip()
        if not name or not expression or not prompt:
            return _err("name, expression, and prompt are required.")
        try:
            from app.db import automations as auto_db
            from app.core.automation_manager import automation_manager
            automation_id = str(uuid.uuid4())
            trigger_config = {
                "expression": expression,
                "timezone": args.get("timezone") or "UTC",
            }
            row = await asyncio.to_thread(
                auto_db.create_automation,
                automation_id=automation_id,
                name=name,
                trigger_type="cron",
                trigger_config=trigger_config,
                profile_id=profile_id,
                prompt=prompt,
                project_id=project_id,
                max_cost_per_run=args.get("max_cost_per_run"),
                max_runs_per_day=args.get("max_runs_per_day"),
            )
            if not row:
                return _err("automation not created.")
            # Register with the scheduler so it actually runs.
            try:
                await automation_manager.schedule_automation(automation_id)
            except Exception:
                logger.exception("Failed to register cron with scheduler")
            return _ok({"automation_id": automation_id, "name": name, "expression": expression})
        except Exception as e:
            logger.exception("CronCreate failed")
            return _err(f"create failed: {e}")

    all_tools.append(cron_create)

    @tool(
        "CronList",
        "List existing scheduled automations (cron-triggered).",
        {"type": "object", "properties": {}},
    )
    async def cron_list(args: dict) -> dict:
        try:
            from app.db import automations as auto_db
            rows = await asyncio.to_thread(auto_db.get_all_automations)
            out = []
            for r in rows or []:
                triggers = r.get("triggers") or []
                cron_triggers = [t for t in triggers if t.get("type") == "cron"]
                if not cron_triggers:
                    continue
                out.append({
                    "id": r.get("id"),
                    "name": r.get("name"),
                    "status": r.get("status"),
                    "expressions": [t.get("config", {}).get("expression") for t in cron_triggers],
                    "last_run_at": r.get("last_run_at"),
                })
            return _ok({"crons": out, "count": len(out)})
        except Exception as e:
            logger.exception("CronList failed")
            return _err(f"list failed: {e}")

    all_tools.append(cron_list)

    @tool(
        "CronDelete",
        "Delete a scheduled automation by ID. Requires confirm=true.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
                "confirm": {"type": "boolean"},
            },
            "required": ["automation_id"],
        },
    )
    async def cron_delete(args: dict) -> dict:
        if not is_admin:
            return _err("CronDelete requires admin session.")
        if not args.get("confirm"):
            return _err("confirm=true required for delete.")
        aid = (args.get("automation_id") or "").strip()
        if not aid:
            return _err("automation_id is required.")
        try:
            from app.db import automations as auto_db
            from app.core.automation_manager import automation_manager
            try:
                await automation_manager.unschedule_automation(aid)
            except AttributeError:
                pass  # older impl
            except Exception:
                logger.exception("Failed to unregister cron from scheduler")
            ok = await asyncio.to_thread(auto_db.delete_automation, aid)
            return _ok({"deleted": ok, "automation_id": aid})
        except Exception as e:
            logger.exception("CronDelete failed")
            return _err(f"delete failed: {e}")

    all_tools.append(cron_delete)

    # ------------------------------------------------------------------
    # RemoteTrigger — fire an existing automation on demand
    # ------------------------------------------------------------------
    @tool(
        "RemoteTrigger",
        "Trigger an existing automation immediately. Use for ad-hoc runs of scheduled agents.",
        {
            "type": "object",
            "properties": {
                "automation_id": {"type": "string"},
            },
            "required": ["automation_id"],
        },
    )
    async def remote_trigger(args: dict) -> dict:
        aid = (args.get("automation_id") or "").strip()
        if not aid:
            return _err("automation_id is required.")
        try:
            from app.core.automation_manager import automation_manager
            result = await automation_manager.trigger_manual(aid)
            if result is None:
                return _err("automation not found.", automation_id=aid)
            return _ok(result)
        except Exception as e:
            logger.exception("RemoteTrigger failed")
            return _err(f"trigger failed: {e}")

    all_tools.append(remote_trigger)

    # ------------------------------------------------------------------
    # Monitor / TaskOutput / TaskStop — background shell
    # ------------------------------------------------------------------
    @tool(
        "Monitor",
        _monitor_description(),
        {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "cwd": {
                    "type": "string",
                    "description": (
                        "Working directory for this command (default: the "
                        "session's own working directory). Set it instead of "
                        "prefixing the command with cd."
                    ),
                },
                "wait_seconds": {
                    "type": "integer",
                    "minimum": 0,
                    "maximum": MAX_WAIT_SECONDS,
                    "description": WAIT_SECONDS_PARAM,
                },
            },
            "required": ["command"],
        },
    )
    async def monitor(args: dict) -> dict:
        from app.core.monitor_spawn import (
            clamp_wait_seconds,
            monitor_inline_receipt_text,
            monitor_receipt_text,
            monitor_still_running_note,
            settle_inline_monitor_task,
            wait_for_inline_settle,
        )

        command = (args.get("command") or "").strip()
        if not command:
            return _err("command is required.")
        wait_seconds = clamp_wait_seconds(args.get("wait_seconds"))
        try:
            # Fall back to the SESSION's working directory, the way fluid's
            # Bash does. Without it the shell inherited the server's cwd and
            # every git/gh command in an audited worktree session died with
            # "fatal: not a git repository".
            task = await _spawn_background(
                command, args.get("cwd") or cwd, session_id
            )
            if session_id:
                from app.core.task_registry import task_registry
                # OWN-lane admission: without this the shell's terminal frame is
                # dropped as not-own work and a parked parent never wakes.
                task_registry.mark_monitor_shell_task(session_id, task.task_id)

            # A command that settles inside the requested wait comes back HERE,
            # with its exit code and output: no park, no wake, no fresh turn.
            if wait_seconds and await wait_for_inline_settle(task, wait_seconds):
                status, output = settle_inline_monitor_task(
                    task, command=command, cwd=task.cwd, session_id=session_id
                )
                failed = status != "completed"
                message = monitor_inline_receipt_text(
                    task.task_id, task.exit_code, output, failed=failed
                )
                # A non-zero exit is the command's RESULT, not a tool failure.
                # It used to come back through _err, and an is_error result is
                # raised by the code-execution runner: a red TDD baseline —
                # the expected outcome of what the model asked for — killed the
                # whole cell and every call queued behind it (five Edits lost
                # in one audited session; another wrapped every later Monitor
                # in try/except, which would swallow real harness failures
                # too). _err is now reserved for spawn/infra faults. The task
                # ROW still settles as "error" so the chip and the wake
                # notification stay honest about a command that failed.
                return _ok({
                    "message": message,
                    "task_id": task.task_id,
                    "exit_code": task.exit_code,
                    "status": status,
                    "failed": failed,
                    "output": output,
                })

            # The receipt COMMANDS the park (same wording as the native leg's
            # spawner and the TaskList nudge). Audited sessions read a bare
            # "started, here's your id" as licence to sleep-poll in foreground
            # Bash for a notification already queued for them.
            message = monitor_receipt_text(task.task_id)
            if wait_seconds:
                message += monitor_still_running_note(wait_seconds)
            return _ok({
                "message": message,
                "task_id": task.task_id,
                "pid": task.process.pid if task.process else None,
                "started_at": task.started_at,
            })
        except Exception as e:
            logger.exception("Monitor spawn failed")
            return _err(f"spawn failed: {e}")

    all_tools.append(monitor)

    @tool(
        "TaskOutput",
        "Read buffered stdout/stderr from a background task spawned by Monitor. "
        "Optionally pass since_line to get only new lines.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "since_line": {"type": "integer", "description": "Line index to start from (exclusive)."},
                "tail": {"type": "integer", "description": "Return at most N most recent lines."},
            },
            "required": ["task_id"],
        },
    )
    async def task_output(args: dict) -> dict:
        tid = (args.get("task_id") or "").strip()
        # SCOPED: the buffer belongs to a session, and until now any session
        # could read any other's. A worker's own task stays readable by the
        # parent that dispatched it — that is the case an audited parent could
        # not serve and re-ran a whole test suite over.
        task, owner_session_id, relation = _resolve_task_scope(tid, session_id)
        if relation == "foreign" and task is None:
            return _foreign_task_error(tid, owner_session_id)
        if not task:
            # Not a Monitor shell task — it may be a background AGENT task tracked
            # in the registry. Agent tasks have no buffered stdout here (their
            # output arrives via <task-notification> + the subagent transcript),
            # so return their live status/summary rather than a confusing
            # "not found" for an id the agent legitimately saw via TaskList.
            lookup_session = owner_session_id if relation == "lineage" else session_id
            if lookup_session:
                from app.core.task_registry import task_registry
                record = task_registry.get_task(lookup_session, tid)
                if record is not None:
                    payload = {
                        "task_id": tid,
                        "kind": record.get("kind"),
                        "status": record.get("status"),
                        "summary": record.get("summary"),
                        "lines": [],
                    }
                    # Task-tool workers (kind=agent, task id == worker session
                    # id): serve the worker's report from its durable event
                    # log so background dispatches are collectable (A8).
                    if record.get("kind") == "agent":
                        try:
                            from app.core import provider_agent as pa

                            outcome = pa.final_turn_outcome(tid)
                            if outcome.get("terminal_found"):
                                payload["worker_status"] = (
                                    "interrupted" if outcome.get("interrupted")
                                    else ("error" if outcome.get("error") else "completed")
                                )
                                if outcome.get("error"):
                                    payload["worker_error"] = outcome["error"]
                                report = pa.final_assistant_text(tid)
                                if report:
                                    payload["report"], payload["truncated"] = pa.bound_report(report)
                            else:
                                payload["note"] = (
                                    "Worker still running or no report yet. "
                                    "Completion is announced automatically; "
                                    "continue useful work instead of polling."
                                )
                        except Exception:  # noqa: BLE001
                            payload["note"] = (
                                "Background agent task — report unavailable; "
                                "see the worker session transcript."
                            )
                    else:
                        payload["note"] = (
                            "Background agent task — output is delivered via "
                            "<task-notification> and the subagent transcript; no "
                            "buffered stdout is available here."
                        )
                    if relation == "lineage":
                        payload["owner_session_id"] = lookup_session
                        payload["scope_note"] = _WORKER_TASK_SCOPE_NOTE
                    return _ok(payload)
            if owner_session_id:
                return _foreign_task_error(tid, owner_session_id)
            return _err("task not found.", task_id=tid)
        lines = task.lines
        since = int(args.get("since_line") or 0)
        if since > 0:
            lines = lines[since:]
        tail = args.get("tail")
        if tail is not None:
            try:
                lines = lines[-int(tail):]
            except (TypeError, ValueError):
                pass
        payload = {
            "task_id": tid,
            "running": task.is_running(),
            "exit_code": task.exit_code,
            "total_lines": len(task.lines),
            "lines": lines,
        }
        if relation == "lineage":
            payload["owner_session_id"] = owner_session_id
            payload["scope_note"] = _WORKER_TASK_SCOPE_NOTE
        return _ok(payload)

    all_tools.append(task_output)

    @tool(
        "TaskStop",
        "Stop a background task spawned by Monitor.",
        {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    )
    async def task_stop(args: dict) -> dict:
        tid = (args.get("task_id") or "").strip()
        result = await stop_background_shell_task(tid, session_id=session_id)
        if result is None:
            # A background AGENT task (SDK-managed) can't be force-stopped from
            # here — this tool only owns Monitor shell subprocesses. Say so
            # clearly instead of "not found" when the id is a real agent task the
            # registry knows.
            if session_id:
                from app.core.task_registry import task_registry
                record = task_registry.get_task(session_id, tid)
                if record is not None:
                    return _err(
                        "Background agent tasks are managed by the SDK and cannot "
                        "be stopped from here; they finish on their own and notify "
                        "via <task-notification>. Only Monitor shell tasks support "
                        "TaskStop.",
                        task_id=tid,
                        kind=record.get("kind"),
                        status=record.get("status"),
                    )
            return _err("task not found.", task_id=tid)
        if result.get("survivors"):
            # Never claim a stop the OS did not give us. The audited failure was
            # exactly this line answering success/running:false while port 8010
            # stayed LISTENING and the model believed the server was down.
            return _err(
                result["warning"],
                task_id=tid,
                running=True,
                survivors=result["survivors"],
            )
        return _ok({
            "task_id": tid,
            "running": result["running"],
            "exit_code": result["exit_code"],
        })

    all_tools.append(task_stop)

    # ------------------------------------------------------------------
    # TaskList — live view of this session's background tasks
    # ------------------------------------------------------------------
    @tool(
        "TaskList",
        "List this session's background tasks — background AGENT tasks (Task "
        "tool) and background shell commands (Monitor). You do NOT need this "
        "to find out whether a task finished: a completion wakes this session "
        "automatically and tells you. Call it only after such a wake, or when "
        "the user asks what is running. Never call it in a polling loop, and "
        "never call it to decide whether to keep waiting — park instead.",
        {
            "type": "object",
            "properties": {
                "include_finished": {
                    "type": "boolean",
                    "description": "Include recently-finished tasks (default true).",
                },
            },
        },
    )
    async def task_list(args: dict) -> dict:
        if not session_id:
            return _err("TaskList requires an active session context.")
        from app.core.task_registry import task_registry

        include_finished = args.get("include_finished", True)
        if include_finished:
            tasks = task_registry.get_all(session_id)
        else:
            tasks = task_registry.get_active(session_id)
        running_count = sum(1 for t in tasks if t.get("status") == "running")
        payload: Dict[str, Any] = {"tasks": tasks, "running_count": running_count}
        if _all_still_running(tasks):
            # Nothing here that a wake had not already told the caller. Say so
            # at the top of the result so the answer to "should I call this
            # again?" is in the same breath as the empty answer.
            payload = {"note": _PARK_NUDGE, **payload}
        return _ok(payload)

    all_tools.append(task_list)

    # ------------------------------------------------------------------
    # TaskGet — inspect ONE background task in detail
    # ------------------------------------------------------------------
    @tool(
        "TaskGet",
        "Get the details of ONE background task by task_id (background AGENT "
        "task or Monitor shell command). For a COMPLETED agent task this is "
        "how you read what the worker produced: it returns report_head (the "
        "start of the worker's report), report_chars, and report_path (the "
        "full report on disk — Read it only if the head does not answer your "
        "question). report_path is ALWAYS present for an agent task and is "
        "null when no readable report file exists; check it before using it. "
        "For shell tasks it returns whether the process is still running, its "
        "exit code, and the tail of its output buffer. "
        "Completions wake this session on their own, so this is for inspecting "
        "a task you were just woken about or one the user asked about — never "
        "a polling loop. Use TaskList to discover ids.\n"
        "\n"
        "A task belonging to a worker YOU dispatched is readable and comes "
        "back with owner_session_id set — but its completion wakes that "
        "worker, not you, so never park waiting for one. A task belonging to "
        "any other session is refused by name rather than as 'not found'.",
        {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "tail": {
                    "type": "integer",
                    "description": "For shell tasks, return at most N most recent output lines (default 20).",
                },
            },
            "required": ["task_id"],
        },
    )
    async def task_get(args: dict) -> dict:
        if not session_id:
            return _err("TaskGet requires an active session context.")
        tid = (args.get("task_id") or "").strip()
        if not tid:
            return _err("task_id is required.")
        from app.core.task_registry import task_registry

        # SCOPED. ``_background_tasks`` is process-global: one audited parent
        # named an id it had read in a WORKER's report, was handed that other
        # session's live shell plus "you will be woken on completion", parked,
        # and was never woken — nothing had subscribed it to that task. A
        # worker's task is still readable here (the parent dispatched it), but
        # it is labelled as the worker's and never carries the park nudge.
        shell, owner_session_id, relation = _resolve_task_scope(tid, session_id)
        if relation == "foreign" and shell is None:
            return _foreign_task_error(tid, owner_session_id)
        # ``durable=True``: the whole point of this call is a parent that was
        # WOKEN about a finished task, and the wake can land long after the
        # in-memory record has been evicted with its session. The registry
        # resolves live → retained snapshot → durable row, so "task not found"
        # now means genuinely never seen rather than merely no longer resident.
        lookup_session = owner_session_id if relation == "lineage" else session_id
        record = task_registry.get_task(lookup_session, tid, durable=True)
        if record is None and shell is None:
            if owner_session_id and owner_session_id != session_id:
                return _foreign_task_error(tid, owner_session_id)
            return _err("task not found.", task_id=tid)

        out: Dict[str, Any] = {"task": record}
        if relation == "lineage":
            out["owner_session_id"] = owner_session_id
        # Enrich with live process detail for Monitor shell tasks.
        if shell is not None:
            try:
                tail_raw = args.get("tail")
                tail_n = 20 if tail_raw is None else int(tail_raw)
            except (TypeError, ValueError):
                tail_n = 20
            out["running"] = shell.is_running()
            out["exit_code"] = shell.exit_code
            out["total_lines"] = len(shell.lines)
            out["lines"] = shell.lines[-tail_n:] if tail_n > 0 else []
        # A finished agent worker's actual output. This is the whole point of
        # the call for a parent that was just woken about its child.
        if record is not None and record.get("kind") == "agent":
            if record.get("status") == "completed":
                out.update(_agent_report_fields(tid, record))
            # ALWAYS present, null when there is no readable file. It used to be
            # written only inside the read-success branch, so an unreadable
            # report returned no key at all and the caller's `j['report_path']`
            # raised KeyError — which, in a code cell, took the rest of the
            # cell with it (one audited call, one dropped queued command).
            out.setdefault("report_path", None)
        still_running = (
            out.get("running") if shell is not None else _all_still_running([record])
        )
        if still_running:
            # The nudge is a PROMISE that a completion will wake this session.
            # It is only true for this session's own work: a worker's task
            # wakes the worker.
            out = {
                "note": (
                    _WORKER_TASK_SCOPE_NOTE if relation == "lineage" else _PARK_NUDGE
                ),
                **out,
            }
        return _ok(out)

    all_tools.append(task_get)

    # ------------------------------------------------------------------
    # ToolSearch — search local tool registry
    # ------------------------------------------------------------------
    @tool(
        "ToolSearch",
        "Search available tools by keyword or exact name. Returns tool metadata (name, category, description).",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keywords or 'select:Name1,Name2' for exact match."},
                "max_results": {"type": "integer", "description": "Default 5."},
            },
            "required": ["query"],
        },
    )
    async def tool_search(args: dict) -> dict:
        query = (args.get("query") or "").strip()
        if not query:
            return _err("query is required.")
        max_results = int(args.get("max_results") or 5)
        try:
            # The live surface (ruling T1): fluid's builtins plus the app's own
            # bridge tools. Deliberately NOT the MCP groups — enumerating those
            # spawns every configured server, which is far too much for a
            # keyword lookup inside a turn.
            from app.core.tool_catalog import app_tools, harness_tools

            catalog = {
                row["name"]: {
                    "name": row["name"],
                    "category": category,
                    "description": row.get("description") or "",
                    "risk": row.get("risk"),
                }
                for category, rows in (
                    ("builtin", harness_tools()),
                    ("app", app_tools()),
                )
                for row in rows
            }

            if query.startswith("select:"):
                names = [n.strip() for n in query[len("select:"):].split(",") if n.strip()]
                hits = [catalog[n] for n in names if n in catalog]
            else:
                terms = [t for t in query.lower().split() if t]
                scored: list = []
                for t in catalog.values():
                    hay = f"{t['name']} {t.get('description', '')}".lower()
                    score = sum(1 for term in terms if term in hay)
                    if score:
                        scored.append((score, t))
                scored.sort(key=lambda x: (-x[0], x[1]["name"]))
                hits = [t for _, t in scored[:max_results]]
            return _ok({"results": hits, "count": len(hits)})
        except Exception as e:
            logger.exception("ToolSearch failed")
            return _err(f"search failed: {e}")

    all_tools.append(tool_search)

    # ------------------------------------------------------------------
    # GetAppErrors — read the durable app error log
    # ------------------------------------------------------------------
    @tool(
        "GetAppErrors",
        "Read recent application errors (frontend, backend, API, and WebSocket) "
        "from AI Shuffle's error log so you can debug what the user just hit. Use "
        "this when the user says something like 'I got an error, check the logs and "
        "fix it'. Each entry has source, level, message, detail (stack trace), url, "
        "status_code, occurrence_count, and timestamps.\n"
        "\n"
        "An empty result means no ERROR-level record matched — NOT that nothing "
        "went wrong. Plenty of real failures (provider polls, auth refusals, "
        "degraded fallbacks) are logged at WARNING. If you are chasing something "
        "the user saw and this comes back empty, retry with min_level: "
        "\"WARNING\" before concluding the app is healthy.\n"
        "\n"
        "Output is capped server-side: stack traces are clipped and the entry "
        "list is cut off once the payload gets large. A `truncated` field says "
        "so — narrow with source / since_minutes / this_session_only rather "
        "than raising limit.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max errors to return (default 10, max 50)."},
                "min_level": {
                    "type": "string",
                    "enum": ["ERROR", "WARNING"],
                    "description": (
                        "Lowest level to include. ERROR (default) returns "
                        "ERROR and CRITICAL only; WARNING adds warnings — "
                        "where silently-degraded behaviour usually lands."
                    ),
                },
                "source": {"type": "string", "description": "Filter by source: frontend, backend, ws, or api."},
                "this_session_only": {"type": "boolean", "description": "Only errors tied to this chat session."},
                "since_minutes": {"type": "integer", "description": "Only errors seen within the last N minutes."},
            },
            "required": [],
        },
    )
    async def get_app_errors(args: dict) -> dict:
        try:
            from app.db import error_logs

            limit = max(1, min(int(args.get("limit") or 10), _APP_ERRORS_MAX_LIMIT))
            source = (args.get("source") or "").strip() or None
            min_level = (args.get("min_level") or "ERROR").strip().upper()
            levels = _APP_ERROR_LEVELS.get(min_level)
            if levels is None:
                return _err(
                    f"min_level must be one of "
                    f"{', '.join(sorted(_APP_ERROR_LEVELS))} (got {min_level!r})."
                )
            since = None
            since_minutes = args.get("since_minutes")
            if since_minutes:
                try:
                    since = datetime.now(timezone.utc) - timedelta(minutes=float(since_minutes))
                except (TypeError, ValueError):
                    since = None
            sess = session_id if args.get("this_session_only") else None

            errors = await asyncio.to_thread(
                error_logs.get_recent_errors,
                limit=limit,
                source=source,
                session_id=sess,
                since=since,
                levels=levels,
            )
            matched = len(errors)
            errors, truncated = _cap_app_errors(errors)
            payload = {
                "errors": errors,
                "count": len(errors),
                "min_level": min_level,
            }
            if truncated:
                payload["truncated"] = truncated
                payload["matched"] = matched
            elif min_level == "ERROR" and not errors:
                # The failure this tool was built to prevent: an agent read
                # count: 0 as "the app is fine" while the thing it was chasing
                # sat one level down.
                payload["note"] = (
                    "no ERROR-level records matched — this is not proof nothing "
                    "went wrong; retry with min_level: \"WARNING\"."
                )
            return _ok(payload)
        except Exception as e:
            logger.exception("GetAppErrors failed")
            return _err(f"failed to read errors: {e}")

    all_tools.append(get_app_errors)

    # ------------------------------------------------------------------
    # GetAppLogs — read the in-memory runtime log ring buffer
    # ------------------------------------------------------------------
    @tool(
        "GetAppLogs",
        "Read AI Shuffle's own runtime log — the same in-memory ring buffer "
        "the admin Logs view shows, every level including INFO and DEBUG. Use "
        "it to see what the SERVER did: which code path ran, what a background "
        "task or a wake did, why a request took the branch it took.\n"
        "\n"
        "GetAppErrors is the durable ERROR/WARNING table; this is the live "
        "narrative around them, and it is where anything logged below WARNING "
        "lives. Reach for it when GetAppErrors comes back empty but something "
        "clearly happened. The buffer holds the most recent ~5000 lines and "
        "does not survive a restart.\n"
        "\n"
        "Filter before you read: `search` is a case-insensitive substring "
        "(a session id, a task id, a function name) and is by far the best "
        "way to make the answer small. Output is capped server-side.",
        {
            "type": "object",
            "properties": {
                "limit": {
                    "type": "integer",
                    "description": (
                        f"Max lines to return, newest first "
                        f"(default {_APP_LOGS_DEFAULT_LIMIT}, max "
                        f"{_APP_LOGS_MAX_LIMIT})."
                    ),
                },
                "level": {
                    "type": "string",
                    "description": (
                        "Exact level to return: DEBUG, INFO, WARNING, ERROR or "
                        "CRITICAL. Omit for all levels."
                    ),
                },
                "search": {
                    "type": "string",
                    "description": (
                        "Case-insensitive substring the line must contain — a "
                        "session id, task id, logger name or message fragment."
                    ),
                },
                "since_minutes": {
                    "type": "number",
                    "description": "Only lines written within the last N minutes.",
                },
            },
            "required": [],
        },
    )
    async def get_app_logs(args: dict) -> dict:
        try:
            from app.api.logs import ring_handler

            try:
                limit = int(args.get("limit") or _APP_LOGS_DEFAULT_LIMIT)
            except (TypeError, ValueError):
                limit = _APP_LOGS_DEFAULT_LIMIT
            limit = max(1, min(limit, _APP_LOGS_MAX_LIMIT))
            level = (args.get("level") or "").strip().upper() or None
            if level is not None and level not in _APP_LOG_LEVELS:
                return _err(
                    f"level must be one of {', '.join(sorted(_APP_LOG_LEVELS))} "
                    f"(got {level!r})."
                )
            search = (args.get("search") or "").strip().lower() or None
            cutoff = None
            since_minutes = args.get("since_minutes")
            if since_minutes:
                try:
                    cutoff = datetime.now(timezone.utc) - timedelta(
                        minutes=float(since_minutes)
                    )
                except (TypeError, ValueError):
                    cutoff = None

            buffer = list(ring_handler.buffer)
            lines, matched, used = [], 0, 0
            for record in reversed(buffer):  # newest first
                if level and record.level != level:
                    continue
                if search and search not in record.message.lower():
                    continue
                if cutoff is not None and not _log_record_after(record, cutoff):
                    continue
                matched += 1
                if len(lines) >= limit or used >= _APP_LOGS_MAX_CHARS:
                    continue
                message = _clip(record.message, _APP_LOGS_MAX_LINE_CHARS)
                used += len(message)
                lines.append({
                    "timestamp": record.timestamp,
                    "level": record.level,
                    "logger": record.logger_name,
                    "message": message,
                })
            payload: Dict[str, Any] = {
                "lines": lines,
                "count": len(lines),
                "matched": matched,
                "buffered": len(buffer),
            }
            if matched > len(lines):
                payload["truncated"] = (
                    f"{matched - len(lines)} more matching line(s) omitted. "
                    "Narrow with search / level / since_minutes rather than "
                    "raising limit."
                )
            elif not lines:
                payload["note"] = (
                    "nothing matched. The buffer holds only this process's "
                    "recent lines and is empty after a restart; try a wider "
                    "search or drop the level filter."
                )
            return _ok(payload)
        except Exception as e:
            logger.exception("GetAppLogs failed")
            return _err(f"failed to read logs: {e}")

    all_tools.append(get_app_logs)

    # ------------------------------------------------------------------
    # App-memory bridge — Remember / Recall / SessionSearch / SessionView.
    # These exist for the DELEGATED (claude CLI) leg, which otherwise sees
    # none of the app's memory (see docs/MEMORY_ARCHITECTURE.md): the CLI
    # subprocess only receives tools via MCP, so the fluid builtins backed by
    # the same stores are re-exposed here. On native legs the bridged copies
    # are shadowed (fluid_tool_bridge.NATIVE_SHADOWED_BUILTINS) in favor of
    # fluid's bare-named originals.
    # ------------------------------------------------------------------
    try:
        all_tools.extend(
            _build_memory_bridge_tools(tool, session_id=session_id, cwd=cwd)
        )
    except Exception:  # noqa: BLE001 — memory is context, never a blocker
        logger.warning("memory bridge tool registration failed", exc_info=True)

    # ------------------------------------------------------------------
    # Connector action-gateway tools ({type}_actions / {type}_invoke) —
    # registered ONLY when this AI session is linked to a gateway-enabled
    # connector session. This is the seam that makes connector capabilities
    # available to connector sessions (and to nobody else).
    # ------------------------------------------------------------------
    if session_id:
        try:
            from app.mcp.connector_gateway_sdk import build_gateway_tools
            all_tools.extend(build_gateway_tools(session_id))
        except Exception:
            logger.debug("Connector gateway tool registration skipped", exc_info=True)

    # ------------------------------------------------------------------
    # Server assembly
    # ------------------------------------------------------------------
    logger.info(
        "Registering %d builtin tool(s) as MCP tools (session=%s, project=%s, admin=%s)",
        len(all_tools), session_id, project_id, is_admin,
    )
    return patch_sdk_server(
        create_sdk_mcp_server(name="builtins", version="1.0.0", tools=all_tools),
        all_tools,
    )


def get_builtin_tool_names() -> List[str]:
    """Return MCP tool names so they can be added to ``allowed_tools``."""
    names = [
        "mcp__builtins__ScheduleWakeup",
        "mcp__builtins__Task",
        "mcp__builtins__NotifyDevices",
        "mcp__builtins__EnterWorktree",
        "mcp__builtins__ExitWorktree",
        "mcp__builtins__CronCreate",
        "mcp__builtins__CronList",
        "mcp__builtins__CronDelete",
        "mcp__builtins__RemoteTrigger",
        "mcp__builtins__Monitor",
        "mcp__builtins__TaskOutput",
        "mcp__builtins__TaskStop",
        "mcp__builtins__TaskList",
        "mcp__builtins__TaskGet",
        "mcp__builtins__ToolSearch",
        "mcp__builtins__GetAppErrors",
        "mcp__builtins__GetAppLogs",
        # The app-memory bridge (delegated leg; shadowed on native legs where
        # fluid's bare-named builtins serve the same stores). Allow-listed
        # unconditionally — per-session registration gates real availability.
        "mcp__builtins__Remember",
        "mcp__builtins__Recall",
        "mcp__builtins__SessionSearch",
        "mcp__builtins__SessionView",
    ]
    # Connector gateway tools share the `builtins` server namespace. Allow-list
    # them unconditionally — per-session REGISTRATION (only for linked
    # sessions, in create_builtin_tools_server) is what gates real availability.
    try:
        from app.mcp.connector_gateway_sdk import get_gateway_tool_names
        names.extend(get_gateway_tool_names())
    except Exception:
        logger.debug("Could not add connector gateway tool names to allowed_tools", exc_info=True)
    return names
