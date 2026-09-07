"""Plan mode must never stay locked once the user has approved leaving it.

The reported failure: a session approves the plan panel and keeps behaving
read-only. Two fluid-side mechanisms produced that.

1. ``stop_handler`` re-checked ``is_active()`` on the MRTR **retry** leg. A
   host that gates the approval itself (the embedding app renders the panel,
   syncs its own state, *then* hands the answer back) has already flipped the
   mode off by the time the answer lands, so the retry leg fell into the first
   leg's guard and reported the approval to the model as the error "Not in plan
   mode — nothing to stop. **Enter it with Plan.**" — an error whose stated
   remedy re-locks the session.

2. ``SetPermissionMode`` assigned ``self.plan_mode`` directly, so a host that
   wrapped ``PlanModeControl`` to persist the mode never heard about a
   chip-driven flip and its idea of the mode diverged from the engine's.

Plus the answer vocabulary itself: a REPL user typing "go ahead" was read as
plan feedback and left read-only with no sign anything had gone wrong.
"""

from __future__ import annotations

import json

from fluid.engine.native import NativeLoop
from fluid.events import (
    QuestionRequest,
    RespondQuestion,
    SetPermissionMode,
    ToolResult,
    TurnComplete,
    UserTurn,
)
from fluid.permissions.engine import PermissionEngine
from fluid.store.base import MemoryStore
from fluid.tools.builtins.plan import (
    ALREADY_OFF_RESULT,
    APPROVED_RESULT,
    PLAN_SPEC,
    STOP_SPEC,
    PlanModeControl,
    is_approval,
    plan_handler,
    stop_handler,
)
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

from tests.unit.test_empty_completion_retry import (
    ScriptedProvider,
    _text,
    _tool_call,
)

# Text the model must never be handed on an APPROVED exit: it is an
# instruction to re-enter the mode it was just released from.
_RE_LOCK_BAIT = "Enter it with Plan"


def _ctx(active: bool) -> tuple[ToolCtx, dict]:
    state = {"active": active}
    ctx = ToolCtx()
    ctx.extra["plan_mode_control"] = PlanModeControl(
        is_active=lambda: state["active"],
        set_active=lambda value: state.__setitem__("active", bool(value)),
    )
    return ctx, state


async def _answer(state_active: bool, answer: str) -> tuple[ToolOutcome, dict]:
    ctx, state = _ctx(active=state_active)
    outcome = await stop_handler(
        {"plan": "the plan", "__input_responses__": {"approve": answer}}, ctx
    )
    return outcome, state


# ═══ 1. The retry leg answers the ANSWER, not the flag ═══════════════════════


async def test_approval_confirms_even_when_the_host_already_left_plan_mode() -> None:
    """The exact app ordering: sync first, answer second."""
    outcome, state = await _answer(state_active=False, answer="approve")
    assert not outcome.is_error
    assert state["active"] is False
    text = outcome.text
    # Round 7 reordered this result to lead with the instruction; what it must
    # still convey is the mode change and the unlock, not a verdict.
    assert text.splitlines()[0].startswith("Plan mode is off")
    assert "proceed with the plan now" in text
    assert "write tools are back on the surface" in text
    assert _RE_LOCK_BAIT not in text


async def test_an_approved_exit_never_tells_the_model_to_re_enter_plan_mode() -> None:
    for answer in ("1", "approve", "yes", "Proceed."):
        for already_off in (False, True):
            outcome, state = await _answer(state_active=not already_off, answer=answer)
            assert not outcome.is_error, (answer, already_off)
            assert state["active"] is False, (answer, already_off)
            assert _RE_LOCK_BAIT not in outcome.text, (answer, already_off)


async def test_approval_leaves_the_flag_off_whichever_way_it_started() -> None:
    for started_active in (True, False):
        _, state = await _answer(state_active=started_active, answer="1")
        assert state["active"] is False


