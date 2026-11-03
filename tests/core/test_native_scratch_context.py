"""The native leg's ``# Harness scratch`` reminder rides one message per segment.

It used to ride every user message, so history held one more copy each turn
(242 turns: 242 stored copies, 101,156 characters, and 29,403 copies across the
requests). It now goes out when the active path shows the model does not hold
this exact text. "Holds" is read from the stored path, and is true live or
folded, because the compactor carries the newest pinned block onto any fold
(``CompactionConfig.pinned_blocks``). These run the real adapter and NativeLoop.
"""
from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest
from fluid.context.compaction import CompactionConfig
from fluid.engine.native import NativeLoop
from fluid.events import ContextCompaction
from fluid.messages import Message
from fluid.messages.content import Text
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, StreamChunk
from fluid.state import SCRATCH_HEADER
from fluid.store.base import MemoryStore, SessionNode, iter_events, new_id
from fluid.tools.inproc import create_inproc_server, tool
from fluid.tools.registry import ToolRegistry

from app.core import memory_reminder as mr
from scripts.measure_native_scratch_context import fixture_note, measure
from tests.core.test_memory_reminder import FACTS, _adapter, _Recording, _section, _turn
from tests.test_harness_adapter import _collect

NOTE = fixture_note()
LOW = CompactionConfig(trigger_tokens=4_000, keep_recent_turns=1)
LONG = "words " * 400


def _copies(request, note: str = NOTE) -> int:
    return "\n".join((m.text() or "") for m in request.messages).count(note)


async def _scratch_adapter(monkeypatch, *, memory=True, loop=None, note=None):
    state = {"section": _section(*FACTS) if memory else None}
    adapter, loop = await _adapter(monkeypatch, state, loop=loop)
    monkeypatch.setattr(adapter, "_scratch_note", lambda: (note or {"text": NOTE})["text"])
    return adapter, loop


async def _stored(loop) -> list[int]:
    """Pinned copies per user node on the active path."""
    nodes = await loop.store.load_path(loop.session_id)
    return [n.message.pinned_text().count(SCRATCH_HEADER)
            for n in nodes if n.message.role == "user"]


async def _folds(loop) -> list[ContextCompaction]:
    return [e async for e in iter_events(loop.store, loop.session_id, 0)
            if isinstance(e, ContextCompaction) and e.tier >= 2]


def _recording_loop(compaction=None, reply="ok", **kwargs) -> tuple[_Recording, NativeLoop]:
    provider = _Recording(reply)
    return provider, NativeLoop(
        provider=provider, store=kwargs.pop("store", None) or MemoryStore(), model="fake",
        compaction=compaction or CompactionConfig(), **kwargs,
    )


# ---------------------------------------------------------------------------
# Fixed-fixture guards
# ---------------------------------------------------------------------------

def test_the_block_text_is_unchanged_and_within_its_allowance():
    """A prompt-tightening pass may not reword this rule; a growth must be seen."""
    path = NOTE.split("Use ", 1)[1].split(" (absolute", 1)[0]
    assert NOTE == mr.wrap(
        "# Harness scratch\n"
        f"Use {path} (absolute, per-session) for temporary briefs, "
        "artifacts and review evidence; create it only when needed. Never put "
        "harness state or scratch in the working folder's .fluid/. Project "
        ".fluid is for loading skills, agents, commands, plugins and settings "
        "(and host-managed worktrees); explicit skill/plugin authoring is unchanged."
    )
    assert len(NOTE) <= 450


def test_size_and_duplicate_guard_on_the_measured_scenarios():
    numbers = measure(turns=40, folded_turns=30)
    for name in ("steady", "steady_no_memory"):
        steady = numbers[name]
        assert steady["stored_copies"] == 1, name
        assert steady["stored_chars"] == steady["block_chars"], name
        assert steady["requested_copies"] == steady["model_requests"], name
        assert steady["max_copies_in_one_request"] == 1, name
    for name in ("folded", "folded_no_memory"):
        folded = numbers[name]
        assert folded["folds"] > 0, name
        assert folded["stored_copies"] == 1, name
        assert folded["requests_without_scratch"] == 0, name
        assert folded["max_copies_in_one_request"] == 1, name


