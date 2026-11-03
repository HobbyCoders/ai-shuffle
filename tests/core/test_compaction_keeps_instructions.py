"""Nothing an agent was told is lost across a Claude Code compaction (#996, #755).

The app's real integration (HarnessAdapter, DelegatedLoop, the mod's compaction
route on the bundled bridge) drives ``scripts/devloop/fake_claude``, which holds
hook context the way Claude Code 2.1.316 does: an attachment grouped on the row
before it, kept through a compaction only when that row is handed back by its
handle, and over 10,000 characters saved to a file with a 2 KB preview.

``fake_claude.context_path`` is what the fake's newest model call could read:
every assertion below is about that text, not about what Fluid stored.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from fluid.context.compaction import INSTRUCTIONS_LEAD
from fluid.engine.compact_restore import RestorationSection
from fluid.events import CompactionCheckpoint, ContextCompaction
from fluid.events.model import DelegatedDiagnostic
from fluid.messages.content import ToolResultBlock
from fluid.tools.registry import ToolRegistry

import app.core.provider.harness_adapter as ha
from app.core import claude_compaction
from app.core.provider.events import AssistantMessage, ResultMessage
from app.core.provider.types import SessionOptions
from scripts.devloop import fake_claude

pytestmark = pytest.mark.asyncio

MESSAGE = "Stop using the old table, the owner wants the new one (marker 7f3a)."
BRIEF_TAIL = "Finally: report the migration count (marker b41c)."
LONG_BRIEF = "\n".join(f"Requirement {i}: keep the import stable." for i in range(160)) + "\n" + BRIEF_TAIL


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "ws" / ".git").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    from app.core import guidance_surface

    monkeypatch.setenv(guidance_surface.GUIDANCE_HOME_ENV, str(home))


async def _adapter(monkeypatch, tmp_path: Path, *, fluid: bool = True) -> ha.HarnessAdapter:
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
        env={"CLAUDE_CONFIG_DIR": str(instance / "config")},
        extra={"fluid_registry": ToolRegistry()},
    )
    adapter = ha.HarnessAdapter()
    await adapter.connect(options)
    return adapter


async def _turn(adapter: ha.HarnessAdapter, prompt: str, *, peer: str | None = None) -> list:
    """One turn. ``peer`` is sent as an agent's message as soon as the turn's
    first tool call is visible, while that (slow) tool runs."""
    await adapter.send_query(prompt)
    messages = []
    async for message in adapter.receive_events():
        messages.append(message)
        if peer and isinstance(message, AssistantMessage) and any(
                getattr(b, "name", None) for b in message.content):
            await adapter.steer_peer(peer, sender="coordinator", id="peer-1")
            peer = None
        if isinstance(message, ResultMessage):
            return messages
    raise AssertionError("turn ended without a result")


async def _history(adapter: ha.HarnessAdapter, reads: int = 3) -> None:
    for _ in range(reads):
        await _turn(adapter, "[script:bulk-read] read the big file")


def _seen(tmp_path: Path) -> str:
    return fake_claude.context_path(tmp_path / "instance").read_text(encoding="utf-8")


def _calls(tmp_path: Path) -> list[dict]:
    lines = fake_claude.log_path(tmp_path / "instance").read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]
    return [r for r in rows if r.get("kind") == "model_call" or r.get("event") == "model_call"]


async def _events(adapter: ha.HarnessAdapter) -> list:
    engine = adapter._engine
    return list(await engine.store.events_since(engine.session_id, -1))


@pytest.mark.parametrize("fluid, scenario", [
    (True, "message-then-compact"),  # the mod's answer keeps the row
    (False, "message-then-compact"),  # Claude Code's summarizer keeps no rows
], ids=["fluid", "summarizer"])
async def test_a_message_delivered_just_before_a_compaction_is_still_in_context(
    monkeypatch, tmp_path, fluid, scenario,
):
    """#996's order: a message arrives on a tool result, and the next thing that
    happens is the compaction, with no assistant turn between."""
    adapter = await _adapter(monkeypatch, tmp_path, fluid=fluid)
    try:
        await _history(adapter)
        await _turn(adapter, f"[script:{scenario}] carry on", peer=MESSAGE)
        events = await _events(adapter)
        assert [e for e in events if isinstance(e, ContextCompaction)], "no compaction ran"
        assert MESSAGE in _seen(tmp_path), "the delivered message is gone after the compaction"
    finally:
        await adapter.disconnect()


async def test_a_message_delivered_turns_ago_survives_folding(monkeypatch, tmp_path):
    adapter = await _adapter(monkeypatch, tmp_path)
    try:
        await _turn(adapter, "[script:message-then-compact] first", peer=MESSAGE)
        await _history(adapter, reads=4)
        for _ in range(3):
            await _turn(adapter, "/compact")
            await _history(adapter, reads=2)
        await _turn(adapter, "/compact")
        assert MESSAGE in _seen(tmp_path)
    finally:
        await adapter.disconnect()


async def test_a_long_brief_survives_every_compaction(monkeypatch, tmp_path):
    """With history behind it the brief is folded into a stub, where it was
    clipped at 2,000 characters."""
    adapter = await _adapter(monkeypatch, tmp_path)
    try:
        await _turn(adapter, f"[script:bulk-read] {LONG_BRIEF}")
        await _history(adapter, reads=4)
        for _ in range(3):
            await _turn(adapter, "/compact")
            assert BRIEF_TAIL in _seen(tmp_path), "the brief's end was clipped away"
            await _history(adapter, reads=2)
    finally:
        await adapter.disconnect()


async def test_a_message_and_brief_survive_claude_codes_own_summarizer(monkeypatch, tmp_path):
    """No Fluid answer: the summarizer rewrites what it folds and clips the requests
    it quotes. Fluid cannot tell what it kept, so both are repeated: never gone,
    and one copy beside the original at most, however many compactions run."""
    adapter = await _adapter(monkeypatch, tmp_path, fluid=False)
    try:
        await _turn(adapter, f"[script:message-then-compact] {LONG_BRIEF}", peer=MESSAGE)
        for _ in range(3):
            await _history(adapter, reads=4)
            await _turn(adapter, "/compact")
            await _turn(adapter, "carry on")
            seen = _seen(tmp_path)
            assert 1 <= seen.count(MESSAGE) <= 2, "the delivered message was lost to the summary"
            assert 1 <= seen.count(BRIEF_TAIL) <= 2, "the brief was lost to the summary"
    finally:
        await adapter.disconnect()


async def test_a_delivered_message_is_stored_on_its_tool_result(monkeypatch, tmp_path):
    """Every later render reads it from there, as on the native loop."""
    adapter = await _adapter(monkeypatch, tmp_path)
    try:
        await _turn(adapter, "[script:message-then-compact] carry on", peer=MESSAGE)
        engine = adapter._engine
        nodes = await engine.store.load_path(engine.session_id)
        delivered = [steer for node in nodes for block in node.message.content
                     if isinstance(block, ToolResultBlock) for steer in block.delivered_steers]
        assert [(s.text, s.sender) for s in delivered] == [(MESSAGE, "coordinator")]
    finally:
        await adapter.disconnect()


async def test_a_note_too_long_for_one_answer_reaches_the_model_in_one_long_turn(
    monkeypatch, tmp_path,
):
    """#755: over 10,000 characters Claude Code shows a 2 KB preview, and the note
    was credited anyway. Here all of it arrives, once, and it ends: one
    compaction, no repeat, and the size stops growing when it is out."""
    head, tail = "memory head (marker c21d)", "memory tail (marker 9e77)"
    section = "\n".join([head, *(f"- fact {i}: keep it stable" for i in range(800)), tail])
    plain_note = ha.HarnessAdapter._compact_note

    def with_long_section(self):
        return [*plain_note(self), RestorationSection(
            name="long", text=section, priority=3, cuttable=True,
        )]

    monkeypatch.setattr(ha.HarnessAdapter, "_compact_note", with_long_section)
    adapter = await _adapter(monkeypatch, tmp_path)
    try:
        await _history(adapter)
        await _turn(adapter, "[script:compact-then-tools] carry on")
        seen = _seen(tmp_path)
        assert "<persisted-output>" not in seen, "the model got a preview"
        assert seen.count(head) == 1 and seen.count(tail) == 1
        assert seen.count("- fact 400:") == 1
        events = await _events(adapter)
        assert len([e for e in events if isinstance(e, ContextCompaction)]) == 1
        restored = [e for e in events if isinstance(e, DelegatedDiagnostic)
                    and e.kind == "compaction_restoration"]
        assert 2 <= len(restored) <= 4
        long = next(s for s in restored[-1].data["sections"] if s["name"] == "long")
        assert long["status"] == "delivered" and restored[-1].data["complete"] is True
        assert len({r.data["compaction_id"] for r in restored}) == 1
    finally:
        await adapter.disconnect()


async def test_every_fluid_answered_boundary_gets_its_checkpoint_under_one_id(
    monkeypatch, tmp_path, caplog,
):
    """A real boundary's logical parent is almost never a row the mod posted, so
    matching on it lost the checkpoint and the stable id (#755)."""
    caplog.set_level(logging.INFO, logger="fluid.engine.delegated")
    adapter = await _adapter(monkeypatch, tmp_path)
    try:
        await _history(adapter)
        await _turn(adapter, "[script:compact] carry on")
        events = await _events(adapter)
        [compaction] = [e for e in events if isinstance(e, ContextCompaction)]
        [checkpoint] = [e for e in events if isinstance(e, CompactionCheckpoint)]
        assert checkpoint.compaction_id == compaction.compaction_id
        restored = [e for e in events if isinstance(e, DelegatedDiagnostic)
                    and e.kind == "compaction_restoration"]
        assert restored and {r.data["compaction_id"] for r in restored} == {compaction.compaction_id}
        assert "names none" not in caplog.text
        assert "summary was not in its transcript" not in caplog.text
    finally:
        await adapter.disconnect()


async def test_repeating_what_it_was_told_does_not_add_up(monkeypatch, tmp_path):
    """A repeat costs a flat amount. A note from an earlier summarizer compaction
    can stay on a kept row beside the fold's copy, so two copies at most, however
    many compactions run. What still grows here is the fake's kept history, and
    as much on the code before this fix (see the PR's table). ``-s`` prints it."""
    adapter = await _adapter(monkeypatch, tmp_path)
    try:
        await _turn(adapter, f"[script:message-then-compact] {LONG_BRIEF}", peer=MESSAGE)
        sizes, counts = [], []
        for _ in range(5):
            await _history(adapter, reads=2)
            await _turn(adapter, "/compact")
            await _turn(adapter, "Still there?")
            seen = _seen(tmp_path)
            sizes.append(len(seen))
            counts.append((seen.count(MESSAGE), seen.count(BRIEF_TAIL)))
            assert 1 <= seen.count(MESSAGE) <= 2 and 1 <= seen.count(BRIEF_TAIL) <= 2
            assert seen.count(INSTRUCTIONS_LEAD) <= 1
        print("context chars after each compaction:", sizes, "copies:", counts)
        restored = [e for e in await _events(adapter) if isinstance(e, DelegatedDiagnostic)
                    and e.kind == "compaction_restoration"]
        compactions = [e for e in await _events(adapter) if isinstance(e, ContextCompaction)]
        assert len(compactions) >= 5
        assert len(restored) <= 3 * len(compactions), "restoration is looping"
    finally:
        await adapter.disconnect()


def test_a_re_seed_restores_a_repeated_instruction_in_its_place():
    """The rows show one "Do not deploy". The later one is what stands, so it comes
    back with what came between, in order. Dedupe by text would have dropped both."""
    import re

    from fluid.messages.content import SteerDelivery, Text
    from fluid.store.base import SessionNode

    from app.core.provider.fluid_transcript import _with_what_it_was_told
    from fluid.messages import Message

    said = ["Do not deploy", "You may deploy now", "Do not deploy"]
    steers = [SteerDelivery(id=f"p{i}", text=text, origin="peer", sender="lead")
              for i, text in enumerate(said)]
    nodes = [SessionNode(node_id="n0", parent_id=None,
                         message=Message(role="user", content=[Text("the brief")]))]
    for i, steer in enumerate(steers):
        nodes.append(SessionNode(
            node_id=f"n{i + 1}", parent_id=None,
            message=Message(role="user", content=[ToolResultBlock(
                tool_use_id=f"t{i}", content=[Text("x")], delivered_steers=[steer],
            )]),
        ))
    entries = [
        {"type": "user", "message": {"role": "user", "content": "the brief"}},
        {"type": "user", "message": {"role": "user", "content": steers[0].render()}},
    ]
    out = _with_what_it_was_told("sid", entries, nodes)
    notes = [e["message"]["content"] for e in out if INSTRUCTIONS_LEAD in str(e["message"]["content"])]
    assert len(notes) == 1
    assert re.findall("|".join(said[:2]), notes[0]) == said[1:]
    assert "the brief" not in notes[0]  # it still shows


@pytest.mark.asyncio
async def test_summary_pin_survives_the_re_seed_replay(tmp_path):
    """What a native checkpoint stored comes back through the re-seed's replay of
    its event, so a re-seed over a window shows the same checkpoint."""
    from fluid.context.compaction import CompactionConfig, Compactor, Promotion
    from fluid.messages import Message
    from fluid.messages.content import SteerDelivery, Text
    from fluid.store.base import SessionNode

    pin = {"brief": ["n0", "the brief"], "messages": [["n1", "a kept rule"]], "left_out": 0}
    nodes = [SessionNode(node_id=f"n{i}", parent_id=None if i == 0 else f"n{i - 1}",
                         message=Message(role="user", content=[Text("x" * 500)]))
             for i in range(4)]
    compactor = Compactor(config=CompactionConfig())
    compactor.rehydrate([Promotion(
        tier=4, reason="summary", summary_text="checkpoint", spans=(("n0", "n2", "summary"),),
        summary_pin=pin,
    )])
    text = str(compactor.view(nodes)[0].content[0].text)
    assert "a kept rule" in text and "the brief" in text
