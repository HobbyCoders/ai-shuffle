"""Desktop and browser tool results on their way from the tool to the model.

Covers the app's half of the pipeline: the in-proc computer-use server bridged
onto fluid's registry (timer cancellation, image blocks in the provider
request), the native browser proxy's server instructions reaching the prompt,
and the repeat guard for screen tools.
"""

from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import pytest
from fluid.messages import Image, Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderRequest
from fluid.tools.registry import ToolRegistry

from app.core.provider.fluid_tool_bridge import register_recorded_sdk_servers
from app.core.provider.sdk_tool_records import record_sdk_server_tools

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-screen").decode("ascii")


class _Instance:
    def call_tool(self):
        return lambda fn: fn


def _computer_use(registry: ToolRegistry, **handlers) -> None:
    tools = [
        SimpleNamespace(
            name=name, description=f"{name} tool",
            input_schema={"type": "object", "properties": {}}, handler=handler,
        )
        for name, handler in handlers.items()
    ]
    cfg = {"type": "sdk", "name": "computer-use", "instance": _Instance()}
    record_sdk_server_tools(cfg, tools)
    assert register_recorded_sdk_servers(registry, {"computer-use": cfg}) == ["computer-use"]


def _slow_action():
    state = {"cancelled": False, "finished": False}
    release = asyncio.Event()

    async def type_text(args: dict) -> dict:
        try:
            await release.wait()
        except asyncio.CancelledError:
            # The real handlers latch the session halted here.
            state["cancelled"] = True
            raise
        state["finished"] = True
        return {"content": [{"type": "text", "text": '{"success": true}'}]}

    return type_text, release, state


async def test_a_desktop_call_past_the_hang_guard_is_answered_not_cancelled():
    registry = ToolRegistry(external_call_timeout=0.05)
    handler, release, state = _slow_action()
    _computer_use(registry, type=handler)

    outcome = await registry.dispatch("mcp__computer-use__type", {"text": "hello"})

    assert outcome.is_error
    assert "still running" in outcome.text and "do not repeat it" in outcome.text
    # The same code vocabulary as the desktop tools' own errors.
    body = json.loads(outcome.text)
    assert body["code"] == "EXECUTION_OUTCOME_UNKNOWN" and body["outcome"] == "unknown"
    from app.mcp.computer_use import errors
    assert body["code"] in errors.__doc__
    assert not state["cancelled"]
    release.set()
    await asyncio.sleep(0.01)
    assert state["finished"] and not state["cancelled"]


async def test_cancelling_the_dispatch_cancels_the_desktop_action():
    # A turn interrupt or steering cancels the dispatch. The action must not
    # keep typing after its call was reported cancelled: the handler's own
    # cancel path runs (abort.cancel_inflight, which does not halt the
    # session; a real Stop halts it separately with its reason).
    registry = ToolRegistry(external_call_timeout=30)
    handler, _release, state = _slow_action()
    _computer_use(registry, type=handler)

    task = asyncio.create_task(registry.dispatch("mcp__computer-use__type", {"text": "x"}))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0.01)
    assert state["cancelled"] and not state["finished"]


async def test_a_fast_desktop_call_keeps_its_result_and_error_flag():
    async def bad_click(args: dict) -> dict:
        return {"isError": True, "content": [
            {"type": "text", "text": '{"success": false, "error": "no screenshot"}'}]}

    registry = ToolRegistry(external_call_timeout=30)
    _computer_use(registry, left_click=bad_click)
    outcome = await registry.dispatch("mcp__computer-use__left_click", {})
    assert outcome.is_error and '"no screenshot"' in outcome.text


def _bodies(block: ToolResultBlock) -> dict:
    from fluid.providers.anthropic.serialize import build_request_body as anthropic
    from fluid.providers.openai_responses.serialize import (
        build_request_body as responses,
    )
    from fluid.providers.openrouter.serialize import build_request_body as openrouter

    messages = [
        Message("user", [Text("look")]),
        Message("assistant", [ToolUseBlock(id="call_1", name="mcp__computer-use__screenshot",
                                           input={})]),
        Message("user", [block]),
    ]
    return {
        "anthropic": anthropic(ProviderRequest(model="claude-sonnet-4-5", messages=messages)),
        "responses": responses(ProviderRequest(model="gpt-5", messages=messages)),
        "openrouter": openrouter(ProviderRequest(model="vendor/m", messages=messages)),
    }


async def test_a_bridged_screenshot_is_an_image_part_in_every_dialect():
    async def screenshot(args: dict) -> dict:
        return {"content": [
            {"type": "text", "text": json.dumps({"success": True, "virtual_width": 1280})},
            {"type": "image", "data": PNG, "mimeType": "image/png"},
        ]}

    registry = ToolRegistry(external_call_timeout=30)
    _computer_use(registry, screenshot=screenshot)
    outcome = await registry.dispatch("mcp__computer-use__screenshot", {})
    assert [(i.media_type, i.data) for i in outcome.images] == [("image/png", PNG)]
    assert PNG not in outcome.text

    # The block the native loop builds from the outcome.
    block = ToolResultBlock(
        tool_use_id="call_1",
        content=[Text(outcome.text), *(Image(media_type=i.media_type, data=i.data)
                                      for i in outcome.images)],
    )
    bodies = _bodies(block)
    [result] = [b for m in bodies["anthropic"]["messages"] for b in m["content"]
                if b.get("type") == "tool_result"]
    assert result["content"][1] == {
        "type": "image", "source": {"type": "base64", "media_type": "image/png", "data": PNG},
    }
    [output] = [i for i in bodies["responses"]["input"] if i["type"] == "function_call_output"]
    assert output["output"][1]["image_url"] == f"data:image/png;base64,{PNG}"
    chat = bodies["openrouter"]["messages"]
    tool_message = next(m for m in chat if m["role"] == "tool")
    assert PNG not in tool_message["content"]
    follow = chat[chat.index(tool_message) + 1]
    assert follow["content"][1]["image_url"]["url"] == f"data:image/png;base64,{PNG}"