# ---------------------------------------------------------------------------
# Once per segment
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [True, False])
async def test_plain_turns_send_it_once_and_every_request_shows_it(monkeypatch, memory):
    provider, loop = _recording_loop()
    adapter, loop = await _scratch_adapter(monkeypatch, memory=memory, loop=loop)
    for i in range(5):
        await _turn(adapter, f"turn {i}")
    assert await _stored(loop) == [1, 0, 0, 0, 0]
    assert [_copies(r) for r in provider.requests] == [1] * 5


@pytest.mark.asyncio
async def test_the_pin_covers_exactly_what_the_host_appended(monkeypatch):
    # The owner's own words stay out of the range, even when they quote the block.
    prompt = f"please read this: {NOTE}"
    adapter, loop = await _scratch_adapter(monkeypatch)
    await _turn(adapter, prompt)
    [node] = [n for n in await loop.store.load_path(loop.session_id) if n.message.role == "user"]
    assert node.message.pinned_text() == f"{mr.wrap(_section(*FACTS))}\n\n{NOTE}"
    assert node.message.text().startswith(prompt)
    # Memory reads its own block back out of the shared range.
    assert mr.newest_full_node([node]) == node.node_id

    bare, loop = await _scratch_adapter(monkeypatch, memory=False)
    await _turn(bare, "hello")
    [node] = [n for n in await loop.store.load_path(loop.session_id) if n.message.role == "user"]
    assert node.message.pinned_text() == NOTE


@pytest.mark.asyncio
async def test_a_quoted_copy_is_not_evidence(monkeypatch):
    """Owner text and history from before the fix hold no host-pinned range."""
    _provider, loop = _recording_loop()
    adapter, loop = await _scratch_adapter(monkeypatch, memory=False, loop=loop)
    await loop.store.append(loop.session_id, SessionNode(
        node_id=new_id("node"), parent_id=None,
        message=Message(role="user", content=[Text(f"old turn\n\n{NOTE}")]),
    ))
    await _turn(adapter, "next")
    await _turn(adapter, "again")
    assert await _stored(loop) == [0, 1, 0]


@pytest.mark.asyncio
async def test_new_text_goes_out_and_the_newest_survives_a_fold(monkeypatch):
    note = {"text": NOTE}
    provider, loop = _recording_loop(LOW, reply="reply " * 300)
    adapter, loop = await _scratch_adapter(monkeypatch, loop=loop, note=note)
    await _turn(adapter, f"a {LONG}")
    moved = NOTE.replace("/fixture/", "/moved/")
    note["text"] = moved
    start = len(provider.requests)
    for i in range(8):
        await _turn(adapter, f"b{i} {LONG}")
    assert await _folds(loop)
    assert sum(await _stored(loop)) == 2
    assert all(_copies(r, moved) == 1 for r in provider.requests[start:])
    # The superseded copy may stay on a fold made before the change, never more.
    assert all(_copies(r) <= 1 for r in provider.requests[start:])


# ---------------------------------------------------------------------------
# Compaction keeps it in view
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("memory", [True, False])
async def test_automatic_compaction_keeps_it_in_every_request(monkeypatch, memory):
    provider, loop = _recording_loop(LOW, reply="reply " * 300)
    adapter, loop = await _scratch_adapter(monkeypatch, memory=memory, loop=loop)
    for i in range(10):
        await _turn(adapter, f"turn {i} {LONG}")
    assert await _folds(loop), "the scripted session never digested a turn"
    assert await _stored(loop) == [1] + [0] * 9
    assert [_copies(r) for r in provider.requests] == [1] * len(provider.requests)
    # The fold is the reason: without the pin the digest drops it.
    nodes = await loop.store.load_path(loop.session_id)
    loop._compactor.config = replace(loop._compactor.config, pinned_blocks=None)
    assert "\n".join(m.text() or "" for m in loop._compactor.view(nodes)).count(NOTE) == 0


