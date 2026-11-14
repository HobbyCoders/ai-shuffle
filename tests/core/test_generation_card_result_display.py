"""A generation tool's card result reaches the chat whole (#1603).

The chat draws the choice, confirmation and saved-choice cards by parsing the
unified generation tool's JSON result. Every tool result used to be cut to the
2,000-character display limit on the way to the chat and the event log, so a
``choose`` result with four options (about 17,000 characters) arrived as
invalid JSON and no choice card appeared.
"""
import json
from contextlib import ExitStack

import pytest

import app.core.query_engine as qe
from app.core.provider.events import (
    AssistantMessage,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from app.core.query_streaming import (
    CARD_RESULT_DISPLAY_SIZE,
    CARD_RESULT_TOOLS,
    MAX_DISPLAY_OUTPUT_SIZE,
    is_card_result_tool,
    truncate_large_payload,
)
from tests.core.test_subagent_inner_output import (
    _ack_chat_patches,
    _ack_state,
    _AckStubPool,
)

BUNDLED = "mcp__fluid_bundled__mcp__ai-tools__generateImage"


def _choose_result(options: int = 4) -> str:
    """A ``choose`` envelope shaped like the real one, each option with its card fields."""
    field = {
        "name": "aspect_ratio", "type": "string", "title": "Aspect Ratio",
        "description": "The aspect ratio of the generated image. " * 6,
        "required": False, "default": "1:1", "enum": ["1:1", "16:9", "9:16", "4:3", "3:4"],
        "minimum": None, "maximum": None, "format": None, "is_file": False, "is_primary_text": False,
    }
    return json.dumps({
        "status": "choose", "success": False,
        "price": {"currency": "USD", "unit": "item", "amount": None, "total": None, "status": "unknown"},
        "kind": "image",
        "options": [{
            "source": "fal", "account": "fal-ai", "worker": "dev_test", "model": f"fal-ai/model-{i}",
            "options": {"aspect_ratio": "1:1"}, "kind": "image", "verified": False,
            "price": {"currency": "USD", "unit": "item", "amount": "0.15", "total": "0.15", "status": "estimated"},
            "schema": {"endpoint_id": f"fal-ai/model-{i}", "primary_text_field": "prompt", "fields": [field] * 8},
            "available": True, "reason": None,
        } for i in range(options)],
        "message": "The user sees these options as a choice card and can remember a pick.",
    }, indent=2)


def test_choose_result_is_larger_than_the_display_limit():
    assert len(_choose_result()) > MAX_DISPLAY_OUTPUT_SIZE


@pytest.mark.parametrize("name", [
    "generateImage", "generateVideo", "generateAudio", "textTo3D",
    "GenerateNativeImage", "GenerateNativeVideo",
    BUNDLED, "mcp__ai-tools__generateVideo",
])
def test_card_tools_keep_their_whole_json(name):
    text = _choose_result()
    out = truncate_large_payload([{"type": "text", "text": text}], tool_name=name)
    assert out == text
    assert json.loads(out)["status"] == "choose"


@pytest.mark.parametrize("name", [
    None, "unknown", "Bash", "mcp__fluid_bundled__mcp__ai-tools__searchModels",
    # A user MCP server that happens to name a tool the same is not ours, and
    # the native aliases are registered bare only.
    "mcp__someserver__generateImage", "mcp__ai-tools__GenerateNativeImage",
])
def test_other_tools_keep_the_display_limit(name):
    assert len(truncate_large_payload("x" * 10_000, tool_name=name)) == MAX_DISPLAY_OUTPUT_SIZE
    assert not is_card_result_tool(name)


def test_card_tools_match_the_unified_generation_tools():
    from app.mcp.ai_tools_sdk import UNIFIED_TOOL_KINDS

    assert set(UNIFIED_TOOL_KINDS) <= CARD_RESULT_TOOLS


def test_card_limit_holds_the_bounded_choose_result():
    from app.core.turn_recorder import RESULT_TRUNCATE_CHARS
    from app.mcp.ai_tools_sdk import CHOOSE_RESULT_CHARS

    assert CHOOSE_RESULT_CHARS < CARD_RESULT_DISPLAY_SIZE <= RESULT_TRUNCATE_CHARS


@pytest.mark.asyncio
async def test_chat_stream_carries_the_whole_choose_result():
    """The interactive chat lane (Agent SDK / Fluid harness) yields the full result."""
    text = _choose_result()
    messages = [
        AssistantMessage(content=[ToolUseBlock(id="toolu_gen", name=BUNDLED, input={"prompt": "an alley"})],
                         model="claude"),
        UserMessage(content=[ToolResultBlock(tool_use_id="toolu_gen", content=[{"type": "text", "text": text}],
                                             is_error=False)]),
    ]
    pool = _AckStubPool(_ack_state(messages))
    results = []
    with ExitStack() as stack:
        _ack_chat_patches(stack, pool)
        async for event in qe._stream_to_websocket_impl("prompt", "sess-gen-card", "claude-code"):
            if isinstance(event, dict) and event.get("type") == "tool_result":
                results.append(event)

    assert [r["tool_use_id"] for r in results] == ["toolu_gen"]
    assert results[0]["output"] == text


@pytest.mark.asyncio
async def test_headless_recording_carries_the_whole_choose_result():
    """HTTP and headless queries record the result under the tool's own name."""
    recorded = []

    class Recorder:
        async def on_event(self, event):
            recorded.append(event)

    text = _choose_result()
    names: dict = {}
    await qe._record_query_message(Recorder(), AssistantMessage(
        content=[ToolUseBlock(id="toolu_gen", name=BUNDLED, input={})], model="claude"), names)
    await qe._record_query_message(Recorder(), UserMessage(
        content=[ToolResultBlock(tool_use_id="toolu_gen", content=text, is_error=False)]), names)
    await qe._record_query_message(Recorder(), UserMessage(
        content=[ToolResultBlock(tool_use_id="toolu_other", content="y" * 5000, is_error=False)]), names)

    outputs = [e["output"] for e in recorded if e["type"] == "tool_result"]
    assert outputs == [text, "y" * MAX_DISPLAY_OUTPUT_SIZE]
