"""HistoryExpand — re-read compacted history verbatim.

The recall half of recoverable compaction: digests, epoch stubs, trim
placeholders and intake stubs all carry node refs; this builtin resolves a ref
against the session store and returns the ORIGINAL content. The engine injects
``ctx.extra["history_recall"]`` (:class:`fluid.context.recall.HistoryRecall`).
"""

from __future__ import annotations

from typing import Any

from fluid.context.recall import DEFAULT_EXPAND_CHARS, expand_ref
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="HistoryExpand",
    description=(
        "Re-read earlier conversation content verbatim from the session "
        "store. Compacted history (digest lines, epoch stubs, trimmed-result "
        "placeholders, large-attachment stubs) carries refs like "
        '"node_ab12cd34ef56" or "node_a..node_b" — pass one to recover the '
        "original, exact text. Large content is windowed: use offset to "
        "continue reading."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "ref": {
                "type": "string",
                "description": 'A node ref: "node_<id>" or "node_<a>..node_<b>".',
            },
            "offset": {
                "type": "integer",
                "description": "Character offset to start reading from (default 0).",
            },
            "max_chars": {
                "type": "integer",
                "description": f"Window size in chars (default {DEFAULT_EXPAND_CHARS}, max 50000).",
            },
        },
        "required": ["ref"],
    },
    risk="safe",
    # Engine-injected recall handle; absent (no session store) the engine
    # drops the tool from the request schemas.
    requires_extra=("history_recall",),
)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    recall = (ctx.extra or {}).get("history_recall")
    if recall is None:
        return ToolOutcome(
            text="HistoryExpand unavailable: no session store injected by the host.",
            is_error=True,
        )
    ref = str(input.get("ref", "")).strip()
    if not ref:
        return ToolOutcome(text="empty ref", is_error=True)
    nodes = await recall.load_path()
    text, is_error = expand_ref(
        nodes,
        ref,
        offset=int(input.get("offset") or 0),
        max_chars=int(input.get("max_chars") or DEFAULT_EXPAND_CHARS),
    )
    return ToolOutcome(text=text, is_error=is_error)
