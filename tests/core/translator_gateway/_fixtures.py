"""Synthetic Responses SSE streams built from the exact event inventory and item
shapes in docs/CODEX_SPIKE_FINDINGS.md. Deterministic, no network.

Each builder returns a ``list[(event_name, payload)]`` matching what a parsed
Responses SSE stream would hand the translator.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple

Event = Tuple[str, Dict[str, Any]]


def split_into(s: str, n: int) -> List[str]:
    """Split ``s`` into exactly ``n`` fragments that concatenate byte-for-byte
    back to ``s`` (last fragment absorbs the remainder). Empty fragments are
    avoided when possible so tests genuinely exercise many deltas."""
    if n <= 1 or len(s) <= 1:
        return [s]
    n = min(n, len(s))
    size = len(s) // n
    frags = [s[i * size : (i + 1) * size] for i in range(n - 1)]
    frags.append(s[(n - 1) * size :])
    assert "".join(frags) == s
    return frags


def _usage(
    input_tokens: int = 99,
    output_tokens: int = 52,
    reasoning_tokens: int = 45,
    cached_tokens: int = 0,
    cache_write_tokens: int = 0,
    total_tokens: int | None = None,
) -> Dict[str, Any]:
    if total_tokens is None:
        total_tokens = input_tokens + output_tokens
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {
            "cache_write_tokens": cache_write_tokens,
            "cached_tokens": cached_tokens,
        },
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "total_tokens": total_tokens,
    }


def plain_text_stream(
    text: str = "PONG",
    response_id: str = "resp_text",
    model: str = "gpt-5.6-sol",
    n_deltas: int = 3,
) -> List[Event]:
    frags = split_into(text, n_deltas)
    events: List[Event] = [
        ("response.created", {"response": {"id": response_id, "model": model, "status": "in_progress", "output": []}}),
        ("response.in_progress", {"response": {"id": response_id, "model": model, "status": "in_progress", "output": []}}),
        ("response.output_item.added", {"output_index": 0, "item": {"id": "msg_1", "type": "message", "role": "assistant", "content": []}}),
        ("response.content_part.added", {"item_id": "msg_1", "part": {"type": "output_text", "text": ""}}),
    ]
    for frag in frags:
        events.append(("response.output_text.delta", {"item_id": "msg_1", "delta": frag, "obfuscation": "PADPADPAD"}))
    events += [
        ("response.output_text.done", {"item_id": "msg_1", "text": text}),
        ("response.content_part.done", {"item_id": "msg_1", "part": {"type": "output_text", "text": text}}),
        ("response.output_item.done", {"item": {"id": "msg_1", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": text}]}}),
        ("response.completed", {"response": {"id": response_id, "model": model, "status": "completed", "output": [], "usage": _usage(input_tokens=10, output_tokens=len(text), reasoning_tokens=0, total_tokens=10 + len(text))}}),
    ]
    return events


def tool_call_stream(
    arguments: str = '{"city":"Paris","unit":"celsius","include_forecast":true}',
    call_id: str = "call_abc123",
    name: str = "get_weather",
    response_id: str = "resp_tool",
    model: str = "gpt-5.6-sol",
    n_deltas: int = 16,
) -> List[Event]:
    """A streaming function-call turn. ``n_deltas`` argument fragments each carry
    an ``obfuscation`` padding field that MUST be ignored (surprise #10)."""
    frags = split_into(arguments, n_deltas)
    events: List[Event] = [
        ("response.created", {"response": {"id": response_id, "model": model, "status": "in_progress", "output": []}}),
        ("response.output_item.added", {"output_index": 0, "item": {"id": "fc_1", "type": "function_call", "call_id": call_id, "name": name, "arguments": ""}}),
    ]
    for i, frag in enumerate(frags):
        events.append(("response.function_call_arguments.delta", {"item_id": "fc_1", "delta": frag, "obfuscation": f"PAD{i:04d}"}))
    events += [
        ("response.function_call_arguments.done", {"item_id": "fc_1", "arguments": arguments}),
        ("response.output_item.done", {"item": {"id": "fc_1", "type": "function_call", "status": "completed", "call_id": call_id, "name": name, "arguments": arguments}}),
        ("response.completed", {"response": {"id": response_id, "model": model, "status": "completed", "output": [], "usage": _usage(input_tokens=194, output_tokens=29, reasoning_tokens=0, total_tokens=223)}}),
    ]
    return events


def reasoning_stream(
    encrypted_content: str = "ENCRYPTED_BLOB_" + ("x" * 64),
    summary_text: str = "**Solving the price equation step by step**",
    answer: str = "75",
    response_id: str = "resp_reason",
    model: str = "gpt-5.6-sol",
) -> List[Event]:
    """A reasoning turn: one reasoning item (with encrypted_content) followed by a
    message. Mirrors the E4 shape."""
    summary = [{"type": "summary_text", "text": summary_text}]
    return [
        ("response.created", {"response": {"id": response_id, "model": model, "status": "in_progress", "output": []}}),
        ("response.output_item.added", {"output_index": 0, "item": {"id": "rs_1", "type": "reasoning", "content": [], "summary": []}}),
        ("response.reasoning_summary_part.added", {"item_id": "rs_1", "part": {"type": "summary_text", "text": ""}}),
        ("response.reasoning_summary_text.delta", {"item_id": "rs_1", "delta": summary_text, "obfuscation": "PAD"}),
        ("response.reasoning_summary_text.done", {"item_id": "rs_1", "text": summary_text}),
        ("response.reasoning_summary_part.done", {"item_id": "rs_1", "part": {"type": "summary_text", "text": summary_text}}),
        ("response.output_item.done", {"item": {"id": "rs_1", "type": "reasoning", "content": [], "encrypted_content": encrypted_content, "summary": summary}}),
        ("response.output_item.added", {"output_index": 1, "item": {"id": "msg_1", "type": "message", "role": "assistant", "content": []}}),
        ("response.output_text.delta", {"item_id": "msg_1", "delta": answer, "obfuscation": "PAD"}),
        ("response.output_text.done", {"item_id": "msg_1", "text": answer}),
        ("response.output_item.done", {"item": {"id": "msg_1", "type": "message", "status": "completed", "role": "assistant", "content": [{"type": "output_text", "text": answer}]}}),
        ("response.completed", {"response": {"id": response_id, "model": model, "status": "completed", "output": [], "usage": _usage(input_tokens=99, output_tokens=52, reasoning_tokens=45, total_tokens=151)}}),
    ]
