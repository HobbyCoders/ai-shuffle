"""A lock timeout on an event write must not end the turn (#1065).

The server's own 55P03 answer means the transaction rolled back, so only that
error is re-sent. Any other failure may have committed and recorder events have
no stable id, so those keep the never-retry rule. The SQLite shim drives the
recorder here; tests/db/test_event_write_lock_timeout_postgres.py proves the
PostgreSQL behaviour these rules rest on.
"""
import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock, Mock, patch

import psycopg2
import pytest
from psycopg2.errors import LockNotAvailable

from app.core import provider_agent as pa
from app.core import turn_recorder as tr
from app.core.turn_recorder import TurnRecorder, record_drained_event
from app.db import base
from app.db import session_events as se
from app.db import sessions as sessions_db

SID = "sess-lock-timeout"
BIG = 3000  # over FLUSH_MAX_BUFFER_CHARS: the delta flushes as its own batch
REAL_CREATE_FOR_RUN = TurnRecorder.create_for_run


def _lock_timeout() -> LockNotAvailable:
    return LockNotAvailable("canceling statement due to lock timeout")


@pytest.fixture(autouse=True)
def _session(mock_db):
    sessions_db.create_session(SID, "default")


@pytest.fixture
def no_backoff(monkeypatch):
    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (0, 0, 0))


def _fail_first(monkeypatch, failures):
    """Raise each of ``failures`` on successive appends, then write for real."""
    real = se.append_events
    remaining = list(failures)
    calls = Mock()

    def append(*args, **kwargs):
        calls()
        if remaining:
            raise remaining.pop(0)
        return real(*args, **kwargs)

    monkeypatch.setattr(se, "append_events", append)
    return calls


async def _until_called(calls: Mock, times: int = 1) -> None:
    """Wait until the append mock has refused a batch, so its re-send is pending."""
    while calls.call_count < times:
        await asyncio.sleep(0.001)
    await asyncio.sleep(0.01)


def _delta(text: str) -> dict:
    return {"type": "stream_delta", "delta_type": "text", "content": text}


def _texts() -> list[str]:
    """Each recorded delta, identified by its first character."""
    return [str(row["payload"]["text"])[0] for row in se.get_events(SID)
            if row["event_type"] == "assistant.text_delta"]


async def _started() -> TurnRecorder:
    recorder = await TurnRecorder.maybe_create(SID)
    await recorder.start("hello")
    recorder.on_recording_failed = Mock()
    return recorder


async def test_lock_timeout_is_resent_once_without_ending_the_turn(monkeypatch, no_backoff):
    recorder = await _started()
    calls = _fail_first(monkeypatch, [_lock_timeout(), _lock_timeout()])

    await recorder.on_event(_delta("A" * BIG))

    assert calls.call_count == 3
    assert _texts() == ["A"]
    assert not recorder.recording_failed
    recorder.on_recording_failed.assert_not_called()
    await recorder.on_event({"type": "done", "metadata": {}})
    await recorder.close()
    assert "turn.completed" in [row["event_type"] for row in se.get_events(SID)]


async def test_later_events_wait_behind_a_batch_being_resent(monkeypatch):
    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (0.05,))
    recorder = await _started()
    calls = _fail_first(monkeypatch, [_lock_timeout()])

    first = asyncio.create_task(recorder.on_event(_delta("A" * BIG)))
    await _until_called(calls)
    await recorder.on_event(_delta("B" * BIG))
    await first

    assert _texts() == ["A", "B"]
    seqs = [row["seq"] for row in se.get_events(SID)]
    assert seqs == list(range(seqs[0], seqs[0] + len(seqs)))
    await recorder.close()


async def test_lock_timeouts_past_the_budget_end_the_turn_once(monkeypatch, no_backoff):
    recorder = await _started()
    calls = _fail_first(monkeypatch, [_lock_timeout() for _ in range(50)])

    await recorder.on_event(_delta("A" * BIG))
    await recorder.on_event(_delta("B" * BIG))

    assert calls.call_count == len(tr._LOCK_RETRY_DELAYS_SECONDS) + 1
    assert recorder.recording_failed
    recorder.on_recording_failed.assert_called_once_with()
    assert _texts() == []
    await recorder.close()


