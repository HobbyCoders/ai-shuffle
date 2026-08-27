"""Audit fix: leaving plan mode has to be legible to the model.

Two audited sessions spent whole turns re-checking whether they were allowed to
write yet. The approval leg now says the state changed in as many words, the
rejection leg says plan mode is still on, and the per-turn reminder — which is
written into the user node before the turn runs, while approval lands in the
middle of it — stops being sent the moment the flag flips.
"""

from __future__ import annotations

import json

from fluid.engine.native import PLAN_MODE_REMINDER_MARK, NativeLoop
from fluid.events import UserTurn
from fluid.messages import Message, Text
from fluid.permissions.engine import PermissionEngine
from fluid.store.base import MemoryStore
from fluid.tools.builtins.plan import PlanModeControl, stop_handler
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolCtx, ToolRegistry

from tests.unit.test_empty_completion_retry import (
    ScriptedProvider,
    _text,
    _tool_call,
)


def _ctx(active: bool) -> tuple[ToolCtx, dict]:
    state = {"active": active}
    ctx = ToolCtx()
    ctx.extra["plan_mode_control"] = PlanModeControl(
        is_active=lambda: state["active"],
        set_active=lambda value: state.__setitem__("active", value),
    )
    return ctx, state


# ─── StopPlanning result text ────────────────────────────────────────────────


async def test_approval_states_the_mode_change_and_what_it_unlocks() -> None:
    ctx, state = _ctx(active=True)
    outcome = await stop_handler(
        {"plan": "the plan", "__input_responses__": {"approve": "1"}}, ctx
    )
    assert not outcome.is_error and state["active"] is False
    text = outcome.text
    assert text.strip()  # never "(no output)"
    assert "approved" in text.lower()
    assert "OFF" in text  # the mode change, stated
    assert "write" in text.lower()  # what is now available
    assert "execute" in text.lower()  # what to do next


async def test_keep_planning_says_plan_mode_is_still_active() -> None:
    ctx, state = _ctx(active=True)
    outcome = await stop_handler(
        {
            "plan": "the plan",
            "__input_responses__": {"approve": "use redis instead"},
        },
        ctx,
    )
    assert state["active"] is True
    assert "STILL ACTIVE" in outcome.text
    assert "use redis instead" in outcome.text  # the feedback, verbatim
    assert "NOT approve" in outcome.text


async def test_keep_planning_without_feedback_still_names_the_state() -> None:
    ctx, state = _ctx(active=True)
    outcome = await stop_handler(
        {"plan": "the plan", "__input_responses__": {"approve": ""}}, ctx
    )
    assert state["active"] is True
    assert "STILL ACTIVE" in outcome.text and "no feedback given" in outcome.text


# ─── The per-turn reminder follows the live flag ─────────────────────────────


def _loop(**kwargs) -> NativeLoop:
    return NativeLoop(
        provider=ScriptedProvider([_text("ok")]), store=MemoryStore(),
        model="m", **kwargs,
    )


def test_reminder_reflects_the_flag_at_the_moment_it_is_built() -> None:
    loop = _loop(plan_mode=True)
    assert PLAN_MODE_REMINDER_MARK in (loop._plan_mode_reminder() or "")
    loop._set_plan_mode(False)
    assert loop._plan_mode_reminder() is None
    loop._set_plan_mode(True)
    assert loop._plan_mode_reminder() is not None


def test_stale_reminder_is_dropped_once_the_flag_is_off() -> None:
    loop = _loop(plan_mode=True)
    reminder = loop._plan_mode_reminder() or ""
    history = [
        Message(role="user", content=[Text("build the thing"), Text(reminder)]),
        Message(role="assistant", content=[Text("planning")]),
    ]
    # While it is still on, the reminder rides along untouched.
    assert loop._without_stale_plan_reminder(history) is history
    loop._set_plan_mode(False)
    kept = loop._without_stale_plan_reminder(history)
    assert [b.text for b in kept[0].content] == ["build the thing"]
    assert kept[1] is history[1]
    assert history[0].content[1].text == reminder  # the stored node is untouched


def test_a_reminder_only_message_is_left_alone() -> None:
    # Emptying a message's content would break the request shape; the guard
    # keeps such a message whole rather than sending nothing.
    loop = _loop(plan_mode=False)
    history = [Message(role="user", content=[Text(f"{PLAN_MODE_REMINDER_MARK} ...")])]
    assert loop._without_stale_plan_reminder(history) is history


def _flip_server(loop_box: dict):
    """A tool that leaves plan mode mid-turn, the way StopPlanning's approval does."""

    @tool("approve", "Leave plan mode", {"type": "object", "properties": {}})
    async def approve(args: dict) -> dict:
        loop_box["loop"]._set_plan_mode(False)
        return {"content": [{"type": "text", "text": "approved"}]}

    return create_inproc_server("planflip", [approve])


async def test_calls_after_a_mid_turn_approval_do_not_carry_the_reminder() -> None:
    # The audited shape: the reminder is written into the user node when the
    # turn starts, approval lands several calls later, and every remaining
    # call of that same turn used to re-read "Plan mode is active".
    box: dict = {}
    registry = ToolRegistry()
    registry.register_inproc(_flip_server(box))
    provider = ScriptedProvider([
        _tool_call("t1", "mcp__planflip__approve", json.dumps({})),
        _text("implementing now"),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="m", registry=registry,
        permissions=PermissionEngine("never_ask"), plan_mode=True,
    )
    box["loop"] = loop
    await loop.submit(UserTurn(content="design the feature"))
    await loop.wait_turn()

    def _carries_reminder(request) -> bool:
        return any(
            isinstance(block, Text) and PLAN_MODE_REMINDER_MARK in block.text
            for message in request.messages
            for block in message.content
        )

    assert loop.plan_mode is False
    assert _carries_reminder(provider.requests[0])  # still planning
    assert not _carries_reminder(provider.requests[1])  # approved mid-turn
