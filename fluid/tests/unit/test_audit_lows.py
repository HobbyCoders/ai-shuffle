"""The 2026-09-01 audit's low findings, one regression each.

Every test here reproduces the audit's input against the fixed code: the
store's session cache, the credential-file exemptions, an MCP result with no
text part, a tool list that changes under a session, a plugin server's
environment, the synthesis cursor across a corrupt journal line, memory
order after an eviction, a bus reused across sessions, a stream that skips
its block_start, and a question nobody answers.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from fluid.engine.native import NativeLoop
from fluid.events import TurnComplete, UserTurn
from fluid.events.bus import EventBus
from fluid.providers.base import StreamChunk
from fluid.store.base import MemoryStore
from fluid.store.jsonl import _MAX_CACHED_SESSIONS, JsonlStore
from tests.unit.test_tool_loop import ScriptedProvider, _text


# ─── STO: the store forgets sessions it is not using ─────────────────────────


async def test_store_keeps_a_bounded_number_of_sessions_in_memory(tmp_path) -> None:
    # Earlier processes left these logs behind.
    writer = JsonlStore(tmp_path)
    others = []
    for _ in range(_MAX_CACHED_SESSIONS + 4):
        others.append((await writer.create_session()).session_id)
    writer.close()

    store = JsonlStore(tmp_path)
    live = (await store.create_session()).session_id
    # Touch every old session the way a cross-session listing does.
    for sid in others:
        await store.load_path(sid)
    assert len(store._nodes) <= _MAX_CACHED_SESSIONS
    # The live session (write lock held) is never the one dropped.
    assert live in store._nodes
    # A dropped session reloads from disk on the next access.
    dropped = next(s for s in others if s not in store._nodes)
    assert await store.load_path(dropped) is not None
    assert dropped in store._nodes
    store.close()


# ─── ENG: .env.example is exempt from the credential ask ─────────────────────


def test_env_templates_are_read_without_a_prompt() -> None:
    from fluid.config.loader import _sensitive_file_rules
    from fluid.permissions.engine import PermissionEngine
    from fluid.tools.builtins.read import SPEC as read_spec

    engine = PermissionEngine("on_write", rules=_sensitive_file_rules())
    assert engine.decide(read_spec, {"file_path": ".env"}).decision == "ask"
    assert engine.decide(read_spec, {"file_path": "app/.env.local"}).decision == "ask"
    assert engine.decide(read_spec, {"file_path": ".env.example"}).decision == "allow"
    assert engine.decide(read_spec, {"file_path": "app/.env.sample"}).decision == "allow"


# ─── ENG: MCP results with no text part ──────────────────────────────────────


def test_mcp_resource_audio_and_structured_results_are_not_empty() -> None:
    from fluid.tools.mcp.client import _map_call_result

    out = _map_call_result({
        "content": [
            {"type": "resource", "resource": {"uri": "file:///notes.md", "text": "hello"}},
            {"type": "resource", "resource": {"uri": "file:///a.bin", "mimeType": "application/octet-stream", "blob": "QUJD"}},
            {"type": "audio", "data": "QUJD", "mimeType": "audio/wav"},
            {"type": "resource_link", "uri": "file:///b.md", "name": "b"},
        ],
    })
    assert "hello" in out["text"]
    assert "file:///a.bin" in out["text"] and "omitted" in out["text"]
    assert "audio/wav" in out["text"]
    assert "file:///b.md" in out["text"]

    structured_only = _map_call_result({
        "content": [], "structuredContent": {"temperature": 21, "unit": "C"},
    })
    assert json.loads(structured_only["text"]) == {"temperature": 21, "unit": "C"}
    assert structured_only["structured"] == {"temperature": 21, "unit": "C"}


def test_tool_list_changed_drops_the_cached_listing() -> None:
    from fluid.tools.mcp.client import McpClient, _is_tool_list_changed

    class Notification:
        method = "notifications/tools/list_changed"

    class Other:
        method = "notifications/message"

    assert _is_tool_list_changed(Notification())
    assert not _is_tool_list_changed(Other())

    client = McpClient("srv", {"transport": "stdio", "command": "x"})
    client._cache = object()
    asyncio.run(client._on_server_notification(Other()))
    assert client._cache is not None
    asyncio.run(client._on_server_notification(Notification()))
    assert client._cache is None


# ─── CFG: a plugin's servers get CLAUDE_PLUGIN_ROOT ──────────────────────────


def test_plugin_stdio_servers_receive_the_plugin_root(tmp_path: Path) -> None:
    from fluid.context.plugins import _mcp_servers_for

    root = tmp_path / "plug"
    root.mkdir()
    manifest = {"mcpServers": {"srv": {"command": "python", "args": ["${CLAUDE_PLUGIN_ROOT}/s.py"]}}}
    out = _mcp_servers_for(root, manifest, project_dir=tmp_path, disabled=set())
    assert out["srv"]["env"]["CLAUDE_PLUGIN_ROOT"] == str(root)
    assert out["srv"]["args"] == [f"{root}/s.py"]


# ─── CL: the synthesis cursor steps over a corrupt journal line ──────────────


def test_journal_slice_counts_the_lines_it_consumed(tmp_path: Path) -> None:
    from fluid.codemode.skills import SkillStore

    store = SkillStore(tmp_path)
    journal = store._journal_path
    journal.parent.mkdir(parents=True, exist_ok=True)
    journal.write_text(
        json.dumps({"kind": "a"}) + "\n" + "{not json\n" + json.dumps({"kind": "b"}) + "\n",
        encoding="utf-8",
    )
    entries, reached = store.journal_slice(0, 10)
    assert [e["kind"] for e in entries] == ["a", "b"]
    assert reached == 3  # the corrupt line counts as consumed
    # A cursor advanced by the entry count would re-read line 3 forever.
    assert store.journal_slice(reached, 10) == ([], 3)


def test_memory_eviction_keeps_the_survivors_in_order(tmp_path: Path, monkeypatch) -> None:
    from fluid.codemode import skills as skills_mod
    from fluid.codemode.skills import SkillStore

    monkeypatch.setattr(skills_mod, "MAX_MEMORIES", 3)
    store = SkillStore(tmp_path)
    ids = [store.save_memory(f"fact {i}").id for i in range(3)]
    # The first entry is the one retrieval keeps asking for.
    store.note_memories_recalled([ids[0]])
    store.save_memory("fact 3")
    remaining = [m.text for m in store.memories()]
    assert len(remaining) == 3
    assert "fact 0" in remaining  # protected by its recalls
    # File order is creation order, not the eviction ranking.
    assert remaining == sorted(remaining, key=lambda t: int(t.split()[-1]))


# ─── STO: close() ends today's subscribers, not tomorrow's ───────────────────


async def test_bus_close_does_not_end_later_subscriptions() -> None:
    from fluid.events import TextDelta

    def delta(sid: str, text: str) -> TextDelta:
        return TextDelta(session_id=sid, turn_id="t", index=0, text=text)

    bus = EventBus()
    await bus.publish(delta("s1", "one"))
    await bus.close()

    async def collect() -> list:
        got = []
        async for event in bus.subscribe("s2", from_store=False):
            got.append(event)
            if len(got) == 2:
                break
        return got

    task = asyncio.create_task(collect())
    await asyncio.sleep(0.05)
    await bus.publish(delta("s2", "two"))
    await bus.publish(delta("s2", "three"))
    got = await asyncio.wait_for(task, timeout=5)
    assert [e.text for e in got] == ["two", "three"]


# ─── ENG: a delta with no block_start opens its own block ────────────────────


async def test_text_delta_without_block_start_does_not_lose_the_turn() -> None:
    script = [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("text_delta", {"index": 0, "text": "no start, "}),
        StreamChunk("text_delta", {"index": 0, "text": "still here"}),
        StreamChunk("block_stop", {"index": 0}),
        StreamChunk("usage", {"usage": {"output_tokens": 4}, "stop_reason": "end_turn"}),
        StreamChunk("message_stop", {}),
    ]
    provider = ScriptedProvider([script, _text("done")])
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)
    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(loop.wait_turn(), timeout=5)
    events = loop.bus.log(sid)
    assert any(isinstance(e, TurnComplete) for e in events)
    nodes = await store.load_path(sid)
    assert nodes[-1].message.text() == "no start, still here"


# ─── ENG: a question nobody answers does not park the turn forever ───────────


async def test_unanswered_question_times_out() -> None:
    from fluid.tools.registry import InputRequired

    store = MemoryStore()
    sid = (await store.create_session()).session_id
    loop = NativeLoop(provider=ScriptedProvider([_text("x")]), store=store, model="m", session_id=sid)
    loop.question_timeout = 0.1
    marker = InputRequired(server="srv", tool="t", request_state="rs", requests={
        "r1": {"message": "which one?"},
    })
    answers = await asyncio.wait_for(
        loop._collect_input_responses(sid, "turn", "mcp__srv__t", "tu", marker),
        timeout=5,
    )
    assert answers is None
    assert not loop._pending_questions