@pytest.mark.parametrize("failure", [
    psycopg2.OperationalError("server closed the connection unexpectedly"),
    psycopg2.InterfaceError("connection already closed"),
    psycopg2.errors.QueryCanceled("canceling statement due to statement timeout"),
    RuntimeError("anything else"),
], ids=lambda failure: type(failure).__name__)
async def test_an_unknown_outcome_is_never_resent(monkeypatch, no_backoff, failure):
    recorder = await _started()
    calls = _fail_first(monkeypatch, [failure])

    await recorder.on_event(_delta("A" * BIG))

    assert calls.call_count == 1
    assert recorder.recording_failed
    recorder.on_recording_failed.assert_called_once_with()
    await recorder.close()


async def test_a_lost_acknowledgement_after_commit_does_not_duplicate(monkeypatch, no_backoff):
    """The rows committed and only the reply was lost: the case the rule protects."""
    recorder = await _started()
    real = se.append_events
    calls = Mock()

    def committed_then_lost(*args, **kwargs):
        calls()
        real(*args, **kwargs)
        raise psycopg2.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(se, "append_events", committed_then_lost)
    await recorder.on_event(_delta("A" * BIG))

    assert calls.call_count == 1
    assert _texts() == ["A"]
    assert recorder.recording_failed
    await recorder.close()


async def test_a_resend_still_answers_to_the_fence(monkeypatch, no_backoff):
    recorder = await _started()
    recorder.on_superseded = Mock()
    violation = se.FenceViolation("token moved during the back-off")
    calls = _fail_first(monkeypatch, [_lock_timeout(), violation])

    await recorder.on_event(_delta("A" * BIG))

    assert calls.call_count == 2
    assert recorder._superseded
    assert not recorder.recording_failed
    assert _texts() == []
    await recorder.close()


async def test_a_resend_rechecks_a_restarted_task(monkeypatch, no_backoff):
    """The registry's incarnation check re-runs on each attempt, not once up front."""
    from app.core.task_registry import task_registry

    recorder = await _started()
    answers = iter([True, False])
    monkeypatch.setattr(task_registry, "matches_task_incarnation", lambda *_: next(answers))
    calls = _fail_first(monkeypatch, [_lock_timeout()])

    await recorder._append([{"type": "task.updated", "payload": {"task_id": "t"}}])

    assert calls.call_count == 1
    assert not recorder.recording_failed
    assert "task.updated" not in [row["event_type"] for row in se.get_events(SID)]
    await recorder.close()


async def test_cancelling_the_writer_during_the_backoff_keeps_the_batch_in_order(monkeypatch):
    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (0.05,))
    recorder = await _started()
    calls = _fail_first(monkeypatch, [_lock_timeout()])

    pump = asyncio.create_task(recorder.on_event(_delta("A" * BIG)))
    await _until_called(calls)
    pump.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pump
    await recorder.on_event(_delta("B" * BIG))

    await recorder.close()

    rows = [row["event_type"] for row in se.get_events(SID)]
    assert _texts() == ["A", "B"]
    assert rows.index("assistant.text_delta") < rows.index("turn.aborted")
    assert not recorder.recording_failed


async def test_cancelling_close_during_a_backoff_still_writes_the_final_batch(monkeypatch):
    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (0.05,))
    recorder = await _started()
    await recorder.on_event(_delta("A"))
    calls = _fail_first(monkeypatch, [_lock_timeout()])

    closing = asyncio.create_task(recorder.close())
    await _until_called(calls)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await recorder.close()  # a second close waits for the first; it does not skip it

    rows = [row["event_type"] for row in se.get_events(SID)]
    assert _texts() == ["A"]
    assert rows.index("assistant.text_delta") < rows.index("turn.aborted")
    assert not recorder.recording_failed
    recorder.on_recording_failed.assert_not_called()
    assert calls.call_count == 2


async def test_cancelling_close_past_the_budget_reports_the_loss(monkeypatch, no_backoff):
    recorder = await _started()
    await recorder.on_event(_delta("A"))
    monkeypatch.setattr(se, "append_events", Mock(side_effect=_lock_timeout()))
    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (0.02, 0.02))

    closing = asyncio.create_task(recorder.close())
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    closing.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closing
    await recorder.writes_settled()

    assert recorder.recording_failed
    recorder.on_recording_failed.assert_called_once()


