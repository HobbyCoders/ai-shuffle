"""Anthropic Messages request → Responses request translation tests (§D2).

Covers: (3) multi-turn history with tool_use/tool_result joined by call_id,
(7) native-Claude thinking block dropped, plus system/tools/tool_choice/thinking
mapping and the store/stream/include/max_output_tokens invariants.
"""

from __future__ import annotations

import json

import copy

from app.core.translator_gateway.anthropic_to_responses import (
    _strictify_schema,
    translate_request,
)
from app.core.translator_gateway.events import encode_reasoning_signature


def test_request_invariants_store_stream_include():
    body = translate_request({"model": "claude-x", "messages": []})
    assert body["store"] is False
    assert body["stream"] is True
    assert body["include"] == ["reasoning.encrypted_content"]


def test_system_becomes_instructions():
    body = translate_request({"model": "m", "system": "You are helpful.", "messages": []})
    assert body["instructions"] == "You are helpful."

    body2 = translate_request({"model": "m", "system": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}], "messages": []})
    assert body2["instructions"] == "AB"


def test_target_model_overrides_alias():
    body = translate_request({"model": "claude-alias", "messages": []}, target_model="gpt-5.6-sol")
    assert body["model"] == "gpt-5.6-sol"
    # without override, the request model passes through
    body2 = translate_request({"model": "claude-alias", "messages": []})
    assert body2["model"] == "claude-alias"


def test_max_output_tokens_omitted_for_codex_included_for_openai():
    req = {"model": "m", "messages": [], "max_tokens": 512}
    codex = translate_request(req, upstream_mode="codex")
    assert "max_output_tokens" not in codex  # surprise #7
    openai = translate_request(req, upstream_mode="openai")
    assert openai["max_output_tokens"] == 512


def test_tools_and_tool_choice_mapping():
    req = {
        "model": "m",
        "messages": [],
        "tools": [
            {"name": "get_weather", "description": "Weather", "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}
        ],
        "tool_choice": {"type": "tool", "name": "get_weather"},
    }
    body = translate_request(req)
    assert body["tools"][0] == {
        "type": "function",
        "name": "get_weather",
        "description": "Weather",
        "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    }
    assert body["tool_choice"] == {"type": "function", "name": "get_weather"}


def test_tool_choice_auto_any_none():
    assert translate_request({"model": "m", "messages": [], "tools": [{"name": "t", "input_schema": {}}], "tool_choice": {"type": "auto"}})["tool_choice"] == "auto"
    assert translate_request({"model": "m", "messages": [], "tools": [{"name": "t", "input_schema": {}}], "tool_choice": {"type": "any"}})["tool_choice"] == "required"
    assert translate_request({"model": "m", "messages": [], "tools": [{"name": "t", "input_schema": {}}], "tool_choice": {"type": "none"}})["tool_choice"] == "none"


def test_parallel_tool_calls_false_without_tools_and_respects_disable():
    assert translate_request({"model": "m", "messages": []})["parallel_tool_calls"] is False
    # tools present, not disabled → allowed
    body = translate_request({"model": "m", "messages": [], "tools": [{"name": "t", "input_schema": {}}], "tool_choice": {"type": "auto"}})
    assert body["parallel_tool_calls"] is True
    # explicit disable
    body2 = translate_request({"model": "m", "messages": [], "tools": [{"name": "t", "input_schema": {}}], "tool_choice": {"type": "auto", "disable_parallel_tool_use": True}})
    assert body2["parallel_tool_calls"] is False


def test_thinking_budget_maps_to_effort():
    def effort(budget):
        return translate_request({"model": "m", "messages": [], "thinking": {"type": "enabled", "budget_tokens": budget}})["reasoning"]["effort"]

    assert effort(1000) == "low"
    assert effort(8000) == "medium"
    assert effort(32000) == "high"
    body = translate_request({"model": "m", "messages": [], "thinking": {"type": "enabled", "budget_tokens": 8000}})
    assert body["reasoning"]["summary"] == "auto"
    # thinking absent → no reasoning key
    assert "reasoning" not in translate_request({"model": "m", "messages": []})


# ── (3) multi-turn history: tool_use / tool_result joined by call_id ─────────

def test_multi_turn_tool_use_and_result_join_by_call_id():
    history = {
        "model": "m",
        "messages": [
            {"role": "user", "content": "What's the weather in Paris?"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "Let me check."},
                {"type": "tool_use", "id": "call_1", "name": "get_weather", "input": {"city": "Paris", "unit": "celsius"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "18C sunny"},
            ]},
        ],
    }
    body = translate_request(history)
    items = body["input"]

    # user text, assistant text, function_call, function_call_output
    types = [it["type"] for it in items]
    assert types == ["message", "message", "function_call", "function_call_output"]

    # no id / status leaked onto any reconstructed item (surprise #6)
    for it in items:
        assert "id" not in it
        assert "status" not in it

    fc = items[2]
    assert fc["call_id"] == "call_1"
    assert fc["name"] == "get_weather"
    # arguments is a JSON *string* and parses to the original input
    assert json.loads(fc["arguments"]) == {"city": "Paris", "unit": "celsius"}

    fco = items[3]
    assert fco["call_id"] == "call_1"  # join key preserved
    assert fco["output"] == "18C sunny"

    # user content type is input_text, assistant is output_text
    assert items[0]["content"][0]["type"] == "input_text"
    assert items[1]["content"][0]["type"] == "output_text"


# ── (7) native-Claude thinking (foreign signature) dropped ───────────────────

def test_native_claude_thinking_block_dropped():
    # a signature that is NOT our envelope (opaque base64 blob)
    history = {
        "model": "m",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm", "signature": "bm90LW91ci1lbnZlbG9wZS1 qust-garbage=="},
                {"type": "text", "text": "answer"},
            ]},
        ],
    }
    body = translate_request(history)
    types = [it["type"] for it in body["input"]]
    # thinking dropped, only the text message survives — no reasoning fabricated
    assert "reasoning" not in types
    assert types == ["message"]


