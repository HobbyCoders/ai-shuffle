"""Responses SSE → Anthropic SSE translation tests (§D3).

Covers findings-doc cases: (1) plain text end-to-end, (2) streaming tool call
with 16+ obfuscated fragments reassembling byte-exact, (6) usage mapping, and
(9) empty response.completed.output ignored in favor of output_item.done.
"""

from __future__ import annotations

import json

from app.core.translator_gateway.responses_to_anthropic import (
    ResponsesToAnthropicTranslator,
    _normalize_usage,
    strip_null_values,
    translate_responses_stream,
)

from tests.core.translator_gateway import _fixtures


def _run(events):
    """Push a synthetic stream, return the flat list of Anthropic frames."""
    return list(translate_responses_stream(events))


def _types(frames):
    return [f["type"] for f in frames]


# ── (1) plain text end-to-end ────────────────────────────────────────────────

def test_plain_text_event_ordering_and_final_text():
    frames = _run(_fixtures.plain_text_stream(text="PONG", n_deltas=4))
    kinds = _types(frames)

    # Canonical Anthropic sequence.
    assert kinds[0] == "message_start"
    assert kinds[1] == "content_block_start"
    assert kinds[-1] == "message_stop"
    assert kinds[-2] == "message_delta"
    # exactly one text block opened + closed
    assert kinds.count("content_block_start") == 1
    assert kinds.count("content_block_stop") == 1
    # content_block_stop precedes message_delta precedes message_stop
    assert kinds.index("content_block_stop") < kinds.index("message_delta") < kinds.index("message_stop")

    start = frames[0]["message"]
    assert start["role"] == "assistant"
    assert start["model"] == "gpt-5.6-sol"
    assert start["content"] == []

    # reassembled text
    text = "".join(
        f["delta"]["text"]
        for f in frames
        if f["type"] == "content_block_delta" and f["delta"]["type"] == "text_delta"
    )
    assert text == "PONG"

    # first content block is a text block at index 0
    cb_start = frames[kinds.index("content_block_start")]
    assert cb_start["content_block"]["type"] == "text"
    assert cb_start["index"] == 0


def test_message_delta_carries_stop_reason_end_turn():
    frames = _run(_fixtures.plain_text_stream())
    delta = next(f for f in frames if f["type"] == "message_delta")
    assert delta["delta"]["stop_reason"] == "end_turn"


# ── (2) streaming tool call: byte-exact argument reassembly ──────────────────

def test_tool_call_arguments_consolidated_into_single_delta():
    # New contract: the streamed argument fragments are buffered and re-emitted as
    # ONE consolidated, null-stripped, compactly re-serialized input_json_delta.
    arguments = '{"city":"Paris","unit":"celsius","include_forecast":true}'
    frames = _run(_fixtures.tool_call_stream(arguments=arguments, n_deltas=16))

    # tool_use block opened with preserved call_id + name
    cb_start = next(f for f in frames if f["type"] == "content_block_start")
    assert cb_start["content_block"]["type"] == "tool_use"
    assert cb_start["content_block"]["id"] == "call_abc123"
    assert cb_start["content_block"]["name"] == "get_weather"
    assert cb_start["content_block"]["input"] == {}

    partials = [
        f["delta"]["partial_json"]
        for f in frames
        if f["type"] == "content_block_delta" and f["delta"]["type"] == "input_json_delta"
    ]
    # exactly one delta regardless of how many fragments streamed
    assert len(partials) == 1
    joined = partials[0]
    assert json.loads(joined) == {"city": "Paris", "unit": "celsius", "include_forecast": True}
    # already compact + null-free → re-serialization is byte-identical
    assert joined == arguments


def test_tool_call_arguments_null_stripped_in_single_delta():
    # The upstream (nullable-wrapped optional params) emits explicit nulls; they
    # must be stripped, non-null falsy values (0) preserved.
    arguments = '{"path":"x","old_string":null,"count":0,"nested":{"a":1,"b":null}}'
    frames = _run(_fixtures.tool_call_stream(arguments=arguments, n_deltas=8))
    partials = [
        f["delta"]["partial_json"]
        for f in frames
        if f["type"] == "content_block_delta" and f["delta"]["type"] == "input_json_delta"
    ]
    assert len(partials) == 1
    assert json.loads(partials[0]) == {"path": "x", "count": 0, "nested": {"a": 1}}
    assert "null" not in partials[0]  # no explicit nulls survive


