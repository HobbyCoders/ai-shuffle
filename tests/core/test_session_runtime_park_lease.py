"""SessionRuntime: the park -> unpark hand-off, the run-lease heartbeat, and the
WS tool-input cap the chat-socket docs promise ("replaced by a truncation
marker when over 4 KB").

The turns/runs tables are real (pg_shim SQLite); the pump, recorder, idle
reader and task registry are the same mocks ``tests/core/test_session_runtime.py``
uses (``_runtime_env``), so these read as a continuation of that suite.
"""

import asyncio
import logging
from unittest.mock import patch

import pytest

from app.core import session_runtime
from app.db import runs as runs_db
from app.db import turns as turns_db
from tests.core.helpers import make_pump
from tests.core.test_session_runtime import (  # noqa: F401 — autouse fixture
    _clean_module_state,
    _runtime_env,
)

DONE = [{"type": "done", "metadata": {}}]


# ---------------------------------------------------------------------------
# park -> unpark
# ---------------------------------------------------------------------------


async def test_park_then_unpark_settles_the_parked_row_and_logs_it(mock_db, caplog):
    """The ordinary unpark resumes and completes the original parked turn,
    rather than completing an old row and starting a separate logical turn."""
    sid = "park-unpark"
    first = turns_db.enqueue_turn(sid, "user", prompt="launch a worker")
    with _runtime_env(pump=make_pump(DONE), bg_work=[{"id": "worker-1"}]) as env:
        assert await session_runtime.SessionRuntime(first).execute() == "parked"
    parked = turns_db.get_turn(first["id"])
    assert parked["state"] == "parked"
    assert parked["payload"]["task_ids"] == ["worker-1"]
    env["scheduler"].assert_called_once_with(sid, first["id"], "parked")
    env["record_parked"].assert_awaited_once_with(sid, ["worker-1"], turn_id=first["id"], usage={})
    # The lease was released even though the turn parked.
    assert runs_db.get_active_run(sid) is None

    wake = turns_db.enqueue_turn(
        sid, "unpark", prompt="background task completed",
        payload={"task_ids": ["worker-1"]},
    )
    caplog.set_level(logging.INFO, logger="app.core.session_runtime")
    with _runtime_env(pump=make_pump(DONE)) as env:
        assert await session_runtime.SessionRuntime(wake).execute() == "completed"
    env["scheduler"].assert_called_once_with(sid, first["id"], "completed")

    settled = turns_db.get_turn(first["id"])
    assert settled["state"] == "completed"
    assert settled["stop_reason"] is None
    assert turns_db.get_turn(wake["id"])["stop_reason"] == "steered"
    assert "resolved parked turn(s)" not in caplog.text


async def test_unpark_while_other_work_still_runs_leaves_the_park(mock_db, caplog):
    """A wake for ONE finished task resumes the same turn, then parks it
    again on the remaining work instead of creating a second parked row."""
    sid = "park-partial"
    first = turns_db.enqueue_turn(sid, "user", prompt="launch two")
    with _runtime_env(pump=make_pump(DONE), bg_work=[{"id": "a"}, {"id": "b"}]):
        assert await session_runtime.SessionRuntime(first).execute() == "parked"

    wake = turns_db.enqueue_turn(sid, "unpark", prompt="a finished")
    caplog.set_level(logging.INFO, logger="app.core.session_runtime")
    with _runtime_env(pump=make_pump(DONE), bg_work=[{"id": "b"}]):
        assert await session_runtime.SessionRuntime(wake).execute() == "parked"

    assert turns_db.get_turn(first["id"])["state"] == "parked"
    assert turns_db.get_turn(first["id"])["payload"]["task_ids"] == ["b"]
    assert turns_db.get_turn(wake["id"])["stop_reason"] == "steered"
    assert "resolved parked turn(s)" not in caplog.text


