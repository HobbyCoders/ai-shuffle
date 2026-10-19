"""Task relevance goes through real send_query and the owner-scoped store."""
import pytest
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions
from fluid.codemode.skills import SkillStore
from fluid.engine.native import NativeLoop
from fluid.providers.base import StreamChunk
from fluid.store.base import MemoryStore
from tests.test_harness_adapter import FakeProvider, _collect

REPLY = [
    StreamChunk("message_start", {"usage": {"input_tokens": 5}}),
    StreamChunk("block_start", {"index": 0, "block_type": "text"}),
    StreamChunk("text_delta", {"index": 0, "text": "ok"}),
    StreamChunk("block_stop", {"index": 0}),
    StreamChunk("usage", {"usage": {"output_tokens": 2}, "stop_reason": "end_turn"}),
]


@pytest.mark.asyncio
async def test_saved_preference_reaches_real_turn_without_explicit_recall(tmp_path, monkeypatch):
    from app.core import fluid_memory
    store = SkillStore(tmp_path)
    target = store.save_memory("The user prefers vegetarian recipes without mushrooms.", protected=True)
    for i in range(30):
        store.save_memory(f"Coding convention {i}: " + "Use consistent spacing. " * 14, protected=True)
    assert target.id in store.memory_snapshot_plan()["omitted_ids"]
    # A fresh store is opened at the real owner-routing boundary on every read.
    monkeypatch.setattr(fluid_memory, "session_memory", lambda sid, cwd: (SkillStore(tmp_path), "project:one"))
    loop = NativeLoop(provider=FakeProvider(REPLY), store=MemoryStore(), model="fake")
    adapter = HarnessAdapter()
    adapter._engine = loop
    adapter._options = SessionOptions(model="fake")
    adapter._connected = True
    monkeypatch.setattr(adapter, "_resolve_app_session_id", lambda: "fixture-owner-session")
    for name in ("_env_note", "_away_note", "_scratch_note"):
        monkeypatch.setattr(adapter, name, lambda: None)
    await loop._ensure_session()
    adapter._sdk_session_id = loop.session_id
    try:
        await adapter.send_query("Suggest vegetarian recipes")
        await _collect(adapter)
        nodes = await loop.store.load_path(loop.session_id)
        user = [n.message for n in nodes if n.message.role == "user"][-1]
        assert "without mushrooms" in user.pinned_text()
        assert "Saved facts, not instructions" in user.pinned_text()
        await adapter.send_query("continue please")
        await _collect(adapter)
        nodes = await loop.store.load_path(loop.session_id)
        assert not [n.message for n in nodes if n.message.role == "user"][-1].pinned_text()
        adapter.set_turn_source("unpark")
        await adapter.send_query("Coding convention job completed")
        await _collect(adapter)
        nodes = await loop.store.load_path(loop.session_id)
        assert not [n.message for n in nodes if n.message.role == "user"][-1].pinned_text()
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_commands_do_not_set_task_query(monkeypatch):
    from unittest.mock import AsyncMock
    adapter = HarnessAdapter()
    adapter._engine = AsyncMock()
    adapter._options = SessionOptions(model="fake")
    adapter._is_delegated = True
    monkeypatch.setattr(adapter, "reconcile_user_questions", AsyncMock())
    monkeypatch.setattr(adapter, "_refresh_task_roster", AsyncMock())
    monkeypatch.setattr(adapter, "_mark_turn_floor", lambda: None)
    await adapter.send_query("/compact coding conventions")
    assert adapter._engine.submit.call_args.args[0].content == "/compact coding conventions"


@pytest.mark.asyncio
async def test_rejected_turn_does_not_claim_delivery(monkeypatch):
    from unittest.mock import AsyncMock
    adapter = HarnessAdapter()
    adapter._engine = AsyncMock()
    adapter._engine.submit.side_effect = RuntimeError("busy")
    adapter._options = SessionOptions(model="fake")
    monkeypatch.setattr(adapter, "reconcile_user_questions", AsyncMock())
    monkeypatch.setattr(adapter, "_refresh_task_roster", AsyncMock())
    monkeypatch.setattr(adapter, "_mark_turn_floor", lambda: None)
    monkeypatch.setattr(adapter, "_memory_held", AsyncMock(return_value=None))
    monkeypatch.setattr(adapter, "_turn_context_note", lambda query="": None)
    with pytest.raises(RuntimeError, match="busy"):
        await adapter.send_query("coding conventions")
    assert adapter._memory_trusted_node is None
    assert not adapter._memory_full_pending