class _ToolLoop(_Recording):
    """Answers the first turn, then calls a tool ``tool_calls`` times in the next."""

    def __init__(self, tool_calls: int) -> None:
        super().__init__("reply " * 300)
        self.caps = ProviderCaps()
        self.tool_calls = tool_calls
        self.turn_start: int | None = None

    async def stream(self, request):
        last = request.messages[-1].text() or ""
        if "tool turn" in last and self.turn_start is None:
            self.turn_start = len(self.requests)
        if self.turn_start is None or len(self.requests) - self.turn_start >= self.tool_calls:
            async for chunk in super().stream(request):
                yield chunk
            return
        self.requests.append(request)
        yield StreamChunk("message_start", {"usage": {"input_tokens": 5}})
        yield StreamChunk("block_start", {
            "index": 0, "block_type": "tool_use",
            "tool_name": "mcp__t__ping", "tool_use_id": f"t{len(self.requests)}",
        })
        yield StreamChunk("tool_args_delta", {"index": 0, "partial_json": "{}"})
        yield StreamChunk("block_stop", {"index": 0})
        yield StreamChunk("usage", {"usage": {"output_tokens": 5}, "stop_reason": "tool_use"})


def _ping_registry() -> ToolRegistry:
    @tool("ping", "Ping", {"type": "object", "properties": {}})
    async def ping(args: dict) -> dict:
        return {"content": [{"type": "text", "text": "pong " * 1_500}]}

    registry = ToolRegistry()
    registry.register_inproc(create_inproc_server("t", [ping]))
    return registry


@pytest.mark.asyncio
async def test_a_fold_inside_a_turn_keeps_it_on_the_very_next_request(monkeypatch):
    """A mid-turn fold has no new user message to ride: only the pin can keep it."""
    provider = _ToolLoop(tool_calls=6)
    loop = NativeLoop(
        provider=provider, store=MemoryStore(), model="fake", compaction=LOW,
        registry=_ping_registry(), permissions=PermissionEngine("never_ask"),
    )
    adapter, loop = await _scratch_adapter(monkeypatch, memory=False, loop=loop)
    await _turn(adapter, f"{LONG} FIRST-TURN-END")
    await _turn(adapter, "tool turn")
    turn = provider.requests[provider.turn_start:]
    assert len(turn) == 7
    # Tool results are user-role nodes too: the first message holds the only copy.
    stored = await _stored(loop)
    assert stored[0] == 1 and sum(stored) == 1
    assert [_copies(r) for r in provider.requests] == [1] * len(provider.requests)
    def folded(request):
        # By its marker: from render version 3 a fold keeps this opening prompt whole.
        text = "\n".join(m.text() or "" for m in request.messages)
        return "[compacted turn" in text or "[epoch " in text
    assert not folded(turn[0]), "the first turn was folded before the tool loop started"
    assert any(folded(r) for r in turn[1:]), "nothing folded the first turn mid-turn"


@pytest.mark.asyncio
async def test_manual_compaction_keeps_it_without_a_new_copy(monkeypatch):
    provider, loop = _recording_loop(reply="reply " * 300)
    adapter, loop = await _scratch_adapter(monkeypatch, memory=False, loop=loop)
    for i in range(5):
        await _turn(adapter, f"turn {i} {LONG}")
    await _turn(adapter, "/compact")
    assert [e.reason for e in await _folds(loop) if e.reason == "manual_compact"]
    await _turn(adapter, "after")
    assert await _stored(loop) == [1, 0, 0, 0, 0, 0]
    assert _copies(provider.requests[-1]) == 1
    assert f"turn 0 {LONG}" not in "\n".join(
        m.text() or "" for m in provider.requests[-1].messages)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["skipped", "failed"])
