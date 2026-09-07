"""ListAgents — the roster of agents this session can message.

Discovery half of the messaging pair (SendMessage is the other). Like Task and
Monitor, Fluid owns the tool's SHAPE and the host owns the machinery: the
embedding application injects an async ``agent_roster(input, ctx)`` through
``ctx.extra``, and without one the tool is invisible rather than a schema that
errors on call. Fluid's own CLI is a host too — :class:`fluid.cli.host.CliHost`
lists its Task workers plus the parent conversation — so ``fluid chat`` gets a
working roster out of the box.

The roster returns rows, not prose: the host hands back a list of mappings
(``name``, ``ref``, ``status``, ``description``, ``kind``) and this module
formats them. A name is the address; ``ref`` disambiguates the case where two
live agents share one, which is why every row prints both.
"""

from __future__ import annotations

import inspect
from typing import Any

from fluid.tools.registry import ToolCtx, ToolOutcome, ToolSpec

_ROW_CAP = 200


def describe(tools: frozenset[str] | None = None) -> str:
    """ListAgents' description, specialized to the session's tool surface."""
    has = (lambda name: tools is None or name in tools)
    send_route = (
        " Send to one with SendMessage, using the row's name as `to` — a "
        "finished agent is reachable too, and a message resumes it."
        if has("SendMessage")
        else ""
    )
    spawn_route = (
        " Agents appear here once Task starts them; a roster with nothing but "
        "`main` in it means nothing has been delegated yet."
        if has("Task")
        else ""
    )
    return (
        "List the agents this session can reach: the subagents it started and "
        "the parent conversation, with what each is working on and whether it "
        "is running right now." + send_route + "\n"
        "\n"
        "Each row leads with the agent's `name [ref]`. The NAME is the "
        "address — send the bare name, and append the ` [ref]` only when two "
        "rows share a name or an error asks you to disambiguate." + spawn_route + "\n"
        "\n"
        "This is a snapshot, not a subscription. A running agent's completion "
        "is pushed to you in a later turn; never call this in a loop to watch "
        "for it."
    )


SPEC = ToolSpec(
    name="ListAgents",
    description=describe(),
    input_schema={
        "type": "object",
        "properties": {
            "running_only": {
                "type": "boolean",
                "description": (
                    "List only agents still working (default false: finished "
                    "ones are listed too, and can be messaged)."
                ),
            },
        },
    },
    risk="safe",
    requires_extra=("agent_roster",),
)


def _format(rows: list[dict[str, Any]]) -> str:
    """One line per agent: ``name [ref] — status · description``."""
    if not rows:
        return (
            "No other agents in this session. Delegate one with Task before "
            "trying to message it."
        )
    lines: list[str] = []
    for row in rows[:_ROW_CAP]:
        name = str(row.get("name") or "(unnamed)")
        ref = row.get("ref")
        head = f"{name} [{ref}]" if ref else name
        status = str(row.get("status") or "unknown")
        parts = [f"{head} — {status}"]
        description = str(row.get("description") or "").strip()
        if description:
            parts.append(description)
        lines.append(" · ".join(parts))
    if len(rows) > _ROW_CAP:
        lines.append(f"… and {len(rows) - _ROW_CAP} more")
    return "\n".join(lines)


async def handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    roster = (ctx.extra or {}).get("agent_roster")
    if roster is None:
        return ToolOutcome(
            text=(
                "Agent listing is not available in this environment — no "
                "roster was injected by the host application."
            ),
            is_error=True,
        )
    result = roster(input, ctx)
    if inspect.isawaitable(result):
        result = await result
    if isinstance(result, ToolOutcome):
        return result
    if isinstance(result, list):
        rows = [r for r in result if isinstance(r, dict)]
        return ToolOutcome(text=_format(rows), ui={"agents": rows})
    return ToolOutcome(text=str(result))
