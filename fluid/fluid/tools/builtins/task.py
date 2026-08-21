"""Task — dispatch work to a subagent (agents-program rulings A6/A8/A13).

Fluid owns the tool's SHAPE (bare name, schema, description travel with the
harness, Claude Code-parity); the actual dispatch machinery — agent registry,
session creation, approval routing, nesting/budget enforcement — is host-app
territory. The embedding application injects an async dispatcher as
``ctx.extra["task_dispatch"]``; without one the tool reports itself
unavailable instead of pretending to work (fluid's own CLI runs without it
until it grows a dispatcher of its own).

The dispatcher receives ``(input, ctx)`` and must return a ``ToolOutcome`` (or
a plain string, which is wrapped). Contract for ``input``:
  * agent   — target subagent id or unambiguous name (required)
  * prompt  — the task for the subagent (required)
  * mode    — "sync" (default): block until the worker finishes and return its
              report; "background": return immediately with the worker's
              session id — collect later via the host's task tools.
  * label   — optional short display label

The host may replace this fallback spec per session with an eligible subagent
roster. Provider and model selection remain host-owned and are not accepted as
model-authored Task arguments.
"""

from __future__ import annotations

from typing import Any

from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="Task",
    description=(
        "Delegate a self-contained task to a configured subagent. The "
        "subagent runs in an isolated session with its own configuration, "
        "memory, and tools — it does not see this conversation, so give it a "
        "complete brief: objective, relevant paths and context, constraints, "
        "and the report you expect back.\n"
        "\n"
        "Delegate independent or context-heavy work (broad searches, "
        "multi-file investigations, parallelizable changes); handle a quick "
        "lookup or single edit yourself. Do not duplicate delegated work by "
        "also starting it yourself.\n"
        "\n"
        "mode='sync' (default) waits for the subagent's report; "
        "mode='background' returns immediately with the worker session id "
        "and completion is pushed to you later — keep doing other useful "
        "work instead of polling."
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
