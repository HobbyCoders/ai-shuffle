"""ProviderAlive: a silent but accepted stream is visible to the host (#1418).

A reasoning model whose reasoning is encrypted-only streams nothing the engine
could publish until the reasoning item ends. These tests pin down what the
Responses provider and the engine now say during that stretch, and that it
stays bounded and out of the conversation.
"""

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx

from fluid.engine.native import NativeLoop
from fluid.events import (
    BlockStart,
    ProviderAlive,
    TextDelta,
    ThinkingDelta,
    UserTurn,
)
from fluid.events.model import event_from_dict, event_to_dict
from fluid.messages import Message, Reasoning, Text
from fluid.providers._retry import RetryPolicy
from fluid.providers._sse import KEEPALIVE_EVENT, sse_events
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.providers.openai_responses.client import OpenAIResponsesClient
from fluid.providers.openai_responses.serialize import ResponsesStreamNormalizer
from fluid.store.base import RECONSTRUCTIBLE_EVENTS, MemoryStore

GOLDEN_DIR = Path(__file__).parent.parent / "golden"
ENCRYPTED_ONLY = GOLDEN_DIR / "openai_responses_encrypted_reasoning_only.sse"


async def _lines(text: str) -> AsyncIterator[str]:
    for line in text.splitlines(keepends=True):
        yield line


async def _normalized(path: Path, *, keepalives: bool = True) -> list[StreamChunk]:
    normalizer = ResponsesStreamNormalizer()
    out: list[StreamChunk] = []
    async for name, data in sse_events(_lines(path.read_text()), keepalives=keepalives):
        out.extend(normalizer.consume(name, data))
    return out


def _client_for(body: bytes) -> tuple[OpenAIResponsesClient, httpx.AsyncClient]:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body, headers={"content-type": "text/event-stream"}
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return OpenAIResponsesClient(api_key="sk-test", http_client=http), http


async def _run_turn(provider, **kw) -> tuple[NativeLoop, str, MemoryStore]:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(
        provider=provider, store=store, model="gpt-5.6-luna", session_id=sid,
        max_continuations_per_turn=0, **kw,
    )
    await loop.submit(UserTurn(content="think hard"))
    await asyncio.wait_for(loop.wait_turn(), timeout=10)
    return loop, sid, store


# ─── SSE parsing ─────────────────────────────────────────────────────────────


async def test_comment_lines_are_still_dropped_by_default() -> None:
    names = [name async for name, _ in sse_events(_lines(ENCRYPTED_ONLY.read_text()))]
    assert KEEPALIVE_EVENT not in names


async def test_comment_lines_surface_as_keepalives_when_asked() -> None:
    names = [
        name async for name, _ in
        sse_events(_lines(ENCRYPTED_ONLY.read_text()), keepalives=True)
    ]
    assert names.count(KEEPALIVE_EVENT) == 3


async def test_a_comment_inside_a_frame_does_not_break_the_frame() -> None:
    text = 'event: response.created\n: ping\ndata: {"response": {"id": "r"}}\n\n'
    got = [pair async for pair in sse_events(_lines(text), keepalives=True)]
    assert got == [(KEEPALIVE_EVENT, {}), ("response.created", {"response": {"id": "r"}})]


# ─── Responses normalizer, from the recorded wire shape ──────────────────────


async def test_encrypted_only_reasoning_reports_liveness_before_any_block() -> None:
    chunks = await _normalized(ENCRYPTED_ONLY)
    kinds = [c.kind for c in chunks]
    assert kinds == [
        "message_start",
        "provider_alive",  # reasoning output_item.added
        "provider_alive", "provider_alive", "provider_alive",  # heartbeats
        "block_start", "block_stop",  # the encrypted item, only when done
        "block_start", "text_delta", "block_stop",
        "usage", "message_stop",
    ]
    signals = [c.payload["signal"] for c in chunks if c.kind == "provider_alive"]
    assert signals == ["reasoning", "keepalive", "keepalive", "keepalive"]
    # The encrypted envelope still rides the reasoning block's stop.
    stop = next(c for c in chunks if c.kind == "block_stop")
    assert stop.payload["block"]["encrypted_content"] == "ENCRYPTED_ONLY_BLOB"


async def test_without_the_new_signals_the_reasoning_stretch_is_empty() -> None:
    # What the engine had to go on before: message_start, then nothing at all
    # until the reasoning item finished.
    chunks = [c for c in await _normalized(ENCRYPTED_ONLY, keepalives=False)
              if c.kind != "provider_alive"]
    assert [c.kind for c in chunks[:2]] == ["message_start", "block_start"]


async def test_client_yields_the_liveness_chunks_end_to_end() -> None:
    client, http = _client_for(ENCRYPTED_ONLY.read_bytes())
    request = ProviderRequest(
        model="gpt-5.6-luna", messages=[Message(role="user", content=[Text("go")])]
    )
    try:
        chunks = [c async for c in client.stream(request)]
    finally:
        await http.aclose()
    first_block = next(i for i, c in enumerate(chunks) if c.kind == "block_start")
    alive = [i for i, c in enumerate(chunks) if c.kind == "provider_alive"]
    assert len(alive) == 4
    assert all(i < first_block for i in alive)


