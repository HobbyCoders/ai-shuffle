"""monitor_spawn — the host-side spawner for fluid's bare Monitor builtin.

Fluid owns the tool's SHAPE (bare name, schema, description); the host owns the
supervision (ruling A13, Task-parity). The native leg injects the callable
returned by :func:`make_fluid_monitor_spawner` as
``ToolCtx.extra["monitor_spawn"]``; without it fluid's Monitor reports itself
unavailable.

The spawner reuses :func:`app.mcp.builtin_tools._spawn_background` — the SAME
detached-subprocess registry the MCP ``Monitor``/``TaskOutput``/``TaskStop``
tools serve — so a fluid-Monitor shell shows up in ``TaskList``, is stoppable,
and is reaped with its session exactly like an MCP-Monitor one. On completion
the output tail becomes the frame ``summary`` and the terminal handoff goes to
:func:`app.core.turn_scheduler.on_task_terminal`, the sole unpark producer —
the same seam a ProviderAgent worker's completion uses — so a parked parent is
woken with the command's output instead of having to poll.

Three guardrails live here, all from the round-4 park audit:

* a DEFAULT TIME BUDGET (``monitor_command_timeout_seconds``) — the completion
  fires on exit and only on exit, so a command that cannot exit holds the
  session parked until the session ends. On expiry the process is stopped and
  the completion fires with a "no result after Ns" summary: parked forever
  becomes woken with a timeout. Skipped for a ``serves`` process and for a
  command that already carries its own bound.
* an HONEST REAP (:func:`settle_monitor_task`) — the registry's recorded
  status is consulted BEFORE the exit code, so a deliberately stopped shell
  arrives as a cancellation with a cause line instead of "(no output; exit
  code None)", which an audited session read as a crash and re-ran by hand.
* a BOUNDED WAKE SUMMARY (``monitor_summary_max_chars``) — and, for a
  deliberate stop, the attribution REPLACES the output tail rather than
  riding along with it. The full buffer stays available via TaskOutput.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Fraction of the summary budget spent on the HEAD of the output; the rest is
# the tail. Failures name themselves at the end far more often than at the
# start, so the tail gets the larger share.
_SUMMARY_HEAD_FRACTION = 0.3

# Bound for the log-file tail folded into a summary whose buffer came back
# empty (see :func:`_log_tail`). Kept under the summary budget so the tail
# cannot itself blow the cap.
_LOG_TAIL_BYTES = 2_000

# How often the completion watcher looks at the process (and at its deadline).
_WATCH_POLL_SECONDS = 0.5

# Registry ``reason`` stamps this module writes. They are what turns a
# deliberate stop into a CAUSE the model can read, instead of the bare
# "(no output; exit code None)" an audited session read as a crash and
# re-launched work over.
MONITOR_TIMEOUT_REASON = "monitor_timeout"

# Registry statuses that mean the task was stopped ON PURPOSE — by the user,
# by the app's own reaper, by an agent's TaskStop, or by the time budget below.
# The registry is the authority here: the exit code cannot tell a deliberate
# SIGTERM apart from a crash (on Windows both surface as 1, and a SIGKILL
# leaves no code at all).
_DELIBERATE_STATUSES = frozenset({"killed", "stopped", "cancelled"})

# POSIX signal exit codes that mean "something stopped this", not "this failed".
_SIGNAL_EXIT_CODES = frozenset({-2, -9, -15})

# Evidence that the command carries its OWN time budget or iteration cap, in
# which case the default deadline below stays out of its way. Mirrors the
# vocabulary of fluid's Monitor spawn-time lint; kept local so the bound does
# not depend on a vendored-harness version.
_OWN_TIME_BOUND_RE = re.compile(
    r"""\bg?timeout\s+(?:-\S+\s+)*\d"""          # timeout 900 …
    r"""|--(?:timeout|max-time|deadline|wait-timeout|time-limit)\b"""
    r"""|\btimeout=\d"""
    r"""|\bdate\s+\+%s"""                         # deadline arithmetic
    r"""|\$SECONDS\b|\bSECONDS="""
    r"""|\bin\s+\$\(\s*seq\b"""                   # for i in $(seq 1 60)
    r"""|\bfor\s*\(\([^;]*;[^;]*[<>][^;]*;"""     # for ((i=0; i<60; i++))
    r"""|-(?:lt|le)\s"""                          # while [ $i -lt 60 ]
)

# Redirect / tee targets in a monitored command line. Matches `> f`, `>> f`,
# `2> f`, `1>>f` and `| tee [-a] f`; quoted paths keep their spaces. `2>&1`
# does NOT match because a target may not start with `&`.
_REDIRECT_RE = re.compile(
    r"""(?:\d?>>?|\|\s*tee(?:\s+-a)?)\s*"""
    r"""(?P<path>"[^"]+"|'[^']+'|[^\s;|&<>()]+)"""
)

# Redirect targets that are sinks, not logs.
_NULL_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "nul", "NUL"})


