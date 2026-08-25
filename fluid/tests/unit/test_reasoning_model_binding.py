"""Reasoning envelopes are model-bound, not just provider-bound.

xAI and OpenAI both speak the openai_responses dialect, so a mid-session model
(or account) switch leaves encrypted reasoning blobs in history that the new
model cannot decrypt — and one such blob 400s every later request
(invalid_encrypted_content), bricking the session. Two defenses under test:
envelopes stamped with the producing model are dropped at request-build time
when they mismatch, and a history that still fails (legacy untagged blobs) is
retried once with opaque reasoning stripped, then pre-stripped for the rest of
the engine's lifetime.
"""

import asyncio
from collections.abc import AsyncIterator

from fluid.engine.native import NativeLoop, strip_opaque_reasoning
from fluid.events import ErrorEvent, ProviderRetry, TurnComplete, UserTurn
from fluid.messages import Message, Reasoning, Text, block_from_dict
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore

INVALID_ENCRYPTED_BODY = (
    'status 400: {"error": {"message": "The encrypted content RJO1nVP2 could '
    "not be verified. Reason: Encrypted content could not be decrypted or "
    'parsed.", "type": "invalid_request_error", "param": null, '
    '"code": "invalid_encrypted_content"}}'
)


class ScriptedProvider:
    """Replays one scripted chunk list per model call; repeats the last."""

    name = "openai_responses"
    caps = ProviderCaps(stateful_reasoning=True)

    def __init__(self, scripts: list[list[StreamChunk]]) -> None:
        self.scripts = scripts
        self.calls = 0
        self.requests: list[ProviderRequest] = []

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        script = self.scripts[min(self.calls, len(self.scripts) - 1)]
        self.calls += 1
        for chunk in script:
            yield chunk


