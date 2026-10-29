"""Claude Code compaction, end to end against the dev loop's replaying fake CLI.

The app's real integration (HarnessAdapter, DelegatedLoop, the mod's
compaction route on the bundled bridge) drives ``scripts/devloop/fake_claude``,
which prints each compaction the way Claude Code 2.1.312 does, frame for frame
and in its order (the replay fixtures in ``scripts/devloop/fake_claude/
compaction/``; the protocol is in docs/internal/claude-code-compaction-
protocol.md). One case per kind of compaction:

* automatic, answered by Fluid, mid-turn and right after a tool result: no
  ``compacting`` status, SessionStart before the closing status, the boundary,
  then the answer replayed with fresh uuids;
* a typed ``/compact`` answered by Fluid, and one Fluid has nothing left for;
* Claude Code's own summarizer (the Smarter compaction setting off);
* the CLI started again with ``--resume`` after a compaction.

Each asserts what the chat shows: one compaction card, no history stored twice
after the replay, the context put back, the follow-up turn answered.

Hermetic: the fake holds no credential and opens no socket but the app's own
loopback bridge.
"""

from __future__ import annotations

import collections
import logging
from pathlib import Path

import pytest
from fluid.engine import delegated
from fluid.events import CompactionCheckpoint, ContextArchived, ContextCompaction, ToolUse
from fluid.events.model import DelegatedDiagnostic
from fluid.messages import ToolResultBlock, ToolUseBlock
from fluid.tools.registry import ToolRegistry

import app.core.provider.harness_adapter as ha
from app.core import claude_compaction
from app.core.provider.events import AssistantMessage, ResultMessage, SystemMessage
from app.core.provider.types import SessionOptions
from scripts.devloop import fake_claude

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "ws" / ".git").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    from app.core import guidance_surface

    monkeypatch.setenv(guidance_surface.GUIDANCE_HOME_ENV, str(home))


async def _adapter(monkeypatch, tmp_path: Path, *, fluid: bool = True) -> ha.HarnessAdapter:
    """The app's adapter on the fake CLI, as a dev instance runs it."""
    instance = tmp_path / "instance"
    instance.mkdir(exist_ok=True)
    shim = fake_claude.install(instance) / "claude"
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda **_: [str(shim)])
    monkeypatch.setattr(claude_compaction, "read", lambda: fluid)
    options = SessionOptions(
        model="sonnet",
        system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND"},
        cwd=str(tmp_path / "ws"),
        permission_mode="default",
        # Inside the fake's root, so it keeps a transcript and --resume works.
        env={"CLAUDE_CONFIG_DIR": str(instance / "config")},
        extra={"fluid_registry": ToolRegistry()},
    )
    adapter = ha.HarnessAdapter()
    await adapter.connect(options)
    return adapter


async def _turn(adapter: ha.HarnessAdapter, prompt: str) -> list:
    await adapter.send_query(prompt)
    messages = []
    async for message in adapter.receive_events():
        messages.append(message)
        if isinstance(message, ResultMessage):
            return messages
    raise AssertionError("turn ended without a result")


def _system(messages: list, subtype: str) -> list[dict]:
    return [m.data for m in messages if isinstance(m, SystemMessage) and m.subtype == subtype]


def _replies(messages: list) -> list[str]:
    return [b.text for m in messages if isinstance(m, AssistantMessage)
            for b in m.content if getattr(b, "text", None)]


async def _events(adapter: ha.HarnessAdapter) -> list:
    engine = adapter._engine
    return list(await engine.store.events_since(engine.session_id, -1))


async def _stored_tool_ids(adapter: ha.HarnessAdapter) -> tuple[list[str], list[str]]:
    engine = adapter._engine
    calls, results = [], []
    for node in await engine.store.load_path(engine.session_id):
        for block in node.message.content:
            if isinstance(block, ToolUseBlock):
                calls.append(block.id)
            elif isinstance(block, ToolResultBlock):
                results.append(block.tool_use_id)
    return calls, results


def _repeated(ids: list[str]) -> list[str]:
    return [i for i, n in collections.Counter(ids).items() if n > 1]


async def _history(adapter: ha.HarnessAdapter, reads: int = 3) -> None:
    """A conversation Fluid can compact: earlier turns with big tool results."""
    for _ in range(reads):
        messages = await _turn(adapter, "[script:bulk-read] read the big file")
        assert _replies(messages)[-1] == "Read 45,000 characters."


