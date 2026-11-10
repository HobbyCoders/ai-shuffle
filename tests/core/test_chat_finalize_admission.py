"""A chat still owns session-wide cleanup after its engine stream ends."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.core import session_runtime
from app.core.provider import session_admission as admission
from app.db import turns
from tests._admission_cleanup import wait_for_lease_release
from tests.core.test_session_runtime import _runtime_env

SID = "chat-finalize-admission"


def pump(**kwargs):
    async def events(lease):
        admission.turn_ended(lease)
        yield {"type": "done", "metadata": {}}

    lease = kwargs.get("admission_lease")
    return events(lease) if lease is not None else admission.stream_turn(SID, events)


async def test_direct_successor_waits_until_chat_finalization_has_finished(mock_db):
    turn = turns.enqueue_turn(SID, "user", prompt="hello")
    finalizing, finish_cleanup = asyncio.Event(), asyncio.Event()
    attempted, admitted = asyncio.Event(), asyncio.Event()

    async def persist(*args, **kwargs):
        finalizing.set()
        await finish_cleanup.wait()

    async def direct_turn():
        attempted.set()
        lease = await admission.acquire(SID)
        try:
            admitted.set()
        finally:
            await wait_for_lease_release(lease)

    with _runtime_env(pump=pump), patch(
        "app.core.turn_finalize.persist_turn_end_state", side_effect=persist,
    ):
        runtime = asyncio.create_task(session_runtime.SessionRuntime(turn).execute())
        successor = None
        try:
            await asyncio.wait_for(finalizing.wait(), 5)
            successor = asyncio.create_task(direct_turn())
            await asyncio.wait_for(attempted.wait(), 5)
            assert not admitted.is_set(), "a successor can start workers before the previous turn tears them down"
        finally:
            finish_cleanup.set()
            assert await asyncio.wait_for(runtime, 5) == "completed"
            if successor is not None:
                await asyncio.wait_for(successor, 5)
        assert admitted.is_set()
    assert SID not in admission._holders


async def test_busy_direct_turn_keeps_chat_queued_without_session_cleanup(mock_db):
    turn = turns.enqueue_turn(SID, "user", prompt="hello")
    direct = await admission.acquire(SID)
    try:
        with _runtime_env(pump=pump) as env, patch(
            "app.core.provider_agent_supervisor.teardown_workers", new=AsyncMock(),
        ) as teardown:
            outcome = await session_runtime.SessionRuntime(turn).execute()
        assert outcome is None
        assert turns.get_turn(turn["id"])["state"] == "queued"
        teardown.assert_not_awaited()
        env["recorder"].start.assert_not_awaited()
    finally:
        await wait_for_lease_release(direct)


@pytest.mark.parametrize("cancel", [False, True], ids=["error", "cancelled"])
async def test_chat_releases_admission_when_pump_ends_abnormally(mock_db, cancel):
    entered = asyncio.Event()
    turn = turns.enqueue_turn(SID, "user", prompt="hello")

    async def failed_pump(**kwargs):
        entered.set()
        if cancel:
            await asyncio.Event().wait()
        raise RuntimeError("pump failed")
        yield  # turn the helper into an async iterator

    with _runtime_env(pump=failed_pump):
        runtime = asyncio.create_task(session_runtime.SessionRuntime(turn).execute())
        await asyncio.wait_for(entered.wait(), 5)
        if cancel:
            runtime.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(runtime, 5)
        else:
            assert await asyncio.wait_for(runtime, 5) == "aborted"
    assert SID not in admission._holders


@pytest.mark.parametrize("blocked_step", ["persist", "persist_error", "teardown", "pending", "park_read"])
async def test_rewind_timeout_fences_old_finalizer_from_direct_successor(mock_db, blocked_step):
    """A's delayed cleanup must not touch B after a rewind has taken A's lease."""
    from app.core import pending_reconcile, provider_agent_supervisor as supervisor

    turn = turns.enqueue_turn(SID, "user", prompt="old turn")
    entered, resume = asyncio.Event(), asyncio.Event()
    worker = supervisor.WorkerRecord(
        parent_session_id=SID, worker_session_id="new-worker", task_id="new-worker",
        tool_use_id="new-worker", background=False,
    )
    stopped = []

    async def barrier(*args, **kwargs):
        entered.set()
        await resume.wait()
        if blocked_step == "persist_error":
            raise RuntimeError("old persistence failed after takeover")
        return []

    async def stop(record, **kwargs):
        stopped.append(record.task_id)
        return {}

    with _runtime_env(pump=pump) as env, patch.object(
        supervisor, "teardown_worker", side_effect=stop,
    ), patch.object(
        pending_reconcile, "reconcile_engine_pending", new=AsyncMock(return_value=[]),
    ) as reconcile, patch.object(
        pending_reconcile, "nonterminal_agent_task_ids", return_value=[],
    ) as park_read, patch.object(
        admission, "_ENDING_WAIT_SECONDS", 0.01,
    ):
        if blocked_step in ("persist", "persist_error"):
            blocker = patch("app.core.turn_finalize.persist_turn_end_state", side_effect=barrier)
        elif blocked_step == "teardown":
            blocker = patch.object(supervisor, "teardown_workers", side_effect=barrier)
        elif blocked_step == "pending":
            reconcile.side_effect = barrier
            blocker = patch("app.core.turn_finalize.persist_turn_end_state", new=AsyncMock())
        else:
            # Use the runtime's thread seam for the durable park read so the
            # barrier is deterministic without blocking a worker thread.
            original_to_thread = asyncio.to_thread

            async def to_thread(fn, *args, **kwargs):
                if fn is park_read:
                    return await barrier()
                return await original_to_thread(fn, *args, **kwargs)

            blocker = patch.object(asyncio, "to_thread", side_effect=to_thread)
        with blocker:
            rt = session_runtime.SessionRuntime(turn)
            running = asyncio.create_task(rt.execute())
            direct = None
            try:
                await asyncio.wait_for(entered.wait(), 5)
                old = rt._admission_lease
                rewind = await admission.begin_rewind(SID)
                assert admission.take_for_rewind(rewind) is old
                # Exercise the real takeover wait, with a shortened budget.
                with pytest.raises(asyncio.TimeoutError):
                    await asyncio.wait_for(old.exited.wait(), 0.01)
                assert not old.exited.is_set()
                await wait_for_lease_release(rewind)
                direct = await admission.acquire(SID)
                # This is the supervisor's actual live-worker lookup. Teardown
                # is real, only the process-stop boundary is intercepted.
                supervisor.register_worker(worker)
                env["registry"].get_parking_background_work.reset_mock()
                reconcile.reset_mock()
                park_read.reset_mock()
                resume.set()
                outcome = await asyncio.wait_for(running, 5)
                assert stopped == [], "A killed B's newly admitted synchronous worker"
                assert outcome == "aborted"
                assert admission._holders[SID] is direct
                env["registry"].get_parking_background_work.assert_not_called()
                reconcile.assert_not_awaited()
                park_read.assert_not_called()
                env["record_parked"].assert_not_awaited()
                assert turns.get_turn(turn["id"])["state"] == "aborted"
            finally:
                resume.set()
                await asyncio.wait_for(running, 5)
                if direct is not None:
                    await wait_for_lease_release(direct)
                supervisor.unregister_worker(SID, worker.worker_session_id)


@pytest.mark.parametrize("effect", ["computer_use", "done"])
async def test_replaced_runtime_deregisters_without_stopping_or_completing_direct_successor(mock_db, effect):
    from app.mcp.computer_use import abort as cu_abort

    entered, resume = asyncio.Event(), asyncio.Event()
    turn = turns.enqueue_turn(SID, "user", prompt="old turn")
    rt = session_runtime.SessionRuntime(turn)

    async def persist(*args, **kwargs):
        entered.set()
        await resume.wait()

    with _runtime_env(pump=pump) as env, patch(
        "app.core.turn_finalize.persist_turn_end_state", side_effect=persist,
    ):
        running = asyncio.create_task(rt.execute())
        direct = None
        try:
            await asyncio.wait_for(entered.wait(), 5)
            rewind = await admission.begin_rewind(SID)
            assert admission.take_for_rewind(rewind) is rt._admission_lease
            await wait_for_lease_release(rewind)
            direct = await admission.acquire(SID)
            # A direct-API successor never replaces _live_runtimes[sid].
            assert session_runtime._live_runtimes[SID] is rt
            cu_abort.resume(SID)
            generation = cu_abort.generation(SID)
            env["broadcaster"].reset_mock()
            resume.set()
            assert await asyncio.wait_for(running, 5) == "aborted"
            if effect == "computer_use":
                assert not cu_abort.is_stopped(SID), "A halted B's newly armed computer use"
                assert cu_abort.generation(SID) == generation
            else:
                assert not any(
                    call.args[1].get("type") == "done"
                    for call in env["broadcaster"].await_args_list
                ), "A's stale done frame completed B's chat"
            assert SID not in session_runtime._live_runtimes
            assert rt._admission_lease.exited.is_set()
            assert admission._holders[SID] is direct
            from app.db import runs
            assert runs.get_active_run(SID) is None
        finally:
            resume.set()
            await asyncio.wait_for(running, 5)
            if direct is not None:
                await wait_for_lease_release(direct)


@pytest.mark.parametrize("delay", ["before_write", "inside_write"])
async def test_rewind_timeout_fences_old_parked_marker_write_and_broadcast(mock_db, delay):
    """A's delayed parked-marker clear can neither undo nor publish over B's marker (#1285).

    The real turn-end helper and the real row update run; only the update's
    worker thread is held. ``before_write`` holds it before the update starts
    (the issue's interleaving: B writes while A waits). ``inside_write`` holds
    it after A passed its ownership check, while the rewind still ran, so B's
    write has to queue behind A's instead of landing first.
    """
    import threading

    from app.core import turn_finalize
    from app.db import database

    database.create_session(session_id=SID, profile_id="default")
    turn = turns.enqueue_turn(SID, "user", prompt="old turn")
    real_persist = turn_finalize.persist_turn_end_state
    real_write = database.set_session_parked_task_kind
    entered, release = threading.Event(), threading.Event()
    calls = []

    def held_write(session_id, kind, **kwargs):
        calls.append(kind)
        if len(calls) == 1:  # turn A's write
            guard = kwargs.get("still_valid")
            if delay == "inside_write" and guard is not None:
                def checked_then_held():
                    owner = guard()
                    entered.set()
                    assert release.wait(5)
                    return owner
                kwargs["still_valid"] = checked_then_held
            else:
                entered.set()
                assert release.wait(5)
        return real_write(session_id, kind, **kwargs)

    published = []

    async def broadcast(*, user_id, session_id, updates, **kwargs):
        if "parked_task_kind" in updates:
            published.append(updates["parked_task_kind"])

    with _runtime_env(pump=pump) as env, patch(
        "app.core.turn_finalize.persist_turn_end_state", new=real_persist,
    ), patch.object(database, "set_session_parked_task_kind", side_effect=held_write), patch(
        "app.core.sync_engine.sync_engine.broadcast_user_session_updated", side_effect=broadcast,
    ), patch("app.core.session_pr_state.schedule_session_pr_reconcile"), patch(
        "app.core.session_handoff.schedule",
    ), patch("app.core.pg_session_store.flush_summary_folds"), patch.object(
        admission, "_ENDING_WAIT_SECONDS", 0.01,
    ):
        rt = session_runtime.SessionRuntime(turn)
        running = asyncio.create_task(rt.execute())
        direct = successor = None
        try:
            assert await asyncio.to_thread(entered.wait, 5), "turn A never reached its marker write"
            old = rt._admission_lease
            rewind = await admission.begin_rewind(SID)
            assert admission.take_for_rewind(rewind) is old
            with pytest.raises(asyncio.TimeoutError):
                await asyncio.wait_for(old.exited.wait(), 0.01)
            await wait_for_lease_release(rewind)
            # Turn B is admitted, starts background work and records its park.
            direct = await admission.acquire(SID)
            env["registry"].get_parking_background_work.return_value = [{"kind": "agent"}]
            successor = asyncio.create_task(turn_finalize.persist_turn_end_state(SID, resync_branch=False))
            if delay == "before_write":
                await asyncio.wait_for(successor, 5)
                assert database.get_session(SID)["parked_task_kind"] == "agent"
            else:
                while len(calls) < 2:  # B's write thread has started
                    await asyncio.sleep(0.01)
                done, _ = await asyncio.wait({successor}, timeout=0.2)
                assert not done, "B's park landed while A's checked write was still in flight"
            release.set()
            assert await asyncio.wait_for(running, 5) == "aborted"
            await asyncio.wait_for(successor, 5)
            assert database.get_session(SID)["parked_task_kind"] == "agent", "A's stale clear overwrote B's park"
            assert published == ["agent"], f"A published a stale marker update: {published}"
            assert admission._holders[SID] is direct
        finally:
            release.set()
            await asyncio.wait_for(running, 5)
            if successor is not None:
                await asyncio.wait_for(successor, 5)
            if direct is not None:
                await wait_for_lease_release(direct)


async def test_old_parked_marker_publication_is_fenced_inside_the_broadcaster(mock_db):
    """#1285 review: the broadcaster awaits its owner lookup before publishing.

    A writes its clear, passes every check before the broadcast, then waits
    inside ``_broadcast_user_message`` on the owner lookup. A rewind takes the
    chat and gives up; B writes and publishes "agent". A's resumed broadcast
    must reach neither the lifecycle stream nor any device queue.
    """
    from types import SimpleNamespace

    from app.core import turn_finalize
    from app.core.global_events import global_events
    from app.core.sync_engine import sync_engine
    from app.db import async_database, database

    database.create_session(session_id=SID, profile_id="default")
    real_run_sync = async_database.run_sync
    entered, release = asyncio.Event(), asyncio.Event()
    hold = {"next": False}

    async def run_sync(fn, *args, **kwargs):
        if hold["next"] and fn is database.get_session:
            hold["next"] = False
            entered.set()
            await release.wait()
        return await real_run_sync(fn, *args, **kwargs)

    lifecycle, devices = [], []

    def fan_out(bucket, targets, message):
        devices.append(message["updates"].get("parked_task_kind", "-"))
        return []

    with patch.object(async_database, "run_sync", new=run_sync), patch.object(
        global_events, "lifecycle",
        side_effect=lambda owner, message, **kw: lifecycle.append(message["updates"].get("parked_task_kind", "-")),
    ), patch.object(sync_engine, "_fan_out_to_user_devices", side_effect=fan_out), patch.dict(
        sync_engine._user_connections, {"admin": {"d1": SimpleNamespace(device_id="d1")}},
    ), patch.object(admission, "_ENDING_WAIT_SECONDS", 0.01):
        old = await admission.acquire(SID)
        direct = None
        hold["next"] = True
        stale = asyncio.create_task(turn_finalize._write_parked_task_kind(SID, None, writer=old))
        try:
            await asyncio.wait_for(entered.wait(), 5)
            rewind = await admission.begin_rewind(SID)
            assert admission.take_for_rewind(rewind) is old
            await wait_for_lease_release(rewind)
            assert not admission.holds_chat(old)
            direct = await admission.acquire(SID)
            assert await turn_finalize._write_parked_task_kind(SID, "agent", writer=direct)
            release.set()
            await asyncio.wait_for(stale, 5)
            assert database.get_session(SID)["parked_task_kind"] == "agent"
            assert lifecycle == ["agent"], f"A's stale clear reached the lifecycle stream: {lifecycle}"
            assert devices == ["agent"], f"A's stale clear reached a device queue: {devices}"
        finally:
            release.set()
            await asyncio.wait_for(stale, 5)
            admission.close(old)
            await wait_for_lease_release(old)
            if direct is not None:
                await wait_for_lease_release(direct)
