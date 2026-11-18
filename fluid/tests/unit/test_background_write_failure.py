"""A failed lifecycle write leaves a background task retryable, never lost.

Each background mutator writes its SubagentEvent first and changes the
engine's in-memory task state only after that write returns. A store that
refuses the write (or raises after the record landed) therefore leaves the task
where the durable record says it is, so the host's resubmission completes it and
the parent is woken exactly once, in this engine and after a restart.

A terminal record that landed although its write raised is read back from the
store and applied then, published once. Held completions are finalized again on
every lifecycle access, so a failure while draining them is retried. The CLI
host retries a completion the engine refused a bounded number of times.
"""

from __future__ import annotations

import contextlib
import logging

import pytest
from fluid.cli import host as host_mod
from fluid.cli.host import CliHost, Worker
from fluid.events import BackgroundTaskFinished, SessionWoken, SubagentEvent, UserTurn
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

    await loop.submit(done)  # a later duplicate is still a no-op
    await loop.wait_turn()
    assert provider.calls == 3
    assert len(_finished(await store.events_since(SID, 0))) == 1
    await loop.aclose()
    assert await _restart_wakes(tmp_path) == 1


async def test_a_terminal_write_that_landed_is_applied_and_published_once(tmp_path) -> None:
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
    await loop.submit(done)  # the record is durable, so the completion is accepted
    await loop.wait_turn()
    assert provider.calls == 3
    assert "The answer is 42." in provider.requests[-1].messages[-1].text()

    await loop.submit(done)  # a duplicate from the host stays a no-op
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


class _RefusingEngine:
    def __init__(self, refusals: int) -> None:
        self.refusals = refusals
        self.calls = 0

    async def submit(self, op) -> None:
        self.calls += 1
        if self.calls <= self.refusals:
            raise OSError(28, "No space left on device")


async def test_cli_host_retries_a_completion_the_engine_refused(monkeypatch) -> None:
    monkeypatch.setattr(host_mod, "_REPORT_RETRY_BASE_SECONDS", 0.0, raising=False)
    host = CliHost(config=None, store=None)
    host.engine = _RefusingEngine(refusals=2)
    worker = Worker(task_id="t", kind="monitor", description="x")
    await host._report(worker, "finished", "ok")
    assert host.engine.calls == 3
    assert worker.status == "finished"


async def test_cli_host_gives_up_after_a_bounded_number_of_attempts(
    monkeypatch, caplog
) -> None:
    monkeypatch.setattr(host_mod, "_REPORT_RETRY_BASE_SECONDS", 0.0, raising=False)
    host = CliHost(config=None, store=None)
    host.engine = _RefusingEngine(refusals=100)
    worker = Worker(task_id="t", kind="monitor", description="x")
    with caplog.at_level(logging.WARNING):
        await host._report(worker, "failed", "boom")
    assert host.engine.calls == 3
    assert worker.status == "failed"
    assert "could not deliver result of t" in caplog.text
