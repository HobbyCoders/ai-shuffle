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
        # The reasoning item's output_item.added: liveness, not a block.
        "provider_alive",
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


@pytest.mark.parametrize(
    "base_url,expect_ceiling",
    [
        ("https://chatgpt.com/backend-api/codex/responses", False),
        ("https://api.openai.com/v1/responses", True),
    ],
)
async def test_codex_backend_request_carries_no_output_ceiling(
    base_url: str, expect_ceiling: bool,
) -> None:
    """The memory learner sets max_tokens on every request; on the Codex
    backend every such request came back HTTP 400 while session turns (no
    ceiling) succeeded. The public API keeps the field."""

    class FakeAuth:
        account_id = "acct_1"

        async def get_access_token(self) -> str:
            return "tok"

        async def on_401(self) -> None:  # pragma: no cover - not hit here
            pass

    bodies: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json as _json

        bodies.append(_json.loads(request.content))
        return httpx.Response(
            200,
            content=GOLDEN.read_text().encode(),
            headers={"content-type": "text/event-stream"},
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIResponsesClient(auth=FakeAuth(), base_url=base_url, http_client=http)
    request = ProviderRequest(
        model="gpt-5.6-sol",
        messages=[Message(role="user", content=[Text("journal slice")])],
        system="You maintain a library",
        max_tokens=16_000,
        effort="low",
    )
    async for _ in client.stream(request):
        pass
    await http.aclose()
    (body,) = bodies
    assert ("max_output_tokens" in body) is expect_ceiling
    assert body["reasoning"]["effort"] == "low"
    assert body["instructions"] == "You maintain a library"


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
        # sticky=False still omits the key. The engine never asks for that on
        # this lane: its sticky policy keeps the key (test_sticky_policy.py).
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


# ─── in-stream errors: classify, hold message_start, retry before content ───
# Modelled on test_anthropic_client.py: an error sent in place of content is
# retried transparently; one after content surfaces.

import json as _json

from fluid.providers._retry import RetryPolicy
from fluid.providers.openai_responses import client as responses_client_module
from fluid.providers.openai_responses.serialize import (
    OVERLOAD_BACKOFF_FLOOR_S,
    OVERLOAD_MAX_RETRIES,
    classify_error,
)


def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {_json.dumps({'type': name, **data})}\n\n"


_CREATED = _event("response.created", {"response": {"id": "resp_1", "status": "in_progress"}})
_TEXT = (
    _event("response.output_text.delta", {"item_id": "msg_1", "delta": "hi"})
    + _event("response.output_item.done", {"item": {
        "id": "msg_1", "type": "message", "role": "assistant",
        "content": [{"type": "output_text", "text": "hi"}],
    }})
)
_COMPLETED = _event("response.completed", {"response": {
    "id": "resp_1", "status": "completed", "output": [],
    "usage": {"input_tokens": 5, "output_tokens": 1},
}})


def _failed(code: str, etype: str = "service_unavailable_error") -> str:
    return _event("response.failed", {"response": {
        "id": "resp_1", "status": "failed",
        "error": {"code": code, "type": etype, "message": code},
    }})


def _sse(body: str, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(
        200, content=body.encode(),
        headers={"content-type": "text/event-stream", **(headers or {})},
    )


@pytest.fixture
def _sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record every backoff the client asks for instead of sleeping."""
    slept: list[float] = []

    async def fake_sleep(delay: float) -> None:
        slept.append(delay)

    monkeypatch.setattr(responses_client_module.asyncio, "sleep", fake_sleep)
    return slept


async def _run_client(handler, *, retries: int = 3, tools: list | None = None) -> list[StreamChunk]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIResponsesClient(
        api_key="sk-test", http_client=http,
        retry=RetryPolicy(max_retries=retries, base_delay=0, jitter=0),
    )
    request = ProviderRequest(
        model="gpt-6-astra", messages=[Message(role="user", content=[Text("hi")])],
    )
    if tools is not None:
        request.extra["tools"] = tools
    chunks = [c async for c in client.stream(request)]
    await http.aclose()
    return chunks


@pytest.mark.parametrize("code,etype,retriable", [
    ("server_is_overloaded", "service_unavailable_error", True),
    ("server_error", "server_error", True),
    ("rate_limit_exceeded", "rate_limit_error", True),
    ("slow_down", "invalid_request_error", True),
    ("invalid_prompt", "invalid_request_error", False),
    ("cyber_policy", "invalid_request_error", False),
    ("context_length_exceeded", "invalid_request_error", False),
])
def test_in_stream_errors_are_classified(code: str, etype: str, retriable: bool) -> None:
    for events in (
        [("response.created", {"response": {"id": "r"}}),
         ("response.failed", {"response": {"error": {"code": code, "type": etype}}})],
        # The bare SSE error event carries its fields at the top level.
        [("error", {"type": "error", "code": code, "message": code})],
    ):
        error = _consume_all(events)[-1]
        assert error.kind == "error"
        assert error.payload["retriable"] is retriable
        assert error.payload["code"] == code


def test_overload_class_has_a_small_budget_and_a_long_floor() -> None:
    verdict = classify_error({"code": "server_is_overloaded"})
    assert verdict.retriable is True
    assert verdict.max_retries == OVERLOAD_MAX_RETRIES <= 2
    assert verdict.backoff_floor >= 10.0
    assert classify_error({"code": "server_error"}).backoff_floor == 0.0
    assert classify_error({"code": "invalid_prompt"}).drop_sticky is False


async def test_overload_before_content_is_retried_then_succeeds(_sleeps: list[float]) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _sse(_CREATED + _failed("server_is_overloaded"))
        return _sse(_CREATED + _TEXT + _COMPLETED)

    chunks = await _run_client(handler)
    kinds = [c.kind for c in chunks]
    assert calls == 2
    assert kinds.count("message_start") == 1
    assert kinds[0] == "message_start"
    assert "error" not in kinds
    assert kinds[-1] == "message_stop"
    assert _sleeps == [OVERLOAD_BACKOFF_FLOOR_S]


async def test_overload_retries_stop_at_the_overload_budget(_sleeps: list[float]) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _sse(_CREATED + _failed("server_is_overloaded"))

    chunks = await _run_client(handler, retries=5)
    assert calls == 1 + OVERLOAD_MAX_RETRIES
    assert [c.kind for c in chunks] == ["message_start", "error"]
    assert chunks[-1].payload["retriable"] is True
    assert chunks[-1].payload["code"] == "server_is_overloaded"


async def test_server_error_before_content_uses_the_normal_policy(_sleeps: list[float]) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls <= 3:
            return _sse(_CREATED + _failed("server_error", "server_error"))
        return _sse(_CREATED + _TEXT + _COMPLETED)

    chunks = await _run_client(handler, retries=3)
    assert calls == 4
    assert [c.kind for c in chunks].count("message_start") == 1
    assert chunks[-1].kind == "message_stop"
    assert all(delay < OVERLOAD_BACKOFF_FLOOR_S for delay in _sleeps)


async def test_error_after_content_is_surfaced_not_retried(_sleeps: list[float]) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _sse(_CREATED + _TEXT + _failed("server_is_overloaded"))

    chunks = await _run_client(handler)
    assert calls == 1
    assert any(c.kind == "text_delta" for c in chunks)
    assert chunks[-1].kind == "error"
    assert chunks[-1].payload["retriable"] is True
    assert _sleeps == []


async def test_non_retriable_in_stream_error_is_terminal(_sleeps: list[float]) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _sse(_CREATED + _failed("cyber_policy", "invalid_request_error"))

    chunks = await _run_client(handler)
    assert calls == 1
    assert [c.kind for c in chunks] == ["message_start", "error"]
    assert chunks[-1].payload["retriable"] is False
    assert _sleeps == []


async def test_native_image_request_never_replays_an_in_stream_error(_sleeps: list[float]) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _sse(_CREATED + _failed("server_is_overloaded"))

    chunks = await _run_client(handler, tools=[{"type": "image_generation"}])
    assert calls == 1
    assert chunks[-1].kind == "error"


async def test_liveness_before_content_keeps_the_start_first_and_single(_sleeps: list[float]) -> None:
    # The reasoning item's provider_alive releases the held start, so the host
    # still sees acceptance first; the retry's own start is then dropped.
    calls = 0
    reasoning = _event("response.output_item.added", {"item": {"id": "rs_1", "type": "reasoning"}})

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _sse(_CREATED + reasoning + _failed("server_error", "server_error"))
        return _sse(_CREATED + _TEXT + _COMPLETED)

    chunks = await _run_client(handler)
    kinds = [c.kind for c in chunks]
    assert calls == 2
    assert kinds[:2] == ["message_start", "provider_alive"]
    assert kinds.count("message_start") == 1
    assert "error" not in kinds


class _DropAfter(httpx.AsyncByteStream):
    """A response body that sends ``body`` and then loses the connection."""

    def __init__(self, body: str) -> None:
        self._body = body.encode()

    async def __aiter__(self):
        yield self._body
        raise httpx.ReadError("connection reset by peer")

    async def aclose(self) -> None:
        pass


def _dropped(body: str) -> httpx.Response:
    return httpx.Response(
        200, stream=_DropAfter(body), headers={"content-type": "text/event-stream"},
    )


async def test_transport_drop_after_created_before_content_is_retried(
    _sleeps: list[float],
) -> None:
    """The held message_start is not content: a drop right after
    response.created is replayed, as the Anthropic lane does."""
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _dropped(_CREATED)
        return _sse(_CREATED + _TEXT + _COMPLETED)

    chunks = await _run_client(handler)
    kinds = [c.kind for c in chunks]
    assert calls == 2
    assert kinds.count("message_start") == 1
    assert kinds[0] == "message_start"
    assert "error" not in kinds
    assert kinds[-1] == "message_stop"


async def test_transport_drop_after_the_first_content_item_is_surfaced(
    _sleeps: list[float],
) -> None:
    calls = 0
    first_delta = _event("response.output_text.delta", {"item_id": "msg_1", "delta": "hi"})

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _dropped(_CREATED + first_delta)

    chunks = await _run_client(handler)
    assert calls == 1
    kinds = [c.kind for c in chunks]
    assert kinds[0] == "message_start"
    assert "text_delta" in kinds
    assert chunks[-1].kind == "error"
    assert chunks[-1].payload["retriable"] is False
    assert _sleeps == []


def _rate_limited(message: str) -> str:
    return _event("response.failed", {"response": {
        "id": "resp_1", "status": "failed",
        "error": {"code": "rate_limit_exceeded", "type": "rate_limit_error",
                  "message": message},
    }})


@pytest.mark.parametrize("message,expected", [
    ("Rate limit reached for gpt-6 on tokens per min. Please try again in 1.898s.", 1.898),
    ("Rate limit reached. Please try again in 250ms.", 0.25),
    # A hint past the long floor is capped at it.
    ("Rate limit reached. Please try again in 45s.", OVERLOAD_BACKOFF_FLOOR_S),
])
async def test_rate_limit_hint_drives_the_backoff(
    _sleeps: list[float], message: str, expected: float,
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _sse(_CREATED + _rate_limited(message))
        return _sse(_CREATED + _TEXT + _COMPLETED)

    chunks = await _run_client(handler)
    assert calls == 2
    assert _sleeps == [pytest.approx(expected)]
    assert chunks[-1].kind == "message_stop"


async def test_rate_limit_without_a_hint_uses_the_default_backoff(
    _sleeps: list[float],
) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return _sse(_CREATED + _rate_limited("Rate limit reached."))
        return _sse(_CREATED + _TEXT + _COMPLETED)

    await _run_client(handler)
    assert calls == 2
    # base_delay=0 and no jitter in _run_client's policy: the default is 0.
    assert _sleeps == [0]


# ─── routing identity (experiment E1 arms, FLUID_RESPONSES_ROUTING_ARM) ─────

import logging

from fluid.providers.base import TURN_MARKER_KEY, WireState
from fluid.providers.openai_responses.client import (
    API_URL,
    CODEX_RESPONSES_URL,
    ROUTING_ARM_ENV,
    TURN_STATE_HEADER,
)

_IDENTITY_HEADERS = ("session-id", "thread-id", "x-client-request-id", TURN_STATE_HEADER)
# What a Codex-backend request carried before the routing experiment existed.
_CODEX_BASE_HEADERS = {
    "host", "accept", "accept-encoding", "connection", "user-agent",
    "content-length", "content-type", "authorization",
    "chatgpt-account-id", "openai-beta", "originator",
}


class _CodexAuth:
    account_id = "acct_1"

    async def get_access_token(self) -> str:
        return "tok"

    async def on_401(self) -> None:  # pragma: no cover - not hit here
        pass


class _Recorder:
    """MockTransport handler: records each request, returns a turn-state token.

    Every response carries ``x-codex-turn-state: tok-<n>`` for the n-th
    request, the way the backend hands out a routing token.
    """

    def __init__(self, extra_headers: dict[str, str] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.extra_headers = extra_headers or {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return _sse(
            _CREATED + _TEXT + _COMPLETED,
            {TURN_STATE_HEADER: f"tok-{len(self.requests)}", **self.extra_headers},
        )

    def headers(self, n: int) -> dict[str, str]:
        return dict(self.requests[n].headers)


def _codex_client(recorder: _Recorder, arm: str | None, *, base_url: str = CODEX_RESPONSES_URL):
    http = httpx.AsyncClient(transport=httpx.MockTransport(recorder))
    client = OpenAIResponsesClient(
        auth=_CodexAuth(), base_url=base_url, http_client=http, routing_arm=arm,
    )
    return client, http


def _routed_request(sid: str, turn: str | None, wire: WireState | None) -> ProviderRequest:
    extra: dict[str, Any] = {"prompt_cache_key": sid}
    if turn is not None:
        extra[TURN_MARKER_KEY] = turn
    return ProviderRequest(
        model="gpt-6-astra", messages=[Message(role="user", content=[Text("hi")])],
        extra=extra, wire_state=wire,
    )


async def _send(client: OpenAIResponsesClient, request: ProviderRequest) -> None:
    async for _ in client.stream(request):
        pass


@pytest.mark.parametrize("arm", ["A", "B", "C", "D"])
async def test_routing_arm_decides_which_identity_headers_are_sent(
    arm: str, caplog: pytest.LogCaptureFixture,
) -> None:
    sid = "ses_x"
    recorder = _Recorder()
    client, http = _codex_client(recorder, arm)
    wire = WireState(session_id=sid)
    with caplog.at_level(logging.INFO, logger="fluid.providers.openai_responses.client"):
        # Turn 1 makes two calls, turn 2 one.
        for turn in ("turn_1", "turn_1", "turn_2"):
            await _send(client, _routed_request(sid, turn, wire))
    await http.aclose()

    assert len(recorder.requests) == 3, "a prewarm must not send a request"
    sent = [recorder.headers(i) for i in range(3)]
    # The exact header names per request: arm A sends today's set, B and D
    # add the three identity headers, C adds turn-state where it replays one.
    identity = set() if arm == "A" else {"session-id", "thread-id", "x-client-request-id"}
    replay = {1} if arm == "C" else set()
    assert [set(headers) for headers in sent] == [
        _CODEX_BASE_HEADERS | identity | ({TURN_STATE_HEADER} if i in replay else set())
        for i in range(3)
    ]
    if arm == "A":
        return
    # As Codex sends them: session-id, thread-id and x-client-request-id
    # all equal the prompt_cache_key for the main role, on every request.
    for headers in sent:
        assert headers["session-id"] == sid
        assert headers["thread-id"] == sid
        assert headers["x-client-request-id"] == sid
    turn_states = [headers.get(TURN_STATE_HEADER) for headers in sent]
    if arm == "C":
        # The first response of turn 1 returned tok-1: replayed within the
        # turn, dropped when the turn changes.
        assert turn_states == [None, "tok-1", None]
        assert wire.turn_marker == "turn_2" and wire.turn_state == "tok-3"
    else:
        assert turn_states == [None, None, None]
    prewarms = [r for r in caplog.records if "prewarm skipped" in r.getMessage()]
    assert len(prewarms) == (2 if arm == "D" else 0), "one prewarm per turn start"


@pytest.mark.parametrize("arm", ["A", "B", "C", "D"])
async def test_probe_routing_headers_keep_session_identity_and_drop_per_turn_headers(
    arm: str,
) -> None:
    # The cache probe hashes routing_headers(request) into the request
    # identity. Under arms B-D that must cover session-id and thread-id,
    # which are sent and stay fixed for the session, and leave out
    # x-client-request-id (a copy of thread-id) and x-codex-turn-state, which
    # changes on every turn: hashing that would read every turn start as a
    # routing change.
    from fluid.context.cache_probe import fingerprint_prefix, fingerprint_responses_request

    sid = "ses_x"
    recorder = _Recorder()
    client, http = _codex_client(recorder, arm)
    wire = WireState(session_id=sid)
    fingerprints = []
    for turn in ("turn_1", "turn_1", "turn_2"):
        request = _routed_request(sid, turn, wire)
        routing = client.routing_headers(request)
        fingerprint = fingerprint_prefix(
            system=request.system, tools=request.tools, messages=request.messages,
            routing_headers=routing,
        )
        fingerprint_responses_request(
            request, fingerprint, previous_count=0,
            endpoint=CODEX_RESPONSES_URL, routing_headers=routing,
        )
        fingerprints.append(fingerprint)
        await _send(client, request)
    await http.aclose()

    stable = {"session-id", "thread-id"} if arm != "A" else set()
    routing = client.routing_headers(_routed_request(sid, "turn_3", wire))
    assert set(routing) == {"chatgpt-account-id", "OpenAI-Beta", "originator"} | stable
    for name in stable:
        assert routing[name] == sid
    # Everything the probe hashes is also sent.
    sent = recorder.headers(2)
    assert all(sent[name.lower()] == value for name, value in routing.items())
    # A new turn and (arm C) a replayed turn-state token leave the probe's
    # request identity unchanged.
    assert len({f["request"] for f in fingerprints}) == 1


async def test_routing_arm_a_requests_are_byte_identical_to_the_pre_experiment_shape() -> None:
    # Arm A with a wire state and a turn marker sends exactly the bytes a
    # request without either sent before the experiment existed.
    recorder = _Recorder()
    client, http = _codex_client(recorder, "A")
    await _send(client, _routed_request("ses_x", "turn_1", WireState(session_id="ses_x")))
    legacy = ProviderRequest(
        model="gpt-6-astra", messages=[Message(role="user", content=[Text("hi")])],
        extra={"prompt_cache_key": "ses_x"},
    )
    await _send(client, legacy)
    await http.aclose()

    routed, plain = recorder.requests
    assert routed.content == plain.content
    assert routed.headers.raw == plain.headers.raw
    assert TURN_MARKER_KEY.encode() not in routed.content
    assert set(dict(routed.headers)) == _CODEX_BASE_HEADERS


async def test_client_retries_reuse_the_request_id_and_replay_the_turn_state(
    _sleeps: list[float],
) -> None:
    # Arm C. The first response of the turn hands out a token and then fails
    # before content; the client's own retry is the same request, so it keeps
    # its x-client-request-id and replays that token.
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = _CREATED + (
            _failed("server_error", "server_error") if len(requests) == 1
            else _TEXT + _COMPLETED
        )
        return _sse(body, {TURN_STATE_HEADER: f"tok-{len(requests)}"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client = OpenAIResponsesClient(
        auth=_CodexAuth(), base_url=CODEX_RESPONSES_URL, http_client=http,
        routing_arm="C", retry=RetryPolicy(max_retries=3, base_delay=0, jitter=0),
    )
    wire = WireState(session_id="ses_x")
    chunks = [c async for c in client.stream(_routed_request("ses_x", "turn_1", wire))]
    await http.aclose()

    assert chunks[-1].kind == "message_stop"
    first, retry = requests
    assert first.headers["x-client-request-id"] == retry.headers["x-client-request-id"]
    assert TURN_STATE_HEADER not in first.headers
    assert retry.headers[TURN_STATE_HEADER] == "tok-1"
    assert wire.turn_state == "tok-1"


def test_routing_arm_comes_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(ROUTING_ARM_ENV, raising=False)
    assert OpenAIResponsesClient(api_key="k")._routing_arm == "A"
    monkeypatch.setenv(ROUTING_ARM_ENV, "c")
    assert OpenAIResponsesClient(api_key="k")._routing_arm == "C"
    monkeypatch.setenv(ROUTING_ARM_ENV, "Z")
    assert OpenAIResponsesClient(api_key="k")._routing_arm == "A"
    assert OpenAIResponsesClient(api_key="k", routing_arm="B")._routing_arm == "B"


async def test_identity_headers_never_reach_the_public_api() -> None:
    recorder = _Recorder()
    client, http = _codex_client(recorder, "C", base_url=API_URL)
    wire = WireState(session_id="ses_x")
    for turn in ("turn_1", "turn_1"):
        await _send(client, _routed_request("ses_x", turn, wire))
    await http.aclose()
    for request in recorder.requests:
        assert not any(h in request.headers for h in _IDENTITY_HEADERS)
    assert wire.turn_state is None


async def test_turn_state_is_tracked_per_role_on_one_client() -> None:
    # The main loop and the memory learner share one Responses client. Each
    # role replays only the token its own turn received.
    sid = "ses_x"
    recorder = _Recorder()
    client, http = _codex_client(recorder, "C")
    main = WireState(session_id=sid, role="main")
    learner = WireState(session_id=sid, role="learner")

    await _send(client, _routed_request(sid, "turn_1", main))          # tok-1 → main
    await _send(client, _routed_request(sid, "learn_1", learner))      # tok-2 → learner
    await _send(client, _routed_request(sid, "turn_1", main))          # replays tok-1
    await _send(client, _routed_request(sid, "learn_1", learner))      # replays tok-2
    await _send(client, _routed_request(sid, "turn_1", None))          # no wire state
    await _send(client, _routed_request(sid, "turn_1", main))          # still tok-1
    await http.aclose()

    sent = [recorder.headers(i) for i in range(6)]
    assert [h.get(TURN_STATE_HEADER) for h in sent] == [
        None, None, "tok-1", "tok-2", None, "tok-1",
    ]
    assert [h.get("thread-id") for h in sent] == [
        sid, f"learner:{sid}", sid, f"learner:{sid}", sid, sid,
    ]
    # x-client-request-id follows thread-id, as Codex sends it.
    assert [h.get("x-client-request-id") for h in sent] == [
        h.get("thread-id") for h in sent
    ]
    assert main.turn_state == "tok-1"
    assert learner.turn_state == "tok-2"


async def test_usage_limit_headers_are_logged_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    recorder = _Recorder({
        "x-codex-primary-used-percent": "42",
        "x-codex-secondary-reset-after-seconds": "3600",
        "x-ratelimit-remaining-tokens": "9000",
    })
    client, http = _codex_client(recorder, "B")
    with caplog.at_level(logging.DEBUG, logger="fluid.providers.openai_responses.client"):
        await _send(client, _routed_request("ses_x", "turn_1", WireState(session_id="ses_x")))
    await http.aclose()

    (record,) = [r for r in caplog.records if "usage-limit headers" in r.getMessage()]
    message = record.getMessage()
    assert record.levelno == logging.DEBUG
    assert "arm=B" in message
    assert '"x-codex-primary-used-percent": "42"' in message
    assert '"x-ratelimit-remaining-tokens": "9000"' in message
    assert TURN_STATE_HEADER not in message


async def test_loop_hands_the_client_its_wire_state_and_turn_marker() -> None:
    loop, sid = await _loop_with_session(_NamedProvider("openai_responses"))
    try:
        first = loop._build_request([], turn_id="turn_1")
        second = loop._build_request([], turn_id="turn_2")
        assert first.extra[TURN_MARKER_KEY] == "turn_1"
        assert first.wire_state is second.wire_state
        assert first.wire_state.session_id == sid and first.wire_state.role == "main"
        # The marker is a signal to the client, never part of the body.
        assert TURN_MARKER_KEY not in build_request_body(first)
        assert build_request_body(first) == build_request_body(loop._build_request([]))
    finally:
        await loop.aclose()
    other, _ = await _loop_with_session(_NamedProvider("openrouter"))
    try:
        request = other._build_request([], turn_id="turn_1")
        assert request.wire_state is None
        assert TURN_MARKER_KEY not in request.extra
    finally:
        await other.aclose()


async def test_memory_learner_calls_carry_their_own_role_routing(tmp_path: Path) -> None:
    # Engine wiring, not a hand-built WireState: the loop binds the learner
    # that shares its Responses client, so a learner pass is sent with the
    # "learner:<sid>" cache key and the loop's "learner" wire state, and the
    # main loop keeps its own state.
    from fluid.codemode.continual import Synthesizer
    from fluid.codemode.journal import ToolJournal
    from fluid.codemode.skills import SkillStore

    recorder = _Recorder()
    client, http = _codex_client(recorder, "B")
    store = SkillStore(tmp_path)
    synth = Synthesizer(client, "gpt-6-astra", store)
    loop, sid = await _loop_with_session(client, journal=ToolJournal(store, synth))
    try:
        await synth._complete("learn from this")
        main = loop._build_request([], turn_id="turn_1")
        await _send(client, main)
    finally:
        await loop.aclose()
        await http.aclose()

    learner_call, main_call = recorder.requests
    assert _json.loads(learner_call.content)["prompt_cache_key"] == f"learner:{sid}"
    assert learner_call.headers["session-id"] == f"learner:{sid}"
    assert learner_call.headers["thread-id"] == f"learner:{sid}"
    assert learner_call.headers["x-client-request-id"] == f"learner:{sid}"
    assert main_call.headers["session-id"] == sid
    assert main_call.headers["thread-id"] == sid
    learner_state = loop._wire_states[(sid, "learner")]
    assert learner_state.role == "learner"
    assert learner_state is not loop._wire_states[(sid, "main")]


async def test_a_worker_loop_does_not_rebind_the_parent_learner(tmp_path: Path) -> None:
    from fluid.codemode.continual import Synthesizer
    from fluid.codemode.journal import ToolJournal
    from fluid.codemode.skills import SkillStore

    store = SkillStore(tmp_path)
    provider = _NamedProvider("openai_responses")
    synth = Synthesizer(provider, "gpt-6-astra", store)
    parent, parent_sid = await _loop_with_session(provider, journal=ToolJournal(store, synth))
    child, _ = await _loop_with_session(provider, journal=ToolJournal(store, synth))
    try:
        extra, wire = synth._request_routing()
        assert extra == {"prompt_cache_key": f"learner:{parent_sid}"}
        assert wire is parent._wire_states[(parent_sid, "learner")]
    finally:
        await child.aclose()
        await parent.aclose()
