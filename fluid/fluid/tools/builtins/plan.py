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

The exit is decided on the MRTR **retry** leg, by the answer alone. The live
flag is deliberately not a precondition there: a host that gates the approval
itself (an embedding app renders the panel and syncs its own state before it
hands the answer back) has already left plan mode by then, and re-checking the
flag turned an approved exit into an error telling the model to enter plan mode
again — the session re-locked itself the moment the user approved it.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fluid.tools.registry import InputRequired, ToolCtx, ToolOutcome, ToolSpec


@dataclass(frozen=True)
class PlanModeControl:
    """Engine-injected plan-mode switch (``ctx.extra["plan_mode_control"]``).

    ``auto_approve`` says the approval gate is a no-op on this host: the plan
    is accepted by policy (bypass permissions, an unattended runner) without
    anyone reading it. Fluid cannot detect that — only the host knows — and
    left unset the tools keep promising a human decision that will not happen.
    Set it and the entry result says the gate is automatic instead, so a model
    does not go on to tell the user their plan was approved.
    """

    is_active: Callable[[], bool]
    set_active: Callable[[bool], None]
    auto_approve: bool = False


# What the mode takes away, in the words every plan-mode message uses: the
# tools with write or destructive risk, which is more than files and the
# shell. Task and Monitor start work outside this context; WebFetch reaches
# the network. The description used to promise only "write and shell".
LEAVES = "file writes and edits, the shell, Task, Monitor, and WebFetch"

ENTER_RESULT = (
    f"Plan mode is on. {LEAVES[0].upper()}{LEAVES[1:]} have left your surface. "
    "Explore the codebase with the read tools, weigh the approaches, then "
    "present a concrete plan with StopPlanning for the user's approval. Do "
    "not describe edits as if you were making them."
)

# Same mode, honest about the gate: on a host that approves by policy nobody
# reads the plan, so promising "the user's approval" produces a model that
# later reports back "the plan is approved" as if a person had said so.
ENTER_RESULT_AUTO = (
    f"Plan mode is on. {LEAVES[0].upper()}{LEAVES[1:]} have left your surface. "
    "Explore the codebase with the read tools, weigh the approaches, then "
    "present a concrete plan with StopPlanning. Note that this host approves "
    "automatically — StopPlanning restores your tools by policy, no one "
    "reviews the plan, so do not report it back as user-approved. Keep the "
    "plan short: nobody is reading it before you execute it, and the host may "
    "post it for the user to read afterwards. Do not describe edits as if you "
    "were making them."
)


def enter_result(*, auto_approve: bool = False) -> str:
    """The Plan entry text, honest about whether a human will decide."""
    return ENTER_RESULT_AUTO if auto_approve else ENTER_RESULT

APPROVED_RESULT = (
    "Plan approved — plan mode is now OFF. Your write tools are back on the "
    f"surface ({LEAVES}). Execute the approved plan now; steps it already "
    "covers need no further permission."
)

