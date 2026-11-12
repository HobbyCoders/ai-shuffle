"""Gemini Interactions leg: serializer round-trips and the real client over HTTP.

Fixture shapes follow Google's Interactions docs (aistudio.google.com/docs/
streaming, /thinking, /function-calling, read 2026-11-11). The end-to-end
tests run the real ``GeminiClient`` and ``NativeLoop`` against the scripted
loopback server in ``fixtures/fake_provider_server.py``. Key values are
obviously fake. Hermetic: loopback only.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

import fluid.engine.native as native_mod
from fluid.engine.native import NativeLoop
from fluid.events import ErrorEvent, TurnComplete, Usage, UserTurn
from fluid.events.model import Interrupted, ProviderRetry
from fluid.messages import Image, Message, Reasoning, Text, Thinking, ToolResultBlock, ToolUseBlock
from fluid.permissions.engine import PermissionEngine
from fluid.providers._retry import RetryPolicy
from fluid.providers.base import ProviderRequest
from fluid.providers.gemini import pricing
from fluid.providers.gemini.client import GeminiClient
from fluid.providers.gemini.serialize import (
    StreamNormalizer,
    build_input,
    build_request_body,
    classify_error,
    normalize_usage,
    thinking_level,
)
from fluid.store.base import MemoryStore
from fluid.tools.registry import ToolCtx, ToolOutcome, ToolRegistry, ToolSpec

FAKE_KEY = "fake-gemini-test-key"
SIG_A = "Q2lZQjFmYWtlLXNpZ25hdHVyZS1hYmM="  # opaque base64-looking, byte-exact
SIG_B = "RmFrZS1zaWduYXR1cmUtLXNlY29uZA=="
FAST_RETRY = RetryPolicy(max_retries=2, base_delay=0.01, max_delay=0.05, jitter=0)


def _load_fake_server():
    name = "fake_provider_server"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(__file__).parent.parent / "fixtures" / "fake_provider_server.py"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fps = _load_fake_server()
Interactions = fps.Interactions


# ─── request serialization ───────────────────────────────────────────────────


def _thought(signature: str, summary: str = "Thinking it over.") -> Reasoning:
    raw = {"type": "thought", "signature": signature, "summary": [{"type": "text", "text": summary}]}
    return Reasoning(origin="gemini", raw=raw, summary_text=summary, model="gemini-3.8-flash")


def test_multi_step_tool_turn_resends_thoughts_verbatim_and_results_by_id():
    first_thought = _thought(SIG_A)
    second_thought = _thought(SIG_B, "Now the second city.")
    messages = [
        Message(role="user", content=[Text("Weather in Paris and London?")]),
        Message(role="assistant", content=[
            Thinking("Thinking it over."), first_thought,
            ToolUseBlock(id="call_1", name="get_weather", input={"city": "Paris"}),
            ToolUseBlock(id="call_2", name="get_weather", input={"city": "London"}),
        ]),
        Message(role="user", content=[
            ToolResultBlock(tool_use_id="call_1", content=[Text("18C")]),
            ToolResultBlock(tool_use_id="call_2", content=[Text("not found")], is_error=True),
        ]),
        Message(role="assistant", content=[second_thought, Text("Paris is 18C.")]),
        Message(role="user", content=[Text("Thanks")]),
    ]
    steps = build_input(messages)
    assert [s["type"] for s in steps] == [
        "user_input", "thought", "function_call", "function_call",
        "function_result", "function_result", "thought", "model_output", "user_input",
    ]
    # The thought step is the exact object the stream produced (same identity
    # of content, signature bytes unchanged).
    assert steps[1] == first_thought.raw and steps[1]["signature"] == SIG_A
    assert steps[6]["signature"] == SIG_B
    assert steps[2] == {"type": "function_call", "id": "call_1", "name": "get_weather",
                        "arguments": {"city": "Paris"}}
    assert steps[4] == {"type": "function_result", "call_id": "call_1",
                        "result": "18C", "name": "get_weather"}
    assert steps[5]["is_error"] is True and steps[5]["call_id"] == "call_2"
    assert steps[-1] == {"type": "user_input", "content": [{"type": "text", "text": "Thanks"}]}
    json.dumps(steps)  # wire-serializable


def test_foreign_reasoning_is_dropped_and_a_trailing_model_turn_is_cut():
    messages = [
        Message(role="user", content=[Text("hi")]),
        Message(role="assistant", content=[
            Reasoning(origin="anthropic", raw={"type": "thinking", "signature": "x"}),
            Reasoning(origin="openai_responses", raw={"type": "reasoning", "id": "rs_1"}),
            Text("hello"),
        ]),
    ]
    assert build_input(messages) == [
        {"type": "user_input", "content": [{"type": "text", "text": "hi"}]},
    ]


def test_images_go_inline_and_tool_results_with_images_become_content_lists():
    png = "iVBORw0KGgo="
    messages = [
        Message(role="user", content=[Text("look"), Image(media_type="image/png", data=png)]),
        Message(role="assistant", content=[ToolUseBlock(id="c1", name="Shot", input={})]),
        Message(role="user", content=[ToolResultBlock(
            tool_use_id="c1", content=[Text("shot:"), Image(media_type="image/png", data=png)])]),
    ]
    steps = build_input(messages)
    assert steps[0]["content"][1]["type"] == "image"
    assert steps[0]["content"][1]["mime_type"] == "image/png" and steps[0]["content"][1]["data"]
    result = steps[2]["result"]
    assert [item["type"] for item in result] == ["text", "image"]


def test_request_body_is_stateless_and_drops_sampling_knobs():
    request = ProviderRequest(
        model="gemini-4-carbon",
        messages=[Message(role="user", content=[Text("hi")])],
        system=[{"type": "text", "text": "Be brief."}],
        tools=[{"name": "Echo", "description": "Echo.", "input_schema": {"type": "object"}}],
        max_tokens=1024, effort="max",
        extra={"temperature": 0.2, "top_p": 0.9, "candidate_count": 2,
               "generation_config": {"top_k": 3, "seed": 7}, "service_tier": "flex"},
    )
    body = build_request_body(request)
    assert body["store"] is False and body["stream"] is True
    assert "previous_interaction_id" not in body
    assert body["system_instruction"] == "Be brief."
    assert body["tools"] == [{"type": "function", "name": "Echo", "description": "Echo.",
                              "parameters": {"type": "object"}}]
    assert body["generation_config"] == {"thinking_summaries": "auto", "thinking_level": "high",
                                         "max_output_tokens": 1024, "seed": 7}
    assert body["service_tier"] == "flex"
    for knob in ("temperature", "top_p", "top_k", "candidate_count"):
        assert knob not in body and knob not in body["generation_config"]


@pytest.mark.parametrize(("model", "effort", "level"), [
    ("gemini-4-carbon", "minimal", "low"),      # Gemini 4 has no minimal
    ("gemini-4-argon", "xhigh", "high"),
    ("gemini-3.8-flash", "none", "low"),        # minimal is an error on 3.8 Flash
    ("gemini-3.6-flash", "minimal", "minimal"),
    ("gemini-3.5-flash-lite", "medium", "medium"),
    ("gemini-3-pro-preview", "medium", "high"),  # only low/high
    ("gemini-unknown-next", "medium", "medium"),
    ("gemini-unknown-next", "ultra", "high"),
    ("gemini-4-carbon", None, None),
])
def test_effort_maps_to_a_documented_thinking_level(model, effort, level):
    assert thinking_level(model, effort) == level


# ─── response normalization ──────────────────────────────────────────────────


def _events(frames: list[bytes]) -> list[tuple[str, dict]]:
    out = []
    for frame in frames:
        lines = frame.decode().strip().splitlines()
        name = lines[0].removeprefix("event: ")
        out.append((name, json.loads(lines[1].removeprefix("data: "))))
    return out


def test_thought_signature_is_reassembled_on_block_stop():
    frames = [Interactions.created(), *Interactions.thought(0, "Plan the reply.", SIG_A),
              *Interactions.output(1, "Done."), Interactions.completed(thought=12)]
    normalizer = StreamNormalizer()
    chunks = [c for name, data in _events(frames) for c in normalizer.consume(name, data)]
    kinds = [c.kind for c in chunks]
    assert kinds[0] == "message_start" and kinds[-2:] == ["usage", "message_stop"]
    stop = next(c for c in chunks if c.kind == "block_stop" and c.payload["index"] == 0)
    assert stop.payload["block"] == {
        "type": "thought", "signature": SIG_A,
        "summary": [{"type": "text", "text": "Plan the reply."}],
    }
    assert "".join(c.payload["text"] for c in chunks if c.kind == "thinking_delta") == "Plan the reply."
    assert normalizer.terminal


def test_usage_folds_thinking_into_output_and_reports_implicit_cache_hits():
    usage = normalize_usage({"total_input_tokens": 1000, "total_cached_tokens": 600,
                             "total_output_tokens": 40, "total_thought_tokens": 160,
                             "total_tokens": 1200})
    assert usage == {"input_tokens": 400, "output_tokens": 200, "cache_read_input_tokens": 600,
                     "cache_creation_input_tokens": 0, "reasoning_tokens": 160, "total_tokens": 1200}


@pytest.mark.parametrize(("error", "retriable"), [
    ({"code": "rate_limit_exceeded", "message": "slow down"}, True),
    ({"code": "service_unavailable", "message": "busy"}, True),
    ({"code": "quota_exceeded", "message": "daily quota"}, False),
    ({"code": "invalid_request", "message": "bad"}, False),
    ({"code": 429, "status": "RESOURCE_EXHAUSTED", "message": "legacy"}, True),
    ({"code": 400, "status": "INVALID_ARGUMENT", "message": "legacy bad"}, False),
])
def test_error_classification(error, retriable):
    assert classify_error({"error": error}).retriable is retriable


def test_pricing_follows_googles_published_schedule():
    usage = {"input_tokens": 1_000_000, "output_tokens": 1_000_000, "cache_read_input_tokens": 1_000_000}
    assert pricing.estimate_cost("gemini-4-carbon", usage, dt.date(2026, 11, 30)) == pytest.approx(12.10)
    assert pricing.estimate_cost("gemini-4-carbon", usage, dt.date(2026, 12, 1)) == pytest.approx(24.20)
    assert pricing.estimate_cost("gemini-3.8-flash", usage, dt.date(2027, 1, 1)) == pytest.approx(9.15)
    assert pricing.estimate_cost("gemini-unpriced", usage) is None


# ─── the real client and engine over HTTP ────────────────────────────────────


@pytest.fixture
def server():
    srv = fps.FakeProviderServer()
    try:
        yield srv
    finally:
        srv.close()


@pytest.fixture(autouse=True)
def _no_empty_retry_backoff(monkeypatch):
    monkeypatch.setattr(native_mod, "_empty_retry_delay", lambda attempt: 0.0)


def _loop(server, ran: list) -> NativeLoop:
    registry = ToolRegistry()

    async def lookup(inp: dict, _ctx: ToolCtx) -> ToolOutcome:
        ran.append(inp)
        return ToolOutcome(text="sunny in " + str(inp.get("city")))

    registry.register_builtin(ToolSpec(
        name="get_weather", description="Weather.",
        input_schema={"type": "object", "properties": {"city": {"type": "string"}}}), lookup)
    client = GeminiClient(api_key=FAKE_KEY, base_url=server.url + "/v1beta/interactions",
                          retry=FAST_RETRY)
    return NativeLoop(provider=client, store=MemoryStore(), model="gemini-3.8-flash",
                      registry=registry, permissions=PermissionEngine("never_ask"))


async def _turn(loop: NativeLoop, prompt: str) -> list:
    before = len(loop.bus.log(loop.session_id)) if loop._session else 0
    await loop.submit(UserTurn(content=prompt))
    await loop.wait_turn()
    return loop.bus.log(loop.session_id)[before:]


def _terminal(events: list):
    [done] = [e for e in events if isinstance(e, (TurnComplete, Interrupted, ErrorEvent))]
    return done


async def test_parallel_tool_calls_round_trip_their_thought_signature(server):
    calls = [{"id": "fc_1", "name": "get_weather", "args": {"city": "Paris"}},
             {"id": "fc_2", "name": "get_weather", "args": {"city": "London"}}]
    server.script(
        Interactions.tool_calls(calls, signature=SIG_A, pieces=4, thought=30),
        Interactions.text("Both are sunny.", input_tokens=200, cached=150, output_tokens=6),
    )
    ran: list = []
    loop = _loop(server, ran)
    try:
        events = await _turn(loop, "Weather in Paris and London?")
    finally:
        await loop.aclose()

    assert isinstance(_terminal(events), TurnComplete)
    assert ran == [{"city": "Paris"}, {"city": "London"}]
    first, second = (r.body for r in server.requests)
    for body in (first, second):
        assert body["store"] is False and body["stream"] is True
        assert "temperature" not in body["generation_config"]
    headers = server.requests[0].headers
    assert headers.get("x-goog-api-key") == FAKE_KEY
    assert FAKE_KEY not in server.requests[0].path
    steps = second["input"]
    assert [s["type"] for s in steps] == [
        "user_input", "thought", "function_call", "function_call",
        "function_result", "function_result",
    ]
    assert steps[1]["signature"] == SIG_A  # byte-for-byte
    assert steps[2]["arguments"] == {"city": "Paris"} and steps[3]["id"] == "fc_2"
    assert [s["call_id"] for s in steps[4:]] == ["fc_1", "fc_2"]
    assert steps[4]["result"] == "sunny in Paris"
    usages = [e for e in events if isinstance(e, Usage)]
    assert usages[-1].cache_read_tokens == 150
    assert usages[-1].input_tokens == 50
    done = _terminal(events)
    assert done.cost_usd is not None and done.cost_usd > 0  # priced from the Gemini table


async def test_a_transient_error_before_content_is_retried_once(server):
    server.script(
        [Interactions.created(), Interactions.error("service_unavailable", "Overloaded")],
        Interactions.text("Recovered."),
    )
    loop = _loop(server, [])
    try:
        events = await _turn(loop, "hi")
    finally:
        await loop.aclose()
    assert isinstance(_terminal(events), TurnComplete)
    assert len(server.requests) == 2


async def test_http_429_retries_but_a_daily_quota_does_not(server):
    server.script(
        fps.error_reply(429, body={"error": {"code": "rate_limit_exceeded", "message": "slow"}}),
        Interactions.text("ok"),
    )
    client = GeminiClient(api_key=FAKE_KEY, base_url=server.url + "/v1beta/interactions",
                          retry=FAST_RETRY)
    request = ProviderRequest(model="gemini-3.8-flash",
                              messages=[Message(role="user", content=[Text("hi")])])
    chunks = [c async for c in client.stream(request)]
    assert chunks[-1].kind == "message_stop" and len(server.requests) == 2

    server.script(fps.error_reply(429, body={"error": {"code": "quota_exceeded",
                                                       "message": "You have exceeded your daily quota."}}))
    chunks = [c async for c in client.stream(request)]
    [error] = [c for c in chunks if c.kind == "error"]
    assert error.payload["retriable"] is False and error.payload.get("usage_limit") is True
    assert len(server.requests) == 3


async def test_an_error_after_content_is_surfaced_not_retried(server):
    server.script([Interactions.created(), *Interactions.output(0, "partial"),
                   Interactions.error("service_unavailable", "Overloaded")])
    client = GeminiClient(api_key=FAKE_KEY, base_url=server.url + "/v1beta/interactions",
                          retry=FAST_RETRY)
    request = ProviderRequest(model="gemini-3.8-flash",
                              messages=[Message(role="user", content=[Text("hi")])])
    chunks = [c async for c in client.stream(request)]
    assert any(c.kind == "text_delta" for c in chunks)
    assert chunks[-1].kind == "error" and len(server.requests) == 1


async def test_context_overflow_text_is_recognized_by_the_engine(server):
    server.script(fps.error_reply(400, body={"error": {
        "code": "invalid_request",
        "message": "The input token count (1100000) exceeds the maximum number of tokens allowed (1048576)."}}))
    client = GeminiClient(api_key=FAKE_KEY, base_url=server.url + "/v1beta/interactions",
                          retry=FAST_RETRY)
    request = ProviderRequest(model="gemini-3.8-flash",
                              messages=[Message(role="user", content=[Text("hi")])])
    [error] = [c async for c in client.stream(request)]
    assert error.kind == "error" and error.payload["retriable"] is False
    assert any(marker in error.payload["message"].lower() for marker in native_mod._OVERFLOW_MARKERS)


def _dropped_reply():
    """Text has started streaming when the connection is cut (no completion)."""
    return fps.Reply(frames=[Interactions.created(), *Interactions.output(0, "Partial"), fps.CUT],
                     framing="chunked")


@pytest.mark.parametrize("drops", [1, native_mod.TRANSPORT_DROP_MAX_RETRIES])
async def test_a_drop_after_text_started_gets_the_engines_bounded_replay(server, drops):
    """The client itself retries only before the first chunk. A stream cut
    after content started is reported with transport evidence, and the engine
    re-sends the same stateless request, as for every native leg."""
    server.script(*[_dropped_reply() for _ in range(drops)], Interactions.text("Whole answer."))
    loop = _loop(server, [])
    try:
        events = await _turn(loop, "hi")
        stored = [n.message for n in await loop.store.load_path(loop.session_id)]
    finally:
        await loop.aclose()

    assert isinstance(_terminal(events), TurnComplete)
    assert len(server.requests) == 1 + drops
    assert all(r.body == server.requests[0].body for r in server.requests)
    retries = [e for e in events if isinstance(e, ProviderRetry)]
    assert [r.cause for r in retries] == ["connection_dropped"] * drops
    assert all(r.max_attempts == native_mod.TRANSPORT_DROP_MAX_RETRIES for r in retries)
    # The partial text is discarded: the reply is the replayed call's answer only.
    assert [m.role for m in stored] == ["user", "assistant"]
    assert stored[1].text() == "Whole answer."
    # Each failed call's usage is still recorded, with its drop evidence.
    dropped = [u for u in events if isinstance(u, Usage) and u.raw.get("transport_failure")]
    assert [u.raw["transport_drop_retry"] for u in dropped] == list(range(1, drops + 1))


async def test_drops_beyond_the_replay_budget_end_the_turn(server):
    server.script(*[_dropped_reply() for _ in range(2 + native_mod.TRANSPORT_DROP_MAX_RETRIES)])
    loop = _loop(server, [])
    try:
        events = await _turn(loop, "hi")
    finally:
        await loop.aclose()

    error = _terminal(events)
    assert isinstance(error, ErrorEvent) and not error.retriable
    assert len(server.requests) == 1 + native_mod.TRANSPORT_DROP_MAX_RETRIES
    assert len([e for e in events if isinstance(e, ProviderRetry)]) == native_mod.TRANSPORT_DROP_MAX_RETRIES


def test_cut_stream_without_completion_is_reported():
    normalizer = StreamNormalizer()
    for name, data in _events([Interactions.created(), *Interactions.output(0, "hi")]):
        normalizer.consume(name, data)
    assert not normalizer.terminal


def _unused(_: Any) -> None:  # keep imports honest for linters
    return None
