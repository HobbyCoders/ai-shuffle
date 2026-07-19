"""Reasoning round-trip tests (§D4 / §D6, ruling R3).

Covers: (4) a Responses reasoning item with encrypted_content → Anthropic
thinking block with envelope signature → back to a reasoning input item verbatim
(id/status stripped); and (5) the COMBINED resend — one history carrying BOTH a
reasoning item AND function_call/function_call_output through the full round-trip.
"""

from __future__ import annotations

import json

from app.core.translator_gateway.anthropic_to_responses import translate_request
from app.core.translator_gateway.events import (
    decode_reasoning_signature,
    encode_reasoning_signature,
)
from app.core.translator_gateway.responses_to_anthropic import (
    ResponsesToAnthropicTranslator,
    translate_responses_stream,
)

from tests.core.translator_gateway import _fixtures


def test_envelope_codec_roundtrip_and_foreign_rejected():
    sig = encode_reasoning_signature("BLOB", [{"type": "summary_text", "text": "x"}], [{"foo": 1}])
    env = decode_reasoning_signature(sig)
    assert env is not None
    assert env["encrypted_content"] == "BLOB"
    assert env["summary"] == [{"type": "summary_text", "text": "x"}]
    assert env["content"] == [{"foo": 1}]
    # deterministic
    assert encode_reasoning_signature("BLOB", [{"type": "summary_text", "text": "x"}], [{"foo": 1}]) == sig
    # foreign / garbage signatures decode to None
    assert decode_reasoning_signature(None) is None
    assert decode_reasoning_signature("") is None
    assert decode_reasoning_signature("not-base64-!!!") is None
    assert decode_reasoning_signature("aGVsbG8gd29ybGQ=") is None  # valid b64, not our JSON


# ── (4) reasoning item → thinking block(envelope) → reasoning item verbatim ──

def test_reasoning_item_streams_as_thinking_with_signature():
    enc = "ENCRYPTED_" + ("z" * 80)
    events = _fixtures.reasoning_stream(encrypted_content=enc, summary_text="**Deriving x**", answer="75")
    frames = list(translate_responses_stream(events))

    # thinking block: start (thinking) → thinking_delta → signature_delta → stop
    kinds = [f["type"] for f in frames]
    # first content block is the thinking block
    first_start = frames[kinds.index("content_block_start")]
    assert first_start["content_block"]["type"] == "thinking"

    thinking_text = "".join(
        f["delta"]["thinking"] for f in frames
        if f["type"] == "content_block_delta" and f["delta"]["type"] == "thinking_delta"
    )
    assert thinking_text == "**Deriving x**"

    sig_delta = next(
        f for f in frames
        if f["type"] == "content_block_delta" and f["delta"]["type"] == "signature_delta"
    )
    signature = sig_delta["delta"]["signature"]

    # signature carries the round-trip envelope with the encrypted_content verbatim
    env = decode_reasoning_signature(signature)
    assert env is not None
    assert env["encrypted_content"] == enc
    assert env["summary"] == [{"type": "summary_text", "text": "**Deriving x**"}]

    # a text block for the answer follows the (closed) thinking block
    assert "content_block_stop" in kinds
    text = "".join(
        f["delta"]["text"] for f in frames
        if f["type"] == "content_block_delta" and f["delta"]["type"] == "text_delta"
    )
    assert text == "75"


def test_reasoning_thinking_block_translates_back_to_reasoning_item_verbatim():
    enc = "ENCRYPTED_" + ("q" * 40)
    events = _fixtures.reasoning_stream(encrypted_content=enc, summary_text="**S**", answer="42")
    frames = list(translate_responses_stream(events))

    signature = next(
        f["delta"]["signature"] for f in frames
        if f["type"] == "content_block_delta" and f["delta"].get("type") == "signature_delta"
    )

    # Claude Code resends the thinking block verbatim (with our signature).
    resend = {
        "model": "m",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "**S**", "signature": signature},
                {"type": "text", "text": "42"},
            ]},
        ],
    }
    body = translate_request(resend)
    reasoning_items = [it for it in body["input"] if it["type"] == "reasoning"]
    assert len(reasoning_items) == 1
    item = reasoning_items[0]
    assert item["encrypted_content"] == enc  # verbatim
    assert item["summary"] == [{"type": "summary_text", "text": "**S**"}]
    assert "id" not in item and "status" not in item  # stripped


# ── (5) COMBINED resend: reasoning + function_call + function_call_output ─────

def test_combined_reasoning_and_tool_call_full_roundtrip():
    """Ruling R3: one history carrying BOTH a resent reasoning item AND
    function_call / function_call_output survives the full round-trip."""
    enc = "ENC_COMBINED_" + ("k" * 32)
    arguments = '{"city":"Tokyo","unit":"fahrenheit","include_forecast":false}'

    # 1) Build a Responses turn that emits a reasoning item + a function call.
    translator = ResponsesToAnthropicTranslator()
    reasoning_events = _fixtures.reasoning_stream(encrypted_content=enc, summary_text="**plan**", answer="")
    # drop the trailing message + completed from the reasoning fixture, then splice
    # in a function call so one turn carries reasoning THEN a tool call.
    reasoning_only = reasoning_events[:7]  # up to reasoning output_item.done
    tool_events = _fixtures.tool_call_stream(arguments=arguments, call_id="call_9", name="get_weather")
    tool_tail = tool_events[1:]  # skip its response.created (already started)
    combined = reasoning_only + tool_tail

    anthropic_frames = []
    for name, payload in combined:
        anthropic_frames.extend(translator.push(name, payload))

    # Extract the thinking signature and the tool_use block Claude Code would keep.
    signature = next(
        f["delta"]["signature"] for f in anthropic_frames
        if f["type"] == "content_block_delta" and f["delta"].get("type") == "signature_delta"
    )
    tool_start = next(
        f for f in anthropic_frames
        if f["type"] == "content_block_start" and f["content_block"]["type"] == "tool_use"
    )
    tool_args = "".join(
        f["delta"]["partial_json"] for f in anthropic_frames
        if f["type"] == "content_block_delta" and f["delta"].get("type") == "input_json_delta"
    )
    assert tool_args == arguments  # byte-exact through the stream

    # 2) Reconstruct an Anthropic history with BOTH, plus the tool result, and
    #    translate back to Responses (the stateless full-history resend).
    resend = {
        "model": "m",
        "messages": [
            {"role": "user", "content": "weather in Tokyo?"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "**plan**", "signature": signature},
                {"type": "tool_use", "id": tool_start["content_block"]["id"], "name": "get_weather", "input": json.loads(tool_args)},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_9", "content": "12F"},
            ]},
        ],
    }
    body = translate_request(resend)
    types = [it["type"] for it in body["input"]]
    assert types == ["message", "reasoning", "function_call", "function_call_output"]

    reasoning_item = body["input"][1]
    assert reasoning_item["encrypted_content"] == enc  # verbatim across the trip

    fc = body["input"][2]
    assert fc["call_id"] == "call_9"  # byte-exact call_id (§D6)
    assert json.loads(fc["arguments"]) == {"city": "Tokyo", "unit": "fahrenheit", "include_forecast": False}

    fco = body["input"][3]
    assert fco["call_id"] == "call_9"

    # id/status stripped from every reconstructed item (surprise #6)
    for it in body["input"]:
        assert "id" not in it and "status" not in it
