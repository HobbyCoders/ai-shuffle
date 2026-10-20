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
    finally:
        await adapter.disconnect()


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
