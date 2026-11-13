"""The ACP bridge's deferral: a small loaded core, the rest behind CallTool.

ACP agents (Google's ``agy acp``) have no deferral of their own. With
``AcpLoop(deferred=...)`` the bundled bridge lists the core with schemas plus
``LoadTools`` (every other tool announced by name) and ``CallTool`` (runs one
by its real name). These drive the real AcpLoop and bridge socket end to end
against the scripted agent in tests/fixtures/fake_acp_agent.py.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fluid.engine.acp import loop as acp_loop
from fluid.events.model import PermissionRequest, QuestionRequest, ToolUse, TurnComplete
from fluid.events.ops import Interrupt, RespondPermission, RespondQuestion, UserTurn
from fluid.hooks.bus import PRE_TOOL_USE, HookBus, HookDecision
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.mcp.bundled_server import _answer, _socket_rpc, serve_registry_socket
from fluid.tools.mcp.call_through import CALL_TOOL, LOAD_TOOL, catalog_rows, unwrap_call
from fluid.tools.mrtr import INTERRUPTED_TEXT, unresolved_notice
from fluid.tools.registry import (
    InputRequired,
    ToolCtx,
    ToolOutcome,
    ToolRegistry,
    ToolSpec,
)

from tests.unit.test_acp_engine import TERMINAL, _driver

SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}}


def _registry(ran: list[tuple[str, dict]]) -> ToolRegistry:
    registry = ToolRegistry()

    def handler(name: str):
        async def run(args: dict, ctx: ToolCtx) -> ToolOutcome:
            ran.append((name, args))
            return ToolOutcome(text=f"{name} ran {args.get('text')}")
        return run

    for name in ("Core", "Echo", "Hidden"):
        registry.register_builtin(ToolSpec(name=name, description=f"{name} tool", input_schema=SCHEMA),
                                  handler(name))
    paint_run = handler("mcp__media__paint")

    @tool("paint", "paint a picture", SCHEMA)
    async def paint(args: dict) -> dict:
        outcome = await paint_run(args, ToolCtx())
        return {"content": [{"type": "text", "text": outcome.text}]}

    # A tool the registry itself keeps deferred is served behind CallTool too.
    registry.register_inproc(create_inproc_server("media", [paint]), defer=True)
    return registry


async def _deferred_driver(tmp_path: Path, ran: list, **kw: Any):
    return await _driver(
        tmp_path, registry=_registry(ran), ctx=ToolCtx(cwd=str(tmp_path)),
        tool_filter=lambda name: name != "Hidden", deferred=lambda name: name != "Core", **kw,
    )


def _served_list(tmp_path: Path) -> list[dict]:
    return json.loads((tmp_path / "tools_list.json").read_text())


async def test_the_agent_is_served_the_core_and_the_rest_by_name(tmp_path: Path) -> None:
    d = await _deferred_driver(tmp_path, [])
    try:
        end = await d.turn("mcp __list__ {}", timeout=60)
        assert isinstance(end, TurnComplete), end
        tools = {t["name"]: t for t in _served_list(tmp_path)}
        assert list(tools) == ["Core", LOAD_TOOL, CALL_TOOL]
        assert tools["Core"]["inputSchema"] == SCHEMA
        catalog = tools[LOAD_TOOL]["description"]
        # Announced by name, grouped by server; the filtered tool is not.
        assert "core (<tool>): Echo" in catalog
        assert "media (mcp__media__<tool>): paint" in catalog
        assert "Hidden" not in catalog and "paint a picture" not in catalog
        assert d.loop.bridged_tools() == ["Core", LOAD_TOOL, CALL_TOOL]
        assert d.loop.deferred_tools() == ["Echo", "mcp__media__paint"]
    finally:
        await d.close()


async def test_load_tools_returns_schemas_without_asking(tmp_path: Path) -> None:
    d = await _deferred_driver(tmp_path, [])
    try:
        await d.turn('mcpask LoadTools {"names": ["mcp__media__paint", "Hidden"]}', timeout=60)
        assert not [e for e in d.events if isinstance(e, PermissionRequest)]
        text = d.text()
        assert '"name": "mcp__media__paint"' in text and '"input_schema"' in text
        # A tool outside the session's filter reads as one that does not exist.
        assert "Not found: Hidden" in text
        await d.turn('mcp LoadTools {"query": "picture"}', timeout=60)
        assert d.text().count('"name": "mcp__media__paint"') == 2
    finally:
        await d.close()


async def test_call_tool_runs_the_real_tool_through_policy_card_and_hooks(tmp_path: Path) -> None:
    ran: list = []
    policy_saw: list = []
    hook_saw: list = []
    hooks = HookBus()

    async def pre(event: str, payload: dict) -> HookDecision:
        hook_saw.append((payload["name"], payload["input"]))
        return HookDecision()

    hooks.on(PRE_TOOL_USE, pre)

    async def policy(name: str, data: dict) -> str | None:
        policy_saw.append((name, data))
        return None

    d = await _deferred_driver(tmp_path, ran, hooks=hooks, tool_policy=policy)
    try:
        await d.loop.submit(UserTurn(
            content='mcpask CallTool {"name": "mcp__media__paint", "input": {"text": "cat"}}'))
        ask = await d.until(PermissionRequest)
        # The owner's card and saved rules see the real tool and its input.
        assert ask.name == "mcp__fluid_bundled__mcp__media__paint"
        assert ask.input == {"text": "cat"}
        assert policy_saw == [("mcp__fluid_bundled__mcp__media__paint", {"text": "cat"})]
        await d.loop.submit(RespondPermission(request_id=ask.request_id, decision="allow"))
        end = await d.until(TERMINAL, timeout=60)
        assert isinstance(end, TurnComplete), end
        assert ran == [("mcp__media__paint", {"text": "cat"})]
        assert hook_saw == [("mcp__media__paint", {"text": "cat"})]
        assert "mcp said: mcp__media__paint ran cat" in d.text()
        uses = [e for e in d.events if isinstance(e, ToolUse)]
        assert {u.name for u in uses} == {"mcp__fluid_bundled__mcp__media__paint"}

        # "Always" on the card allows a CallTool call once: the agent must
        # not remember CallTool itself. A directly listed tool keeps it.
        assert "chose=allow " in d.text()
        await d.loop.submit(UserTurn(content='mcpask CallTool {"name": "Echo", "input": {"text": "a"}}'))
        ask = await d.until(PermissionRequest)
        await d.loop.submit(RespondPermission(request_id=ask.request_id, decision="allow_always"))
        await d.until(TERMINAL, timeout=60)
        assert d.text().count("chose=allow ") == 2 and "chose=always" not in d.text()
        assert ran[-1] == ("Echo", {"text": "a"})
        await d.loop.submit(UserTurn(content='mcpask Core {"text": "b"}'))
        ask = await d.until(PermissionRequest)
        assert ask.name == "mcp__fluid_bundled__Core"
        await d.loop.submit(RespondPermission(request_id=ask.request_id, decision="allow_always"))
        await d.until(TERMINAL, timeout=60)
        assert "chose=always" in d.text()
        del ran[1:]

        # Denied on the card: nothing runs.
        await d.loop.submit(UserTurn(content='mcpask CallTool {"name": "Echo", "input": {"text": "x"}}'))
        ask = await d.until(PermissionRequest)
        assert ask.name == "mcp__fluid_bundled__Echo"
        await d.loop.submit(RespondPermission(request_id=ask.request_id, decision="deny"))
        await d.until(TERMINAL, timeout=60)
        assert ran == [("mcp__media__paint", {"text": "cat"})]
        assert d.text().endswith("mcp denied")
    finally:
        await d.close()


async def test_call_tool_refuses_a_tool_outside_the_filter(tmp_path: Path) -> None:
    ran: list = []
    d = await _deferred_driver(tmp_path, ran)
    try:
        await d.loop.submit(UserTurn(content='mcpask CallTool {"name": "Hidden", "input": {"text": "x"}}'))
        ask = await d.until(PermissionRequest)
        # Not a served tool: the card stays on the proxy, never the hidden name.
        assert ask.name == "mcp__fluid_bundled__CallTool"
        await d.loop.submit(RespondPermission(request_id=ask.request_id, decision="allow"))
        await d.until(TERMINAL, timeout=60)
        assert ran == []
        assert "tool not available in this session: Hidden" in d.text()
    finally:
        await d.close()


async def test_without_deferral_the_bridge_lists_every_tool(tmp_path: Path) -> None:
    d = await _driver(tmp_path, registry=_registry([]), ctx=ToolCtx(cwd=str(tmp_path)),
                      tool_filter=lambda name: name != "Hidden")
    try:
        await d.turn("mcp __list__ {}", timeout=60)
        # The registry's own deferred tier stays off an eager bridge, as before.
        assert [t["name"] for t in _served_list(tmp_path)] == ["Core", "Echo"]
        assert d.loop.deferred_tools() == []
    finally:
        await d.close()


def test_unwrap_and_catalog_helpers() -> None:
    assert unwrap_call({"name": "mcp__fluid_bundled__Echo", "input": '{"a": 1}'}) == ("Echo", {"a": 1})
    assert unwrap_call({"name": "Echo", "input": "nope"}) == ("Echo", {})
    assert unwrap_call(None) == ("", {})
    assert catalog_rows(["mcp__b__y", "Zed", "mcp__b__x", "mcp__a__q"]) == [
        "core (<tool>): Zed", "a (mcp__a__<tool>): q", "b (mcp__b__<tool>): x, y",
    ]


# ─── input_required through CallTool ─────────────────────────────────────────


def _asking_registry(ran: list) -> ToolRegistry:
    """``Ask`` pauses once on input_required, then finishes with the answer;
    ``Nag`` never stops asking."""
    registry = ToolRegistry()

    def asking(name: str, forever: bool):
        async def run(args: dict, ctx: ToolCtx) -> ToolOutcome:
            ran.append((name, args))
            answers = args.get("__input_responses__")
            if answers is not None and not forever:
                return ToolOutcome(text=f"{name} got {answers['colour']}")
            return ToolOutcome(text="", input_required=InputRequired(
                server="", tool=name, request_state="s1",
                requests={"colour": {"method": "elicitation/create", "message": "Which colour?",
                                     "schema": {"type": "string"}}},
            ))
        return run

    registry.register_builtin(ToolSpec(name="Core", description="core", input_schema=SCHEMA),
                              asking("Core", False))
    for name, forever in (("Ask", False), ("Nag", True)):
        registry.register_builtin(ToolSpec(name=name, description=name, input_schema=SCHEMA),
                                  asking(name, forever))
    return registry


async def _asking_driver(tmp_path: Path, ran: list):
    return await _driver(tmp_path, registry=_asking_registry(ran), ctx=ToolCtx(cwd=str(tmp_path)),
                         deferred=lambda name: name != "Core")


async def test_a_bridged_tool_that_asks_finishes_with_the_owners_answer(tmp_path: Path) -> None:
    ran: list = []
    d = await _asking_driver(tmp_path, ran)
    try:
        await d.loop.submit(UserTurn(content='mcp CallTool {"name": "Ask", "input": {"text": "x"}}'))
        question = await d.until(QuestionRequest)
        # Posted as AskUser (hosts answer only that name), naming the tool.
        assert question.message == "Ask asks: Which colour?" and question.name == "AskUser"
        assert question.schema == {"type": "string"}
        await d.loop.submit(RespondQuestion(request_id=question.request_id, answer="blue"))
        end = await d.until(TERMINAL, timeout=60)
        assert isinstance(end, TurnComplete), end
        assert "mcp said: Ask got blue" in d.text()
        # The same call re-run with the answers, as the native loop does.
        assert ran == [("Ask", {"text": "x"}), ("Ask", {"text": "x", "__input_responses__": {"colour": "blue"}})]
        assert d.loop._pending_questions == {}
    finally:
        await d.close()


async def test_interrupt_while_a_bridged_tool_asks_ends_the_round_trip(tmp_path: Path) -> None:
    ran: list = []
    d = await _asking_driver(tmp_path, ran)
    try:
        await d.loop.submit(UserTurn(content='mcp CallTool {"name": "Ask", "input": {}}'))
        await d.until(QuestionRequest)
        await d.loop.submit(Interrupt())
        await d.until(TERMINAL, timeout=60)
        assert len(ran) == 1  # never re-run without an answer
        assert d.loop._pending_questions == {}
    finally:
        await d.close()


async def test_an_unanswered_question_times_out_as_an_interrupted_round_trip(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(acp_loop, "QUESTION_TIMEOUT", 0.2)
    ran: list = []
    d = await _asking_driver(tmp_path, ran)
    try:
        end = await d.turn('mcp CallTool {"name": "Ask", "input": {}}', timeout=60)
        assert isinstance(end, TurnComplete), end
        assert f"mcp said: {INTERRUPTED_TEXT}" in d.text()
        assert len(ran) == 1 and d.loop._pending_questions == {}
    finally:
        await d.close()


async def test_a_tool_that_never_stops_asking_is_cut_off(tmp_path: Path) -> None:
    ran: list = []
    d = await _asking_driver(tmp_path, ran)
    try:
        await d.loop.submit(UserTurn(content='mcp CallTool {"name": "Nag", "input": {}}'))
        while True:
            event = await d.until((QuestionRequest, *TERMINAL), timeout=60)
            if not isinstance(event, QuestionRequest):
                break
            await d.loop.submit(RespondQuestion(request_id=event.request_id, answer="red"))
        assert "still input_required after 3 round-trips" in d.text()
        assert len(ran) == 4
    finally:
        await d.close()


async def test_without_a_user_the_bridge_keeps_the_old_notice() -> None:
    """The delegated CLI's bridge (no ``on_input_required``): unchanged."""
    reply = await _answer(_asking_registry([]), ToolCtx(), {"op": "call", "name": "Ask", "arguments": {}})
    assert reply == {"text": unresolved_notice(
        "Ask", "Ask the user directly in your reply and wait for them, or "
               "proceed without the answer and say what you assumed."), "is_error": True}


async def test_two_calls_can_ask_at_once() -> None:
    """Each bridge request has its own connection: one parked question does
    not hold up another call's."""
    ran: list = []
    pending: dict[str, asyncio.Future] = {}

    async def collect(name: str, marker: InputRequired) -> dict | None:
        pending[name] = asyncio.get_running_loop().create_future()
        return {"colour": await pending[name]}

    server = await serve_registry_socket(_asking_registry(ran), "tcp:127.0.0.1:0", ToolCtx(),
                                         on_input_required=collect)
    address = f"tcp:127.0.0.1:{server.sockets[0].getsockname()[1]}"
    try:
        calls = [asyncio.create_task(_socket_rpc(address, {"op": "call", "name": n, "arguments": {}}))
                 for n in ("Ask", "Core")]
        while len(pending) < 2:
            await asyncio.sleep(0.01)
        pending["Core"].set_result("green")
        assert (await asyncio.wait_for(calls[1], 10))["text"] == "Core got green"
        assert not calls[0].done()
        pending["Ask"].set_result("blue")
        assert (await asyncio.wait_for(calls[0], 10))["text"] == "Ask got blue"
    finally:
        server.close()
        await server.wait_closed()
