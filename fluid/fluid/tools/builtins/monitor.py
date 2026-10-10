"""Monitor — spawn a long-running shell command and be woken when it finishes.

Like Task, Fluid owns the tool's SHAPE (bare name, schema, description travel
with the harness, Claude Code-parity); the actual supervision — detached process
management, output buffering, lifetime beyond the turn — is host-app territory.
The embedding application injects an async spawner as
``ctx.extra["monitor_spawn"]``; without one the tool reports itself unavailable
instead of pretending to work. Fluid's own CLI is a host too:
:class:`fluid.cli.host.CliHost` injects a spawner that runs the command as a
detached asyncio subprocess, so ``fluid chat`` / ``fluid run`` get working
Monitor out of the box.

The spawner receives ``(input, ctx)`` and must return a ``BackgroundTaskReceipt``
(or a ``ToolOutcome`` carrying one, or a mapping with ``task_id``). Contract for
``input``:
  * command — the shell command to run (required)
  * cwd     — working directory (default: the session cwd)
  * serves  — the process is MEANT to stay up (a dev server, a tunnel). The
              task is still registered, surfaced and stoppable, but it is kept
              out of park accounting: see ``BackgroundTaskReceipt.serves``.

A host that enforces a wall-clock budget on monitored commands declares it as
``ctx.extra["monitor_budget_seconds"]`` (see :data:`BUDGET_EXTRA_KEY`) and
passes the same number to :func:`describe` when it refines the builtin
descriptions. Fluid then names that number instead of guessing: the description
says what to bound the command below, and the spawn warning says the command
will be stopped at the budget and woken with a timeout rather than claiming no
wake is coming — which is what the host's own receipt says too.

Returning the receipt is the atomic detach boundary: the monitored process must
outlive the originating HTTP request, WebSocket turn, and this tool coroutine.
When the command reaches a terminal state the host submits
``BackgroundTaskFinished(task_id, status, summary=<output tail>)`` to the parent
engine; Fluid persists the summary and wakes the parked session — the model sees
build logs or CI results it never had to poll for.
"""

from __future__ import annotations

import asyncio
import dataclasses
import inspect
import re
from typing import Any

from fluid.tools.builtins.bash_risk import classify as classify_bash_risk
from fluid.tools.builtins.bash_risk import session_scope as bash_session_scope
from fluid.tools.fragments import (
    CWD_PARAM,
    DONT_PREDICT_PENDING,
    END_TURN_ON_BACKGROUND,
    FRESH_PROCESS,
)
from fluid.tools.registry import (
    BackgroundTaskReceipt,
    ToolCtx,
    ToolOutcome,
    ToolSpec,
)


#: The ctx key a host uses to declare the wall-clock budget it enforces on a
#: monitored command (seconds). The host that stops the process at N seconds is
#: the only one that knows N, and Monitor prose that talks about bounds (the
#: description, the spawn warning) must name it or contradict the host's own
#: receipt. Absent or non-positive, the prose falls back to the budget-less
#: wording.
BUDGET_EXTRA_KEY = "monitor_budget_seconds"