async def test_an_unknown_outcome_inside_a_cancelled_close_is_not_resent(monkeypatch, no_backoff):
    recorder = await _started()
    await recorder.on_event(_delta("A"))
    calls = _fail_first(monkeypatch, [psycopg2.OperationalError("server closed the connection")])

    closing = asyncio.create_task(recorder.close())
    await _until_called(calls)
    closing.cancel()
    await asyncio.gather(closing, return_exceptions=True)
    await recorder.writes_settled()

    assert calls.call_count == 1
    assert recorder.recording_failed
    assert _texts() == []


async def test_a_write_cancelled_by_process_teardown_is_recorded_as_lost(monkeypatch):
    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (30,))
    recorder = await _started()
    calls = _fail_first(monkeypatch, [_lock_timeout()])

    pump = asyncio.create_task(recorder.on_event(_delta("A" * BIG)))
    await _until_called(calls)
    recorder._write_task.cancel()
    await asyncio.gather(pump, return_exceptions=True)

    assert recorder.recording_failed
    recorder.on_recording_failed.assert_called_once()
    await recorder.close()


async def test_shutdown_drain_waits_for_a_write_that_outlived_its_caller(monkeypatch):
    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (0.05,))
    recorder = await _started()
    calls = _fail_first(monkeypatch, [_lock_timeout()])
    pump = asyncio.create_task(recorder.on_event(_delta("A" * BIG)))
    await _until_called(calls)
    pump.cancel()
    await asyncio.gather(pump, return_exceptions=True)
    assert _texts() == []

    await tr.drain_event_writes(5)

    assert _texts() == ["A"]
    await recorder.close()


async def test_a_cancelled_close_releases_the_lease_only_after_its_write(monkeypatch):
    """The fence would refuse a re-send once the lease is gone."""
    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (0.05,))
    recorder = await _started()
    calls = _fail_first(monkeypatch, [_lock_timeout()])
    releases = []
    real_release = tr.runs_db.release_run
    monkeypatch.setattr(tr.runs_db, "release_run", lambda *a: (
        releases.append(len(_texts())), real_release(*a))[1])
    await recorder.on_event(_delta("A"))
    closing = asyncio.create_task(recorder.close())
    await _until_called(calls)
    closing.cancel()
    await asyncio.gather(closing, return_exceptions=True)
    await recorder.writes_settled()

    assert releases == [1]


# ---------------------------------------------------------------------------
# Writes between turns: the caller waits one attempt, the batch waits the budget
# ---------------------------------------------------------------------------

async def test_an_unfenced_caller_moves_on_at_the_first_lock_timeout(monkeypatch):
    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (0.05,))
    calls = _fail_first(monkeypatch, [_lock_timeout()])

    await tr.record_interrupting(SID, "stop")
    assert calls.call_count == 1
    assert "turn.interrupting" not in [row["event_type"] for row in se.get_events(SID)]

    await tr.drain_event_writes(5)
    assert "turn.interrupting" in [row["event_type"] for row in se.get_events(SID)]


async def test_cancelling_an_unfenced_caller_does_not_lose_its_batch(monkeypatch):
    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (0.05,))
    gate = threading.Event()
    real = se.append_events

    def slow(*args, **kwargs):
        gate.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(se, "append_events", slow)
    caller = asyncio.create_task(tr.record_parked(SID, ["t1"]))
    await asyncio.sleep(0.01)
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    gate.set()

    await tr.drain_event_writes(5)
    assert "turn.parked" in [row["event_type"] for row in se.get_events(SID)]


async def test_an_unfenced_write_cancelled_by_teardown_logs_the_loss(monkeypatch, caplog):
    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (30,))
    _fail_first(monkeypatch, [_lock_timeout()])
    await tr.record_interrupting(SID, "stop")

    tr._unfenced_tails[SID].task.cancel()
    await asyncio.sleep(0.01)

    assert "lost: write cancelled" in caplog.text


