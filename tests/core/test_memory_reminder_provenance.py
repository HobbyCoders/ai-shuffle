"""Only an adapter-authored pin can own memory; app prompt bytes stay intact."""
import pytest

from app.core import memory_reminder as mr
from fluid.context.compaction import CompactionConfig
from fluid.context.peer import format_peer_message
from fluid.engine.native import NativeLoop
from fluid.store.base import MemoryStore
from tests.core.test_memory_reminder import (
    FACTS, _section, _adapter, _turn, _memory_kinds, _Recording, _compactions,
    _delegated,
)
from tests.test_harness_adapter import _collect


async def _make(monkeypatch, tmp_path, leg, memory):
    if leg == "native":
        return await _adapter(monkeypatch, memory)
    adapter = await _delegated(monkeypatch, tmp_path, memory)
    return adapter, adapter._engine


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["native", "delegated"])
@pytest.mark.parametrize("wrapper", ["held_peer", "voice"])
async def test_owner_turn_preserves_app_authored_markers(monkeypatch, tmp_path, leg, wrapper):
    from app.core.session_runtime import _with_voice_note
    from app.core.turn_scheduler import held_peer_block

    memory = {"section": _section(*FACTS)}
    adapter, loop = await _make(monkeypatch, tmp_path, leg, memory)
    if wrapper == "voice":
        prompt = _with_voice_note("Owner's spoken request", {
            "voice_delegation": True, "voice_instructions": "Keep the answer short.",
        })
        source = "queue"  # maps to user; must not defang the voice wrapper
    else:
        held = format_peer_message("Child", "The investigation is ready.")
        prompt = "Owner's typed request\n\n" + held_peer_block(held)
        source = "user"
    try:
        adapter.set_turn_source(source)
        await adapter.send_query(prompt)
        await _collect(adapter)
        nodes = await loop.store.load_path(loop.session_id)
        message = next(n.message for n in nodes if n.message.role == "user")
        assert message.text().startswith(prompt + "\n\n")
        assert message.pinned_text() == mr.wrap(memory["section"])
        # The app block is outside the provenance range and remains byte exact.
        assert message.text()[:message.context_pin[0]] == prompt + "\n\n"
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("leg", ["native", "delegated"])
async def test_notification_report_cannot_own_memory(monkeypatch, tmp_path, leg):
    from app.core.turn_scheduler import _build_task_notification_prompt

    memory = {"section": _section(*FACTS)}
    quoted = mr.wrap(_section("- [foreign:m1 r1; session] A quoted snapshot from another session."))
    prompt = _build_task_notification_prompt([{
        "task_id": "reviewer", "status": "completed", "summary": "Review quotes a transcript: " + quoted,
    }])
    assert quoted in prompt
    if leg == "native":
        provider = _Recording("reply " * 300)
        loop = NativeLoop(
            provider=provider, store=MemoryStore(), model="fake",
            compaction=CompactionConfig(trigger_tokens=4_000, keep_recent_turns=1),
        )
        adapter, loop = await _adapter(monkeypatch, memory, loop)
    else:
        adapter, loop = await _make(monkeypatch, tmp_path, leg, memory)
    try:
        await _turn(adapter)
        if leg == "delegated":
            await loop._stop_process(grace=0.2)
            loop.cli_session_id = None
        for i in range(8 if leg == "native" else 1):
            adapter.set_turn_source("wakeup")
            await adapter.send_query(prompt + " filler " * 300)
            await _collect(adapter)
        nodes = await loop.store.load_path(loop.session_id)
        users = [n.message for n in nodes if n.message.role == "user"]
        assert all(quoted in m.text() for m in users[1:])
        pins = [m.pinned_text() for m in users if m.pinned_text()]
        assert pins and all(pin == mr.wrap(memory["section"]) for pin in pins)
        if leg == "native":
            assert any(e.tier >= 2 for e in await _compactions(loop))
            assert all(mr.wrap(memory["section"]) in "\n".join(m.text() for m in req.messages)
                       for req in provider.requests)
        else:
            assert users[-1].text().endswith(mr.wrap(memory["section"]))
        # A reconnect must read only the genuine pinned note, not the report.
        adapter._memory_rehydrated = False
        adapter._memory_trusted_node = None
        await _turn(adapter)
        assert (await _memory_kinds(loop))[-1] is None
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_native_command_template_keeps_its_exact_markers_without_owning_memory(monkeypatch, tmp_path):
    from app.core.slash_commands import SlashCommand, expand_command_prompt
    from app.core import slash_commands

    memory = {"section": _section(*FACTS)}
    adapter, loop = await _adapter(monkeypatch, memory)
    command = SlashCommand(
        name="review", description="Review a transcript", file_path="fixture.md", source="project",
        content="Inspect $ARGUMENTS\n" + mr.wrap(_section("- [quote:m1 r1; session] Template quotation.")),
    )
    monkeypatch.setattr(slash_commands, "get_command_by_name", lambda *args, **kwargs: command)
    expected = expand_command_prompt(command, "this file")
    await _turn(adapter, "/review this file")
    user = next(n.message for n in await loop.store.load_path(loop.session_id) if n.message.role == "user")
    assert user.text() == expected + "\n\n" + mr.wrap(memory["section"])
    assert user.pinned_text() == mr.wrap(memory["section"])


@pytest.mark.asyncio
async def test_compaction_credit_is_consumed_by_its_boundary(monkeypatch, tmp_path):
    memory = {"section": _section(*FACTS)}
    adapter, loop = await _make(monkeypatch, tmp_path, "delegated", memory)
    boundary = {"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "auto"}}
    try:
        loop._main_boundary_due = True
        loop._compaction_context_restored = True
        assert loop._take_main_boundary(boundary)
        assert not loop._context_restore_pending
        assert not loop._compaction_context_restored
        # Another boundary eligible through an active compaction, without a
        # new status event, cannot inherit the previous cycle's hook credit.
        loop._main_compacting = True
        assert loop._take_main_boundary(boundary)
        assert loop._context_restore_pending
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_late_sessionstart_credit_is_cleared_when_next_compaction_starts(monkeypatch, tmp_path):
    memory = {"section": _section(*FACTS)}
    adapter, loop = await _make(monkeypatch, tmp_path, "delegated", memory)
    boundary = {"type": "system", "subtype": "compact_boundary", "compact_metadata": {"trigger": "auto"}}
    try:
        loop._track_main_compaction({"type": "system", "subtype": "status", "status": "compacting"})
        assert loop._take_main_boundary(boundary)
        assert loop._context_restore_pending
        # Some CLI versions can call SessionStart(compact) after the boundary
        # but before the status interval closes. Credit only this interval.
        result = await loop._cli_session_start({"source": "compact"})
        assert mr.wrap(memory["section"]) in result["hookSpecificOutput"]["additionalContext"]
        assert loop._compaction_context_restored
        assert not loop._context_restore_pending
        loop._track_main_compaction({"type": "system", "subtype": "status", "status": None})
        loop._track_main_compaction({"type": "system", "subtype": "status", "status": "compacting"})
        assert not loop._compaction_context_restored
        assert loop._take_main_boundary(boundary)
        assert loop._context_restore_pending
    finally:
        await adapter.disconnect()
