#!/usr/bin/env python3
"""A fake ``claude`` CLI honoring the stream-json contract, for offline tests.

No real ``claude`` binary exists on the test host, so ``DelegatedLoop`` is driven
against this script (invoke as ``[sys.executable, fake_claude.py]`` or directly
via its shebang). It reads JSON lines on stdin and replays a scripted frame
sequence on stdout, selected by ``FAKE_CLAUDE_SCENARIO``:

  a  simple text turn:      init → assistant(text) → result
  b  tool-use turn:         init → assistant(tool_use) → control_request
                            (can_use_tool) → [wait control_response] →
                            user(tool_result) → assistant(text) → result
  c  resume:                asserts ``--resume`` matches FAKE_CLAUDE_EXPECT_RESUME,
                            then behaves like (a) echoing the resumed session id
  d  partial streaming:     init → stream_event(deltas) → assistant(full text)
                            → result  (drives the dedupe path)

``claude --version`` prints a version line and exits (the loop's version guard).
Frames model the real vocabulary (``claude_agent_sdk`` message_parser + query.py).
"""

from __future__ import annotations

import json
import os
import sys


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def read_line() -> dict | None:
    line = sys.stdin.readline()
    if not line:
        return None
    line = line.strip()
    if not line:
        return {}
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {}


def wait_for(msg_type: str) -> dict | None:
    """Block reading stdin until a frame of ``msg_type`` arrives (or EOF)."""
    while True:
        msg = read_line()
        if msg is None:
            return None
        if msg.get("type") == msg_type:
            return msg


def assistant_frame(sid: str, content: list[dict], stop_reason: str, usage: dict | None = None) -> dict:
    return {
        "type": "assistant",
        "session_id": sid,
        "message": {
            "id": "msg_fake",
            "model": "claude-fake",
            "content": content,
            "usage": usage or {
                "input_tokens": 12, "output_tokens": 5,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
            },
            "stop_reason": stop_reason,
        },
    }


def result_frame(sid: str, subtype: str = "success") -> dict:
    return {
        "type": "result",
        "subtype": subtype,
        "duration_ms": 12,
        "duration_api_ms": 9,
        "is_error": subtype != "success",
        "num_turns": 1,
        "session_id": sid,
        "total_cost_usd": 0.00042,
        "usage": {
            "input_tokens": 12, "output_tokens": 5,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        },
    }


def stream_event(sid: str, event: dict) -> dict:
    return {"type": "stream_event", "uuid": "u", "session_id": sid, "event": event}


def turn_simple(sid: str) -> None:
    emit(assistant_frame(sid, [{"type": "text", "text": "Hello from fake."}], "end_turn"))
    emit(result_frame(sid))


def turn_tool(sid: str) -> None:
    tid = "toolu_fake_1"
    tool_name = "mcp__omni_bundled__get_weather"
    args = {"city": "Paris"}
    emit(assistant_frame(
        sid, [{"type": "tool_use", "id": tid, "name": tool_name, "input": args}], "tool_use",
    ))
    emit({
        "type": "control_request",
        "request_id": "cli_req_1",
        "request": {
            "subtype": "can_use_tool",
            "tool_name": tool_name,
            "input": args,
            "tool_use_id": tid,
            "permission_suggestions": [],
        },
    })
    resp = wait_for("control_response")
    behavior = None
    if resp is not None:
        behavior = resp.get("response", {}).get("response", {}).get("behavior")
    if behavior == "allow":
        emit({"type": "user", "session_id": sid, "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": tid,
            "content": [{"type": "text", "text": "Paris: sunny, 21C"}], "is_error": False,
        }]}})
        emit(assistant_frame(sid, [{"type": "text", "text": "It is sunny in Paris."}], "end_turn"))
    else:
        emit({"type": "user", "session_id": sid, "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": tid,
            "content": [{"type": "text", "text": "permission denied by user"}], "is_error": True,
        }]}})
        emit(assistant_frame(sid, [{"type": "text", "text": "Understood, skipping that."}], "end_turn"))
    emit(result_frame(sid))


def turn_partial(sid: str) -> None:
    emit(stream_event(sid, {"type": "message_start", "message": {"usage": {"input_tokens": 9}}}))
    emit(stream_event(sid, {"type": "content_block_start", "index": 0, "content_block": {"type": "text"}}))
    emit(stream_event(sid, {"type": "content_block_delta", "index": 0,
                            "delta": {"type": "text_delta", "text": "Hello "}}))
    emit(stream_event(sid, {"type": "content_block_delta", "index": 0,
                            "delta": {"type": "text_delta", "text": "world."}}))
    emit(stream_event(sid, {"type": "content_block_stop", "index": 0}))
    emit(stream_event(sid, {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                            "usage": {"output_tokens": 3}}))
    # Final full assistant frame — its whole-block text must be deduped away.
    emit(assistant_frame(sid, [{"type": "text", "text": "Hello world."}], "end_turn"))
    emit(result_frame(sid))


def main() -> int:
    if "--version" in sys.argv:
        print("claude 2.0.0 (fake-omni)")
        return 0

    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "a")
    resume = None
    if "--resume" in sys.argv:
        i = sys.argv.index("--resume")
        resume = sys.argv[i + 1] if i + 1 < len(sys.argv) else None

    if scenario == "c":
        expect = os.environ.get("FAKE_CLAUDE_EXPECT_RESUME")
        assert resume == expect, f"--resume mismatch: got {resume!r}, expected {expect!r}"

    session_id = resume or "cli-sess-123"
    emit({"type": "system", "subtype": "init", "session_id": session_id,
          "model": "claude-fake", "tools": [], "mcp_servers": []})

    while True:
        msg = read_line()
        if msg is None:
            return 0
        if msg.get("type") != "user":
            continue
        if scenario == "b":
            turn_tool(session_id)
        elif scenario == "d":
            turn_partial(session_id)
        else:  # a and c share the simple turn
            turn_simple(session_id)


if __name__ == "__main__":
    sys.exit(main())