async def test_a_write_queued_behind_a_resend_does_not_hold_its_caller(monkeypatch):
    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (0.2,))
    _fail_first(monkeypatch, [_lock_timeout()])

    await tr.record_interrupting(SID, "stop")
    began = time.monotonic()
    await tr.record_parked(SID, ["t1"])
    assert time.monotonic() - began < 0.1

    await tr.drain_event_writes(5)
    types = [row["event_type"] for row in se.get_events(SID)]
    assert types.index("turn.interrupting") < types.index("turn.parked")


async def test_a_turn_event_stays_behind_a_between_turns_batch_being_resent(monkeypatch):
    """The terminal event cannot overtake the Stop intent that is still being re-sent."""
    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (0.05,))
    recorder = await _started()
    _fail_first(monkeypatch, [_lock_timeout()])

    await tr.record_interrupting(SID, "stop")
    await recorder.close()

    types = [row["event_type"] for row in se.get_events(SID)]
    assert types.index("turn.interrupting") < types.index("turn.aborted")


# ---------------------------------------------------------------------------
# Stop reaches the provider whatever the event log is doing
# ---------------------------------------------------------------------------

async def _interrupt_with_a_stuck_log(monkeypatch, mode="stop", *, stuck_prompts=False):
    """Run the real interrupt with event writes wedged until the signal has gone out."""
    from app.core import session_runtime as sr
    from app.db import turns as turns_db

    monkeypatch.setattr(sr, "INTERRUPT_STEP_WAIT_SECONDS", 0.02)
    release = threading.Event()
    real = se.append_events

    def stuck(*args, **kwargs):
        release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(se, "append_events", stuck)
    turn = turns_db.enqueue_turn(SID, "user", prompt="hi")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.interrupt_turn(SID, mode)
    turns_db.settle_turn(turn["id"], "aborted", "racer")

    signalled_at = []
    began = time.monotonic()
    signal = AsyncMock(side_effect=lambda _sid: signalled_at.append(time.monotonic() - began))

    async def cancel_prompts(_sid):
        if stuck_prompts:
            await asyncio.to_thread(release.wait, 5)

    with patch("app.core.session_runtime._should_evict_after_escalation", return_value=False), \
            patch("app.core.query_session_lifecycle.interrupt_session", new=signal), \
            patch.object(sr.permission_handler, "cancel_all_requests", new=cancel_prompts):
        await sr.SessionRuntime(turn).interrupt(mode, escalation_timeout=0.02)
    release.set()
    await tr.drain_event_writes(5)
    return signalled_at


@pytest.mark.parametrize("mode", ["stop", "send_now"])
async def test_a_stuck_event_write_does_not_hold_the_provider_signal(monkeypatch, mode):
    signalled_at = await _interrupt_with_a_stuck_log(monkeypatch, mode)

    assert len(signalled_at) == 1 and signalled_at[0] < 1
    assert "turn.interrupting" in [row["event_type"] for row in se.get_events(SID)]


async def test_stuck_prompt_cancellation_does_not_hold_the_provider_signal(monkeypatch):
    signalled_at = await _interrupt_with_a_stuck_log(monkeypatch, stuck_prompts=True)

    assert len(signalled_at) == 1 and signalled_at[0] < 1


async def test_a_failure_after_an_unfenced_caller_was_cancelled_is_logged(monkeypatch, caplog):
    gate = threading.Event()
    failed = Mock()

    def slow_then_lost(*args, **kwargs):
        gate.wait(5)
        failed()
        raise psycopg2.OperationalError("server closed the connection unexpectedly")

    monkeypatch.setattr(se, "append_events", slow_then_lost)
    caller = asyncio.create_task(tr.record_parked(SID, ["t1"]))
    await asyncio.sleep(0.01)
    caller.cancel()
    await asyncio.gather(caller, return_exceptions=True)
    gate.set()

    await tr.drain_event_writes(5)
    failed.assert_called_once()  # an unknown outcome is never re-sent
    assert "between-turns event(s) for sess-lock-timeout lost" in caplog.text