PLAN_SPEC = ToolSpec(
    name="Plan",
    description=(
        "Enter plan mode: a read-only phase for designing an approach before "
        f"writing code. Every tool that changes something ({LEAVES}) leaves "
        "the surface until the user approves a plan.\n"
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
        "Present your finished plan and end plan mode. The decision is THIS "
        "tool's job — never ask \"is this plan okay?\" through another "
        "channel.\n"
        "\n"
        "Pass the complete plan in your final response: what will change "
        "(file by file), the approach chosen and why, and how the result "
        "will be verified. On approval plan mode ends — implement exactly "
        "the approved plan without re-asking permission for steps it "
        "already covers. On rejection you stay in plan mode: read the "
        "user's feedback, revise, and present again.\n"
        "\n"
        "Who answers depends on the host. Some hosts put the plan in front of "
        "a person; others answer it by policy (bypass permissions, an "
        "unattended runner) and nobody reads it before you execute — the "
        "result text says which happened. When the answer is policy, keep the "
        "plan short, do not report it back as user-approved, and expect the "
        "host to post it for the user to read afterwards.\n"
        "\n"
        "Calling this outside plan mode is fine, not an error: the plan is "
        "recorded and you proceed. Plan mode is engine-session state and does "
        "not survive an engine teardown between turns, so a plan you began in "
        "an earlier turn can land there — never re-enter plan mode just to "
        "leave it again.\n"
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

# The host ended plan mode before the answer came back, and nothing in the
# payload says a person answered. The old text opened "Plan approved" and
# models relayed that to users verbatim — on hosts that auto-approve under
# bypass permissions, nobody had read the plan at all. State the mechanism, not
# a verdict: the tools are back either way, which is the actionable part.
#
# ORDER IS THE POINT. An earlier revision led with two sentences about the
# absence of approval and buried "proceed with the plan" in the third; an
# audited session read it, made two more calls and ended the turn with "The
# plan has not received user approval" - needless spend and no edits, and the user had
# to type "Complete the plan." to restart it. The imperative comes first now,
# and the attribution caveat is scoped to what it is actually about: how to
# describe the plan later, not whether to execute it.
ALREADY_OFF_RESULT = (
    "Plan mode is off — proceed with the plan now. Your write tools are back "
    f"on the surface ({LEAVES}) and steps the plan already covers need no "
    "further permission; raise anything a user would want to weigh in on "
    "before you do the irreversible part.\n"
    "One caveat, for how you describe this later and not for what you do "
    "next: the host ended plan mode by policy, so no user review was "
    "recorded — do not report the plan as user-approved."
)

# The host ended plan mode itself AND is telling us a real answer came back
# from a person (see :data:`USER_ANSWERED_KEY`) — approval is attributable.
ALREADY_OFF_APPROVED_RESULT = (
    "Plan approved by the user — plan mode is OFF (the host had already ended "
    f"it). Your write tools are on the surface ({LEAVES}). Execute the "
    "approved plan now; steps it already covers need no further permission."
)

# StopPlanning called while plan mode is already off. This used to be an ERROR
# reading "Not in plan mode — nothing to stop. Enter it with Plan.", and models
# followed it literally: an audited session entered plan mode purely so it could
# leave it again one call later, paying for a full uncached prefix on the way.
# Plan mode is engine-session state and does not survive an engine teardown, so
# a plan begun before a park legitimately lands here. Accept the plan, say the
# tools are already available, and forbid the round trip by name.
NOT_PLANNING_RESULT = (
    "Plan mode is off — proceed with the plan now. Nothing had to be exited: "
    f"your write tools were already on the surface ({LEAVES}). Plan mode does "
    "not survive an engine teardown between turns, so a plan begun in an "
    "earlier turn arrives here rather than at an approval prompt. Do NOT "
    "re-enter plan mode just to leave it again; present the plan to the user "
    "in your own words if it needs their input."
)

# Same situation, but the call carried no plan text — there is nothing to
# record, so the only useful thing left to say is "carry on".
NOT_PLANNING_NO_PLAN_RESULT = (
    "Plan mode is off — there was nothing to exit, and no plan was passed. "
    f"Your write tools are on the surface ({LEAVES}); carry on. Do not enter "
    "plan mode just to leave it again."
)

#: Optional key in the ``__input_responses__`` payload. A host that can tell a
#: human answer from a policy auto-approval sets it truthy on the human one;
#: absent, fluid assumes the honest thing — that nobody approved anything —
#: rather than crediting a user with a decision the host made.
USER_ANSWERED_KEY = "__user_answered__"


def _user_answered(answers: Any) -> bool:
    """Whether the host says a person actually answered the approval prompt."""
    if not isinstance(answers, dict):
        return False
    return bool(answers.get(USER_ANSWERED_KEY))

# Answers treated as approval on the MRTR second leg. "1" is the first
# option's index (Approve is listed first); anything else is feedback.
#
# Matched against the NORMALIZED answer, so each entry covers its punctuated
# and capitalized forms ("Proceed.", "LGTM!", "Yes, go ahead"). A host that
# renders the approval as a button sends a canonical token, but the fluid REPL
# hands the user's literal words through — and a user who types the way users
# actually type ("go ahead") was silently read as plan FEEDBACK and left the
# session read-only with no sign anything had gone wrong.
_APPROVALS = frozenset({
    "1",
    "y", "yes", "yes please", "yep", "yeah", "yup",
    "ok", "okay", "k", "sure", "sounds good", "sgtm",
    "approve", "approved", "approve it", "approve plan", "approve the plan",
    "approve and proceed", "i approve", "plan approved",
    "proceed", "please proceed", "proceed please", "yes proceed",
    "proceed with the plan",
    "go", "go ahead", "yes go ahead", "please go ahead", "go for it",
    "do it", "yes do it", "just do it", "do it please",
    "lgtm", "looks good", "looks good to me", "this looks good",
    "ship it", "make it so",
    "let's do it", "lets do it", "let's go", "lets go",
    "implement", "implement it", "implement the plan",
    "execute the plan", "run it",
    "start", "begin", "continue", "carry on",
    "complete the plan", "finish the plan",
    "accept", "accepted", "confirm", "confirmed",
    "exit plan mode", "leave plan mode",
})

# Punctuation that only ever decorates such an answer. The apostrophe is NOT
# here — stripping it turns "let's do it" into two unmatchable words.
_ANSWER_PUNCT_RE = re.compile(r"[.,;:!?\"`*()\[\]]+")


def normalize_answer(text: Any) -> str:
    """Lowercase, de-punctuate and collapse an approval answer for matching."""
    cleaned = _ANSWER_PUNCT_RE.sub(" ", str(text or "").replace("’", "'"))
    return re.sub(r"\s+", " ", cleaned).strip().lower()


def is_approval(text: Any) -> bool:
    """True when this answer means "approve the plan and leave plan mode"."""
    return normalize_answer(text) in _APPROVALS


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
    return ToolOutcome(
        text=enter_result(auto_approve=bool(getattr(control, "auto_approve", False)))
    )


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
    answers = input.get("__input_responses__")
    if answers is None:
        # ── First leg: nothing has been asked yet. ───────────────────────────
        plan = str(input.get("plan", "")).strip()
        if not control.is_active():
            # Not an error: there is nothing to stop, the write tools are
            # already available, and the plan the model just wrote is the
            # thing to act on. See NOT_PLANNING_RESULT.
            return ToolOutcome(
                text=NOT_PLANNING_RESULT if plan else NOT_PLANNING_NO_PLAN_RESULT
            )
        if not plan:
            return ToolOutcome(
                text="pass the complete plan as markdown in `plan`", is_error=True
            )
        # Hand the engine one approval request and park (the same MRTR
        # round-trip AskUser rides).
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

    # ── Second leg: the user has answered. ───────────────────────────────────
    #
    # ``is_active()`` is deliberately NOT re-checked as a precondition here.
    # A host that gates the approval itself (the embedding app renders the
    # panel, then syncs its own state) flips the mode OFF before it hands the
    # answer back, so the retry leg used to hit the first leg's "Not in plan
    # mode — nothing to stop. Enter it with Plan." guard: an approved exit was
    # reported to the model as an ERROR whose remedy was to re-enter plan
    # mode. Models did exactly that, and the session re-locked itself the
    # instant the user approved it. The answer decides the outcome; the flag
    # only decides how the outcome is worded.
    raw = str(answers.get("approve", "")).strip()
    if is_approval(raw):
        already_off = not control.is_active()
        control.set_active(False)
        # The state change is the whole point of the call, so it is stated
        # outright: an approval that reads as empty (or merely implied) cost
        # audited sessions whole turns of the model re-checking whether it was
        # allowed to write yet.
        if not already_off:
            return ToolOutcome(text=APPROVED_RESULT)
        # Plan mode was already off when the answer arrived, so the answer did
        # not end it — the host did. Whether a person was behind it is the
        # host's to say (USER_ANSWERED_KEY); unsaid means unattributable.
        return ToolOutcome(
            text=(
                ALREADY_OFF_APPROVED_RESULT
                if _user_answered(answers)
                else ALREADY_OFF_RESULT
            )
        )
    feedback = raw or "(no feedback given)"
    if not control.is_active():
        # Rejected, but the host has already ended plan mode out from under the
        # call (a mode switch while the panel was up). Never claim the mode is
        # still on when it is not — and never silently turn it back on.
        return ToolOutcome(
            text=(
                "The user did NOT approve the plan and answered:\n"
                f"{feedback}\n"
                "Plan mode is no longer active (the host ended it separately), "
                "so this feedback is not a rejection you can revise against in "
                "plan mode. Address it before implementing, and ask the user "
                "how to proceed if it conflicts with the plan."
            )
        )
    return ToolOutcome(
        text=(
            "The user did NOT approve the plan and answered:\n"
            f"{feedback}\n"
            f"PLAN MODE IS STILL ACTIVE — {LEAVES} are still off the "
            "surface. Revise the plan to address that feedback and "
            "present it again with StopPlanning; do not attempt to implement "
            "anything yet."
        )
    )


SPECS = ((PLAN_SPEC, plan_handler), (STOP_SPEC, stop_handler))
