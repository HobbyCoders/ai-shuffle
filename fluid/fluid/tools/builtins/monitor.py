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

Returning the receipt is the atomic detach boundary: the monitored process must
outlive the originating HTTP request, WebSocket turn, and this tool coroutine.
When the command reaches a terminal state the host submits
``BackgroundTaskFinished(task_id, status, summary=<output tail>)`` to the parent
engine; Fluid persists the summary and wakes the parked session — the model sees
build logs or CI results it never had to poll for.
"""

from __future__ import annotations

import asyncio
import inspect
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


def describe(tools: frozenset[str] | None = None) -> str:
    """Monitor's description, specialized to the session's tool surface."""
    has = (lambda name: tools is None or name in tools)
    shell_note = (
        " The command runs in the same shell environment as Bash."
        if has("Bash")
        else ""
    )
    pipe_note = (
        " Never pipe a build or test command through `tail`/`head` to shorten "
        "it: the cap already handles size, and the pipe replaces the real exit "
        "code with the pipe's."
        if has("Bash")
        else ""
    )
    wait_here = (
        "**Waiting happens here, never in Bash.** Whatever sleeping, retrying, or "
        "watching the job needs belongs inside this one Monitor command — never "
        "in a foreground Bash call."
        if has("Bash")
        else "**Waiting happens here.** Whatever sleeping, retrying, or "
        "watching the job needs belongs inside this one Monitor command."
    ) + (
        " Do not start a Monitor and then wait for it yourself: start it and "
        "end the turn."
    )
    closing = (
        "Prefer Monitor over a blocking Bash call whenever the wait is longer "
        "than a few seconds: start it, keep doing useful work, and incorporate "
        "the result when the completion arrives."
        if has("Bash")
        else "Start it, keep doing useful work, and incorporate the result "
        "when the completion arrives."
    ) + f" {END_TURN_ON_BACKGROUND}"
    return (
        "Run a long-running shell command in the background and be woken when "
        "it finishes. Returns immediately with a task_id; when the command "
        "reaches a terminal state you receive its exit code and an output "
        f"summary as a notification in a later turn — no polling. "
        f"{DONT_PREDICT_PENDING}\n"
        "\n"
        "Use it for anything slow enough to be worth overlapping with other "
        "work: CI runs, release and frontend builds, long test suites, "
        "training jobs, waiting on a dev server to come up.\n"
        "\n"
        f"{wait_here}\n"
        "\n"
        "**The command must exit.** The completion fires on exit and only on "
        "exit, so an unbounded command (`tail -f`, `inotifywait -m`, a bare "
        "`while true`) never wakes you — it just holds a slot until the "
        "session ends. For \"tell me when X is ready\", give Monitor something "
        "that ends on its own: the tool's own blocking wait subcommand where "
        "one exists, otherwise a watch whose condition and overall time budget "
        "both end it.\n"
        "\n"
        "**Silence is not success.** The exit condition must cover every "
        "terminal state, not just the happy path. A watch that ends only on "
        "the success marker runs forever through a crash, a hung job, or an "
        "unexpected exit — and \"still running\" looks exactly like \"never "
        "finished\". Before starting one, ask: if this job died right now, "
        "would the command still end? If not, widen it to every terminal "
        "outcome — failed, cancelled and timed out, not only succeeded — and "
        "cap the total wait. A watch on a remote API should survive one failed "
        "request rather than die on it, and should check at a human interval "
        "(tens of seconds), not in a tight spin.\n"
        "\n"
        "**Output.** stdout and stderr are merged into one buffer, and the "
        "completion summary keeps head and tail while dropping the middle — so "
        "filter a noisy command down to the lines you would act on, which "
        "means the failure signatures too, not only good news."
        + pipe_note + "\n"
        "\n"
        f"{FRESH_PROCESS}{shell_note}\n"
        "\n"
        + closing
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
    the spawn: sessions that read a neutral acknowledgement have gone on to
    sleep-poll the very task they just handed off."""
    return (
        f"Monitoring started (task {task_id}). End the turn now — the "
        "completion wakes the session when the command exits. Do not sleep, "
        "poll, or check on it yourself."
    )


def _receipt_from(result: Any, input: dict[str, Any]) -> ToolOutcome:
    if isinstance(result, ToolOutcome):
        return result
    if isinstance(result, BackgroundTaskReceipt):
        return ToolOutcome(
            text=started_text(result.task_id),
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
        )
        return ToolOutcome(
            text=str(
                result.get("text")
                or result.get("message")
                or started_text(receipt.task_id)
            ),
            ui=result.get("ui", result),
            is_error=bool(result.get("is_error", False)),
            background_task=receipt,
        )
    return ToolOutcome(text=str(result))


async def _spawn_detached(spawn: Any, input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    outcome = _receipt_from(await spawn(input, ctx), input)
    receipt = outcome.background_task
    if receipt is None or outcome.is_error:
        return outcome
    started = (ctx.extra or {}).get("background_task_started")
    if started is not None:
        notified = started(receipt)
        if inspect.isawaitable(notified):
            await notified
    return outcome


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
