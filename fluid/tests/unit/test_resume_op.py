"""Resume — re-run the agent loop from the session head, appending nothing.

A turn that dies on an ErrorEvent (the empty-completion cap, a 400, a dropped
connection) leaves the head on a user node the model never answered: either the
original text prompt or the tool results it was about to read. Resume picks the
loop back up from that exact node, so the retry sends the same history the
failed call saw and the transcript gains only the answer.
"""

import asyncio
from collections.abc import AsyncIterator

import pytest

from fluid.engine import native
from fluid.engine.native import NativeLoop
from fluid.events import (
    ErrorEvent,
    Resume,
    TurnComplete,
    TurnStarted,
    UserTurn,
)
from fluid.permissions.engine import PermissionEngine
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore
from fluid.store.jsonl import JsonlStore
from fluid.tools.registry import ToolRegistry

from tests.unit.test_empty_completion_retry import (
    ScriptedProvider,
    _empty,
    _text,
    _tool_call,
    _weather_server,
)

NOTHING_TO_RESUME = "nothing to resume: the last message is already an assistant reply"


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """The empty-completion backoff is not what these tests are about."""
    monkeypatch.setattr(native, "_empty_retry_delay", lambda attempt: 0.0)


def _bad_request() -> list[StreamChunk]:
    return [
        StreamChunk("message_start", {"usage": {"input_tokens": 10}}),
        StreamChunk("error", {"message": "status 400: invalid request",
                              "retriable": False}),
    ]


def _rescript(provider: ScriptedProvider, scripts: list[list[StreamChunk]]) -> None:
    """Point the fake provider at a new script and forget the old call count."""
    provider.scripts = scripts
    provider.calls = 0


async def _turn(loop: NativeLoop, op) -> None:
    await loop.submit(op)
    await asyncio.wait_for(loop.wait_turn(), timeout=10)


def _events_since(loop: NativeLoop, sid: str, mark: int) -> list:
    return loop.bus.log(sid)[mark:]


# ─── (a) resume from tool results, over the persisted store ──────────────────


async def test_resume_continues_from_a_tool_result_head(tmp_path) -> None:
    """The retry cap gives up with the tool results at the head; Resume answers
    them. Runs on the JSONL store so ``load_path`` is exercised against real
    persistence, not just the in-memory dict."""
    store = JsonlStore(tmp_path / "sessions")
    sid = (await store.create_session()).session_id
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    # One tool call, then nothing but empties: the last script repeats, so the
    # loop burns every retry and ends the turn on an ErrorEvent.
    provider = ScriptedProvider([
        _tool_call("t1", "mcp__wx__get_weather", '{"city": "Paris"}'),
        _empty(),
    ])
    loop = NativeLoop(
        provider=provider, store=store, model="stealth/ox-alpha", session_id=sid,
        registry=registry, permissions=PermissionEngine("never_ask"),
    )

    await _turn(loop, UserTurn(content="weather in Paris?"))
    errors = [e for e in loop.bus.log(sid) if isinstance(e, ErrorEvent)]
    assert len(errors) == 1 and "giving up" in errors[0].message

    before = await store.load_path(sid)
    head = before[-1]
    assert head.message.role == "user"  # the tool results, unanswered
    assert [b.tool_use_id for b in head.message.content] == ["t1"]

    # A fresh store instance would replay the same log; the point of this test
    # is that Resume reads the head back through load_path rather than from any
    # in-process turn state.
    mark = len(loop.bus.log(sid))
    _rescript(provider, [_text("It is sunny in Paris.")])
    from unittest.mock import AsyncMock
    loop._environment_reminder = AsyncMock(return_value="Current time: 2026-11-09T18:00:00+00:00 (UTC)")
    original = head.message.to_dict()
    await _turn(loop, Resume())

    events = _events_since(loop, sid, mark)
    started = [e for e in events if isinstance(e, TurnStarted)]
    assert len(started) == 1
    assert started[0].node_id == head.node_id
    assert any(isinstance(e, TurnComplete) for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)
    assert provider.calls == 1
    request = provider.requests[-1]
    assert [m.role for m in request.messages] == [n.message.role for n in before]
    assert request.messages[0].text() == "weather in Paris?\n\nCurrent time: 2026-11-09T18:00:00+00:00 (UTC)"
    assert [m.to_dict() for m in request.messages[1:]] == [n.message.to_dict() for n in before[1:]]
    assert request.messages[-1].to_dict() == original
    assert head.message.to_dict() == original

    after = await store.load_path(sid)
    assert len(after) == len(before) + 1  # only the answer was written
    assert [n.node_id for n in after[:-1]] == [n.node_id for n in before]
    answer = after[-1]
    assert answer.message.role == "assistant"
    assert answer.parent_id == head.node_id
    assert answer.message.text() == "It is sunny in Paris."

    # Same story on disk: a store that reloads the log sees one added node.
    reloaded = JsonlStore(tmp_path / "sessions")
    replayed = await reloaded.load_path(sid)
    assert [n.node_id for n in replayed] == [n.node_id for n in after]


