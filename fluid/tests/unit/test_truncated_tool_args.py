"""A tool call whose arguments never finished must not take the turn with it.

Seen live on `stealth/ox-alpha`: a run_python call whose `arguments` blob was
cut off mid-value left `{"code":"…` in the accumulator, the bare `json.loads`
in the stream consumer raised, and the whole turn died with
`JSONDecodeError('Unterminated string starting at: line 1 column 9 (char 8)')`
— every completed step in that turn lost with it. The call should fail; the
turn should carry on.
"""

from __future__ import annotations

import pytest

from fluid.engine.native import NativeLoop, _parse_tool_args
from fluid.events import ToolResult, ToolUse, TurnComplete, UserTurn
from fluid.permissions.engine import PermissionEngine
from fluid.providers._sse import sse_events
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec


# ── argument decoding ────────────────────────────────────────────────────


def test_truncated_arguments_report_instead_of_raising() -> None:
    args, err = _parse_tool_args('{"code":"print(1)')
    assert args == {}
    assert err is not None
    assert "not valid JSON" in err
    assert "was not run" in err
    # The model sees what actually arrived, so it can tell truncation from a
    # malformed call it authored.
    assert 'Received: {"code":"print(1)' in err


def test_long_truncated_arguments_are_previewed_not_echoed() -> None:
    _args, err = _parse_tool_args('{"code":"' + "x" * 5000)
    assert err is not None
    assert len(err) < 1000
    assert err.endswith("…")


def test_valid_arguments_are_unchanged() -> None:
    assert _parse_tool_args('{"code":"print(1)"}') == ({"code": "print(1)"}, None)
    assert _parse_tool_args("") == ({}, None)


def test_non_object_arguments_are_rejected() -> None:
    args, err = _parse_tool_args('["code"]')
    assert args == {}
    assert err is not None
    assert "must be a JSON object" in err


# ── the SSE reader ───────────────────────────────────────────────────────


async def _lines(*items: str):
    for item in items:
        yield item


async def _collect(*items: str) -> list[tuple[str, dict]]:
    return [event async for event in sse_events(_lines(*items))]


async def test_a_cut_off_final_frame_ends_the_stream_quietly() -> None:
    events = await _collect(
        'data: {"choices":[{"delta":{"content":"hi"}}]}',
        "",
        'data: {"choices":[{"delta":{"content":"the',  # connection died here
    )
    assert len(events) == 1
    assert events[0][1]["choices"][0]["delta"]["content"] == "hi"


async def test_a_malformed_mid_stream_frame_is_dropped_not_fatal() -> None:
    events = await _collect(
        "data: {not json}",
        "",
        'data: {"ok":true}',
        "",
    )
    assert events == [("message", {"ok": True})]


async def test_done_and_comments_are_still_ignored() -> None:
    events = await _collect(
        ": heartbeat",
        'data: {"ok":true}',
        "",
        "data: [DONE]",
        "",
    )
    assert events == [("message", {"ok": True})]


@pytest.mark.parametrize("payload", ["null", "123", '"text"'])
async def test_non_object_frames_are_dropped(payload: str) -> None:
    assert await _collect(f"data: {payload}", "") == []


# ── end to end: the turn survives and the model is told why ──────────────


class _ScriptedProvider:
    name = "openrouter"
    caps = ProviderCaps()

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, request: ProviderRequest):
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


def _tool_call(tuid: str, name: str, args_json: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk(
            "block_start",
            {"index": 0, "block_type": "tool_use", "tool_name": name, "tool_use_id": tuid},
        ),
        StreamChunk("tool_args_delta", {"index": 0, "partial_json": args_json}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


def _text_reply(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


async def test_a_truncated_call_fails_alone_and_the_turn_finishes() -> None:
    """The reproduction: `{"code":"…` arrives, the call errors, the turn ends
    normally on the model's next reply instead of dying mid-stream."""
    registry = ToolRegistry()

    async def _ran(_input: dict, _ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text="ran")

    registry.register_builtin(
        ToolSpec(
            name="Probe", description="A probe.",
            input_schema={"type": "object", "properties": {}}, source="builtin",
        ),
        _ran,
    )

    provider = _ScriptedProvider([
        _tool_call("call-1", "Probe", '{"code":"print(1)'),
        _text_reply("recovered"),
    ])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="stealth/ox-alpha",
        registry=registry, permissions=PermissionEngine("never_ask"),
    )
    try:
        await loop.submit(UserTurn(content="go"))
        await loop.wait_turn()
        events = loop.bus.log(loop.session_id)

        # Every tool_use still gets exactly one result — the transcript stays
        # well-formed for the next model call.
        uses = [e for e in events if isinstance(e, ToolUse)]
        results = [e for e in events if isinstance(e, ToolResult)]
        assert len(uses) == len(results) == 1
        assert results[0].is_error
        assert "not valid JSON" in results[0].output
        assert 'Received: {"code":"print(1)' in results[0].output
        assert "ran" not in results[0].output  # the tool never executed

        # The turn ended normally on the model's follow-up.
        done = next(e for e in events if isinstance(e, TurnComplete))
        assert done.stop_reason == "end_turn"
        assert provider.calls == 2
    finally:
        await loop.aclose()
