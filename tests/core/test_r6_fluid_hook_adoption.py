"""Round-6 follow-ups: the app adopts the hooks fluid #34 added.

Each hook exists because an audited session went wrong without it:

* Monitor's prose never named the budget the host enforces, so a model wrote a
  3600s watcher against a 1200s backstop — ``monitor_budget_seconds`` now
  rides the ctx and the description.
* SendMessage / ListAgents were advertised in the prompt and billed in the
  context breakdown on a leg that never wired agent messaging — the prompt's
  tool surface is now the WIRED surface.
* Task notifications were steered in the user's marker — they are
  ``origin="system"`` now.
* A provider's reasoning summary and its reasoning text were glued into one
  thinking block — the subtype now rides the stream so the UI can label it.
* grok / GLM / gpt-6 turns cost $0 — the bound catalog's rates reach fluid
  through ``NativeLoop(pricing=)``.
* Under bypassPermissions the model reported "the plan is approved" as if a
  person had said so — the plan controller now says the gate is automatic,
  and a real panel click is attributed as such.
"""

from __future__ import annotations

import dataclasses
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.core.provider import harness_adapter as ha
from app.core.provider.events import StreamEvent
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import PermissionRequestEvent, SessionOptions
from app.core.user_question_handler import FLUID_ANSWER_KEY, HOST_USER_ANSWERED_KEY
from fluid.events import model as lm
from fluid.events.ops import RespondQuestion, Steer
from fluid.tools.builtins import refine_builtin_descriptions, register_builtins
from fluid.tools.builtins.plan import PlanModeControl
from fluid.tools.registry import ToolCtx, ToolRegistry

SID = "ses_r6"


class _Engine:
    def __init__(self, events=()):
        self._events = [dataclasses.replace(e, seq=i + 1) for i, e in enumerate(events)]
        self.ops = []
        self.model = "m"

    async def submit(self, op):
        self.ops.append(op)

    async def events(self, *, since_seq: int = 0):
        for e in self._events:
            if e.seq > since_seq:
                yield e

    @property
    def session_id(self):
        return SID

    async def aclose(self):
        pass


def _adapter(engine, **opts) -> HarnessAdapter:
    a = HarnessAdapter()
    a._engine = engine
    a._connected = True
    a._options = SessionOptions(model="m", **opts)
    return a


async def _collect(adapter, limit=50):
    out = []
    async for ev in adapter.receive_events():
        out.append(ev)
        if len(out) >= limit:
            break
    return out


# ─── steer origin ─────────────────────────────────────────────────────────────


async def test_steer_origin_reaches_the_fluid_op():
    engine = _Engine()
    adapter = _adapter(engine)
    await adapter.steer("user words", id="q1")
    await adapter.steer("<task-notification/>", id="n1", origin="system")
    steers = [op for op in engine.ops if isinstance(op, Steer)]
    assert [s.origin for s in steers] == ["user", "system"]
    assert steers[1].text == "<task-notification/>" and steers[1].id == "n1"


# ─── thinking subtype ─────────────────────────────────────────────────────────


async def test_reasoning_summary_subtype_rides_the_stream_events():
    events = [
        lm.TurnStarted(session_id=SID, model="m", provider="fake"),
        lm.BlockStart(session_id=SID, index=0, block_type="thinking", subtype="reasoning_summary"),
        lm.ThinkingDelta(session_id=SID, index=0, text="summary"),
        lm.BlockStop(session_id=SID, index=0),
        lm.BlockStart(session_id=SID, index=1, block_type="thinking", subtype="reasoning"),
        lm.ThinkingDelta(session_id=SID, index=1, text="the real thing"),
        lm.BlockStop(session_id=SID, index=1),
        lm.BlockStart(session_id=SID, index=2, block_type="thinking"),
        lm.ThinkingDelta(session_id=SID, index=2, text="plain"),
        lm.BlockStop(session_id=SID, index=2),
        lm.TurnComplete(session_id=SID, stop_reason="end_turn", num_model_calls=1),
    ]
    adapter = _adapter(_Engine(events), include_partial_messages=True)
    got = [e for e in await _collect(adapter) if isinstance(e, StreamEvent)]
    starts = [e.event["content_block"] for e in got if e.event["type"] == "content_block_start"]
    deltas = [e.event["delta"] for e in got if e.event["type"] == "content_block_delta"]
    assert starts[0] == {"type": "thinking", "thinking": "", "subtype": "reasoning_summary"}
    assert starts[1] == {"type": "thinking", "thinking": "", "subtype": "reasoning"}
    assert starts[2] == {"type": "thinking", "thinking": ""}  # no distinction drawn
    assert deltas[0] == {"type": "thinking_delta", "thinking": "summary", "subtype": "reasoning_summary"}
    assert deltas[1] == {"type": "thinking_delta", "thinking": "the real thing", "subtype": "reasoning"}
    assert deltas[2] == {"type": "thinking_delta", "thinking": "plain"}
    assert adapter._block_subtypes == {}  # BlockStop cleans up


