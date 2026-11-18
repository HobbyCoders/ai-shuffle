"""A failed lifecycle write leaves a background task retryable, never lost.

Each background mutator writes its SubagentEvent first and changes the
engine's in-memory task state only after that write returns. A write whose
outcome is unknown (it raised, or its caller was cancelled while it ran) is
not reasoned about: the event, already published live, is held, and the next
lifecycle access writes it unless the store already has it, then rebuilds the
task state from the store with the code a reopen uses. Live subscribers see
each lifecycle event once, and the parent is woken exactly once, in this
engine and after a restart.

The CLI host keeps a result the engine refused and offers it again on later
activity and at close.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest
from fluid.cli.host import CliHost, Worker
from fluid.events import (
    BackgroundTaskFinished,
    SessionWoken,
    SubagentEvent,
    TurnComplete,
    UserTurn,
)
from fluid.store.jsonl import JsonlStore
from fluid.tools.registry import BackgroundTaskReceipt

from tests.unit.test_background_tasks import _loop, _task_call
from tests.unit.test_tool_loop import ScriptedProvider, _text

SID = "parent-session"


class FlakyStore(JsonlStore):
    """Refuses the first event matching ``fail``; ``landed`` writes it first."""

    fail = None
    landed = False

    async def append_event(self, session_id, event):
        if self.fail is not None and self.fail(event):
            self.fail = None
            if self.landed:
                await super().append_event(session_id, event)
            raise OSError(28, "No space left on device")
        await super().append_event(session_id, event)


def _kind(kind: str):
    return lambda e: isinstance(e, SubagentEvent) and e.kind == kind


async def _no_dispatch(input, ctx):
    raise AssertionError("not used")


def _woken(loop) -> list[SessionWoken]:
    return [e for e in loop.bus.log(loop.session_id) if isinstance(e, SessionWoken)]


async def _restart_wakes(tmp_path) -> int:
    """Model calls a fresh engine makes for one user turn on the same session."""
    provider = ScriptedProvider([_text("user turn"), _text("unexpected wake")])
    fresh = _loop(provider, JsonlStore(tmp_path), _no_dispatch, session_id=SID)
    await fresh.submit(UserTurn(content="anything new?"))
    await fresh.wait_turn()
    await fresh.aclose()
    return provider.calls


def _finished(events) -> list[SubagentEvent]:
    return [e for e in events if isinstance(e, SubagentEvent) and e.kind == "finished"]


def _started(events) -> list[SubagentEvent]:
    return [e for e in events if isinstance(e, SubagentEvent) and e.kind == "started"]


async def _parked(tmp_path, store):
    async def dispatch(input, ctx):
        return BackgroundTaskReceipt(task_id="task-1", worker_session_id="worker-1")

    provider = ScriptedProvider([
        _task_call(), _text("parked"), _text("incorporated"), _text("second wake"),
    ])
    loop = _loop(provider, store, dispatch, session_id=SID)
    await loop.submit(UserTurn(content="start"))
    await loop.wait_turn()
    assert provider.calls == 2
    return loop, provider


async def test_failed_terminal_write_is_retried_and_wakes_exactly_once(tmp_path) -> None:
    async def dispatch(input, ctx):
        return BackgroundTaskReceipt(task_id="task-1", worker_session_id="worker-1")

    store = FlakyStore(tmp_path)
    provider = ScriptedProvider([
        _task_call(), _text("parked"), _text("incorporated"), _text("second wake"),
    ])
    loop = _loop(provider, store, dispatch, session_id=SID)
    await loop.submit(UserTurn(content="start"))
    await loop.wait_turn()
    assert provider.calls == 2

    done = BackgroundTaskFinished(task_id="task-1", summary="The answer is 42.")
    store.fail = _kind("finished")
    with pytest.raises(OSError):
        await loop.submit(done)
    await loop.wait_turn()
    assert provider.calls == 2  # nothing woke on a record that may not exist

    await loop.submit(done)  # the host resubmits the completion
    await loop.wait_turn()
    assert provider.calls == 3
    assert "The answer is 42." in provider.requests[-1].messages[-1].text()
    assert [w.task_ids for w in _woken(loop)] == [("task-1",)]
    # Live subscribers saw the one terminal the store now holds, not two.
    assert [e.seq for e in _finished(loop.bus.log(SID))] == [
        e.seq for e in _finished(await store.events_since(SID, 0))
    ]

    await loop.submit(done)  # a later duplicate is still a no-op
    await loop.wait_turn()
    assert provider.calls == 3
    assert len(_finished(await store.events_since(SID, 0))) == 1
    assert len(_finished(loop.bus.log(SID))) == 1
    await loop.aclose()
    assert await _restart_wakes(tmp_path) == 1


async def test_a_terminal_write_that_landed_is_applied_and_published_once(tmp_path) -> None:
    # The write raised, so the caller is told so; nothing guesses whether it
    # landed. The host's resend (any later access would do) rebuilds from the
    # store, which has the record, and wakes once on it.
    async def dispatch(input, ctx):
        return BackgroundTaskReceipt(task_id="task-1", worker_session_id="worker-1")

    store = FlakyStore(tmp_path)
    provider = ScriptedProvider([
        _task_call(), _text("parked"), _text("incorporated"), _text("second wake"),
    ])
    loop = _loop(provider, store, dispatch, session_id=SID)
    await loop.submit(UserTurn(content="start"))
    await loop.wait_turn()

    done = BackgroundTaskFinished(task_id="task-1", summary="The answer is 42.")
    store.fail, store.landed = _kind("finished"), True
    with pytest.raises(OSError):
        await loop.submit(done)
    await loop.submit(done)  # the host's resend
    await loop.wait_turn()
    assert provider.calls == 3
    assert "The answer is 42." in provider.requests[-1].messages[-1].text()

    await loop.submit(done)  # a later duplicate from the host stays a no-op
    await loop.wait_turn()
    assert provider.calls == 3
    assert len(_finished(loop.bus.log(SID))) == 1
    assert len(_finished(await store.events_since(SID, 0))) == 1
    assert [w.task_ids for w in _woken(loop)] == [("task-1",)]
    await loop.aclose()
    assert await _restart_wakes(tmp_path) == 1


async def test_failed_early_completion_write_is_recorded_on_retry(tmp_path) -> None:
    store = FlakyStore(tmp_path)
    loop = _loop(ScriptedProvider([]), store, _no_dispatch, session_id=SID)
    done = BackgroundTaskFinished(task_id="task-early", summary="done before receipt")
    store.fail = _kind("completion_pending")
    with pytest.raises(OSError):
        await loop.submit(done)
    await loop.submit(done)
    await loop.aclose()

    # The receipt reaches a fresh engine: the completion it finds is durable.
    provider = ScriptedProvider([_text("woke on the early completion")])
    fresh = _loop(provider, JsonlStore(tmp_path), _no_dispatch, session_id=SID)
    await fresh._background_task_started(BackgroundTaskReceipt(task_id="task-early"))
    await fresh.wait_turn()
    assert provider.calls == 1
    assert "done before receipt" in provider.requests[-1].messages[-1].text()
    await fresh.aclose()


async def test_failed_started_write_is_recorded_on_retry(tmp_path) -> None:
    store = FlakyStore(tmp_path)
    loop = _loop(ScriptedProvider([]), store, _no_dispatch, session_id=SID)
    receipt = BackgroundTaskReceipt(task_id="task-s", worker_session_id="worker-s")
    store.fail = _kind("started")
    with pytest.raises(OSError):
        await loop._background_task_started(receipt)
    assert "task-s" not in loop._background_pending
    await loop._background_task_started(receipt)
    assert "task-s" in loop._background_pending
    assert len(_started(loop.bus.log(SID))) == 1  # published once, not per attempt
    assert len(_started(await store.events_since(SID, 0))) == 1
    await loop.aclose()

    provider = ScriptedProvider([_text("woke after restart")])
    fresh = _loop(provider, JsonlStore(tmp_path), _no_dispatch, session_id=SID)
    await fresh.submit(BackgroundTaskFinished(task_id="task-s", summary="result"))
    await fresh.wait_turn()
    assert provider.calls == 1
    await fresh.aclose()


async def _held_completion_without_terminal(tmp_path) -> None:
    """Leave a log holding completion_pending and started, but no terminal."""
    store = FlakyStore(tmp_path)
    loop = _loop(ScriptedProvider([]), store, _no_dispatch, session_id=SID)
    await loop.submit(BackgroundTaskFinished(task_id="task-h", summary="held result"))
    store.fail = _kind("finished")
    with pytest.raises(OSError):
        await loop._background_task_started(BackgroundTaskReceipt(task_id="task-h"))
    assert "task-h" in loop._background_pending
    assert "task-h" in loop._background_early
    return loop, store


async def test_a_retried_receipt_finalizes_its_held_completion(tmp_path) -> None:
    loop, _store = await _held_completion_without_terminal(tmp_path)
    provider = loop.provider = ScriptedProvider([_text("woke on the held result")])
    await loop._background_task_started(BackgroundTaskReceipt(task_id="task-h"))
    await loop.wait_turn()
    assert provider.calls == 1
    assert "held result" in provider.requests[-1].messages[-1].text()
    assert "task-h" not in loop._background_pending
    await loop.aclose()


async def test_a_failed_drain_during_rehydration_is_retried(tmp_path, caplog) -> None:
    loop, _store = await _held_completion_without_terminal(tmp_path)
    await loop.aclose()

    store = FlakyStore(tmp_path)
    provider = ScriptedProvider([_text("woke after restart")])
    fresh = _loop(provider, store, _no_dispatch, session_id=SID)

    async def touch() -> None:
        async with fresh._background_lock:
            await fresh._rehydrate_background_lifecycle_unlocked()

    store.fail = _kind("finished")
    with caplog.at_level(logging.WARNING), contextlib.suppress(OSError):
        await touch()
    assert "task-h" in fresh._background_pending  # still parked, not lost

    await touch()  # storage recovered: the next lifecycle access drains it
    assert "task-h" in fresh._background_completions
    fresh._kick_background_wake()
    await fresh.wait_turn()
    assert provider.calls == 1
    assert "held result" in provider.requests[-1].messages[-1].text()
    await fresh.aclose()


async def test_a_started_write_that_landed_lets_its_completion_wake(tmp_path) -> None:
    store = FlakyStore(tmp_path)
    provider = ScriptedProvider([_text("woke on the result")])
    loop = _loop(provider, store, _no_dispatch, session_id=SID)
    store.fail, store.landed = _kind("started"), True
    with pytest.raises(OSError):
        await loop._background_task_started(BackgroundTaskReceipt(task_id="task-s"))

    # The store holds "started", so the completion finalizes the task, as it
    # would in a reopened engine, instead of being held as early forever.
    await loop.submit(BackgroundTaskFinished(task_id="task-s", summary="result"))
    await loop.wait_turn()
    assert provider.calls == 1
    assert "result" in provider.requests[-1].messages[-1].text()
    assert len(_started(loop.bus.log(SID))) == 1
    assert len(_finished(await store.events_since(SID, 0))) == 1
    await loop.aclose()
    assert await _restart_wakes(tmp_path) == 1


async def test_a_completion_pending_write_that_landed_is_finalized_by_the_receipt(
    tmp_path,
) -> None:
    store = FlakyStore(tmp_path)
    provider = ScriptedProvider([_text("woke on the early result")])
    loop = _loop(provider, store, _no_dispatch, session_id=SID)
    store.fail, store.landed = _kind("completion_pending"), True
    with pytest.raises(OSError):
        await loop.submit(BackgroundTaskFinished(task_id="task-e", summary="early result"))

    await loop._background_task_started(BackgroundTaskReceipt(task_id="task-e"))
    await loop.wait_turn()
    assert provider.calls == 1
    assert "early result" in provider.requests[-1].messages[-1].text()
    await loop.aclose()
    assert await _restart_wakes(tmp_path) == 1


async def test_a_turn_still_ends_when_task_state_cannot_be_rebuilt(
    tmp_path, monkeypatch, caplog
) -> None:
    store = FlakyStore(tmp_path)
    loop, _provider = await _parked(tmp_path, store)
    store.fail = _kind("finished")
    with pytest.raises(OSError):
        await loop.submit(BackgroundTaskFinished(task_id="task-1", summary="42"))

    async def unreadable(*_args, **_kwargs):
        raise OSError(5, "I/O error")

    monkeypatch.setattr(store, "events_since", unreadable)
    with caplog.at_level(logging.WARNING):
        await loop.submit(UserTurn(content="still there?"))
        await loop.wait_turn()
    assert any(isinstance(e, TurnComplete) for e in loop.bus.log(SID)[-3:])
    assert "could not be read back at turn end" in caplog.text

    monkeypatch.undo()  # storage recovered: the next access heals and wakes once
    await loop.submit(BackgroundTaskFinished(task_id="task-1", summary="42"))
    await loop.wait_turn()
    assert len(_finished(await store.events_since(SID, 0))) == 1
    assert [w.task_ids for w in _woken(loop)] == [("task-1",)]
    await loop.aclose()
    assert await _restart_wakes(tmp_path) == 1


async def test_a_rebuild_from_any_caller_arms_the_wake_it_finds_owed(tmp_path) -> None:
    store = FlakyStore(tmp_path)
    loop, provider = await _parked(tmp_path, store)
    store.fail, store.landed = _kind("finished"), True
    with pytest.raises(OSError):
        await loop.submit(BackgroundTaskFinished(task_id="task-1", summary="42"))

    # The app's reconcile rebuilds under the engine's lock and submits nothing.
    async with loop._background_lock:
        await loop._rehydrate_background_lifecycle_unlocked()
    assert loop._background_wake_task is not None  # armed, not left for later
    await loop._background_wake_task
    await loop.wait_turn()
    assert provider.calls == 3
    assert [w.task_ids for w in _woken(loop)] == [("task-1",)]
    await loop.aclose()
    assert await _restart_wakes(tmp_path) == 1


class LateStore(JsonlStore):
    """The terminal write lands after its caller was cancelled (a DB thread)."""

    gate: asyncio.Event | None = None
    late: asyncio.Future | None = None

    async def append_event(self, session_id, event):
        if self.gate is not None and _kind("finished")(event):
            gate, self.gate = self.gate, None

            async def write_later() -> None:
                await gate.wait()
                await JsonlStore.append_event(self, session_id, event)

            self.late = asyncio.ensure_future(write_later())
            await asyncio.shield(self.late)
            return
        await super().append_event(session_id, event)


async def test_a_terminal_write_that_lands_after_its_caller_was_cancelled(tmp_path) -> None:
    store = LateStore(tmp_path)
    loop, provider = await _parked(tmp_path, store)
    done = BackgroundTaskFinished(task_id="task-1", summary="The answer is 42.")

    store.gate = gate = asyncio.Event()
    submitter = asyncio.create_task(loop.submit(done))
    while store.late is None:
        await asyncio.sleep(0)
    submitter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submitter
    gate.set()
    await store.late  # the write the caller gave up on has now landed

    await loop.submit(done)  # the host's resend, or any later lifecycle access
    await loop.wait_turn()
    assert provider.calls == 3
    assert "The answer is 42." in provider.requests[-1].messages[-1].text()
    assert len(_finished(await store.events_since(SID, 0))) == 1
    assert len(_finished(loop.bus.log(SID))) == 1
    assert [w.task_ids for w in _woken(loop)] == [("task-1",)]
    await loop.aclose()
    assert await _restart_wakes(tmp_path) == 1


async def test_a_cancelled_terminal_write_that_never_landed_is_written_once(tmp_path) -> None:
    store = LateStore(tmp_path)
    loop, provider = await _parked(tmp_path, store)
    done = BackgroundTaskFinished(task_id="task-1", summary="The answer is 42.")

    store.gate = asyncio.Event()  # never set: this write never lands
    submitter = asyncio.create_task(loop.submit(done))
    while store.late is None:
        await asyncio.sleep(0)
    submitter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await submitter
    store.late.cancel()

    await loop.submit(done)
    await loop.wait_turn()
    assert provider.calls == 3
    # The event live subscribers already saw is the one written, seq and all.
    assert [e.seq for e in _finished(loop.bus.log(SID))] == [
        e.seq for e in _finished(await store.events_since(SID, 0))
    ]
    await loop.aclose()
    assert await _restart_wakes(tmp_path) == 1


class _RefusingEngine:
    def __init__(self) -> None:
        self.refusing = True
        self.accepted: list[str] = []

    async def submit(self, op) -> None:
        if self.refusing:
            raise OSError(28, "No space left on device")
        self.accepted.append(op.task_id)


def _worker(task_id: str) -> Worker:
    return Worker(task_id=task_id, kind="monitor", description="x")


async def test_cli_host_delivers_a_refused_result_on_later_activity() -> None:
    host = CliHost(config=None, store=None)
    host.engine = engine = _RefusingEngine()
    first = _worker("t1")
    await host._report(first, "finished", "ok")
    assert first.status == "finished"  # the process exited ...
    assert engine.accepted == []  # ... but its result is not delivered yet
    assert not host.busy()

    engine.refusing = False  # storage recovered
    await host._report(_worker("t2"), "failed", "boom")
    assert engine.accepted == ["t1", "t2"]


async def test_cli_host_delivers_a_refused_result_at_close_or_says_it_gave_up(
    caplog,
) -> None:
    host = CliHost(config=None, store=None)
    host.engine = engine = _RefusingEngine()
    await host._report(_worker("t1"), "finished", "ok")
    engine.refusing = False
    await host.aclose()
    assert engine.accepted == ["t1"]

    host = CliHost(config=None, store=None)
    host.engine = _RefusingEngine()
    await host._report(_worker("t2"), "finished", "ok")
    with caplog.at_level(logging.WARNING):
        await host.aclose()
    assert "result of t2 was never delivered" in caplog.text