async def test_a_failing_parked_settle_never_blocks_the_next_turn(mock_db, caplog):
    sid = "park-settle-fails"
    stale = turns_db.enqueue_turn(sid, "user", prompt="old")
    turns_db.start_turn(stale["id"], run_id="run-old")
    turns_db.park_turn(stale["id"], ["gone"])
    # An incompatible send still exercises the separate-turn cleanup path.
    fresh = turns_db.enqueue_turn(sid, "user", prompt="new", profile_id="different")
    with _runtime_env(pump=make_pump(DONE)), patch.object(
        turns_db, "settle_parked_turns", side_effect=RuntimeError("db blip"),
    ):
        assert await session_runtime.SessionRuntime(fresh).execute() == "completed"
    assert turns_db.get_turn(stale["id"])["state"] == "parked"
    assert f"SessionRuntime: settle_parked_turns failed for {sid}" in caplog.text


async def test_a_failed_park_write_does_not_fire_the_scheduler(mock_db, caplog):
    """``settled_here`` is True only when THIS finalize performed the DB
    transition. A park that did not land reports ``parked`` but leaves the
    scheduler seam alone (the reap sweep owns the row now)."""
    turn = turns_db.enqueue_turn("park-write-fails", "user", prompt="hi")
    with _runtime_env(pump=make_pump(DONE), bg_work=[{"id": "t"}]) as env, patch.object(
        turns_db, "park_turn", side_effect=RuntimeError("db blip"),
    ):
        outcome = await session_runtime.SessionRuntime(turn).execute()
    assert outcome == "parked"
    env["scheduler"].assert_not_called()
    env["record_parked"].assert_not_awaited()
    assert f"SessionRuntime: park_turn failed for {turn['id']}" in caplog.text
    assert runs_db.get_active_run("park-write-fails") is None


async def test_a_failing_parked_event_still_parks(mock_db):
    turn = turns_db.enqueue_turn("park-event-fails", "user", prompt="hi")
    with _runtime_env(pump=make_pump(DONE), bg_work=[{"id": "t"}]) as env:
        env["record_parked"].side_effect = RuntimeError("event log down")
        outcome = await session_runtime.SessionRuntime(turn).execute()
    assert outcome == "parked"
    assert turns_db.get_turn(turn["id"])["state"] == "parked"
    env["scheduler"].assert_called_once_with("park-event-fails", turn["id"], "parked")


async def test_a_user_stopped_turn_never_parks(mock_db):
    """Stop kills the session's work; a turn aborted by it settles instead of
    waiting on background work that is being torn down."""
    turn = turns_db.enqueue_turn("park-stopped", "user", prompt="hi")
    with _runtime_env(
        pump=make_pump([{"type": "interrupted"}]), bg_work=[{"id": "t"}],
    ) as env:
        rt = session_runtime.SessionRuntime(turn)
        rt._abort_reason = "user_stopped"
        outcome = await rt.execute()
    assert outcome == "aborted"
    assert turns_db.get_turn(turn["id"])["state"] == "aborted"
    env["record_parked"].assert_not_awaited()


# ---------------------------------------------------------------------------
# run-lease heartbeat
# ---------------------------------------------------------------------------


def _lease(run_id):
    from app.db.base import get_db

    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT state, heartbeat_at, lease_expires_at FROM runs WHERE id=%s", (run_id,)
        )
        return dict(cur.fetchone())


async def test_the_heartbeat_renews_the_lease_while_the_turn_runs(mock_db, monkeypatch):
    sid = "lease-renew"
    turn = turns_db.enqueue_turn(sid, "user", prompt="long turn")
    monkeypatch.setattr(session_runtime, "RUN_LEASE_RENEW_SECONDS", 0.02)
    renewals = []
    real_renew = runs_db.renew_run
    enough = asyncio.Event()
    seen = {}

    def _renew(*args, **kwargs):
        ok = real_renew(*args, **kwargs)
        renewals.append(ok)
        if len(renewals) >= 3:
            enough.set()
        return ok

    async def pump(**kwargs):
        run_id = turns_db.get_turn(turn["id"])["run_id"]
        seen["before"] = _lease(run_id)
        await asyncio.wait_for(enough.wait(), 3.0)
        seen["after"] = _lease(run_id)
        seen["run_id"] = run_id
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=pump), patch.object(runs_db, "renew_run", side_effect=_renew), \
            patch.object(session_runtime.SessionRuntime, "_on_lease_lost") as lost:
        outcome = await session_runtime.execute(turn)

    assert outcome == "completed"
    assert renewals[:3] == [True, True, True]
    lost.assert_not_called()
    # Each True above is the guarded UPDATE matching this runner's active,
    # unexpired run; the row's clocks only move forward (second resolution).
    assert str(seen["after"]["heartbeat_at"]) >= str(seen["before"]["heartbeat_at"])
    assert str(seen["after"]["lease_expires_at"]) >= str(seen["before"]["lease_expires_at"])
    # Released at the end, and the heartbeat stopped with the turn.
    assert _lease(seen["run_id"])["state"] == "released"
    count = len(renewals)
    await asyncio.sleep(0.08)
    assert len(renewals) == count


