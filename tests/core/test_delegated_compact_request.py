"""A ``/compact`` in a delegated Claude session (#1820, #1794).

Driven through the real HarnessAdapter + DelegatedLoop against a small fake
``claude`` written to tmp_path, so what the CLI receives on stdin and what the
chat gets back are both checked:

* the command reaches the CLI exactly as typed; the turn note (memory, date,
  branch) is not appended, because the CLI takes everything after the
  command name as the summarizer's custom instructions;
* the "Context Compacted" card appears only for the CLI's boundary, and a
  ``/compact`` the app sent (the idle watchdog's wake) is marked as such;
* a ``/compact`` the CLI refuses shows as a "compact_failed" card with the
  CLI's reason, and no compacted card.

Hermetic: no network, no real CLI.
"""

from __future__ import annotations

import json
import sys
import textwrap
from pathlib import Path

import pytest
from fluid.tools.registry import ToolRegistry

import app.core.provider.harness_adapter as ha
from app.core.provider.events import ResultMessage, SystemMessage
from app.core.provider.types import SessionOptions

FAKE_CLI = textwrap.dedent(
    r'''
    import json, os, sys, time

    MODE = os.environ["COMPACT_MODE"]  # "ok" or "refuse"
    RECORD = os.environ["COMPACT_RECORD"]
    SID = "cli-compact-req"

    def emit(obj):
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()

    def usage():
        return {"input_tokens": 1, "output_tokens": 1,
                "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}

    def result(text=""):
        return {"type": "result", "subtype": "success", "is_error": False,
                "num_turns": 1, "session_id": SID, "total_cost_usd": 0.0,
                "usage": usage(), "result": text}

    def prompt_text(msg):
        content = msg.get("message", {}).get("content")
        if isinstance(content, str):
            return content
        return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict))

    if "--version" in sys.argv:
        print("claude 9.9.9 (fake compact)")
        sys.exit(0)

    first = True
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        if msg.get("type") == "control_request":
            emit({"type": "control_response", "response": {
                "subtype": "success", "request_id": msg.get("request_id"),
                "response": {}}})
            continue
        if msg.get("type") != "user":
            continue
        text = prompt_text(msg)
        with open(RECORD, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(text) + "\n")
        if first:
            first = False
            emit({"type": "system", "subtype": "init", "session_id": SID,
                  "model": "claude-fake", "tools": ["Bash"], "mcp_servers": []})
        if text.startswith("/compact"):
            if MODE == "refuse":
                emit(result("Not enough messages to compact."))
                continue
            emit({"type": "system", "subtype": "status", "status": "compacting",
                  "session_id": SID})
            emit({"type": "system", "subtype": "status", "status": None,
                  "compact_result": "success", "session_id": SID})
            emit({"type": "system", "subtype": "compact_boundary", "session_id": SID,
                  "uuid": "b-1", "compact_metadata": {
                      "trigger": "manual", "pre_tokens": 870000, "post_tokens": 9000}})
            emit(result("Compacted."))
            continue
        emit({"type": "assistant", "session_id": SID, "message": {
            "id": "msg_%d" % time.time_ns(), "model": "claude-fake",
            "content": [{"type": "text", "text": "ANSWER"}], "usage": usage(),
            "stop_reason": "end_turn"}})
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


async def _adapter(monkeypatch, tmp_path, mode: str):
    script = tmp_path / "fake_compact_claude.py"
    script.write_text(FAKE_CLI)
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(script)])
    options = SessionOptions(
        model="fable",
        system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND"},
        cwd=str(tmp_path),
        permission_mode="default",
        env={
            "COMPACT_MODE": mode,
            "COMPACT_RECORD": str(tmp_path / "record.jsonl"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "config"),
        },
        extra={"fluid_registry": ToolRegistry()},
    )
    adapter = ha.HarnessAdapter()
    await adapter.connect(options)
    # A turn note to be appended (or, for a command, not).
    adapter._turn_context_note = lambda: "<turn-note>today and the branch</turn-note>"
    return adapter


async def _turn(adapter, prompt: str, *, source: str | None = None) -> list:
    if source:
        adapter.set_turn_source(source)
    await adapter.send_query(prompt)
    messages = []
    async for message in adapter.receive_events():
        messages.append(message)
        if isinstance(message, ResultMessage):
            return messages
    raise AssertionError("turn ended without a result")


def _system(messages, subtype: str) -> list:
    return [m.data for m in messages if isinstance(m, SystemMessage) and m.subtype == subtype]


def _recorded(tmp_path) -> list[str]:
    path = tmp_path / "record.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.mark.asyncio
async def test_compact_reaches_the_cli_bare_and_shows_its_card(monkeypatch, tmp_path):
    adapter = await _adapter(monkeypatch, tmp_path, "ok")
    try:
        await _turn(adapter, "hello")
        messages = await _turn(adapter, "/compact")
        from fluid.events import ContextCompaction
        [boundary] = [e for e in await adapter._engine.store.events_since(adapter._engine.session_id, -1)
                      if isinstance(e, ContextCompaction)]
    finally:
        await adapter.disconnect()
    hello, compact = _recorded(tmp_path)
    assert hello.startswith("hello") and "<turn-note>" in hello
    assert compact == "/compact"
    cards = _system(messages, "compact_boundary")
    assert cards == [{"compact_metadata": {
        "trigger": "manual", "pre_tokens": 870000, "post_tokens": 9000,
    }, "compaction_event_id": f"{boundary.session_id}:{boundary.seq}",
        "compaction_event_ts": boundary.ts}]
    assert _system(messages, "compact_failed") == []


@pytest.mark.asyncio
async def test_a_compact_the_app_sent_is_marked_as_the_apps(monkeypatch, tmp_path):
    adapter = await _adapter(monkeypatch, tmp_path, "ok")
    try:
        await _turn(adapter, "hello")
        messages = await _turn(adapter, "/compact", source="wakeup")
    finally:
        await adapter.disconnect()
    cards = _system(messages, "compact_boundary")
    assert len(cards) == 1
    assert cards[0]["compact_metadata"]["requested_by"] == "app"


@pytest.mark.asyncio
async def test_a_refused_compact_shows_as_failed_with_the_reason(monkeypatch, tmp_path):
    """#1820: no "Context Compacted" card for a compaction the CLI did not
    carry out; the chat says it failed and why."""
    adapter = await _adapter(monkeypatch, tmp_path, "refuse")
    try:
        messages = await _turn(adapter, "/compact")
    finally:
        await adapter.disconnect()
    assert _system(messages, "compact_boundary") == []
    assert _system(messages, "compact_failed") == [{
        "reason": "Not enough messages to compact.", "requested_by": "user",
    }]