def budget_seconds_of(value: Any) -> int | None:
    """Coerce a host-declared budget to a positive whole number of seconds."""
    try:
        seconds = int(float(value))
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def describe(
    tools: frozenset[str] | None = None,
    *,
    budget_seconds: int | None = None,
) -> str:
    """Monitor's description, specialized to the session's tool surface.

    ``budget_seconds`` is the host's enforced wall-clock budget (see
    :data:`BUDGET_EXTRA_KEY`). Given it, the description names the number, so
    a watcher is not written to outlive the budget the host kills it at.
    """
    has = lambda name: tools is None or name in tools
    budget = budget_seconds_of(budget_seconds)
    wait_here = (
        "Waiting happens here, never in Bash. Keep sleeping, retrying and watching "
        "inside one Monitor command, not a foreground Bash call. "
        if has("Bash") else
        "Keep sleeping, retrying and watching inside one Monitor command. "
    )
    budget_note = (
        f" The host stops a Monitor command after {budget} seconds and wakes you with "
        "a timeout; bound the command below that budget."
        if budget else ""
    )
    pipe_note = (
        " Never pipe builds/tests through tail/head: the output cap already handles size "
        "and the pipe masks the real exit code. A trailing || true or ; next-command "
        "does too. Keep the command whose result matters last and unmasked."
        if has("Bash") else ""
    )
    return (
        "Run a long-running shell command in the background; return task_id immediately. "
        "On termination a later turn receives its exit code and bounded head/tail summary "
        "of merged stdout/stderr (a few thousand characters), not the full buffer. "
        "TaskOutput has the full buffer: retrieve it only if failure details were cut; "
        "exit 0 needs no pull. No polling. " + DONT_PREDICT_PENDING + "\n\n"
        "Use for work worth overlapping: CI, release/frontend builds, long test suites, "
        "training jobs. Prefer it for waits over a few seconds: project type-checks, "
        "webpack/vite/cargo builds, full suites and installs. Continue useful disjoint "
        "work and inspect completion before reporting. " + wait_here +
        "Start every known independent job before ending the turn (one park covers several); "
        "do not start a Monitor then wait yourself: end the turn. " + END_TURN_ON_BACKGROUND + "\n\n"
        "The command must exit unless serves=true. Completion fires only on exit: "
        "unbounded tail -f, inotifywait -m or bare while true holds the session parked "
        "until it ends. For 'tell me when X is ready', use a self-terminating job wait "
        "subcommand when available: gh pr checks <PR> --watch --fail-fast settles checks "
        "and exits non-zero on failure, without a custom loop. Otherwise bound both the "
        "watch condition and total time." + budget_note + "\n\n"
        "Give CI time to register after a push: sleep 20 && gh pr checks <PR> --watch "
        "--fail-fast, all inside Monitor. An immediate 'no checks reported' means not "
        "started yet, never as success. Silence is not success: cover EVERY terminal state "
        "(succeeded, failed, cancelled, timed out), not just a success marker. Ask whether "
        "the command would end if the job died now; handle crashes, hangs and unexpected "
        "exits and cap total wait. Remote API watches should survive one failed request "
        "and check at human intervals (tens of seconds), not spin.\n\n"
        "serves: true is only for a process MEANT to stay up: dev server, tunnel or queue "
        "worker needed during other work, never a job whose result you await. It remains "
        "registered/visible, notifies you if it dies, but does not park the session or "
        "appear as pending awaited work; the starting turn ends normally. Use this "
        "instead of making a server exit.\n\n"
        "Output merges stdout/stderr; summaries keep head/tail and drop the middle. "
        "Filter noisy commands to actionable lines INCLUDING failure signatures, not "
        "only good news." + pipe_note + "\n\n" + FRESH_PROCESS
        + (" The command runs in the same shell environment as Bash." if has("Bash") else "")
    )


SPEC = ToolSpec(
    name="Monitor",
    description=describe(),
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command to run in the background.",
            },
            "cwd": {
                "type": "string",
                "description": CWD_PARAM,
            },
            "serves": {
                "type": "boolean",
                "description": (
                    "Set true for a process that is MEANT to stay up — a dev "
                    "server, a tunnel, a queue worker. The task is registered "
                    "and you are notified if it dies, but the session never "
                    "parks on it, so the turn that starts it can end "
                    "normally. Never set it for a job whose result you want."
                ),
            },
        },
        "required": ["command"],
    },
    risk="write",
    # Monitored commands are shell commands: the same read-only proof that
    # lets Bash skip a prompt applies (a background `ls` is as safe as a
    # foreground one), and an always-allow scopes to the programs run.
    classify_risk=classify_bash_risk,
    session_scope=bash_session_scope,
    # Host-injected spawner (see module docstring). Without it the engine
    # drops Monitor from the request schemas rather than advertising a tool
    # that can only error.
    requires_extra=("monitor_spawn",),
)


def started_text(task_id: str) -> str:
    """The success receipt. It commands the park rather than merely reporting
    the spawn: after a neutral acknowledgement a model may sleep-poll the very
    task it just handed off.

    It agrees with Task's receipt ("unless you have genuinely disjoint work"),
    asks for the OTHER independent jobs to start first so one park covers
    them, and allows reading the full output once the completion arrives,
    which is not polling and is often the only way to see the failure.
    """
    return (
        f"Monitoring started (task {task_id}). Start every other independent "
        "job you already know you need before you end the turn — one park can "
        "cover several. Then end the turn, unless you have genuinely disjoint "
        "work to do first: the completion wakes the session when the command "
        "exits. Do not sleep or poll waiting for it — but once its completion "
        "notification arrives, reading the full output is expected, not "
        "polling. Inspect failed or truncated output and finish outstanding "
        "requested work before the final report."
    )


