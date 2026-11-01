"""Provider safety checks: served-model reports and the blocked-call prompt.

A reroute (the provider answered with another model) is reported, never
blocked. A refusal pauses the turn on a ``ModelGuardrail`` permission prompt:
allow retries the same history on the offered model for the rest of the turn,
deny ends the turn with ``stop_reason="refusal"``. The provider's own
server-side fallback is never switched on.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from fluid.engine.guardrail import (
    GUARDRAIL_TOOL,
    anthropic_fallback,
    blocked_message,
    prompt_input,
    same_model,
)
from fluid.events.model import (
    GuardrailBlocked,
    ModelServed,
    PermissionRequest,
    TurnComplete,
    event_from_dict,
    event_to_dict,
)
from fluid.events.ops import RespondPermission, UserTurn
from fluid.providers.anthropic.serialize import build_request_body, sse_to_chunk
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk


# ─── helpers ─────────────────────────────────────────────────────────────────


def _reply(text: str, model: str) -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 5}, "message": {"model": model}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": text}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 2}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


def _refusal(model: str, details: dict[str, Any] | None) -> list[StreamChunk]:
    usage: dict[str, Any] = {"usage": {"output_tokens": 1}, "stop_reason": "refusal"}
    if details is not None:
        usage["stop_details"] = {"type": "refusal", **details}
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 5}, "message": {"model": model}}),
        StreamChunk("block_start", {"index": 0, "block_type": "text"}),
        StreamChunk("text_delta", {"index": 0, "text": "Partial answer"}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", usage),
        StreamChunk("message_stop", {}),
    ]


class _Provider:
    caps = ProviderCaps()

    def __init__(self, scripts: list[list[StreamChunk]], name: str = "anthropic") -> None:
        self.name = name
        self.scripts = scripts
        self.models: list[str] = []
        self.histories: list[list[Any]] = []

    async def stream(self, request: ProviderRequest):
        self.models.append(request.model)
        self.histories.append(list(request.messages))
        script = self.scripts[min(len(self.models) - 1, len(self.scripts) - 1)]
        for chunk in script:
            yield chunk


def _loop(provider: _Provider, model: str = "claude-opus-5-5"):
    from fluid.engine.native import NativeLoop
    from fluid.store.base import MemoryStore

    return NativeLoop(provider=provider, store=MemoryStore(), model=model)


async def _answer(loop, decision: str, **extra: Any) -> PermissionRequest:
    while True:
        log = loop.bus.log(loop.session_id) if loop._session is not None else []
        event = next((e for e in log if isinstance(e, PermissionRequest)), None)
        if event is not None:
            break
        await asyncio.sleep(0.01)
    await loop.submit(RespondPermission(request_id=event.request_id, decision=decision, **extra))
    return event


async def _run(loop, decision: str | None, **extra: Any) -> tuple[list[Any], PermissionRequest | None]:
    watcher = asyncio.create_task(_answer(loop, decision, **extra)) if decision else None
    try:
        await loop.submit(UserTurn(content="scan this host"))
        prompt = await asyncio.wait_for(watcher, 10) if watcher else None
        await asyncio.wait_for(loop.wait_turn(), 10)
        return loop.bus.log(loop.session_id), prompt
    finally:
        if watcher is not None:
            watcher.cancel()


# ─── pure helpers ────────────────────────────────────────────────────────────


def test_same_model_ignores_snapshot_prefix_and_context_suffix() -> None:
    assert same_model("claude-sonnet-5", "claude-sonnet-5-20260801")
    assert same_model("gpt-5.5", "openai/gpt-5.5")
    assert same_model("claude-opus-5-5[1m]", "claude-opus-5-5")
    assert same_model(None, "x") and same_model("x", "")
    assert not same_model("gpt-5.5", "gpt-5.2")
    assert same_model("openai/gpt-5.5:free", "openai/gpt-5.5")
    assert same_model("google/gemini-3-pro", "google/gemini-3-pro-preview-11-2026")
    assert not same_model("gpt-5.5", "gpt-5.5-mini")
    assert not same_model("claude-opus-5-5", "claude-opus-5")
    assert not same_model("claude-opus-5", "claude-opus-5-5")


def test_anthropic_fallback_follows_the_category_table() -> None:
    assert anthropic_fallback("claude-opus-5-5", "cyber") == "claude-opus-4-8"
    assert anthropic_fallback("claude-opus-5-5", "bio") == "claude-opus-5"
    assert anthropic_fallback("claude-sonnet-5-5", "frontier_llm") == "claude-sonnet-5"
    assert anthropic_fallback("claude-opus-5", "bio") is None
    assert anthropic_fallback("claude-opus-5-5", "reasoning_extraction") is None
    assert anthropic_fallback("claude-opus-5-5", None) is None


def test_prompt_input_reads_plainly() -> None:
    assert blocked_message("anthropic", "cyber") == "Blocked by Anthropic's safety check (cyber)."
    assert blocked_message(None, None) == "Blocked by the provider's safety check."
    data = prompt_input(
        provider="anthropic", requested_model="claude-opus-5-5", category="cyber",
        explanation=None, suggested_model="claude-opus-4-8",
    )
    assert data["guardrail"] is True and data["provider_label"] == "Anthropic"
    assert data["message"].endswith("Continue this turn on claude-opus-4-8?")


def test_new_events_round_trip_and_old_rows_load() -> None:
    served = ModelServed(
        session_id="s", turn_id="t", requested_model="a", served_model="b", rerouted=True,
    )
    assert event_from_dict(event_to_dict(served)) == served
    minimal = {"_cls": "ModelServed", "session_id": "s", "requested_model": "a", "served_model": "a"}
    assert event_from_dict(minimal).rerouted is False
    blocked = GuardrailBlocked(
        session_id="s", turn_id="t", provider="anthropic", requested_model="a",
        outcome="stopped", category="cyber",
    )
    assert event_from_dict(event_to_dict(blocked)) == blocked


def test_anthropic_stream_carries_stop_details_and_no_fallback_is_requested() -> None:
    chunk = sse_to_chunk("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": "refusal", "stop_details": {
            "type": "refusal", "category": "cyber", "explanation": None,
        }},
        "usage": {"output_tokens": 3},
    })
    assert chunk is not None and chunk.payload["stop_details"]["category"] == "cyber"
    plain = sse_to_chunk("message_delta", {
        "type": "message_delta", "delta": {"stop_reason": "end_turn", "stop_details": None},
    })
    assert plain is not None and "stop_details" not in plain.payload
    from fluid.messages import Message, Text

    body = build_request_body(ProviderRequest(
        model="claude-opus-5-5", messages=[Message(role="user", content=[Text("hi")])],
    ))
    assert "fallbacks" not in body  # server-side fallback stays off


def test_responses_terminal_reports_served_model() -> None:
    from fluid.providers.openai_responses.serialize import ResponsesStreamNormalizer

    norm = ResponsesStreamNormalizer()
    chunks = norm.consume("response.completed", {
        "type": "response.completed",
        "response": {"model": "gpt-5.2", "status": "completed", "usage": {
            "input_tokens": 3, "output_tokens": 1,
        }},
    })
    usage = [c for c in chunks if c.kind == "usage"]
    assert usage and usage[0].payload.get("served_model") == "gpt-5.2"


def test_chat_completions_terminal_reports_served_model() -> None:
    from fluid.providers._chat_completions import ChatCompletionsNormalizer

    norm = ChatCompletionsNormalizer()
    norm.consume("message", {"model": "fake-ai-2", "choices": [
        {"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"},
    ]})
    out = norm.consume("message", {"choices": [], "usage": {"prompt_tokens": 5, "completion_tokens": 1}})
    usage = next(c for c in out if c.kind == "usage")
    assert usage.payload["served_model"] == "fake-ai-2"
    assert next(c for c in out if c.kind == "message_stop").payload == {}


# ─── native loop ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reroute_is_reported_once_and_not_blocked() -> None:
    provider = _Provider([_reply("hello", "gpt-5.2")], name="openai_responses")
    loop = _loop(provider, model="gpt-5.5")
    try:
        events, _ = await _run(loop, None)
        served = [e for e in events if isinstance(e, ModelServed)]
        assert len(served) == 1
        assert served[0].rerouted and served[0].served_model == "gpt-5.2"
        assert served[0].requested_model == "gpt-5.5"
        assert served[0].provider == "openai_responses"
        assert not any(isinstance(e, PermissionRequest) for e in events)
        assert next(e for e in events if isinstance(e, TurnComplete)).stop_reason == "end_turn"
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_served_model_is_reported_only_when_it_is_news() -> None:
    provider = _Provider([_reply("hello", "claude-opus-5-5")])
    loop = _loop(provider)
    try:
        events, _ = await _run(loop, None)
        assert not any(isinstance(e, ModelServed) for e in events)
    finally:
        await loop.aclose()
    snapshot = _loop(_Provider([_reply("hello", "claude-opus-5-5-20261001")]))
    try:
        events, _ = await _run(snapshot, None)
        [served] = [e for e in events if isinstance(e, ModelServed)]
        assert served.served_model == "claude-opus-5-5-20261001" and not served.rerouted
    finally:
        await snapshot.aclose()


@pytest.mark.asyncio
async def test_refusal_continue_retries_on_suggested_model_for_this_turn_only() -> None:
    provider = _Provider([
        _refusal("claude-opus-5-5", {"category": "cyber", "explanation": None}),
        _reply("retried answer", "claude-opus-4-8"),
        _reply("next turn", "claude-opus-5-5"),
    ])
    loop = _loop(provider)
    try:
        events, prompt = await _run(loop, "allow")
        assert prompt is not None and prompt.name == GUARDRAIL_TOOL
        assert prompt.input["category"] == "cyber"
        assert prompt.input["suggested_model"] == "claude-opus-4-8"
        assert prompt.input["partial_output_discarded"] is True
        blocked = next(e for e in events if isinstance(e, GuardrailBlocked))
        assert blocked.outcome == "continued" and blocked.retry_model == "claude-opus-4-8"
        assert provider.models == ["claude-opus-5-5", "claude-opus-4-8"]
        # The retry re-sends the same history: the declined reply is not in it.
        assert len(provider.histories[1]) == len(provider.histories[0])
        assert next(e for e in events if isinstance(e, TurnComplete)).stop_reason == "end_turn"
        assert loop.model == "claude-opus-5-5"  # back on the chat's model

        await loop.submit(UserTurn(content="thanks"))
        await asyncio.wait_for(loop.wait_turn(), 10)
        assert provider.models[-1] == "claude-opus-5-5"
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_refusal_continue_uses_the_model_the_responder_picked() -> None:
    provider = _Provider([
        _refusal("claude-opus-5-5", {"category": "bio", "recommended_model": "claude-opus-5"}),
        _reply("ok", "claude-sonnet-5"),
    ])
    loop = _loop(provider)
    try:
        events, prompt = await _run(loop, "allow", updated_input={"model": "claude-sonnet-5"})
        assert prompt is not None and prompt.input["suggested_model"] == "claude-opus-5"
        assert provider.models[-1] == "claude-sonnet-5"
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_refusal_stop_ends_the_turn_and_discards_partial_output() -> None:
    provider = _Provider([_refusal("claude-opus-5-5", {"category": "cyber"})])
    loop = _loop(provider)
    try:
        events, _ = await _run(loop, "deny", message="declined")
        blocked = next(e for e in events if isinstance(e, GuardrailBlocked))
        assert blocked.outcome == "stopped" and blocked.reason == "declined"
        done = next(e for e in events if isinstance(e, TurnComplete))
        assert done.stop_reason == "refusal"
        assert provider.models == ["claude-opus-5-5"]
        nodes = await loop.store.load_path(loop.session_id, done.node_id)
        assert all("Partial answer" not in str(n.message.content) for n in nodes)
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_refusal_with_no_suggestion_stops_even_on_allow() -> None:
    provider = _Provider([_refusal("claude-opus-5-5", {"category": "reasoning_extraction"})])
    loop = _loop(provider)
    try:
        events, prompt = await _run(loop, "allow")
        assert prompt is not None and prompt.input["suggested_model"] is None
        blocked = next(e for e in events if isinstance(e, GuardrailBlocked))
        assert blocked.outcome == "stopped" and blocked.reason == "no model to retry on"
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_unanswered_prompt_times_out_into_a_stop() -> None:
    provider = _Provider([_refusal("claude-opus-5-5", {"category": "cyber"})])
    loop = _loop(provider)
    loop.permission_timeout = 0.05
    try:
        events, _ = await _run(loop, None)
        blocked = next(e for e in events if isinstance(e, GuardrailBlocked))
        assert blocked.outcome == "stopped" and "timed out" in (blocked.reason or "")
        assert next(e for e in events if isinstance(e, TurnComplete)).stop_reason == "refusal"
    finally:
        await loop.aclose()


@pytest.mark.asyncio
async def test_content_filter_error_raises_the_same_prompt() -> None:
    provider = _Provider([[
        StreamChunk("message_start", {"usage": {"input_tokens": 5}, "message": {}}),
        StreamChunk("error", {
            "message": "the provider's content filter stopped the response before any output",
            "retriable": False, "native_stop_reason": "content_filter",
        }),
    ]], name="openrouter")
    loop = _loop(provider, model="openai/gpt-5.5")
    try:
        events, prompt = await _run(loop, "deny")
        assert prompt is not None and prompt.input["category"] == "content_filter"
        assert prompt.input["suggested_model"] is None
        assert next(e for e in events if isinstance(e, TurnComplete)).stop_reason == "refusal"
    finally:
        await loop.aclose()