async def _forced_end_with_a_stuck_write(monkeypatch, *, mid_reply=False):
    """A real runtime, recorder and lease: force-end it while its last write re-sends."""
    from tests.core.test_session_runtime import _runtime_env

    from app.core import session_runtime as sr
    from app.db import runs as runs_db
    from app.db import turns as turns_db

    monkeypatch.setattr(tr, "_LOCK_RETRY_DELAYS_SECONDS", (0.02,) * 200)
    held = threading.Event()
    refused = Mock()
    real = se.append_events

    def append(*args, **kwargs):
        if held.is_set():
            refused()
            raise _lock_timeout()
        return real(*args, **kwargs)

    monkeypatch.setattr(se, "append_events", append)

    async def pump(**_kwargs):
        held.set()
        yield _delta("kept")
        if mid_reply:
            await asyncio.Event().wait()
        yield {"type": "done", "metadata": {}}

    row = turns_db.enqueue_turn(SID, "user", prompt="hi", profile_id="default")
    lease = {}
    real_release = runs_db.release_run

    def release(run_id, fence_token):
        lease["writes_held_at_release"] = held.is_set()
        return real_release(run_id, fence_token)

    monkeypatch.setattr(runs_db, "release_run", release)
    with _runtime_env(pump=pump), patch.object(
        sr.TurnRecorder, "create_for_run", REAL_CREATE_FOR_RUN,
    ):
        execution = asyncio.create_task(sr.execute(row))
        if not mid_reply:
            await _until_called(refused, 2)
        else:
            await asyncio.to_thread(held.wait, 5)
            await asyncio.sleep(0.05)
        await sr.force_end_executions({SID}, grace=0.05)
        lease["active_while_stuck"] = bool(runs_db.get_active_run(SID))
        lease["ran_before_release"] = "writes_held_at_release" not in lease
        held.clear()
        await tr.drain_event_writes(5)
        await asyncio.gather(execution, return_exceptions=True)
    lease["active_after"] = bool(runs_db.get_active_run(SID))
    return lease


async def test_a_forced_end_keeps_the_lease_until_its_writes_land(monkeypatch):
    lease = await _forced_end_with_a_stuck_write(monkeypatch)

    assert lease["ran_before_release"] and lease["active_while_stuck"]
    assert lease["writes_held_at_release"] is False
    assert not lease["active_after"]
    assert _texts() == ["k"]


async def test_a_forced_end_mid_reply_keeps_the_text_and_the_terminal_event(monkeypatch):
    lease = await _forced_end_with_a_stuck_write(monkeypatch, mid_reply=True)

    assert lease["ran_before_release"] and lease["active_while_stuck"]
    assert lease["writes_held_at_release"] is False
    assert _texts() == ["k"]
    assert se.get_events(SID)[-1]["event_type"] == "turn.aborted"


@pytest.fixture
def stop_env(monkeypatch):
    """A real Stop with its own stops recorded instead of performed."""
    from app.core import query_session_lifecycle as qsl
    from app.core.task_registry import task_registry

    stopped: list[str] = []

    async def stop_session_task(_sid, task_id, **_kwargs):
        stopped.append(task_id)
        return {"ok": True, "task_id": task_id}

    monkeypatch.setattr(qsl, "stop_session_task", stop_session_task)
    monkeypatch.setattr("app.core.session_pool.get_session_pool",
                        lambda: type("Pool", (), {"peek": staticmethod(lambda _sid: None)})())
    task_registry.clear_user_stop(SID)
    task_registry.start_task(SID, "old-task", "agent", "old-task")
    yield stopped
    task_registry.clear_user_stop(SID)
    task_registry.clear_session(SID)


def _pause_first_call(monkeypatch, module, name):
    """Hold the first off-loop call of ``module.name`` until told, as a stuck lock would."""
    resume = threading.Event()
    reached = threading.Event()
    real = getattr(module, name)

    def paused(*args, **kwargs):
        if threading.current_thread() is not threading.main_thread() and not reached.is_set():
            reached.set()
            resume.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, name, paused)
    return reached, resume


def _next_message_arrives() -> str:
    """What the person's next message does, in its order: lift the latch, enqueue."""
    from app.core.task_registry import task_registry
    from app.db import turns as turns_db
    from app.db import wakeups as wakeups_db

    task_registry.clear_user_stop(SID)
    task_registry.start_task(SID, "new-task", "agent", "new-task")
    wakeups_db.create_wakeup(
        id="newer-wake", session_id=SID, prompt="newer wake", fire_at=time.time() + 60,
    )
    return turns_db.enqueue_turn(SID, "user", prompt="next", profile_id="default")["id"]


