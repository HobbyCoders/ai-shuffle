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

Three more from the round-6 audit:

* the SESSION CWD (``ctx.cwd``) when the model named none, exactly as fluid's
  Bash resolves it. The shell used to inherit the server's directory, and four
  audited worktree sessions lost a park+wake+turn each to "fatal: not a git
  repository".
* an INLINE WAIT (``wait_seconds``) — a command that settles inside the window
  returns its exit code and output in the receipt, so a six-second test run no
  longer costs a park, a wake, a fresh turn and a full prompt re-send. The
  still-running case is unchanged, receipt wording included.
* an OUTCOME-DERIVED STATUS (:func:`record_settled_status`) — a watcher that
  swallowed a failure (``… || true``) exits 0 while its own last word is
  ``EXIT:1``, and the notification said "completed" over a summary that said
  the run had failed. The row and the notification are now written from, and
  re-read at, one place.

And four from round 7:

* a NON-ZERO EXIT IS DATA — an inline settle no longer rides on ``is_error``.
  The code-mode runner raises on that flag, so a red TDD baseline aborted the
  whole cell and every call queued behind it. The registry row still says
  ``error`` (the chip and the wake notification must stay honest about a
  command that failed); only the tool envelope changed.
* a TAIL-BIASED, VERDICT-PRESERVING SUMMARY (:func:`_summarize`) — head+tail
  truncation deleted exactly the ``Tests 45 passed`` block, which sits in the
  MIDDLE of a chained command's buffer, and repeated watch-status lines filled
  the rest.
* a SESSION-SCOPED BOUND FOR SERVICES (``monitor_serving_idle_seconds``) — a
  ``serves`` process outlived its session by 8h15m on an audited runner.
* an HONEST SERVICE EXIT — a server that goes away on its own settles as a
  stop with a capped tail, not as a failure with 34k of access log.
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Share of the summary budget reserved for VERDICT lines rescued out of the
# elided middle (see :func:`_verdict_lines`). Whatever the verdicts do not use
# goes back to the tail, so a build log with no summary lines in it is a pure
# tail window.
_SUMMARY_VERDICT_FRACTION = 0.35

# At most this many verdict lines are rescued, newest first. A run with one
# failure per test file would otherwise fill the whole budget with them.
_SUMMARY_MAX_VERDICT_LINES = 12

# Preserve command verdicts when summarizing chained output. Test/check summary blocks can sit between command
# segments rather than at the end of the full buffer, so head-and-tail truncation alone can discard them.
_VERDICT_RE = re.compile(
    r"\b\d+\s+(?:passed|failed|skipped|errors?|warnings?|passing|failing"
    r"|pending|todo|xfailed|xpassed|deselected)\b"
    r"|\b(?:Test Files|Tests|Suites|Snapshots|Tests:|Suites:)\b"
    r"|\b(?:FAILED|FAIL|PASSED|ERROR|CRITICAL)\b"
    r"|\bfound \d+ (?:error|problem)"
    r"|\bnpm ERR!"
    r"|\bTraceback \(most recent call last\)"
    r"|\bAll checks (?:were successful|have passed)"
    r"|\bBUILD (?:SUCCESSFUL|FAILED)"
    r"|\bcompiled (?:successfully|with)"
    r"|\bEXIT[:=]\s*-?\d"
    r"|\bexit(?:ed with)? code\s*-?\d",
    re.IGNORECASE,
)

# Longest repeating BLOCK the collapser looks for. A `--watch` command prints
# the same two- or three-line status block every poll; one audited summary was
# ~8KB of `Refreshing checks status every 10 seconds.` and its sibling line,
# with the two lines that mattered pushed to the very end.
_MAX_REPEAT_PERIOD = 8

# How many times a block must appear before it is collapsed. Three, not two:
# collapsing a line that merely appears twice costs more characters than it
# saves and makes ordinary output look edited.
_MIN_REPEATS = 3

