"""The adapter's just-in-time memory wiring (fluid.codemode.jit_delivery).

A tool call's matching facts ride its PostToolUse result on every gate and
are recorded late (the model reads them only after the call ran). PreCompact
re-arms, a user turn adds word matches to its reminder (on time), and
FLUID_MEMORY_JIT turns it all off.
"""
from __future__ import annotations

import pytest
from fluid.codemode import jit_delivery
from fluid.codemode.skills import SkillStore
from fluid.hooks.bus import POST_TOOL_USE, PRE_COMPACT, PRE_TOOL_USE, place_attachments

from app.core.config import Settings
from app.core.provider.harness_adapter import HarnessAdapter
from app.core.provider.types import SessionOptions

PUSH = "Pushes go through the merge queue; never push to main directly."
OFFSITE = "The team offsite is booked for the second week of March in Lisbon."


@pytest.fixture
def jit_adapter(tmp_path, monkeypatch):
    store = SkillStore(tmp_path / "memory")
    store.save_memory(PUSH, durable=True, when="When publishing commits",
                      triggers={"command": ["git push"]})
    store.save_memory("Release notes list every merged PR.", durable=True,
                      when="When releasing", triggers={"tools": ["Release"]})
    store.save_memory(OFFSITE, durable=True, when="When planning a team offsite dinner")
    monkeypatch.setattr("app.core.fluid_memory.session_memory", lambda sid, cwd=None: (store, None))
    monkeypatch.delenv("FLUID_MEMORY_JIT", raising=False)
    return _adapter(tmp_path, monkeypatch), store


def _adapter(tmp_path, monkeypatch, sid="app-session-1"):
    adapter = HarnessAdapter()
    adapter._options = SessionOptions(model="fake", cwd=str(tmp_path))
    monkeypatch.setattr(adapter, "_resolve_app_session_id", lambda: sid)
    return adapter


def _context(decision):
    """What a gate puts on the result: the attachments, settled as placed."""
    return "\n".join([decision.additional_context or "", *place_attachments(decision.attachments)])


@pytest.mark.asyncio
async def test_a_matching_call_gets_the_fact_with_its_result_once(jit_adapter):
    adapter, store = jit_adapter
    call = {"name": "Bash", "input": {"command": "git push --dry-run"}, "tool_use_id": "t1",
            "output": "ok"}
    # Nothing rides PreToolUse: hook context reaches the model only after
    # the call ran, so the result carries it and it is counted late.
    pre = await adapter._hooks.emit(PRE_TOOL_USE, call)
    assert PUSH not in _context(pre)
    first = await adapter._hooks.emit(POST_TOOL_USE, call)
    assert PUSH in _context(first)
    again = await adapter._hooks.emit(POST_TOOL_USE, {**call, "tool_use_id": "t2"})
    assert PUSH not in _context(again)
    assert jit_delivery.counts(store)["totals"] == {"late": 1, "command": 1}
    # Compaction may drop the text: the next match delivers it again.
    await adapter._hooks.emit(PRE_COMPACT, {})
    third = await adapter._hooks.emit(POST_TOOL_USE, {**call, "tool_use_id": "t3"})
    assert PUSH in _context(third)


@pytest.mark.asyncio
async def test_an_explicit_tool_trigger_matches_a_prefixed_tool(jit_adapter):
    adapter, store = jit_adapter
    post = await adapter._hooks.emit(POST_TOOL_USE, {
        "name": "mcp__builtins__Release", "input": {}, "tool_use_id": "t9", "output": "ok"})
    assert "Release notes" in _context(post)
    assert jit_delivery.counts(store)["totals"] == {"late": 1, "tool": 1}


@pytest.mark.asyncio
async def test_user_turn_note_carries_word_matches(jit_adapter):
    adapter, _ = jit_adapter
    note = adapter._memory_jit_note("Help me plan the team offsite dinner for spring.")
    assert note.startswith("<system-reminder>\n# Memory for this step") and OFFSITE in note
    assert adapter._memory_jit_note("Help me plan the team offsite dinner for spring.") is None