def test_thinking_row_carries_the_subtype_in_metadata():
    from app.core.db_message_loader import _transform_row
    from app.core.query_persistence import _row_for_thinking

    row = _row_for_thinking("s", {"content": "sum", "subtype": "reasoning_summary"}, "p", "grok")
    assert row["metadata"] == {"model": "grok", "subtype": "reasoning_summary"}
    plain = _row_for_thinking("s", {"content": "t"}, "p", "grok")
    assert plain["metadata"] == {"model": "grok"}

    loaded = _transform_row({
        "id": 1, "session_id": "s", "role": "thinking", "content": "sum",
        "metadata": row["metadata"], "profile_id": "p", "created_at": None,
    })
    assert loaded["type"] == "thinking"
    assert loaded["metadata"]["subtype"] == "reasoning_summary"


# ─── pricing resolver ─────────────────────────────────────────────────────────


def test_catalog_pricing_resolver_scales_per_token_rates_to_per_mtok(monkeypatch):
    monkeypatch.setattr(ha, "_openrouter_pricing", lambda options: {
        "x-ai/grok-4.6": {"prompt": 0.000002, "completion": 0.000006, "input_cache_read": 0.0000005},
        "z-ai/glm-5.3": {"prompt": 0.0000014},
    })
    resolve = ha._catalog_pricing_resolver(SessionOptions(model="m", extra={"provider_id": "pr1"}))
    assert resolve is not None
    grok = resolve("x-ai/grok-4.6")
    assert grok["input"] == pytest.approx(2.0)
    assert grok["output"] == pytest.approx(6.0)
    assert grok["cached_input"] == pytest.approx(0.5)
    assert "cache_write" not in grok
    assert grok["source"] == "provider catalog pr1"
    # Routing tags and case do not defeat the lookup.
    assert resolve("X-AI/grok-4.6:nitro")["input"] == pytest.approx(2.0)
    assert resolve("z-ai/glm-5.3") == {"input": pytest.approx(1.4), "source": "provider catalog pr1"}
    assert resolve("unknown/model") is None


def test_catalog_pricing_resolver_is_none_without_rates(monkeypatch):
    monkeypatch.setattr(ha, "_openrouter_pricing", lambda options: {})
    assert ha._catalog_pricing_resolver(SessionOptions(model="m")) is None


def test_catalog_rates_price_a_turn_through_fluid(monkeypatch):
    from fluid.providers.pricing import estimate_cost

    monkeypatch.setattr(ha, "_openrouter_pricing", lambda options: {
        "grok-3": {"prompt": 0.000003, "completion": 0.000015},
    })
    resolve = ha._catalog_pricing_resolver(SessionOptions(model="m"))
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000}
    # grok-3 is recognized-but-unpriced in fluid's table; the catalog prices it.
    assert estimate_cost("grok-3", usage) is None
    assert estimate_cost("grok-3", usage, resolver=resolve) == pytest.approx(18.0)


# ─── the wired tool surface ───────────────────────────────────────────────────


def test_wired_capabilities_include_what_native_loop_wires_itself():
    ctx = ToolCtx()
    ctx.extra["task_dispatch"] = object()
    wired = ha._wired_capabilities(ctx)
    assert {"task_dispatch", "plan_mode_control", "tool_registry", "history_recall"} <= wired
    assert "agent_message" not in wired


def test_wired_tool_names_drop_builtins_whose_capability_is_missing():
    registry = ToolRegistry()
    register_builtins(registry)
    wired = ha._wired_capabilities(ToolCtx())
    names = ha._wired_tool_names(registry, None, wired)
    assert names is not None
    assert "Read" in names and "Plan" in names and "ToolSearch" in names
    # No agent messaging, no monitor spawner, no task dispatcher on this ctx.
    assert "SendMessage" not in names and "ListAgents" not in names
    assert "Monitor" not in names and "Task" not in names
    # Permission still narrows first.
    only = ha._wired_tool_names(registry, frozenset({"Read", "SendMessage"}), wired)
    assert only == frozenset({"Read"})
    # And a wired capability brings the tool back.
    ctx = ToolCtx()
    ctx.extra["monitor_spawn"] = object()
    assert "Monitor" in ha._wired_tool_names(registry, None, ha._wired_capabilities(ctx))


def test_permit_within_composes_the_predicate():
    assert ha._permit_within(None, None) is None
    inside = ha._permit_within(None, frozenset({"Read"}))
    assert inside("Read") and not inside("SendMessage")
    both = ha._permit_within(lambda n: n != "Read", frozenset({"Read", "Grep"}))
    assert both("Grep") and not both("Read") and not both("Write")