async def test_first_leg_outside_plan_mode_accepts_the_plan() -> None:
    """Nothing has been asked yet and there is nothing to exit.

    Round 7: this leg used to answer with an error whose stated remedy was the
    re-lock bait itself, and an audited session followed it — Plan, then
    StopPlanning one call later, purely to leave a mode it never wanted. Plan
    mode is engine-session state that does not survive a teardown between
    turns, so a plan written before a park lands exactly here.
    """
    ctx, _ = _ctx(active=False)
    outcome = await stop_handler({"plan": "do the thing"}, ctx)
    assert not outcome.is_error
    assert "proceed with the plan now" in outcome.text
    assert _RE_LOCK_BAIT not in outcome.text


async def test_rejection_after_an_external_exit_does_not_claim_plan_mode_is_on() -> None:
    outcome, state = await _answer(state_active=False, answer="use redis instead")
    assert not outcome.is_error
    assert "use redis instead" in outcome.text
    assert "STILL ACTIVE" not in outcome.text
    assert "no longer active" in outcome.text
    # A rejection must never turn plan mode back ON behind the host's back.
    assert state["active"] is False


async def test_rejection_while_still_planning_is_unchanged() -> None:
    outcome, state = await _answer(state_active=True, answer="use redis instead")
    assert state["active"] is True
    assert "STILL ACTIVE" in outcome.text and "NOT approve" in outcome.text


# ═══ 2. The approval vocabulary ══════════════════════════════════════════════


def test_free_text_approvals_are_recognized_through_case_and_punctuation() -> None:
    for text in (
        "1", "y", "Yes", "YES!", "ok", "Sure",
        "Proceed.", "please proceed", "go ahead", "Go for it!",
        "do it", "LGTM", "lgtm.", "Looks good to me",
        "ship it!", "Let's do it", "let’s go", "make it so",
        "approve the plan", "Plan approved", "implement it",
        "exit plan mode",
    ):
        assert is_approval(text), text


def test_anything_else_is_still_feedback() -> None:
    for text in (
        "", "   ", "no", "not yet", "use Postgres instead",
        "yes but split step 2 first", "why that library?",
        "keep planning",
    ):
        assert not is_approval(text), text


async def test_a_typed_approval_actually_ends_plan_mode() -> None:
    """The REPL leg: the user's literal words are the answer."""
    for typed in ("Proceed.", "go ahead", "ship it!", "let's do it"):
        outcome, state = await _answer(state_active=True, answer=typed)
        assert not outcome.is_error and state["active"] is False, typed
        assert outcome.text == APPROVED_RESULT, typed


# ═══ 3. SetPermissionMode goes through the host's controller ═════════════════


def _bare_loop(**kwargs) -> NativeLoop:
    return NativeLoop(
        provider=ScriptedProvider([_text("ok")]), store=MemoryStore(),
        model="m", permissions=PermissionEngine("on_write"), **kwargs,
    )


def _wrap_control(loop: NativeLoop, seen: list, *, boom: bool = False) -> None:
    """Stand in for the app's ``_wire_plan_mode_control`` wrapper."""
    original = loop.ctx.extra["plan_mode_control"]

    def set_active(active: bool) -> None:
        original.set_active(active)
        seen.append(bool(active))
        if boom:
            raise RuntimeError("host persistence exploded")

    loop.ctx.extra["plan_mode_control"] = PlanModeControl(
        is_active=original.is_active, set_active=set_active,
    )


async def test_set_permission_mode_notifies_a_host_wrapped_controller() -> None:
    loop = _bare_loop(plan_mode=False)
    seen: list = []
    _wrap_control(loop, seen)

    await loop.submit(SetPermissionMode(mode="plan"))
    assert loop.plan_mode is True and seen == [True]

    await loop.submit(SetPermissionMode(mode="default"))
    assert loop.plan_mode is False and seen == [True, False]


async def test_set_permission_mode_is_quiet_when_the_mode_does_not_change() -> None:
    loop = _bare_loop(plan_mode=True)
    seen: list = []
    _wrap_control(loop, seen)
    await loop.submit(SetPermissionMode(mode="plan"))
    assert seen == [] and loop.plan_mode is True


