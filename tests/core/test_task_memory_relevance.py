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
    for i in range(160):  # enough when-line index entries to overflow the base
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


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["native", "delegated"])
async def test_rotated_task_slice_survives_real_compaction_or_cli_restore(tmp_path, monkeypatch, leg):
    from app.core import fluid_memory
    from app.core.memory_reminder import held_from_path, newest_full_node, TASK_TOTAL_CHARS
    from tests.core.test_task_memory_growth import setup_store
    from tests.core.test_memory_reminder import _adapter, _delegated, _turn, _Recording, _compactions
    from fluid.context.compaction import CompactionConfig

    store_root = tmp_path / "memory"
    store = setup_store(store_root)
    base = store.render_memory_section()
    monkeypatch.setattr(fluid_memory, "session_memory", lambda sid, cwd: (SkillStore(store_root), "project:one"))
    if leg == "native":
        loop = NativeLoop(provider=_Recording("reply " * 300), store=MemoryStore(), model="fake",
                          compaction=CompactionConfig(trigger_tokens=4000, keep_recent_turns=1))
        adapter, loop = await _adapter(monkeypatch, {"section": base}, loop)
    else:
        adapter = await _delegated(monkeypatch, tmp_path, {"section": base})
        loop = adapter._engine
    # Use the actual owner-scoped producer, not the fixture's section stub.
    monkeypatch.setattr(adapter, "_memory_section", HarnessAdapter._memory_section.__get__(adapter))
    monkeypatch.setattr(adapter, "_resolve_app_session_id", lambda: "fixture-session")
    try:
        for i in range(4):
            await _turn(adapter, f"topic{i} habit{i}")
        nodes = await loop.store.load_path(loop.session_id)
        held = held_from_path(nodes, newest_full_node(nodes))
        assert held is not None
        assert any("topic3 habit3" in x and "; task-match] " in x for x in held.items)
        assert sum(len(x) + 1 for x in held.items if "; task-match] " in x) <= TASK_TOTAL_CHARS
        assert held.task_order
        if leg == "native":
            for i in range(10):
                await _turn(adapter, "topic3 habit3 " + "words " * 400)
            assert any(e.tier >= 2 for e in await _compactions(loop))
            # The real compacted provider view, not just the stored transcript,
            # must retain the additions and ID withdrawals.
            view = "\n".join(m.text() for m in loop._compactor.view(await loop.store.load_path(loop.session_id)))
            assert "task-match" in view and "No longer in the snapshot" in view
        else:
            await loop._stop_process(grace=0.2)
            loop.cli_session_id = None
            await _turn(adapter, "topic3 habit3")
            nodes = await loop.store.load_path(loop.session_id)
            assert "topic3 habit3" in [n.message for n in nodes if n.message.role == "user"][-1].pinned_text()
    finally:
        await adapter.disconnect()

