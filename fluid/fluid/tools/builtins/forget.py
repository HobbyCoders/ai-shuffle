"""Explicit, revision-checked deletion from durable memory."""
from __future__ import annotations

import asyncio
from fluid.tools.registry import ToolOutcome, ToolSpec

SPEC = ToolSpec(
    name="Forget", risk="write", requires_extra=("memory_store",),
    description=("Forget a saved fact only when the user explicitly asks. First Recall "
                 "the fact to obtain its ID and revision. Removes its live, archived "
                 "and retained historical memory values and prevents stale replica "
                 "replay. This does not erase the source conversation or provider logs; "
                 "session deletion is a separate action. Never use this for automatic cleanup."),
    input_schema={"type": "object", "properties": {
        "memory_id": {"type": "string"},
        "expected_revision": {"type": "integer", "minimum": 1},
        "confirm": {"type": "boolean", "description": "True only after the user's explicit request to forget this fact."},
    }, "required": ["memory_id", "expected_revision", "confirm"]},
)


async def handler(input, ctx):
    if input.get("confirm") is not True:
        return ToolOutcome(text="User confirmation is required; nothing was forgotten.", is_error=True)
    store = ctx.extra.get("memory_store")
    if store is None:
        return ToolOutcome(text="Memory storage is unavailable; nothing was forgotten.", is_error=True)
    if type(input.get("expected_revision")) is not int:
        return ToolOutcome(text="Recall the fact first and supply its revision.", is_error=True)
    try:
        deleted = await asyncio.to_thread(store.delete_memory, str(input.get("memory_id", "")),
                                         explicit=True, expected_revision=input["expected_revision"])
    except (ValueError, OSError, TimeoutError) as exc:
        return ToolOutcome(text=f"Not forgotten: {exc}", is_error=True)
    return ToolOutcome(text=("Forgotten locally, including archived memory and retained revisions. "
                             "Deletion will replicate when workers synchronize. Source conversations "
                             "and already-issued provider requests are not erased."
                             if deleted else "No current or archived memory has that ID."))
