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

    MODE = os.environ["COMPACT_MODE"]  # "ok", "refuse", "noop" or "fluid"
    RECORD = os.environ["COMPACT_RECORD"]
    SID = "cli-compact-req"
    CONTEXT = int(os.environ.get("COMPACT_CONTEXT") or 0)  # Messages tokens, 0: no breakdown

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

    def not_compacted(reason):
        # What Claude Code 2.1.311 prints when a session.compact hook skips:
        # the status pair, a synthetic assistant line, then a successful
        # result with no model call, the same line as its text.
        line = "Not compacted \u00b7 " + reason
        emit({"type": "system", "subtype": "status", "status": "compacting", "session_id": SID})
        emit({"type": "system", "subtype": "status", "status": None, "session_id": SID})
        emit({"type": "assistant", "session_id": SID, "message": {
            "id": "msg_synthetic", "model": "<synthetic>", "role": "assistant",
            "content": [{"type": "text", "text": line}], "usage": usage(),
            "stop_reason": "stop_sequence"}})
        out = result(line)
        out["num_turns"] = 0
        emit(out)

    # Real-shaped rows the mod would post: a turn, a thinking-only assistant
    # row (its text is empty), Claude Code's own skill text in the newest
    # turn, and a long paste.
    ROWS = [
        {"role": "user", "text": "hello", "toolUses": [], "handle": "h1"},
        {"role": "assistant", "text": "", "toolUses": [], "handle": "h2"},
        {"role": "assistant", "text": "ANSWER", "toolUses": [], "handle": "h3"},
        {"role": "user", "text": "SPEC " + "s" * 30000, "toolUses": [], "handle": "h4"},
        {"role": "user", "text": "SKILL-BODY: never push to main", "toolUses": [], "handle": "h5"},
        {"role": "assistant", "text": "ok", "toolUses": [], "handle": "h6"},
    ]

    def ask_fluid():
        """The mod's session.compact hook, as far as the bridge sees it."""
        import http.client, socket
        url = os.environ.get("FLUID_CLAUDE_MOD_COMPACT_URL")
        if not url:
            return None
        body = json.dumps({"v": 1, "trigger": "manual", "messages": ROWS})
        headers = {"Authorization": "Bearer " + os.environ["FLUID_CLAUDE_MOD_TOKEN"],
                   "Content-Type": "application/json"}
        sock_path = os.environ.get("FLUID_CLAUDE_MOD_SOCKET")
        host = url.split("/")[2]
        conn = http.client.HTTPConnection(host, timeout=30)
        if sock_path:
            conn.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            conn.sock.connect(sock_path)
        conn.request("POST", "/" + url.split("/", 3)[3], body, headers)
        answer = conn.getresponse()
        return answer.status, json.loads(answer.read() or b"null")

    def prompt_text(msg):
        content = msg.get("message", {}).get("content")
        if isinstance(content, str):
            return content
        return "".join(b.get("text", "") for b in content or [] if isinstance(b, dict))

    def answer_control(msg):
        answer = {}
        if (msg.get("request") or {}).get("subtype") == "get_context_usage" and CONTEXT:
            # The CLI's /context breakdown: 61k of fixed overhead.
            answer = {"totalTokens": 61000 + CONTEXT, "maxTokens": 200000,
                      "categories": [
                          {"name": "System prompt", "tokens": 9000},
                          {"name": "System tools", "tokens": 40000},
                          {"name": "Memory files", "tokens": 12000},
                          {"name": "Messages", "tokens": CONTEXT},
                          {"name": "Free space", "tokens": 100000},
                          {"name": "Autocompact buffer", "tokens": 33000}]}
        emit({"type": "control_response", "response": {
            "subtype": "success", "request_id": msg.get("request_id"),
            "response": answer}})

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
            answer_control(msg)
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
            if MODE == "noop":
                # The mod skipped a manual compaction Fluid could not shrink.
                not_compacted("Nothing more to compact")
                continue
            if MODE == "fluid":
                answered = ask_fluid()
                with open(RECORD, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps({"bridge": answered}) + "\n")
                if answered and answered[0] == 200 and "skip" in (answered[1] or {}):
                    not_compacted(answered[1]["skip"])
                else:
                    emit(result("unexpected bridge answer"))
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
        if CONTEXT:
            # The engine asks the call's /context snapshot; answer it before
            # the result ends the turn, as the real CLI does mid-turn.
            asked = json.loads(sys.stdin.readline())
            answer_control(asked)
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


async def _adapter(monkeypatch, tmp_path, mode: str, *, context: int = 0):
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
            "COMPACT_CONTEXT": str(context),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "config"),
        },
        extra={"fluid_registry": ToolRegistry()},
    )
    adapter = ha.HarnessAdapter()
    await adapter.connect(options)
    # A turn note to be appended (or, for a command, not).
    adapter._turn_context_note = lambda query="": "<turn-note>today and the branch</turn-note>"
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
        # No /context breakdown seen: the count is labelled as history only.
        "post_tokens_basis": "history",
    }, "compaction_event_id": f"{boundary.session_id}:{boundary.seq}",
        "compaction_event_ts": boundary.ts,
        "compaction_after_node_id": boundary.node_id}]
    assert _system(messages, "compact_failed") == []