def serving_text(task_id: str) -> str:
    """The receipt for a ``serves`` spawn. A serving task must NOT be read as
    pending work: the session is not parked on it and no completion is coming
    while it is healthy, so the receipt says so instead of leaving the model to
    infer a wake that will never arrive."""
    return (
        f"Serving task {task_id} started and is expected to stay up. The "
        "session is NOT parked on it — end the turn whenever this step is "
        "done; no completion is coming while it is healthy, and you are "
        "notified if it stops or dies. Do not sleep or poll waiting on it; "
        "reading its output when you actually need it is fine."
    )


# ─── Spawn-time lint ─────────────────────────────────────────────────────────
#
# The description already forbids an unbounded watch and demands a time budget,
# and models still write them (for example a CI watcher looping on
# ``case $s in *pending*|*""*)``, where quote removal turns ``*""*`` into the
# glob ``**``, matching every string). Prose in a tool description is read
# once; a warning stapled to the receipt is read at the moment the
# mistake was made. Warn, never refuse: the model may know something the
# patterns do not.

# Shapes with no natural end. Matching one is not proof of a bug — a bound may
# be supplied elsewhere in the command, which _TIME_BOUND_PATTERNS covers.
#
# The third element says whether the shape is a LOOP: a loop can also be bounded
# from inside its body (a counter that breaks, a bail-out `exit`), which a
# `tail -f` cannot.
_UNBOUNDED_PATTERNS: tuple[tuple[re.Pattern[str], str, bool], ...] = (
    (re.compile(r"\bwhile\s+(?:true|:)(?:\s|;|$)"), "a `while true` loop", True),
    (re.compile(r"\bwhile\s+\[\s*1\s*\]"), "a `while [ 1 ]` loop", True),
    (re.compile(r"\buntil\s"), "an `until` loop", True),
    (re.compile(r"\bfor\s*\(\(\s*;\s*;\s*\)\)"), "a `for((;;))` loop", True),
    (re.compile(r"\btail\s+(?:-[A-Za-z]*f\b|--follow)"), "`tail -f`", False),
    (re.compile(r"\binotifywait\b[^;|]*\s(?:-[A-Za-z]*m\b|--monitor)"),
     "`inotifywait -m`", False),
    (re.compile(r"(?:^|[|;&]\s*)watch\s"), "the `watch` utility", False),
    (re.compile(
        r"\b(?:journalctl|docker\s+logs|docker\s+compose\s+logs|kubectl\s+logs)"
        r"\b[^;|]*(?:\s-[A-Za-z]*f\b|--follow)"
    ), "a log follow", False),
)

# Evidence that the command DOES carry a time budget or an iteration cap, so a
# loop above will end on its own.
_TIME_BOUND_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bg?timeout\s+(?:-\S+\s+)*\d"),      # timeout 900 …
    re.compile(r"--(?:timeout|max-time|deadline|wait-timeout|time-limit)\b"),
    re.compile(r"\btimeout=\d"),
    re.compile(r"\bdate\s+\+%s"),                      # deadline arithmetic
    re.compile(r"\$SECONDS\b|\bSECONDS="),
    re.compile(r"\bin\s+\$\(\s*seq\b"),                # for i in $(seq 1 60)
    re.compile(r"\bfor\s*\(\([^;]*;[^;]*[<>][^;]*;"),  # for ((i=0; i<60; i++))
    # A count-and-continue test (`while [ $i -lt 60 ]`) and its mirror image,
    # the count-and-BREAK test (`[ $i -ge 60 ] && break`), the more common
    # shape inside a `while true`.
    re.compile(r"-(?:lt|le|ge|gt)\s"),
    # The arithmetic spellings of the same cap: ((i>=60)), (( n < 60 )),
    # ((i++ >= 60)).
    re.compile(r"\(\([^)]*[<>]=?[^)]*\)\)"),
)

# A loop that ends from inside its own body. Consulted only for loop-shaped
# reasons, and only when the body can be trusted — a match-all glob makes the
# arm holding the `break` dead code.
_LOOP_EXIT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:^|[\s;&|(])break(?:\s|;|$|\))"),
    re.compile(r"(?:^|[\s;&|(])exit\b"),
    re.compile(r"(?:^|[\s;&|(])return\b"),
)