def test_our_envelope_thinking_becomes_reasoning_item():
    sig = encode_reasoning_signature("ENC_BLOB", [{"type": "summary_text", "text": "s"}], [])
    history = {
        "model": "m",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "s", "signature": sig},
            ]},
        ],
    }
    body = translate_request(history)
    assert len(body["input"]) == 1
    item = body["input"][0]
    assert item["type"] == "reasoning"
    assert item["encrypted_content"] == "ENC_BLOB"
    assert item["summary"] == [{"type": "summary_text", "text": "s"}]
    assert "id" not in item and "status" not in item


# ── system-role messages → developer (Codex 400s role:"system") ──────────────

def test_system_role_message_becomes_developer_input_item():
    history = {
        "model": "m",
        "messages": [
            {"role": "user", "content": "hello"},
            {"role": "system", "content": "Available agent types: foo, bar"},
        ],
    }
    body = translate_request(history)
    items = body["input"]
    # ordering preserved: user first, then the remapped system message
    assert [it["role"] for it in items] == ["user", "developer"]
    dev = items[1]
    assert dev["type"] == "message"
    assert dev["role"] == "developer"
    assert dev["content"] == [{"type": "input_text", "text": "Available agent types: foo, bar"}]


def test_system_role_message_with_block_list_content_maps_same():
    history = {
        "model": "m",
        "messages": [
            {"role": "system", "content": [
                {"type": "text", "text": "A"},
                {"type": "text", "text": "B"},
            ]},
        ],
    }
    body = translate_request(history)
    items = body["input"]
    assert len(items) == 1
    dev = items[0]
    assert dev["type"] == "message"
    assert dev["role"] == "developer"
    assert dev["content"] == [{"type": "input_text", "text": "AB"}]


def test_assistant_text_still_output_text_after_content_type_change():
    history = {
        "model": "m",
        "messages": [
            {"role": "assistant", "content": "sure thing"},
        ],
    }
    body = translate_request(history)
    item = body["input"][0]
    assert item["type"] == "message"
    assert item["role"] == "assistant"
    assert item["content"] == [{"type": "output_text", "text": "sure thing"}]


