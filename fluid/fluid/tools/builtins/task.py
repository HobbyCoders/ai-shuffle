"""Task — dispatch work to a subagent (agents-program rulings A6/A8/A13).

Fluid owns the tool's SHAPE (bare name, schema, description travel with the
harness, Claude Code-parity); the actual dispatch machinery — agent registry,
session creation, approval routing, nesting/budget enforcement — is host-app
territory. The embedding application injects an async dispatcher as
``ctx.extra["task_dispatch"]``; without one the tool reports itself
unavailable instead of pretending to work. Fluid's own CLI is a host too:
:class:`fluid.cli.host.CliHost` injects a dispatcher that runs the subagent as
a child NativeLoop, so ``fluid chat`` / ``fluid run`` get working Task out of
the box.

The dispatcher receives ``(input, ctx)`` and must return a ``ToolOutcome`` (or
a plain string, which is wrapped). Contract for ``input``:
  * agent   — target subagent id or unambiguous name (required)
  * prompt  — the task for the subagent (required)
  * mode    — "sync" (default): block until the worker finishes and return its
              report; "background": atomically detach and return a
              ``BackgroundTaskReceipt`` (or ``ToolOutcome.background_task``).
              The host later submits ``BackgroundTaskFinished`` to the parent
              engine; Fluid persists the result and wakes the parked session.
  * label   — optional short display label

The host may replace this fallback spec per session with an eligible subagent
roster. Provider and model selection remain host-owned and are not accepted as
model-authored Task arguments.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from fluid.tools.fragments import DONT_PREDICT_PENDING, END_TURN_ON_BACKGROUND
from fluid.tools.registry import (
    BackgroundTaskReceipt,
    ToolCtx,
    ToolOutcome,
    ToolSpec,
)

SPEC = ToolSpec(
    name="Task",
    description=(
        "Delegate a self-contained task to a configured subagent. Available "
        "agent types are listed under '# Available agent types' in your "
        "system prompt — use only names listed there; when no such section "
        "exists, do not call Task. The subagent runs in an isolated session "
        "with its own configuration, memory, and tools — it does not see "
        "this conversation, so give it a complete brief: objective, relevant "
        "paths and context, constraints, and the report you expect back.\n"
        "\n"
        "Delegate independent or context-heavy work (broad searches, "
        "multi-file investigations, parallelizable changes); handle a quick "
        "lookup or single edit yourself. To run several independent "
        "subagents concurrently, send their Task calls together in one "
        "reply. Do not duplicate delegated work by also starting it "
        "yourself.\n"
        "\n"
        "mode='sync' (default) waits for the subagent's report; "
        "mode='background' returns immediately with the worker session id "
        "and completion is pushed to you later — keep doing other useful "
        f"work instead of polling. {DONT_PREDICT_PENDING} "
        f"{END_TURN_ON_BACKGROUND}\n"
        "\n"
        "A subagent's report is a SELF-REPORT, not a verified fact: for any "
        "external side effect it claims (a file written, a push, an upload), "
        "verify the handle it returns — stat the path, fetch the URL, read "
        "the content — before telling the user the operation succeeded."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": "Target subagent id (or unambiguous name).",
            },
            "prompt": {
                "type": "string",
                "description": "The task for the subagent. Self-contained — "
                "the subagent does not see this conversation.",
            },
            "mode": {
                "type": "string",
                "enum": ["sync", "background"],
                "description": "sync (default) waits; background returns at once.",
            },
            "label": {
                "type": "string",
                "description": "Short display label for progress UI.",
            },
        },
        "required": ["agent", "prompt"],
    },
    risk="write",
    # Host-injected dispatcher (see module docstring). Without it the engine
    # drops Task from the request schemas — an undeliverable delegation tool
    # must be invisible, not advertised.
    requires_extra=("task_dispatch",),
)


def _background_outcome(result: Any, input: dict[str, Any]) -> ToolOutcome:
    if isinstance(result, ToolOutcome):
        return result
    if isinstance(result, BackgroundTaskReceipt):
        return ToolOutcome(
            text=f"Background task {result.task_id} started.",
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
            description=result.get("description") or input.get("label"),
        )
        return ToolOutcome(
            text=str(
                result.get("text")
                or result.get("message")
                or f"Background task {receipt.task_id} started."
            ),
            ui=result.get("ui", result),
            is_error=bool(result.get("is_error", False)),
            background_task=receipt,
        )
    return ToolOutcome(text=str(result))


async def _dispatch_background(dispatch: Any, input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    outcome = _background_outcome(await dispatch(input, ctx), input)
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
    dispatch = (ctx.extra or {}).get("task_dispatch")
    if dispatch is None:
        return ToolOutcome(
            text=(
                "Task dispatch is not available in this environment — no "
                "dispatcher was injected by the host application."
            ),
            is_error=True,
        )
    if input.get("mode", "sync") == "background":
        # The spawn acknowledgement is an atomic detach boundary. Interrupting
        # the parent turn may stop consuming this tool result, but must not cancel
        # a worker the host has already started or lose its durable task id.
        spawn = asyncio.create_task(_dispatch_background(dispatch, input, ctx))
        tracked = (ctx.extra or {}).get("background_spawn_tasks")
        if isinstance(tracked, set):
            tracked.add(spawn)
            spawn.add_done_callback(tracked.discard)
        return await asyncio.shield(spawn)
    result = await dispatch(input, ctx)
    if isinstance(result, ToolOutcome):
        return result
    return ToolOutcome(text=str(result))
