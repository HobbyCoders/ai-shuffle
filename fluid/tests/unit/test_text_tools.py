"""Text-protocol tool calling: parser, adapter stream, history round-trip."""

import json
from collections.abc import AsyncIterator

from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.providers.local.text_tools import (
    TextToolParser,
    TextToolsAdapter,
    render_tool_protocol,
    serialize_history,
    tool_call_text,
    tool_result_to_text,
)

# ─── parser: incremental event extraction ────────────────────────────────────


def _events(*chunks: str) -> list[tuple]:
    parser = TextToolParser()
    out: list[tuple] = []
    for chunk in chunks:
        out.extend(parser.feed(chunk))
    out.extend(parser.finalize())
    return out


def _calls(events: list[tuple]) -> list[tuple]:
    return [e for e in events if e[0] == "tool_call"]


def _text(events: list[tuple]) -> str:
    return "".join(e[1] for e in events if e[0] == "text")


def test_plain_text_passes_through() -> None:
    events = _events("Hello ", "world")
    assert events == [("text", "Hello "), ("text", "world")]


def test_single_tool_call_extracted_and_suppressed() -> None:
    events = _events(
        'I will check.\n<tool_call>{"name": "Read", "arguments": {"path": "a.txt"}}</tool_call>'
    )
    assert _text(events) == "I will check.\n"
    assert _calls(events) == [("tool_call", "Read", {"path": "a.txt"})]


def test_marker_split_across_chunk_boundaries() -> None:
    events = _events(
        "Sure. <tool_c",
        'all>{"name": "Read", "argu',
        'ments": {"path": "a.txt"}}</tool',
        "_call> Done.",
    )
    assert _text(events) == "Sure.  Done."
    assert _calls(events) == [("tool_call", "Read", {"path": "a.txt"})]


def test_partial_marker_never_leaks_mid_stream() -> None:
    parser = TextToolParser()
    events = parser.feed("thinking about it <tool_ca")
    # The held-back suffix must not appear in emitted text yet.
    assert "".join(e[1] for e in events if e[0] == "text") == "thinking about it "


def test_partial_marker_that_was_just_text_flushes() -> None:
    events = _events("a < b and <tool is fine")
    assert _text(events) == "a < b and <tool is fine"
    assert _calls(events) == []


def test_multiple_tool_calls_in_one_reply() -> None:
    events = _events(
        '<tool_call>{"name": "Read", "arguments": {"path": "a"}}</tool_call>\n'
        '<tool_call>{"name": "Read", "arguments": {"path": "b"}}</tool_call>'
    )
    assert _calls(events) == [
        ("tool_call", "Read", {"path": "a"}),
        ("tool_call", "Read", {"path": "b"}),
    ]


def test_malformed_json_emits_tool_use_with_error_payload() -> None:
    events = _events('<tool_call>{"name": "Read", "arguments": {broken</tool_call>')
    (call,) = _calls(events)
    assert call[1] == "Read"  # name salvaged from the broken JSON
    assert call[2]["error"] == "malformed JSON inside <tool_call> block"
    assert "broken" in call[2]["raw"]
    # The payload is always dispatchable JSON.
    json.dumps(call[2])


def test_malformed_json_without_name_salvage() -> None:
    events = _events("<tool_call>total garbage</tool_call>")
    (call,) = _calls(events)
    assert call[1] == ""
    assert call[2]["error"] == "malformed JSON inside <tool_call> block"


def test_fenced_tool_call_block() -> None:
    events = _events(
        'Okay.\n```json\n<tool_call>{"name": "Read", "arguments": {"path": "a"}}</tool_call>\n```\nDone.'
    )
    assert _calls(events) == [("tool_call", "Read", {"path": "a"})]
    text = _text(events)
    assert "```" not in text
    assert "Okay." in text and "Done." in text


def test_inner_fenced_json_inside_markers() -> None:
    events = _events(
        '<tool_call>\n```json\n{"name": "Read", "arguments": {"path": "a"}}\n```\n</tool_call>'
    )
    assert _calls(events) == [("tool_call", "Read", {"path": "a"})]


def test_double_encoded_arguments_string() -> None:
    events = _events(
        '<tool_call>{"name": "Read", "arguments": "{\\"path\\": \\"a\\"}"}</tool_call>'
    )
    assert _calls(events) == [("tool_call", "Read", {"path": "a"})]


def test_parameters_key_accepted() -> None:
    events = _events('<tool_call>{"name": "Read", "parameters": {"path": "a"}}</tool_call>')
    assert _calls(events) == [("tool_call", "Read", {"path": "a"})]


def test_missing_arguments_defaults_to_empty() -> None:
    events = _events('<tool_call>{"name": "ListFiles"}</tool_call>')
    assert _calls(events) == [("tool_call", "ListFiles", {})]


def test_think_block_becomes_thinking_events() -> None:
    events = _events("<think>hmm, ", "let me see</think>The answer is 4.")
    thinking = "".join(e[1] for e in events if e[0] == "thinking")
    assert thinking == "hmm, let me see"
    assert _text(events) == "The answer is 4."