# ─── (b) nothing to resume ───────────────────────────────────────────────────


async def test_resume_on_an_assistant_head_reports_nothing_to_resume() -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    provider = ScriptedProvider([_text("all done")])
    loop = NativeLoop(
        provider=provider, store=store, model="stealth/ox-alpha", session_id=sid,
    )

    await _turn(loop, UserTurn(content="go"))
    before = await store.load_path(sid)
    assert before[-1].message.role == "assistant"

    mark = len(loop.bus.log(sid))
    await _turn(loop, Resume())

    events = _events_since(loop, sid, mark)
    started = [e for e in events if isinstance(e, TurnStarted)]
    assert len(started) == 1
    assert started[0].node_id == before[-1].node_id
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1
    assert errors[0].message == NOTHING_TO_RESUME
    assert errors[0].retriable is False
    assert not any(isinstance(e, TurnComplete) for e in events)

    assert provider.calls == 1  # the model was never asked again
    after = await store.load_path(sid)
    assert [n.node_id for n in after] == [n.node_id for n in before]


async def test_resume_on_an_empty_session_reports_nothing_to_resume() -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    provider = ScriptedProvider([_text("unreachable")])
    loop = NativeLoop(
        provider=provider, store=store, model="stealth/ox-alpha", session_id=sid,
    )

    await _turn(loop, Resume())

    events = loop.bus.log(sid)
    assert [e.node_id for e in events if isinstance(e, TurnStarted)] == [""]
    errors = [e for e in events if isinstance(e, ErrorEvent)]
    assert len(errors) == 1 and errors[0].message == NOTHING_TO_RESUME
    assert provider.calls == 0
    assert await store.load_path(sid) == []


# ─── (c) one turn at a time ──────────────────────────────────────────────────


class BlockingProvider:
    """Parks inside the stream until released."""

    name = "openrouter"
    caps = ProviderCaps()

    def __init__(self) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.entered.set()
        await self.release.wait()
        for chunk in _text("finally"):
            yield chunk


async def test_resume_during_a_running_turn_raises() -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    provider = BlockingProvider()
    loop = NativeLoop(
        provider=provider, store=store, model="stealth/ox-alpha", session_id=sid,
    )

    await loop.submit(UserTurn(content="go"))
    await asyncio.wait_for(provider.entered.wait(), timeout=5)
    with pytest.raises(RuntimeError, match="a turn is already running"):
        await loop.submit(Resume())

    provider.release.set()
    await asyncio.wait_for(loop.wait_turn(), timeout=10)
    assert (await store.load_path(sid))[-1].message.role == "assistant"


# ─── (d) resume from a text prompt the first call never answered ─────────────


