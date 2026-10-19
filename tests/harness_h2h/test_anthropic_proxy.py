"""Offline tests for the bench's Anthropic → fluid Codex proxy."""
from __future__ import annotations

import json

import pytest

pytest.importorskip("starlette")

from fluid.messages import Reasoning, Text, ToolResultBlock, ToolUseBlock  # noqa: E402
from fluid.providers.base import StreamChunk  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

from scripts.harness_h2h import anthropic_proxy as P  # noqa: E402

REASONING_ITEM = {"type": "reasoning", "id": "rs_1", "encrypted_content": "BLOB",
                  "summary": [{"type": "summary_text", "text": "think"}]}


def tool_turn():
    return [
        StreamChunk("message_start", {"usage": {}, "message": {}}),
        StreamChunk("block_start", {"index": 0, "block_type": "thinking", "subtype": "reasoning_summary"}),
        StreamChunk("thinking_delta", {"index": 0, "text": "think"}),
        StreamChunk("block_stop", {"index": 0, "block": REASONING_ITEM}),
        StreamChunk("block_start", {"index": 1, "block_type": "tool_use", "tool_name": "Bash",
                                    "tool_use_id": "call_1"}),
        StreamChunk("tool_args_delta", {"index": 1, "partial_json": '{"command":'}),
        StreamChunk("tool_args_delta", {"index": 1, "partial_json": '"echo hi"}'}),
        StreamChunk("block_stop", {"index": 1, "block": {"type": "function_call"}}),
        StreamChunk("usage", {"usage": {"input_tokens": 100, "cache_read_input_tokens": 900,
                                        "output_tokens": 20}, "stop_reason": "tool_use"}),
        StreamChunk("message_stop", {}),
    ]


def text_turn():
    return [
        StreamChunk("message_start", {"usage": {}, "message": {}}),
        StreamChunk("text_delta", {"index": 0, "text": "done"}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"input_tokens": 5, "output_tokens": 1}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]


class FakeClient:
    name = "openai_responses"

    def __init__(self, scripts):
        self.scripts = list(scripts)
        self.requests = []

    async def stream(self, request):
        self.requests.append(request)
        for chunk in self.scripts.pop(0):
            yield chunk


def _events(body: str) -> list[tuple[str, dict]]:
    out = []
    for frame in body.strip().split("\n\n"):
        lines = frame.split("\n")
        out.append((lines[0][len("event: "):], json.loads(lines[1][len("data: "):])))
    return out


def test_signature_round_trips_the_reasoning_item():
    sig = P.encode_signature(REASONING_ITEM)
    assert sig.startswith(P.SIGNATURE_PREFIX)
    assert P.decode_signature(sig) == REASONING_ITEM
    assert P.decode_signature("anthropic-signature") is None


def test_build_request_maps_blocks_tools_and_cache_key():
    body = {
        "model": "claude-opus-5", "stream": True,
        "metadata": {"user_id": "user_abc_account_x_session_1234abcd-0000-1111-2222-333344445555"},
        "system": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}],
        "tools": [{"name": "Bash", "input_schema": {"$schema": "x", "type": "object"}},
                  {"type": "web_search_20250305", "name": "web_search"}],
        "tool_choice": {"type": "any"},
        "messages": [
            {"role": "user", "content": "fix it"},
            {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "t", "signature": P.encode_signature(REASONING_ITEM)},
                {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {"command": "ls"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "call_1", "content": "a.py", "is_error": False},
                {"type": "text", "text": "go on"}]},
        ],
    }
    req = P.build_request(body, target_model="gpt-6.1-sol", effort="high", small_effort="low",
                          wire_states={})
    assert req.model == "gpt-6.1-sol" and req.effort == "high"
    assert req.system == "A\n\nB"
    assert [t["name"] for t in req.tools] == ["Bash"]
    assert "$schema" not in req.tools[0]["input_schema"]
    assert req.extra["prompt_cache_key"].startswith("cc:1234abcd-0000-1111-2222-333344445555:")
    assert req.extra["tool_choice"] == "required"
    assistant = req.messages[1].content
    assert isinstance(assistant[0], Reasoning) and assistant[0].raw == REASONING_ITEM
    assert assistant[0].model == "gpt-6.1-sol"
    assert isinstance(assistant[1], ToolUseBlock)
    result, text = req.messages[2].content
    assert isinstance(result, ToolResultBlock) and result.content[0].text == "a.py"
    assert isinstance(text, Text)


def test_each_agent_thread_gets_its_own_cache_partition():
    def key(system):
        body = {"model": "claude-opus-5", "metadata": {"user_id": "session_aaaaaaaa-1"},
                "system": system, "messages": [{"role": "user", "content": "x"}]}
        return P.build_request(body, target_model="m", effort="high", small_effort="low",
                               wire_states={}).extra["prompt_cache_key"]
    assert key("main prompt") == key("main prompt")
    assert key("main prompt") != key("explore subagent prompt")


def test_small_model_gets_its_own_partition_and_effort():
    body = {"model": "claude-haiku-4-5", "metadata": {"user_id": "session_aaaaaaaa-1"},
            "messages": [{"role": "user", "content": "title?"}]}
    req = P.build_request(body, target_model="m", effort="high", small_effort="low", wire_states={})
    assert req.effort == "low"
    assert req.extra["prompt_cache_key"].endswith(":small")


def test_streaming_response_is_valid_anthropic_sse():
    client = FakeClient([tool_turn()])
    app = P.make_app(client, target_model="gpt-6.1-sol", effort="high", small_effort="low")
    with TestClient(app) as tc:
        resp = tc.post("/v1/messages", json={"model": "claude-opus-5", "stream": True,
                                             "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    events = _events(resp.text)
    names = [e for e, _ in events]
    assert names[0] == "message_start" and names[-2:] == ["message_delta", "message_stop"]
    starts = [d["content_block"] for e, d in events if e == "content_block_start"]
    assert [b["type"] for b in starts] == ["thinking", "tool_use"]
    assert starts[1]["name"] == "Bash" and starts[1]["id"] == "call_1"
    sig = [d["delta"]["signature"] for e, d in events
           if e == "content_block_delta" and d["delta"]["type"] == "signature_delta"]
    assert P.decode_signature(sig[0]) == REASONING_ITEM
    args = "".join(d["delta"]["partial_json"] for e, d in events
                   if e == "content_block_delta" and d["delta"]["type"] == "input_json_delta")
    assert json.loads(args) == {"command": "echo hi"}
    delta = [d for e, d in events if e == "message_delta"][0]
    assert delta["delta"]["stop_reason"] == "tool_use"
    assert delta["usage"]["cache_read_input_tokens"] == 900


def test_non_streaming_response_assembles_message():
    client = FakeClient([text_turn()])
    app = P.make_app(client, target_model="m", effort="high", small_effort="low")
    with TestClient(app) as tc:
        resp = tc.post("/v1/messages", json={"model": "x", "messages": [{"role": "user", "content": "hi"}]})
    msg = resp.json()
    assert msg["content"] == [{"type": "text", "text": "done"}]
    assert msg["stop_reason"] == "end_turn"


def test_error_before_output_returns_http_status():
    client = FakeClient([[StreamChunk("error", {"message": "slow down", "code": "rate_limit_exceeded"})]])
    app = P.make_app(client, target_model="m", effort="high", small_effort="low")
    with TestClient(app) as tc:
        resp = tc.post("/v1/messages", json={"model": "x", "stream": True,
                                             "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 429
    assert resp.json()["type"] == "error"