async def test_a_stop_paused_at_the_wakeups_spares_the_next_turns_work(monkeypatch, stop_env):
    """The whole interrupt returns with its teardown still paused, as a held lock would."""
    from app.core import session_runtime as sr
    from app.db import turns as turns_db
    from app.db import wakeups as wakeups_db

    monkeypatch.setattr(sr, "INTERRUPT_STEP_WAIT_SECONDS", 0.01)
    reached, resume = _pause_first_call(monkeypatch, wakeups_db, "pop_session_wakeups")
    turn = turns_db.enqueue_turn(SID, "user", prompt="hi", profile_id="default")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.interrupt_turn(SID, "stop", stop_background=True, source="user_stop")

    async def provider_stops(_sid):
        turns_db.settle_turn(turn["id"], "aborted", "interrupted")

    with patch("app.core.query_session_lifecycle.interrupt_session", new=provider_stops):
        assert await sr.SessionRuntime(turn).interrupt("stop", escalation_timeout=1) == "aborted"
    assert reached.is_set()

    next_turn = _next_message_arrives()
    resume.set()
    await tr.drain_event_writes(5)

    assert stop_env == ["old-task"]
    assert wakeups_db.get_session_wakeups(SID)
    assert turns_db.get_turn(next_turn)["state"] == "queued"


async def test_a_stop_paused_at_the_queue_read_leaves_the_new_queue_alone(monkeypatch, stop_env):
    from app.core import query_session_lifecycle as qsl
    from app.db import turns as turns_db

    reached, resume = _pause_first_call(monkeypatch, turns_db, "get_queued_turns")
    stop = asyncio.create_task(qsl.stop_all_background_tasks(SID, user_stop=True))
    await asyncio.to_thread(reached.wait, 5)

    next_turn = _next_message_arrives()
    resume.set()
    await stop

    assert stop_env == ["old-task"]
    assert turns_db.get_turn(next_turn)["state"] == "queued"


async def test_a_stop_still_removes_what_was_queued_before_it(stop_env):
    from app.core import query_session_lifecycle as qsl
    from app.db import turns as turns_db

    queued = turns_db.enqueue_turn(SID, "user", prompt="older", profile_id="default")["id"]

    await qsl.stop_all_background_tasks(SID, user_stop=True)

    assert stop_env == ["old-task"]
    assert turns_db.get_turn(queued)["state"] != "queued"


async def test_a_stop_outlived_by_the_next_message_reaps_only_the_shells_it_saw(monkeypatch, stop_env):
    from app.core import provider_agent_supervisor as sup
    from app.core import query_session_lifecycle as qsl
    from app.db import turns as turns_db
    from app.mcp import builtin_tools

    reached, resume = _pause_first_call(monkeypatch, turns_db, "get_queued_turns")
    monkeypatch.setattr(builtin_tools, "session_background_shell_ids", lambda _sid: {"old-shell"})
    reap = AsyncMock(return_value=0)
    monkeypatch.setattr(builtin_tools, "stop_session_background_shells", reap)
    sup.register_worker(sup.WorkerRecord(
        parent_session_id=SID, worker_session_id="worker-a", task_id="old-task", tool_use_id="old-task",
    ))
    try:
        stop = asyncio.create_task(qsl.stop_all_background_tasks(SID, user_stop=True))
        await asyncio.to_thread(reached.wait, 5)
        _next_message_arrives()
        resume.set()
        await stop
    finally:
        sup.reset()

    reap.assert_awaited_once_with("worker-a", only={"old-shell"})


def test_a_wake_that_arrives_after_the_read_is_not_popped():
    from app.db import wakeups as wakeups_db

    wakeups_db.create_wakeup(
        id="older-wake", session_id=SID, prompt="older wake", fire_at=time.time() + 60,
    )
    answers = iter([True, False])

    assert wakeups_db.pop_session_wakeups(SID, can_pop=lambda: next(answers)) == []
    assert wakeups_db.get_session_wakeups(SID)