async def test_resume_after_a_first_call_failure_answers_the_prompt() -> None:
    store = MemoryStore()
    sid = (await store.create_session()).session_id
    provider = ScriptedProvider([_bad_request()])
    loop = NativeLoop(
        provider=provider, store=store, model="stealth/ox-alpha", session_id=sid,
    )

    await _turn(loop, UserTurn(content="what is 2 + 2?"))
    errors = [e for e in loop.bus.log(sid) if isinstance(e, ErrorEvent)]
    assert len(errors) == 1 and "invalid request" in errors[0].message

    before = await store.load_path(sid)
    assert len(before) == 1
    prompt_node = before[0]
    assert prompt_node.message.role == "user"

    mark = len(loop.bus.log(sid))
    _rescript(provider, [_text("4")])
    from unittest.mock import AsyncMock
    clock = "Current time: 2026-11-09T18:00:00+00:00 (UTC)"
    overhead = loop._context_overhead_tokens()
    loop._environment_reminder = AsyncMock(return_value=clock)
    sent_overheads = []
    stream = provider.stream
    async def observe(request):
        sent_overheads.append(loop._context_overhead_tokens())
        async for chunk in stream(request):
            yield chunk
    provider.stream = observe
    await _turn(loop, Resume())

    events = _events_since(loop, sid, mark)
    started = [e for e in events if isinstance(e, TurnStarted)]
    assert len(started) == 1 and started[0].node_id == prompt_node.node_id
    assert any(isinstance(e, TurnComplete) for e in events)
    assert not any(isinstance(e, ErrorEvent) for e in events)

    after = await store.load_path(sid)
    assert len(after) == 2
    assert after[0].node_id == prompt_node.node_id
    assert after[1].parent_id == prompt_node.node_id
    assert after[1].message.text() == "4"
    # The resumed call sees the same user, with a fresh request-local clock.
    assert provider.calls == 1
    assert [m.role for m in provider.requests[-1].messages] == ["user"]
    assert provider.requests[-1].messages[0].text() == "what is 2 + 2?\n\n" + clock
    assert after[0].message.text() == "what is 2 + 2?"
    from fluid.context.trim import estimate_tokens_of_text
    assert sent_overheads == [overhead + estimate_tokens_of_text("\n\n" + clock)]
    assert loop._context_overhead_tokens() == overhead
    assert loop._resume_environment is loop._resume_anchor is None

    next_clock = "Current time: 2026-11-09T19:00:00+00:00 (UTC)"
    loop._environment_reminder = AsyncMock(return_value=next_clock)
    await _turn(loop, UserTurn(content="a new question"))
    assert provider.requests[-1].messages[-1].text() == "a new question" + next_clock
    assert not any(clock in m.text() for m in provider.requests[-1].messages)
    assert loop._context_overhead_tokens() == overhead

@pytest.mark.parametrize("duplicate", [False, True])
@pytest.mark.parametrize("retry", [False, True])
async def test_resume_clock_is_an_append_only_prefix_across_two_tool_calls(duplicate, retry):
    from unittest.mock import AsyncMock

    store = MemoryStore()
    sid = (await store.create_session()).session_id
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    provider = ScriptedProvider([_text("prior answer"), _bad_request()] if duplicate else [_bad_request()])
    loop = NativeLoop(provider=provider, store=store, model="stealth/ox-alpha", session_id=sid,
                      registry=registry, permissions=PermissionEngine("never_ask"))
    if duplicate:
        await _turn(loop, UserTurn(content="question"))
    await _turn(loop, UserTurn(content="question"))
    before = await store.load_path(sid)
    anchor_index = len(before) - 1
    clock = "Current time: 2026-11-09T18:00:00+00:00 (UTC)"
    loop._environment_reminder = AsyncMock(return_value=clock)
    scripts = [_tool_call("t1", "mcp__wx__get_weather", '{"city":"Paris"}'),
               _tool_call("t2", "mcp__wx__get_weather", '{"city":"London"}'), _text("done")]
    if retry:
        scripts.insert(0, _empty())
    _rescript(provider, scripts)
    mark = len(provider.requests)
    await _turn(loop, Resume())
    requests = provider.requests[mark:]
    assert len(requests) == len(scripts)
    for request in requests:
        assert [i for i, message in enumerate(request.messages) if clock in message.text()] == [anchor_index]
        assert request.messages[anchor_index].text().count(clock) == 1
    for previous, following in zip(requests, requests[1:]):
        assert [m.to_dict() for m in following.messages[:len(previous.messages)]] == [m.to_dict() for m in previous.messages]
    after = await store.load_path(sid)
    assert [n.message.to_dict() for n in after[:len(before)]] == [n.message.to_dict() for n in before]
    assert all(clock not in n.message.text() for n in after)
    assert loop._resume_environment is loop._resume_anchor is None

