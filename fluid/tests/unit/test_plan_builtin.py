"""Plan / StopPlanning — model-initiated plan mode with approval-gated exit."""

from __future__ import annotations

from fluid.tools.builtins.plan import (
    APPROVED_RESULT,
    PLAN_SPEC,
    STOP_SPEC,
    USER_ANSWERED_KEY,
    PlanModeControl,
    plan_handler,
    stop_handler,
)
from fluid.tools.registry import ToolCtx


def _ctx(active: bool = False) -> tuple[ToolCtx, dict]:
    state = {"active": active}
    ctx = ToolCtx()
    ctx.extra["plan_mode_control"] = PlanModeControl(
        is_active=lambda: state["active"],
        set_active=lambda value: state.__setitem__("active", value),
    )
    return ctx, state


# ─── Shape ───────────────────────────────────────────────────────────────────


def test_specs_are_capability_gated_and_safe() -> None:
    # Both tools ride the plan-mode controller; without one (delegated leg,
    # plain hosts) the engine must drop them from the surface. risk="safe"
    # keeps them inside plan mode's read-only schema.
    for spec in (PLAN_SPEC, STOP_SPEC):
        assert spec.requires_extra == ("plan_mode_control",)
        assert spec.risk == "safe"


# ─── Plan (enter) ────────────────────────────────────────────────────────────


async def test_plan_enters_plan_mode() -> None:
    ctx, state = _ctx()
    outcome = await plan_handler({}, ctx)
    assert not outcome.is_error
    assert state["active"] is True
    assert "Plan mode is on" in outcome.text


async def test_plan_without_control_is_unavailable() -> None:
    outcome = await plan_handler({}, ToolCtx())
    assert outcome.is_error
    assert "not available" in outcome.text


async def test_plan_twice_errors_but_stays_active() -> None:
    ctx, state = _ctx(active=True)
    outcome = await plan_handler({}, ctx)
    assert outcome.is_error
    assert state["active"] is True


# ─── StopPlanning (approval-gated exit) ──────────────────────────────────────


async def test_stop_outside_plan_mode_errors() -> None:
    ctx, _ = _ctx(active=False)
    outcome = await stop_handler({"plan": "do the thing"}, ctx)
    assert outcome.is_error


async def test_stop_requires_a_plan() -> None:
    ctx, state = _ctx(active=True)
    outcome = await stop_handler({"plan": "  "}, ctx)
    assert outcome.is_error
    assert state["active"] is True


async def test_stop_first_leg_parks_on_input_required() -> None:
    ctx, state = _ctx(active=True)
    outcome = await stop_handler({"plan": "1. edit foo.py\n2. run tests"}, ctx)
    assert outcome.input_required is not None
    assert outcome.input_required.tool == "StopPlanning"
    request = outcome.input_required.requests["approve"]
    # The user reviews exactly the plan text; nothing is decided yet.
    assert "edit foo.py" in request["message"]
    assert state["active"] is True


async def test_stop_approval_exits_plan_mode() -> None:
    ctx, state = _ctx(active=True)
    outcome = await stop_handler(
        {"plan": "the plan", "__input_responses__": {"approve": "1"}}, ctx
    )
    assert not outcome.is_error
    assert state["active"] is False
    assert "approved" in outcome.text.lower()


async def test_stop_rejection_keeps_planning_with_feedback() -> None:
    ctx, state = _ctx(active=True)
    outcome = await stop_handler(
        {
            "plan": "the plan",
            "__input_responses__": {"approve": "use redis instead"},
        },
        ctx,
    )
    assert not outcome.is_error
    assert state["active"] is True
    assert "use redis instead" in outcome.text
    assert "NOT approve" in outcome.text


# ─── honesty about who approved ──────────────────────────────────────────────
#
# Under bypass permissions the host ends plan mode itself and hands the answer
# back already approved. Nobody read the plan — and models relayed "the plan is
# approved" to users as though somebody had.


def _auto_ctx(active: bool = False) -> tuple[ToolCtx, dict]:
    state = {"active": active}
    ctx = ToolCtx()
    ctx.extra["plan_mode_control"] = PlanModeControl(
        is_active=lambda: state["active"],
        set_active=lambda value: state.__setitem__("active", value),
        auto_approve=True,
    )
    return ctx, state


async def test_entry_text_promises_a_human_only_when_there_is_one() -> None:
    ctx, _ = _ctx()
    human = await plan_handler({}, ctx)
    assert "for the user's approval" in human.text

    auto_ctx, auto_state = _auto_ctx()
    auto = await plan_handler({}, auto_ctx)
    assert auto_state["active"] is True
    assert "for the user's approval" not in auto.text
    assert "approves automatically" in auto.text
    assert "do not report it back as user-approved" in auto.text


async def test_already_off_exit_does_not_claim_a_user_approved() -> None:
    ctx, _ = _ctx(active=False)
    outcome = await stop_handler(
        {"plan": "p", "__input_responses__": {"approve": "1"}}, ctx
    )
    assert not outcome.is_error
    assert not outcome.text.startswith("Plan approved")
    assert "ended it by policy" in outcome.text
    assert "No user review of this plan was recorded" in outcome.text
    # The actionable half survives: the tools are back.
    assert "write tools are on the surface" in outcome.text


async def test_a_host_that_saw_a_real_answer_gets_the_approved_wording() -> None:
    ctx, _ = _ctx(active=False)
    outcome = await stop_handler(
        {
            "plan": "p",
            "__input_responses__": {
                "approve": "1",
                USER_ANSWERED_KEY: True,
            },
        },
        ctx,
    )
    assert outcome.text.startswith("Plan approved by the user")
    assert "No user review" not in outcome.text


async def test_a_live_exit_is_unaffected_by_the_marker() -> None:
    # Plan mode was still on, so the answer itself ended it — attribution is
    # not in doubt and the marker changes nothing.
    ctx, state = _ctx(active=True)
    outcome = await stop_handler(
        {"plan": "p", "__input_responses__": {"approve": "go ahead"}}, ctx
    )
    assert state["active"] is False
    assert outcome.text == APPROVED_RESULT