def test_unterminated_tool_call_with_complete_json_still_emits() -> None:
    # Stream cut off after the JSON but before </tool_call> (e.g. max_tokens).
    events = _events('<tool_call>{"name": "Read", "arguments": {"path": "a"}}')
    assert _calls(events) == [("tool_call", "Read", {"path": "a"})]


def test_unterminated_tool_call_with_partial_json_restores_text() -> None:
    events = _events('<tool_call>{"name": "Rea')
    assert _calls(events) == []
    assert _text(events) == '<tool_call>{"name": "Rea'


# ─── history round-trip serialization ────────────────────────────────────────


def test_tool_blocks_serialize_to_protocol_text() -> None:
    history = [
        Message(role="user", content=[Text("read a.txt")]),
        Message(
            role="assistant",
            content=[
                Text("Checking."),
                ToolUseBlock(id="call_1", name="Read", input={"path": "a.txt"}),
            ],
        ),
        Message(
            role="user",
            content=[ToolResultBlock(tool_use_id="call_1", content=[Text("hello")])],
        ),
    ]
    out = serialize_history(history)
    assert out[0] is history[0]  # untouched messages pass through identically
    assistant_text = "".join(b.text for b in out[1].content if isinstance(b, Text))
    assert (
        '<tool_call>{"name":"Read","arguments":{"path":"a.txt"}}</tool_call>'
        in assistant_text
    )
    assert assistant_text.startswith("Checking.")
    result_text = out[2].content[0].text
    assert result_text.startswith('<tool_result name="Read">')
    assert "hello" in result_text
    assert result_text.rstrip().endswith("</tool_result>")


def test_error_results_carry_error_attribute() -> None:
    block = ToolResultBlock(tool_use_id="x", content=[Text("boom")], is_error=True)
    assert tool_result_to_text(block, "Bash").startswith(
        '<tool_result name="Bash" error="true">'
    )


def test_round_trip_call_text_reparses() -> None:
    # What we write into history must reparse to the identical call.
    text = tool_call_text("Read", {"path": "a.txt", "limit": 5})
    parser = TextToolParser()
    events = parser.feed(text) + parser.finalize()
    assert [e for e in events if e[0] == "tool_call"] == [
        ("tool_call", "Read", {"path": "a.txt", "limit": 5})
    ]


# ─── adapter: request rewrite + synthetic chunk stream ───────────────────────


class ScriptedInner:
    """Fake inner client: records the request, replays scripted text deltas."""

    name = "local"
    caps = ProviderCaps(parallel_tool_calls=True)

    def __init__(self, deltas: list[str], stop_reason: str = "end_turn") -> None:
        self.deltas = deltas
        self.stop_reason = stop_reason
        self.request: ProviderRequest | None = None

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.request = request
        yield StreamChunk("message_start", {"usage": {}, "message": {}})
        yield StreamChunk("block_start", {
            "index": 0, "block_type": "text",
            "tool_name": None, "tool_use_id": None, "block": {},
        })
        for delta in self.deltas:
            yield StreamChunk("text_delta", {"index": 0, "text": delta})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "stop_reason": self.stop_reason,
        })
        yield StreamChunk("message_stop", {})