async def test_a_renewal_error_inside_the_ttl_is_retried_not_fatal(mock_db, monkeypatch, caplog):
    sid = "lease-blip"
    turn = turns_db.enqueue_turn(sid, "user", prompt="long turn")
    monkeypatch.setattr(session_runtime, "RUN_LEASE_RENEW_SECONDS", 0.02)
    calls = []
    real_renew = runs_db.renew_run
    recovered = asyncio.Event()

    def _renew(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("connection reset")
        ok = real_renew(*args, **kwargs)
        if ok:
            recovered.set()
        return ok

    async def pump(**kwargs):
        await asyncio.wait_for(recovered.wait(), 3.0)
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=pump), patch.object(runs_db, "renew_run", side_effect=_renew), \
            patch.object(session_runtime.SessionRuntime, "_on_lease_lost") as lost:
        outcome = await session_runtime.execute(turn)

    assert outcome == "completed"
    lost.assert_not_called()
    assert "SessionRuntime: renew_run raised for run" in caplog.text
    assert turns_db.get_turn(turn["id"])["stop_reason"] != "lease_lost"


@pytest.mark.parametrize("outcome", ["completed", "aborted", "parked"])
@pytest.mark.parametrize("renewal", ["sleeping", "rejected", "error"])
async def test_final_write_stops_heartbeat_without_lease_loss(
    mock_db, monkeypatch, outcome, renewal,
):
    """Hold cleanup open after the real DB transition, including a renewal
    already in flight when the turn becomes terminal/parked."""
    turn = turns_db.enqueue_turn("final-heartbeat", "user", prompt="finish")
    runtime = session_runtime.SessionRuntime(turn)
    written = asyncio.Event()
    started = asyncio.Event()
    renewing = asyncio.Event()
    heartbeat_done = asyncio.Event()
    retried = asyncio.Event()
    rejected = []
    errors = []
    real_loop = runtime._renew_loop
    monkeypatch.setattr(session_runtime, "RUN_LEASE_RENEW_SECONDS", 0.001)

    async def heartbeat(*args):
        started.set()
        try:
            if renewal == "sleeping":
                await written.wait()
            await real_loop(*args)
        finally:
            heartbeat_done.set()

    async def renew(fn, *args):
        renewing.set()
        await written.wait()
        if renewal == "error":
            errors.append(1)
            if len(errors) >= 2:
                retried.set()
            raise RuntimeError("delayed SQL failure")
        ok = await asyncio.to_thread(fn, *args)
        rejected.append(ok)
        return ok

    async def pump(**kwargs):
        await asyncio.wait_for(started.wait(), 3)
        if renewal != "sleeping":
            await asyncio.wait_for(renewing.wait(), 3)
        yield {"type": "interrupted" if outcome == "aborted" else "done", "metadata": {}}

    async def slow_cleanup(*args, **kwargs):
        assert turns_db.get_turn(turn["id"])["state"] == outcome
        written.set()
        if renewal == "error":
            # A transient error is retried (not treated as loss) until
            # execute's final cancel ends the heartbeat.
            await asyncio.wait_for(retried.wait(), 3)
        else:
            # The heartbeat must finish on its own, not via execute's final cancel.
            await asyncio.wait_for(heartbeat_done.wait(), 3)

    with _runtime_env(
        pump=pump, bg_work=[{"id": "background"}] if outcome == "parked" else [],
    ) as env, patch.object(runtime, "_renew_loop", new=heartbeat), patch.object(
        session_runtime, "run_lease", new=renew,
    ), patch.object(runtime, "_snapshot_working_set", new=slow_cleanup), patch.object(
        runtime, "_on_lease_lost", wraps=runtime._on_lease_lost,
    ) as lost, patch.object(
        runtime, "_stop_unowned", wraps=runtime._stop_unowned,
    ) as stop, patch("app.core.query_session_lifecycle.interrupt_session") as interrupt:
        env["record_parked"].side_effect = slow_cleanup
        assert await asyncio.wait_for(runtime.execute(), 5) == outcome

    assert written.is_set()
    assert rejected == ([] if renewal == "error" else [False])
    if renewal == "error":
        assert len(errors) >= 2
    lost.assert_not_called()
    stop.assert_not_called()
    interrupt.assert_not_called()
    assert runtime._abort_reason is None
    assert not runtime._unowned


