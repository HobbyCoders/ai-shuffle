"""Round-7 audit fixes: what a turn survives, and what it stops.

Four failures from one audited production day, each of which ended a turn that
should have kept going or kept a turn going that should have ended:

* a transient Postgres blip refusing one ``ToolArgsDelta`` INSERT killed two
  turns and three workers;
* a degenerate generation streamed 60 KB / 224 KB / 423 KB of pure whitespace
  into a tool call's arguments for up to 14 minutes, until the user pressed
  Stop;
* an ``httpx.AsyncClient`` the host had closed made nine wake-driven turns die
  on a non-retriable ``RuntimeError``;
* turns that died carried ``cost_usd: null`` and turns that were interrupted
  carried ``{}``, so real spend in one session was never billed.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest

from fluid.context import compaction as compaction_module
from fluid.context.compaction import CompactionConfig, Compactor, SPAN_DIGEST
from fluid.context.trim import TrimConfig
from fluid.engine.native import (
    NativeLoop,
    ToolArgStreamGuard,
    _describe_engine_failure,
    _is_transient_error,
)
from fluid.events import (
    ErrorEvent,
    Interrupt,
    Interrupted,
    ProviderRetry,
    TextDelta,
    ThinkingDelta,
    ToolArgsDelta,
    ToolResult,
    ToolUse,
    TurnComplete,
    Usage,
    UserTurn,
)
from fluid.messages import Message, Text
from fluid.permissions.engine import PermissionEngine
from fluid.providers._retry import RetryPolicy
from fluid.providers._transport import (
    StreamDeadlineExceeded,
    is_closed_client_error,
    with_deadline,
)
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.providers.openai_responses.client import OpenAIResponsesClient
from fluid.providers.openrouter.client import OpenRouterClient
from fluid.store.base import MemoryStore, SessionNode
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

MODEL = "claude-sonnet-5"  # priced by the shared table, so cost is assertable


# ─── scaffolding ─────────────────────────────────────────────────────────────


class _Scripted:
    """Yields one prepared chunk list per model call, repeating the last."""

    name = "anthropic"
    caps = ProviderCaps()

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


def _text_call(text: str = "done") -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 1200}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk(
            "usage",
            {
                "usage": {"input_tokens": 1200, "output_tokens": 40},
                "stop_reason": "end_turn",
            },
        ),
        StreamChunk("message_stop", {}),
    ]


def _tool_call(fragments: list[str], *, name: str = "Write") -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 1200}}),
        StreamChunk(
            "block_start",
            {
                "index": 0, "block_type": "tool_use",
                "tool_name": name, "tool_use_id": "call_1",
            },
        ),
        *[
            StreamChunk("tool_args_delta", {"index": 0, "partial_json": part})
            for part in fragments
        ],
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk(
            "usage",
            {
                "usage": {"input_tokens": 1200, "output_tokens": 90},
                "stop_reason": "tool_use",
            },
        ),
        StreamChunk("message_stop", {}),
    ]


def _runaway_call(*, closed: bool = True, pad: int = 12_000) -> list[StreamChunk]:
    """A valid payload, then nothing but whitespace, forever.

    ``closed=False`` reproduces session a702a9f8, whose object never got its
    closing brace — the guard has to fire on that too.
    """
    body = '{"file_path":"/a.txt","content":"hello"}' if closed else '{"file_path":"/a.txt'
    return _tool_call([body, *[" " * 500 for _ in range(pad // 500)]])


def _write_registry() -> ToolRegistry:
    async def _wrote(input: dict, _ctx: ToolCtx) -> ToolOutcome:
        return ToolOutcome(text=f"wrote {len(input.get('content', ''))} bytes")

    registry = ToolRegistry()
    registry.register_builtin(
        ToolSpec(
            name="Write", description="Write a file.",
            input_schema={"type": "object", "properties": {}}, source="builtin",
        ),
        _wrote,
    )
    return registry


class _RefusingStore(MemoryStore):
    """A store that refuses to persist events of the given classes."""

    def __init__(self, *refuse: type, error: type[Exception] = RuntimeError) -> None:
        super().__init__()
        self._refuse = refuse
        self._error = error
        self.refused = 0

    async def append_event(self, session_id: str, event: Any) -> None:
        if isinstance(event, self._refuse):
            self.refused += 1
            raise self._error("connection to server failed: Address already in use")
        await super().append_event(session_id, event)


class _OperationalError(Exception):
    """Stands in for psycopg2/sqlite3 ``OperationalError`` (matched by name)."""


async def _run(loop: NativeLoop, prompt: str = "go") -> list[Any]:
    await loop.submit(UserTurn(content=prompt))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)
    return loop.bus.log(loop.session_id)


# ─── a store failure must not kill a turn ────────────────────────────────────


async def test_a_refused_delta_insert_lets_the_turn_finish() -> None:
    """One throwaway ToolArgsDelta row is not worth a turn: it is fully
    reconstructible from the assistant message the turn goes on to store."""
    store = _RefusingStore(ToolArgsDelta, TextDelta, ThinkingDelta)
    provider = _Scripted([
        _tool_call(['{"file_path":"/a.txt","content":"hi"}']),
        _text_call(),
    ])
    loop = NativeLoop(
        provider=provider, store=store, model=MODEL,
        registry=_write_registry(), permissions=PermissionEngine("never_ask"),
    )
    try:
        events = await _run(loop)
    finally:
        await loop.aclose()

    assert store.refused > 0
    assert loop.dropped_delta_events == store.refused
    assert not [e for e in events if isinstance(e, ErrorEvent)]
    done = next(e for e in events if isinstance(e, TurnComplete))
    assert done.stop_reason == "end_turn"
    # The tool still ran, and the deltas still reached live clients.
    result = next(e for e in events if isinstance(e, ToolResult))
    assert not result.is_error
    assert [e for e in events if isinstance(e, ToolArgsDelta)]


async def test_a_refused_load_bearing_insert_still_ends_the_turn() -> None:
    """Losing a ToolUse row really does corrupt the record, so it surfaces."""
    store = _RefusingStore(ToolUse)
    provider = _Scripted([_tool_call(['{"file_path":"/a.txt","content":"hi"}'])])
    loop = NativeLoop(
        provider=provider, store=store, model=MODEL,
        registry=_write_registry(), permissions=PermissionEngine("never_ask"),
    )
    try:
        events = await _run(loop)
    finally:
        await loop.aclose()

    error = next(e for e in events if isinstance(e, ErrorEvent))
    assert not any(isinstance(e, TurnComplete) for e in events)
    assert loop.dropped_delta_events == 0
    assert "RuntimeError" in error.message


async def test_a_driver_error_ends_the_turn_as_retriable_and_billed() -> None:
    """The catch-all used to ship ``repr(exc)`` with ``retriable`` at its False
    default and no usage, so a transient DB blip read as a permanent failure
    that cost nothing."""
    store = _RefusingStore(Usage, error=_OperationalError)
    provider = _Scripted([_text_call()])
    loop = NativeLoop(provider=provider, store=store, model=MODEL)
    try:
        events = await _run(loop)
    finally:
        await loop.aclose()

    error = next(e for e in events if isinstance(e, ErrorEvent))
    assert error.retriable is True
    assert "transient" in error.message and "resume" in error.message.lower()
    assert not error.message.startswith("_OperationalError(")
    # The call that completed before the store gave out is still billed.
    assert error.num_model_calls == 1
    assert error.usage["input_tokens"] == 1200
    assert error.cost_usd is not None and error.cost_usd > 0


@pytest.mark.parametrize(
    "exc",
    [
        OSError(10048, "Address already in use"),
        ConnectionResetError(),
        TimeoutError(),
        _OperationalError("connection closed"),
    ],
)
def test_transport_and_driver_failures_are_classified_transient(exc) -> None:
    assert _is_transient_error(exc)


def test_a_bug_in_a_handler_is_not_classified_transient() -> None:
    assert not _is_transient_error(KeyError("tool_use_id"))
    described = _describe_engine_failure(KeyError("x"), transient=False)
    assert "unexpected harness error" in described


def test_a_wrapped_socket_error_is_still_transient() -> None:
    try:
        try:
            raise OSError(10055, "No buffer space available")
        except OSError as cause:
            raise _OperationalError("could not connect") from cause
    except _OperationalError as exc:
        assert _is_transient_error(exc)


# ─── runaway tool-argument streams ───────────────────────────────────────────


async def test_a_whitespace_runaway_is_abandoned_and_the_call_retried() -> None:
    provider = _Scripted([_runaway_call(), _text_call("recovered")])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model=MODEL,
        registry=_write_registry(), permissions=PermissionEngine("never_ask"),
        tool_arg_guard=ToolArgStreamGuard(stall_bytes=4_000, stall_seconds=None),
    )
    try:
        events = await _run(loop)
    finally:
        await loop.aclose()

    retry = next(e for e in events if isinstance(e, ProviderRetry))
    assert "runaway tool-argument stream" in retry.reason
    assert provider.calls == 2
    # Nothing was dispatched, and no ToolUse was left without a ToolResult.
    assert not [e for e in events if isinstance(e, ToolUse)]
    done = next(e for e in events if isinstance(e, TurnComplete))
    assert done.stop_reason == "end_turn"
    # The abandoned call was billed rather than recorded as free.
    aborted = next(
        e for e in events
        if isinstance(e, Usage) and e.raw.get("tool_arg_stream_aborted")
    )
    assert aborted.output_tokens > 0
    assert "whitespace" in aborted.raw["tool_arg_stream_aborted"]


async def test_an_unclosed_object_padded_with_whitespace_also_trips() -> None:
    """Session a702a9f8 closed its string and then padded forever without ever
    emitting the closing brace, so "the JSON is complete" cannot be the test."""
    provider = _Scripted([_runaway_call(closed=False), _text_call("recovered")])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model=MODEL,
        registry=_write_registry(), permissions=PermissionEngine("never_ask"),
        tool_arg_guard=ToolArgStreamGuard(stall_bytes=4_000, stall_seconds=None),
    )
    try:
        events = await _run(loop)
    finally:
        await loop.aclose()

    assert [e for e in events if isinstance(e, ProviderRetry)]
    assert provider.calls == 2


async def test_a_second_runaway_becomes_a_tool_error_the_model_can_act_on() -> None:
    provider = _Scripted([_runaway_call(), _runaway_call(), _text_call("gave up")])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model=MODEL,
        registry=_write_registry(), permissions=PermissionEngine("never_ask"),
        tool_arg_guard=ToolArgStreamGuard(stall_bytes=4_000, stall_seconds=None),
    )
    try:
        events = await _run(loop)
    finally:
        await loop.aclose()

    uses = [e for e in events if isinstance(e, ToolUse)]
    results = [e for e in events if isinstance(e, ToolResult)]
    assert len(uses) == len(results) == 1  # every use answered
    assert uses[0].input == {}
    assert results[0].is_error
    assert "was not run" in results[0].output
    assert "reissue the call" in results[0].output
    assert "wrote" not in results[0].output  # the handler never ran
    assert provider.calls == 3
    assert next(e for e in events if isinstance(e, TurnComplete))


async def test_the_total_byte_cap_stops_arguments_that_never_end() -> None:
    """Content that keeps coming is the other runaway shape: no whitespace run
    to watch, just a payload that cannot plausibly still be honest."""
    fragments = ['{"file_path":"/a.txt","content":"'] + ["abcd" * 64] * 40
    provider = _Scripted([_tool_call(fragments), _text_call("recovered")])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model=MODEL,
        registry=_write_registry(), permissions=PermissionEngine("never_ask"),
        tool_arg_guard=ToolArgStreamGuard(max_bytes=2_000),
    )
    try:
        events = await _run(loop)
    finally:
        await loop.aclose()

    retry = next(e for e in events if isinstance(e, ProviderRetry))
    assert "runaway" in retry.reason
    aborted = next(
        e for e in events
        if isinstance(e, Usage) and e.raw.get("tool_arg_stream_aborted")
    )
    assert "2000 bytes" in aborted.raw["tool_arg_stream_aborted"]


class _SlowRunaway:
    """Whitespace that arrives steadily but slowly: too little to hit any byte
    cap, and it would never stop."""

    name = "anthropic"
    caps = ProviderCaps()

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        if self.calls > 1:
            for chunk in _text_call("recovered"):
                yield chunk
            return
        yield StreamChunk("message_start", {"usage": {"input_tokens": 1200}})
        yield StreamChunk(
            "block_start",
            {
                "index": 0, "block_type": "tool_use",
                "tool_name": "Write", "tool_use_id": "call_1",
            },
        )
        yield StreamChunk(
            "tool_args_delta",
            {"index": 0, "partial_json": '{"file_path":"/a.txt","content":"hi"}'},
        )
        while True:
            yield StreamChunk(
                "tool_args_delta", {"index": 0, "partial_json": " " * 300}
            )
            await asyncio.sleep(0.005)


async def test_a_slow_whitespace_runaway_trips_the_clock_not_the_byte_cap() -> None:
    provider = _SlowRunaway()
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model=MODEL,
        registry=_write_registry(), permissions=PermissionEngine("never_ask"),
        tool_arg_guard=ToolArgStreamGuard(
            max_bytes=None, stall_bytes=None, stall_seconds=0.05
        ),
    )
    try:
        events = await _run(loop)
    finally:
        await loop.aclose()

    aborted = next(
        e for e in events
        if isinstance(e, Usage) and e.raw.get("tool_arg_stream_aborted")
    )
    assert "nothing but whitespace arrived for 0.05s" in (
        aborted.raw["tool_arg_stream_aborted"]
    )
    assert provider.calls == 2
    assert next(e for e in events if isinstance(e, TurnComplete))


async def test_the_shipped_defaults_catch_the_audited_shape() -> None:
    """bb10c865 streamed ~10.9 KB of tabs and spaces after a complete call and
    ran for 224 s; the default 8 KB whitespace budget ends it."""
    provider = _Scripted([_runaway_call(pad=11_000), _text_call("recovered")])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model=MODEL,
        registry=_write_registry(), permissions=PermissionEngine("never_ask"),
    )
    try:
        events = await _run(loop)
    finally:
        await loop.aclose()

    assert loop.tool_arg_guard == ToolArgStreamGuard(
        max_bytes=256 * 1024, stall_bytes=8 * 1024, stall_seconds=20.0
    )
    assert [e for e in events if isinstance(e, ProviderRetry)]
    assert provider.calls == 2


async def test_an_ordinary_call_with_indentation_is_left_alone() -> None:
    """The guard has to be invisible to honest calls: a code payload streams
    plenty of whitespace-only fragments, just never thousands in a row."""
    fragments = ['{"file_path":"/a.py","content":"def f():']
    for i in range(60):
        fragments.extend(["\\n    ", f"line{i}()"])
    fragments.append('"}')
    provider = _Scripted([_tool_call(fragments), _text_call()])
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model=MODEL,
        registry=_write_registry(), permissions=PermissionEngine("never_ask"),
    )
    try:
        events = await _run(loop)
    finally:
        await loop.aclose()

    assert not [e for e in events if isinstance(e, ProviderRetry)]
    result = next(e for e in events if isinstance(e, ToolResult))
    assert not result.is_error


# ─── an abandoned call still costs money ─────────────────────────────────────


class _Hanging:
    """Streams a little, then stops answering — the shape a Stop press meets."""

    name = "anthropic"
    caps = ProviderCaps()

    def __init__(self) -> None:
        self.streaming = asyncio.Event()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        yield StreamChunk("message_start", {"usage": {}})
        yield StreamChunk("block_start", {"index": 0, "block_type": "text"})
        yield StreamChunk("text_delta", {"index": 0, "text": "x " * 600})
        self.streaming.set()
        await asyncio.Event().wait()  # never returns


async def test_an_interrupt_mid_stream_bills_an_estimate_not_zero() -> None:
    """``_partial_usage`` snapshots totals that are only folded in after a call
    completes, so an interrupt used to report ``usage: {}`` and
    ``cost_usd: 0.0`` for a prompt and an output the provider had charged
    for."""
    provider = _Hanging()
    loop = NativeLoop(provider=provider, store=MemoryStore(), model=MODEL)
    try:
        await loop.submit(UserTurn(content="write a long thing"))
        await asyncio.wait_for(provider.streaming.wait(), timeout=5)
        await loop.submit(Interrupt())
        await asyncio.wait_for(loop.wait_turn(), timeout=10)
        events = loop.bus.log(loop.session_id)
    finally:
        await loop.aclose()

    stopped = next(e for e in events if isinstance(e, Interrupted))
    assert stopped.num_model_calls == 1
    assert stopped.usage["output_tokens"] > 0
    assert stopped.usage["input_tokens"] > 0
    assert stopped.cost_usd is not None and stopped.cost_usd > 0
    billed = next(e for e in events if isinstance(e, Usage))
    assert billed.raw["output_tokens_estimated"] is True
    assert billed.raw["input_tokens_estimated"] is True


# ─── whole-stream deadline ───────────────────────────────────────────────────


async def test_with_deadline_gives_up_on_a_stream_that_never_stops() -> None:
    async def forever() -> AsyncIterator[int]:
        while True:
            await asyncio.sleep(0.01)
            yield 1

    seen = 0
    with pytest.raises(StreamDeadlineExceeded):
        async for _ in with_deadline(forever(), 0.05):
            seen += 1
    assert seen > 0  # it was streaming happily the whole time


async def test_with_deadline_passes_a_finite_stream_through() -> None:
    async def three() -> AsyncIterator[int]:
        for i in range(3):
            yield i

    assert [i async for i in with_deadline(three(), 5.0)] == [0, 1, 2]
    assert [i async for i in with_deadline(three(), None)] == [0, 1, 2]


class _EndlessStream(httpx.AsyncByteStream):
    """An SSE body that keeps arriving, so no per-read timeout ever fires."""

    def __init__(self, chunk: bytes) -> None:
        self._chunk = chunk

    async def __aiter__(self) -> AsyncIterator[bytes]:
        while True:
            yield self._chunk
            await asyncio.sleep(0.005)


def _endless_handler(chunk: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            stream=_EndlessStream(chunk),
            headers={"content-type": "text/event-stream"},
        )

    return handler


async def test_the_responses_client_abandons_a_stream_past_its_deadline() -> None:
    body = (
        b"event: response.function_call_arguments.delta\n"
        b'data: {"type":"response.function_call_arguments.delta",'
        b'"item_id":"fc_1","delta":"   "}\n\n'
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(_endless_handler(body)))
    client = OpenAIResponsesClient(
        api_key="sk-test", http_client=http, stream_deadline=0.2
    )
    request = ProviderRequest(
        model="gpt-5.6-sol", messages=[Message(role="user", content=[Text("hi")])]
    )
    chunks = [c async for c in client.stream(request)]
    await http.aclose()

    error = chunks[-1]
    assert error.kind == "error"
    assert "whole-stream deadline" in error.payload["message"]
    assert error.payload["retriable"] is True


async def test_chat_completions_abandons_a_stream_past_its_deadline() -> None:
    body = (
        b'data: {"choices":[{"delta":{"content":" "},"index":0}]}\n\n'
    )
    http = httpx.AsyncClient(transport=httpx.MockTransport(_endless_handler(body)))
    client = OpenRouterClient(
        api_key="or-test", http_client=http, stream_deadline=0.2,
        retry=RetryPolicy(max_retries=0, base_delay=0, jitter=0),
    )
    request = ProviderRequest(
        model="anthropic/claude-sonnet-5",
        messages=[Message(role="user", content=[Text("hi")])],
    )
    chunks = [c async for c in client.stream(request)]
    await http.aclose()

    error = chunks[-1]
    assert error.kind == "error"
    assert "whole-stream deadline" in error.payload["message"]
    assert error.payload["retriable"] is True


# ─── a closed injected client ────────────────────────────────────────────────


SSE_OK = (
    b"event: response.output_text.delta\n"
    b'data: {"type":"response.output_text.delta","item_id":"msg_1","delta":"hi"}\n\n'
    b"event: response.completed\n"
    b'data: {"type":"response.completed","response":{"usage":'
    b'{"input_tokens":10,"output_tokens":2},"output":[]}}\n\n'
)

CHAT_OK = (
    b'data: {"choices":[{"delta":{"content":"hi"},"index":0}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}],'
    b'"usage":{"prompt_tokens":10,"completion_tokens":2}}\n\n'
    b"data: [DONE]\n\n"
)


def _ok_handler(payload: bytes):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=payload, headers={"content-type": "text/event-stream"}
        )

    return handler


@pytest.fixture
def mock_clients(monkeypatch):
    """Make every client the code builds for itself talk to a mock transport."""
    real = httpx.AsyncClient
    built: list[httpx.AsyncClient] = []

    def factory(payload: bytes):
        def build(*_args, **kwargs):
            client = real(
                transport=httpx.MockTransport(_ok_handler(payload)),
                timeout=kwargs.get("timeout"),
            )
            built.append(client)
            return client

        return build

    def install(payload: bytes) -> list[httpx.AsyncClient]:
        monkeypatch.setattr(httpx, "AsyncClient", factory(payload))
        return built

    return install


class _DeadClient:
    """Reports itself open, then raises httpx's closed-client RuntimeError."""

    is_closed = False

    def stream(self, *_args, **_kwargs):
        raise RuntimeError("Cannot send a request, as the client has been closed.")