def _reasoning_then_text(text: str) -> list[StreamChunk]:
    """A Responses completion: reasoning item with an encrypted blob, then text."""
    return [
        StreamChunk("message_start", {"usage": {}}),
        StreamChunk("block_start", {"index": 0, "block_type": "thinking", "block": {}}),
        StreamChunk("thinking_delta", {"index": 0, "text": "pondering"}),
        StreamChunk("block_stop", {"index": 0, "block": {
            "id": "rs_1", "type": "reasoning", "encrypted_content": "BLOB",
            "content": [],
        }}),
        StreamChunk("block_start", {"index": 1, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 1, "text": text}),
        StreamChunk("block_stop", {"index": 1}),
        StreamChunk("usage", {"usage": {}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _text(text: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _error(message: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {}}),
        StreamChunk("error", {"message": message, "retriable": False}),
    ]


def _opaque_reasoning(request: ProviderRequest) -> list[Reasoning]:
    return [
        b for m in request.messages for b in m.content
        if isinstance(b, Reasoning)
        and (b.encrypted_content is not None or b.raw is not None)
    ]


# ─── 1. the model tag round-trips through dict serialization ─────────────────


def test_reasoning_model_round_trips() -> None:
    block = Reasoning(
        origin="openai_responses", summary_text="thought",
        encrypted_content="BLOB", raw={"type": "reasoning"}, model="grok-4.6",
    )
    d = Message(role="assistant", content=[block]).to_dict()
    assert d["content"][0]["model"] == "grok-4.6"
    assert Message.from_dict(d).content[0] == block


def test_reasoning_legacy_dict_without_model_deserializes() -> None:
    # Histories written before the field existed have no "model" key.
    legacy = {"kind": "reasoning", "origin": "openai_responses",
              "encrypted_content": "BLOB"}
    block = block_from_dict(legacy)
    assert isinstance(block, Reasoning)
    assert block.model is None


# ─── 2. the envelope builder stamps the producing model ──────────────────────


async def test_envelope_builder_stamps_producing_model() -> None:
    provider = ScriptedProvider([_reasoning_then_text("hi")])
    store = MemoryStore()
    loop = NativeLoop(provider=provider, store=store, model="grok-4.6")
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    nodes = await store.load_path(loop.session_id)
    reasoning = next(
        b for n in nodes for b in n.message.content if isinstance(b, Reasoning)
    )
    assert reasoning.origin == "openai_responses"
    assert reasoning.model == "grok-4.6"
    assert reasoning.encrypted_content == "BLOB"


# ─── 3. serializers drop model-mismatched reasoning ──────────────────────────


def _responses_request(model: str) -> ProviderRequest:
    def envelope(tag: str | None, blob: str) -> Reasoning:
        return Reasoning(
            origin="openai_responses", encrypted_content=blob,
            raw={"type": "reasoning", "encrypted_content": blob, "content": []},
            model=tag,
        )

    return ProviderRequest(
        model=model,
        messages=[
            Message(role="user", content=[Text("hi")]),
            Message(role="assistant", content=[
                envelope("grok-4.6", "GROK_BLOB"),   # produced by another model
                envelope(model, "OWN_BLOB"),          # produced by this model
                envelope(None, "LEGACY_BLOB"),        # pre-tagging history
                Text("answer"),
            ]),
        ],
    )


def test_responses_serializer_drops_only_model_mismatched_reasoning() -> None:
    from fluid.providers.openai_responses.serialize import build_request_body

    items = build_request_body(_responses_request("gpt-5.6-sol"))["input"]
    blobs = [
        it["encrypted_content"] for it in items if it["type"] == "reasoning"
    ]
    # The matched and the legacy-untagged blobs resend; the foreign-model
    # blob is dropped.
    assert blobs == ["OWN_BLOB", "LEGACY_BLOB"]


def test_anthropic_serializer_drops_only_model_mismatched_thinking() -> None:
    from fluid.providers.anthropic.serialize import build_request_body

    def envelope(tag: str | None, sig: str) -> Reasoning:
        return Reasoning(
            origin="anthropic", summary_text="t", signature=sig,
            raw={"type": "thinking", "thinking": "t", "signature": sig},
            model=tag,
        )

    request = ProviderRequest(
        model="claude-b",
        messages=[
            Message(role="assistant", content=[
                envelope("claude-a", "FOREIGN_SIG"),
                envelope("claude-b", "OWN_SIG"),
                envelope(None, "LEGACY_SIG"),
                Text("answer"),
            ]),
            Message(role="user", content=[Text("next")]),
        ],
    )
    body = build_request_body(request)
    signatures = [
        c["signature"] for c in body["messages"][0]["content"]
        if c["type"] == "thinking"
    ]
    assert signatures == ["OWN_SIG", "LEGACY_SIG"]


# ─── 4. engine self-heal on invalid_encrypted_content ────────────────────────


async def test_invalid_encrypted_content_strips_and_retries() -> None:
    provider = ScriptedProvider([
        _reasoning_then_text("turn one"),    # turn 1: leaves a blob in history
        _error(INVALID_ENCRYPTED_BODY),      # turn 2, call 1: rejected
        _text("recovered"),                  # turn 2, call 2: stripped retry
        _text("turn three"),                 # turn 3: pre-stripped, no failure
    ])
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="gpt")
    await loop.submit(UserTurn(content="one"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    await loop.submit(UserTurn(content="two"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    await loop.submit(UserTurn(content="three"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    # The failing call still carried the blob; the retry and every later
    # request are stripped.
    assert provider.calls == 4
    assert _opaque_reasoning(provider.requests[1])
    assert not _opaque_reasoning(provider.requests[2])
    assert not _opaque_reasoning(provider.requests[3])

    events = loop.bus.log(loop.session_id)
    assert not any(isinstance(e, ErrorEvent) for e in events)  # healed silently
    retries = [e for e in events if isinstance(e, ProviderRetry)]
    assert len(retries) == 1 and "encrypted reasoning" in retries[0].reason
    completes = [e for e in events if isinstance(e, TurnComplete)]
    assert [e.stop_reason for e in completes] == ["end_turn"] * 3


async def test_invalid_encrypted_content_recurring_surfaces() -> None:
    provider = ScriptedProvider([
        _reasoning_then_text("turn one"),
        _error(INVALID_ENCRYPTED_BODY),  # repeats: the stripped retry fails too
    ])
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="gpt")
    await loop.submit(UserTurn(content="one"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    await loop.submit(UserTurn(content="two"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)

    assert provider.calls == 3  # exactly one self-heal retry, no loop
    err = next(
        e for e in loop.bus.log(loop.session_id) if isinstance(e, ErrorEvent)
    )
    assert "invalid_encrypted_content" in err.message


def test_strip_opaque_reasoning_is_pure_and_keeps_summaries() -> None:
    opaque = Reasoning(origin="openai_responses", encrypted_content="BLOB")
    summary_only = Reasoning(origin="openrouter", summary_text="just text")
    message = Message(role="assistant", content=[opaque, summary_only, Text("a")])
    clean = Message(role="user", content=[Text("hi")])
    history = [clean, message]

    stripped = strip_opaque_reasoning(history)
    assert stripped[0] is clean  # untouched messages pass through by identity
    assert stripped[1].content == [summary_only, Text("a")]
    assert message.content == [opaque, summary_only, Text("a")]  # never mutated
    assert strip_opaque_reasoning([clean]) == [clean]
