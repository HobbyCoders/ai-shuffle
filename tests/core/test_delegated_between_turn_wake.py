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
from app.core.provider.events import AssistantMessage, ResultMessage, SystemMessage, TextBlock
from app.core.provider.types import SessionOptions

TURN_BUDGET_SECONDS = 15.0

FAKE_CLI = textwrap.dedent(
    r'''
    import json, os, sys, time

    # "perm", "latency", "notify", "plain", or a compaction: "compact" (the
    # main conversation's, between turns), "subcompact" (a subagent's, between
    # turns) or "midturn" (the main one's and a subagent's inside turn 1).
    MODE = os.environ["WAKE_MODE"]
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

    def compaction(main, pre):
        """The frames 2.1.289 writes for a compaction: status frames only
        around the main conversation's."""
        if main:
            emit({"type": "system", "subtype": "status", "status": "compacting",
                  "session_id": SID})
            emit({"type": "system", "subtype": "status", "status": None,
                  "compact_result": "success", "session_id": SID})
        emit({"type": "system", "subtype": "compact_boundary", "session_id": SID,
              "compact_metadata": {"trigger": "auto", "pre_tokens": pre,
                                   "post_tokens": 30000}})

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
        if first and MODE == "midturn":
            compaction(False, 111000)
            compaction(True, 150000)
        emit(result())
        if not first:
            continue
        first = False
        if MODE in ("compact", "subcompact"):
            # An idle compaction, written after the turn's result.
            compaction(MODE == "compact", 150000)
            continue
        if MODE == "midturn":
            continue
        # The background job the first turn started has finished: the CLI
        # runs a turn of its own to report it, with no user frame.
        if MODE == "plain":
            continue
        if MODE == "notify":
            # A notification with no wake turn behind it.
            emit({"type": "system", "subtype": "task_notification", "session_id": SID,
                  "task_id": "job-1", "status": "completed"})
            continue
        if MODE == "latency":
            # The wake turn's first frame lands inside the drain's settle window
            # (fluid.engine.delegated._WAKE_SETTLE) after the previous result.
            time.sleep(0.1)
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


async def _timed_drains(monkeypatch, adapter) -> list[float]:
    """Wrap the engine's between-turn drain and record how long each one took."""
    loop = adapter._engine
    original = loop._discard_stale_stdout
    durations: list[float] = []

    async def timed():
        started = asyncio.get_running_loop().time()
        try:
            await original()
        finally:
            durations.append(asyncio.get_running_loop().time() - started)

    monkeypatch.setattr(loop, "_discard_stale_stdout", timed)
    return durations


@pytest.mark.asyncio
async def test_a_plain_next_turn_is_not_held_by_the_drain(monkeypatch, tmp_path):
    adapter = await _adapter(monkeypatch, tmp_path, "plain")
    try:
        assert "ANSWER: first" in await _turn_text(adapter, "first")
        durations = await _timed_drains(monkeypatch, adapter)
        # Sent the moment the previous result arrived: the drain may watch for
        # a wake briefly, but must not add more than ~300 ms to the turn.
        assert "ANSWER: second" in await asyncio.wait_for(
            _turn_text(adapter, "second"), TURN_BUDGET_SECONDS,
        )
        assert durations and max(durations) < 0.3, durations
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_a_lone_task_notification_does_not_arm_the_wake_wait(monkeypatch, tmp_path):
    adapter = await _adapter(monkeypatch, tmp_path, "notify")
    try:
        assert "ANSWER: first" in await _turn_text(adapter, "first")
        durations = await _timed_drains(monkeypatch, adapter)
        assert "ANSWER: second" in await asyncio.wait_for(
            _turn_text(adapter, "second"), TURN_BUDGET_SECONDS,
        )
        assert durations and max(durations) < 1.0, durations
    finally:
        await adapter.disconnect()


async def _turn_messages(adapter, prompt: str) -> list:
    await adapter.send_query(prompt)
    messages = []
    async for message in adapter.receive_events():
        messages.append(message)
        if isinstance(message, ResultMessage):
            return messages
    raise AssertionError("turn ended without a result")


def _compaction_cards(messages) -> list:
    return [
        m.data for m in messages
        if isinstance(m, SystemMessage) and m.subtype == "compact_boundary"
    ]


# The fake CLI answers /context with an empty breakdown, so no overhead is
# known and the "after" count is labelled as the history only (#1907).
_CARD = {"compact_metadata": {"trigger": "auto", "pre_tokens": 150000, "post_tokens": 30000,
                              "post_tokens_basis": "history"}}


async def _expected_card(adapter) -> dict:
    """The card's identity, time and optional anchor from its persisted event.

    The recorder derives its row id from the identity (replay cannot add a
    second row); exports place the card after the anchored node's full group,
    with timestamps as the legacy fallback. These are not display metadata.
    """
    from fluid.events import ContextCompaction

    loop = adapter._engine
    [boundary] = [e for e in await loop.store.events_since(loop.session_id, -1)
                  if isinstance(e, ContextCompaction)]
    card = {**_CARD, "compaction_event_id": f"{boundary.session_id}:{boundary.seq}",
            "compaction_event_ts": boundary.ts}
    if boundary.node_id:
        card["compaction_after_node_id"] = boundary.node_id
    return card


@pytest.mark.asyncio
async def test_a_mid_turn_compaction_reaches_the_chat_as_its_card(monkeypatch, tmp_path):
    """Only the main conversation's: the subagent's boundary in the same turn
    gives no card in the parent chat."""
    adapter = await _adapter(monkeypatch, tmp_path, "midturn")
    try:
        cards = _compaction_cards(await _turn_messages(adapter, "first"))
        expected = await _expected_card(adapter)
        assert expected["compaction_after_node_id"]
        assert cards == [expected]
        assert _compaction_cards(await _turn_messages(adapter, "second")) == []
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_a_compaction_between_turns_reaches_the_chat_with_the_next_turn(
    monkeypatch, tmp_path,
):
    adapter = await _adapter(monkeypatch, tmp_path, "compact")
    try:
        assert _compaction_cards(await _turn_messages(adapter, "first")) == []
        await asyncio.sleep(0.2)  # the CLI compacts after the first result
        second = await asyncio.wait_for(
            _turn_messages(adapter, "second"), TURN_BUDGET_SECONDS,
        )
        assert _compaction_cards(second) == [await _expected_card(adapter)]
        texts = [
            block.text for m in second if isinstance(m, AssistantMessage)
            for block in m.content or [] if isinstance(block, TextBlock)
        ]
        assert "ANSWER: second" in "".join(texts)
        # Shown once: the third turn does not replay it.
        assert _compaction_cards(await _turn_messages(adapter, "third")) == []
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_a_subagent_compaction_between_turns_gives_no_card(monkeypatch, tmp_path):
    adapter = await _adapter(monkeypatch, tmp_path, "subcompact")
    try:
        await _turn_messages(adapter, "first")
        await asyncio.sleep(0.2)
        assert _compaction_cards(await asyncio.wait_for(
            _turn_messages(adapter, "second"), TURN_BUDGET_SECONDS,
        )) == []
    finally:
        await adapter.disconnect()


def test_the_clis_own_background_tools_are_banned_on_this_leg(tmp_path):
    """Their completions arrive as between-turn wakes; the app's bridged
    copies (mcp__builtins__Monitor/TaskOutput/TaskStop) are the ones to use."""
    blocked = ha._delegated_disallowed(SessionOptions(model="fable", cwd=str(tmp_path)))
    assert {"Monitor", "TaskOutput", "TaskStop", "ScheduleWakeup"} <= set(blocked)
    assert not any(name.startswith("mcp__") for name in blocked)
