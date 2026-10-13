"""The content codec reads sessions written by a newer release (invariant I0).

Workers update one at a time, and a session can resume on any of them. A
newer release that writes a new block kind, or a new field on a known block,
must not make the older release fail to load the session. Before this codec
change ``block_from_dict`` raised ``KeyError`` on an unknown kind and
``TypeError`` on an unknown field, and the JSONL store then skipped the whole
node as a corrupt line.
"""

import json
from dataclasses import asdict

from fluid.context.cache_probe import fingerprint_prefix
from fluid.context.recall import expand_ref
from fluid.context.trim import block_chars, estimate_tokens_of_messages
from fluid.messages import (
    Message,
    Reasoning,
    Text,
    ToolResultBlock,
    ToolUseBlock,
    UnknownBlock,
    block_from_dict,
)
from fluid.providers._chat_completions import translate_message
from fluid.providers.anthropic.serialize import build_request_body as anthropic_body
from fluid.providers.base import ProviderRequest
from fluid.providers.local.text_tools import serialize_history
from fluid.providers.openai_responses.serialize import build_request_body as responses_body
from fluid.store.jsonl import JsonlStore

# Text only a future block carries. It must never reach a model request.
_MARKER = "FUTURE-ONLY-PAYLOAD"

_FUTURE_BLOCK = {
    "kind": "opaqueblock",
    "origin": "openai_responses",
    "payload": {"blob": _MARKER, "n": [1, 2, 3]},
}
_FUTURE_REASONING = {
    "kind": "reasoning",
    "origin": "anthropic",
    "summary_text": "thought about it",
    "signature": "sig",
    "encrypted_content": None,
    "raw": {"type": "thinking", "thinking": "thought about it", "signature": "sig"},
    "model": "claude-opus-5",
    "comp_hash": _MARKER,  # a field this release does not know
}


def _future_session_messages() -> list[dict]:
    """Three messages as a newer release would store them."""
    return [
        {
            "role": "user",
            "content": [{"kind": "text", "text": "hello"}, dict(_FUTURE_BLOCK)],
            "provider_meta": {},
        },
        {
            "role": "assistant",
            "content": [
                dict(_FUTURE_REASONING),
                {"kind": "text", "text": "calling a tool"},
                {"kind": "tooluseblock", "id": "t1", "name": "Read", "input": {"path": "a"}},
            ],
            "provider_meta": {"model": "claude-opus-5"},
        },
        {
            "role": "user",
            "content": [
                {
                    "kind": "toolresultblock",
                    "tool_use_id": "t1",
                    "content": [{"kind": "text", "text": "file body"}, dict(_FUTURE_BLOCK)],
                    "is_error": False,
                    "delivered_steers": [
                        {"text": "steer", "origin": "user", "sender": None, "id": "s1",
                         "priority": "urgent"},  # a SteerDelivery field from the future
                    ],
                    "delivery_metadata_version": 1,
                    "render_hint": "compact",  # a ToolResultBlock field from the future
                },
            ],
            "provider_meta": {},
        },
    ]


def _write_future_session(directory, session_id: str) -> None:
    lines = [{"t": "meta", "session_id": session_id, "created_at": 1.0,
              "head_node_id": None, "cli_session_id": None, "project_root": None}]
    parent = None
    for i, message in enumerate(_future_session_messages()):
        node_id = f"n{i}"
        lines.append({
            "t": "node",
            "node": {"node_id": node_id, "parent_id": parent, "ts": float(i), "message": message},
            "head": True,
        })
        parent = node_id
    (directory / f"{session_id}.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8"
    )


def _assert_marker_absent(payload) -> None:
    assert _MARKER not in json.dumps(payload, default=str)


