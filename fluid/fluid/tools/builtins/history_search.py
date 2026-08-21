"""HistorySearch — find content anywhere in this session's stored history.

Searches the STORED nodes, not the rendered view: text that compaction
digested or folded away is still found here, and every hit carries a node ref
for HistoryExpand. Engine-injected ``ctx.extra["history_recall"]`` provides
store access.
"""

from __future__ import annotations

from typing import Any

from fluid.context.recall import DEFAULT_SEARCH_RESULTS, render_hits, search_path
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="HistorySearch",
    description=(
        "Keyword-search the FULL stored history of this session — including "
        "turns that were compacted into digests or epoch stubs. Returns "
        "matching nodes with snippets and refs; re-read any hit verbatim "
        "with HistoryExpand."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Space-separated keywords (case-insensitive).",
            },
            "max_results": {
                "type": "integer",
                "description": f"Maximum hits to return (default {DEFAULT_SEARCH_RESULTS}).",
            },
        },
        "required": ["query"],
    },
    risk="safe",
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    recall = (ctx.extra or {}).get("history_recall")
    if recall is None:
        return ToolOutcome(
            text="HistorySearch unavailable: no session store injected by the host.",
            is_error=True,
        )
    query = str(input.get("query", "")).strip()
    if not query:
        return ToolOutcome(text="empty query", is_error=True)
    nodes = await recall.load_path()
    hits = search_path(
        nodes,
        query,
        max_results=int(input.get("max_results") or DEFAULT_SEARCH_RESULTS),
    )
    return ToolOutcome(text=render_hits(hits, query))