@pytest.mark.parametrize("outcome", ["completed", "aborted", "parked"])
@pytest.mark.parametrize("renewal", ["success", "rejected", "error"])
async def test_finalizing_tick_before_the_write_keeps_renewing_or_exits_quietly(
    mock_db, monkeypatch, caplog, outcome, renewal,
):
    """The finalizing flag precedes the DB write; it must not stop renewal."""
    turn = turns_db.enqueue_turn("pre-write-heartbeat", "user", prompt="finish")
    runtime = session_runtime.SessionRuntime(turn)
    writing = asyncio.Event()
    tick_ready = asyncio.Event()
    real_to_thread = asyncio.to_thread
    real_loop = runtime._renew_loop
    final_write = turns_db.park_turn if outcome == "parked" else turns_db.settle_turn
    successes = []
    attempts = []
    errors = []
    seen = {}
    monkeypatch.setattr(session_runtime, "RUN_LEASE_RENEW_SECONDS", 0.001)

    async def heartbeat(*args):
        try:
            await real_loop(*args)
        finally:
            if renewal != "success":
                tick_ready.set()

    async def renew(fn, *args):
        # Make this tick land after the flag, but BEFORE the actual DB write.
        await asyncio.wait_for(writing.wait(), 3)
        assert runtime._lease_finalizing
        attempts.append(fn)
        if turns_db.get_turn(turn["id"])["state"] == outcome:
            return await real_to_thread(fn, *args)
        if renewal == "error":
            # Transient errors are retried while finalizing; release the
            # final write once a retry has proven the loop kept going.
            errors.append(1)
            if len(errors) >= 2:
                tick_ready.set()
            raise RuntimeError("SQL failure before final write")
        if renewal == "rejected":
            return False
        ok = await real_to_thread(fn, *args)
        successes.append(ok)
        seen["after"] = _lease(runtime._run_id)
        if len(successes) >= 2:
            tick_ready.set()
        return ok

    async def slow_write(fn, *args, **kwargs):
        if fn is final_write:
            assert runtime._lease_finalizing
            run = runs_db.get_active_run(turn["session_id"])
            # A shorter initial lease makes extension observable even with the
            # shim's second-resolution clocks, without sleeping for a second.
            assert runs_db.renew_run(run["id"], run["runner_id"], run["fence_token"], 1)
            seen["before"] = _lease(run["id"])
            writing.set()
            await asyncio.wait_for(tick_ready.wait(), 3)
        return await real_to_thread(fn, *args, **kwargs)

    frames = [{"type": "interrupted" if outcome == "aborted" else "done", "metadata": {}}]
    with _runtime_env(
        pump=make_pump(frames), bg_work=[{"id": "background"}] if outcome == "parked" else [],
    ), patch.object(runtime, "_renew_loop", new=heartbeat), patch.object(
        session_runtime, "run_lease", new=renew,
    ), patch.object(asyncio, "to_thread", new=slow_write), patch.object(
        runtime, "_on_lease_lost", wraps=runtime._on_lease_lost,
    ) as lost, patch.object(
        runtime, "_stop_unowned", wraps=runtime._stop_unowned,
    ) as stop, patch("app.core.query_session_lifecycle.interrupt_session") as interrupt:
        assert await asyncio.wait_for(runtime.execute(), 5) == outcome

    assert writing.is_set()
    assert tick_ready.is_set()
    assert attempts and all(fn is runs_db.renew_run for fn in attempts)
    if renewal == "success":
        # A successful finalizing tick must continue at the normal cadence.
        assert successes[:2] == [True, True]
        assert str(seen["after"]["lease_expires_at"]) > str(seen["before"]["lease_expires_at"])
    if renewal == "error":
        assert len(errors) >= 2
        assert "SessionRuntime: renew_run raised" in caplog.text
    else:
        assert "SessionRuntime: renew_run" not in caplog.text
    lost.assert_not_called()
    stop.assert_not_called()
    interrupt.assert_not_called()
    assert "kept failing" not in caplog.text
    assert "SessionRuntime: lease renewal lost" not in caplog.text
    assert runtime._abort_reason is None
    assert not runtime._unowned
    assert turns_db.get_turn(turn["id"])["state"] == outcome


