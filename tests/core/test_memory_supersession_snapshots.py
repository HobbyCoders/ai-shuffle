"""Supersession travels through owner routing, NativeLoop, restore and folds."""
import pytest
from app.core import fluid_memory
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions
from fluid.codemode.skills import SkillStore
from fluid.engine.native import NativeLoop
from fluid.events import ContextCompaction
from fluid.store.base import MemoryStore
from tests.core.test_memory_reminder import _REPLY, _turn, _memory_kinds
from tests.test_harness_adapter import FakeProvider

OLD = "Backend subagent code tasks use Orion."
NEW = "Backend subagent code tasks use Lyra, replacing Orion."


@pytest.mark.asyncio
@pytest.mark.parametrize("reason,expected", [("trigger", None), ("cli_auto_compact", "full")])
async def test_current_fact_survives_adapter_restoration_and_compaction(tmp_path, monkeypatch, reason, expected):
    store = SkillStore(tmp_path)
    store.save_memory(OLD, key="subagents.model-routing", scope="project:fixture", protected=True)
    for i in range(15):
        store.save_memory(f"Coding convention {i}. " + "Use consistent spacing. " * 8, protected=True)
    monkeypatch.setattr(fluid_memory, "session_memory", lambda sid, cwd: (SkillStore(tmp_path), "project:fixture"))
    loop = NativeLoop(provider=FakeProvider(_REPLY), store=MemoryStore(), model="fake")
    await loop._ensure_session()
    def adapter():
        result = HarnessAdapter()
        result._engine = loop
        result._options = SessionOptions(model="fake")
        result._connected = True
        result._sdk_session_id = loop.session_id
        monkeypatch.setattr(result, "_resolve_app_session_id", lambda: "fixture-owner-session")
        for name in ("_env_note", "_away_note", "_scratch_note"):
            monkeypatch.setattr(result, name, lambda: None)
        return result
    current = adapter()
    try:
        await _turn(current, "Review coding conventions")
        store.save_memory(NEW, key="delegation.model", protected=True)
        await _turn(current, "Continue the code review")
        assert await _memory_kinds(loop) == ["full", "changes"]
        held = await current._memory_held()
        assert any(NEW in line for line in held.items)
        assert not any(OLD in line for line in held.items)
        assert not any("unresolved conflict" in line for line in held.items)
        current._observe_memory_event(ContextCompaction(session_id=loop.session_id, tier=3, reason=reason))
        await _turn(current, "Continue the code review")
        assert (await _memory_kinds(loop))[-1] == expected
        current = adapter()  # recover the host-marked snapshot chain
        await _turn(current, "Continue after restoring the session")
        assert (await _memory_kinds(loop))[-1] is None
        restored = await current._memory_held()
        assert any(NEW in line for line in restored.items)
        assert not any(OLD in line for line in restored.items)
        assert not any("unresolved conflict" in line for line in restored.items)
    finally:
        await current.disconnect()