def monitor_receipt_text(task_id: str) -> str:
    """The receipt every Monitor spawn hands back to the model.

    Fluid's default is the bare fact ("Monitoring started (task X)."), and
    audited sessions read that as an invitation to sleep-poll in foreground
    Bash for a notification already queued for them — 9 of 10 spawns in the
    worst session, 24 minutes of polling. The receipt has to give the
    instruction, mirroring the TaskList nudge that stopped the same loop there.
    Shared with the MCP ``Monitor`` tool so both legs say the same thing.

    The final clause is fluid's, restored: the app's copy had dropped "Do not
    sleep, poll, or check on it yourself", and the same sessions went on to
    sleep-poll with `wait`+`curl` after every spawn.
    """
    return (
        f"Monitoring started (task {task_id}). End the turn now — you will be "
        "woken when it comes to rest. Do not sleep, poll, or check on it "
        "yourself."
    )


def monitor_serving_receipt_text(task_id: str) -> str:
    """The receipt for a ``serves`` spawn — a process MEANT to stay up.

    A serving task must not be read as pending work: the session does not park
    on it and no completion is coming while it is healthy, so the receipt says
    so rather than leaving the model waiting for a wake that never arrives.
    """
    return (
        f"Serving task {task_id} started and is expected to stay up. The "
        "session is NOT parked on it — end the turn whenever this step is "
        "done; no completion is coming while it is healthy, and you are "
        "notified if it stops or dies. Do not sleep, poll, or check on it "
        "yourself."
    )


def command_has_own_time_bound(command: str) -> bool:
    """Whether ``command`` already declares a time budget or iteration cap."""
    return bool(_OWN_TIME_BOUND_RE.search(command or ""))


def monitor_deadline_seconds(command: str, *, serves: bool) -> int:
    """The time budget to enforce on ``command`` — 0 for "no deadline".

    Three ways out of the default budget, in order: the model declared the
    process a service (``serves``), the operator disabled the backstop, or the
    command already carries its own bound. Everything else gets the default so
    a watcher that cannot exit wakes the session with a timeout instead of
    holding it parked until the session ends.
    """
    if serves:
        return 0
    try:
        from app.core.config import settings

        budget = int(getattr(settings, "monitor_command_timeout_seconds", 0) or 0)
    except Exception:  # noqa: BLE001 — config must never break a spawn
        return 0
    if budget <= 0 or command_has_own_time_bound(command):
        return 0
    return budget


def _strip_ansi(text: str) -> str:
    """Fluid's ANSI scrubber, imported lazily so this module stays importable
    without the vendored harness on the path."""
    try:
        from fluid.tools.builtins._util import strip_ansi
    except Exception:  # noqa: BLE001 — cosmetics must never break a completion
        return text
    return strip_ansi(text)


def _summary_budget() -> int:
    try:
        from app.core.config import settings

        return max(120, int(getattr(settings, "monitor_summary_max_chars", 1000)))
    except Exception:  # noqa: BLE001 — config must never break a completion
        return 1000


def _summarize(lines: list, *, budget: Optional[int] = None) -> str:
    """Head+tail of the buffered output, capped at the wake-summary budget.

    The cap is about what a WAKE TURN has to read, not about what is kept: the
    full buffer stays available through TaskOutput. Audited wake turns were
    handed 12KB stack-trace tails from servers the agent had killed itself and
    spent the entire turn reading them.
    """
    # Scrub BEFORE slicing: escape sequences are gibberish to the model (and
    # to the UI), and counting them against the head/tail budget evicts real
    # output. Ruling A13 makes the app the supervisor here, so fluid's own
    # supervisor-side strip never runs for these buffers.
    text = _strip_ansi("\n".join(lines))
    cap = _summary_budget() if budget is None else budget
    if len(text) <= cap:
        return text
    head = int(cap * _SUMMARY_HEAD_FRACTION)
    tail = cap - head
    dropped = len(text) - head - tail
    return (
        text[:head]
        + f"\n... [{dropped} chars truncated — TaskOutput has the full buffer] ...\n"
        + text[-tail:]
    )


