"""Plan / StopPlanning — model-initiated plan mode with approval-gated exit."""

from __future__ import annotations

from fluid.tools.builtins.plan import (
    PLAN_SPEC,
    STOP_SPEC,
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