@pytest.mark.parametrize("phase", ["background", "adapter"])
@pytest.mark.parametrize("renewal", ["rejected", "expired", "error"])
async def test_stop_during_slow_teardown_does_not_lose_the_lease_again(
    mock_db, monkeypatch, caplog, phase, renewal,
):
    """Stop intent precedes teardown, not just the eventual finalize write."""
    sid = "stop-heartbeat"
    turn = turns_db.enqueue_turn(sid, "provider_agent", prompt="work")
    runtime = session_runtime.SessionRuntime(turn)
    pumping = asyncio.Event()
    stopping = asyncio.Event()
    heartbeat_done = asyncio.Event()
    finish_pump = asyncio.Event()
    real_loop = runtime._renew_loop
    rejected = []
    monkeypatch.setattr(session_runtime, "RUN_LEASE_RENEW_SECONDS", 0.001)
    if renewal == "expired":
        monkeypatch.setattr(session_runtime, "RUN_LEASE_TTL_SECONDS", 0.001)
        monkeypatch.setattr(session_runtime, "RUN_LEASE_RENEW_SECONDS", 0.01)
    elif renewal == "error":
        monkeypatch.setattr(session_runtime, "RUN_LEASE_TTL_SECONDS", 0.02)

    async def pump(**kwargs):
        pumping.set()
        await finish_pump.wait()
        yield {"type": "interrupted", "metadata": {}}

    async def heartbeat(*args):
        await stopping.wait()
        try:
            await real_loop(*args)
        finally:
            heartbeat_done.set()

    async def renew(fn, *args):
        assert runtime._interrupt_driving
        assert not runtime._lease_finalizing
        if renewal == "error":
            await asyncio.sleep(0.03)
            raise RuntimeError("SQL timeout during stop")
        ok = await asyncio.to_thread(fn, *args)
        rejected.append(ok)
        return ok

    async def slow_stop():
        assert runtime._interrupt_driving
        assert not runtime._lease_finalizing
        if renewal == "rejected":
            # The supervisor's killed-worker settlement can beat execute's
            # finalize. Exercise the real producer of a rejected renewal.
            assert turns_db.settle_turn(turn["id"], "aborted", "agent_stopped")
        stopping.set()
        await asyncio.wait_for(heartbeat_done.wait(), 2)
        assert runtime._abort_reason is None
        assert not runtime._unowned
        finish_pump.set()

    async def background(*args, **kwargs):
        if phase == "background":
            await slow_stop()

    async def adapter(*args, **kwargs):
        if phase == "adapter":
            await slow_stop()

    with _runtime_env(pump=pump) as env, patch.object(
        runtime, "_renew_loop", new=heartbeat,
    ), patch.object(session_runtime, "run_lease", new=renew), patch.object(
        runtime, "_on_lease_lost", wraps=runtime._on_lease_lost,
    ) as lost, patch.object(runtime, "_stop_unowned", wraps=runtime._stop_unowned) as unowned, patch(
        "app.core.query_session_lifecycle.stop_all_background_tasks", side_effect=background,
    ), patch("app.core.query_session_lifecycle.interrupt_session", side_effect=adapter) as interrupt:
        execution = asyncio.create_task(runtime.execute())
        try:
            await asyncio.wait_for(pumping.wait(), 2)
            assert turns_db.interrupt_turn(sid, "stop", True, source="agent_stopped")
            assert await asyncio.wait_for(runtime.interrupt("stop"), 3) == "aborted"
            assert await asyncio.wait_for(execution, 3) == "aborted"
        finally:
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)

    assert heartbeat_done.is_set()
    assert rejected == ([False] if renewal == "rejected" else [])
    lost.assert_not_called()
    unowned.assert_not_called()
    interrupt.assert_awaited_once_with(sid)
    assert runtime._abort_reason is None
    assert not runtime._unowned
    settled = turns_db.get_turn(turn["id"])
    assert settled["state"] == "aborted"
    assert settled["stop_reason"] == ("agent_stopped" if renewal == "rejected" else "interrupted")
    assert settled["payload"]["interrupt_source"] == "agent_stopped"
    assert runs_db.get_active_run(sid) is None
    env["record_parked"].assert_not_awaited()
    assert "lease lost" not in caplog.text
    assert "lease renewal lost" not in caplog.text
    assert "past the" not in caplog.text