def _redirect_targets(command: str) -> List[str]:
    """Ordered, de-duplicated redirect/tee targets named by ``command``."""
    seen: List[str] = []
    for match in _REDIRECT_RE.finditer(command or ""):
        path = (match.group("path") or "").strip().strip("\"'")
        if not path or path in _NULL_SINKS or path in seen:
            continue
        seen.append(path)
    return seen


def _log_tail(command: str, cwd: Optional[str]) -> str:
    """A bounded tail of the log file ``command`` redirected its output into.

    The audited failure: 8 of 9 monitor notifications said "(no output; exit
    code 0)" because the monitored command sent everything to a log file. The
    buffer is legitimately empty in that case, and the one thing the parent
    wants is the end of that file. Redirect targets are the only path the
    monitor can know without guessing, so that is all this reads — the LAST one
    named (the command's own final sink) that exists and is readable.
    """
    targets = _redirect_targets(command)
    for path in reversed(targets):
        candidate = path if os.path.isabs(path) else os.path.join(cwd or "", path)
        try:
            if not os.path.isfile(candidate):
                continue
            size = os.path.getsize(candidate)
            with open(candidate, "rb") as fh:
                if size > _LOG_TAIL_BYTES:
                    fh.seek(size - _LOG_TAIL_BYTES)
                raw = fh.read()
        except OSError:
            continue
        text = _strip_ansi(raw.decode("utf-8", errors="replace")).strip()
        if not text:
            continue
        prefix = "" if size <= _LOG_TAIL_BYTES else "... [earlier lines omitted] ...\n"
        return f"(no buffered output; tail of {path})\n{prefix}{text}"
    return ""


def _recorded_status(session_id: Optional[str], task_id: str) -> Tuple[str, str, str]:
    """``(status, reason, summary)`` the registry recorded for this task.

    Empty strings when nothing is recorded. The registry is consulted BEFORE
    the exit code because it is the only place that knows a stop was
    deliberate: every kill site (user stop, server shutdown, TaskStop, session
    reap, the time budget below) stamps the record, while the exit code of a
    SIGTERM'd process is indistinguishable from a crash — and a SIGKILL'd one
    has no exit code at all.
    """
    if not session_id:
        return "", "", ""
    try:
        from app.core.task_registry import task_registry

        record = task_registry.get_task(session_id, task_id) or {}
    except Exception:  # noqa: BLE001 — the registry is best-effort
        return "", "", ""
    return (
        str(record.get("status") or ""),
        str(record.get("reason") or ""),
        str(record.get("summary") or ""),
    )


def _cause_line(reason: str, recorded_summary: str, *, serves: bool) -> str:
    """One line naming WHO stopped this task and why.

    This replaces the output tail for a deliberate stop. The audited case: an
    agent killed a dev server itself, then burned a whole wake turn reading the
    12KB starlette traceback that death produced, only to conclude "that's the
    old process we killed". The traceback carried no information the parent did
    not already have; the attribution is the whole message.
    """
    if reason == MONITOR_TIMEOUT_REASON:
        return recorded_summary or "Stopped: the monitor's time budget expired."
    from app.core.task_registry import SERVER_SHUTDOWN_REASON, USER_STOP_REASON

    if reason == USER_STOP_REASON:
        who = "Stopped by the user."
    elif reason == SERVER_SHUTDOWN_REASON:
        who = "Stopped because the server restarted."
    elif reason:
        who = f"Stopped by the app ({reason})."
    elif recorded_summary:
        who = f"Stopped: {recorded_summary}"
    else:
        who = "Stopped deliberately by the app or by an agent, not by a crash."
    what = (
        " The service is no longer running; restart it only if you still need it."
        if serves
        else " This was not a failure — do not re-run the work unless you still need it."
    )
    return who + what


