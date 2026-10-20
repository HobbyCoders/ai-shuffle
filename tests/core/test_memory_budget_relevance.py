"""Task-ranked bases survive continuations and real owner-store restoration."""
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions
from fluid.codemode.skills import SkillStore
from fluid.engine.native import NativeLoop
from fluid.providers.base import StreamChunk
from fluid.store.base import MemoryStore
from tests.test_harness_adapter import FakeProvider, _collect

REPLY = [StreamChunk("message_start", {"usage": {"input_tokens": 5}}),
         StreamChunk("text_delta", {"index": 0, "text": "ok"}),
         StreamChunk("usage", {"usage": {"output_tokens": 2}, "stop_reason": "end_turn"})]


def seed(path):
    store = SkillStore(path)
    for raw in json.loads((Path(__file__).parents[2] / "fluid/tests/fixtures/memory_budget.json").read_text()):
        row = dict(raw)
        row.pop("label", None)
        store.save_memory(**row)
    return store


def adapter_for(loop, monkeypatch):
    adapter = HarnessAdapter()
    adapter._engine = loop
    adapter._options = SessionOptions(model="fake")
    adapter._connected = True
    adapter.sdk_session_id = loop.session_id
    monkeypatch.setattr(adapter, "_resolve_app_session_id", lambda: "fixture-owner-session")
    for name in ("_env_note", "_away_note", "_scratch_note"):
        monkeypatch.setattr(adapter, name, lambda: None)
    return adapter


@pytest.mark.asyncio
async def test_initial_ranked_base_is_stable_and_restored_from_owner_not_wake(tmp_path, monkeypatch):
    from app.core import fluid_memory
    seed(tmp_path)
    monkeypatch.setattr(fluid_memory, "session_memory", lambda sid, cwd: (SkillStore(tmp_path), "project:fixture"))
    loop = NativeLoop(provider=FakeProvider(REPLY), store=MemoryStore(), model="fake")
    await loop._ensure_session()
    adapter = adapter_for(loop, monkeypatch)
    try:
        adapter.set_turn_source("unpark")
        await adapter.send_query("career biography")
        await _collect(adapter)
        assert not adapter._memory_initial_task
        await adapter.send_query("memory backend delegated approval")
        await _collect(adapter)
        base = adapter._memory_section()
        assert "Authorized delegated memory backend work" in base
        assert "Career biography detail" not in base
        await adapter.send_query("continue")
        await _collect(adapter)
        nodes = await loop.store.load_path(loop.session_id)
        assert not [n.message for n in nodes if n.message.role == "user"][-1].pinned_text()
        assert adapter._memory_section() == base
        restored = adapter_for(loop, monkeypatch)
        await restored._memory_held()
        assert restored._memory_initial_task == "memory backend delegated approval"
        assert restored._memory_section() == base
        await restored.send_query("career biography")
        await _collect(restored)
        assert restored._memory_section() == base
        last = [n.message for n in await loop.store.load_path(loop.session_id) if n.message.role == "user"][-1]
        assert "Career biography detail" in last.pinned_text()
        assert len(last.pinned_text()) < len(base) / 2
        from tests.core.test_memory_reminder import _memory_kinds
        for query in ("documentation contracts", "database schema", "career biography", "continue"):
            await restored.send_query(query)
            await _collect(restored)
            assert restored._memory_section() == base
        # Topics supplement a stable base; they do not cause full base churn.
        assert (await _memory_kinds(loop)).count("full") == 1
        held = await restored._memory_held()
        from app.core.memory_reminder import TASK_TOTAL_CHARS
        assert sum(len(item) + 1 for item in held.items if "; task-match] " in item) <= TASK_TOTAL_CHARS
    finally:
        await adapter.disconnect()