def test_httpx_closed_client_errors_are_recognized() -> None:
    assert is_closed_client_error(
        RuntimeError("Cannot send a request, as the client has been closed.")
    )
    assert not is_closed_client_error(RuntimeError("something else entirely"))
    assert not is_closed_client_error(ValueError("closed"))


async def test_a_closed_injected_client_is_replaced_not_fatal(mock_clients) -> None:
    """A host that closes its client between calls (session eviction, pool
    teardown) killed nine wake-driven turns; the client now rebuilds."""
    closed = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler(SSE_OK)))
    await closed.aclose()
    mock_clients(SSE_OK)

    client = OpenAIResponsesClient(api_key="sk-test", http_client=closed)
    request = ProviderRequest(
        model="gpt-5.6-sol", messages=[Message(role="user", content=[Text("hi")])]
    )
    chunks = [c async for c in client.stream(request)]

    assert any(c.kind == "text_delta" for c in chunks)
    assert not [c for c in chunks if c.kind == "error"]
    assert client._http is None  # the dead client is not consulted again


async def test_a_closed_client_error_mid_send_is_retried(mock_clients) -> None:
    mock_clients(SSE_OK)
    client = OpenAIResponsesClient(
        api_key="sk-test", http_client=_DeadClient(),
        retry=RetryPolicy(max_retries=2, base_delay=0, jitter=0),
    )
    request = ProviderRequest(
        model="gpt-5.6-sol", messages=[Message(role="user", content=[Text("hi")])]
    )
    chunks = [c async for c in client.stream(request)]

    assert not [c for c in chunks if c.kind == "error"]
    assert any(c.kind == "text_delta" for c in chunks)


