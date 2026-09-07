"""OpenAI Responses provider: SSE normalization, request body, e2e chunk walk."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from fluid.messages import Message, Reasoning, Text, ToolResultBlock, ToolUseBlock
from fluid.providers._sse import sse_events
from fluid.engine.native import NativeLoop
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.providers.openai_responses.client import OpenAIResponsesClient
from fluid.providers.openai_responses.serialize import (
    ResponsesStreamNormalizer,
    build_request_body,
)
from fluid.store.base import MemoryStore

GOLDEN = Path(__file__).parent.parent / "golden" / "openai_responses_text_reasoning_tool.sse"


async def _lines(text: str) -> AsyncIterator[str]:
    for line in text.splitlines(keepends=True):
        yield line


async def _golden_chunks() -> list[StreamChunk]:
    normalizer = ResponsesStreamNormalizer()
    chunks: list[StreamChunk] = []
    async for name, data in sse_events(_lines(GOLDEN.read_text())):
        chunks.extend(normalizer.consume(name, data))
    return chunks


# ─── SSE → chunk normalization ───────────────────────────────────────────────


async def test_sse_normalization_kind_sequence() -> None:
    chunks = await _golden_chunks()
    assert [c.kind for c in chunks] == [
        "message_start",
        "block_start", "thinking_delta", "block_stop",
        "block_start", "text_delta", "text_delta", "block_stop",
        "block_start", "tool_args_delta", "tool_args_delta", "block_stop",
        "usage", "message_stop",
    ]


async def test_block_start_indices_and_tool_metadata() -> None:
    chunks = await _golden_chunks()
    starts = [c for c in chunks if c.kind == "block_start"]
    assert [s.payload["index"] for s in starts] == [0, 1, 2]
    assert [s.payload["block_type"] for s in starts] == ["thinking", "text", "tool_use"]
    tool_start = starts[2]
    assert tool_start.payload["tool_name"] == "get_weather"
    assert tool_start.payload["tool_use_id"] == "call_xyz"


async def test_tool_args_accumulate_byte_exact() -> None:
    import json

    chunks = await _golden_chunks()
    args = "".join(
        c.payload["partial_json"] for c in chunks if c.kind == "tool_args_delta"
    )
    assert json.loads(args) == {"city": "Paris"}


async def test_block_stop_carries_full_done_item() -> None:
    # response.completed.output is empty; the authoritative items ride on
    # block_stop under payload["block"].
    chunks = await _golden_chunks()
    stops = [c for c in chunks if c.kind == "block_stop"]
    reasoning_block = stops[0].payload["block"]
    assert reasoning_block["type"] == "reasoning"
    assert reasoning_block["encrypted_content"] == "ENCRYPTED_BLOB_RS1"
    fc_block = stops[2].payload["block"]
    assert fc_block["type"] == "function_call"
    assert fc_block["arguments"] == '{"city": "Paris"}'
    assert fc_block["call_id"] == "call_xyz"


async def test_usage_maps_cached_tokens_anthropic_style() -> None:
    chunks = await _golden_chunks()
    usage = next(c for c in chunks if c.kind == "usage")
    u = usage.payload["usage"]
    # input_tokens 200 includes 64 cached → 136 exclusive + 64 cache_read.
    assert u["input_tokens"] == 136
    assert u["cache_read_input_tokens"] == 64
    assert u["output_tokens"] == 30
    assert usage.payload["stop_reason"] == "tool_use"


# ─── request body ────────────────────────────────────────────────────────────


def _request_with_history() -> ProviderRequest:
    reasoning_item = {
        "id": "rs_1",
        "type": "reasoning",
        "status": "completed",
        "encrypted_content": "BLOB",
        "summary": [{"type": "summary_text", "text": "thought"}],
        "content": [],
    }
    return ProviderRequest(
        model="gpt-5.6-sol",
        system="be terse",
        messages=[
            Message(role="user", content=[Text("hi")]),
            Message(
                role="assistant",
                content=[
                    Reasoning(origin="openai_responses", raw=reasoning_item),
                    Reasoning(origin="anthropic", raw={"type": "thinking", "thinking": "x"}),
                    Text("prev answer"),
                    ToolUseBlock(id="call_1", name="get_weather", input={"city": "Paris"}),
                ],
            ),
            Message(
                role="user",
                content=[ToolResultBlock(tool_use_id="call_1", content=[Text("sunny")])],
            ),
        ],
        tools=[
            {
                "name": "get_weather",
                "description": "Get weather",
                "input_schema": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                    "required": ["city"],
                },
            }
        ],
    )


def test_request_body_envelope_and_tools() -> None:
    body = build_request_body(_request_with_history())
    assert body["store"] is False
    assert body["stream"] is True
    assert body["include"] == ["reasoning.encrypted_content"]
    assert body["instructions"] == "be terse"
    assert body["tools"] == [
        {
            "type": "function",
            "name": "get_weather",
            "description": "Get weather",
            "parameters": {
                "type": "object",
                "properties": {"city": {"type": "string"}},
                "required": ["city"],
            },
        }
    ]


def test_request_body_reasoning_verbatim_and_foreign_dropped() -> None:
    body = build_request_body(_request_with_history())
    items = body["input"]
    types = [it["type"] for it in items]
    # user message, own reasoning (verbatim), assistant message, function_call,
    # function_call_output. The foreign (anthropic) reasoning is dropped entirely.
    assert types == [
        "message",
        "reasoning",
        "message",
        "function_call",
        "function_call_output",
    ]
    reasoning = items[1]
    # id + status stripped on stateless resend; everything else verbatim.
    assert "id" not in reasoning and "status" not in reasoning
    assert reasoning["encrypted_content"] == "BLOB"
    assert reasoning["summary"] == [{"type": "summary_text", "text": "thought"}]

    assert items[0] == {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    assert items[2] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "prev answer"}],
    }
    assert items[3] == {
        "type": "function_call",
        "call_id": "call_1",
        "name": "get_weather",
        "arguments": '{"city":"Paris"}',
    }
    assert items[4] == {
        "type": "function_call_output",
        "call_id": "call_1",
        "output": "sunny",
    }


def test_extra_enables_stateful_passthrough() -> None:
    request = ProviderRequest(
        model="gpt-5.6-sol",
        messages=[Message(role="user", content=[Text("hi")])],
        extra={"previous_response_id": "resp_prev", "store": True},
    )
    body = build_request_body(request)
    assert body["previous_response_id"] == "resp_prev"
    assert body["store"] is True  # extra overrides the default


# ─── fake-transport end-to-end ───────────────────────────────────────────────


async def test_client_end_to_end_chunk_walk() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=GOLDEN.read_text().encode(),
            headers={"content-type": "text/event-stream"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIResponsesClient(api_key="sk-test", http_client=http)
    request = ProviderRequest(
        model="gpt-5.6-sol", messages=[Message(role="user", content=[Text("weather?")])]
    )
    chunks = [c async for c in client.stream(request)]
    await http.aclose()

    assert chunks[0].kind == "message_start"
    assert chunks[-1].kind == "message_stop"
    # The done reasoning item survived the wire → block_stop carries it.
    stops = [c for c in chunks if c.kind == "block_stop"]
    assert stops[0].payload["block"]["encrypted_content"] == "ENCRYPTED_BLOB_RS1"


async def test_client_refreshes_and_retries_once_on_401() -> None:
    class FakeAuth:
        account_id = "acct_1"

        def __init__(self) -> None:
            self.tokens = ["stale", "fresh"]
            self.on_401_calls = 0

        async def get_access_token(self) -> str:
            return self.tokens[0]

        async def on_401(self) -> None:
            self.on_401_calls += 1
            self.tokens.pop(0)

    auth = FakeAuth()
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization", ""))
        if request.headers.get("authorization") == "Bearer stale":
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return httpx.Response(
            200,
            content=GOLDEN.read_text().encode(),
            headers={"content-type": "text/event-stream"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIResponsesClient(auth=auth, http_client=http)
    request = ProviderRequest(
        model="gpt-5.6-sol", messages=[Message(role="user", content=[Text("hi")])]
    )
    chunks = [c async for c in client.stream(request)]
    await http.aclose()

    assert auth.on_401_calls == 1
    assert seen == ["Bearer stale", "Bearer fresh"]
    assert chunks[-1].kind == "message_stop"
    assert not any(c.kind == "error" for c in chunks)


async def test_client_account_id_header_present() -> None:
    class FakeAuth:
        account_id = "acct_42"

        async def get_access_token(self) -> str:
            return "tok"

        async def on_401(self) -> None:  # pragma: no cover - not hit here
            pass

    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["account"] = request.headers.get("chatgpt-account-id")
        captured["auth"] = request.headers.get("authorization")
        captured["originator"] = request.headers.get("originator")
        captured["beta"] = request.headers.get("openai-beta")
        return httpx.Response(
            200,
            content=GOLDEN.read_text().encode(),
            headers={"content-type": "text/event-stream"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIResponsesClient(
        auth=FakeAuth(),
        base_url="https://chatgpt.com/backend-api/codex/responses",
        http_client=http,
    )
    request = ProviderRequest(
        model="gpt-5.6-sol", messages=[Message(role="user", content=[Text("hi")])]
    )
    async for _ in client.stream(request):
        pass
    await http.aclose()
    assert captured["account"] == "acct_42"
    assert captured["auth"] == "Bearer tok"
    assert captured["originator"] == "codex_cli_rs"
    assert captured["beta"] == "responses=experimental"


async def test_xai_auth_does_not_send_codex_headers() -> None:
    class FakeAuth:
        account_id = "should-not-leak"

        async def get_access_token(self) -> str:
            return "xai-tok"

        async def on_401(self) -> None:  # pragma: no cover - not hit here
            pass

    client = OpenAIResponsesClient(
        auth=FakeAuth(),
        base_url="https://api.x.ai/v1/responses",
    )
    headers = await client._headers()
    assert headers["Authorization"] == "Bearer xai-tok"
    assert "originator" not in headers
    assert "OpenAI-Beta" not in headers
    assert "chatgpt-account-id" not in headers


@pytest.mark.parametrize("effort", ["none", "low", "medium", "high", "xhigh", "max"])
def test_sol_effort_overrides_thinking_without_clamping(effort: str) -> None:
    # https://developers.openai.com/api/docs/models/gpt-5.6-sol.md
    from fluid.messages import Message as _Msg
    from fluid.messages import Text as _Text

    request = ProviderRequest(
        model="gpt-5.6",
        messages=[_Msg(role="user", content=[_Text("hi")])],
        thinking={"budget_tokens": 1024},
        effort=effort,
    )
    body = build_request_body(request)
    assert body["reasoning"] == {"effort": effort, "summary": "auto"}


@pytest.mark.parametrize("effort", ["none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"])
def test_thinking_effort_still_honored_without_explicit_effort(effort: str) -> None:
    from fluid.messages import Message as _Msg
    from fluid.messages import Text as _Text

    request = ProviderRequest(
        model="gpt-5.6",
        messages=[_Msg(role="user", content=[_Text("hi")])],
        thinking={"effort": effort},
    )
    body = build_request_body(request)
    assert body["reasoning"] == {"effort": effort, "summary": "auto"}


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5.6-sol", "gpt-5.6-terra"])
@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max", "ultra"])
@pytest.mark.parametrize("via_thinking", [False, True])
def test_codex_catalog_efforts_reach_responses_unchanged(model, effort, via_thinking) -> None:
    request = ProviderRequest(
        model=model,
        messages=[Message(role="user", content=[Text("hi")])],
        effort=None if via_thinking else effort,
        thinking={"effort": effort} if via_thinking else {"effort": "medium"},
    )
    assert build_request_body(request)["reasoning"] == {"effort": effort, "summary": "auto"}


# ─── prompt_cache_key: the session id, unless the host says otherwise ────────


class _NamedProvider:
    caps = ProviderCaps()

    def __init__(self, name: str) -> None:
        self.name = name

    async def stream(self, request):  # pragma: no cover - never streamed here
        if False:
            yield None


async def _loop_with_session(provider, **kw) -> tuple[NativeLoop, str]:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(provider=provider, store=store, model="gpt-5.6", session_id=sid, **kw)
    await loop._ensure_session()
    return loop, sid


async def test_responses_requests_carry_the_session_as_prompt_cache_key() -> None:
    loop, sid = await _loop_with_session(_NamedProvider("openai_responses"))
    try:
        request = loop._build_request([])
        assert request.extra == {"prompt_cache_key": sid}
        assert build_request_body(request)["prompt_cache_key"] == sid
        # The non-sticky retry drops the key, as it does for OpenRouter.
        assert "prompt_cache_key" not in loop._build_request([], sticky=False).extra
    finally:
        await loop.aclose()


async def test_host_request_extra_overrides_the_prompt_cache_key() -> None:
    loop, _ = await _loop_with_session(
        _NamedProvider("openai_responses"), request_extra={"prompt_cache_key": "host-key"}
    )
    try:
        assert loop._build_request([]).extra["prompt_cache_key"] == "host-key"
    finally:
        await loop.aclose()


async def test_other_providers_get_no_prompt_cache_key() -> None:
    for name in ("anthropic", "openrouter", "local"):
        loop, sid = await _loop_with_session(_NamedProvider(name))
        try:
            extra = loop._build_request([]).extra
        finally:
            await loop.aclose()
        assert "prompt_cache_key" not in extra
        assert extra == ({"session_id": sid} if name == "openrouter" else {})


# ─── truncation, refusals, the output ceiling ────────────────────────────────


def _consume_all(events: list[tuple[str, dict]]) -> list[StreamChunk]:
    normalizer = ResponsesStreamNormalizer()
    out: list[StreamChunk] = []
    for name, data in events:
        out.extend(normalizer.consume(name, data))
    return out


def _truncated_call_events() -> list[tuple[str, dict]]:
    """A response cut off by max_output_tokens halfway through a tool call."""
    return [
        ("response.created", {"response": {"id": "resp_1"}}),
        ("response.output_item.added", {"item": {
            "id": "fc_1", "type": "function_call", "name": "Write", "call_id": "call_1",
        }}),
        ("response.function_call_arguments.delta", {
            "item_id": "fc_1", "delta": '{"path": "a.txt", "content": "half',
        }),
        ("response.incomplete", {"response": {
            "id": "resp_1", "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "usage": {"input_tokens": 120, "output_tokens": 4096},
        }}),
    ]


def test_incomplete_response_closes_the_open_call_and_reports_max_tokens() -> None:
    chunks = _consume_all(_truncated_call_events())
    assert [c.kind for c in chunks] == [
        "message_start", "block_start", "tool_args_delta", "block_stop",
        "usage", "message_stop",
    ]
    usage = chunks[-2].payload
    assert usage["stop_reason"] == "max_tokens"
    assert usage["usage"]["input_tokens"] == 120
    assert usage["usage"]["output_tokens"] == 4096
    assert usage["native_stop_reason"] == "incomplete:max_output_tokens"


async def test_truncated_call_is_not_mistaken_for_an_empty_completion() -> None:
    # Before the incomplete event was handled the stream ended with no usage
    # chunk, which the engine reads as an empty completion and retries ten
    # times. With usage reported it is a max_tokens stop like any other.
    import asyncio

    from fluid.events import ProviderRetry, UserTurn

    truncated = _consume_all(_truncated_call_events())

    class _Provider:
        name = "openai_responses"
        caps = ProviderCaps(stateful_reasoning=True)

        def __init__(self) -> None:
            self.calls = 0

        async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
            self.calls += 1
            for chunk in truncated:
                yield chunk

    provider = _Provider()
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="gpt-5.6-sol", session_id=sid,
        max_continuations_per_turn=0,
    )
    await loop.submit(UserTurn(content="write it"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)

    assert provider.calls == 1
    assert not any(isinstance(e, ProviderRetry) for e in loop.bus.log(sid))


def test_incomplete_content_filter_before_any_output_is_an_error() -> None:
    chunks = _consume_all([
        ("response.created", {"response": {"id": "resp_1"}}),
        ("response.incomplete", {"response": {
            "id": "resp_1", "status": "incomplete",
            "incomplete_details": {"reason": "content_filter"},
            "usage": {"input_tokens": 12},
        }}),
    ])
    assert [c.kind for c in chunks] == ["message_start", "error"]
    assert "content filter" in chunks[-1].payload["message"]
    assert chunks[-1].payload["retriable"] is False


def test_refusal_delta_renders_as_text() -> None:
    chunks = _consume_all([
        ("response.created", {"response": {"id": "resp_1"}}),
        ("response.output_item.added", {"item": {"id": "msg_1", "type": "message"}}),
        ("response.refusal.delta", {"item_id": "msg_1", "delta": "I can't help with that."}),
        ("response.output_item.done", {"item": {
            "id": "msg_1", "type": "message",
            "content": [{"type": "refusal", "refusal": "I can't help with that."}],
        }}),
        ("response.completed", {"response": {
            "id": "resp_1", "status": "completed",
            "usage": {"input_tokens": 10, "output_tokens": 8},
        }}),
    ])
    text = "".join(c.payload["text"] for c in chunks if c.kind == "text_delta")
    assert text == "I can't help with that."
    assert [c.kind for c in chunks].count("block_stop") == 1
    assert chunks[-2].payload["stop_reason"] == "end_turn"


def test_request_body_carries_the_output_ceiling() -> None:
    request = ProviderRequest(
        model="gpt-5.6-sol",
        messages=[Message(role="user", content=[Text("hi")])],
        max_tokens=2048,
    )
    assert build_request_body(request)["max_output_tokens"] == 2048
    request.max_tokens = None
    assert "max_output_tokens" not in build_request_body(request)


# ─── consecutive text blocks: separated, never empty ─────────────────────


def _status_stub_events(fragments, *, empty_first=False):
    """The recorded gpt-6-astra shape: one message item per status fragment."""
    events = [("response.created", {"response": {"id": "resp_1"}})]
    if empty_first:
        events += [
            ("response.output_item.added", {"item": {"id": "msg_0", "type": "message"}}),
            ("response.output_item.done",
             {"item": {"id": "msg_0", "type": "message", "content": []}}),
        ]
    for n, fragment in enumerate(fragments, start=1):
        item = {"id": f"msg_{n}", "type": "message"}
        events += [
            ("response.output_item.added", {"item": item}),
            ("response.output_text.delta", {"item_id": item["id"], "delta": fragment}),
            ("response.output_item.done", {"item": item}),
        ]
    events.append(("response.completed", {"response": {
        "id": "resp_1", "status": "completed",
        "usage": {"input_tokens": 10, "output_tokens": 8},
    }}))
    return events


def _text_of(chunks) -> str:
    return "".join(c.payload["text"] for c in chunks if c.kind == "text_delta")


def test_consecutive_text_blocks_are_separated_by_a_blank_line() -> None:
    # Recorded: a turn of per-batch status lines arrived as one run-on,
    # "**Stopping local servers****Waiting for local test**".
    chunks = _consume_all(_status_stub_events([
        "**Stopping local servers**",
        "**Waiting for local test**",
        "**Waiting for agent review**",
    ]))
    assert _text_of(chunks) == (
        "**Stopping local servers**\n\n"
        "**Waiting for local test**\n\n"
        "**Waiting for agent review**"
    )


def test_a_message_item_that_streams_nothing_leaves_no_block() -> None:
    chunks = _consume_all(
        _status_stub_events(["**Waiting for agent results**"], empty_first=True)
    )
    starts = [c for c in chunks if c.kind == "block_start"]
    assert [s.payload["block_type"] for s in starts] == ["text"]
    assert [c.kind for c in chunks].count("block_stop") == 1
    assert _text_of(chunks) == "**Waiting for agent results**"


def test_text_that_already_ends_in_newlines_is_not_padded_further() -> None:
    chunks = _consume_all(_status_stub_events(["one\n", "two\n\n", "three"]))
    assert _text_of(chunks) == "one\n\ntwo\n\nthree"


def test_a_block_between_two_texts_leaves_them_as_the_model_wrote_them() -> None:
    # A tool call already separates the message items around it.
    chunks = _consume_all([
        ("response.created", {"response": {"id": "resp_1"}}),
        ("response.output_item.added", {"item": {"id": "msg_1", "type": "message"}}),
        ("response.output_text.delta", {"item_id": "msg_1", "delta": "before"}),
        ("response.output_item.done", {"item": {"id": "msg_1", "type": "message"}}),
        ("response.output_item.added", {"item": {
            "id": "fc_1", "type": "function_call",
            "name": "get_weather", "call_id": "call_1",
        }}),
        ("response.function_call_arguments.delta", {"item_id": "fc_1", "delta": "{}"}),
        ("response.output_item.done", {"item": {
            "id": "fc_1", "type": "function_call", "name": "get_weather",
            "call_id": "call_1", "arguments": "{}",
        }}),
        ("response.output_item.added", {"item": {"id": "msg_2", "type": "message"}}),
        ("response.output_text.delta", {"item_id": "msg_2", "delta": "after"}),
        ("response.output_item.done", {"item": {"id": "msg_2", "type": "message"}}),
        ("response.completed", {"response": {"id": "resp_1", "status": "completed"}}),
    ])
    assert [c.payload["text"] for c in chunks if c.kind == "text_delta"] == [
        "before", "after",
    ]


def test_text_carried_only_by_the_done_item_is_still_delivered() -> None:
    # Dropping an empty text block must not drop a message that never streamed.
    chunks = _consume_all([
        ("response.created", {"response": {"id": "resp_1"}}),
        ("response.output_item.added", {"item": {"id": "msg_1", "type": "message"}}),
        ("response.output_item.done", {"item": {
            "id": "msg_1", "type": "message",
            "content": [{"type": "output_text", "text": "the whole answer"}],
        }}),
        ("response.completed", {"response": {"id": "resp_1", "status": "completed"}}),
    ])
    assert _text_of(chunks) == "the whole answer"
    assert [c.kind for c in chunks].count("block_stop") == 1


# ─── reasoning summary vs reasoning text ─────────────────────────────────────


def _grok_reasoning_events() -> list[tuple[str, dict]]:
    """A recorded xAI/grok shape: both reasoning channels on one item_id.

    ``summary: "auto"`` makes the provider stream its own clipped summary while
    the reasoning draft streams on the second channel. Sharing one block index
    ran them together into a single "thinking" block that read as a phantom
    collaborator answering the user.
    """
    return [
        ("response.created", {"response": {"id": "resp_1"}}),
        ("response.output_item.added", {
            "item": {"id": "rs_1", "type": "reasoning", "summary": [], "content": []},
        }),
        ("response.reasoning_summary_text.delta", {"item_id": "rs_1", "delta": "Check"}),
        ("response.reasoning_text.delta", {"item_id": "rs_1", "delta": "The scroll "}),
        ("response.reasoning_summary_text.delta", {"item_id": "rs_1", "delta": "ing scroll..."}),
        ("response.reasoning_text.delta", {"item_id": "rs_1", "delta": "reset looks fine."}),
        ("response.output_item.done", {"item": {
            "id": "rs_1", "type": "reasoning", "status": "completed",
            "encrypted_content": "BLOB_RS1",
            "summary": [{"type": "summary_text", "text": "Checking scroll..."}],
            "content": [],
        }}),
        ("response.output_item.added", {"item": {"id": "msg_1", "type": "message"}}),
        ("response.output_text.delta", {"item_id": "msg_1", "delta": "Nothing to change."}),
        ("response.output_item.done", {"item": {"id": "msg_1", "type": "message"}}),
        ("response.completed", {"response": {
            "id": "resp_1", "status": "completed",
            "usage": {"input_tokens": 10, "output_tokens": 8},
        }}),
    ]


def test_reasoning_summary_and_text_get_separate_tagged_blocks() -> None:
    chunks = _consume_all(_grok_reasoning_events())
    starts = [c for c in chunks if c.kind == "block_start"]
    thinking = [s for s in starts if s.payload["block_type"] == "thinking"]
    assert [s.payload["subtype"] for s in thinking] == ["reasoning_summary", "reasoning"]
    summary_index, reasoning_index = (s.payload["index"] for s in thinking)
    assert summary_index != reasoning_index
    # Arrival order decides which block comes first: grok summarizes first.
    assert summary_index < reasoning_index

    def text_at(index: int) -> str:
        return "".join(
            c.payload["text"] for c in chunks
            if c.kind == "thinking_delta" and c.payload["index"] == index
        )

    assert text_at(summary_index) == "Checking scroll..."
    assert text_at(reasoning_index) == "The scroll reset looks fine."


def test_only_one_block_carries_the_encrypted_reasoning_item() -> None:
    # Two blocks, one round-trip envelope: a second copy would resend the same
    # encrypted content twice and 400 the whole request.
    chunks = _consume_all(_grok_reasoning_events())
    carriers = [
        c for c in chunks
        if c.kind == "block_stop"
        and (c.payload.get("block") or {}).get("type") == "reasoning"
    ]
    assert len(carriers) == 1
    assert carriers[0].payload["block"]["encrypted_content"] == "BLOB_RS1"
    starts = {
        s.payload["index"]: s.payload["subtype"]
        for s in chunks if s.kind == "block_start"
    }
    assert starts[carriers[0].payload["index"]] == "reasoning"
    # Both thinking blocks are still closed exactly once.
    thinking_indices = {i for i, sub in starts.items() if sub is not None}
    stops = [c.payload["index"] for c in chunks if c.kind == "block_stop"]
    assert sorted(i for i in stops if i in thinking_indices) == sorted(thinking_indices)
    assert len(stops) == len(set(stops))


def test_summary_only_reasoning_stays_one_block_and_keeps_the_envelope() -> None:
    # The ordinary OpenAI shape (the golden stream): only the summary channel
    # streams, so there is nothing to split and the summary block carries the
    # encrypted item exactly as before.
    chunks = _consume_all([
        ("response.created", {"response": {"id": "resp_1"}}),
        ("response.output_item.added", {"item": {"id": "rs_1", "type": "reasoning"}}),
        ("response.reasoning_summary_text.delta", {"item_id": "rs_1", "delta": "Weighing it."}),
        ("response.output_item.done", {"item": {
            "id": "rs_1", "type": "reasoning", "encrypted_content": "BLOB_RS1",
        }}),
        ("response.completed", {"response": {
            "id": "resp_1", "status": "completed", "usage": {"input_tokens": 3},
        }}),
    ])
    starts = [c for c in chunks if c.kind == "block_start"]
    assert len(starts) == 1
    assert starts[0].payload["block_type"] == "thinking"
    assert starts[0].payload["subtype"] == "reasoning_summary"
    stops = [c for c in chunks if c.kind == "block_stop"]
    assert len(stops) == 1
    assert stops[0].payload["block"]["encrypted_content"] == "BLOB_RS1"


def test_reasoning_item_with_no_visible_text_still_opens_one_block() -> None:
    # Encrypted content and nothing else: the blob must not be dropped, and the
    # block is tagged as reasoning rather than as a provider summary.
    chunks = _consume_all([
        ("response.created", {"response": {"id": "resp_1"}}),
        ("response.output_item.added", {"item": {"id": "rs_1", "type": "reasoning"}}),
        ("response.output_item.done", {"item": {
            "id": "rs_1", "type": "reasoning", "encrypted_content": "BLOB_RS1",
        }}),
        ("response.completed", {"response": {
            "id": "resp_1", "status": "completed", "usage": {"input_tokens": 3},
        }}),
    ])
    starts = [c for c in chunks if c.kind == "block_start"]
    assert len(starts) == 1
    assert starts[0].payload["subtype"] == "reasoning"
    stops = [c for c in chunks if c.kind == "block_stop"]
    assert stops[0].payload["block"]["encrypted_content"] == "BLOB_RS1"


async def test_engine_keeps_one_reasoning_envelope_for_a_split_thinking_pair() -> None:
    # End to end: the split reaches the host as two tagged BlockStarts, and the
    # assembled assistant message still carries exactly one envelope with the
    # encrypted blob — so the next request resends it once.
    import asyncio

    from fluid.events import BlockStart, UserTurn

    recorded = _consume_all(_grok_reasoning_events())

    class _Provider:
        name = "openai_responses"
        caps = ProviderCaps(stateful_reasoning=True)

        async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
            for chunk in recorded:
                yield chunk

    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=_Provider(), store=store, model="grok-4", session_id=sid,
        max_continuations_per_turn=0,
    )
    await loop.submit(UserTurn(content="did you fix it?"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)

    subtypes = [
        e.subtype for e in loop.bus.log(sid)
        if isinstance(e, BlockStart) and e.block_type == "thinking"
    ]
    assert subtypes == ["reasoning_summary", "reasoning"]

    nodes = await store.load_path(sid)
    message = next(
        n.message for n in reversed(nodes)
        if n.message is not None and n.message.role == "assistant"
    )
    envelopes = [b for b in message.content if isinstance(b, Reasoning)]
    with_blob = [b for b in envelopes if b.encrypted_content]
    assert len(with_blob) == 1
    assert with_blob[0].encrypted_content == "BLOB_RS1"
    body = build_request_body(ProviderRequest(model="grok-4", messages=[message]))
    reasoning_items = [i for i in body["input"] if i.get("type") == "reasoning"]
    assert len(reasoning_items) == 1
    assert reasoning_items[0]["encrypted_content"] == "BLOB_RS1"