_TOOLS = [
    {
        "name": "Read",
        "description": "Read a file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
]


async def _run(adapter: TextToolsAdapter, request: ProviderRequest) -> list[StreamChunk]:
    return [c async for c in adapter.stream(request)]


def _request(**kw) -> ProviderRequest:
    defaults = dict(
        model="qwen2.5",
        messages=[Message(role="user", content=[Text("go")])],
        tools=_TOOLS,
        system="be helpful",
    )
    defaults.update(kw)
    return ProviderRequest(**defaults)


async def test_adapter_injects_protocol_into_system_and_strips_tools() -> None:
    inner = ScriptedInner(["hi"])
    adapter = TextToolsAdapter(inner)
    await _run(adapter, _request())
    assert inner.request is not None
    assert inner.request.tools == []  # never sent natively
    system = inner.request.system
    assert system.startswith("be helpful")
    assert "# Tool calling" in system
    assert "<tool_call>" in system
    assert "### Read" in system
    assert '"path"' in system  # the JSON schema rides along


async def test_adapter_without_tools_leaves_system_alone() -> None:
    inner = ScriptedInner(["hi"])
    adapter = TextToolsAdapter(inner)
    await _run(adapter, _request(tools=[]))
    assert inner.request.system == "be helpful"


async def test_adapter_serializes_history_for_inner_client() -> None:
    inner = ScriptedInner(["ok"])
    adapter = TextToolsAdapter(inner)
    history = [
        Message(role="user", content=[Text("read it")]),
        Message(
            role="assistant",
            content=[ToolUseBlock(id="c1", name="Read", input={"path": "a"})],
        ),
        Message(role="user", content=[ToolResultBlock(tool_use_id="c1", content=[Text("data")])]),
    ]
    await _run(adapter, _request(messages=history))
    sent = inner.request.messages
    assert "<tool_call>" in sent[1].content[0].text
    assert '<tool_result name="Read">' in sent[2].content[0].text
    # No tool blocks survive into the inner request.
    for msg in sent:
        assert not any(isinstance(b, (ToolUseBlock, ToolResultBlock)) for b in msg.content)


async def test_adapter_emits_synthetic_native_equivalent_chunks() -> None:
    inner = ScriptedInner([
        "Let me read that.\n<tool_c",
        'all>{"name": "Read", "arguments": {"path": "a.txt"}}</tool_call>',
    ])
    adapter = TextToolsAdapter(inner)
    chunks = await _run(adapter, _request())

    text = "".join(c.payload["text"] for c in chunks if c.kind == "text_delta")
    assert text == "Let me read that.\n"
    assert "<tool_call>" not in text

    start = next(
        c for c in chunks
        if c.kind == "block_start" and c.payload["block_type"] == "tool_use"
    )
    assert start.payload["tool_name"] == "Read"
    assert start.payload["tool_use_id"].startswith("call_")
    index = start.payload["index"]
    args = "".join(
        c.payload["partial_json"] for c in chunks
        if c.kind == "tool_args_delta" and c.payload["index"] == index
    )
    assert json.loads(args) == {"path": "a.txt"}
    assert any(c.kind == "block_stop" and c.payload["index"] == index for c in chunks)

    # stop_reason rewritten so the engine loop dispatches the call.
    usage = next(c for c in chunks if c.kind == "usage")
    assert usage.payload["stop_reason"] == "tool_use"
    assert usage.payload["usage"]["output_tokens"] == 5  # payload preserved


async def test_adapter_multiple_calls_get_distinct_ids_and_indexes() -> None:
    inner = ScriptedInner([
        '<tool_call>{"name": "Read", "arguments": {"path": "a"}}</tool_call>'
        '<tool_call>{"name": "Read", "arguments": {"path": "b"}}</tool_call>'
    ])
    adapter = TextToolsAdapter(inner)
    chunks = await _run(adapter, _request())
    starts = [
        c for c in chunks
        if c.kind == "block_start" and c.payload["block_type"] == "tool_use"
    ]
    assert len(starts) == 2
    assert starts[0].payload["index"] != starts[1].payload["index"]
    assert starts[0].payload["tool_use_id"] != starts[1].payload["tool_use_id"]


async def test_adapter_no_tool_call_leaves_stream_untouched() -> None:
    inner = ScriptedInner(["Just an answer."])
    adapter = TextToolsAdapter(inner)
    chunks = await _run(adapter, _request())
    assert [c.kind for c in chunks] == [
        "message_start", "block_start", "text_delta", "block_stop", "usage", "message_stop",
    ]
    usage = next(c for c in chunks if c.kind == "usage")
    assert usage.payload["stop_reason"] == "end_turn"  # not rewritten


async def test_adapter_think_passthrough_as_thinking_block() -> None:
    inner = ScriptedInner(["<think>pondering</think>Answer."])
    adapter = TextToolsAdapter(inner)
    chunks = await _run(adapter, _request())
    thinking_start = next(
        c for c in chunks
        if c.kind == "block_start" and c.payload["block_type"] == "thinking"
    )
    idx = thinking_start.payload["index"]
    thinking = "".join(
        c.payload["text"] for c in chunks
        if c.kind == "thinking_delta" and c.payload["index"] == idx
    )
    assert thinking == "pondering"
    assert any(c.kind == "block_stop" and c.payload["index"] == idx for c in chunks)
    # Thinking block index sorts before the inner text block (message order).
    assert idx < 0
    text = "".join(c.payload["text"] for c in chunks if c.kind == "text_delta")
    assert text == "Answer."


async def test_adapter_held_tail_flushes_at_end_of_stream() -> None:
    # A partial marker at end of stream is plain text after all — released.
    inner = ScriptedInner(["The syntax is <tool_ca"])
    adapter = TextToolsAdapter(inner)
    chunks = await _run(adapter, _request())
    text = "".join(c.payload["text"] for c in chunks if c.kind == "text_delta")
    assert text == "The syntax is <tool_ca"


async def test_adapter_malformed_call_still_dispatches() -> None:
    inner = ScriptedInner(['<tool_call>{"name": "Read", oops}</tool_call>'])
    adapter = TextToolsAdapter(inner)
    chunks = await _run(adapter, _request())
    start = next(
        c for c in chunks
        if c.kind == "block_start" and c.payload["block_type"] == "tool_use"
    )
    assert start.payload["tool_name"] == "Read"
    args = next(c for c in chunks if c.kind == "tool_args_delta")
    payload = json.loads(args.payload["partial_json"])
    assert payload["error"] == "malformed JSON inside <tool_call> block"
    usage = next(c for c in chunks if c.kind == "usage")
    assert usage.payload["stop_reason"] == "tool_use"


def test_protocol_block_lists_every_tool() -> None:
    text = render_tool_protocol([
        {"name": "A", "description": "first", "input_schema": {"type": "object"}},
        {"name": "B", "description": "second", "input_schema": {"type": "object"}},
    ])
    assert "### A" in text and "### B" in text
    assert "first" in text and "second" in text