# Bound for the log-file tail folded into a summary whose buffer came back
# empty (see :func:`_log_tail`). Kept under the summary budget so the tail
# cannot itself blow the cap.
_LOG_TAIL_BYTES = 2_000

# How often the completion watcher looks at the process (and at its deadline).
_WATCH_POLL_SECONDS = 0.5

# wait_seconds is a bounded inline-settle window for short commands. Return a completed command in its receipt
# instead of creating an unnecessary park and wake; detach genuinely longer work.
MAX_WAIT_SECONDS = 120

# How often the inline wait looks at the process. Much tighter than the
# completion watcher's poll — the whole point is to return the moment the
# command exits.
_INLINE_POLL_SECONDS = 0.1

# After the process is gone, how long the inline wait gives the output reader
# to finish draining the pipe and record the exit code.
_DRAIN_GRACE_SECONDS = 2.0

# Cap on the output folded into an inline (settled) receipt. Far above the wake
# summary's budget: an inline result is a tool result like Bash's, read in the
# turn that asked for it, not a wake-turn preamble.
_INLINE_OUTPUT_MAX_CHARS = 30_000

# How long a ``serves`` process must stay up before its exit reads as "the
# service stopped" rather than "the service never came up". Under it, a
# non-zero exit is still a failure with its output attached — a port clash or
# an import error is exactly what the model needs to see.
_SERVING_STARTUP_GRACE_SECONDS = 30.0

# Bound the stopped-service output tail. The completion notification needs the service verdict, not an unbounded
# access log.
_SERVING_EXIT_SUMMARY_MAX_CHARS = 400

# How often the serving watcher asks whether its session is still alive, and
# the default idle bound (overridable via ``monitor_serving_idle_seconds``).
_SERVING_IDLE_POLL_SECONDS = 60.0

# An explicit exit-code marker a watcher prints for itself — `echo "EXIT:$?"`,
# the shape audited CI watchers use. When the command's own exit code says 0
# because the watcher swallowed the failure (`… || true`, or a self-exit after
# the tool it ran errored), this marker is the only honest outcome in the
# buffer, and the notification used to carry <status>completed</status> above a
# summary that said EXIT:1.
_EXIT_MARKER_RE = re.compile(r"\bEXIT[:=]\s*(-?\d+)")

# Registry ``reason`` stamps this module writes. They are what turns a
# deliberate stop into a CAUSE the model can read, instead of the bare
# "(no output; exit code None)" an audited session read as a crash and
# re-launched work over.
MONITOR_TIMEOUT_REASON = "monitor_timeout"
#: Stamp for a serving process stopped because its session went quiet.
SERVING_IDLE_REASON = "monitor_serving_idle"

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
        "yourself. If this session goes quiet for a long stretch the process "
        "is stopped so it cannot outlive the work, and you are told why."
    )


def clamp_wait_seconds(value: Any) -> int:
    """The requested inline wait, bounded to ``[0, MAX_WAIT_SECONDS]``.

    Anything unparseable is 0 — "no inline wait", the historical behaviour —
    rather than an error: a bad ``wait_seconds`` must never cost the spawn.
    """
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return 0
    if seconds <= 0:
        return 0
    return min(seconds, MAX_WAIT_SECONDS)


def monitor_inline_receipt_text(
    task_id: str,
    exit_code: Optional[int],
    output: str,
    *,
    failed: bool = False,
) -> str:
    """The receipt for a command that SETTLED inside its inline wait.

    No park, no wake, no fresh turn: the result is right here, so the text says
    the work is done instead of telling the model to end the turn.

    A non-zero exit says so plainly AND says it is not a tool malfunction. The
    receipt used to ride on an ``is_error`` outcome, which the code-mode runner
    turns into a raised exception: a red TDD baseline — the expected result of
    the operation the model asked for — aborted the whole cell and every call
    queued behind it (five Edits lost in one audited session, four dead cells
    in another, and a third wrapped every later Monitor in try/except).
    """
    code = "unknown" if exit_code is None else str(exit_code)
    verdict = "finished with a failure" if failed else "finished"
    note = (
        " That exit code is the command's own verdict, not a tool failure — "
        "read the output and decide."
        if failed
        else ""
    )
    return (
        f"The command {verdict} while you waited (task {task_id}, exit code "
        f"{code}). Nothing is running and nothing will wake you — this is the "
        f"whole result.{note}\n\n" + (output or "(no output)")
    )