async def _assert_one_fluid_compaction(adapter, trigger: str, caplog) -> ContextCompaction:
    events = await _events(adapter)
    [compaction] = [e for e in events if isinstance(e, ContextCompaction)]
    assert compaction.reason == f"cli_{trigger}_compact"
    assert compaction.before_tokens > compaction.after_tokens > 0
    assert compaction.compaction_id.startswith("cmp_")
    # The boundary named the answer it applied: its portable checkpoint is kept.
    checkpoints = [e for e in events if isinstance(e, CompactionCheckpoint)]
    assert [c.compaction_id for c in checkpoints] in ([], [compaction.compaction_id])
    # The context was put back by SessionStart(compact): the main thread's.
    assert adapter._engine.last_restoration is not None
    assert "a subagent's compaction boundary" not in caplog.text
    calls, results = await _stored_tool_ids(adapter)
    assert not _repeated(calls) and not _repeated(results), "history stored again after the replay"
    tool_uses = [e.tool_use_id for e in events if isinstance(e, ToolUse)]
    assert not _repeated(tool_uses), "old tool calls counted again after the replay"
    return compaction


async def test_an_automatic_fluid_compaction_mid_turn_is_the_main_conversations(
    monkeypatch, tmp_path, caplog,
):
    """The case every automatic Fluid compaction on a real worker hit
    (2026-10-27..29): Claude Code prints no ``compacting`` status when the mod
    answers, so the boundary was taken for a subagent's: no card, no
    checkpoint, and the context was never put back."""
    caplog.set_level(logging.INFO, logger="fluid.engine.delegated")
    adapter = await _adapter(monkeypatch, tmp_path)
    try:
        await _history(adapter)
        messages = await _turn(adapter, "[script:compact] carry on")
        compaction = await _assert_one_fluid_compaction(adapter, "auto", caplog)
        [card] = _system(messages, "compact_boundary")
        assert card["compact_metadata"]["trigger"] == "auto"
        assert card["compaction_event_id"].endswith(f":{compaction.seq}")
        # The answer after the compaction, once; the replayed rows are no reply.
        assert _replies(messages) == [
            "The conversation was compacted mid-turn and I carried on. You said: “carry on”"]
        follow = await _turn(adapter, "And now?")
        assert "You said: “And now?”" in _replies(follow)[-1]
        calls, _ = await _stored_tool_ids(adapter)
        assert len(calls) == 3
    finally:
        await adapter.disconnect()


async def test_a_compaction_right_after_a_tool_result_keeps_the_call_once(
    monkeypatch, tmp_path, caplog,
):
    """Compaction "during a tool call": the CLI compacts before the model call
    that reads the result (the shape of real mid-turn compactions)."""
    caplog.set_level(logging.INFO, logger="fluid.engine.delegated")
    adapter = await _adapter(monkeypatch, tmp_path)
    try:
        await _history(adapter, reads=2)
        messages = await _turn(adapter, "[script:compact-after-tool]")
        await _assert_one_fluid_compaction(adapter, "auto", caplog)
        assert _replies(messages)[-1] == "Compacted after reading it; the result is still here."
        follow = await _turn(adapter, "Next.")
        assert "You said: “Next.”" in _replies(follow)[-1]
        calls, results = await _stored_tool_ids(adapter)
        assert len(calls) == len(results) == 3
    finally:
        await adapter.disconnect()


async def test_a_typed_compact_answered_by_fluid_then_one_with_nothing_left(
    monkeypatch, tmp_path, caplog,
):
    """The second ``/compact`` finds nothing more to reclaim. Claude Code
    2.1.312 reports that skip as ``compact_result: failed`` with
    ``compact_error: "skipped: …"``: a no-op card, never a failed status."""
    from fluid.engine.delegated import NOTHING_TO_COMPACT

    caplog.set_level(logging.INFO, logger="fluid.engine.delegated")
    adapter = await _adapter(monkeypatch, tmp_path)
    try:
        await _history(adapter)
        messages = await _turn(adapter, "/compact")
        await _assert_one_fluid_compaction(adapter, "manual", caplog)
        assert len(_system(messages, "compact_boundary")) == 1
        assert _system(messages, "compact_failed") == []
        assert [s["state"] for s in _system(messages, "compaction_status")] == ["started", "ended"]

        again = await _turn(adapter, "/compact")
        assert _system(again, "compact_boundary") == []
        assert [s["state"] for s in _system(again, "compaction_status")] == ["started", "ended"]
        assert _system(again, "compact_failed") == [{
            "reason": NOTHING_TO_COMPACT, "nothing_to_compact": True, "requested_by": "user",
        }]
        assert not [t for t in _replies(again) if "Not compacted" in t]
        follow = await _turn(adapter, "Still there?")
        assert "You said: “Still there?”" in _replies(follow)[-1]
    finally:
        await adapter.disconnect()