def settle_monitor_task(
    *,
    exit_code: Optional[int],
    lines: list,
    command: str,
    cwd: Optional[str],
    session_id: Optional[str],
    task_id: str,
    serves: bool,
) -> Tuple[str, str]:
    """``(status, summary)`` for a Monitor shell that has come to rest.

    Ordering is the whole point. The old code derived status from the exit code
    alone, so a deliberately reaped shell reached the model as
    ``error`` / "(no output; exit code None)" — read as a crash, re-launched by
    hand. The recorded status wins over the exit code, and a deliberate stop
    settles as ``killed`` (which the fluid adapter maps to ``cancelled``) with
    a cause line INSTEAD of an output tail.
    """
    recorded, reason, recorded_summary = _recorded_status(session_id, task_id)
    deliberate = recorded in _DELIBERATE_STATUSES or (
        exit_code is not None and exit_code in _SIGNAL_EXIT_CODES
    )
    if deliberate:
        return "killed", _cause_line(reason, recorded_summary, serves=serves)
    if exit_code is None:
        # No recorded stop and no exit code: the process was reaped out from
        # under the watcher. Still a stop, not a failure.
        return "killed", _cause_line("", "", serves=serves)

    summary = _summarize(lines)
    if not summary:
        # Empty buffer usually means the command redirected into a log file;
        # the tail of that file is the result the parent was woken for.
        try:
            summary = _log_tail(command, cwd)
        except Exception:  # noqa: BLE001 — best-effort nicety
            logger.debug("monitor_spawn: log-tail read failed", exc_info=True)
            summary = ""
    if serves and exit_code == 0:
        return "completed", (
            "The service exited cleanly on its own (exit code 0). "
            + (summary or "(no output)")
        )
    if not summary:
        summary = f"(no output; exit code {exit_code})"
    return ("completed" if exit_code == 0 else "error"), summary


async def _enforce_deadline(
    task: Any, session_id: Optional[str], budget_seconds: int
) -> None:
    """Stop ``task`` when its time budget expires, with attribution.

    The registry record is stamped FIRST so the terminal chip and the wake
    summary both carry the cause: the stop path's own ``complete_task`` runs
    second and, passing no summary/reason, leaves this attribution in place.
    """
    summary = (
        f"Stopped: no result after {budget_seconds}s, the monitor's time "
        "budget. The command was still running and was stopped — it did not "
        "fail. Give the next attempt its own bound, or use the job's own "
        "blocking wait."
    )
    if session_id:
        try:
            from app.core.task_registry import task_registry

            task_registry.complete_task(
                session_id, task.task_id, "killed",
                summary=summary, reason=MONITOR_TIMEOUT_REASON,
            )
        except Exception:  # noqa: BLE001 — attribution is best-effort
            logger.debug("monitor_spawn: timeout attribution failed", exc_info=True)
    try:
        from app.mcp.builtin_tools import stop_background_shell_task

        await stop_background_shell_task(task.task_id, session_id)
    except Exception:  # noqa: BLE001 — a stop failure must not strand the wake
        logger.warning(
            "monitor_spawn: stopping timed-out task %s failed",
            task.task_id, exc_info=True,
        )
    logger.warning(
        "monitor_spawn: task %s hit its %ss time budget and was stopped",
        task.task_id, budget_seconds,
    )