def test_tool_call_arguments_no_nulls_single_delta_semantically_identical():
    arguments = '{"a":1,"b":"two","c":[1,2,3]}'
    frames = _run(_fixtures.tool_call_stream(arguments=arguments, n_deltas=5))
    partials = [
        f["delta"]["partial_json"]
        for f in frames
        if f["type"] == "content_block_delta" and f["delta"]["type"] == "input_json_delta"
    ]
    assert len(partials) == 1
    assert json.loads(partials[0]) == {"a": 1, "b": "two", "c": [1, 2, 3]}


def test_tool_call_arguments_parse_failure_emitted_verbatim():
    # Safety rule: malformed JSON is NEVER dropped or truncated — the raw buffered
    # string is emitted verbatim as the single delta.
    bad = '{"a": 1, "b":'  # invalid JSON
    frames = _run(_fixtures.tool_call_stream(arguments=bad, n_deltas=1))
    partials = [
        f["delta"]["partial_json"]
        for f in frames
        if f["type"] == "content_block_delta" and f["delta"]["type"] == "input_json_delta"
    ]
    assert len(partials) == 1
    assert partials[0] == bad


def test_tool_call_stop_reason_is_tool_use():
    frames = _run(_fixtures.tool_call_stream())
    delta = next(f for f in frames if f["type"] == "message_delta")
    assert delta["delta"]["stop_reason"] == "tool_use"


def test_obfuscation_fields_never_appear_in_output():
    frames = _run(_fixtures.tool_call_stream(n_deltas=20))
    blob = json.dumps(frames)
    assert "obfuscation" not in blob
    assert "PAD" not in blob


# ── (6) usage mapping incl. reasoning subset + cache fields ──────────────────

def test_usage_mapping_reasoning_subset_and_cache_fields():
    events = _fixtures.reasoning_stream()  # usage 99/52/45, cache 0
    translator = ResponsesToAnthropicTranslator()
    frames = []
    for name, payload in events:
        frames.extend(translator.push(name, payload))

    delta = next(f for f in frames if f["type"] == "message_delta")
    usage = delta["usage"]
    assert usage["input_tokens"] == 99
    # output_tokens already includes reasoning (45) — must NOT be re-added
    assert usage["output_tokens"] == 52
    assert usage["cache_read_input_tokens"] == 0
    assert usage["cache_creation_input_tokens"] == 0
    # reasoning_tokens is NOT surfaced as an Anthropic usage field
    assert "reasoning_tokens" not in usage

    # but it IS available on the normalized final response for internal accounting
    final = translator.final_response()
    assert final.usage.reasoning_tokens == 45
    assert final.usage.output_tokens == 52


def test_usage_cache_fields_map_from_details():
    events = _fixtures.plain_text_stream()
    # inject warm-cache usage on the completed event
    events[-1] = (
        "response.completed",
        {"response": {"id": "r", "model": "m", "status": "completed", "output": [], "usage": {
            "input_tokens": 200,
            "input_tokens_details": {"cached_tokens": 150, "cache_write_tokens": 30},
            "output_tokens": 10,
            "output_tokens_details": {"reasoning_tokens": 0},
            "total_tokens": 210,
        }}},
    )
    frames = _run(events)
    usage = next(f for f in frames if f["type"] == "message_delta")["usage"]
    assert usage["cache_read_input_tokens"] == 150
    assert usage["cache_creation_input_tokens"] == 30
    # Anthropic semantics: input_tokens EXCLUDES the cache subsets (200 - 150 - 30).
    assert usage["input_tokens"] == 20
    # Invariant: emitted input + cache_read + cache_creation == upstream input_tokens.
    assert (
        usage["input_tokens"]
        + usage["cache_read_input_tokens"]
        + usage["cache_creation_input_tokens"]
        == 200
    )


# ── _normalize_usage: cache subsets subtracted from input_tokens ─────────────

def test_normalize_usage_subtracts_cache_subsets_from_input():
    usage = {
        "input_tokens": 100,
        "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 20},
        "output_tokens": 30,
        "output_tokens_details": {"reasoning_tokens": 10},
        "total_tokens": 130,
    }
    norm = _normalize_usage(usage)
    assert norm.input_tokens == 20            # 100 - 60 - 20
    assert norm.cache_read_input_tokens == 60
    assert norm.cache_creation_input_tokens == 20
    assert norm.output_tokens == 30
    assert norm.reasoning_tokens == 10
    assert norm.total_tokens == 130           # raw upstream total, unchanged
    # Invariant: emitted input + caches == upstream input_tokens.
    assert (
        norm.input_tokens
        + norm.cache_read_input_tokens
        + norm.cache_creation_input_tokens
        == 100
    )