async def test_resume_compaction_drops_even_a_single_node_anchor_and_its_overhead(monkeypatch):
    from unittest.mock import AsyncMock
    from fluid.context.compaction import Promotion
    from fluid.context.trim import estimate_tokens_of_text

    store = MemoryStore()
    sid = (await store.create_session()).session_id
    registry = ToolRegistry()
    registry.register_inproc(_weather_server())
    provider = ScriptedProvider([_bad_request()])
    loop = NativeLoop(provider=provider, store=store, model="stealth/ox-alpha", session_id=sid,
                      registry=registry, permissions=PermissionEngine("never_ask"))
    await _turn(loop, UserTurn(content="question"))
    first = (await store.load_path(sid))[0]
    clock = "Current time: 2026-11-09T18:00:00+00:00 (UTC)"
    loop._environment_reminder = AsyncMock(return_value=clock)
    _rescript(provider, [_tool_call("t1", "mcp__wx__get_weather", '{"city":"Paris"}'),
                         _tool_call("t2", "mcp__wx__get_weather", '{"city":"London"}'), _text("done")])
    context_path = loop._context_path
    folded = False
    async def compact_after_first_call(*args, **kwargs):
        nonlocal folded
        nodes = await context_path(*args, **kwargs)
        if provider.calls == 1 and not folded:
            folded = True
            loop._compactor.rehydrate([Promotion(tier=4,
                spans=((first.node_id, first.node_id, "summary"),), summary_text="A compacted input")])
        return nodes
    monkeypatch.setattr(loop, "_context_path", compact_after_first_call)
    overheads = []
    stream = provider.stream
    async def observe(request):
        note = loop._resume_environment
        active = loop._context_overhead_tokens()
        loop._resume_environment = None
        base = loop._context_overhead_tokens()
        loop._resume_environment = note
        overheads.append(active - base)
        async for chunk in stream(request):
            yield chunk
    provider.stream = observe
    mark = len(provider.requests)
    await _turn(loop, Resume())
    requests = provider.requests[mark:]
    assert folded and len(requests) == 3
    assert [sum(clock in m.text() for m in r.messages) for r in requests] == [1, 0, 0]
    assert "[checkpoint summary" in requests[1].messages[0].text()
    assert overheads == [estimate_tokens_of_text("\n\n" + clock), 0, 0]
    assert loop._resume_environment is loop._resume_anchor is None
    assert all(clock not in n.message.text() for n in await store.load_path(sid))
    # Ordinary input after retirement gets its normal fresh note, not the old clock.
    next_clock = "Current time: 2026-11-09T19:00:00+00:00 (UTC)"
    loop._environment_reminder = AsyncMock(return_value=next_clock)
    await _turn(loop, UserTurn(content="next question"))
    assert provider.requests[-1].messages[-1].text() == "next question" + next_clock
    assert not any(clock in m.text() for m in provider.requests[-1].messages)
