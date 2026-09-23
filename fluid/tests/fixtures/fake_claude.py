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
  e  stale wake burst:      like (a), but after the first turn's result it emits
                            an unsolicited assistant+result burst (no prompt)
  f  control replies:       like (a), and answers a ``get_context_usage``
                            control_request with a success control_response
  g  control error:         like (f), but answers with the error subtype
  h  control silence:       like (a), and never answers a control_request
                            (drives the caller's timeout path)
  i  deferred control:      like (a), but a ``get_context_usage`` request is
                            parked unanswered until the *next* user turn, whose
                            frames it then interleaves with:
                            assistant → control_response → result. Forces the
                            reply to arrive through the turn's frame reader
                            while a between-turns pump is still waiting on it.
  j  in-turn context usage: like (a), but the turn PAUSES after its assistant
                            frame until a control_request arrives and answers
                            it before the result frame — what a real CLI does
                            when the loop asks for a per-call context snapshot.
  k  oversize frame:        a tool turn whose tool_result user frame is far
                            over asyncio's default 64 KiB line limit.
  l  dead resume, silent:   with ``--resume`` the process exits at once with
                            no frames (what the real CLI does for an unknown
                            id); without it, behaves like (a).
  m  dead resume, error:    with ``--resume`` the first turn answers with an
                            ``error_during_execution`` result and no other
                            frame; without it, behaves like (a).
  n  failed turn:           an assistant frame, then a result with
                            ``is_error`` and an ``errors`` list.
  o  turn cap:              like (a) but the result is ``error_max_turns``
                            with ``is_error`` set.
  s  long partial stream:   stream_event text deltas (count from
                            FAKE_CLAUDE_DELTAS, "first,second", default 40,8),
                            the full assistant frame, a CLI-owned tool round
                            (tool_use + tool_result, no permission prompt),
                            then a second streamed message and the result.
                            FAKE_CLAUDE_STREAM_MODE=die exits abruptly after
                            the first block's deltas; =interrupt parks there
                            until an ``interrupt`` control_request, then closes
                            the block and returns a success result.

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


def result_frame(sid: str, subtype: str = "success", **extra) -> dict:
    frame = {
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
    frame.update(extra)
    return frame


def stream_event(sid: str, event: dict) -> dict:
    return {"type": "stream_event", "uuid": "u", "session_id": sid, "event": event}


def turn_simple(sid: str) -> None:
    emit(assistant_frame(sid, [{"type": "text", "text": "Hello from fake."}], "end_turn"))
    emit(result_frame(sid))


def turn_compacting(sid: str) -> None:
    """A turn the CLI compacts its own context in the middle of."""
    emit(assistant_frame(sid, [{"type": "text", "text": "Reading everything."}], "end_turn"))
    emit({
        "type": "system", "subtype": "compact_boundary", "session_id": sid,
        "uuid": "uuid-compact-1",
        "compact_metadata": {
            "trigger": "auto", "pre_tokens": 166397, "post_tokens": 34856,
        },
    })
    emit(assistant_frame(sid, [{"type": "text", "text": "Carrying on."}], "end_turn"))
    emit(result_frame(sid))


def turn_tool(sid: str, *, question: bool = False) -> None:
    tid = "toolu_fake_1"
    tool_name = "AskUserQuestion" if question else "mcp__fluid_bundled__get_weather"
    args = {"questions": [{"question": "Which region?"}]} if question else {"city": "Paris"}
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
            "permission_suggestions": [{
                "type": "addRules", "behavior": "allow", "destination": "session",
                "rules": [{"toolName": tool_name}],
            }],
        },
    })
    resp = wait_for("control_response")
    behavior = None
    if resp is not None:
        behavior = resp.get("response", {}).get("response", {}).get("behavior")
    if question:
        response = resp.get("response", {}).get("response", {}) if resp else {}
        assert response.get("behavior") == "deny"
        queued = json.loads(response["message"])
        assert queued["status"] == "queued" and queued["request_ids"]
        emit({"type": "user", "session_id": sid, "message": {"role": "user", "content": [{
            "type": "tool_result", "tool_use_id": tid,
            "content": [{"type": "text", "text": response["message"]}], "is_error": True,
        }]}})
        emit(assistant_frame(sid, [{"type": "text", "text": "Independent work completed."}], "end_turn"))
    elif behavior == "allow":
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