class _KeepaliveThenDrop(httpx.AsyncByteStream):
    """A heartbeat, then the connection dies before the response starts."""

    async def __aiter__(self):
        yield b": keepalive\n\n"
        raise httpx.RemoteProtocolError("peer closed connection")

    async def aclose(self) -> None:
        pass


async def test_a_heartbeat_alone_does_not_make_a_transport_failure_final() -> None:
    # Only delivered content makes a mid-stream failure non-retriable. A
    # heartbeat before response.created is not content.
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(200, stream=_KeepaliveThenDrop())
        return httpx.Response(200, content=ENCRYPTED_ONLY.read_bytes())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = OpenAIResponsesClient(
            api_key="sk-test", http_client=http,
            retry=RetryPolicy(max_retries=1, base_delay=0),
        )
        chunks = [c async for c in client.stream(ProviderRequest(model="m", messages=[]))]
    assert len(attempts) == 2
    assert chunks[-1].kind == "message_stop"


# ─── engine: publish, bound, keep out of the conversation ────────────────────


async def test_engine_publishes_one_alive_event_before_the_reasoning_block() -> None:
    client, http = _client_for(ENCRYPTED_ONLY.read_bytes())
    try:
        loop, sid, store = await _run_turn(client)
        events = loop.bus.log(sid)
    finally:
        await http.aclose()
    alive = [e for e in events if isinstance(e, ProviderAlive)]
    # response.created, the reasoning item and three heartbeats all arrived
    # inside one interval: the first publishes, the rest are absorbed.
    assert [e.signal for e in alive] == ["accepted"]
    first_block = next(i for i, e in enumerate(events) if isinstance(e, BlockStart))
    assert events.index(alive[0]) < first_block
    assert not any(isinstance(e, (TextDelta, ThinkingDelta))
                   for e in events[: events.index(alive[0])])

    # Nothing about it reaches the stored conversation.
    nodes = await store.load_path(sid)
    reply = next(n.message for n in reversed(nodes)
                 if n.message is not None and n.message.role == "assistant")
    # The encrypted envelope round-trips once and the answer is the only text.
    blobs = [b.encrypted_content for b in reply.content if isinstance(b, Reasoning)]
    assert blobs == ["ENCRYPTED_ONLY_BLOB"]
    assert [b.text for b in reply.content if isinstance(b, Text)] == ["Done."]
    await loop.aclose()


async def test_engine_republishes_only_after_an_interval_of_silence() -> None:
    # With no minimum gap every provider signal publishes. This is what the
    # default interval collapsed into one event in the previous test.
    class _NoGapLoop(NativeLoop):
        PROVIDER_ALIVE_INTERVAL_S = 0.0

    client, http = _client_for(ENCRYPTED_ONLY.read_bytes())
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = _NoGapLoop(
        provider=client, store=store, model="gpt-5.6-luna", session_id=sid,
        max_continuations_per_turn=0,
    )
    try:
        await loop.submit(UserTurn(content="think hard"))
        await asyncio.wait_for(loop.wait_turn(), timeout=10)
        signals = [e.signal for e in loop.bus.log(sid) if isinstance(e, ProviderAlive)]
    finally:
        await http.aclose()
        await loop.aclose()
    assert signals == ["accepted", "reasoning", "keepalive", "keepalive", "keepalive"]


class _Scripted:
    name = "openai_responses"
    caps = ProviderCaps(stateful_reasoning=True)

    def __init__(self, chunks: list[StreamChunk]) -> None:
        self._chunks = chunks

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        for chunk in self._chunks:
            yield chunk


async def test_a_call_that_streams_content_publishes_exactly_one() -> None:
    # Heartbeats between text deltas are not silence: the call adds one event.
    loop, sid, _ = await _run_turn(_Scripted([
        StreamChunk("message_start", {"usage": {}}),
        StreamChunk("provider_alive", {"signal": "reasoning"}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": "Hel"}),
        StreamChunk("provider_alive", {"signal": "keepalive"}),
        StreamChunk("text_delta", {"index": 0, "text": "lo"}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 2}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]))
    try:
        alive = [e for e in loop.bus.log(sid) if isinstance(e, ProviderAlive)]
        assert [e.signal for e in alive] == ["accepted"]
    finally:
        await loop.aclose()


async def test_a_provider_that_only_sends_message_start_still_reports_acceptance() -> None:
    # Anthropic and chat-completions providers have no provider_alive chunk;
    # their message_start is the acceptance signal.
    loop, sid, _ = await _run_turn(_Scripted([
        StreamChunk("message_start", {"usage": {"input_tokens": 3}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": "ok"}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 1}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]))
    try:
        events = loop.bus.log(sid)
        alive = [e for e in events if isinstance(e, ProviderAlive)]
        assert [e.signal for e in alive] == ["accepted"]
        assert events.index(alive[0]) < next(
            i for i, e in enumerate(events) if isinstance(e, BlockStart)
        )
    finally:
        await loop.aclose()


def test_the_event_round_trips_and_is_batched_like_a_delta() -> None:
    event = ProviderAlive(session_id="s", turn_id="t", signal="reasoning", seq=4, ts=1.5)
    assert event.type == "provider_alive"
    assert event_from_dict(event_to_dict(event)) == event
    # Its loss costs nothing after the fact, so a store may batch it and a
    # store failure on it never ends a turn.
    assert isinstance(event, RECONSTRUCTIBLE_EVENTS)