@pytest.mark.asyncio
async def test_the_cards_after_count_is_on_the_meters_basis(monkeypatch, tmp_path):
    """#1907: the CLI's post_tokens counts the summary alone, the meter the
    whole prompt. The card carries the meter's reading after the compaction:
    the summary plus the overhead of the latest /context snapshot."""
    adapter = await _adapter(monkeypatch, tmp_path, "ok", context=20000)
    try:
        await _turn(adapter, "hello")
        messages = await _turn(adapter, "/compact")
    finally:
        await adapter.disconnect()
    [card] = _system(messages, "compact_boundary")
    metadata = card["compact_metadata"]
    assert metadata["post_tokens"] == 9000  # the CLI's raw count, unchanged
    assert metadata["context_post_tokens"] == 9000 + 61000
    assert "post_tokens_basis" not in metadata


@pytest.mark.asyncio
async def test_a_reconnected_adapter_takes_the_overhead_from_the_saved_snapshot(monkeypatch, tmp_path):
    """#1907: with no live /context snapshot yet, the persisted one supplies
    the overhead before the compaction card is built."""
    from app.db import database

    adapter = await _adapter(monkeypatch, tmp_path, "ok")
    snapshot = {"captured_at": "2026-10-06T00:00:00Z", "data": {
        "totalTokens": 75000, "maxTokens": 200000,
        "categories": [{"name": "System prompt", "tokens": 50000},
                       {"name": "Messages", "tokens": 25000}]}}
    asked = []
    monkeypatch.setattr(adapter, "_resolve_app_session_id", lambda: "app-sess")
    monkeypatch.setattr(database, "get_session_context_usage_snapshot",
                        lambda sid: asked.append(sid) or snapshot)
    try:
        messages = await _turn(adapter, "/compact")
    finally:
        await adapter.disconnect()
    [card] = _system(messages, "compact_boundary")
    assert card["compact_metadata"]["context_post_tokens"] == 9000 + 50000
    assert asked == ["app-sess"]


@pytest.mark.parametrize("breakdown, expected", [
    ({"totalTokens": 70000, "categories": [{"name": "Messages", "tokens": 20000}]}, 50000),
    ({"totalTokens": 70000, "categories": [{"name": "System prompt", "tokens": 9000}]}, None),
    ({"categories": [{"name": "Messages", "tokens": 20000}]}, None),
    ({"totalTokens": 10, "categories": [{"name": "Messages", "tokens": 20}]}, None),
    ({}, None),
    (None, None),
])
def test_context_overhead_is_the_total_less_the_messages(breakdown, expected):
    assert ha._context_overhead_tokens(breakdown) == expected


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


@pytest.mark.asyncio
async def test_a_compact_with_nothing_left_to_reclaim_is_a_no_op_not_a_failure(monkeypatch, tmp_path):
    """End to end through the real bridge route: the CLI posts its rows,
    Fluid finds nothing it can reclaim and answers a skip, the CLI prints its
    "Not compacted" line (as Claude Code 2.1.311 does) and the chat shows the
    plain no-op card. No summarizer, no compacted card, no reply bubble."""
    from fluid.engine.delegated import NOTHING_TO_COMPACT
    from app.core.provider.events import AssistantMessage

    adapter = await _adapter(monkeypatch, tmp_path, "fluid")
    try:
        await _turn(adapter, "hello")
        messages = await _turn(adapter, "/compact")
    finally:
        await adapter.disconnect()
    bridge = [r for r in _recorded(tmp_path) if isinstance(r, dict)]
    assert bridge == [{"bridge": [200, {"skip": NOTHING_TO_COMPACT}]}]
    assert _system(messages, "compact_boundary") == []
    assert _system(messages, "compact_failed") == [{
        "reason": NOTHING_TO_COMPACT, "nothing_to_compact": True, "requested_by": "user",
    }]
    # Claude Code's synthetic "Not compacted" line is not a reply.
    assert not [m for m in messages if isinstance(m, AssistantMessage)
                and "Not compacted" in json.dumps(getattr(m, "content", ""), default=str)]


@pytest.mark.asyncio
async def test_a_cli_skip_without_fluid_shows_the_failure_card_without_a_reply(monkeypatch, tmp_path):
    adapter = await _adapter(monkeypatch, tmp_path, "noop")
    try:
        messages = await _turn(adapter, "/compact")
    finally:
        await adapter.disconnect()
    from app.core.provider.events import AssistantMessage

    [card] = _system(messages, "compact_failed")
    assert card["reason"] == "Not compacted \u00b7 Nothing more to compact"
    assert "nothing_to_compact" not in card
    assert not [m for m in messages if isinstance(m, AssistantMessage)
                and "Not compacted" in json.dumps(getattr(m, "content", ""), default=str)]
