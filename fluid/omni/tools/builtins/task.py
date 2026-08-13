"""Task — dispatch work to another agent (agents-program rulings A6/A8/A13).

Omni owns the tool's SHAPE (bare name, schema, description travel with the
harness, Claude Code-parity); the actual dispatch machinery — agent registry,
session creation, approval routing, nesting/budget enforcement — is host-app
territory. The embedding application injects an async dispatcher as
``ctx.extra["task_dispatch"]``; without one the tool reports itself
unavailable instead of pretending to work (omni's own CLI runs without it
until it grows a dispatcher of its own).

The dispatcher receives ``(input, ctx)`` and must return a ``ToolOutcome`` (or
a plain string, which is wrapped). Contract for ``input``:
  * agent   — target agent id or unambiguous name (required)
  * prompt  — the task for the agent (required)
  * mode    — "sync" (default): block until the worker finishes and return its
              report; "background": return immediately with the worker's
              session id — collect later via the host's task tools.
  * model   — optional model override honored when the target agent allows it
  * label   — optional short display label
"""

from __future__ import annotations

from typing import Any

from omni.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="Task",
    description=(
        "Dispatch work to another agent. The target agent runs in its own "
        "session with its own configuration, memory, and tools, and reports "
        "back. mode='sync' (default) waits for the result; mode='background' "
        "returns immediately with the worker session id (poll with the task "
        "tools). Use background mode to fan out parallel work."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "agent": {
                "type": "string",
                "description": "Target agent id (or unambiguous name).",
            },
            "prompt": {
                "type": "string",
                "description": "The task for the target agent. Self-contained — "
                "the agent does not see this conversation.",
            },
            "mode": {
                "type": "string",
                "enum": ["sync", "background"],
                "description": "sync (default) waits; background returns at once.",
            },
            "model": {
                "type": "string",
                "description": "Optional model override for this run.",
            },
            "label": {
                "type": "string",
                "description": "Short display label for progress UI.",
            },
        },
        "required": ["agent", "prompt"],
    },
    risk="write",
)


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
    result = await dispatch(input, ctx)
    if isinstance(result, ToolOutcome):
        return result
    return ToolOutcome(text=str(result))