def monitor_still_running_note(wait_seconds: int) -> str:
    """The clause appended when an inline wait expired with work still running."""
    return (
        f" It was still running after the {wait_seconds}s you waited, so it "
        "stays in the background."
    )


def last_exit_marker(text: str) -> Optional[int]:
    """The LAST explicit ``EXIT:<n>`` a command printed for itself, if any.

    Last, not any: a watcher that retries prints EXIT:1 on the attempt that
    failed and EXIT:0 on the one that worked, and the final marker is the
    outcome. ``None`` when the command printed none.
    """
    codes = _EXIT_MARKER_RE.findall(text or "")
    if not codes:
        return None
    try:
        return int(codes[-1])
    except ValueError:  # pragma: no cover — the pattern only matches digits
        return None


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


def collapse_repeated_lines(lines: List[str]) -> List[str]:
    """Collapse runs of a repeated line — or a repeated BLOCK of lines.

    A `--watch` command re-prints its status block every poll, and the buffer
    is then mostly the same three lines over and over: one audited wake summary
    was ~8KB of "Refreshing checks status every 10 seconds." with the two lines
    that carried the verdict at the very end, one truncation away from being
    lost. Block-aware because the repetition is rarely a single line — the
    period is detected (1..``_MAX_REPEAT_PERIOD``) rather than assumed.

    One copy of the block survives, followed by a line saying how many more
    there were. Order is otherwise untouched: this is a summarizer, not a sort.
    """
    out: List[str] = []
    index = 0
    total = len(lines)
    while index < total:
        best_period = 0
        best_reps = 1
        limit = min(_MAX_REPEAT_PERIOD, total - index)
        for period in range(1, limit + 1):
            block = lines[index:index + period]
            if not any(line.strip() for line in block):
                continue  # never collapse a run of blank lines into a notice
            reps = 1
            cursor = index + period
            while cursor + period <= total and lines[cursor:cursor + period] == block:
                reps += 1
                cursor += period
            if reps >= _MIN_REPEATS and reps * period > best_reps * best_period:
                best_period, best_reps = period, reps
        if best_period:
            out.extend(lines[index:index + best_period])
            noun = "line" if best_period == 1 else f"{best_period} lines"
            times = "once" if best_reps == 2 else f"{best_reps - 1} more times"
            out.append(f"... [the previous {noun} repeated {times}] ...")
            index += best_period * best_reps
        else:
            out.append(lines[index])
            index += 1
    return out


def _verdict_lines(text: str, *, budget: int) -> List[str]:
    """Outcome lines rescued from a region that is about to be elided.

    Newest first while selecting (a chained command's LAST verdict is the one
    that decided the exit code), original order when returned.
    """
    if budget <= 0:
        return []
    picked: List[str] = []
    used = 0
    for line in reversed(text.splitlines()):
        stripped = line.strip()
        if not stripped or not _VERDICT_RE.search(stripped):
            continue
        if used + len(stripped) + 1 > budget:
            break
        picked.append(stripped)
        used += len(stripped) + 1
        if len(picked) >= _SUMMARY_MAX_VERDICT_LINES:
            break
    picked.reverse()
    return picked