async def test_finalizing_tick_after_local_ttl_exits_quietly(mock_db, monkeypatch):
    turn = turns_db.enqueue_turn("final-expired-heartbeat", "user", prompt="finish")
    runtime = session_runtime.SessionRuntime(turn)
    runtime._lease_finalizing = True
    monkeypatch.setattr(session_runtime, "RUN_LEASE_TTL_SECONDS", 0.001)
    monkeypatch.setattr(session_runtime, "RUN_LEASE_RENEW_SECONDS", 0.01)
    # Windows' event-loop clock can be coarser than these sub-tick intervals.
    # Advance only this heartbeat's clock instead of relying on a real sleep
    # to have crossed the TTL (asyncio timers may wake one clock tick early).
    from types import SimpleNamespace

    now = [100.0]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    monkeypatch.setattr(session_runtime, "asyncio", SimpleNamespace(
        get_running_loop=lambda: SimpleNamespace(time=lambda: now[0]),
        sleep=sleep, wait_for=asyncio.wait_for,
    ))
    with patch.object(session_runtime, "run_lease") as renew, patch.object(
        runtime, "_on_lease_lost", wraps=runtime._on_lease_lost,
    ) as lost, patch.object(
        runtime, "_stop_unowned", wraps=runtime._stop_unowned,
    ) as stop, patch("app.core.query_session_lifecycle.interrupt_session") as interrupt:
        await asyncio.wait_for(runtime._renew_loop("run", "runner", 1), 1)
    assert sleeps == [0.01]
    renew.assert_not_awaited()
    lost.assert_not_called()
    stop.assert_not_called()
    interrupt.assert_not_called()
    assert not runtime._unowned


@pytest.mark.parametrize("outcome", ["completed", "aborted"])
async def test_finalize_cannot_settle_a_requeued_turn(mock_db, outcome):
    """A finalizing runtime still needs its run binding when it settles."""
    turn = turns_db.enqueue_turn("final-requeue", "user", prompt="finish")
    runtime = session_runtime.SessionRuntime(turn)
    real_settle = turns_db.settle_turn

    def requeue_before_settle(*args, **kwargs):
        assert runtime._lease_finalizing
        assert not runtime._unowned
        assert turns_db.requeue_turn(turn["id"]) is not None
        return real_settle(*args, **kwargs)

    frames = [{"type": "interrupted" if outcome == "aborted" else "done", "metadata": {}}]
    with _runtime_env(pump=make_pump(frames)) as env, patch.object(
        turns_db, "settle_turn", side_effect=requeue_before_settle,
    ) as settle:
        assert await runtime.execute() == outcome

    assert settle.call_args.kwargs["run_id"] == runtime._run_id
    assert turns_db.get_turn(turn["id"])["state"] == "queued"
    env["scheduler"].assert_not_called()