async def test_a_compaction_that_folds_nothing_changes_nothing(monkeypatch, outcome):
    provider, loop = _recording_loop()
    adapter, loop = await _scratch_adapter(monkeypatch, loop=loop)
    await _turn(adapter, "only turn")
    if outcome == "failed":
        monkeypatch.setattr(loop._compactor, "compact_now",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no")))
    await _turn(adapter, "/compact")
    assert not [e for e in await _folds(loop) if e.reason == "manual_compact"]
    await _turn(adapter, "after")
    assert await _stored(loop) == [1, 0]
    assert _copies(provider.requests[-1]) == 1


@pytest.mark.asyncio
async def test_a_restart_after_a_fold_neither_loses_nor_repeats_it(monkeypatch):
    """A new loop and adapter read the persisted decisions and the same path."""
    store = MemoryStore()
    _, first = _recording_loop(LOW, reply="reply " * 300, store=store)
    adapter, first = await _scratch_adapter(monkeypatch, memory=False, loop=first)
    for i in range(8):
        await _turn(adapter, f"turn {i} {LONG}")
    assert await _folds(first)

    provider, second = _recording_loop(
        replace(LOW, trigger_tokens=10**9), store=store, session_id=first.session_id,
    )
    woken, second = await _scratch_adapter(monkeypatch, memory=False, loop=second)
    await _turn(woken, "after the restart")
    assert await _stored(second) == [1] + [0] * 8
    assert _copies(provider.requests[-1]) == 1


# ---------------------------------------------------------------------------
# Retry, rewind, adapter resume, model change
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_fresh_adapter_reads_it_back_and_does_not_resend(monkeypatch):
    provider, loop = _recording_loop()
    adapter, loop = await _scratch_adapter(monkeypatch, loop=loop)
    await _turn(adapter, "first")
    woken, _ = await _scratch_adapter(monkeypatch, loop=loop)
    await _turn(woken, "after the wake")
    assert await _stored(loop) == [1, 0]
    assert _copies(provider.requests[-1]) == 1


@pytest.mark.asyncio
async def test_a_rewind_off_its_node_sends_it_again(monkeypatch):
    provider, loop = _recording_loop()
    adapter, loop = await _scratch_adapter(monkeypatch, loop=loop)
    await _turn(adapter, "first")
    await _turn(adapter, "second")
    await loop.store.set_head(loop.session_id, None)
    loop._session.head_node_id = None
    await _turn(adapter, "again")
    assert await _stored(loop) == [1]
    assert _copies(provider.requests[-1]) == 1


@pytest.mark.asyncio
async def test_a_retry_replays_the_request_byte_identically(monkeypatch):
    provider, loop = _recording_loop()
    adapter, loop = await _scratch_adapter(monkeypatch, loop=loop)
    await _turn(adapter, "first")
    head = SessionNode(
        node_id=new_id("node"),
        parent_id=(await loop.store.load_path(loop.session_id))[-1].node_id,
        message=Message(role="user", content=[Text("Continue the unfinished task.")]),
    )
    await loop.store.append(loop.session_id, head)
    before = len(await loop.store.load_path(loop.session_id))
    await adapter.send_resume()
    await _collect(adapter)
    # Only the assistant's answer was added: no note node, no new copy.
    assert len(await loop.store.load_path(loop.session_id)) == before + 1
    assert _copies(provider.requests[-1]) == 1


@pytest.mark.asyncio
async def test_a_retry_with_nothing_held_restores_it_at_the_tail(monkeypatch):
    _provider, loop = _recording_loop()
    adapter, loop = await _scratch_adapter(monkeypatch, memory=False, loop=loop)
    original = f"unfinished from before this fix\n\n{NOTE}"
    await loop.store.append(loop.session_id, SessionNode(
        node_id=new_id("node"), parent_id=None,
        message=Message(role="user", content=[Text(original)]),
    ))
    await adapter.send_resume()
    await _collect(adapter)
    users = [n for n in await loop.store.load_path(loop.session_id) if n.message.role == "user"]
    assert users[0].message.text() == original
    assert users[-1].message.pinned_text() == NOTE == users[-1].message.text()
    # The next turn trusts the restored copy.
    await _turn(adapter, "next")
    assert await _stored(loop) == [0, 1, 0]


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["set_model", "model_attribute", "provider"])
async def test_a_model_change_sends_one_fresh_copy(monkeypatch, change):
    provider, loop = _recording_loop(LOW, reply="reply " * 300)
    adapter, loop = await _scratch_adapter(monkeypatch, loop=loop)
    await _turn(adapter, "first")
    if change == "set_model":
        await adapter.set_model("other-model")
    elif change == "model_attribute":
        loop.model = "other-model"
    else:
        monkeypatch.setattr(provider, "name", "other-provider")
    await _turn(adapter, "second")
    assert await _stored(loop) == [1, 1]
    await _turn(adapter, "third")
    assert await _stored(loop) == [1, 1, 0]
    # Later folds never stack them: only the newest is carried past a fold of both.
    for i in range(8):
        await _turn(adapter, f"turn {i} {LONG}")
    assert await _folds(loop)
    assert all(1 <= _copies(r) <= 2 for r in provider.requests)
    assert _copies(provider.requests[-1]) == 1


