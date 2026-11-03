"""Scratch is segment context, with receipts after actual model/hook delivery."""
import pytest
from fluid.state import scratch_prompt
from fluid.tools.registry import ToolCtx
from tests.core.test_memory_reminder import _delegated, _delegated_turn, _adapter, _turn

SCRATCH = scratch_prompt(ToolCtx(cwd="/fixture", session_id="fixture",
                                extra={"harness_state_root": "/fixture/state"}))


async def copies(adapter):
    nodes = await adapter._engine.store.load_path(adapter._engine.session_id)
    return [(n.message.text() or "").count("# Harness scratch")
            for n in nodes if n.message.role == "user"]


async def delegated(monkeypatch, tmp_path):
    adapter = await _delegated(monkeypatch, tmp_path, {"section": "# Memory\n- fixture"})
    monkeypatch.setattr(adapter, "_scratch_note", lambda: SCRATCH)
    return adapter


@pytest.mark.asyncio
async def test_one_scratch_per_segment_and_fallback_after_a_fold(monkeypatch, tmp_path):
    adapter = await delegated(monkeypatch, tmp_path)
    try:
        for text in ("first", "second", "go [compact-now]", "after", "steady"):
            await _delegated_turn(adapter, text)
        assert await copies(adapter) == [1, 0, 0, 1, 0]
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_hook_receipt_suppresses_next_copy_even_without_memory(monkeypatch, tmp_path):
    adapter = await delegated(monkeypatch, tmp_path)
    monkeypatch.setattr(adapter, "_memory_section", lambda: None)
    try:
        await _delegated_turn(adapter)
        await _delegated_turn(adapter, "go [compact-now]", put_back=True)
        await _delegated_turn(adapter)
        assert await copies(adapter) == [1, 0, 0]
        assert next(r for r in adapter._engine.last_restoration["sections"]
                    if r["name"] == "scratch")["status"] == "delivered"
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", ["fresh", "valid", "rejected"])
async def test_scratch_survives_cli_restart_and_resume(monkeypatch, tmp_path, restart):
    adapter = await delegated(monkeypatch, tmp_path)
    frames = []
    loop = adapter._engine
    original = loop._write_frame
    async def capture(frame):
        if frame.get("type") == "user":
            frames.append(str(frame["message"]["content"]))
        await original(frame)
    monkeypatch.setattr(loop, "_write_frame", capture)
    try:
        await _delegated_turn(adapter)
        await loop._stop_process(grace=0.2)
        if restart == "fresh":
            loop.cli_session_id = None
        elif restart == "rejected":
            loop.cli_session_id = "missing-conversation"
            monkeypatch.setenv("FAKE_CLAUDE_SCENARIO", "m")
        await _delegated_turn(adapter)
        assert frames[-1].count("# Harness scratch") == (0 if restart == "valid" else 1)
        await _delegated_turn(adapter)
        assert frames[-1].count("# Harness scratch") == 0
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_failed_submit_does_not_consume_scratch_and_commands_stay_verbatim(monkeypatch, tmp_path):
    adapter = await delegated(monkeypatch, tmp_path)
    loop = adapter._engine
    async def reject(op):
        raise RuntimeError("not accepted")
    try:
        with monkeypatch.context() as patch:
            patch.setattr(loop, "submit", reject)
            with pytest.raises(RuntimeError, match="not accepted"):
                await adapter.send_query("first")
        await _delegated_turn(adapter)
        await _delegated_turn(adapter, "/clear")
        await _delegated_turn(adapter)
        assert await copies(adapter) == [1, 0, 1]
    finally:
        await adapter.disconnect()


def test_scratch_block_size_and_duplicate_paragraph_guard():
    # Fixed path, with room for editorial changes, not path-dependent live data.
    assert len(SCRATCH) <= 540
    paragraphs = [p for p in SCRATCH.split("\n\n") if p.strip()]
    assert len(paragraphs) == len(set(paragraphs))


@pytest.mark.asyncio
async def test_partial_hook_does_not_repeat_delivered_scratch(monkeypatch, tmp_path):
    from fluid.engine import delegated as dl
    adapter = await delegated(monkeypatch, tmp_path)
    monkeypatch.setattr(adapter, "_memory_section", lambda: "# Memory\n" + "large fact " * 3000)
    monkeypatch.setattr(dl, "COMPACT_CONTEXT_MAX_CHARS", 2000)
    try:
        await _delegated_turn(adapter)
        await _delegated_turn(adapter, "go [compact-now]", put_back=True)
        receipts = adapter._engine.last_restoration
        assert not receipts["complete"]
        assert next(r for r in receipts["sections"] if r["name"] == "scratch")["status"] == "delivered"
        # Force the same next-turn repair the loop uses after a partial hook.
        adapter._engine._context_restore_pending = True
        await _delegated_turn(adapter)
        assert await copies(adapter) == [1, 0, 0]
        # A later fold without a receipt must NOT reuse the first hook's credit.
        await _delegated_turn(adapter, "go [compact-now]")
        await _delegated_turn(adapter)
        assert (await copies(adapter))[-1] == 1
    finally:
        await adapter.disconnect()


@pytest.mark.asyncio
async def test_merged_native_path_pins_scratch_and_fresh_delegated_adapter_replays_it(monkeypatch):
    # Main's native path-pinning and this PR's delegated segment credit must
    # coexist; neither renderer may displace the other during integration.
    memory = {"section": "# Memory\n- fixture"}
    adapter, loop = await _adapter(monkeypatch, memory)
    monkeypatch.setattr(adapter, "_scratch_note", lambda: SCRATCH)
    try:
        await _turn(adapter)
        await _turn(adapter)
        assert await copies(adapter) == [1, 0]
        fresh, _ = await _adapter(monkeypatch, memory, loop)
        fresh._is_delegated = True  # same resume-side renderer, no CLI spawn
        monkeypatch.setattr(fresh, "_scratch_note", lambda: SCRATCH)
        assert fresh._scratch_turn_note() == SCRATCH
    finally:
        await adapter.disconnect()
