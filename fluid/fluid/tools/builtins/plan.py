"""Plan / StopPlanning — model-initiated plan mode with approval-gated exit.

Distilled from Claude Code's EnterPlanMode/ExitPlanMode pair (compact form):
Plan flips the session into the read-only planning surface (``plan_mode``
drops every write/destructive schema from the request); StopPlanning presents
the finished plan and asks the user to approve leaving it, over the same MRTR
round-trip AskUser uses. Only user approval ends plan mode — a rejected plan
keeps the session read-only with the user's feedback in the tool result.

Fluid owns the tools' SHAPE; the switch itself is engine territory. The
engine injects a :class:`PlanModeControl` as ``ctx.extra["plan_mode_control"]``
(NativeLoop wires its own ``plan_mode`` flag). Without one — the delegated
leg, where the Claude CLI ships its own plan mode — the tools are dropped
from the surface via ``requires_extra``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fluid.tools.registry import InputRequired, ToolCtx, ToolOutcome, ToolSpec


@dataclass(frozen=True)
class PlanModeControl:
    """Engine-injected plan-mode switch (``ctx.extra["plan_mode_control"]``)."""

    is_active: Callable[[], bool]
    set_active: Callable[[bool], None]


ENTER_RESULT = (
    "Plan mode is on. Write and shell tools have left your surface. Explore "
    "the codebase with the read tools, weigh the approaches, then present a "
    "concrete plan with StopPlanning for the user's approval. Do not "
    "describe edits as if you were making them."
)

PLAN_SPEC = ToolSpec(
    name="Plan",
    description=(
        "Enter plan mode: a read-only phase for designing an approach before "
        "writing code. Your write and shell tools leave the surface until "
        "the user approves a plan.\n"
        "\n"
        "Use it proactively for non-trivial implementation work: new "
        "features, changes with several valid approaches or architectural "
        "choices, work likely to touch more than a couple of files, or "
        "requests you must explore before you understand. If you would ask "
        "the user to choose an approach up front, enter plan mode and "
        "present the options with context instead. Skip it for simple "
        "fixes, tasks with very specific instructions, and pure "
        "research/exploration — reading and searching never need plan "
        "mode.\n"
        "\n"
        "In plan mode: explore, understand the existing patterns, weigh "
        "trade-offs, then present one concrete plan with StopPlanning. When "
        "unsure whether to plan, plan — alignment upfront beats redone work."
    ),
    input_schema={"type": "object", "properties": {}},
    risk="safe",
    requires_extra=("plan_mode_control",),
)

STOP_SPEC = ToolSpec(
    name="StopPlanning",
    description=(
        "Present your finished plan and ask the user to approve leaving "
        "plan mode. Approval is THIS tool's job — never ask \"is this plan "
        "okay?\" through another channel.\n"
        "\n"
        "Pass the complete plan as markdown: what will change (file by "
        "file), the approach chosen and why, and how the result will be "
        "verified. On approval plan mode ends — implement exactly the "
        "approved plan without re-asking permission for steps it already "
        "covers. On rejection you stay in plan mode: read the user's "
        "feedback, revise, and present again.\n"
        "\n"
        "Only for implementation planning: a research or explanation task "
        "does not need approval to finish."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "plan": {
                "type": "string",
                "description": (
                    "The complete implementation plan, as markdown. "
                    "Self-contained — the user approves exactly this text."
                ),
            },
        },
        "required": ["plan"],
    },
    risk="safe",
    requires_extra=("plan_mode_control",),
)

# Answers treated as approval on the MRTR second leg. "1" is the first
# option's index (Approve is listed first); anything else is feedback.
_APPROVALS = frozenset({"1", "y", "yes", "ok", "approve", "approved", "lgtm"})


def _control(ctx: ToolCtx) -> PlanModeControl | None:
    control = (ctx.extra or {}).get("plan_mode_control")
    return control if isinstance(control, PlanModeControl) else None


async def plan_handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    control = _control(ctx)
    if control is None:
        return ToolOutcome(
            text=(
                "Plan mode is not available in this environment — no plan "
                "controller was injected by the host."
            ),
            is_error=True,
        )
    if control.is_active():
        return ToolOutcome(
            text=(
                "Already in plan mode — keep planning, then present the "
                "plan with StopPlanning."
            ),
            is_error=True,
        )
    control.set_active(True)
    return ToolOutcome(text=ENTER_RESULT)


async def stop_handler(input: dict[str, Any], ctx: ToolCtx) -> ToolOutcome:
    control = _control(ctx)
    if control is None:
        return ToolOutcome(
            text=(
                "Plan mode is not available in this environment — no plan "
                "controller was injected by the host."
            ),
            is_error=True,
        )
    if not control.is_active():
        return ToolOutcome(
            text="Not in plan mode — nothing to stop. Enter it with Plan.",
            is_error=True,
        )
    plan = str(input.get("plan", "")).strip()
    if not plan:
        return ToolOutcome(
            text="pass the complete plan as markdown in `plan`", is_error=True
        )

    answers = input.get("__input_responses__")
    if answers is None:
        # First leg: hand the engine one approval request and park (the same
        # MRTR round-trip AskUser rides).
        question = {
            "question": "Approve this plan and leave plan mode?",
            "header": "Plan",
            "options": [
                {
                    "label": "Approve",
                    "description": "Exit plan mode and implement this plan.",
                },
                {
                    "label": "Keep planning",
                    "description": "Stay in plan mode; answer with feedback.",
                },
            ],
        }
        return ToolOutcome(
            text="",
            input_required=InputRequired(
                server="builtin",
                tool="StopPlanning",
                request_state=None,
                requests={
                    "approve": {
                        "message": (
                            f"{plan}\n\nApprove this plan and leave plan "
                            "mode? (1 = Approve, anything else = feedback)"
                        ),
                        "schema": question,
                    }
                },
            ),
        )

    raw = str(answers.get("approve", "")).strip()
    if raw.lower() in _APPROVALS:
        control.set_active(False)
        return ToolOutcome(
            text=(
                "Plan approved — plan mode is off. Implement the approved "
                "plan now; steps it covers need no re-asking."
            )
        )
    feedback = raw or "(no feedback given)"
    return ToolOutcome(
        text=(
            "The user did NOT approve the plan and answered:\n"
            f"{feedback}\n"
            "Still in plan mode — revise the plan to address the feedback "
            "and present it again with StopPlanning."
        )
    )


SPECS = ((PLAN_SPEC, plan_handler), (STOP_SPEC, stop_handler))