def test_normalize_usage_clamps_input_at_zero_when_caches_exceed_input():
    # Defensive: if the cache subsets exceed input_tokens, clamp at 0 (never negative).
    usage = {
        "input_tokens": 50,
        "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 20},
        "output_tokens": 5,
        "total_tokens": 55,
    }
    norm = _normalize_usage(usage)
    assert norm.input_tokens == 0
    assert norm.cache_read_input_tokens == 60
    assert norm.cache_creation_input_tokens == 20


def test_normalize_usage_missing_details_passthrough():
    # No input_tokens_details → input_tokens unchanged, cache fields zero.
    usage = {"input_tokens": 100, "output_tokens": 30, "total_tokens": 130}
    norm = _normalize_usage(usage)
    assert norm.input_tokens == 100
    assert norm.cache_read_input_tokens == 0
    assert norm.cache_creation_input_tokens == 0
    assert norm.total_tokens == 130


def test_message_delta_usage_carries_subtracted_input_tokens_end_to_end():
    # End-to-end streaming: the emitted message_delta usage frame must carry the
    # cache-subtracted input_tokens, not the raw upstream value.
    events = _fixtures.plain_text_stream()
    events[-1] = (
        "response.completed",
        {"response": {"id": "r", "model": "m", "status": "completed", "output": [], "usage": {
            "input_tokens": 100,
            "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 20},
            "output_tokens": 30,
            "output_tokens_details": {"reasoning_tokens": 10},
            "total_tokens": 130,
        }}},
    )
    frames = _run(events)
    usage = next(f for f in frames if f["type"] == "message_delta")["usage"]
    assert usage["input_tokens"] == 20
    assert usage["cache_read_input_tokens"] == 60
    assert usage["cache_creation_input_tokens"] == 20
    assert usage["output_tokens"] == 30
    assert "reasoning_tokens" not in usage


# ── (9) empty response.completed.output ignored; output_item.done wins ───────

def test_final_assembly_from_output_item_done_not_completed_output():
    # response.completed.output is [] (as it always is on this backend). The
    # final tool call must still be recovered from output_item.done.
    events = _fixtures.tool_call_stream(arguments='{"a":1}', call_id="call_z", name="do_it")
    completed = events[-1][1]["response"]
    assert completed["output"] == []  # sanity: fixture reproduces surprise #2

    translator = ResponsesToAnthropicTranslator()
    for name, payload in events:
        translator.push(name, payload)

    final = translator.final_response()
    assert len(final.items) == 1
    item = final.items[0]
    assert item.__class__.__name__ == "FinalToolCallItem"
    assert item.call_id == "call_z"
    assert item.name == "do_it"
    assert item.arguments == '{"a":1}'
    assert final.stop_reason == "tool_use"


def test_max_tokens_stop_reason_when_truncated():
    events = _fixtures.plain_text_stream()
    events[-1] = (
        "response.completed",
        {"response": {"id": "r", "model": "m", "status": "incomplete",
                      "incomplete_details": {"reason": "max_output_tokens"},
                      "output": [], "usage": {"input_tokens": 5, "output_tokens": 5, "total_tokens": 10}}},
    )
    frames = _run(events)
    delta = next(f for f in frames if f["type"] == "message_delta")
    assert delta["delta"]["stop_reason"] == "max_tokens"


# ── strip_null_values (return-path counterpart of the request nullable wrap) ──

def test_strip_null_values_top_level_null_removed():
    assert strip_null_values({"a": 1, "b": None}) == {"a": 1}


def test_strip_null_values_nested_null_removed():
    assert strip_null_values(
        {"outer": {"keep": "v", "drop": None}, "top": None}
    ) == {"outer": {"keep": "v"}}


def test_strip_null_values_null_inside_list_of_objects_removed():
    assert strip_null_values(
        {"items": [{"a": 1, "b": None}, {"c": None, "d": 2}]}
    ) == {"items": [{"a": 1}, {"d": 2}]}


def test_strip_null_values_preserves_non_null_falsy():
    # 0, "", False, [] and {} are NOT None and must survive.
    src = {"zero": 0, "empty_str": "", "false": False, "empty_list": [], "empty_obj": {}, "drop": None}
    assert strip_null_values(src) == {
        "zero": 0,
        "empty_str": "",
        "false": False,
        "empty_list": [],
        "empty_obj": {},
    }