async def test_content_codec_forward_compat(tmp_path) -> None:
    # An unknown kind decodes to UnknownBlock and serializes to the same dict.
    block = block_from_dict(_FUTURE_BLOCK)
    assert isinstance(block, UnknownBlock)
    assert block.kind == "opaqueblock"
    assert block.to_dict() == _FUTURE_BLOCK

    # An unknown field on a known block is dropped; the known fields survive.
    reasoning = block_from_dict(_FUTURE_REASONING)
    assert isinstance(reasoning, Reasoning)
    assert not hasattr(reasoning, "comp_hash")
    assert reasoning.model == "claude-opus-5"
    assert reasoning.raw == _FUTURE_REASONING["raw"]

    # A future session file loads whole: before the fix every node carrying
    # an unknown kind or field was skipped as a corrupt line.
    _write_future_session(tmp_path, "future")
    store = JsonlStore(tmp_path, lock=False)
    path = await store.load_path("future")
    assert [n.node_id for n in path] == ["n0", "n1", "n2"]
    user, assistant, result = (n.message for n in path)
    assert isinstance(user.content[1], UnknownBlock)
    assert isinstance(assistant.content[0], Reasoning)
    tool_result = result.content[0]
    assert isinstance(tool_result, ToolResultBlock)
    assert isinstance(tool_result.content[1], UnknownBlock)
    assert tool_result.delivered_steers[0].text == "steer"

    # Saving it again keeps every unknown block verbatim.
    resaved = user.to_dict()["content"][1]
    assert resaved == _FUTURE_BLOCK
    assert result.to_dict()["content"][0]["content"][1] == _FUTURE_BLOCK


def test_unknown_blocks_round_trip_through_a_message() -> None:
    for raw in _future_session_messages():
        once = Message.from_dict(raw).to_dict()
        assert Message.from_dict(once).to_dict() == once
    first = Message.from_dict(_future_session_messages()[0]).to_dict()
    assert first["content"][1] == _FUTURE_BLOCK


def test_decoding_does_not_alias_the_stored_dict() -> None:
    stored = json.loads(json.dumps(_FUTURE_BLOCK))
    block = block_from_dict(stored)
    stored["payload"]["n"].append(4)
    assert block.to_dict() == _FUTURE_BLOCK
    out = block.to_dict()
    out["payload"]["blob"] = "changed"
    assert block.to_dict() == _FUTURE_BLOCK


def test_a_block_with_no_kind_is_unknown_not_an_error() -> None:
    block = block_from_dict({"text": "orphan"})
    assert isinstance(block, UnknownBlock)
    assert block.kind == ""
    assert block.to_dict() == {"text": "orphan"}


def _future_messages() -> list[Message]:
    return [Message.from_dict(m) for m in _future_session_messages()]


def test_every_provider_serializer_skips_unknown_blocks() -> None:
    messages = _future_messages()

    body = anthropic_body(ProviderRequest(model="claude-opus-5", messages=messages))
    _assert_marker_absent(body)
    assert body["messages"][0]["content"] == [{"type": "text", "text": "hello"}]

    body = responses_body(ProviderRequest(model="gpt-5.6-sol", messages=messages))
    _assert_marker_absent(body)
    outputs = [i for i in body["input"] if i.get("type") == "function_call_output"]
    assert outputs == [{"type": "function_call_output", "call_id": "t1", "output": "file body"}]

    translated = [item for m in messages for item in translate_message(m)]
    _assert_marker_absent(translated)
    tool_msgs = [m for m in translated if m.get("role") == "tool"]
    assert tool_msgs[0]["content"] == "file body"

    # The local text-tools leg flattens tool traffic to text, then goes out
    # through the same chat/completions translation.
    history = serialize_history(messages)
    local_text = "".join(b.text for m in history for b in m.content if isinstance(b, Text))
    assert _MARKER not in local_text
    assert "file body" in local_text
    _assert_marker_absent([item for m in history for item in translate_message(m)])


def test_context_tools_tolerate_unknown_blocks() -> None:
    messages = _future_messages()
    unknown = messages[0].content[1]
    assert block_chars(unknown) == 0
    assert estimate_tokens_of_messages(messages) > 0
    assert asdict(unknown)["kind"] == "opaqueblock"
    fingerprint_prefix(system="s", tools=[], messages=messages)

    from fluid.store.base import SessionNode

    nodes = [
        SessionNode(node_id=f"node_a{i}", parent_id=f"node_a{i - 1}" if i else None, message=m)
        for i, m in enumerate(messages)
    ]
    text, is_error = expand_ref(nodes, "node_a0..node_a2")
    assert not is_error
    assert _MARKER not in text
    assert "hello" in text and "file body" in text


def test_known_blocks_still_decode_exactly() -> None:
    message = Message(
        role="assistant",
        content=[
            Text("hi"),
            ToolUseBlock(id="t", name="Read", input={"p": 1}),
            Reasoning(origin="anthropic", signature="s", raw={"type": "thinking"}, model="m"),
        ],
    )
    assert Message.from_dict(message.to_dict()) == message