# ``*""*`` / ``*''*`` in a glob: quote removal collapses it to ``**``, which
# matches every string, so the arm that was meant to catch one state catches
# all of them and the loop's exit arm becomes dead code.
_MATCH_ALL_GLOB = re.compile(r"\*(?:\"\"|'')\*")

# Command shapes that are servers, not jobs — the case `serves` exists for.
_SERVICE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+(?:run\s+)?(?:dev|start|serve|watch)\b"),
    re.compile(r"\b(?:nodemon|http-server|live-server|serve)\b"),
    re.compile(r"\bvite\b(?![^;|&\n]*(?:\b(?:build|optimize)\b|--(?:help|version)\b))"),
    re.compile(r"\bnext\s+(?:dev|start)\b"),
    re.compile(r"\b(?:uvicorn|gunicorn|hypercorn|daphne|waitress-serve)\b"),
    re.compile(r"\bflask\s+run\b|\bpython\s+-m\s+http\.server\b|\bmanage\.py\s+runserver\b"),
    re.compile(r"\brails\s+s(?:erver)?\b|\bphp\s+-S\b|\bgo\s+run\s+\S*\bserve"),
    re.compile(r"\bngrok\b|\bcloudflared\b|\bssh\s+-[A-Za-z]*[LRD]\b"),
    re.compile(r"\bdocker(?:\s+compose)?\s+up\b(?![^;|]*\s-d\b)"),
)


def _matches_any(command: str, patterns: Any) -> bool:
    return any(pattern.search(command) for pattern in patterns)


def unbounded_reason(command: str) -> str | None:
    """The shape that makes ``command`` unbounded, or None if it will end.

    A command carrying its own bound (``timeout``, deadline arithmetic, an
    iteration cap, or a loop body that breaks out) is never reported, however
    loop-shaped it looks.
    """
    if _matches_any(command, _TIME_BOUND_PATTERNS):
        return None
    breaks_out = not _MATCH_ALL_GLOB.search(command) and _matches_any(
        command, _LOOP_EXIT_PATTERNS
    )
    for pattern, reason, is_loop in _UNBOUNDED_PATTERNS:
        if not pattern.search(command):
            continue
        if is_loop and breaks_out:
            return None
        return reason
    return None


def spawn_warning(
    command: str,
    *,
    serves: bool = False,
    budget_seconds: int | None = None,
) -> str | None:
    """A pointed warning to staple onto the receipt, or None when clean.

    ``budget_seconds`` is the host's enforced wall-clock budget (see
    :data:`BUDGET_EXTRA_KEY`). Hosts that enforce one append their own "you
    will be woken with a timeout" line to the receipt; given the budget, the
    warning says what actually happens instead of contradicting that line.
    """
    budget = budget_seconds_of(budget_seconds)
    parts: list[str] = []
    if _MATCH_ALL_GLOB.search(command):
        parts.append(
            "Warning — the glob `*\"\"*` (or `*''*`) in this command collapses "
            "to `**` after quote removal and matches EVERY string, so the "
            "branch it guards always wins and the arm that was meant to end "
            "the loop is dead code. Match the literal state names instead."
        )
    if not serves:
        if _matches_any(command, _SERVICE_PATTERNS):
            parts.append(
                "Warning — this looks like a server or tunnel, i.e. a process "
                "meant to stay up. It has no completion to wake you with, so "
                "as a plain Monitor job it holds the session parked until the "
                "session ends. If it is meant to stay up, re-run it with "
                "`serves: true`; if you want a result, run the thing that "
                "produces one."
            )
        else:
            reason = unbounded_reason(command)
            if reason is not None and budget:
                parts.append(
                    f"Warning — this command has no time budget of its own: "
                    f"{reason} with no `timeout`, deadline, or iteration cap. "
                    f"It is stopped at the host's {budget}s budget and you are "
                    "woken with a timeout then — so the task does finish, but "
                    f"it costs the full {budget}s and tells you nothing about "
                    "the job. Do not stop and relaunch it over this warning. "
                    "For a real signal next time, give the command its own "
                    f"bound well under {budget}s (`timeout ...`, or a deadline "
                    "you break on) or use the job's own blocking wait (`gh pr "
                    "checks <PR> --watch --fail-fast` for GitHub CI). If the "
                    "process is MEANT to stay up, re-run with `serves: true`."
                )
            elif reason is not None:
                parts.append(
                    f"Warning — this command has no time budget: {reason} with "
                    "no `timeout`, deadline, or iteration cap. Unless the host "
                    "enforces a budget of its own, it holds the park until the "
                    "session ends. Add a bound (wrap it in `timeout 900 ...`, "
                    "or compute a deadline and break on it), use the job's own "
                    "blocking wait instead (`gh pr checks <PR> --watch "
                    "--fail-fast` for GitHub CI), or — if the process is MEANT "
                    "to stay up — re-run with `serves: true`."
                )
    if not parts:
        return None
    return "\n\n".join(parts)