# ── adaptive thinking → reasoning effort from output_config ──────────────────

def test_adaptive_thinking_effort_from_output_config():
    body = translate_request({
        "model": "m",
        "messages": [],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
    })
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}


def test_adaptive_thinking_without_output_config_defaults_medium():
    body = translate_request({
        "model": "m",
        "messages": [],
        "thinking": {"type": "adaptive"},
    })
    assert body["reasoning"] == {"effort": "medium", "summary": "auto"}


def test_adaptive_thinking_xhigh_clamps_to_high():
    body = translate_request({
        "model": "m",
        "messages": [],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "xhigh"},
    })
    assert body["reasoning"] == {"effort": "high", "summary": "auto"}


# ── strict-mode nullable wrap (surprise #4 adapter) ──────────────────────────

def test_strictify_optional_string_becomes_nullable_and_all_required():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a"],
    }
    out = _strictify_schema(schema)
    # required is now ALL property names
    assert set(out["required"]) == {"a", "b"}
    # the originally-required prop is untouched
    assert out["properties"]["a"] == {"type": "string"}
    # the optional prop is widened to admit null
    assert out["properties"]["b"] == {"type": ["string", "null"]}


def test_strictify_required_props_not_wrapped():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "integer"}, "b": {"type": "boolean"}},
        "required": ["a", "b"],
    }
    out = _strictify_schema(schema)
    assert out["properties"]["a"] == {"type": "integer"}
    assert out["properties"]["b"] == {"type": "boolean"}
    assert set(out["required"]) == {"a", "b"}


def test_strictify_nested_object_recursion():
    schema = {
        "type": "object",
        "properties": {
            "outer": {
                "type": "object",
                "properties": {"x": {"type": "string"}, "y": {"type": "number"}},
                "required": ["x"],
            }
        },
        "required": [],
    }
    out = _strictify_schema(schema)
    outer = out["properties"]["outer"]
    # 'outer' itself was optional → object type widened with null
    assert outer["type"] == ["object", "null"]
    # recursion: nested required = all props, nested optional 'y' made nullable
    assert set(outer["required"]) == {"x", "y"}
    assert outer["properties"]["x"] == {"type": "string"}
    assert outer["properties"]["y"] == {"type": ["number", "null"]}


def test_strictify_array_items_object_recursion():
    schema = {
        "type": "object",
        "properties": {
            "tags": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"k": {"type": "string"}, "v": {"type": "string"}},
                    "required": ["k"],
                },
            }
        },
        "required": ["tags"],
    }
    out = _strictify_schema(schema)
    items = out["properties"]["tags"]["items"]
    assert set(items["required"]) == {"k", "v"}
    assert items["properties"]["k"] == {"type": "string"}
    assert items["properties"]["v"] == {"type": ["string", "null"]}


def test_strictify_enum_optional_prop_adds_null_to_type_union():
    schema = {
        "type": "object",
        "properties": {"mode": {"type": "string", "enum": ["a", "b"]}},
        "required": [],
    }
    out = _strictify_schema(schema)
    mode = out["properties"]["mode"]
    assert mode["type"] == ["string", "null"]
    assert mode["enum"] == ["a", "b"]