async def test_an_unretriable_closed_client_is_reported_as_retriable() -> None:
    """With no retry budget the failure still has to be labelled honestly: the
    turn is not doomed, its transport is."""
    client = OpenAIResponsesClient(
        api_key="sk-test", http_client=_DeadClient(),
        retry=RetryPolicy(max_retries=0, max_transport_retries=0),
    )
    request = ProviderRequest(
        model="gpt-5.6-sol", messages=[Message(role="user", content=[Text("hi")])]
    )
    chunks = [c async for c in client.stream(request)]

    assert len(chunks) == 1
    assert chunks[0].kind == "error"
    assert chunks[0].payload["retriable"] is True
    assert "has been closed" in chunks[0].payload["message"]


async def test_chat_completions_recovers_from_a_closed_client(mock_clients) -> None:
    mock_clients(CHAT_OK)
    client = OpenRouterClient(
        api_key="or-test", http_client=_DeadClient(),
        retry=RetryPolicy(max_retries=2, max_transport_retries=2,
                          base_delay=0, jitter=0),
    )
    request = ProviderRequest(
        model="anthropic/claude-sonnet-5",
        messages=[Message(role="user", content=[Text("hi")])],
    )
    chunks = [c async for c in client.stream(request)]

    assert any(c.kind == "text_delta" for c in chunks)
    assert not [c for c in chunks if c.kind == "error"]