def make_fluid_monitor_spawner(
    session_id: "str | Callable[[], Optional[str]]",
) -> Callable[[Dict[str, Any], Any], Any]:
    """The ``ctx.extra['monitor_spawn']`` callable for fluid's bare Monitor.

    ``session_id`` mirrors :mod:`app.core.task_dispatch`: the caller's APP
    session id, possibly a zero-argument supplier resolved at CALL time (a
    brand-new native session has no id at connect time; a connect-time
    snapshot would stay empty forever).
    """

    async def _spawn(input: Dict[str, Any], ctx: Any) -> Any:
        from fluid.tools.registry import BackgroundTaskReceipt

        command = str((input or {}).get("command") or "").strip()
        if not command:
            from fluid.tools.registry import ToolOutcome

            payload = {"ok": False, "error": "command is required."}
            return ToolOutcome(text=payload["error"], ui=payload, is_error=True)

        caller_session_id: Any = session_id
        if callable(caller_session_id):
            try:
                caller_session_id = caller_session_id()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "monitor_spawn: caller session-id supplier raised",
                    exc_info=True,
                )
                caller_session_id = None

        cwd = input.get("cwd")
        # A SERVICE, not a job (fluid's Monitor `serves` input): a process
        # meant to stay up. It gets no time budget, its SIGTERM is a stop
        # rather than a failure, and fluid keeps it out of park accounting so
        # the turn that started it can end without parking on it.
        serves = bool((input or {}).get("serves"))
        budget = monitor_deadline_seconds(command, serves=serves)
        from app.mcp.builtin_tools import _spawn_background

        # The launching tool_use_id, which fluid's native loop publishes on the
        # ctx for the duration of the dispatch. Without it a host Monitor shell
        # registers with tool_use_id NULL and its terminal chip has no key to
        # join the launching card on (reducer.ts task.updated → findSubagentByTask).
        launch_tool_use_id = None
        try:
            launch_tool_use_id = (getattr(ctx, "extra", None) or {}).get("tool_use_id")
        except Exception:  # noqa: BLE001 — a join key must never fail a spawn
            launch_tool_use_id = None

        task = await _spawn_background(
            command, cwd, caller_session_id, tool_use_id=launch_tool_use_id,
        )

        if caller_session_id:
            from app.core.task_registry import task_registry
            # OWN-lane admission: without this the watcher's terminal frame is
            # dropped by on_task_terminal's ownership gate as not-own work and a
            # parked parent never wakes.
            task_registry.mark_monitor_shell_task(caller_session_id, task.task_id)

        # Completion watcher: fold the output tail into the registry record and
        # fire the scheduler's terminal seam so a parked parent wakes with the
        # result. Detached — it must outlive this tool call by construction.
        if caller_session_id:
            import asyncio

            async def _on_exit() -> None:
                deadline = (time.monotonic() + budget) if budget else None
                while task.is_running():
                    if deadline is not None and time.monotonic() >= deadline:
                        # "Parked forever" becomes "woken with a timeout": stop
                        # the process, then fall through to the normal settle,
                        # which reads the attribution back off the registry.
                        await _enforce_deadline(task, caller_session_id, budget)
                        break
                    await asyncio.sleep(_WATCH_POLL_SECONDS)
                exit_code = task.exit_code
                status, summary = settle_monitor_task(
                    exit_code=exit_code,
                    lines=task.lines,
                    command=command,
                    cwd=cwd,
                    session_id=caller_session_id,
                    task_id=task.task_id,
                    serves=serves,
                )
                try:
                    from app.core.task_registry import task_registry

                    task_registry.update_task(
                        caller_session_id, task.task_id, summary=summary
                    )
                except Exception:  # noqa: BLE001 — registry is best-effort
                    logger.debug("monitor_spawn: summary write failed", exc_info=True)
                try:
                    from app.core import turn_scheduler

                    await turn_scheduler.on_task_terminal(caller_session_id, {
                        "task_id": task.task_id,
                        "status": status,
                        "summary": summary,
                        "kind": "shell",
                    })
                except Exception:  # noqa: BLE001 — a wake failure must never raise here
                    logger.warning(
                        "monitor_spawn: terminal handoff failed for task %s",
                        task.task_id, exc_info=True,
                    )

            loop = asyncio.get_running_loop()
            watcher = loop.create_task(_on_exit())
            watcher.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)

        from fluid.tools.registry import ToolOutcome

        # ``serves`` rides on the receipt so fluid keeps the task out of park
        # accounting. Set defensively: a vendored fluid predating the field
        # would reject the kwarg, and a Monitor spawn must not fail over a
        # harness version skew.
        receipt_kwargs: Dict[str, Any] = {
            "task_id": task.task_id,
            "description": command[:120],
        }
        try:
            receipt = BackgroundTaskReceipt(serves=serves, **receipt_kwargs)
        except TypeError:
            receipt = BackgroundTaskReceipt(**receipt_kwargs)
        # A ToolOutcome (not the bare receipt) so the app owns the RECEIPT
        # STRING too: fluid's default renders "Monitoring started (task X)."
        # and audited sessions read that as permission to keep waiting — one
        # sleep-polled in foreground Bash after 9 of 10 spawns. The instruction
        # to park has to be in the receipt itself, the way the TaskList nudge is.
        text = (
            monitor_serving_receipt_text(task.task_id)
            if serves
            else monitor_receipt_text(task.task_id)
        )
        if budget:
            text += (
                f" It carries a {budget}s time budget: if it is still running "
                "then, it is stopped and you are woken with a timeout."
            )
        return ToolOutcome(text=text, background_task=receipt)

    return _spawn