@pytest.mark.asyncio
async def test_kill_switch_turns_both_paths_off(jit_adapter, monkeypatch):
    adapter, store = jit_adapter
    monkeypatch.setenv("FLUID_MEMORY_JIT", "false")
    off = await adapter._hooks.emit(POST_TOOL_USE, {
        "name": "Bash", "input": {"command": "git push"}, "tool_use_id": "t1", "output": "ok"})
    assert PUSH not in _context(off)
    assert adapter._memory_jit_note("plan the team offsite dinner") is None
    assert not jit_delivery.counts(store).get("totals")


PUSH_CALL = {"name": "Bash", "input": {"command": "git push"}, "tool_use_id": "t1", "output": "ok"}


@pytest.mark.asyncio
async def test_a_replacement_adapter_keeps_the_session_once_set(jit_adapter, tmp_path, monkeypatch):
    """Idle eviction then resume builds a new adapter for the same session:
    it takes up what the session was given since its last compaction."""
    adapter, _ = jit_adapter
    assert PUSH in _context(await adapter._hooks.emit(POST_TOOL_USE, PUSH_CALL))
    resumed = _adapter(tmp_path, monkeypatch)
    assert PUSH not in _context(await resumed._hooks.emit(POST_TOOL_USE, PUSH_CALL))
    # Another session is unaffected.
    other = _adapter(tmp_path, monkeypatch, sid="app-session-2")
    assert PUSH in _context(await other._hooks.emit(POST_TOOL_USE, PUSH_CALL))
    # After compaction the resumed session gets it again, and a further
    # replacement does not restore what compaction dropped.
    await resumed._hooks.emit(PRE_COMPACT, {})
    again = _adapter(tmp_path, monkeypatch)
    assert PUSH in _context(await again._hooks.emit(POST_TOOL_USE, PUSH_CALL))


@pytest.mark.asyncio
async def test_only_a_placed_delivery_counts(jit_adapter):
    """A gate that drops the attachment leaves the fact undelivered: it is
    offered again with the next match and nothing is recorded for it."""
    adapter, store = jit_adapter
    dropped = await adapter._hooks.emit(POST_TOOL_USE, PUSH_CALL)
    [attachment] = dropped.attachments
    attachment.settle(False)
    assert not jit_delivery.counts(store).get("totals")
    assert PUSH in _context(await adapter._hooks.emit(POST_TOOL_USE, PUSH_CALL))
    assert jit_delivery.counts(store)["totals"] == {"late": 1, "command": 1}


@pytest.mark.asyncio
async def test_an_indexed_act_rule_is_logged_as_detail_not_late(jit_adapter):
    """Both legs render the # Memory section through _memory_section; an
    act-time rule listed there with its summary reached the model before the
    call, so the tool-time text is its detail. After compaction the index is
    noted again only when re-sent: until then a match counts late."""
    adapter, store = jit_adapter
    section = adapter._memory_section()
    assert "When publishing commits: Pushes go through the merge queue" in section
    first = await adapter._hooks.emit(POST_TOOL_USE, PUSH_CALL)
    assert PUSH in _context(first)  # the full text still rides the result
    assert jit_delivery.counts(store)["totals"] == {"detail": 1, "command": 1}
    await adapter._hooks.emit(PRE_COMPACT, {})
    again = await adapter._hooks.emit(POST_TOOL_USE, {**PUSH_CALL, "tool_use_id": "t2"})
    assert PUSH in _context(again)
    assert jit_delivery.counts(store)["totals"] == {"detail": 1, "late": 1, "command": 2}


def test_kill_switch_defaults_on_and_is_not_a_setting(monkeypatch):
    monkeypatch.delenv("FLUID_MEMORY_JIT", raising=False)
    assert jit_delivery.switched_on()
    for off in ("false", "0", " OFF ", "no"):
        monkeypatch.setenv("FLUID_MEMORY_JIT", off)
        assert not jit_delivery.switched_on()
    monkeypatch.setenv("FLUID_MEMORY_JIT", "true")
    assert jit_delivery.switched_on()
    assert "fluid_memory_jit" not in Settings.model_fields