def _receipt_text(task_id: str, *, serves: bool) -> str:
    return serving_text(task_id) if serves else started_text(task_id)


def _receipt_from(result: Any, input: dict[str, Any]) -> ToolOutcome:
    serves = bool(input.get("serves"))
    if isinstance(result, ToolOutcome):
        return result
    if isinstance(result, BackgroundTaskReceipt):
        return ToolOutcome(
            text=_receipt_text(result.task_id, serves=serves or result.serves),
            background_task=result,
        )
    if isinstance(result, dict) and result.get("task_id"):
        receipt = BackgroundTaskReceipt(
            task_id=str(result["task_id"]),
            worker_session_id=(
                str(result["worker_session_id"])
                if result.get("worker_session_id") is not None
                else None
            ),
            description=(
                result.get("description")
                or input.get("label")
                or input.get("command")
            ),
            serves=bool(result.get("serves", serves)),
        )
        return ToolOutcome(
            text=str(
                result.get("text")
                or result.get("message")
                or _receipt_text(receipt.task_id, serves=receipt.serves)
            ),
            ui=result.get("ui", result),
            is_error=bool(result.get("is_error", False)),
            background_task=receipt,
        )
    return ToolOutcome(text=str(result))


def _with_serves(outcome: ToolOutcome, serves: bool) -> ToolOutcome:
    """Stamp ``serves`` onto the receipt when the CALLER asked for it.

    The host spawner may not know about the flag (it predates this, or it
    simply passes the input through), but park accounting reads it off the
    receipt — so fluid, which owns the tool's shape, is what guarantees it is
    there. A host that DID set it wins; nothing here ever clears it.
    """
    receipt = outcome.background_task
    if receipt is None or not serves or receipt.serves:
        return outcome
    return dataclasses.replace(
        outcome, background_task=dataclasses.replace(receipt, serves=True)
    )


def _with_warning(outcome: ToolOutcome, warning: str | None) -> ToolOutcome:
    if not warning or outcome.is_error:
        return outcome
    return dataclasses.replace(outcome, text=f"{outcome.text}\n\n{warning}")


async def _spawn_detached(spawn: Any, input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    serves = bool(input.get("serves"))
    outcome = _with_serves(_receipt_from(await spawn(input, ctx), input), serves)
    receipt = outcome.background_task
    if receipt is None or outcome.is_error:
        return outcome
    if outcome.result_fields is None:
        # Code mode reads the id off the result (``r.task_id``) instead of
        # parsing the receipt text; output and exit code are not known yet.
        outcome = dataclasses.replace(
            outcome, result_fields={"task_id": receipt.task_id}
        )
    started = (ctx.extra or {}).get("background_task_started")
    if started is not None:
        notified = started(receipt)
        if inspect.isawaitable(notified):
            await notified
    return _with_warning(
        outcome,
        spawn_warning(
            str(input.get("command") or ""),
            serves=receipt.serves,
            budget_seconds=budget_seconds_of((ctx.extra or {}).get(BUDGET_EXTRA_KEY)),
        ),
    )


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    spawn = (ctx.extra or {}).get("monitor_spawn")
    if spawn is None:
        return ToolOutcome(
            text=(
                "Monitor is not available in this environment — no monitor "
                "spawner was injected by the host application."
            ),
            is_error=True,
        )
    # Same atomic detach boundary as Task(mode="background"): once the host
    # accepts the spawn, interrupting the parent turn must not cancel the
    # monitored process or lose its durable task id.
    spawn_task = asyncio.create_task(_spawn_detached(spawn, input, ctx))
    tracked = (ctx.extra or {}).get("background_spawn_tasks")
    if isinstance(tracked, set):
        tracked.add(spawn_task)
        spawn_task.add_done_callback(tracked.discard)
    return await asyncio.shield(spawn_task)
