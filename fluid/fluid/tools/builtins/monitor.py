"""Monitor — spawn a long-running shell command and be woken when it finishes.

Like Task, Fluid owns the tool's SHAPE (bare name, schema, description travel
with the harness, Claude Code-parity); the actual supervision — detached process
management, output buffering, lifetime beyond the turn — is host-app territory.
The embedding application injects an async spawner as
``ctx.extra["monitor_spawn"]``; without one the tool reports itself unavailable
instead of pretending to work (fluid's own CLI runs without it until it grows a
spawner of its own).

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

from fluid.tools.registry import (
    BackgroundTaskReceipt,
    ToolCtx,
    ToolOutcome,
    ToolSpec,
)

SPEC = ToolSpec(
    name="Monitor",
    description=(
        "Spawn a long-running shell command in the background and stream its "
        "stdout/stderr into a buffer. Returns immediately with a task_id; when "
        "the command finishes you are woken with its output summary — no "
        "polling. Use for anything slow you want to overlap with other work: "
        "CI runs, frontend or release builds, long test suites, dev servers, "
        "log watchers.\n"
        "\n"
        "Each call is a fresh process: environment variables and `cd` do NOT "
        "persist between calls. Pass `cwd` instead of prefixing the command "
        "with cd. Output is capped — the completion summary keeps head+tail, "
        "dropping the middle.\n"
        "\n"
        "Prefer Monitor over a blocking Bash call whenever the wait is longer "
        "than a few seconds: start it, keep doing useful work, and incorporate "
        "the result when the completion arrives."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "The command to run in the background.",
            },
            "cwd": {
                "type": "string",
                "description": (
                    "Working directory for this command (default: the session "
                    "cwd). Set it instead of prefixing the command with cd."
                ),
            },
        },
        "required": ["command"],
    },
    risk="write",
)


def _receipt_from(result: Any, input: dict[str, Any]) -> ToolOutcome:
    if isinstance(result, ToolOutcome):
        return result
    if isinstance(result, BackgroundTaskReceipt):
        return ToolOutcome(
            text=f"Monitoring started (task {result.task_id}).",
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
                or f"Monitoring started (task {receipt.task_id})."
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