async def test_a_host_controller_that_raises_cannot_desync_the_flag() -> None:
    """Host bookkeeping is best-effort; the engine's own flag is not."""
    loop = _bare_loop(plan_mode=True)
    seen: list = []
    _wrap_control(loop, seen, boom=True)
    await loop.submit(SetPermissionMode(mode="default"))
    assert seen == [False]
    assert loop.plan_mode is False


# ═══ 4. The invariant, end to end ════════════════════════════════════════════


async def _write_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_builtin(PLAN_SPEC, plan_handler)
    registry.register_builtin(STOP_SPEC, stop_handler)

    async def _edit(input: dict, ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text="edited")

    registry.register_builtin(
        ToolSpec(
            name="Edit", description="edit a file",
            input_schema={"type": "object", "properties": {}}, risk="write",
        ),
        _edit,
    )
    return registry


async def test_an_approved_exit_unlocks_the_very_next_call_of_the_same_turn() -> None:
    """End to end, in the app's ordering.

    The host answers the parked StopPlanning the way the app does — it syncs
    its own state (flag off) BEFORE submitting the answer. The model must get
    the approval confirmation, not an error, and the next model call of the
    SAME turn must carry the write tools again.
    """
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    provider = ScriptedProvider([
        _tool_call("t1", "StopPlanning", json.dumps({"plan": "1. edit foo"})),
        _tool_call("t2", "Edit", "{}"),
        _text("done"),
    ])
    loop = NativeLoop(
        provider=provider, store=store, model="m", session_id=sid,
        registry=await _write_registry(),
        permissions=PermissionEngine("never_ask"), plan_mode=True,
    )
    # Plan mode really is locked to start with.
    assert "Edit" not in [t["name"] for t in loop._tools_api()]

    await loop.submit(UserTurn(content="design it"))
    async for e in loop.bus.subscribe(sid):
        if isinstance(e, QuestionRequest):
            # The app's ordering: sync app state (which flips the engine flag
            # through the injected control) and only THEN answer.
            loop.ctx.extra["plan_mode_control"].set_active(False)
            await loop.submit(RespondQuestion(request_id=e.request_id, answer="approve"))
        if isinstance(e, TurnComplete):
            break
    await loop.wait_turn()

    results = {r.tool_use_id: r for r in loop.bus.log(sid) if isinstance(r, ToolResult)}
    stop = results["t1"]
    assert not stop.is_error, stop.output
    # The host had already flipped the flag, so this is the "already off"
    # wording — but it still has to READ as an approval that unlocks writing.
    assert stop.output == ALREADY_OFF_RESULT
    assert "proceed with the plan now" in stop.output
    assert "write tools are back on the surface" in stop.output
    assert _RE_LOCK_BAIT not in stop.output

    # The write tool ran, and the request that offered it carried the schema.
    assert "t2" in results and results["t2"].output == "edited"
    assert loop.plan_mode is False
    assert "Edit" in [t["name"] for t in loop._tools_api()]
    assert any(
        "Edit" in [t["name"] for t in (request.tools or [])]
        for request in provider.requests[1:]
    )


async def test_the_same_turn_stays_locked_when_the_plan_is_rejected() -> None:
    """The mirror image — a rejection must not unlock anything."""
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    provider = ScriptedProvider([
        _tool_call("t1", "StopPlanning", json.dumps({"plan": "1. edit foo"})),
        _text("revising"),
    ])
    loop = NativeLoop(
        provider=provider, store=store, model="m", session_id=sid,
        registry=await _write_registry(),
        permissions=PermissionEngine("never_ask"), plan_mode=True,
    )
    await loop.submit(UserTurn(content="design it"))
    async for e in loop.bus.subscribe(sid):
        if isinstance(e, QuestionRequest):
            await loop.submit(
                RespondQuestion(request_id=e.request_id, answer="use redis instead")
            )
        if isinstance(e, TurnComplete):
            break
    await loop.wait_turn()

    assert loop.plan_mode is True
    assert "Edit" not in [t["name"] for t in loop._tools_api()]