def test_browser_catalog_instructions_reach_the_native_prompt(tmp_path, monkeypatch):
    from app.core import native_browser_proxy as proxy
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions

    entry = tmp_path / "index.js"
    entry.write_text("")
    (tmp_path / "catalog.json").write_text(json.dumps({
        "version": 1,
        "instructions": "Refs die on navigation. Re-snapshot, then act.",
        "tools": [{"name": "browser_snapshot", "description": "Snapshot",
                   "inputSchema": {"type": "object", "properties": {}}}],
    }))
    monkeypatch.setattr("app.core.builtin_mcp_servers._browser_mcp_entry", lambda: str(entry))
    proxy._catalog.cache_clear()
    config = {"command": "node", "args": [str(entry)], "env": {
        "AIS_BROWSER_MODE": "proxy", "AIS_SESSION_ID": "s", "AIS_PANEL_TOKEN": "t",
        "AIS_PANEL_REGISTER_URL": "http://127.0.0.1:9/api/v1/browser/panel/register",
    }}
    registry = ToolRegistry()
    assert proxy.register_native_browser_proxy(registry, config)
    assert registry.server_instructions() == {
        "browser": "Refs die on navigation. Re-snapshot, then act."
    }

    compiled = HarnessAdapter._compile_prompt_sections(
        "Base prompt.", None, SessionOptions(model="gpt-5", cwd=str(tmp_path)),
        native=True, registry=registry,
    )
    assert compiled is not None
    assert "# MCP Server Instructions" in compiled.text
    assert "## browser\nRefs die on navigation. Re-snapshot, then act." in compiled.text


def test_server_instructions_are_left_out_when_no_tool_of_the_server_is_permitted():
    from app.core.provider.harness_adapter import _mcp_instructions_block

    registry = SimpleNamespace(server_instructions=lambda: {"browser": "Rules."})
    assert _mcp_instructions_block(registry, None) == (
        "# MCP Server Instructions\n\nThe following MCP servers have provided "
        "instructions for how to use their tools and resources:\n\n## browser\nRules."
    )
    assert _mcp_instructions_block(registry, frozenset({"Read"})) is None
    assert _mcp_instructions_block(
        registry, frozenset({"mcp__browser__browser_snapshot"})
    ) is not None


@pytest.mark.parametrize("mode", ["bypassPermissions", "default"])
def test_repeating_a_screen_action_is_not_a_doom_loop(mode):
    from app.core.provider.harness_adapter import (
        SCREEN_TOOL_REPEAT_THRESHOLD,
        HarnessAdapter,
    )
    from app.core.provider.types import SessionOptions

    engine = HarnessAdapter()._build_permissions(SessionOptions(model="m", permission_mode=mode))
    tab = ("mcp__computer-use__key", {"text": "Tab"})
    results = [engine.note_execution(*tab) for _ in range(SCREEN_TOOL_REPEAT_THRESHOLD)]
    assert results == [False] * (SCREEN_TOOL_REPEAT_THRESHOLD - 1) + [True]
    shot = ("mcp__browser__browser_screenshot", {})
    assert [engine.note_execution(*shot) for _ in range(3)] == [False, False, False]
    # Everything else keeps the default guard.
    bash = ("Bash", {"command": "flaky"})
    assert [engine.note_execution(*bash) for _ in range(3)] == [False, False, True]


@pytest.mark.parametrize(("cfg", "expected"), [
    ({}, (1280, 800)),
    # A profile saved with the model defaults stores nulls; int(None) used to
    # raise and the session silently lost desktop control.
    ({"virtual_width": None, "virtual_height": None}, (1280, 800)),
    ({"virtual_width": "wide", "virtual_height": -5}, (1280, 800)),
    ({"virtual_width": 1024, "virtual_height": 768}, (1024, 768)),
    # Past the long edge or the pixel budget the model would see a shrunk
    # image while clicks map from the captured size.
    ({"virtual_width": 1920, "virtual_height": 1080}, (1429, 804)),
    ({"virtual_width": 3000, "virtual_height": 400}, (1568, 209)),
])
def test_screenshot_target_stays_inside_what_reaches_the_model_unscaled(cfg, expected):
    from fluid.tools.images import MAX_IMAGE_EDGE

    from app.core.query_options import computer_use_virtual_size

    size = computer_use_virtual_size(cfg)
    assert size == expected
    assert max(size) <= MAX_IMAGE_EDGE and size[0] * size[1] <= 1_150_000