@pytest.mark.parametrize("exit_kind", ["error", "cancel", "blocked_hook", "hook_error"])
async def test_resume_anchor_is_cleared_on_all_early_exits(monkeypatch, exit_kind):
    from unittest.mock import AsyncMock

    store = MemoryStore()
    sid = (await store.create_session()).session_id
    provider = ScriptedProvider([_bad_request()])
    loop = NativeLoop(provider=provider, store=store, model="stealth/ox-alpha", session_id=sid)
    await _turn(loop, UserTurn(content="question"))
    clock = "Current time: old resume clock"
    loop._environment_reminder = AsyncMock(return_value=clock)
    baseline = loop._context_overhead_tokens()
    entered = asyncio.Event()
    async def fail_or_wait(*args):
        assert loop._resume_anchor is not None and loop._resume_environment == clock
        entered.set()
        if exit_kind == "error":
            raise RuntimeError("fixture failure")
        await asyncio.Event().wait()
    with monkeypatch.context() as patch:
        if exit_kind in {"blocked_hook", "hook_error"}:
            patch.setattr(loop, "_turn_start_hook", AsyncMock(
                return_value=(True, None), side_effect=RuntimeError("fixture failure") if exit_kind == "hook_error" else None))
        else:
            patch.setattr(loop, "_start_and_loop", fail_or_wait)
        await loop.submit(Resume())
        if exit_kind == "cancel":
            await asyncio.wait_for(entered.wait(), 5)
            loop._turn_task.cancel()
        if exit_kind in {"error", "hook_error"}:
            with pytest.raises(RuntimeError, match="fixture failure"):
                await loop.wait_turn()
        elif exit_kind == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await loop.wait_turn()
        else:
            await loop.wait_turn()
    assert loop._resume_environment is loop._resume_anchor is None
    assert loop._context_overhead_tokens() == baseline
    assert all(clock not in n.message.text() for n in await store.load_path(sid))
    next_clock = "Current time: next ordinary turn"
    loop._environment_reminder = AsyncMock(return_value=next_clock)
    await _turn(loop, UserTurn(content="next question"))
    assert provider.requests[-1].messages[-1].text() == "next question" + next_clock
    assert not any(clock in m.text() for m in provider.requests[-1].messages)

@pytest.mark.parametrize("rewrite", ["fold", "window", "escalation"])
def test_resume_admission_releases_a_removed_anchor_before_refusing(monkeypatch, rewrite):
    from fluid.context.compaction import Promotion
    from fluid.context.trim import estimate_tokens_of_text
    from fluid.messages import Message, Text
    from fluid.store.base import SessionNode

    store = MemoryStore()
    loop = NativeLoop(provider=ScriptedProvider([]), store=store, model="stealth/ox-alpha", max_tokens=1)
    anchor = SessionNode("anchor", None, Message(role="user", content=[Text("question")]))
    following = SessionNode("following", anchor.node_id, Message(role="user", content=[Text("new input")]))
    nodes = [anchor, following]
    fold = Promotion(tier=4, spans=((anchor.node_id, anchor.node_id, "summary"),), summary_text="Folded input")
    if rewrite == "fold":
        loop._compactor.rehydrate([fold])
    elif rewrite == "window":
        nodes = [following]
    else:
        def escalate(*args, **kwargs):
            loop._compactor.rehydrate([fold])
            return [fold]
        monkeypatch.setattr(loop._compactor, "escalate", escalate)
    loop._resume_anchor = anchor.node_id
    loop._resume_environment = "Current time: " + "x" * 4000
    reserved = estimate_tokens_of_text("\n\n" + loop._resume_environment)
    overhead = loop._context_overhead_tokens()
    # Both the live view and its single-node replacement fit without the
    # retired clock, but neither fits with that stale reservation.
    loop.context_window = max(400, loop._compactor.estimate_view_tokens(nodes, overhead - reserved) + 10)
    promotions, refusal = loop._admission_promotions(nodes, overhead_tokens=overhead,
                                                    measured=None, measured_current=False)
    assert refusal is None
    assert len(promotions) == (rewrite == "escalation")
    assert loop._resume_environment is loop._resume_anchor is None
    assert loop._context_overhead_tokens() == overhead - reserved