def test_strictify_anyof_optional_prop_appends_null_branch():
    schema = {
        "type": "object",
        "properties": {"val": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
        "required": [],
    }
    out = _strictify_schema(schema)
    branches = out["properties"]["val"]["anyOf"]
    assert {"type": "null"} in branches
    assert {"type": "string"} in branches and {"type": "integer"} in branches


def test_strictify_schema_without_type_left_untouched():
    schema = {
        "type": "object",
        "properties": {"anything": {"description": "no type info"}},
        "required": [],
    }
    out = _strictify_schema(schema)
    # typeless (and enum-less) schema is already permissive → unchanged
    assert out["properties"]["anything"] == {"description": "no type info"}
    # still promoted to required
    assert out["required"] == ["anything"]


def test_strictify_does_not_mutate_input():
    schema = {
        "type": "object",
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
        "required": ["a"],
    }
    snapshot = copy.deepcopy(schema)
    _strictify_schema(schema)
    assert schema == snapshot


# ── image content survives translation (B2) ──────────────────────────────────

def _user_image_block(media_type="image/png", data="AAAA"):
    return {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": data}}


def test_user_base64_image_becomes_input_image_data_url():
    history = {
        "model": "m",
        "messages": [
            {"role": "user", "content": [
                {"type": "text", "text": "look at this"},
                _user_image_block("image/png", "AAAA"),
            ]},
        ],
    }
    body = translate_request(history)
    items = body["input"]
    assert len(items) == 1
    msg = items[0]
    assert msg["type"] == "message" and msg["role"] == "user"
    # text and image are interleaved in one content array, in order
    assert msg["content"] == [
        {"type": "input_text", "text": "look at this"},
        {"type": "input_image", "image_url": "data:image/png;base64,AAAA", "detail": "auto"},
    ]


def test_user_image_only_message_produces_input_image_part():
    body = translate_request({"model": "m", "messages": [
        {"role": "user", "content": [_user_image_block("image/jpeg", "ZZZZ")]},
    ]})
    msg = body["input"][0]
    assert msg["content"] == [
        {"type": "input_image", "image_url": "data:image/jpeg;base64,ZZZZ", "detail": "auto"},
    ]


def test_user_url_image_source_passes_url_through():
    body = translate_request({"model": "m", "messages": [
        {"role": "user", "content": [
            {"type": "image", "source": {"type": "url", "url": "https://ex.com/a.png"}},
        ]},
    ]})
    msg = body["input"][0]
    assert msg["content"] == [
        {"type": "input_image", "image_url": "https://ex.com/a.png", "detail": "auto"},
    ]


def test_unsupported_media_type_becomes_text_breadcrumb():
    body = translate_request({"model": "m", "messages": [
        {"role": "user", "content": [
            {"type": "text", "text": "hi"},
            _user_image_block("image/tiff", "AAAA"),
        ]},
    ]})
    msg = body["input"][0]
    assert msg["content"] == [
        {"type": "input_text", "text": "hi"},
        {"type": "input_text", "text": "[unsupported image omitted]"},
    ]


def test_assistant_image_block_dropped_with_breadcrumb_stays_output_text():
    body = translate_request({"model": "m", "messages": [
        {"role": "assistant", "content": [
            {"type": "text", "text": "here"},
            _user_image_block("image/png", "AAAA"),
        ]},
    ]})
    msg = body["input"][0]
    # assistant stays output_text-only; no input_image ever appears on assistant
    assert msg["role"] == "assistant"
    assert msg["content"] == [
        {"type": "output_text", "text": "here"},
        {"type": "output_text", "text": "[unsupported image omitted]"},
    ]


def test_tool_result_image_fallback_placeholder_and_following_user_item():
    history = {
        "model": "m",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_1", "name": "screenshot", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": [
                    {"type": "text", "text": "captured page"},
                    _user_image_block("image/png", "SHOT"),
                ]},
            ]},
        ],
    }
    body = translate_request(history)  # default = fallback
    items = body["input"]
    types = [it["type"] for it in items]
    # function_call, then function_call_output, then the synthetic user message
    assert types == ["function_call", "function_call_output", "message"]

    fco = items[1]
    assert fco["call_id"] == "call_1"
    assert fco["output"] == "captured page[Screenshot captured: img_1]"

    synth = items[2]
    assert synth["role"] == "user"
    assert synth["content"] == [
        {"type": "input_text", "text": "Tool result image img_1 (for tool call call_1):"},
        {"type": "input_image", "image_url": "data:image/png;base64,SHOT", "detail": "auto"},
    ]


def test_tool_result_without_image_is_unchanged_regression():
    # list content with only text → same joined string, no synthetic message
    history = {"model": "m", "messages": [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": [
                {"type": "text", "text": "18C "},
                {"type": "text", "text": "sunny"},
            ]},
        ]},
    ]}
    body = translate_request(history)
    items = body["input"]
    assert [it["type"] for it in items] == ["function_call_output"]
    assert items[0]["output"] == "18C sunny"