# What scenario (f) reports for get_context_usage — the SDK's
# ContextUsageResponse shape, nested under the success frame's inner "response".
CONTEXT_USAGE_PAYLOAD = {
    "categories": [
        {"name": "System prompt", "tokens": 2300, "color": "#6C5CE7"},
        {"name": "Tools", "tokens": 9100, "color": "#FDCB6E"},
        {"name": "Messages", "tokens": 600, "color": "#E17055"},
        {"name": "Free space", "tokens": 188000, "color": "#2D3436"},
    ],
    "totalTokens": 12000,
    "maxTokens": 200000,
    "rawMaxTokens": 200000,
    "percentage": 6.0,
    "model": "claude-fake",
    "isAutoCompactEnabled": True,
}


def context_usage_response(request_id: str) -> dict:
    return {"type": "control_response", "response": {
        "subtype": "success", "request_id": request_id,
        "response": CONTEXT_USAGE_PAYLOAD,
    }}


def answer_control(msg: dict, scenario: str) -> str | None:
    """Reply to a control_request the harness sent (scenarios f/g; h stays mute).

    Returns a request_id when the reply is deliberately deferred (scenario i),
    for the caller to emit inside the next turn.
    """
    if scenario == "h":
        return None
    request_id = msg.get("request_id", "")
    subtype = (msg.get("request") or {}).get("subtype")
    if subtype != "get_context_usage":
        return None
    if scenario == "i":
        return request_id  # parked — the next turn carries it
    if scenario == "g":
        emit({"type": "control_response", "response": {
            "subtype": "error", "request_id": request_id,
            "error": "context usage unavailable",
        }})
        return None
    emit(context_usage_response(request_id))
    return None


def turn_with_deferred_control(sid: str, request_id: str | None) -> None:
    """A simple turn that carries a parked control reply in its frame stream.

    Ordering matters: the assistant frame goes out *before* the reply, so a
    reader that is still pumping for the reply would have to discard a real turn
    frame to reach it.
    """
    emit(assistant_frame(sid, [{"type": "text", "text": "Hello from fake."}], "end_turn"))
    if request_id is not None:
        emit(context_usage_response(request_id))
    emit(result_frame(sid))


def turn_inline_context(sid: str) -> None:
    """A turn that answers the loop's per-call context query mid-stream.

    The real CLI services control_requests while it streams; this fake is a
    single stdin reader, so it parks after the assistant frame and waits for
    the request the loop sends on seeing that frame's usage. EOF (no request
    ever came) just ends the turn.
    """
    emit(assistant_frame(sid, [{"type": "text", "text": "Hello from fake."}], "end_turn"))
    msg = wait_for("control_request")
    if msg is not None:
        emit(context_usage_response(msg.get("request_id", "")))
    emit(result_frame(sid))


def turn_oversize(sid: str) -> None:
    """A tool result the CLI echoes whole: 300 KB in one user frame."""
    tid = "toolu_fake_big"
    emit(assistant_frame(
        sid, [{"type": "tool_use", "id": tid, "name": "Read", "input": {"file_path": "big.txt"}}],
        "tool_use",
    ))
    emit({"type": "user", "session_id": sid, "message": {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": tid,
        "content": [{"type": "text", "text": "x" * 300_000}], "is_error": False,
    }]}})
    emit(assistant_frame(sid, [{"type": "text", "text": "Read it all."}], "end_turn"))
    emit(result_frame(sid))


def turn_failed(sid: str) -> None:
    emit(assistant_frame(sid, [{"type": "text", "text": "Trying..."}], "end_turn"))
    emit(result_frame(
        sid, "error_during_execution", errors=["tool crashed", "stream reset"],
    ))


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


def _stream_text_block(sid: str, index: int, count: int, prefix: str) -> str:
    emit(stream_event(sid, {"type": "content_block_start", "index": index,
                            "content_block": {"type": "text"}}))
    parts = []
    for i in range(count):
        text = f"{prefix}{i} "
        parts.append(text)
        emit(stream_event(sid, {"type": "content_block_delta", "index": index,
                                "delta": {"type": "text_delta", "text": text}}))
    return "".join(parts)