async def test_chat_completions_drops_a_closed_injected_client(mock_clients) -> None:
    closed = httpx.AsyncClient(transport=httpx.MockTransport(_ok_handler(CHAT_OK)))
    await closed.aclose()
    mock_clients(CHAT_OK)

    client = OpenRouterClient(api_key="or-test", http_client=closed)
    request = ProviderRequest(
        model="anthropic/claude-sonnet-5",
        messages=[Message(role="user", content=[Text("hi")])],
    )
    chunks = [c async for c in client.stream(request)]

    assert any(c.kind == "text_delta" for c in chunks)
    assert client._http is None


# ─── compaction that reclaims nothing ────────────────────────────────────────


def _node(nid: str, role: str, blocks: list) -> SessionNode:
    return SessionNode(
        node_id=nid, parent_id=None, message=Message(role=role, content=blocks)
    )


def _history(turns: int) -> list[SessionNode]:
    nodes: list[SessionNode] = []
    for t in range(turns):
        # Make the full batch worthwhile under the optional 5% reclaim floor;
        # these cases isolate whether each epoch fold itself frees tokens.
        nodes.extend([
            _node(f"u{t}", "user", [Text(f"topic-{t} " + "pad " * 500)]),
            _node(f"a{t}", "assistant", [Text(f"answer-{t} " + "pad " * 500)]),
        ])
    return nodes


