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
                 "session deletion is a separate action. Never use this for automatic cleanup. "
                 "With proposal=<topic> and confirm=true instead, it dismisses a [proposed, not saved] "
                 "line the owner declined or a saved memory already states."),
    input_schema={"type": "object", "properties": {
        "memory_id": {"type": "string"},
        "expected_revision": {"type": "integer", "minimum": 1},
        "confirm": {"type": "boolean", "description": "For a fact, true only after the user's explicit request to forget it."},
        "proposal": {"type": "string", "description": "Proposal topic to dismiss; no memory_id."},
    }, "required": ["confirm"]},
)


async def handler(input, ctx):
    if input.get("confirm") is not True:
        return ToolOutcome(text="User confirmation is required; nothing was forgotten.", is_error=True)
    store = ctx.extra.get("memory_store")
    if store is None:
        return ToolOutcome(text="Memory storage is unavailable; nothing was forgotten.", is_error=True)
    from fluid.codemode.preferences import valid_proposal
    proposal = valid_proposal(input.get("proposal"))
    if proposal is None or (proposal and input.get("memory_id")):
        return ToolOutcome(text="Nothing changed: pass either memory_id or proposal (the topic of a "
                                "[proposed, not saved; <topic>] memory line), not both.", is_error=True)
    if proposal:
        return await _dismiss(store, proposal, ctx)
    if not input.get("memory_id") or type(input.get("expected_revision")) is not int:
        return ToolOutcome(text="Recall the fact first and supply its ID and revision.", is_error=True)
    try:
        deleted = await asyncio.to_thread(store.delete_memory, str(input.get("memory_id", "")),
                                         explicit=True, expected_revision=input["expected_revision"])
    except (ValueError, OSError, TimeoutError) as exc:
        return ToolOutcome(text=f"Not forgotten: {exc}", is_error=True)
    return ToolOutcome(text=("Forgotten locally, including archived memory and retained revisions. "
                             "Deletion will replicate when workers synchronize. Source conversations "
                             "and already-issued provider requests are not erased."
                             if deleted else "No current or archived memory has that ID."))


async def _dismiss(store, proposal: str, ctx) -> ToolOutcome:
    """Close a learner proposal without saving it. No fact is touched."""
    from fluid.codemode.preferences import close_proposal
    from fluid.codemode.skills import project_key_for
    project = ctx.extra.get("memory_project") or project_key_for(ctx.cwd)
    try:
        text = await asyncio.to_thread(close_proposal, store, proposal, project, how="dismissed")
    except Exception as exc:  # noqa: BLE001 — busy or unwritable queue: nothing changed
        return ToolOutcome(text=f"Not dismissed: {exc}. Nothing changed; retry.", is_error=True)
    return ToolOutcome(text=text + " No saved fact was changed.")
