"""Work the claude CLI does between turns must not wedge or desynchronise
the next turn.

``DelegatedLoop`` reads the CLI's stdout only during its own turns. A turn the
CLI starts on its own (a wake, e.g. after a background job finished) is drained
at the next turn's start by ``_discard_stale_stdout``: a permission request it
makes is denied instead of dropped (the CLI blocks on every one), and a wake
still waiting on its model is read until its result before the user's frame
goes in, so it cannot answer in the user's place.

Hermetic: a small fake ``claude`` written to tmp_path, driven through the real
HarnessAdapter + DelegatedLoop.
"""

from __future__ import annotations

import asyncio
import sys
import textwrap
from pathlib import Path

import pytest
from fluid.tools.registry import ToolRegistry

import app.core.provider.harness_adapter as ha
from app.core.provider.events import AssistantMessage, ResultMessage, TextBlock
from app.core.provider.types import SessionOptions

TURN_BUDGET_SECONDS = 15.0

FAKE_CLI = textwrap.dedent(
    r'''
    import json, os, sys, time

    MODE = os.environ["WAKE_MODE"]  # "perm" or "latency"
    SID = "cli-wake-1"
    pending_users = []

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def usage():
        return {"input_tokens": 1, "output_tokens": 1,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    def assistant(content, stop):
        return {"type": "assistant", "session_id": SID, "message": {
            "id": "msg_%d" % time.time_ns(), "model": "claude-fake",
            "content": content, "usage": usage(), "stop_reason": stop}}

    def result():
        return {"type": "result", "subtype": "success", "is_error": False,
                "num_turns": 1, "session_id": SID, "total_cost_usd": 0.0,
                "usage": usage()}

    def read():
        line = sys.stdin.readline()
        if not line:
            sys.exit(0)
        line = line.strip()
        return json.loads(line) if line else {}

    def next_frame(want):
        """Read stdin until a frame of type ``want``; answer control
        requests on the way and queue user frames (the real CLI queues a
        user message that arrives while it is busy)."""
        while True:
            if want == "user" and pending_users:
                return pending_users.pop(0)
            msg = read()
            kind = msg.get("type")
            if kind == "control_request":
                emit({"type": "control_response", "response": {
                    "subtype": "success", "request_id": msg.get("request_id"),
                    "response": {}}})
                continue
            if kind == "user" and want != "user":
                pending_users.append(msg)
                continue
            if kind == want:
                return msg

    def prompt_text(msg):
        content = msg.get("message", {}).get("content")
        if isinstance(content, str):
            return content
        return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict))

    if "--version" in sys.argv:
        print("claude 9.9.9 (fake wake)")
        sys.exit(0)

    first = True
    while True:
        msg = next_frame("user")
        if first:
            emit({"type": "system", "subtype": "init", "session_id": SID,
                  "model": "claude-fake", "tools": ["Bash", "Read"], "mcp_servers": []})
        emit(assistant([{"type": "text", "text": "ANSWER: " + prompt_text(msg)}], "end_turn"))
        emit(result())
        if not first:
            continue
        first = False
        # The background job the first turn started has finished: the CLI
        # runs a turn of its own to report it, with no user frame.
        if MODE == "latency":
            time.sleep(0.5)  # the wake turn's model call
            emit(assistant([{"type": "text", "text": "WAKE: background job finished"}], "end_turn"))
            emit(result())
        else:
            emit(assistant([{"type": "tool_use", "id": "toolu_wake", "name": "Bash",
                             "input": {"command": "cat build.log"}}], "tool_use"))
            emit({"type": "control_request", "request_id": "cli_wake_perm", "request": {
                "subtype": "can_use_tool", "tool_name": "Bash",
                "input": {"command": "cat build.log"}, "tool_use_id": "toolu_wake"}})
            next_frame("control_response")  # blocks until someone answers
            emit({"type": "user", "session_id": SID, "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_wake",
                 "content": [{"type": "text", "text": "ok"}], "is_error": False}]}})
            emit(assistant([{"type": "text", "text": "WAKE: build passed"}], "end_turn"))
            emit(result())
    '''
)


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    from app.core import guidance_surface

    monkeypatch.setenv(guidance_surface.GUIDANCE_HOME_ENV, str(home))


async def _turn_text(adapter, prompt: str) -> str:
    await adapter.send_query(prompt)
    texts = []
    async for message in adapter.receive_events():
        if isinstance(message, AssistantMessage):
            texts.extend(
                block.text for block in message.content or [] if isinstance(block, TextBlock)
            )
        if isinstance(message, ResultMessage):
            return "".join(texts)
    raise AssertionError("turn ended without a result")


async def _adapter(monkeypatch, tmp_path, mode: str):
    script = tmp_path / "fake_wake_claude.py"
    script.write_text(FAKE_CLI)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(script)])
    options = SessionOptions(
        model="fable",
        system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND"},
        cwd=str(tmp_path),
        permission_mode="default",
        env={"WAKE_MODE": mode},
        extra={"fluid_registry": ToolRegistry()},
    )
    adapter = ha.HarnessAdapter()
    await adapter.connect(options)
    return adapter


@pytest.mark.asyncio
async def test_a_wake_turn_permission_prompt_does_not_wedge_the_next_turn(monkeypatch, tmp_path):
    adapter = await _adapter(monkeypatch, tmp_path, "perm")
    try:
        assert "ANSWER: first" in await _turn_text(adapter, "first")
        try:
            await asyncio.wait_for(_turn_text(adapter, "second"), TURN_BUDGET_SECONDS)
        except asyncio.TimeoutError:
            pytest.fail(
                "the next turn hung: the CLI's between-turn can_use_tool request "
                "was discarded as a stale frame and never answered"
            )
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_a_slow_wake_turn_does_not_steal_the_next_turns_reply(monkeypatch, tmp_path):
    adapter = await _adapter(monkeypatch, tmp_path, "latency")
    try:
        assert "ANSWER: first" in await _turn_text(adapter, "first")
        second = await asyncio.wait_for(_turn_text(adapter, "second"), TURN_BUDGET_SECONDS)
        assert "ANSWER: second" in second, (
            f"turn 2 was answered with the CLI's own wake turn ({second!r}); the "
            "real reply to 'second' arrives after the turn ended and is discarded"
        )
    finally:
        await adapter.disconnect()


def test_the_clis_own_background_tools_are_banned_on_this_leg(tmp_path):
    """Their completions arrive as between-turn wakes; the app's bridged
    copies (mcp__builtins__Monitor/TaskOutput/TaskStop) are the ones to use."""
    blocked = ha._delegated_disallowed(SessionOptions(model="fable", cwd=str(tmp_path)))
    assert {"Monitor", "TaskOutput", "TaskStop", "ScheduleWakeup"} <= set(blocked)
    assert not any(name.startswith("mcp__") for name in blocked)