async def test_a_stop_overtaken_by_a_newer_turn_does_not_signal_or_halt_it(monkeypatch):
    from app.core import session_runtime as sr
    from app.db import turns as turns_db

    monkeypatch.setattr(sr, "INTERRUPT_STEP_WAIT_SECONDS", 0.01)
    turn = turns_db.enqueue_turn(SID, "user", prompt="hi", profile_id="default")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.interrupt_turn(SID, "stop", stop_background=True, source="user_stop")
    newer = sr.SessionRuntime(turn)

    async def stop_all(*_args, **_kwargs):
        sr._live_runtimes[SID] = newer

    signal = AsyncMock()
    halt = Mock()
    try:
        with patch("app.core.query_session_lifecycle.stop_all_background_tasks", new=stop_all), \
                patch("app.core.query_session_lifecycle.interrupt_session", new=signal), \
                patch("app.mcp.computer_use.abort.request_stop", new=halt):
            old = sr.SessionRuntime(turn)
            sr._live_runtimes[SID] = old
            await old.interrupt("stop", escalation_timeout=0.01)
    finally:
        sr._live_runtimes.pop(SID, None)

    signal.assert_not_awaited()
    halt.assert_not_called()


async def test_a_lock_timeout_from_the_question_projection_rolls_back_and_resends(
    monkeypatch, no_backoff,
):
    """record_question shares append_events' transaction, so it shares this fix."""
    from app.db import session_questions

    recorder = await _started()
    before = len(se.get_events(SID))
    real = session_questions.record_question
    failures = [_lock_timeout()]

    def record_question(*args, **kwargs):
        if failures:
            raise failures.pop()
        return real(*args, **kwargs)

    monkeypatch.setattr(session_questions, "record_question", record_question)
    await recorder.on_event(_delta("A" * BIG))

    assert _texts() == ["A"]
    assert len(se.get_events(SID)) == before + 1
    assert not recorder.recording_failed
    await recorder.close()


async def test_a_drained_event_survives_a_lock_timeout(monkeypatch, no_backoff):
    calls = _fail_first(monkeypatch, [_lock_timeout()])

    await record_drained_event(SID, {"type": "system", "subtype": "note", "content": "drained"})
    await tr.drain_event_writes(5)

    assert calls.call_count == 2
    assert len(se.get_events(SID)) == 1


async def test_a_drained_event_is_not_resent_on_an_unknown_outcome(monkeypatch, no_backoff):
    calls = _fail_first(monkeypatch, [psycopg2.OperationalError("connection lost")])

    await record_drained_event(SID, {"type": "system", "subtype": "note", "content": "drained"})

    assert calls.call_count == 1
    assert se.get_events(SID) == []


async def test_a_drained_event_past_the_budget_never_raises(monkeypatch, no_backoff, caplog):
    calls = _fail_first(monkeypatch, [_lock_timeout() for _ in range(50)])

    await record_drained_event(SID, {"type": "system", "subtype": "note", "content": "drained"})
    await tr.drain_event_writes(5)

    assert calls.call_count == len(tr._LOCK_RETRY_DELAYS_SECONDS) + 1
    assert any(record.levelname == "ERROR" and "lost" in record.getMessage() for record in caplog.records)


def test_stable_ids_dedupe_rows_but_events_without_one_duplicate():
    """Why an unknown outcome cannot simply be re-sent: only stable ids dedupe."""
    stable = {"id": "stable-event", "type": "system.note", "payload": {}}
    se.append_events(SID, [stable])
    se.append_events(SID, [stable])
    rows = se.get_events(SID)
    assert [row["seq"] for row in rows] == [1]  # the replay still burned seq 2

    plain = {"type": "system.note", "payload": {}}
    se.append_events(SID, [plain])
    se.append_events(SID, [plain])
    assert [row["seq"] for row in se.get_events(SID)] == [1, 3, 4]


# ---------------------------------------------------------------------------
# The wake follows the commit
# ---------------------------------------------------------------------------

@pytest.fixture
def wakes(monkeypatch):
    sent: list[tuple] = []
    monkeypatch.setattr(se, "send_wake", lambda *args, **kwargs: sent.append((args, kwargs)))
    return sent


def test_self_owned_append_wakes_once_after_it_commits(monkeypatch):
    seen = []

    def send(session_id, max_seq, **_):
        # A reader woken now must already see the row it was woken for.
        seen.append((max_seq, se.get_max_seq(session_id), len(se.get_events(session_id))))

    monkeypatch.setattr(se, "send_wake", send)
    _, last = se.append_events(SID, [{"type": "system.note", "payload": {}}])
    assert seen == [(last, last, 1)]