@pytest.mark.asyncio
async def test_native_compaction_and_restore_preserve_first_task_and_prefix(tmp_path, monkeypatch):
    from dataclasses import replace
    from app.core import fluid_memory
    from fluid.context.compaction import CompactionConfig
    from tests.core.test_memory_reminder import _Recording, _memory_kinds
    seed(tmp_path)
    monkeypatch.setattr(fluid_memory, "session_memory", lambda sid, cwd: (SkillStore(tmp_path), "project:fixture"))
    provider = _Recording("reply " * 300)
    loop = NativeLoop(provider=provider, store=MemoryStore(), model="fake",
                      compaction=CompactionConfig(trigger_tokens=4000, keep_recent_turns=1))
    await loop._ensure_session()
    adapter = adapter_for(loop, monkeypatch)
    first = "memory backend delegated approval"
    try:
        await adapter.send_query(first)
        await _collect(adapter)
        base = adapter._memory_section()
        for i in range(10):
            await adapter.send_query(f"Topic {i} documentation contracts " + "words " * 400)
            await _collect(adapter)
        nodes = await loop.store.load_path(loop.session_id)
        folded = {**loop._compactor._digest_by_start, **loop._compactor._epoch_by_start}
        assert nodes[0].node_id in folded
        loop._compactor.config = replace(loop._compactor.config, trigger_tokens=10**9)
        before = [message.to_dict() for message in loop._compactor.view(nodes)]
        restored = adapter_for(loop, monkeypatch)
        await restored._memory_held()
        assert restored._memory_initial_task == first
        assert restored._memory_section() == base
        await restored.send_query("continue")
        await _collect(restored)
        assert (await _memory_kinds(loop)).count("full") == 1
        assert [m.to_dict() for m in provider.requests[-1].messages][:len(before)] == before
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_delegated_compaction_resends_same_ranked_base(tmp_path, monkeypatch):
    from app.core import fluid_memory
    from tests.core.test_memory_reminder import _delegated, _delegated_turn, _memory_kinds
    store = seed(tmp_path / "facts")
    monkeypatch.setattr(fluid_memory, "session_memory", lambda sid, cwd: (SkillStore(store.root), "project:fixture"))
    adapter = await _delegated(monkeypatch, tmp_path, {"section": "unused"})
    monkeypatch.setattr(adapter, "_memory_section", HarnessAdapter._memory_section.__get__(adapter))
    monkeypatch.setattr(adapter, "_resolve_app_session_id", lambda: "fixture-owner-session")
    try:
        await _delegated_turn(adapter, "memory backend delegated approval")
        base = adapter._memory_section()
        assert "Career biography detail" not in base
        for query in ("continue", "career biography", "go [compact-now]", "continue", "continue"):
            await _delegated_turn(adapter, query)
            assert adapter._memory_section() == base
        assert (await _memory_kinds(adapter._engine)).count("full") == 2
        # Restore the descriptor from the same host-produced delegated path.
        restored = adapter_for(adapter._engine, monkeypatch)
        await restored._memory_held()
        assert restored._memory_initial_task == "memory backend delegated approval"
        assert restored._memory_section() == base
    finally:
        await adapter.disconnect()






@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["automation", "agent_message", "unpark"])
async def test_restore_task_without_any_full_snapshot(tmp_path, monkeypatch, source):
    from app.core import fluid_memory
    seed(tmp_path)
    monkeypatch.setattr(fluid_memory, "session_memory", lambda sid, cwd: (SkillStore(tmp_path), "project:fixture"))
    loop = NativeLoop(provider=FakeProvider(REPLY), store=MemoryStore(), model="fake")
    await loop._ensure_session()
    adapter = adapter_for(loop, monkeypatch)
    # The producing path writes real TurnStarted sources, but no snapshot.
    monkeypatch.setattr(adapter, "_memory_section", lambda: None)
    try:
        adapter.set_turn_source(source)
        await adapter.send_query("career biography")
        await _collect(adapter)
        await adapter.send_query("memory backend delegated approval")
        await _collect(adapter)
        from app.core.memory_reminder import newest_full_node
        assert newest_full_node(await loop.store.load_path(loop.session_id)) is None
        restored = adapter_for(loop, monkeypatch)
        await restored.send_query("continue")
        await _collect(restored)
        assert restored._memory_initial_task == "memory backend delegated approval"
        assert "Career biography detail" not in restored._memory_section()
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize("prefix", ["", "/compact\n"])
def test_initial_task_uses_unpinned_owner_text_only(prefix):
    from types import SimpleNamespace
    from app.core.memory_reminder import initial_task_query
    from fluid.messages import Message, Text
    reminder = "<system•reminder>career biography</system•reminder>"
    text = "memory backend delegated approval" + reminder
    nodes = [SimpleNamespace(message=Message(role="system", content=[Text(text="wrong task")])),
             SimpleNamespace(message=Message(role="user", content=[Text(text=prefix)])),
             SimpleNamespace(message=Message(role="user", content=[Text(text=text)],
                                              context_pin=(len(text) - len(reminder), len(text))))]
    assert initial_task_query(nodes) == "memory backend delegated approval"


@pytest.mark.asyncio
async def test_rejected_first_turn_does_not_freeze_its_task(tmp_path, monkeypatch):
    from app.core import fluid_memory
    seed(tmp_path)
    monkeypatch.setattr(fluid_memory, "session_memory", lambda sid, cwd: (SkillStore(tmp_path), "project:fixture"))
    loop = NativeLoop(provider=FakeProvider(REPLY), store=MemoryStore(), model="fake")
    await loop._ensure_session()
    adapter = adapter_for(loop, monkeypatch)
    monkeypatch.setattr(loop, "submit", AsyncMock(side_effect=RuntimeError("busy")))
    try:
        with pytest.raises(RuntimeError, match="busy"):
            await adapter.send_query("career biography")
        assert not adapter._memory_initial_task
        assert not adapter._memory_trusted_node
    finally:
        await adapter.disconnect()
