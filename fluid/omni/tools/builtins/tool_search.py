"""ToolSearch — the model-facing progressive-disclosure seam.

MCP tool schemas stay out of the cached prompt prefix until needed (the
Manus/Anthropic "code execution with MCP" lesson). This builtin is how the
model pulls a deferred tool in on the JSON surface: search the registry's full
catalog (loaded or not), load the matches' schemas, and return them. Loaded
tools enter ``list_tools(loaded_only=True)`` and are therefore callable from
the NEXT model call onward.

The registry is injected by the engine as ``ctx.extra["tool_registry"]``.
"""

from __future__ import annotations

import json
from typing import Any

from omni.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="ToolSearch",
    description=(
        "Find and load deferred tools (e.g. MCP server tools) so they can be "
        "called. Deferred tools are known by name and description but their "
        "schemas stay out of context until loaded here. Query forms: "
        "'select:<name>[,<name>...]' loads those exact tools; any other text "
        "is a keyword search over names + descriptions. Once a tool's schema "
        "appears in the result, it is callable from your next reply."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "'select:name1,name2' for exact tools, or keywords "
                    "(e.g. 'browser screenshot')."
                ),
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum keyword-search matches to load (default 5).",
            },
        },
        "required": ["query"],
    },
    risk="safe",
)


def _spec_line(spec: ToolSpec) -> str:
    return json.dumps(
        {
            "name": spec.name,
            "description": spec.description,
            "input_schema": spec.input_schema,
        },
        ensure_ascii=False,
    )


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    registry = (ctx.extra or {}).get("tool_registry")
    if registry is None:
        return ToolOutcome(
            text="ToolSearch unavailable: no tool registry injected by the host.",
            is_error=True,
        )
    query = str(input.get("query", "")).strip()
    if not query:
        return ToolOutcome(text="empty query", is_error=True)
    limit = int(input.get("max_results") or 5)

    if query.startswith("select:"):
        names = [n.strip() for n in query[len("select:"):].split(",") if n.strip()]
        missing = [n for n in names if registry.get(n) is None]
        if missing:
            return ToolOutcome(
                text=f"unknown tool(s): {', '.join(missing)}", is_error=True
            )
        specs = [registry.load_schema(n) for n in names]
    else:
        specs = registry.search_tools(query)[:limit]
        specs = [registry.load_schema(s.name) for s in specs]

    if not specs:
        return ToolOutcome(
            text=f"no tools matched {query!r}; try different keywords."
        )
    lines = "\n".join(_spec_line(s) for s in specs)
    return ToolOutcome(
        text=(
            f"Loaded {len(specs)} tool(s) — callable from your next reply:\n{lines}"
        )
    )