def _cfg(**kw) -> CompactionConfig:
    base: dict[str, Any] = dict(
        trigger_tokens=1_000, target_ratio=0.5, keep_recent_turns=2,
        trim=TrimConfig(trigger_tokens=1_000, keep_recent_tool_results=2,
                        min_result_chars=500),
        max_resident_span_digests=3, epoch_fold_spans=3,
    )
    base.update(kw)
    return CompactionConfig(**base)


def test_a_tier3_fold_that_frees_nothing_is_rolled_back(monkeypatch) -> None:
    """Two tier-3 escalations in one audited session reported
    ``reclaimed_chars=0`` — a cache break and a compaction event bought
    nothing."""
    monkeypatch.setattr(
        compaction_module,
        "render_epoch_stub",
        lambda spans, config: Message(role="user", content=[Text("stub " * 4_000)]),
    )
    nodes = _history(12)
    compactor = Compactor(config=_cfg())
    _rendered, promos = compactor.apply(nodes)

    assert not [p for p in promos if p.tier == 3]
    # The digests the fold would have consumed are still digests, so the next
    # batch can try again once folding would actually help.
    assert SPAN_DIGEST in compactor._digested.values()
    assert not compactor._epoch_by_start


def test_a_tier3_fold_that_frees_tokens_is_kept() -> None:
    nodes = _history(12)
    compactor = Compactor(config=_cfg())
    _rendered, promos = compactor.apply(nodes)
    folds = [p for p in promos if p.tier == 3]

    assert folds and all(p.reclaimed_chars > 0 for p in folds)


def test_an_overflow_rung_that_frees_nothing_falls_through(monkeypatch) -> None:
    """Same rule on the overflow ladder: a rung that changed the view's shape
    without shrinking it must not be reported as progress the retry does not
    have."""
    monkeypatch.setattr(
        compaction_module,
        "render_epoch_stub",
        lambda spans, config: Message(role="user", content=[Text("stub " * 4_000)]),
    )
    nodes = _history(12)
    compactor = Compactor(config=_cfg())
    compactor.apply(nodes)  # get some digests resident
    promos = compactor.escalate(nodes, context_window=None)

    assert all(p.reclaimed_chars > 0 for p in promos)
    assert not [p for p in promos if p.tier == 3]