def _summarize(lines: list, *, budget: Optional[int] = None) -> str:
    """The buffered output, capped at the wake-summary budget, TAIL-BIASED.

    The cap is about what a WAKE TURN has to read, not about what is kept: the
    full buffer stays available through TaskOutput. Audited wake turns were
    handed 12KB stack-trace tails from servers the agent had killed itself and
    spent the entire turn reading them.

    Three round-7 corrections, all from wake summaries that hid their own
    verdict:

    * repeated lines and repeated status BLOCKS collapse first, so a watch
      command's poll noise cannot crowd out its result;
    * the head window is gone — the tail is what a command's outcome is in;
    * verdict lines (``Tests 45 passed``, ``FAILED …``, ``npm ERR!``) are
      rescued out of the elided region and kept above the truncation notice.
      A chained ``npm test && npm run check`` puts its test summary in the
      MIDDLE of the buffer, which is exactly what head+tail deleted — twice in
      one audited session, at the cost of a follow-up TaskOutput call each.
    """
    # Scrub BEFORE slicing: escape sequences are gibberish to the model (and
    # to the UI), and counting them against the budget evicts real output.
    # Ruling A13 makes the app the supervisor here, so fluid's own
    # supervisor-side strip never runs for these buffers.
    text = _strip_ansi("\n".join(lines))
    cap = _summary_budget() if budget is None else max(1, int(budget))
    if len(text) > cap:
        text = "\n".join(collapse_repeated_lines(text.split("\n")))
    if len(text) <= cap:
        return text
    tail_cap = cap - int(cap * _SUMMARY_VERDICT_FRACTION)
    kept = _verdict_lines(text[:-tail_cap], budget=cap - tail_cap)
    kept_text = "\n".join(kept)
    # Whatever the verdicts did not use goes back to the tail, which may then
    # reach back over a line the rescue already took — drop those rather than
    # print them twice.
    tail = text[-(cap - len(kept_text)):]
    kept = [line for line in kept if line not in tail]
    kept_text = "\n".join(kept)
    dropped = len(text) - len(tail) - len(kept_text)
    return (
        (kept_text + "\n" if kept_text else "")
        + f"... [{dropped} chars truncated — TaskOutput has the full buffer] ...\n"
        + tail
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
    if reason == SERVING_IDLE_REASON:
        return recorded_summary or (
            "Stopped: this serving task was left running while its session sat "
            "idle. It did not fail — start it again when you need it."
        )
    from app.core.task_registry import (
        AGENT_STOP_REASON,
        SERVER_SHUTDOWN_REASON,
        USER_STOP_REASON,
    )

    if reason == USER_STOP_REASON:
        who = "Stopped by the user."
    elif reason == AGENT_STOP_REASON:
        # The reason the reason-less branch below used to cover: the stop path
        # now stamps it explicitly so the chip can tell a deliberate stop from
        # a reap, and the wording it produced stays exactly the same.
        who = "Stopped deliberately by the app or by an agent, not by a crash."
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


def _served_before_exiting(
    exit_code: Optional[int], ran_seconds: Optional[float]
) -> bool:
    """Whether a ``serves`` process actually came up before it went away.

    A clean exit always counts. A non-zero one counts only when the process
    stayed up past the startup grace: a uvicorn that dies two seconds in
    because the port is taken never served anything, and reporting THAT as
    "the service stopped" would bury the one failure the model must see.
    Unknown runtime is treated as "never came up", i.e. the pre-existing
    failure path.
    """
    if exit_code == 0:
        return True
    if ran_seconds is None:
        return False
    return ran_seconds >= _SERVING_STARTUP_GRACE_SECONDS


def not_executable_hint(exit_code: Optional[int], summary: str) -> str:
    """The one line a "Permission denied" launch failure is missing.

    A shell script written by a worker that never ``chmod +x``'d it exits 126
    with ``/bin/sh: 1: …/launch-backend.sh: Permission denied`` and nothing
    else. One audited session lost a whole Monitor cycle plus a steer to it
    before prefixing ``sh`` by hand. Empty string when this is not that.
    """
    if exit_code != 126 or "permission denied" not in (summary or "").lower():
        return ""
    return (
        "\nThe file is not executable (exit code 126). Run it as "
        "`sh <path>` (or `bash <path>`), or chmod +x it first — nothing ran, "
        "so re-running it that way is safe."
    )


def settle_monitor_task(
    *,
    exit_code: Optional[int],
    lines: list,
    command: str,
    cwd: Optional[str],
    session_id: Optional[str],
    task_id: str,
    serves: bool,
    ran_seconds: Optional[float] = None,
) -> Tuple[str, str]:
    """``(status, summary)`` for a Monitor shell that has come to rest.

    Ordering is the whole point. The old code derived status from the exit code
    alone, so a deliberately reaped shell reached the model as
    ``error`` / "(no output; exit code None)" — read as a crash, re-launched by
    hand. The recorded status wins over the exit code, and a deliberate stop
    settles as ``killed`` (which the fluid adapter maps to ``cancelled``) with
    a cause line INSTEAD of an output tail.

    ``ran_seconds`` — how long the process was up — is what separates a SERVICE
    that stopped from one that never started; see
    :func:`_served_before_exiting`. The watcher knows it; callers that do not
    may omit it and get the pre-existing "non-zero is a failure" reading.
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
    if serves and _served_before_exiting(exit_code, ran_seconds):
        # A SERVICE that went away is a stop, not a failure — whatever its exit
        # code. The audited case settled as `failed` with an access log for a
        # summary, while its sibling (stopped on purpose) got the correct
        # cancellation wording; the two read as different kinds of event when
        # they are the same one. Its log is never the news either: what the
        # parent needs is "it is gone", so the tail is capped hard.
        tail = _summarize(lines, budget=_SERVING_EXIT_SUMMARY_MAX_CHARS)
        code = "cleanly on its own (exit code 0)" if exit_code == 0 else (
            f"on its own (exit code {exit_code})"
        )
        return "completed", (
            f"The service exited {code}. It is no longer running; restart it "
            "only if you still need it."
            + (f"\n\nLast output:\n{tail}" if tail else "")
        )
    if not summary:
        summary = f"(no output; exit code {exit_code})"
    summary += not_executable_hint(exit_code, summary)
    # The OUTCOME the summary shows beats the process's disposition. A watcher
    # ending `…; gh pr checks N || true`, or one that self-exited after the
    # command inside it errored, exits 0 while its own last word is `EXIT:1` —
    # and the notification carried <status>completed</status> above a summary
    # that said the run had failed.
    marker = last_exit_marker(summary)
    failed = exit_code != 0 or (marker is not None and marker != 0)
    return ("error" if failed else "completed"), summary


def record_settled_status(
    session_id: Optional[str],
    task_id: str,
    status: str,
    summary: str,
    *,
    serves: bool,
) -> Tuple[str, str]:
    """Write the settled outcome onto the task row and return what the row says.

    The notification and the row are two views of one event and they used to
    disagree: a TaskStop that raced a self-exit reached the model as
    ``completed`` in the notification and ``killed`` in the row, and an
    ``EXIT:1`` run reached it as ``completed`` in both. So the row is re-read
    HERE, after the settle and immediately before the wake frame is built:

    * a deliberate stop the registry recorded while the watcher was settling
      still wins — the row is right and the frame adopts it;
    * otherwise the settled status is written onto the row, so what the model
      reads in the notification is what ``/live``, ``TaskGet`` and the terminal
      chip show.
    """
    if not session_id:
        return status, summary
    try:
        from app.core.task_registry import task_registry

        record = task_registry.get_task(session_id, task_id) or {}
        recorded = str(record.get("status") or "")
        if recorded in _DELIBERATE_STATUSES and status != "killed":
            # The stop landed after the settle read the registry. The row is
            # the honest one; rebuild the summary from its attribution.
            return "killed", _cause_line(
                str(record.get("reason") or ""),
                str(record.get("summary") or ""),
                serves=serves,
            )
        if recorded != status:
            task_registry.complete_task(
                session_id, task_id, status, summary=summary
            )
        else:
            task_registry.update_task(session_id, task_id, summary=summary)
    except Exception:  # noqa: BLE001 — the registry is best-effort
        logger.debug("monitor_spawn: settled-status write failed", exc_info=True)
    return status, summary


def settle_inline_monitor_task(
    task: Any,
    *,
    command: str,
    cwd: Optional[str],
    session_id: Optional[str],
) -> Tuple[str, str]:
    """``(status, output)`` for a task that settled inside its inline wait.

    Same honest status as a background settle; the OUTPUT is the difference —
    an inline result is read in the turn that asked for it, not as a wake-turn
    preamble, so it gets the far larger inline budget.
    """
    status, output = settle_monitor_task(
        exit_code=task.exit_code,
        lines=list(task.lines),
        command=command,
        cwd=cwd,
        session_id=session_id,
        task_id=task.task_id,
        serves=False,
    )
    if task.lines:
        output = _summarize(list(task.lines), budget=_INLINE_OUTPUT_MAX_CHARS)
        output += not_executable_hint(task.exit_code, output)
    return record_settled_status(session_id, task.task_id, status, output, serves=False)


async def wait_for_inline_settle(task: Any, wait_seconds: int) -> bool:
    """Block up to ``wait_seconds`` for ``task`` to exit. True if it did."""
    import asyncio

    deadline = time.monotonic() + wait_seconds
    while task.is_running():
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_INLINE_POLL_SECONDS)
    # The process is gone, but the reader drains stdout on its own task and
    # only then records the exit code. Give it a bounded moment so the receipt
    # quotes the whole buffer instead of a prefix.
    drain_deadline = time.monotonic() + _DRAIN_GRACE_SECONDS
    while task.exit_code is None and time.monotonic() < drain_deadline:
        await asyncio.sleep(_INLINE_POLL_SECONDS)
    return True


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

        # ``suppress_wake=False``: a task killed by its own time budget is the
        # one deliberate stop the parent did NOT ask for, so its terminal frame
        # must still reach the model. Every other caller of that function is a
        # stop somebody chose and defaults to suppressing the notification.
        await stop_background_shell_task(
            task.task_id, session_id, suppress_wake=False,
        )
    except Exception:  # noqa: BLE001 — a stop failure must not strand the wake
        logger.warning(
            "monitor_spawn: stopping timed-out task %s failed",
            task.task_id, exc_info=True,
        )
    logger.warning(
        "monitor_spawn: task %s hit its %ss time budget and was stopped",
        task.task_id, budget_seconds,
    )


def serving_idle_budget_seconds() -> int:
    """The idle bound for a ``serves`` process — 0 for "no bound".

    A serving task is deliberately exempt from the command time budget: it is
    MEANT to stay up. Nothing else bounded it either, so when the session that
    started it died, the process outlived it — an audited runner carried a
    uvicorn fixture and a Vite dev server for 8h15m after their session's wake
    failed, next to a resource-monitor warning about 17 orphan children and
    RSS growing 745 MB/hour. The bound is on the SESSION's idleness, not on the
    process's age: a server tended by a live session is never touched.
    """
    try:
        from app.core.config import settings

        return max(0, int(getattr(settings, "monitor_serving_idle_seconds", 0) or 0))
    except Exception:  # noqa: BLE001 — config must never break a watcher
        return 0


def session_idle_seconds(session_id: Optional[str]) -> Optional[float]:
    """Seconds since this session was last touched, or None if unknowable.

    ``sessions.updated_at`` is the cheapest honest "is anyone home": every turn
    boundary and stats write moves it, and a session whose wake died stops
    moving it entirely. None (unknown) always means "leave the task alone".
    """
    if not session_id:
        return None
    try:
        from datetime import datetime, timezone

        from app.db import database

        stamp = (database.get_session(session_id) or {}).get("updated_at")
        if stamp is None:
            return None
        parsed = (
            datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if isinstance(stamp, str)
            else stamp
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())
    except Exception:  # noqa: BLE001 — an unreadable row is simply unknown
        logger.debug("monitor_spawn: session idle read failed", exc_info=True)
        return None


async def _enforce_serving_idle_bound(
    task: Any, session_id: Optional[str], idle_seconds: float, budget_seconds: int
) -> None:
    """Stop an unattended serving process, with attribution the model can read."""
    minutes = int(idle_seconds // 60)
    summary = (
        f"Stopped: this serving task was left running while its session sat "
        f"idle for {minutes} minutes (bound: {budget_seconds}s). It did not "
        "fail and it did not crash — nothing was attending it. Start it again "
        "when you need it."
    )
    if session_id:
        try:
            from app.core.task_registry import task_registry

            task_registry.complete_task(
                session_id, task.task_id, "killed",
                summary=summary, reason=SERVING_IDLE_REASON,
            )
        except Exception:  # noqa: BLE001 — attribution is best-effort
            logger.debug("monitor_spawn: idle attribution failed", exc_info=True)
    try:
        from app.mcp.builtin_tools import stop_background_shell_task

        await stop_background_shell_task(task.task_id, session_id)
    except Exception:  # noqa: BLE001 — a stop failure must not strand the wake
        logger.warning(
            "monitor_spawn: stopping idle serving task %s failed",
            task.task_id, exc_info=True,
        )
    logger.warning(
        "monitor_spawn: serving task %s stopped — session %s idle %.0fs "
        "(bound %ss)",
        task.task_id, session_id, idle_seconds, budget_seconds,
    )


WAIT_SECONDS_PARAM = (
    f"Seconds to wait here for the command to finish (0-{MAX_WAIT_SECONDS}, "
    "default 0 = don't wait). If it finishes inside the wait you get its exit "
    "code and output in this result and nothing is left running — no "
    "notification, no extra turn. If it is still going when the wait expires "
    "you get the usual background receipt and are woken as normal. Set it to "
    "a few seconds for work that will probably be quick (a focused test file, "
    "a lint run, a git or gh command); leave it 0 for a build, a full suite or "
    "a CI wait. Ignored when `serves` is set."
)

WAIT_SECONDS_DESCRIPTION_CLAUSE = (
    "\n\n**`wait_seconds` for work that is probably quick.** Waiting a few "
    "seconds here is cheaper than a completion: if the command finishes inside "
    "the wait, its exit code and output come back in this result and no "
    "notification, wake or extra turn happens at all. If it does not, you get "
    "the normal background receipt and end the turn exactly as you would have. "
    "It is never a reason to wait in Bash instead. A non-zero exit comes back "
    "as an ordinary result carrying that exit code — a failing test run is an "
    "answer, not a tool error, and nothing is raised."
)


def amend_native_monitor_spec(registry: Any) -> None:
    """Teach fluid's registered Monitor spec about ``wait_seconds``.

    Ruling A13 splits the tool: fluid owns Monitor's shape, the app owns its
    supervision — and the inline wait is supervision. The parameter is
    therefore added to the vendored spec here rather than in fluid, the
    same way :func:`_amend_native_task_spec` installs the app's Task contract.
    Fully defensive: a failure leaves the vendor spec as-is and the spawner
    simply never sees a ``wait_seconds``.
    """
    spec = registry.get("Monitor")
    if spec is None:
        # Monitor is not on this session's surface. Amending would ADD it.
        return
    schema = dict(spec.input_schema or {})
    properties = dict(schema.get("properties") or {})
    if "wait_seconds" in properties:
        return
    properties["wait_seconds"] = {
        "type": "integer",
        "minimum": 0,
        "maximum": MAX_WAIT_SECONDS,
        "description": WAIT_SECONDS_PARAM,
    }
    schema["properties"] = properties
    registry.amend_spec(
        "Monitor",
        description=(spec.description or "") + WAIT_SECONDS_DESCRIPTION_CLAUSE,
        input_schema=schema,
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

        # The session's own working directory when the model named none —
        # exactly what fluid's Bash does (`input.get("cwd") or ctx.cwd`), which
        # is what the shared cwd parameter text promises ("default: the session
        # cwd"). Without the fallback the shell inherited the SERVER's cwd:
        # four audited sessions launched `gh pr checks … --watch` from a
        # worktree and every one died with "fatal: not a git repository",
        # costing a park, a wake and a turn each.
        cwd = input.get("cwd") or getattr(ctx, "cwd", None) or None
        wait_seconds = clamp_wait_seconds((input or {}).get("wait_seconds"))
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
            task_registry.mark_monitor_shell_task(caller_session_id, task.task_id, serves=serves)

        # INLINE WAIT: block here for the window the model asked for. A command
        # that settles inside it costs no park, no wake, no fresh turn and no
        # prompt re-send — its output comes back in this very receipt. Only
        # when the window expires does the background machinery below start.
        # A `serves` process is exempt: it is meant to stay up, so there is
        # nothing to wait for.
        if wait_seconds and not serves:
            from fluid.tools.registry import ToolOutcome

            if await wait_for_inline_settle(task, wait_seconds):
                exit_code = task.exit_code
                status, output = settle_inline_monitor_task(
                    task,
                    command=command,
                    cwd=cwd,
                    session_id=caller_session_id,
                )
                failed = status != "completed"
                # NOT ``is_error``. The command ran and produced a result; a
                # non-zero exit is that result. ``is_error`` is the code-mode
                # runner's raise signal, and raising on a red test suite tore
                # down the whole cell — with every call queued behind it — in
                # three audited sessions. Spawn/infra failures still raise.
                return ToolOutcome(
                    text=monitor_inline_receipt_text(
                        task.task_id, exit_code, output, failed=failed
                    ),
                    is_error=False,
                )

        # Completion watcher: fold the output tail into the registry record and
        # fire the scheduler's terminal seam so a parked parent wakes with the
        # result. Detached — it must outlive this tool call by construction.
        if caller_session_id:
            import asyncio

            async def _on_exit() -> None:
                started = time.monotonic()
                deadline = (started + budget) if budget else None
                # A serving process is exempt from the command budget but not
                # from its session: past this it is an orphan, not a service.
                idle_budget = serving_idle_budget_seconds() if serves else 0
                next_idle_check = started + (idle_budget or 0)
                while task.is_running():
                    now = time.monotonic()
                    if deadline is not None and now >= deadline:
                        # "Parked forever" becomes "woken with a timeout": stop
                        # the process, then fall through to the normal settle,
                        # which reads the attribution back off the registry.
                        await _enforce_deadline(task, caller_session_id, budget)
                        break
                    if idle_budget and now >= next_idle_check:
                        next_idle_check = now + _SERVING_IDLE_POLL_SECONDS
                        idle = await asyncio.to_thread(
                            session_idle_seconds, caller_session_id
                        )
                        if not task.is_running():
                            # The process went away while the idle read
                            # was off-thread: settle it now, not after the
                            # next poll.
                            break
                        if idle is not None and idle >= idle_budget:
                            await _enforce_serving_idle_bound(
                                task, caller_session_id, idle, idle_budget
                            )
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
                    ran_seconds=time.monotonic() - started,
                )
                # One event, one status: the row is written from the settle and
                # re-read, so the notification below cannot say "completed"
                # over a row that says "killed" (a TaskStop racing a self-exit)
                # or over a summary that says the run failed.
                status, summary = record_settled_status(
                    caller_session_id, task.task_id, status, summary,
                    serves=serves,
                )
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
        if wait_seconds and not serves:
            # The wait expired with work still running: say so, then the
            # unchanged park instruction above stands.
            text += monitor_still_running_note(wait_seconds)
        if budget:
            text += (
                f" It carries a {budget}s time budget: if it is still running "
                "then, it is stopped and you are woken with a timeout."
            )
        return ToolOutcome(text=text, background_task=receipt)

    return _spawn