def test_failed_append_sends_no_wake(wakes, monkeypatch):
    with pytest.raises(ValueError):
        se.append_events(SID, [])
    assert wakes == []


def test_caller_owned_append_wakes_only_after_the_caller_commits(wakes):
    with base.get_db() as conn:
        _, last = se.append_events(SID, [{"type": "system.note", "payload": {}}], _conn=conn)
        assert wakes == []
    assert wakes == [((SID, last), {})]


def test_caller_owned_append_sends_no_wake_on_rollback(wakes):
    with pytest.raises(RuntimeError, match="caller failed"):
        with base.get_db() as conn:
            se.append_events(SID, [{"type": "system.note", "payload": {}}], _conn=conn)
            raise RuntimeError("caller failed")
    assert wakes == []


def test_backfill_wakes_after_it_commits(wakes):
    _, last = se.append_backfill_events(SID, [{"type": "system.note", "payload": {}}])
    assert wakes == [((SID, last), {})]


def test_refused_backfill_sends_no_wake(wakes):
    se.append_events(SID, [{"type": "system.note", "payload": {}}])
    wakes.clear()
    assert se.append_backfill_events(SID, [{"type": "system.note", "payload": {}}]) is None
    assert wakes == []


def test_a_failed_wake_cannot_fail_the_append(monkeypatch):
    def refused():
        raise psycopg2.pool.PoolError("connection pool exhausted")

    real = se.append_events
    monkeypatch.setattr(se, "get_db", refused)
    assert real(SID, [{"type": "system.note", "payload": {}}]) == (1, 1)
    monkeypatch.undo()
    assert len(se.get_events(SID)) == 1


def test_an_unsendable_wake_still_reaches_this_process(monkeypatch):
    from app.core import event_gateway

    delivered = []
    gateway = Mock(_handle_payload=delivered.append)
    monkeypatch.setattr(event_gateway, "get_gateway", lambda: gateway)

    def refused():
        raise psycopg2.pool.PoolError("connection pool exhausted")

    monkeypatch.setattr(se, "get_db", refused)
    se.send_wake(SID, 7, recheck_log=True)

    assert [json.loads(payload) for payload in delivered] == [
        {"session_id": SID, "max_seq": 7, "recheck_log": True},
    ]


def test_a_broken_local_wake_is_still_swallowed(monkeypatch):
    from app.core import event_gateway

    def refused():
        raise psycopg2.pool.PoolError("connection pool exhausted")

    monkeypatch.setattr(se, "get_db", refused)
    monkeypatch.setattr(event_gateway, "get_gateway", Mock(side_effect=RuntimeError("no gateway")))
    se.send_wake(SID, 7)


def test_the_shim_has_no_pg_notify_and_does_not_fake_a_local_wake(monkeypatch):
    from app.core import event_gateway

    get_gateway = Mock()
    monkeypatch.setattr(event_gateway, "get_gateway", get_gateway)
    se.send_wake(SID, 7)
    get_gateway.assert_not_called()


# ---------------------------------------------------------------------------
# The parent's failure message
# ---------------------------------------------------------------------------

def test_parent_is_told_a_database_write_failed_and_the_worker_can_resume(monkeypatch):
    from app.db import turns as turns_db

    monkeypatch.setattr(turns_db, "get_turn", lambda tid: {
        "id": tid, "state": "aborted", "stop_reason": "recording_failed",
    })
    message = pa._no_terminal_event_error("worker-1", "turn-1")
    assert "database write failure" in message
    assert "resumed" in message
    assert "SendMessage" in message
    assert "stop_reason=recording_failed" in message
    assert "worker-1" in message


def test_other_missing_terminals_keep_the_general_message(monkeypatch):
    from app.db import turns as turns_db

    monkeypatch.setattr(turns_db, "get_turn", lambda tid: {
        "id": tid, "state": "aborted", "stop_reason": "lease_reaped",
    })
    message = pa._no_terminal_event_error("worker-1", "turn-1")
    assert message.startswith("The worker produced no terminal turn event")
    assert "database" not in message
