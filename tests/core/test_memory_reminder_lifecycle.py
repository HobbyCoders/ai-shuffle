"""Lifecycle regressions for send-once memory (real loops, fake providers/CLI)."""
import pytest

from app.core import memory_reminder as mr
from tests.core.test_memory_reminder import (
    FACTS, _section, _adapter, _turn, _memory_kinds, _delegated, _delegated_turn,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["model", "provider", "hot_model"])
async def test_model_or_provider_switch_resends_snapshot(monkeypatch, change):
    memory = {"section": _section(*FACTS)}
    adapter, loop = await _adapter(monkeypatch, memory)
    await _turn(adapter)
    if change == "hot_model":
        await adapter.set_model("other-model")
    else:
        if change == "provider":
            from tests.test_harness_adapter import FakeProvider
            from tests.core.test_memory_reminder import _REPLY
            loop.provider = FakeProvider(_REPLY)
            loop.provider.name = "other"
        else:
            setattr(loop, change, "other")
        # A reconnect must not credit the previous model's snapshot either.
        adapter, _ = await _adapter(monkeypatch, memory, loop=loop)
    await _turn(adapter)
    await _turn(adapter)
    assert await _memory_kinds(loop) == ["full", "full", None]


@pytest.mark.asyncio
async def test_resume_with_dropped_history_and_independent_child_get_full_memory(monkeypatch):
    memory = {"section": _section(*FACTS)}
    parent, loop = await _adapter(monkeypatch, memory)
    await _turn(parent)
    # A handoff that preserved the native transcript can reuse its snapshot.
    handed_off, _ = await _adapter(monkeypatch, memory, loop=loop)
    await _turn(handed_off)
    assert await _memory_kinds(loop) == ["full", None]
    # A new child session never inherits the parent's process-local trust.
    child, child_loop = await _adapter(monkeypatch, memory)
    await _turn(child)
    assert await _memory_kinds(child_loop) == ["full"]
    # A resume with a new/empty active path needs the full copy too.
    await loop.store.set_head(loop.session_id, None)
    loop._session.head_node_id = None
    resumed, _ = await _adapter(monkeypatch, memory, loop=loop)
    await _turn(resumed)
    assert await _memory_kinds(loop) == ["full"]


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", ["no_handle", "valid_handle", "rejected_silent", "rejected_error"])
async def test_cli_restart_restores_memory_only_when_history_was_lost(monkeypatch, tmp_path, restart):
    memory = {"section": _section(*FACTS)}
    adapter = await _delegated(monkeypatch, tmp_path, memory)
    loop = adapter._engine
    frames = []
    write_frame = loop._write_frame

    async def capture(frame):
        if frame.get("type") == "user":
            frames.append(frame["message"]["content"])
        await write_frame(frame)

    monkeypatch.setattr(loop, "_write_frame", capture)
    try:
        await _delegated_turn(adapter)
        await _delegated_turn(adapter)
        prefix_before = await loop.store.load_path(loop.session_id)
        old_text = [n.message.text() for n in prefix_before]
        await loop._stop_process(grace=0.2)
        if restart == "no_handle":
            loop.cli_session_id = None
        elif restart.startswith("rejected"):
            loop.cli_session_id = "missing-conversation"
            monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "l" if restart == "rejected_silent" else "m")
        # Simulate a new adapter after a park/wake or worker handoff. The
        # host path still contains the full copy even when the CLI lost it.
        adapter._memory_rehydrated = False
        adapter._memory_trusted_node = None
        await _delegated_turn(adapter)
        sent = frames[-1]
        text = sent if isinstance(sent, str) else "\n".join(b.get("text", "") for b in sent)
        assert (mr.wrap(memory["section"]) in text) is (restart != "valid_handle")
        # Injection was at the user tail, not into the system or old history.
        if restart != "valid_handle":
            assert text.rstrip().endswith(mr.wrap(memory["section"]))
        after = await loop.store.load_path(loop.session_id)
        assert [n.message.text() for n in after[:len(prefix_before)]] == old_text
        await _delegated_turn(adapter)
        assert (await _memory_kinds(loop))[-1] is None
        # A second adapter can recover the new node without re-sending it.
        adapter._memory_rehydrated = False
        adapter._memory_trusted_node = None
        await _delegated_turn(adapter)
        assert (await _memory_kinds(loop))[-1] is None
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_fresh_cli_slash_command_stays_verbatim(monkeypatch, tmp_path):
    memory = {"section": _section(*FACTS)}
    adapter = await _delegated(monkeypatch, tmp_path, memory)
    try:
        await _delegated_turn(adapter)
        loop = adapter._engine
        await loop._stop_process(grace=0.2)
        loop.cli_session_id = None
        await _delegated_turn(adapter, "/clear")
        nodes = await loop.store.load_path(loop.session_id)
        assert [n.message.text() for n in nodes if n.message.role == "user"][-1] == "/clear"
        await _delegated_turn(adapter)
        assert (await _memory_kinds(loop))[-1] == "full"
    finally:
        await adapter.disconnect()