def turn_long_stream(sid: str) -> None:
    counts = [int(n) for n in os.environ.get("FAKE_CLAUDE_DELTAS", "40,8").split(",")]
    first, second = counts[0], counts[1] if len(counts) > 1 else 0
    mode = os.environ.get("FAKE_CLAUDE_STREAM_MODE", "")
    emit(stream_event(sid, {"type": "message_start", "message": {"usage": {"input_tokens": 9}}}))
    text = _stream_text_block(sid, 0, first, "a")
    if mode == "die":
        os._exit(3)  # no block stop, no result: the stream just ends
    if mode == "interrupt":
        while True:
            msg = read_line()
            if msg is None:
                return
            if (msg.get("type") == "control_request"
                    and (msg.get("request") or {}).get("subtype") == "interrupt"):
                break
    emit(stream_event(sid, {"type": "content_block_stop", "index": 0}))
    emit(assistant_frame(sid, [{"type": "text", "text": text}], "end_turn"))
    if mode == "interrupt":
        emit(result_frame(sid))
        return
    tid = "toolu_stream_1"
    emit(assistant_frame(
        sid, [{"type": "tool_use", "id": tid, "name": "Read", "input": {"file_path": "notes.txt"}}],
        "tool_use",
    ))
    emit({"type": "user", "session_id": sid, "message": {"role": "user", "content": [{
        "type": "tool_result", "tool_use_id": tid,
        "content": [{"type": "text", "text": "notes: ok"}], "is_error": False,
    }]}})
    emit(stream_event(sid, {"type": "message_start", "message": {"usage": {"input_tokens": 11}}}))
    tail = _stream_text_block(sid, 0, second, "b")
    emit(stream_event(sid, {"type": "content_block_stop", "index": 0}))
    emit(assistant_frame(sid, [{"type": "text", "text": tail}], "end_turn"))
    emit(result_frame(sid))


def main() -> int:
    if "--version" in sys.argv:
        print("claude 2.0.0 (fake-fluid)")
        return 0

    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "a")
    resume = None
    if "--resume" in sys.argv:
        i = sys.argv.index("--resume")
        resume = sys.argv[i + 1] if i + 1 < len(sys.argv) else None

    if scenario == "c":
        expect = os.environ.get("FAKE_CLAUDE_EXPECT_RESUME")
        assert resume == expect, f"--resume mismatch: got {resume!r}, expected {expect!r}"
    if scenario == "l" and resume is not None:
        print(f"No conversation found with session ID: {resume}", file=sys.stderr)
        return 1

    session_id = resume or "cli-sess-123"
    emit({"type": "system", "subtype": "init", "session_id": session_id,
          "model": "claude-fake", "tools": ["Read", "Bash"],
          "mcp_servers": [{"name": "fluid", "status": "connected"}]})

    wake_emitted = False
    deferred_control: str | None = None
    while True:
        msg = read_line()
        if msg is None:
            return 0
        if msg.get("type") == "control_request":
            deferred_control = answer_control(msg, scenario) or deferred_control
            continue
        if msg.get("type") != "user":
            continue
        if scenario == "i":
            turn_with_deferred_control(session_id, deferred_control)
            deferred_control = None
        elif scenario == "k":
            turn_oversize(session_id)
        elif scenario == "m" and resume is not None:
            emit(result_frame(
                session_id, "error_during_execution",
                result=f"No conversation found with session ID: {resume}",
            ))
        elif scenario == "n":
            turn_failed(session_id)
        elif scenario == "o":
            emit(assistant_frame(session_id, [{"type": "text", "text": "Still going."}], "end_turn"))
            emit(result_frame(session_id, "error_max_turns"))
        elif scenario == "p":
            turn_compacting(session_id)
        elif scenario == "j":
            turn_inline_context(session_id)
        elif scenario == "q":
            turn_tool(session_id, question=True)
            scenario = "a"
        elif scenario == "b":
            turn_tool(session_id)
        elif scenario == "d":
            turn_partial(session_id)
        elif scenario == "s":
            turn_long_stream(session_id)
        elif scenario == "e":
            turn_simple(session_id)
            if not wake_emitted:
                wake_emitted = True
                emit(assistant_frame(session_id, [{"type": "text", "text": "WAKE: unsolicited"}], "end_turn"))
                emit(result_frame(session_id))
        else:  # a and c share the simple turn
            turn_simple(session_id)


if __name__ == "__main__":
    sys.exit(main())
