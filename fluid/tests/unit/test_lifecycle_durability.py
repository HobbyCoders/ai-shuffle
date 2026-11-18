"""Lifecycle writes that fail must stay retryable without being written twice
(issue #1769 case 1), a failed replay read must not count as rehydrated, the
JSONL event log fails closed, and a handoff's model call is billed,
interruptible, admitted one at a time, bounded at close and sees which tools
the session used without their arguments or output."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import replace

import fluid.engine.background as background_mod
import fluid.engine.native as native_mod
import fluid.store.jsonl as jsonl_mod
import pytest
from fluid.engine.native import NativeLoop, _clip, _handoff_transcript
from fluid.events import (
    BackgroundTaskFinished,
    Handoff,
    HandoffCreated,
    Interrupt,
    SessionWoken,
    SubagentEvent,
    Usage,
    UserTurn,
)
from fluid.messages import Message, Text, ToolResultBlock, ToolUseBlock
from fluid.providers.base import ProviderCaps, ProviderRequest, StreamChunk
from fluid.store.base import MemoryStore, SessionNode
from fluid.store.jsonl import JsonlStore
from fluid.tools.registry import BackgroundTaskReceipt

from tests.unit.test_background_rehydrate import _loop, _rehydrate, _store_with, _sub
from tests.unit.test_tool_loop import ScriptedProvider, _text

SID = "parent"
SECRET = "sk-test-SECRET-0123456789"


class FailOnce(MemoryStore):
    """Fails the first event matching ``predicate``.

    ``mode`` says how: ``refuse`` raises before committing, ``commit_then_raise``
    commits and then raises (a lost acknowledgement), and ``hang_after_commit``
    / ``hang_before_commit`` stall until cancelled, after or before the commit.
    """

    def __init__(self, predicate, mode: str = "refuse") -> None:
        super().__init__()
        self.predicate = predicate
        self.mode = mode
        self.failed = 0
        self.entered = asyncio.Event()

    async def append_event(self, session_id, event):
        if self.failed or not self.predicate(event):
            await super().append_event(session_id, event)
            return
        self.failed += 1
        self.entered.set()
        if self.mode == "refuse":
            raise OSError("store unavailable")
        if self.mode == "commit_then_raise":
            await super().append_event(session_id, event)
            raise OSError("commit acknowledgement lost")
        if self.mode == "hang_after_commit":
            await super().append_event(session_id, event)
        await asyncio.sleep(3600)


def _is_terminal(task_id: str):
    return lambda e: (isinstance(e, SubagentEvent) and e.task_id == task_id
                      and e.kind in {"finished", "failed", "cancelled"})


def _subagent(events, task_id: str, kind: str) -> list[SubagentEvent]:
    return [e for e in events if isinstance(e, SubagentEvent)
            and e.task_id == task_id and e.kind == kind]


async def _stored(store, sid=SID):
    return await store.events_since(sid, 0)


async def _woken(store) -> list[tuple[str, ...]]:
    return [e.task_ids for e in await _stored(store) if isinstance(e, SessionWoken)]


# ─── #1769 case 1: a terminal write that fails ──────────────────────────────


async def test_refused_terminal_write_stays_retryable_and_wakes_once() -> None:
    store = FailOnce(_is_terminal("t"))
    await store.create_session(SID)
    loop = _loop(store, provider=ScriptedProvider([_text("picked it up")]))
    await loop._background_task_started(BackgroundTaskReceipt(task_id="t"))
    done = BackgroundTaskFinished(task_id="t", status="finished", summary="42", wake=True)

    with pytest.raises(OSError):
        await loop.submit(done)

    # Nothing in memory claims the task ended.
    assert "t" in loop._background_pending
    assert "t" not in loop._background_terminal
    assert "t" not in loop._background_completions

    await loop.submit(done)  # the host retries; this write lands
    await loop.wait_turn()

    assert len(_subagent(await _stored(store), "t", "finished")) == 1
    assert await _woken(store) == [("t",)]
    await loop.aclose()


async def test_terminal_that_committed_before_its_write_raised_is_not_written_again() -> None:
    store = FailOnce(_is_terminal("t"), mode="commit_then_raise")
    await store.create_session(SID)
    loop = _loop(store, provider=ScriptedProvider([_text("picked it up")]))
    await loop._background_task_started(BackgroundTaskReceipt(task_id="t"))
    done = BackgroundTaskFinished(task_id="t", status="finished", summary="42", wake=True)

    with pytest.raises(OSError):
        await loop.submit(done)
    assert "t" in loop._background_pending  # unknown outcome: still retryable

    await loop.submit(done)
    await loop.wait_turn()

    assert len(_subagent(await _stored(store), "t", "finished")) == 1
    assert len(_subagent(loop.bus.log(SID), "t", "finished")) == 1
    assert "t" in loop._background_terminal and loop._background_uncertain == {}
    assert await _woken(store) == [("t",)]
    await loop.aclose()


@pytest.mark.parametrize("mode", ["hang_after_commit", "hang_before_commit"])
async def test_terminal_write_cancelled_in_flight_is_stored_once(mode) -> None:
    store = FailOnce(_is_terminal("t"), mode=mode)
    await store.create_session(SID)
    loop = _loop(store, provider=ScriptedProvider([_text("picked it up")]))
    await loop._background_task_started(BackgroundTaskReceipt(task_id="t"))
    done = BackgroundTaskFinished(task_id="t", status="finished", wake=True)

    first = asyncio.create_task(loop.submit(done))
    await asyncio.wait_for(store.entered.wait(), 5)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    assert "t" in loop._background_pending

    await loop.submit(done)
    await loop.wait_turn()

    assert len(_subagent(await _stored(store), "t", "finished")) == 1
    assert await _woken(store) == [("t",)]
    await loop.aclose()


@pytest.mark.parametrize(("stored", "retried"), [
    (("cancelled", False), ("finished", True)),
    (("finished", True), ("cancelled", False)),
])
async def test_a_retry_applies_the_completion_that_was_stored(stored, retried) -> None:
    store = FailOnce(_is_terminal("t"), mode="commit_then_raise")
    await store.create_session(SID)
    loop = _loop(store, provider=ScriptedProvider([_text("picked it up")]))
    await loop._background_task_started(BackgroundTaskReceipt(task_id="t"))

    with pytest.raises(OSError):
        await loop.submit(BackgroundTaskFinished(
            task_id="t", status=stored[0], summary="stored one", wake=stored[1]))
    await loop.submit(BackgroundTaskFinished(
        task_id="t", status=retried[0], summary="retried one", wake=retried[1]))
    await loop.wait_turn()

    terminals = [e for e in await _stored(store) if _is_terminal("t")(e)]
    assert [(e.kind, e.wake) for e in terminals] == [stored]
    assert "t" in loop._background_terminal and "t" not in loop._background_pending
    # The wake follows the stored event, not the payload the retry submitted.
    assert await _woken(store) == ([("t",)] if stored[1] else [])
    await loop.aclose()


async def test_a_stored_terminal_from_another_write_is_not_taken_for_this_one() -> None:
    store = FailOnce(_is_terminal("t"))
    await store.create_session(SID)
    loop = _loop(store)
    await loop._background_task_started(BackgroundTaskReceipt(task_id="t"))
    done = BackgroundTaskFinished(task_id="t", status="failed", worker_session_id="w2",
                                  wake=False)
    with pytest.raises(OSError):
        await loop.submit(done)
    # Meanwhile a terminal for the same task id from a different worker lands.
    foreign = replace(_sub("t", "finished", worker_session_id="w1", wake=False),
                      seq=loop.bus.last_seq(SID) + 1)
    loop.bus.seed(SID, foreign.seq)
    await MemoryStore.append_event(store, SID, foreign)

    await loop.submit(done)
    terminals = [e for e in await _stored(store) if _is_terminal("t")(e)]
    assert [(e.kind, e.worker_session_id) for e in terminals] == [
        ("finished", "w1"), ("failed", "w2")]
    await loop.aclose()


class _RefusingJsonl(JsonlStore):
    """Fails the first terminal event's line: before writing any of it
    (``refuse``), after writing half of it (``torn``) or after writing all of
    it (``landed``, as when only the fsync fails)."""

    def __init__(self, directory, mode: str) -> None:
        super().__init__(directory)
        self.mode = mode
        self.failed = 0

    def _append_line(self, path, record):
        if (not self.failed and record.get("_cls") == "SubagentEvent"
                and record.get("kind") == "finished"):
            self.failed += 1
            if self.mode == "torn":
                line = (json.dumps(record) + "\n").encode()
                with open(path, "ab") as fh:
                    fh.write(line[: len(line) // 2])
            if self.mode == "landed":
                super()._append_line(path, record)
            raise OSError("disk full")
        super()._append_line(path, record)


@pytest.mark.parametrize("mode", ["refuse", "torn"])
async def test_a_refused_jsonl_terminal_is_written_by_the_retry(tmp_path, mode) -> None:
    store = _RefusingJsonl(tmp_path, mode)
    await store.create_session(SID)
    loop = _loop(store, provider=ScriptedProvider([_text("picked it up")]))
    await loop._background_task_started(BackgroundTaskReceipt(task_id="t"))
    done = BackgroundTaskFinished(task_id="t", status="finished", summary="42", wake=True)

    with pytest.raises(OSError):
        await loop.submit(done)
    # Neither the writer's own reads nor a fresh reader see the refused line.
    assert _subagent(await _stored(store), "t", "finished") == []
    assert _subagent(await _stored(JsonlStore(tmp_path, lock=False)), "t", "finished") == []

    await loop.submit(done)
    await loop.wait_turn()
    await loop.aclose()
    store.close()

    fresh = JsonlStore(tmp_path, lock=False)
    assert len(_subagent(await _stored(fresh), "t", "finished")) == 1
    assert await _woken(fresh) == [("t",)]
    lines = (tmp_path / f"{SID}.events.jsonl").read_text().splitlines()
    assert all(json.loads(line) for line in lines)  # no fragment left behind


async def test_started_that_committed_before_its_write_raised_is_not_written_again() -> None:
    store = FailOnce(lambda e: isinstance(e, SubagentEvent) and e.kind == "started",
                     mode="commit_then_raise")
    await store.create_session(SID)
    loop = _loop(store)
    receipt = BackgroundTaskReceipt(task_id="t")
    with pytest.raises(OSError):
        await loop._background_task_started(receipt)
    assert "t" not in loop._background_pending

    await loop._background_task_started(receipt)
    assert "t" in loop._background_pending
    assert len(_subagent(await _stored(store), "t", "started")) == 1
    await loop.aclose()


async def test_refused_terminal_on_early_completion_path_is_retried_by_the_receipt() -> None:
    store = FailOnce(_is_terminal("t"))
    await store.create_session(SID)
    loop = _loop(store)
    # The worker finished before its spawn receipt arrived.
    await loop.submit(BackgroundTaskFinished(task_id="t", status="failed", wake=False))
    assert "t" in loop._background_early

    receipt = BackgroundTaskReceipt(task_id="t")
    with pytest.raises(OSError):
        await loop._background_task_started(receipt)
    assert "t" in loop._background_early and "t" not in loop._background_terminal

    await loop._background_task_started(receipt)  # retried spawn handshake
    assert "t" in loop._background_terminal and "t" not in loop._background_early
    assert len(_subagent(await _stored(store), "t", "failed")) == 1
    await loop.aclose()


async def test_refused_completion_pending_write_is_retried() -> None:
    store = FailOnce(lambda e: isinstance(e, SubagentEvent) and e.kind == "completion_pending")
    await store.create_session(SID)
    loop = _loop(store)
    done = BackgroundTaskFinished(task_id="t", status="finished", wake=False)
    with pytest.raises(OSError):
        await loop.submit(done)
    assert "t" not in loop._background_early

    await loop.submit(done)
    assert "t" in loop._background_early
    assert len(_subagent(await _stored(store), "t", "completion_pending")) == 1
    await loop.aclose()


async def test_superseded_early_completion_is_dropped() -> None:
    store = MemoryStore()
    await store.create_session(SID)
    loop = _loop(store)
    current = {"yes": True}
    await loop.submit(BackgroundTaskFinished(
        task_id="t", status="finished", wake=False, is_current=lambda: current["yes"]))
    assert "t" in loop._background_early

    current["yes"] = False  # a newer incarnation of the task took over
    await loop._background_task_started(BackgroundTaskReceipt(task_id="t"))
    assert "t" not in loop._background_early
    assert _subagent(await _stored(store), "t", "finished") == []
    await loop.aclose()


# ─── recovery after a restart ───────────────────────────────────────────────


async def test_refused_terminal_during_recovery_is_retried_by_a_later_call() -> None:
    store = FailOnce(_is_terminal("t"))
    await _store_with(
        _sub("t", "started"),
        _sub("t", "completion_pending", status="finished", summary="done", wake=False),
        store=store,
    )
    loop = _loop(store)

    with pytest.raises(OSError):
        await _rehydrate(loop)
    assert loop._background_rehydrated and not loop._background_recovered
    assert "t" in loop._background_early

    await _rehydrate(loop)  # the next lifecycle call finishes the recovery
    assert loop._background_recovered
    assert "t" in loop._background_terminal and "t" not in loop._background_early
    assert len(_subagent(await _stored(store), "t", "finished")) == 1
    await loop.aclose()


async def test_a_refused_host_recovered_terminal_is_retried_without_asking_again() -> None:
    store = FailOnce(_is_terminal("dead"))
    await _store_with(_sub("dead", "started"), store=store)
    asked: list[tuple[str, ...]] = []

    async def recover(pending):
        # The host answers once; by the next call the run looks active again.
        asked.append(tuple(pending))
        if len(asked) > 1:
            return []
        return [BackgroundTaskFinished(task_id="dead", status="failed",
                                       summary="host restarted", wake=False)]

    loop = _loop(store, recover_background=recover)
    with pytest.raises(OSError):
        await _rehydrate(loop)
    assert not loop._background_recovered and "dead" in loop._background_pending

    await _rehydrate(loop)
    assert asked == [("dead",)]
    assert loop._background_recovered
    assert "dead" in loop._background_terminal and "dead" not in loop._background_pending
    assert len(_subagent(await _stored(store), "dead", "failed")) == 1
    await loop.aclose()


# ─── a failed replay read is not a rehydration ──────────────────────────────


def _flaky_replay(monkeypatch, failures: int) -> dict[str, int]:
    real = background_mod.iter_events_of_class
    state = {"left": failures, "reads": 0}

    def flaky(store, sid, seq, classes):
        if SessionWoken in classes:  # the history read, not a reconcile lookup
            state["reads"] += 1
            if state["left"]:
                state["left"] -= 1
                raise OSError("store unavailable")
        return real(store, sid, seq, classes)

    monkeypatch.setattr(background_mod, "iter_events_of_class", flaky)
    return state


async def test_failed_replay_read_is_retried_and_owed_wake_delivered(monkeypatch) -> None:
    monkeypatch.setattr(background_mod, "_REPLAY_RETRY_BASE_SECONDS", 0.0)
    store = await _store_with(
        _sub("owed", "started"),
        _sub("owed", "finished", summary="the result", wake=True),
    )
    _flaky_replay(monkeypatch, failures=1)
    provider = ScriptedProvider([_text("first"), _text("second"), _text("woken")])
    loop = _loop(store, provider=provider)

    await _rehydrate(loop)  # the read fails
    assert loop._background_rehydrated is False
    assert loop._background_completions == {}

    await loop.submit(UserTurn(content="hello again"))
    await loop.wait_turn()

    woken = [e for e in loop.bus.log(SID) if isinstance(e, SessionWoken)]
    assert [w.task_ids for w in woken] == [("owed",)]
    assert any("the result" in r.messages[-1].text() for r in provider.requests)
    await loop.aclose()


async def test_failing_replay_backs_off_and_warns_once(monkeypatch, caplog) -> None:
    store = await _store_with(_sub("t", "started"))
    state = _flaky_replay(monkeypatch, failures=10)
    loop = _loop(store)

    with caplog.at_level(logging.WARNING, logger=background_mod.__name__):
        for _ in range(5):  # a burst of lifecycle calls during an outage
            await _rehydrate(loop)
        assert state["reads"] == 1
        loop._background_replay_retry_at = 0.0  # the backoff has elapsed
        await _rehydrate(loop)
        assert state["reads"] == 2
    assert loop._background_rehydrated is False
    assert loop._background_replay_failures == 2
    assert len([r for r in caplog.records if "background task history" in r.message]) == 1
    await loop.aclose()


def test_replay_backoff_stays_capped_after_a_long_outage() -> None:
    loop = _loop(MemoryStore())
    loop._background_replay_failures = 10_000  # hours of failed reads
    loop._note_replay_failure(SID, OSError("store unavailable"))
    wait = loop._background_replay_retry_at - background_mod.time.monotonic()
    assert 0 < wait <= background_mod._REPLAY_RETRY_MAX_SECONDS


# ─── handoff: billed, interruptible, one at a time, tool names only ─────────


class _HandoffProvider:
    name = "anthropic"
    caps = ProviderCaps()

    def __init__(self, *, hang: bool = False) -> None:
        self.requests: list[ProviderRequest] = []
        self.hang = hang
        self.started = asyncio.Event()

    async def stream(self, request: ProviderRequest) -> AsyncIterator[StreamChunk]:
        self.requests.append(request)
        yield StreamChunk("message_start", {"usage": {"input_tokens": 700}})
        self.started.set()
        if self.hang:
            await asyncio.sleep(3600)
        yield StreamChunk("text_delta", {"index": 0, "text": "HANDOFF DOC"})
        yield StreamChunk("usage", {"usage": {"output_tokens": 30}, "stop_reason": "end_turn"})
        yield StreamChunk("message_stop", {})


async def _tool_heavy_session(store: MemoryStore) -> str:
    sid = (await store.create_session()).session_id
    parent = None
    count = 0

    async def add(message: Message) -> None:
        nonlocal parent, count
        count += 1
        node = SessionNode(node_id=f"n{count}", parent_id=parent, message=message)
        await store.append(sid, node)
        parent = node.node_id

    await add(Message(role="user", content=[Text("migrate the billing tables")]))
    for i in range(60):
        await add(Message(role="assistant", content=[ToolUseBlock(
            id=f"tu{i}", name="Bash",
            input={"command": f"PGPASSWORD={SECRET} psql -c 'step {i}'"})]))
        await add(Message(role="user", content=[ToolResultBlock(
            tool_use_id=f"tu{i}", is_error=i == 59,
            content=[Text(f"token={SECRET} ALTER TABLE ok {i}")])]))
    return sid


async def test_handoff_usage_is_recorded_and_booked() -> None:
    store = MemoryStore()
    sid = await _tool_heavy_session(store)
    booked: list[Usage] = []

    async def sink(event: Usage) -> None:
        booked.append(event)

    loop = NativeLoop(provider=_HandoffProvider(), store=store, model="m",
                      session_id=sid, background_usage_sink=sink)
    await loop.submit(Handoff(goal="finish"))

    usage = [e for e in await store.events_since(sid, 0) if isinstance(e, Usage)]
    assert [(u.input_tokens, u.output_tokens, u.raw.get("auxiliary_source")) for u in usage] == [
        (700, 30, "handoff")]
    assert [e.seq for e in booked] == [usage[0].seq]
    await loop.aclose()


async def test_handoff_transcript_names_tools_but_never_their_payloads() -> None:
    store = MemoryStore()
    sid = await _tool_heavy_session(store)
    provider = _HandoffProvider()
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)
    await loop.submit(Handoff())

    prompt = provider.requests[-1].messages[0].content[0].text
    transcript = prompt.split("--- transcript ---\n", 1)[1]
    assert "migrate the billing tables" in transcript
    assert transcript.count("[tool call: Bash]") == 60
    assert "[Bash result: ok, " in transcript and "[Bash result: error, " in transcript
    assert SECRET not in prompt
    assert "psql" not in prompt and "ALTER TABLE" not in prompt
    assert [e for e in loop.bus.log(sid) if isinstance(e, HandoffCreated)]
    await loop.aclose()


def test_handoff_transcript_never_exceeds_its_budget(monkeypatch) -> None:
    assert len(_clip("x" * 10, 5)) == 5 and _clip("x" * 10, 5).endswith("…")
    monkeypatch.setattr(native_mod, "_HANDOFF_TRANSCRIPT_CHARS", 100)
    huge = SessionNode(node_id="n1", parent_id=None,
                       message=Message(role="user", content=[Text("y" * 5000)]))
    assert len(_handoff_transcript([huge])) == 100
    small = [SessionNode(node_id=f"s{i}", parent_id=None,
                         message=Message(role="user", content=[Text("z" * 30)]))
             for i in range(10)]
    assert len(_handoff_transcript(small)) <= 100


async def test_interrupt_cancels_the_handoff_call_and_still_bills_it() -> None:
    store = MemoryStore()
    sid = await _tool_heavy_session(store)
    provider = _HandoffProvider(hang=True)
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)
    sessions_before = set(store._sessions)

    handoff = asyncio.create_task(loop.submit(Handoff()))
    await asyncio.wait_for(provider.started.wait(), 5)
    await loop.submit(Interrupt())

    with pytest.raises(RuntimeError, match="handoff not created.*interrupted"):
        await asyncio.wait_for(handoff, 5)
    assert set(store._sessions) == sessions_before
    usage = [e for e in await store.events_since(sid, 0) if isinstance(e, Usage)]
    assert [(u.input_tokens, u.raw.get("auxiliary_source")) for u in usage] == [(700, "handoff")]
    await loop.aclose()


async def test_a_second_handoff_while_one_runs_fails_fast() -> None:
    store = MemoryStore()
    sid = await _tool_heavy_session(store)
    provider = _HandoffProvider(hang=True)
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)
    sessions_before = set(store._sessions)

    first = asyncio.create_task(loop.submit(Handoff()))
    await asyncio.wait_for(provider.started.wait(), 5)
    with pytest.raises(RuntimeError, match="another handoff is already running"):
        await loop.submit(Handoff())
    assert len(provider.requests) == 1

    await loop.submit(Interrupt())  # still reaches the one handoff running
    with pytest.raises(RuntimeError, match="interrupted"):
        await asyncio.wait_for(first, 5)
    assert set(store._sessions) == sessions_before
    assert loop._handoff_running is False
    await loop.aclose()


class _SlowCreate(MemoryStore):
    """Holds ``create_session`` once ``hold`` is set, until released."""

    def __init__(self) -> None:
        super().__init__()
        self.creating = asyncio.Event()
        self.release = asyncio.Event()
        self.hold = False

    async def create_session(self, session_id=None, *, cwd=None):
        if self.hold:
            self.creating.set()
            await self.release.wait()
        return await super().create_session(session_id, cwd=cwd)


async def test_interrupt_after_the_document_arrived_does_not_undo_the_handoff() -> None:
    store = _SlowCreate()
    sid = await _tool_heavy_session(store)
    store.hold = True
    loop = NativeLoop(provider=_HandoffProvider(), store=store, model="m", session_id=sid)

    handoff = asyncio.create_task(loop.submit(Handoff()))
    await asyncio.wait_for(store.creating.wait(), 5)
    await loop.submit(Interrupt())  # past the cutoff: the session is made
    store.release.set()
    await asyncio.wait_for(handoff, 5)

    created = [e for e in loop.bus.log(sid) if isinstance(e, HandoffCreated)]
    assert len(created) == 1 and created[0].new_session_id in store._sessions
    await loop.aclose()


async def test_close_cancels_a_running_handoff_call() -> None:
    store = MemoryStore()
    sid = await _tool_heavy_session(store)
    provider = _HandoffProvider(hang=True)
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)

    handoff = asyncio.create_task(loop.submit(Handoff()))
    await asyncio.wait_for(provider.started.wait(), 5)
    await asyncio.wait_for(loop.aclose(), 5)
    with pytest.raises(RuntimeError, match="handoff not created"):
        await asyncio.wait_for(handoff, 5)


async def test_close_after_the_document_arrived_waits_for_the_handoff_report() -> None:
    store = _SlowCreate()
    sid = await _tool_heavy_session(store)
    store.hold = True
    loop = NativeLoop(provider=_HandoffProvider(), store=store, model="m", session_id=sid)
    seen: list = []

    async def watch() -> None:
        async for event in loop.bus.subscribe(sid, from_store=False):
            seen.append(event)

    watcher = asyncio.create_task(watch())
    await asyncio.sleep(0)
    handoff = asyncio.create_task(loop.submit(Handoff()))
    await asyncio.wait_for(store.creating.wait(), 5)
    closing = asyncio.create_task(loop.aclose())  # past the cutoff
    await asyncio.sleep(0.05)
    assert not closing.done()  # waiting for the report, not closing under it
    store.release.set()
    await asyncio.wait_for(handoff, 5)
    await asyncio.wait_for(closing, 5)
    await asyncio.wait_for(watcher, 5)

    created = [e for e in seen if isinstance(e, HandoffCreated)]
    assert len(created) == 1 and created[0].new_session_id in store._sessions


# ─── retries reconcile by write id only ─────────────────────────────────────


async def test_an_identical_terminal_from_another_write_is_not_taken_for_this_one() -> None:
    store = FailOnce(_is_terminal("t"))
    await store.create_session(SID)
    loop = _loop(store)
    await loop._background_task_started(BackgroundTaskReceipt(task_id="t"))
    done = BackgroundTaskFinished(task_id="t", status="finished", wake=False)
    with pytest.raises(OSError):
        await loop.submit(done)
    # A terminal with the very same payload lands from some other write.
    foreign = replace(_sub("t", "finished", wake=False, write_id="f" * 32),
                      seq=loop.bus.last_seq(SID) + 1)
    loop.bus.seed(SID, foreign.seq)
    await MemoryStore.append_event(store, SID, foreign)

    await loop.submit(done)
    terminals = [e for e in await _stored(store) if _is_terminal("t")(e)]
    assert len(terminals) == 2 and terminals[1].write_id not in (None, foreign.write_id)
    await loop.aclose()


async def test_a_stored_attempt_its_host_superseded_does_not_stand_in_for_the_current_one() -> None:
    store = FailOnce(lambda e: isinstance(e, SubagentEvent) and e.kind == "completion_pending",
                     mode="commit_then_raise")
    await store.create_session(SID)
    loop = _loop(store, provider=ScriptedProvider([_text("picked it up")]))
    live = {"old": True}
    old = BackgroundTaskFinished(task_id="t", status="failed", worker_session_id="w",
                                 wake=False, is_current=lambda: live["old"])
    new = BackgroundTaskFinished(task_id="t", status="finished", worker_session_id="w",
                                 summary="the new one", wake=True)
    with pytest.raises(OSError):
        await loop.submit(old)  # stored, then the acknowledgement was lost
    live["old"] = False  # a replacement worker took over the task id
    await loop.submit(new)
    assert loop._background_early["t"] is new

    await loop._background_task_started(BackgroundTaskReceipt(task_id="t", worker_session_id="w"))
    await loop.wait_turn()
    stored = await _stored(store)
    assert [e.status for e in _subagent(stored, "t", "completion_pending")] == ["failed", "finished"]
    assert [e.summary for e in _subagent(stored, "t", "finished")] == ["the new one"]
    assert _subagent(stored, "t", "failed") == []
    assert await _woken(store) == [("t",)]
    await loop.aclose()


async def test_the_same_op_retried_keeps_its_write_id() -> None:
    store = FailOnce(_is_terminal("t"))
    await store.create_session(SID)
    loop = _loop(store)
    await loop._background_task_started(BackgroundTaskReceipt(task_id="t"))
    done = BackgroundTaskFinished(task_id="t", status="failed", wake=False)
    with pytest.raises(OSError):
        await loop.submit(done)
    (_, attempts), = loop._background_uncertain.values()
    first_id = attempts[0][0]
    await loop.submit(done)
    (terminal,) = [e for e in await _stored(store) if _is_terminal("t")(e)]
    assert terminal.write_id == first_id
    assert loop._background_uncertain == {}
    await loop.aclose()


def test_uncertain_attempts_are_bounded() -> None:
    loop = _loop(MemoryStore())
    for i in range(50):
        loop._remember_uncertain(("t", "terminal"), 5, f"id{i}", object())
    (since, attempts), = loop._background_uncertain.values()
    assert since == 5 and len(attempts) == background_mod._UNCERTAIN_ATTEMPTS_PER_STEP
    assert attempts[-1][0] == "id49"
    for i in range(background_mod._UNCERTAIN_STEPS + 20):
        loop._remember_uncertain((f"task{i}", "terminal"), 1, "x", None)
    assert len(loop._background_uncertain) == background_mod._UNCERTAIN_STEPS
    assert ("t", "terminal") not in loop._background_uncertain  # the oldest went first


# ─── host recovery never overrides a live completion ────────────────────────


async def test_a_live_completion_wins_over_a_refused_recovery_inference() -> None:
    store = FailOnce(lambda e: isinstance(e, SubagentEvent) and e.kind == "failed")
    await _store_with(_sub("t", "started"), store=store)

    async def recover(pending):
        return [BackgroundTaskFinished(task_id="t", status="failed", wake=False)]

    loop = _loop(store, recover_background=recover)
    with pytest.raises(OSError):
        await _rehydrate(loop)  # the inferred "failed" is refused and kept
    assert "t" in loop._background_host_recovery

    await loop.submit(BackgroundTaskFinished(task_id="t", status="finished", wake=False))
    stored = await _stored(store)
    assert len(_subagent(stored, "t", "finished")) == 1
    assert _subagent(stored, "t", "failed") == []
    assert loop._background_recovered and loop._background_host_recovery == {}
    await loop.aclose()


async def test_recovery_leaves_the_live_task_to_its_completion_on_the_first_call() -> None:
    store = await _store_with(_sub("t", "started"), _sub("u", "started"))

    async def recover(pending):
        return [BackgroundTaskFinished(task_id=t, status="failed", wake=False) for t in pending]

    loop = _loop(store, recover_background=recover)
    await loop.submit(BackgroundTaskFinished(task_id="t", status="finished", wake=False))
    stored = await _stored(store)
    assert [e.kind for e in stored if isinstance(e, SubagentEvent) and e.task_id == "t"] == [
        "started", "finished"]
    assert len(_subagent(stored, "u", "failed")) == 1  # the other task is still recovered
    await loop.aclose()


async def test_a_cached_recovery_op_its_host_superseded_is_not_committed() -> None:
    store = FailOnce(lambda e: isinstance(e, SubagentEvent) and e.kind == "failed")
    await _store_with(_sub("t", "started"), store=store)
    live = {"op": True}

    async def recover(pending):
        return [BackgroundTaskFinished(task_id="t", status="failed", wake=False,
                                       is_current=lambda: live["op"])]

    loop = _loop(store, recover_background=recover)
    with pytest.raises(OSError):
        await _rehydrate(loop)
    live["op"] = False
    await _rehydrate(loop)  # a later lifecycle call, about something else
    assert loop._background_recovered and "t" in loop._background_pending
    assert _subagent(await _stored(store), "t", "failed") == []
    await loop.aclose()


# ─── JSONL: fail closed, never retract a complete line ──────────────────────


def _event(task_id: str, seq: int) -> SubagentEvent:
    return replace(_sub(task_id, "started"), seq=seq)


def _fresh_tasks(tmp_path) -> list[str]:
    fresh = JsonlStore(tmp_path, lock=False)
    return [e.task_id for e in asyncio.run(fresh.events_since(SID, 0))]


def test_two_stores_in_one_process_never_erase_each_others_events(tmp_path, monkeypatch) -> None:
    a = JsonlStore(tmp_path, fsync=True)
    asyncio.run(a.create_session(SID))
    b = JsonlStore(tmp_path, fsync=True)
    real_fsync = jsonl_mod.os.fsync
    entered, release = threading.Event(), threading.Event()

    def fsync(fd):
        if threading.current_thread().name == "writer-a" and not entered.is_set():
            entered.set()  # A's whole line is written; its fsync stalls, then fails
            release.wait(5)
            raise OSError("fsync failed")
        real_fsync(fd)

    monkeypatch.setattr(jsonl_mod.os, "fsync", fsync)
    outcome: dict[str, object] = {}

    def run(name, store, event):
        try:
            asyncio.run(store.append_event(SID, event))
            outcome[name] = "ok"
        except OSError as exc:
            outcome[name] = exc

    writer_a = threading.Thread(target=run, name="writer-a", args=("a", a, _event("A", 1)))
    writer_a.start()
    assert entered.wait(5)
    writer_b = threading.Thread(target=run, name="writer-b", args=("b", b, _event("B", 2)))
    writer_b.start()
    time.sleep(0.2)
    assert writer_b.is_alive()  # waits for A's append to settle
    release.set()
    writer_a.join(5)
    writer_b.join(5)

    assert isinstance(outcome["a"], OSError) and outcome["b"] == "ok"
    assert _fresh_tasks(tmp_path) == ["A", "B"]
    # A's line stayed and is cached, so a retry through A finds it.
    assert [e.task_id for e in asyncio.run(a.events_since(SID, 0))] == ["A"]
    a.close()
    b.close()


async def test_a_jsonl_terminal_whose_line_landed_is_kept_and_not_written_again(tmp_path) -> None:
    store = _RefusingJsonl(tmp_path, "landed")
    await store.create_session(SID)
    loop = _loop(store, provider=ScriptedProvider([_text("picked it up")]))
    await loop._background_task_started(BackgroundTaskReceipt(task_id="t"))
    done = BackgroundTaskFinished(task_id="t", status="finished", summary="42", wake=True)

    with pytest.raises(OSError):
        await loop.submit(done)
    # Raised, yet the line is on disk: the writer and a fresh reader agree.
    assert len(_subagent(await _stored(store), "t", "finished")) == 1
    assert len(_subagent(await _stored(JsonlStore(tmp_path, lock=False)), "t", "finished")) == 1

    await loop.submit(done)
    await loop.wait_turn()
    await loop.aclose()
    store.close()
    fresh = JsonlStore(tmp_path, lock=False)
    assert len(_subagent(await _stored(fresh), "t", "finished")) == 1
    assert await _woken(fresh) == [("t",)]


def test_a_refused_rollback_refuses_appends_until_it_is_repaired(tmp_path, monkeypatch) -> None:
    store = _RefusingJsonl(tmp_path, "torn")
    asyncio.run(store.create_session(SID))
    asyncio.run(store.append_event(SID, _event("before", 1)))
    path = tmp_path / f"{SID}.events.jsonl"
    real_open = type(path).open
    blocked = {"on": True}

    def open_(self, mode="r", *args, **kwargs):
        if blocked["on"] and self == path and mode == "r+b":
            raise PermissionError("rollback refused")
        return real_open(self, mode, *args, **kwargs)

    monkeypatch.setattr(type(path), "open", open_)
    torn = replace(_sub("t", "finished"), seq=2)
    with pytest.raises(OSError, match="disk full"):
        asyncio.run(store.append_event(SID, torn))
    fragment = path.read_bytes()
    # Nothing joins the fragment: not this store, and not another one.
    with pytest.raises(OSError):
        asyncio.run(store.append_event(SID, _event("after", 3)))
    other = JsonlStore(tmp_path)
    with pytest.raises(OSError, match="torn last line|partial line"):
        asyncio.run(other.append_event(SID, _event("other", 3)))
    assert path.read_bytes() == fragment
    assert _fresh_tasks(tmp_path) == ["before"]

    blocked["on"] = False  # the repair can run now
    asyncio.run(store.append_event(SID, _event("after", 3)))
    assert _fresh_tasks(tmp_path) == ["before", "after"]
    assert all(json.loads(line) for line in path.read_text().splitlines())
    store.close()
    other.close()


async def test_a_whole_record_missing_its_newline_is_kept(tmp_path) -> None:
    store = JsonlStore(tmp_path)
    await store.create_session(SID)
    await store.append_event(SID, _event("a", 1))
    store.close()
    path = tmp_path / f"{SID}.events.jsonl"
    path.write_bytes(path.read_bytes().rstrip(b"\n"))  # a crash before the newline

    reopened = JsonlStore(tmp_path)
    await reopened.append_event(SID, _event("b", 2))
    reopened.close()
    assert await asyncio.to_thread(_fresh_tasks, tmp_path) == ["a", "b"]


# ─── handoff: bounded close, abandoned after the deadline ───────────────────


async def test_close_is_bounded_while_a_cancelled_document_call_books_its_usage(
    monkeypatch,
) -> None:
    monkeypatch.setattr(native_mod, "_HANDOFF_CLOSE_WAIT_S", 0.2)
    store = MemoryStore()
    sid = await _tool_heavy_session(store)
    provider = _HandoffProvider(hang=True)
    loop = NativeLoop(provider=provider, store=store, model="m", session_id=sid)

    async def stalled_booking(*args, **kwargs):
        await asyncio.sleep(3600)

    loop._record_side_call_usage = stalled_booking
    handoff = asyncio.create_task(loop.submit(Handoff()))
    await asyncio.wait_for(provider.started.wait(), 5)
    started = time.monotonic()
    await asyncio.wait_for(loop.aclose(), 5)
    assert time.monotonic() - started < 2
    with pytest.raises(RuntimeError, match="handoff not created"):
        await asyncio.wait_for(handoff, 5)


class _StubbornCreate(_SlowCreate):
    """A ``create_session`` that ignores cancellation until released."""

    async def create_session(self, session_id=None, *, cwd=None):
        if self.hold:
            self.creating.set()
            while not self.release.is_set():
                try:
                    await self.release.wait()
                except asyncio.CancelledError:
                    continue
        return await MemoryStore.create_session(self, session_id, cwd=cwd)


async def test_close_abandons_a_handoff_whose_creation_outlives_the_deadline(monkeypatch) -> None:
    monkeypatch.setattr(native_mod, "_HANDOFF_CLOSE_WAIT_S", 0.2)
    store = _StubbornCreate()
    sid = await _tool_heavy_session(store)
    store.hold = True
    loop = NativeLoop(provider=_HandoffProvider(), store=store, model="m", session_id=sid)

    handoff = asyncio.create_task(loop.submit(Handoff()))
    await asyncio.wait_for(store.creating.wait(), 5)
    await asyncio.wait_for(loop.aclose(), 5)  # gives up at the deadline
    store.release.set()  # the store finishes after close
    with pytest.raises(RuntimeError, match="engine closed before it was reported"):
        await asyncio.wait_for(handoff, 5)
    # Never reported: not durable, and not published behind the closed bus.
    assert not [e for e in await store.events_since(sid, 0) if isinstance(e, HandoffCreated)]
    assert not [e for e in loop.bus.log(sid) if isinstance(e, HandoffCreated)]


async def test_a_handoff_on_a_closed_loop_creates_no_session() -> None:
    store = MemoryStore()
    loop = NativeLoop(provider=_HandoffProvider(), store=store, model="m")
    await loop.aclose()
    with pytest.raises(RuntimeError, match="the engine is closing"):
        await loop.submit(Handoff())
    assert store._sessions == {}


async def test_handoff_usage_is_booked_against_the_model_it_ran_on() -> None:
    store = MemoryStore()
    sid = await _tool_heavy_session(store)

    class _SwitchMidStream(_HandoffProvider):
        async def stream(self, request):
            async for chunk in super().stream(request):
                loop.model = "replacement"  # a model switch while it streams
                yield chunk

    loop = NativeLoop(provider=_SwitchMidStream(), store=store, model="original",
                      session_id=sid)
    await loop.submit(Handoff())
    usage = [e for e in await store.events_since(sid, 0) if isinstance(e, Usage)]
    assert [u.raw.get("model") for u in usage] == ["original"]
    await loop.aclose()