def test_multi_image_parallel_tool_use_ordering_and_numbering():
    history = {
        "model": "m",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "call_a", "name": "shot", "input": {}},
                {"type": "tool_use", "id": "call_b", "name": "shot", "input": {}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_a", "content": [
                    _user_image_block("image/png", "A1"),
                    _user_image_block("image/png", "A2"),
                ]},
                {"type": "tool_result", "tool_use_id": "call_b", "content": [
                    _user_image_block("image/png", "B1"),
                ]},
            ]},
        ],
    }
    body = translate_request(history)
    items = body["input"]
    # two function_calls, then per tool_result: its fco immediately followed by
    # its synthetic image messages, in walk order.
    assert [it["type"] for it in items] == [
        "function_call", "function_call",
        "function_call_output", "message", "message",  # call_a + img_1, img_2
        "function_call_output", "message",              # call_b + img_3
    ]
    assert items[2]["output"] == "[Screenshot captured: img_1][Screenshot captured: img_2]"
    assert items[3]["content"][0]["text"] == "Tool result image img_1 (for tool call call_a):"
    assert items[4]["content"][0]["text"] == "Tool result image img_2 (for tool call call_a):"
    assert items[5]["output"] == "[Screenshot captured: img_3]"
    assert items[6]["content"][0]["text"] == "Tool result image img_3 (for tool call call_b):"
    assert items[6]["content"][1]["image_url"] == "data:image/png;base64,B1"


def test_translation_is_deterministic_stable_img_ids():
    history = {
        "model": "m",
        "messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": [_user_image_block("image/png", "X")]},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c2", "content": [_user_image_block("image/png", "Y")]},
            ]},
        ],
    }
    first = translate_request(copy.deepcopy(history))
    second = translate_request(copy.deepcopy(history))
    assert first == second  # byte-for-byte identical output
    # counter is request-scoped: restarts at img_1 on a fresh call, not global
    ids = [
        it["content"][0]["text"]
        for it in first["input"]
        if it["type"] == "message"
    ]
    assert ids == [
        "Tool result image img_1 (for tool call c1):",
        "Tool result image img_2 (for tool call c2):",
    ]
    assert [it["content"][0]["text"] for it in second["input"] if it["type"] == "message"] == ids


def test_native_mode_tool_result_image_array_output_no_synthetic_message():
    history = {
        "model": "m",
        "messages": [
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "c1", "content": [
                    {"type": "text", "text": "page"},
                    _user_image_block("image/png", "SHOT"),
                ]},
            ]},
        ],
    }
    body = translate_request(history, images_native=True)
    items = body["input"]
    # native: only the function_call_output, output is an array; no synthetic msg
    assert [it["type"] for it in items] == ["function_call_output"]
    fco = items[0]
    assert fco["call_id"] == "c1"
    assert fco["output"] == [
        {"type": "input_text", "text": "page"},
        {"type": "input_image", "image_url": "data:image/png;base64,SHOT", "detail": "auto"},
    ]


def test_native_mode_leaves_image_free_tool_result_as_string():
    # native flag must NOT change image-free results (regression)
    history = {"model": "m", "messages": [
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "c1", "content": "plain text"},
        ]},
    ]}
    body = translate_request(history, images_native=True)
    assert body["input"][0]["output"] == "plain text"


def test_translate_tools_applies_strictify_end_to_end():
    req = {
        "model": "m",
        "messages": [],
        "tools": [
            {
                "name": "edit",
                "description": "Edit",
                "input_schema": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}, "old": {"type": "string"}},
                    "required": ["path"],
                },
            }
        ],
    }
    body = translate_request(req)
    params = body["tools"][0]["parameters"]
    assert set(params["required"]) == {"path", "old"}
    assert params["properties"]["path"] == {"type": "string"}
    assert params["properties"]["old"] == {"type": ["string", "null"]}