@pytest.mark.parametrize("failure", ["bridge", "archive", "history"])
async def test_a_failed_manual_compaction_stays_failed_through_the_bridge(
    monkeypatch, tmp_path, caplog, failure,
):
    """Real route failures become skipped closes, not a successful no-op."""
    caplog.set_level(logging.INFO, logger="fluid.engine.delegated")
    adapter = await _adapter(monkeypatch, tmp_path)
    try:
        await _history(adapter)
        engine = adapter._engine
        compact = delegated.compact_messages
        emit = engine._emit
        archive_attempts = []

        def failing_compaction(rows, nodes, **kwargs):
            if failure == "bridge":
                raise OSError("bridge handler unavailable")
            # A provider-only attachment must be archived before it can be dropped.
            rows = [rows[0], {
                "role": "user", "handle": "provider-attachment", "toolUses": [],
                "text": "Earlier provider attachment. " * 30_000,
            }, *rows[1:]]
            return compact(rows, nodes, **kwargs)

        async def failing_archive(event):
            if isinstance(event, ContextArchived):
                archive_attempts.append(event)
                raise OSError("archive unavailable")
            return await emit(event)

        if failure == "history":
            available = engine.registry.available
            monkeypatch.setattr(engine.registry, "available", lambda name: (
                False if name == "HistoryExpand" else available(name)
            ))
        else:
            monkeypatch.setattr(delegated, "compact_messages", failing_compaction)
            if failure == "archive":
                monkeypatch.setattr(engine, "_emit", failing_archive)

        messages = await _turn(adapter, "/compact")
        [started, ended] = _system(messages, "compaction_status")
        assert started["state"] == "started"
        assert ended["state"] == "failed"
        status = 500 if failure == "bridge" else 400
        error = ("skipped: Fluid could not compact the conversation "
                 f"(bridge HTTP {status}); it is left as it is")
        assert ended["error"] == error
        assert engine._turn_compact_error == error
        assert not engine._compact_noop
        [card] = _system(messages, "compact_failed")
        assert card["reason"] == error and not card.get("nothing_to_compact")
        events = await _events(adapter)
        [diagnostic] = [e for e in events if isinstance(e, DelegatedDiagnostic)
                        and e.kind == "compaction_failed"]
        assert diagnostic.message == diagnostic.data["compact_error"] == error
        assert _system(messages, "compact_boundary") == []
        assert not [e for e in events if isinstance(e, ContextCompaction)]
        if failure == "archive":
            assert archive_attempts
            assert "could not archive dropped rows" in caplog.text
        follow = await _turn(adapter, "Still there after the failure?")
        assert "You said: “Still there after the failure?”" in _replies(follow)[-1]
    finally:
        await adapter.disconnect()


async def test_claude_codes_own_summarizer_when_fluid_compaction_is_off(monkeypatch, tmp_path):
    adapter = await _adapter(monkeypatch, tmp_path, fluid=False)
    try:
        await _history(adapter, reads=2)
        messages = await _turn(adapter, "[script:compact] go on")
        events = await _events(adapter)
        [compaction] = [e for e in events if isinstance(e, ContextCompaction)]
        assert compaction.reason == "cli_auto_compact" and compaction.after_tokens > 0
        # The CLI's summary, read back from its transcript, for a re-seed.
        assert compaction.summary.startswith("This session is being continued")
        assert not [e for e in events if isinstance(e, CompactionCheckpoint)]
        assert len(_system(messages, "compact_boundary")) == 1
        # The summary frame the CLI prints is not a reply or a message.
        assert _replies(messages) == [
            "The conversation was compacted mid-turn and I carried on. You said: “go on”"]
        assert adapter._engine.last_restoration is not None
        follow = await _turn(adapter, "Next?")
        assert "You said: “Next?”" in _replies(follow)[-1]
        calls, results = await _stored_tool_ids(adapter)
        assert len(calls) == len(set(calls)) == 2 and len(results) == 2
    finally:
        await adapter.disconnect()


async def test_a_cli_resumed_after_a_compaction_goes_on_from_the_compacted_conversation(
    monkeypatch, tmp_path, caplog,
):
    caplog.set_level(logging.INFO, logger="fluid.engine.delegated")
    adapter = await _adapter(monkeypatch, tmp_path)
    try:
        await _history(adapter)
        await _turn(adapter, "/compact")
        await _assert_one_fluid_compaction(adapter, "manual", caplog)
        engine = adapter._engine
        cli_session = engine.cli_session_id
        before = len(await engine.store.load_path(engine.session_id))
        # The CLI process goes away (a crash, an app restart): the next turn
        # starts it again with --resume.
        await engine._stop_process(grace=1.0)
        messages = await _turn(adapter, "After the restart.")
        assert engine.cli_session_id == cli_session
        assert "You said: “After the restart.”" in _replies(messages)[-1]
        # Only the new exchange was stored: nothing replayed on resume.
        assert len(await engine.store.load_path(engine.session_id)) == before + 2
        assert len([e for e in await _events(adapter) if isinstance(e, ContextCompaction)]) == 1
    finally:
        await adapter.disconnect()
    log = fake_claude.log_path(tmp_path / "instance").read_text()
    assert '"resumed": true' in log