def test_prompt_compile_drops_unwired_tools_from_the_code_mode_catalog():
    """The compiled prompt's tool set is the wired surface, not the registry."""
    registry = ToolRegistry()
    register_builtins(registry)
    seen = {}

    def _fake_compile(inputs):
        seen["tools"] = inputs.tools
        return SimpleNamespace(sections=(), text="")

    with patch("fluid.context.compiler.compile_prompt", _fake_compile):
        HarnessAdapter._compile_prompt_sections(
            "base", None, SessionOptions(model="m"),
            registry=registry, wired=ha._wired_capabilities(ToolCtx()),
        )
    assert seen["tools"] is not None
    assert "Read" in seen["tools"] and "SendMessage" not in seen["tools"]


# ─── Monitor budget ───────────────────────────────────────────────────────────


def test_monitor_budget_reads_the_setting(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "monitor_command_timeout_seconds", 1200)
    assert ha._monitor_budget_seconds() == 1200
    monkeypatch.setattr(settings, "monitor_command_timeout_seconds", 0)
    assert ha._monitor_budget_seconds() is None


def test_monitor_description_names_the_enforced_budget(monkeypatch):
    from app.core.config import settings

    monkeypatch.setattr(settings, "monitor_command_timeout_seconds", 1200)
    registry = ToolRegistry()
    register_builtins(registry)
    refine_builtin_descriptions(
        registry, allowed=None, disallowed=(),
        monitor_budget_seconds=ha._monitor_budget_seconds(),
    )
    text = registry.get("Monitor").description
    assert "1200 seconds" in text
    assert "bound your command below that" in text


# ─── plan gate honesty ────────────────────────────────────────────────────────


def _engine_with_plan_control():
    flag = {"on": False}
    engine = _Engine()
    engine.ctx = ToolCtx()
    engine.ctx.extra["plan_mode_control"] = PlanModeControl(
        is_active=lambda: flag["on"],
        set_active=lambda v: flag.__setitem__("on", bool(v)),
    )
    engine.plan_mode = False
    engine.permissions = SimpleNamespace(approval_policy="never_ask")
    return engine, flag


async def test_plan_controller_says_the_gate_is_automatic_under_bypass():
    engine, flag = _engine_with_plan_control()
    adapter = _adapter(engine, permission_mode="bypassPermissions")
    adapter._wire_plan_mode_control(SessionOptions(model="m", permission_mode="bypassPermissions"))
    control = engine.ctx.extra["plan_mode_control"]
    assert control.auto_approve is True
    # The wrapped callables still drive the engine flag.
    control.set_active(True)
    assert flag["on"] is True and control.is_active() is True

    # Leaving bypass through the chip re-stamps the flag; the callables stay.
    async def _no_native(op):
        raise NotImplementedError

    engine.submit = _no_native
    await adapter.set_permission_mode("default")
    refreshed = engine.ctx.extra["plan_mode_control"]
    assert refreshed.auto_approve is False
    assert refreshed.is_active is control.is_active


async def test_plan_controller_is_honest_by_default():
    engine, _ = _engine_with_plan_control()
    adapter = _adapter(engine, permission_mode="default")
    adapter._wire_plan_mode_control(SessionOptions(model="m", permission_mode="default"))
    assert engine.ctx.extra["plan_mode_control"].auto_approve is False


class PermissionResultAllow:
    """The adapter recognises the allow result by its class NAME."""

    def __init__(self, updated_input):
        self.updated_input = updated_input


_Allow = PermissionResultAllow


async def test_a_real_panel_answer_is_attributed_to_the_user():
    engine = _Engine()
    adapter = _adapter(engine)

    async def can_use_tool(name, tool_input, ctx):
        return _Allow({**tool_input, FLUID_ANSWER_KEY: "approve", HOST_USER_ANSWERED_KEY: True})

    adapter._can_use_tool = can_use_tool
    ev = PermissionRequestEvent(
        tool_use_id="tu1", name="StopPlanning", input={"plan": "p"}, request_id="q1",
    )
    await adapter._bridge_question(ev)
    answers = [op for op in engine.ops if isinstance(op, RespondQuestion)]
    assert len(answers) == 1
    assert answers[0].answer == "approve" and answers[0].user_answered is True


async def test_a_policy_answer_is_not_attributed():
    engine = _Engine()
    adapter = _adapter(engine)

    async def can_use_tool(name, tool_input, ctx):
        return _Allow({**tool_input, FLUID_ANSWER_KEY: "approve"})

    adapter._can_use_tool = can_use_tool
    ev = PermissionRequestEvent(
        tool_use_id="tu2", name="StopPlanning", input={"plan": "p"}, request_id="q2",
    )
    await adapter._bridge_question(ev)
    answers = [op for op in engine.ops if isinstance(op, RespondQuestion)]
    assert answers[0].answer == "approve" and answers[0].user_answered is False
