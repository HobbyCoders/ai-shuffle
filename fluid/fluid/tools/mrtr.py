"""MRTR (Multi Round-Trip Request) resolution — one loop, every gate.

A ``tools/call`` may answer ``input_required`` (MCP spec 2026-07-28) instead of
a result: the tool needs something from the human before it can finish. A
builtin or in-proc tool may return the same marker.

The marker is only half a call. Whoever dispatches a tool MUST resolve it:
collect one answer per request, then re-dispatch the *same* call with those
answers plus the echoed ``request_state``. A gate that dispatches once and
returns the first leg hands the model an empty string — which reads as "the
tool ran and said nothing". This module is the one implementation both gates
(the JSON path and code mode's host-call gate) call.

Bounded at :data:`MAX_ROUNDS`: a tool that keeps pausing gets its marker
surfaced as an in-context error instead of wedging the turn.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from fluid.tools.registry import InputRequired, ToolOutcome

MAX_ROUNDS = 3

# Collect the answers for one marker: {server-request-id: answer}, or None if
# the turn was interrupted while parked on the question.
CollectAnswers = Callable[[InputRequired], Awaitable[dict[str, Any] | None]]
# Re-dispatch the same call with (answers, request_state).
Redispatch = Callable[[dict[str, Any], str | None], Awaitable[ToolOutcome]]

INTERRUPTED_TEXT = "input_required round-trip interrupted"


JSON_PATH_REMEDY = "Call it through the JSON tool interface instead."


def unresolved_notice(tool_name: str, remedy: str = JSON_PATH_REMEDY) -> str:
    """What a gate that *cannot* run the round-trip must tell the model.

    Never an empty result: the tool asked the user a question and nobody was
    listening, so the model has to know the question was not asked.
    """
    return (
        f"{tool_name} paused to ask the user something, and this call path "
        "cannot run that round-trip — the user was NOT asked and there is no "
        f"answer. Do not treat this as a reply or invent one. {remedy}"
    )


async def resolve_input_required(
    outcome: ToolOutcome,
    *,
    name: str,
    collect: CollectAnswers,
    redispatch: Redispatch,
    max_rounds: int = MAX_ROUNDS,
) -> ToolOutcome:
    """Drive an ``input_required`` outcome to a real result.

    Returns ``outcome`` untouched when it carries no marker, so callers can pipe
    every dispatch through this without branching.
    """
    rounds = 0
    while outcome.input_required is not None and rounds < max_rounds:
        rounds += 1
        marker = outcome.input_required
        answers = await collect(marker)
        if answers is None:  # interrupted mid-question
            return ToolOutcome(text=INTERRUPTED_TEXT, is_error=True)
        outcome = await redispatch(answers, marker.request_state)
    if outcome.input_required is not None:
        return ToolOutcome(
            text=f"tool {name} still input_required after {rounds} round-trips",
            is_error=True,
        )
    return outcome
