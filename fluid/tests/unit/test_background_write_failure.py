"""A failed lifecycle write leaves a background task retryable, never lost.

Each background mutator writes its SubagentEvent first and changes the
engine's in-memory task state only after that write returns. A store that
refuses the write (or raises after the record landed) therefore leaves the task
where the durable record says it is, so the host's resubmission completes it and
the parent is woken exactly once, in this engine and after a restart.
"""

from __future__ import annotations

import pytest

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


@pytest.mark.parametrize("landed", [False, True])
async def test_failed_terminal_write_is_retried_and_wakes_exactly_once(
    tmp_path, landed
) -> None:
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
    store.fail, store.landed = _kind("finished"), landed
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
    await loop.aclose()

    # A landed first write left two terminal records; a restart honours one.
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