@pytest.mark.parametrize("outcome", ["completed", "aborted", "parked"])
async def test_finalize_cannot_park_or_settle_a_new_owners_turn(mock_db, outcome):
    turn = turns_db.enqueue_turn("final-new-owner", "user", prompt="finish")
    runtime = session_runtime.SessionRuntime(turn)
    final_write = turns_db.park_turn if outcome == "parked" else turns_db.settle_turn
    seen = {}

    def take_over_before_write(*args, **kwargs):
        assert runtime._lease_finalizing
        assert not runtime._unowned
        old_run = runs_db.get_active_run(turn["session_id"])
        assert runs_db.release_run(old_run["id"], old_run["fence_token"])
        assert turns_db.requeue_turn(turn["id"]) is not None
        new_run = runs_db.claim_run(turn["session_id"], "new-runner", 60)
        assert new_run is not None
        assert turns_db.start_turn(turn["id"], run_id=new_run["id"]) is not None
        seen["row"] = turns_db.get_turn(turn["id"])
        seen["run_id"] = new_run["id"]
        return final_write(*args, **kwargs)

    frames = [{"type": "interrupted" if outcome == "aborted" else "done", "metadata": {}}]
    with _runtime_env(
        pump=make_pump(frames), bg_work=[{"id": "old-task"}] if outcome == "parked" else [],
    ) as env, patch.object(
        turns_db, "park_turn" if outcome == "parked" else "settle_turn",
        side_effect=take_over_before_write,
    ) as write:
        assert await runtime.execute() == outcome

    assert write.call_args.kwargs["run_id"] == runtime._run_id
    assert turns_db.get_turn(turn["id"]) == seen["row"]
    assert runs_db.get_active_run(turn["session_id"])["id"] == seen["run_id"]
    env["scheduler"].assert_not_called()
    env["record_parked"].assert_not_awaited()


# ---------------------------------------------------------------------------
# WS tool-input cap (docs: "replaced by a truncation marker when over 4 KB")
# ---------------------------------------------------------------------------


def _input_of_encoded_length(n):
    """A tool input whose ``json.dumps`` is exactly ``n`` characters."""
    overhead = len('{"content": ""}')
    return {"content": "x" * (n - overhead)}


def test_the_ws_tool_input_cap_is_4_kb():
    assert session_runtime._WS_MAX_TOOL_INPUT_CHARS == 4096


@pytest.mark.parametrize("frame_type", ["tool_use", "subagent_tool_use"])
def test_tool_input_at_the_cap_is_sent_whole(frame_type):
    event = {"type": frame_type, "id": "t1", "input": _input_of_encoded_length(4096)}
    assert session_runtime._cap_ws_tool_input(event) is event


@pytest.mark.parametrize("frame_type", ["tool_use", "subagent_tool_use"])
def test_tool_input_over_the_cap_becomes_the_truncation_marker(frame_type):
    import json

    tool_input = _input_of_encoded_length(4097)
    event = {"type": frame_type, "id": "t1", "name": "Write", "input": tool_input}
    capped = session_runtime._cap_ws_tool_input(event)
    encoded = json.dumps(tool_input)
    assert capped == {
        "type": frame_type,
        "id": "t1",
        "name": "Write",
        "input": {
            "_ws_truncated": True,
            "_original_chars": 4097,
            "_preview": encoded[:4096],
        },
    }
    # The recorded frame is never mutated.
    assert event["input"] is tool_input


def test_other_frames_and_missing_inputs_pass_through():
    big = _input_of_encoded_length(10_000)
    for event in (
        {"type": "tool_result", "tool_use_id": "t1", "input": big},
        {"type": "tool_use", "id": "t1"},
    ):
        assert session_runtime._cap_ws_tool_input(event) is event


def test_an_unencodable_input_is_measured_by_its_repr():
    class Opaque:
        def __repr__(self):
            return "O" * 5000

    event = {"type": "tool_use", "id": "t1", "input": {Opaque(): 1}}
    capped = session_runtime._cap_ws_tool_input(event)
    assert capped["input"]["_ws_truncated"] is True
    assert capped["input"]["_preview"].startswith("{O")