@pytest.mark.asyncio
async def test_a_compact_right_after_a_model_change_does_not_use_up_the_fresh_copy(monkeypatch):
    # /compact stores no node, so the copy it was built with never went out.
    _provider, loop = _recording_loop()
    adapter, loop = await _scratch_adapter(monkeypatch, memory=False, loop=loop)
    await _turn(adapter, "first")
    await adapter.set_model("other-model")
    await _turn(adapter, "/compact")
    await _turn(adapter, "after")
    assert await _stored(loop) == [1, 1]
    await _turn(adapter, "later")
    assert await _stored(loop) == [1, 1, 0]


@pytest.mark.asyncio
async def test_a_rejected_submit_leaves_nothing_believed_delivered(monkeypatch):
    _provider, loop = _recording_loop()
    adapter, loop = await _scratch_adapter(monkeypatch, loop=loop)
    await _turn(adapter, "first")
    await adapter.set_model("other-model")
    real = loop.submit

    async def rejected(op):
        raise RuntimeError("a turn is already running")

    monkeypatch.setattr(loop, "submit", rejected)
    with pytest.raises(RuntimeError):
        await adapter.send_query("refused")
    monkeypatch.setattr(loop, "submit", real)
    await _turn(adapter, "accepted")
    assert await _stored(loop) == [1, 1]


# ---------------------------------------------------------------------------
# Doubt means send
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_an_unreadable_path_sends_it_and_the_turn_goes_on(monkeypatch):
    from fluid.engine import native

    provider, loop = _recording_loop()
    adapter, loop = await _scratch_adapter(monkeypatch, loop=loop)
    await _turn(adapter, "first")
    assert await loop.holds_pinned_block(loop.session_id, SCRATCH_HEADER) == NOTE

    def broken(*_args, **_kwargs):
        raise OSError("store is down")

    monkeypatch.setattr(native, "newest_pinned_block", broken)
    assert await loop.holds_pinned_block(loop.session_id, SCRATCH_HEADER) is None
    await _turn(adapter, "second")
    assert await _stored(loop) == [1, 1]
    assert _copies(provider.requests[-1]) == 2


@pytest.mark.asyncio
async def test_a_compactor_that_does_not_pin_it_sends_it_every_turn(monkeypatch):
    _provider, loop = _recording_loop(replace(CompactionConfig(), pinned_blocks=None))
    adapter, loop = await _scratch_adapter(monkeypatch, loop=loop)
    await _turn(adapter, "first")
    await _turn(adapter, "second")
    assert await _stored(loop) == [1, 1]


@pytest.mark.asyncio
async def test_an_engine_that_cannot_answer_means_send(monkeypatch):
    adapter, _loop = await _scratch_adapter(monkeypatch)
    adapter._engine = SimpleNamespace(store=MemoryStore())
    assert await adapter._native_scratch_held() is None
    assert adapter._native_scratch_note() == NOTE


@pytest.mark.asyncio
async def test_the_delegated_leg_is_untouched(monkeypatch):
    adapter, _loop = await _scratch_adapter(monkeypatch, memory=False)
    await _turn(adapter, "first")
    adapter._is_delegated = True
    assert await adapter._native_scratch_held() is None
    # Its note keeps its order and copy, and nothing of it joins the pin.
    assert adapter._turn_context_note() == NOTE
    assert adapter._native_scratch_turn is None
    assert adapter._memory_context_pin(f"x\n\n{NOTE}") is None
