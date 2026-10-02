"""Unit tests for app/core/session_runtime.py — the one spawn path (v2 core,
Phase 2 A4).

The turns/runs DB layer is exercised for real (pg_shim SQLite), while the pump
(``_stream_to_websocket_impl``), the recorder, and the between-turns idle reader
are mocked at the CONSUMER module's binding (session_runtime imported the names,
so patch there — the lazy-import-mock-freeze rule).
"""

import asyncio
from contextlib import ExitStack, contextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import session_runtime
from app.db import runs as runs_db
from app.db import turns as turns_db
from tests.core.helpers import make_pump


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _clean_module_state():
    """Reset the module-level reader-token / idle-reader registries and seams so
    a lock left held (or a spy left registered) by one test never leaks."""
    session_runtime._reader_locks.clear()
    session_runtime._idle_readers.clear()
    session_runtime._live_runtimes.clear()
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)
    yield
    session_runtime._reader_locks.clear()
    session_runtime._idle_readers.clear()
    session_runtime._live_runtimes.clear()
    session_runtime.register_scheduler(None)
    session_runtime.register_broadcaster(None)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _crashing_pump():
    async def _impl(**kwargs):
        if False:  # make this a generator
            yield {}
        raise RuntimeError("pump boom")
    return _impl


def _recorder_mock():
    rec = MagicMock(name="recorder")
    rec.start = AsyncMock()
    rec.on_event = AsyncMock()
    rec.close = AsyncMock()
    rec.record_task_terminal = AsyncMock(return_value=0)
    return rec


@contextmanager
def _runtime_env(*, pump, bg_work=None, bg_launches=None, patch_idle=True):
    """Patch everything around the DB: pump, recorder, taps, idle-reader seams,
    and the task_registry park gate. Yields a dict of the spies.

    ``patch_idle=False`` keeps the REAL ``start_idle_reader`` (tests that need
    the drain reader to actually run patch ``idle_reader.run_idle_reader``
    themselves)."""
    bg_work = bg_work if bg_work is not None else []
    bg_launches = bg_launches if bg_launches is not None else set()

    registry = MagicMock(name="task_registry")
    registry.get_active_background_work.return_value = bg_work
    registry.get_background_launches.return_value = bg_launches
    # Park gate (amended 2026-08-08) reads has_unresolved_background_launches, not
    # the raw set; the 4b self-heal guard reads has_active_background_work. Derive
    # both from the same knobs so the fixture stays a single source of truth.
    registry.has_unresolved_background_launches.return_value = bool(bg_launches)
    registry.has_active_background_work.return_value = bool(bg_work)
    registry.reset_started_this_turn.return_value = None
    registry.stop_generation.return_value = 0
    registry.stop_invalidated.return_value = False
    registry.is_user_stopped.return_value = False

    recorder = _recorder_mock()
    broadcaster = AsyncMock(name="broadcaster")
    scheduler = MagicMock(name="on_turn_settled")
    session_runtime.register_broadcaster(broadcaster)
    session_runtime.register_scheduler(scheduler)

    start_idle = MagicMock(name="start_idle_reader")
    stop_idle = AsyncMock(name="stop_idle_reader")
    record_parked = AsyncMock(name="record_parked")

    with ExitStack() as stack:
        stack.enter_context(patch("app.core.query_engine._stream_to_websocket_impl", new=pump))
        stack.enter_context(patch.object(
            session_runtime.TurnRecorder, "create_for_run",
            new=AsyncMock(return_value=recorder),
        ))
        stack.enter_context(patch("app.core.session_runtime.record_parked", new=record_parked))
        if patch_idle:
            stack.enter_context(patch("app.core.session_runtime.start_idle_reader", new=start_idle))
        stack.enter_context(patch("app.core.session_runtime.stop_idle_reader", new=stop_idle))
        stack.enter_context(patch("app.core.task_registry.task_registry", new=registry))
        # Persist end-of-turn state is a legacy call we absorb — stub it.
        stack.enter_context(patch("app.core.turn_finalize.persist_turn_end_state", new=AsyncMock()))
        yield {
            "recorder": recorder,
            "broadcaster": broadcaster,
            "scheduler": scheduler,
            "registry": registry,
            "start_idle": start_idle,
            "stop_idle": stop_idle,
            "record_parked": record_parked,
        }


# ---------------------------------------------------------------------------
# 1. Mandatory lease

@pytest.mark.asyncio
async def test_handoff_after_placement_read_releases_claim_without_running(mock_db):
    turn = turns_db.enqueue_turn("handoff-race", "user", prompt="hello")
    with _runtime_env(pump=make_pump([{"type": "done"}])) as env, patch(
        "app.network.engine.context", return_value=object()
    ), patch("app.db.database.get_session", return_value={"id": "handoff-race"}), patch(
        "app.network.control.owner_of", return_value="new-owner"
    ):
        result = await session_runtime.SessionRuntime(turn).execute()
    assert result is None
    env["recorder"].start.assert_not_awaited()
    assert runs_db.get_active_run("handoff-race") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("held", [True, False])
async def test_checkout_reserved_by_a_handoff_keeps_the_turn_queued(mock_db, held):
    """A handoff publishing/receiving this session's checkout holds admission."""
    turn = turns_db.enqueue_turn("checkout-user", "user", prompt="hello")
    admission = MagicMock(return_value=held)
    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])) as env, patch(
        "app.network.engine.context", return_value=object()
    ), patch("app.db.database.get_session", return_value={"id": "checkout-user"}), patch(
        "app.network.control.owner_of", return_value=None
    ), patch("app.network.handoff.admission_held", new=admission):
        result = await session_runtime.SessionRuntime(turn).execute()
    admission.assert_called_once_with("checkout-user")
    if held:
        assert result is None
        env["recorder"].start.assert_not_awaited()
        assert turns_db.get_turn(turn["id"])["state"] == "queued"
        assert runs_db.get_active_run("checkout-user") is None
    else:
        assert result == "completed"
        assert turns_db.get_turn(turn["id"])["state"] == "completed"


@pytest.mark.parametrize("phase", ["start", "done", "close", "timer"])
@pytest.mark.parametrize("background", [False, True])
async def test_recording_failure_aborts_and_cleans_up(mock_db, phase, background):
    turn = turns_db.enqueue_turn("recording-failure", "user", prompt="hello")
    holder = {}

    async def pump(**kwargs):
        if phase == "timer":
            asyncio.get_running_loop().call_soon(holder["runtime"]._on_recorder_recording_failed)
            await asyncio.Event().wait()
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=pump, bg_work=[{"id": "task"}] if background else []) as env:
        rt = session_runtime.SessionRuntime(turn)
        holder["runtime"] = rt
        async def fail(*args, **kwargs):
            rt._on_recorder_recording_failed()
            await asyncio.sleep(0)  # A self-cancel would interrupt cleanup here.
        if phase != "timer":
            method = {"start": "start", "done": "on_event", "close": "close"}[phase]
            getattr(env["recorder"], method).side_effect = fail
        with patch("app.core.query_session_lifecycle.interrupt_session", new=AsyncMock()) as interrupt:
            await asyncio.wait_for(rt.execute(), 2)
            await asyncio.sleep(0)
            interrupt.assert_awaited_once_with(turn["session_id"])
        assert rt._abort_reason == "recording_failed"
        assert turns_db.get_turn(turn["id"])["state"] == "aborted"
        env["recorder"].close.assert_awaited_once()
        assert all(call.args[1].get("type") != "done" for call in env["broadcaster"].await_args_list)
        env["start_idle"].assert_not_called()
        assert rt._pump_task is None
        assert session_runtime.get_live_runtime(turn["session_id"]) is None

# ---------------------------------------------------------------------------

@pytest.mark.parametrize("repair", [False, True])
@pytest.mark.parametrize("snapshot_status", ["running", "completed"])
async def test_pooled_swarm_resume_repairs_after_claim_before_pump(mock_db, monkeypatch, repair, snapshot_status):
    """Real lease + pooled NativeLoop receipts + boot-produced terminal proof.

    Disabling only the new boundary reproduces the old pre-pump phantom,
    despite the unrelated end-of-turn pending reconciliation still existing.
    """
    from types import SimpleNamespace
    from fluid.engine.native import NativeLoop
    from fluid.store.base import MemoryStore
    from fluid.tools.registry import BackgroundTaskReceipt
    from app.core import swarm, session_pool
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.db.background_tasks import upsert_background_task, mark_stale_background_tasks_killed
    from app.db.session_events import append_events

    sid, key = "pooled-swarm", "swarm_dead"
    snapshot = dict(id=key, session_id=sid, status=snapshot_status, tasks=[
        dict(id="done", status="completed", result="keep me"),
        dict(id="lost", status="running" if snapshot_status == "running" else "completed"),
    ])
    append_events(sid, [{"type": "swarm.updated", "payload": {"swarm": snapshot}}])
    upsert_background_task(dict(task_id=key, session_id=sid, kind="agent", status="running"))
    assert any(row["task_id"] == key for row in mark_stale_background_tasks_killed())
    store = MemoryStore()
    previous = NativeLoop(provider=MagicMock(), store=store, model="fake")
    await previous._background_task_started(BackgroundTaskReceipt(task_id=key))
    meta = await previous._ensure_session()
    await previous.aclose()
    engine = NativeLoop(provider=MagicMock(), store=store, model="fake", session_id=meta.session_id)
    engine.ctx.extra["recover_background"] = swarm.make_recovery(lambda: sid)
    adapter = HarnessAdapter()
    adapter._engine = engine
    adapter._connected = True
    pool = SimpleNamespace(peek=lambda session_id: SimpleNamespace(adapter=adapter) if session_id == sid else None)
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: pool)
    original = swarm.reconcile_owned_pending
    seen = []

    async def boundary(session_id, run_id, fence_token):
        active = runs_db.get_active_run(sid)
        assert active["id"] == run_id and active["fence_token"] == fence_token
        assert active["runner_id"] == session_runtime.settings.effective_instance_id
        # The engine callback cannot authorize itself under this new lease.
        assert await swarm.make_recovery(lambda: sid)((key,)) == []
        seen.append("claimed")
        if repair:
            assert await original(session_id, run_id, fence_token) == [key]

    monkeypatch.setattr(swarm, "reconcile_owned_pending", boundary)

    async def pump(**kwargs):
        assert seen == ["claimed"]
        async with engine._background_lock:
            await engine._rehydrate_background_lifecycle_unlocked()
        assert (key not in engine._background_pending) is repair
        rows = await swarm.snapshots(sid)
        expected = "interrupted" if repair and snapshot_status == "running" else snapshot_status
        assert rows[0]["status"] == expected
        assert rows[0]["tasks"][0] == snapshot["tasks"][0]
        assert rows[0]["tasks"][1]["status"] == expected
        seen.append("pumped")
        yield {"type": "done", "metadata": {}}

    turn = turns_db.enqueue_turn(sid, "resume", prompt="resume")
    try:
        with _runtime_env(pump=pump) as env:
            env["registry"].get_task.return_value = None
            assert await session_runtime.SessionRuntime(turn).execute() == "completed"
        assert seen == ["claimed", "pumped"]
    finally:
        await engine.aclose()


async def test_execute_requires_run_claim(mock_db):
    """No run claim (another runner owns the session) → the turn stays queued and
    execute returns None without ever starting it."""
    turn = turns_db.enqueue_turn("s1", "user", prompt="hi")
    # Someone else already owns the session's active run.
    other = runs_db.claim_run("s1", "other-runner", 60)
    assert other is not None

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome is None
    # Turn untouched — still queued, no start race consumed.
    assert turns_db.get_turn(turn["id"])["state"] == "queued"
    env["scheduler"].assert_not_called()
    # A runtime that never won start_turn never registered itself.
    assert session_runtime.get_live_runtime("s1") is None


async def test_execute_behind_a_stranded_live_turn_does_not_claim_or_retry(mock_db):
    """Round-9 follow-up (#1237): a live turn whose lease is dead leaves the
    session claimable, and every claim pass used to claim a run, trip
    idx_turns_one_live_per_session in start_turn and release the run again.
    The kick must now leave the queued turn alone without touching runs or
    start_turn, and the reap sweep must still free the slot."""
    stranded = turns_db.enqueue_turn("s-strand", "unpark", prompt="wake")
    run = runs_db.claim_run("s-strand", "me", 60)
    assert turns_db.start_turn(stranded["id"], run["id"], "me") is not None
    # The owner released its lease without settling the turn.
    assert runs_db.release_run(run["id"], run["fence_token"])
    queued = turns_db.enqueue_turn("s-strand", "user", prompt="next")

    def _run_count():
        from app.db.base import get_db
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) AS n FROM runs WHERE session_id = %s", ("s-strand",))
            return cur.fetchone()["n"]

    runs_before = _run_count()
    real_start = turns_db.start_turn
    start_calls = []

    def _spy_start(*args, **kwargs):
        start_calls.append(args)
        return real_start(*args, **kwargs)

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])) as env:
        with patch.object(turns_db, "start_turn", side_effect=_spy_start):
            # Several claim passes in a row, as the NOTIFY-driven loop does.
            for _ in range(5):
                assert await session_runtime.SessionRuntime(queued).execute() is None
            assert start_calls == []
            assert _run_count() == runs_before
            assert turns_db.get_turn(queued["id"])["state"] == "queued"

            # The reap sweep frees the slot; the next kick runs the queued turn.
            reaped = turns_db.reap_expired()
            assert [t["id"] for t in reaped] == [stranded["id"]]
            env["registry"].get_task.return_value = None
            assert await session_runtime.SessionRuntime(queued).execute() == "completed"
            assert len(start_calls) == 1


# ---------------------------------------------------------------------------
# 1b. Live-runtime registry — the command→runtime interrupt seam (D1)
# ---------------------------------------------------------------------------

async def test_live_runtime_registered_during_pump_and_popped_after(mock_db):
    """The runtime registers itself once it wins start_turn (so
    turn_manager.interrupt can reach it) and self-guarded-pops when it finishes."""
    turn = turns_db.enqueue_turn("s1r", "user", prompt="hi")

    pump_entered = asyncio.Event()
    release_pump = asyncio.Event()

    async def _pump(**kwargs):
        pump_entered.set()
        await release_pump.wait()
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=_pump):
        rt = session_runtime.SessionRuntime(turn)
        task = asyncio.create_task(rt.execute())
        await asyncio.wait_for(pump_entered.wait(), 1.0)

        # Registered as the live runtime for the session while it runs.
        assert session_runtime.get_live_runtime("s1r") is rt

        release_pump.set()
        await asyncio.wait_for(task, 1.0)

    # Popped after settlement.
    assert session_runtime.get_live_runtime("s1r") is None


async def test_execute_notifies_streaming_status_at_claim_and_release(mock_db):
    """The board-spinner seam: the runtime must notify streaming_status at the
    lease boundary — FORCE at claim (a stale previous-turn busy entry would
    otherwise suppress the ON frame) and plain at release (immediate OFF
    instead of waiting for the sweep)."""
    calls = []

    def _spy(session_id, force=False):
        calls.append((session_id, force))

    turn = turns_db.enqueue_turn("s1n", "user", prompt="hi")

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])):
        with patch("app.core.session_runtime._notify_run_change", new=_spy):
            await session_runtime.SessionRuntime(turn).execute()

    assert ("s1n", True) in calls   # claim — force=True
    assert ("s1n", False) in calls  # release — force=False


# ---------------------------------------------------------------------------
# 2. Reader-token serialization
# ---------------------------------------------------------------------------

async def test_reader_token_serializes_pump_and_idle_reader(mock_db):
    """While the pump holds the reader token, a would-be idle reader acquiring
    the SAME per-session lock blocks until the pump releases it."""
    turn = turns_db.enqueue_turn("s2", "user", prompt="hi")

    pump_entered = asyncio.Event()
    release_pump = asyncio.Event()

    async def _pump(**kwargs):
        pump_entered.set()
        await release_pump.wait()
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=_pump):
        task = asyncio.create_task(session_runtime.SessionRuntime(turn).execute())
        await asyncio.wait_for(pump_entered.wait(), 1.0)

        # The pump holds the session's reader token.
        assert session_runtime.get_reader_lock("s2").locked()

        acquired = False

        async def _idle_reader_try():
            nonlocal acquired
            async with session_runtime.get_reader_lock("s2"):
                acquired = True

        t2 = asyncio.create_task(_idle_reader_try())
        await asyncio.sleep(0.02)
        assert acquired is False  # blocked behind the pump

        release_pump.set()
        outcome = await asyncio.wait_for(task, 1.0)
        await asyncio.wait_for(t2, 1.0)

    assert outcome == "completed"
    assert acquired is True
    assert not session_runtime.get_reader_lock("s2").locked()


# ---------------------------------------------------------------------------
# 3. Park gate — both branches
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("enqueue_at_park", [False, True])
async def test_parking_drains_user_messages_before_background_completion(mock_db, enqueue_at_park):
    from app.core import turn_manager, turn_scheduler

    sid = "park-drain"
    first = turns_db.enqueue_turn(sid, "user", prompt="launch background work")
    delivered = []
    queued = []
    second_started = asyncio.Event()

    def enqueue():
        queued.append(turns_db.enqueue_turn(sid, "user", prompt="answer me now"))

    async def pump(**kwargs):
        delivered.append(kwargs["prompt"])
        if len(delivered) == 1:
            if not enqueue_at_park:
                enqueue()
        else:
            second_started.set()
        yield {"type": "done", "metadata": {}}

    async def on_park(*args):
        if enqueue_at_park and not queued:
            enqueue()

    with _runtime_env(pump=pump, bg_work=[{"id": "still-running"}]) as env:
        env["record_parked"].side_effect = on_park
        session_runtime.register_scheduler(turn_scheduler.on_turn_settled)
        turn_manager.register_kick(turn_scheduler.kick)
        try:
            assert await session_runtime.execute(first) == "parked"
            await asyncio.wait_for(second_started.wait(), 3)
            # Await scheduler-owned execution so the successor also releases its lease.
            while turn_scheduler._kick_tasks:
                await asyncio.gather(*list(turn_scheduler._kick_tasks))
        finally:
            turn_manager.register_kick(None)
    assert delivered == ["launch background work", "answer me now"]
    assert turns_db.get_turn(queued[0]["id"])["state"] == "parked"
    assert turns_db.get_queued_turns(sid) == []
    assert turns_db.get_turn(first["id"])["payload"]["task_ids"] == ["still-running"]


async def test_finalize_parks_when_background_work_survives(mock_db):
    turn = turns_db.enqueue_turn("s3", "user", prompt="hi")

    with _runtime_env(
        pump=make_pump([{"type": "done", "metadata": {}}]),
        bg_work=[{"id": "task-1"}],
    ) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "parked"
    assert turns_db.get_turn(turn["id"])["state"] == "parked"
    # payload.task_ids recorded, turn.parked event tapped; no reader (only a
    # draining turn starts one).
    assert turns_db.get_turn(turn["id"])["payload"]["task_ids"] == ["task-1"]
    env["record_parked"].assert_awaited_once()
    env["start_idle"].assert_not_called()
    env["scheduler"].assert_called_once_with("s3", turn["id"], "parked")
    done = [c.args[1] for c in env["broadcaster"].await_args_list if c.args[1]["type"] == "done"]
    assert done == [{"type": "done", "metadata": {}, "session_id": "s3", "outcome": "parked"}]


async def test_finalize_completes_when_no_background_work(mock_db):
    turn = turns_db.enqueue_turn("s4", "user", prompt="hi")

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    assert turns_db.get_turn(turn["id"])["state"] == "completed"
    env["start_idle"].assert_not_called()
    env["record_parked"].assert_not_called()
    env["scheduler"].assert_called_once_with("s4", turn["id"], "completed")
    done = [c.args[1] for c in env["broadcaster"].await_args_list if c.args[1]["type"] == "done"]
    assert done == [{"type": "done", "metadata": {}, "session_id": "s4", "outcome": "completed"}]


# ---------------------------------------------------------------------------
# 3b. Park gate — durable truth (2026-09-05 audit F7/F9)
# ---------------------------------------------------------------------------

def _durable_task(session_id, task_id, status, kind="agent"):
    from app.db import background_tasks as background_tasks_db
    background_tasks_db.upsert_background_task({
        "task_id": task_id,
        "session_id": session_id,
        "kind": kind,
        "status": status,
        "title": task_id,
        "started_at": "2026-09-05T00:00:00+00:00",
        "updated_at": "2026-09-05T00:00:01+00:00",
    })


async def test_park_drops_a_task_that_has_already_settled(mock_db):
    """F7: a park named a shell that had settled four seconds earlier, and the
    late terminal frame bought a no-op wake turn. The id is dropped."""
    turn = turns_db.enqueue_turn("s-drop", "user", prompt="hi")
    _durable_task("s-drop", "settled", "completed", kind="shell")
    _durable_task("s-drop", "still-going", "running", kind="shell")

    with _runtime_env(
        pump=make_pump([{"type": "done", "metadata": {}}]),
        bg_work=[{"id": "settled"}, {"id": "still-going"}],
    ) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "parked"
    assert turns_db.get_turn(turn["id"])["payload"]["task_ids"] == ["still-going"]
    assert env["record_parked"].await_args.args[1] == ["still-going"]


async def test_no_park_when_every_registry_id_has_already_settled(mock_db):
    """Parking on nothing is the stuck-parked shape; if the gate's only ids are
    all durably terminal the turn simply completes."""
    turn = turns_db.enqueue_turn("s-nopark", "user", prompt="hi")
    _durable_task("s-nopark", "settled", "completed", kind="shell")

    with _runtime_env(
        pump=make_pump([{"type": "done", "metadata": {}}]),
        bg_work=[{"id": "settled"}],
    ) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    env["record_parked"].assert_not_called()
    env["start_idle"].assert_not_called()


async def test_park_gate_falls_back_to_the_durable_subagent_rows(mock_db):
    """F9: a turn ended end_turn with an Explore still running because the gate
    read only the in-memory registry. The durable row parks it instead."""
    turn = turns_db.enqueue_turn("s-durable", "user", prompt="hi")
    _durable_task("s-durable", "explore-1", "running", kind="agent")

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "parked"
    assert turns_db.get_turn(turn["id"])["payload"]["task_ids"] == ["explore-1"]


async def test_durable_park_arm_ignores_monitor_shells(mock_db):
    """A Monitor shell can be a ``serves`` process meant to stay up; parking on
    one would pin the session forever — the same exclusion fluid makes."""
    turn = turns_db.enqueue_turn("s-serves", "user", prompt="hi")
    _durable_task("s-serves", "dev-server", "running", kind="shell")

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    env["record_parked"].assert_not_called()


async def test_turn_end_reconciles_the_engine_pending_set(mock_db):
    """F1: the next park must name only work that is genuinely outstanding, so
    turn end retires whatever the engine still counts pending and the app can
    prove is over."""
    turn = turns_db.enqueue_turn("s-reconcile", "user", prompt="hi")
    reconcile = AsyncMock(return_value=["ghost"])

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])):
        with patch(
            "app.core.pending_reconcile.reconcile_engine_pending", new=reconcile
        ):
            await session_runtime.SessionRuntime(turn).execute()

    reconcile.assert_awaited_once_with("s-reconcile")


async def test_turn_start_self_heal_settles_stale_park_when_no_bg_work(mock_db):
    """FIX 2: the 4b turn-start self-heal settles a stale parked row when NO
    background work is running — the parked row was residue from work that has
    since finished, and a fresh app-turn is proof the session is live."""
    sid = "s4heal"
    # A prior turn parked (residue) — take + release the live slot so the new
    # turn can start.
    stale = turns_db.enqueue_turn(sid, "user", prompt="old")
    turns_db.start_turn(stale["id"], run_id="run-old")
    turns_db.park_turn(stale["id"], ["task-gone"])
    assert turns_db.get_turn(stale["id"])["state"] == "parked"

    fresh = turns_db.enqueue_turn(sid, "user", prompt="new")
    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])):
        outcome = await session_runtime.SessionRuntime(fresh).execute()

    assert outcome == "completed"
    # The stale parked residue was self-healed to completed.
    assert turns_db.get_turn(stale["id"])["state"] == "completed"


async def test_turn_start_self_heal_spares_park_with_running_bg_work(mock_db):
    """FIX 2: the self-heal is GATED on has_active_background_work. A queued turn
    intentionally starts while a prior turn is parked on RUNNING work; that parked
    row is the live signal _latest_context_turn relies on, so settling it early
    would risk wrong context attribution. With running bg work the
    stale-looking parked row must SURVIVE."""
    sid = "s4spare"
    stale = turns_db.enqueue_turn(sid, "user", prompt="old")
    turns_db.start_turn(stale["id"], run_id="run-old")
    turns_db.park_turn(stale["id"], ["task-live"])
    assert turns_db.get_turn(stale["id"])["state"] == "parked"

    fresh = turns_db.enqueue_turn(sid, "user", prompt="new")
    # bg_work present → has_active_background_work True → self-heal is skipped.
    with _runtime_env(
        pump=make_pump([{"type": "done", "metadata": {}}]),
        bg_work=[{"id": "task-live"}],
    ):
        await session_runtime.SessionRuntime(fresh).execute()

    # The parked row was NOT settled by the self-heal (live work still parks the
    # session legitimately).
    assert turns_db.get_turn(stale["id"])["state"] == "parked"


# ---------------------------------------------------------------------------
# 3c. Failed-worker revive (turn start un-fails the session)
# ---------------------------------------------------------------------------

def _seed_session(sid, status):
    """A real sessions row with an explicit status."""
    from app.core.config import settings
    from app.db import database

    # The implicit-pin placement gate only considers approved, online runners,
    # so the self install needs a registry row before any turn is submitted.
    database.register_install(
        settings.effective_instance_id, "test-runner", None, "local", "1", {},
        approve_new=True,
    )

    # profile_id stays NULL: this module's ``mock_db`` is a bare shim connection
    # (no seeded "default" profile), and sessions.profile_id is a FK.
    database.create_session(session_id=sid, profile_id=None, title="worker")
    from app.db.base import get_db

    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE sessions SET status = %s WHERE id = %s", (status, sid)
        )


def _status_of(sid):
    from app.db import database

    return database.get_session(sid)["status"]


async def test_turn_start_revives_a_failed_worker_session(mock_db):
    """A worker session ProviderAgent stamped ``'failed'`` is a verdict about a
    run that is OVER. A new turn STARTING on it — a user message typed into the
    worker session, a SendMessage-style resume, a scheduler wake — is proof the
    session is live again, so the status flips back to ``'active'``."""
    sid = "s4failed"
    _seed_session(sid, "failed")
    turn = turns_db.enqueue_turn(sid, "user", prompt="carry on")

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])):
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    assert _status_of(sid) == "active"


async def test_turn_start_revives_on_a_server_originated_wake(mock_db):
    """The revive is at the ONE choke point every turn passes through, so a
    server-originated wake (``source='unpark'``) revives too — the caller-wake
    turn a failed worker's own background work could produce must not run against
    a session still marked failed."""
    sid = "s4failedwake"
    _seed_session(sid, "failed")
    turn = turns_db.enqueue_turn(sid, "unpark", prompt="<task-notification/>")

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])):
        await session_runtime.SessionRuntime(turn).execute()

    assert _status_of(sid) == "active"


async def test_server_wake_leaves_an_archived_session_archived(mock_db):
    """Archived is not a run verdict — a server-originated wake must not undo it."""
    sid = "s4archived"
    _seed_session(sid, "archived")
    turn = turns_db.enqueue_turn(sid, "unpark", prompt="<task-notification/>")

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])):
        await session_runtime.SessionRuntime(turn).execute()

    assert _status_of(sid) == "archived"


async def test_user_turn_brings_an_archived_session_back(mock_db):
    """The user talking to an archived session again is the one thing that
    brings it back into the history."""
    sid = "s4archiveduser"
    _seed_session(sid, "archived")
    turn = turns_db.enqueue_turn(sid, "user", prompt="hi")

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])):
        await session_runtime.SessionRuntime(turn).execute()

    assert _status_of(sid) == "active"


async def test_queued_turn_that_never_starts_does_not_revive(mock_db):
    """The revive is keyed on the turn STARTING, not on it being enqueued: a
    turn that loses the run claim leaves the verdict standing."""
    sid = "s4failedqueued"
    _seed_session(sid, "failed")
    turn = turns_db.enqueue_turn(sid, "user", prompt="hi")
    # Another runner owns the session's active run → execute bails before 4c.
    assert runs_db.claim_run(sid, "other-runner", 60) is not None

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])):
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome is None
    assert _status_of(sid) == "failed"


# ---------------------------------------------------------------------------
# 4. finalize always runs (pump crash → aborted)
# ---------------------------------------------------------------------------

async def test_finalize_runs_even_when_pump_crashes(mock_db):
    turn = turns_db.enqueue_turn("s5", "user", prompt="hi")

    with _runtime_env(pump=_crashing_pump()) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    # A crash (no terminal event) settles aborted — finalize ran despite the raise.
    assert outcome == "aborted"
    assert turns_db.get_turn(turn["id"])["state"] == "aborted"
    # Run lease released even on the crash path.
    assert runs_db.get_active_run("s5") is None
    env["scheduler"].assert_called_once_with("s5", turn["id"], "aborted")


async def test_interrupted_terminal_settles_aborted(mock_db):
    turn = turns_db.enqueue_turn("s6", "user", prompt="hi")

    with _runtime_env(pump=make_pump([{"type": "interrupted"}])):
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "aborted"
    assert turns_db.get_turn(turn["id"])["state"] == "aborted"


async def test_error_terminal_records_the_error_message(mock_db):
    # Regression (2026-08-31): an 'error' terminal settled the turn with
    # stop_reason='error' and error=NULL — the provider's own error body (a
    # raw 401 from the gateway) lived only in the event log, making the abort
    # undiagnosable from the turns table.
    turn = turns_db.enqueue_turn("s6err", "user", prompt="hi")
    body = '{"error":{"message":"User not found.","code":401}}'

    with _runtime_env(pump=make_pump([{"type": "error", "message": body}])):
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "aborted"
    row = turns_db.get_turn(turn["id"])
    assert row["state"] == "aborted"
    assert row["stop_reason"] == "error"
    assert row["error"] == body


# ---------------------------------------------------------------------------
# 5. on_turn_settled fired exactly once
# ---------------------------------------------------------------------------

async def test_on_turn_settled_fired_exactly_once(mock_db):
    turn = turns_db.enqueue_turn("s7", "user", prompt="hi")

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])) as env:
        await session_runtime.SessionRuntime(turn).execute()

    assert env["scheduler"].call_count == 1


# ---------------------------------------------------------------------------
# 6. Escalation — both decisions, driven through a genuinely WEDGED pump.
#    (Interrupt() standalone masked the reader-token deadlock: the pump holds
#    the token, so escalation must cancel it before anything else can read.)
# ---------------------------------------------------------------------------

def _wedged_pump(pump_entered: asyncio.Event):
    """A pump that signals entry and then blocks forever (a wedged CLI read)."""
    async def _impl(**kwargs):
        pump_entered.set()
        await asyncio.Event().wait()
        yield {}  # pragma: no cover — never reached
    return _impl


@contextmanager
def _interrupt_env(*, evict: bool):
    with ExitStack() as stack:
        stack.enter_context(patch("app.core.session_runtime.record_interrupting", new=AsyncMock()))
        stack.enter_context(patch(
            "app.core.query_session_lifecycle.interrupt_session", new=AsyncMock()
        ))
        stack.enter_context(patch(
            "app.core.session_runtime._should_evict_after_escalation", return_value=evict
        ))
        stack.enter_context(patch(
            "app.core.session_runtime._evict_pooled_client", new=AsyncMock()
        ))
        yield


async def test_interrupt_escalation_evict_cancels_wedged_pump(mock_db):
    """Escalation-evict: interrupt() cancels the wedged pump; execute()'s
    finalize settles aborted (stop_reason=interrupt_escalation) and fires the
    scheduler exactly once — interrupt() itself never settles or fires."""
    turn = turns_db.enqueue_turn("s8", "user", prompt="hi")
    pump_entered = asyncio.Event()

    with _runtime_env(pump=_wedged_pump(pump_entered)) as env, _interrupt_env(evict=True):
        rt = session_runtime.SessionRuntime(turn)
        exec_task = asyncio.create_task(rt.execute())
        await asyncio.wait_for(pump_entered.wait(), 1.0)
        # Command layer flips running -> interrupting.
        assert turns_db.interrupt_turn("s8", "stop") is not None

        state = await asyncio.wait_for(rt.interrupt(escalation_timeout=0.05), 2.0)
        outcome = await asyncio.wait_for(exec_task, 2.0)

    assert state == "aborted"
    assert outcome == "aborted"
    row = turns_db.get_turn(turn["id"])
    assert row["state"] == "aborted"
    assert row["stop_reason"] == "interrupt_escalation"
    env["scheduler"].assert_called_once_with("s8", turn["id"], "aborted")
    # Lease released and reader token free after teardown.
    assert runs_db.get_active_run("s8") is None
    assert not session_runtime.get_reader_lock("s8").locked()


async def test_interrupt_escalation_keep_drains_without_deadlock(mock_db):
    """Escalation-keep (the review blocker): the wedged pump holds the reader
    token; interrupt() must cancel it so the drain reader — started by
    execute() under the still-live lease — can acquire the token and settle
    the drained turn. Before the fix this deadlocked forever."""
    turn = turns_db.enqueue_turn("s9", "user", prompt="hi")
    pump_entered = asyncio.Event()
    drain_calls = []

    async def _fake_drain(session_id, *, drain_turn_id=None):
        # Stands in for idle_reader.run_idle_reader: acquire the reader token
        # (proving the wedged pump released it), then settle like _settle_drained.
        async with session_runtime.get_reader_lock(session_id):
            drain_calls.append((session_id, drain_turn_id))
            settled = turns_db.settle_turn(drain_turn_id, "aborted", "drain_stale_result")
            if settled is not None:
                await session_runtime._fire_on_turn_settled(session_id, drain_turn_id, "aborted")

    with _runtime_env(
        pump=_wedged_pump(pump_entered),
        bg_work=[{"id": "bg-1"}],  # live background work → the keep branch
        patch_idle=False,          # real start_idle_reader, fake drain body
    ) as env, _interrupt_env(evict=False), \
            patch("app.core.idle_reader.run_idle_reader", new=_fake_drain):
        rt = session_runtime.SessionRuntime(turn)
        exec_task = asyncio.create_task(rt.execute())
        await asyncio.wait_for(pump_entered.wait(), 1.0)
        assert turns_db.interrupt_turn("s9", "stop") is not None

        state = await asyncio.wait_for(rt.interrupt(escalation_timeout=0.05), 2.0)
        assert state == "draining"
        # The deadlock assertion: execute() completes the drain and returns.
        outcome = await asyncio.wait_for(exec_task, 2.0)

    assert drain_calls == [("s9", turn["id"])]
    assert outcome == "aborted"
    row = turns_db.get_turn(turn["id"])
    assert row["state"] == "aborted"
    assert row["stop_reason"] == "drain_stale_result"
    # Fired exactly once — by the drain settle, NOT again by execute().
    env["scheduler"].assert_called_once_with("s9", turn["id"], "aborted")
    # Lease held through the drain, released after.
    assert runs_db.get_active_run("s9") is None


async def test_interrupt_honors_durable_stop_background_on_owning_runner(mock_db):
    """A foreign-runner Stop reaches workers through the shared turn-row flag."""
    turn = turns_db.enqueue_turn("s9-bg", "user", prompt="hi")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.interrupt_turn("s9-bg", "stop", stop_background=True)
    turns_db.settle_turn(turn["id"], "aborted", "racer")

    stop_all = AsyncMock()
    with _interrupt_env(evict=False), patch(
        "app.core.query_session_lifecycle.stop_all_background_tasks",
        new=stop_all,
    ):
        state = await session_runtime.SessionRuntime(turn).interrupt(
            escalation_timeout=0.02,
        )

    assert state == "aborted"
    # Same semantics as the WebSocket Stop that set the bit (round 7): kill ALL
    # of the session's work and latch the session stopped, HERE — this is the
    # runner that owns the tasks the foreign WebSocket cannot see.
    stop_all.assert_awaited_once_with("s9-bg", user_stop=True)


@pytest.mark.parametrize("source,user_stop", [("session_closed", False), ("user_stopped", True), ("user_stop", True)])
async def test_internal_interrupt_preserves_source_on_owning_runner(mock_db, source, user_stop):
    turn = turns_db.enqueue_turn("s9-internal", "provider_agent", prompt="hi")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.interrupt_turn("s9-internal", "stop", stop_background=True, source=source)
    turns_db.settle_turn(turn["id"], "aborted", "racer")
    stop_all = AsyncMock()
    with _interrupt_env(evict=False), patch(
        "app.core.query_session_lifecycle.stop_all_background_tasks", new=stop_all,
    ):
        await session_runtime.SessionRuntime(turn).interrupt(escalation_timeout=0.02)
    stop_all.assert_awaited_once_with("s9-internal", user_stop=user_stop,
                                      **({"source": source} if not user_stop else {}))


async def test_interrupt_cancels_pending_prompts_before_signalling_the_sdk(mock_db):
    """Stop must also release whatever is blocked ON THE USER.

    A pending tool-approval prompt is awaited inside a DETACHED SDK task sitting
    in ``interaction_wait.await_interaction``; the SDK-level interrupt cannot
    reach it, so cancelling the durable rows is the only unblock — and it has to
    happen BEFORE the SDK signal, or the escalation clock runs against a session
    that is still waiting on a human.
    """
    turn = turns_db.enqueue_turn("s9d", "user", prompt="hi")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.interrupt_turn("s9d", "stop")
    turns_db.settle_turn(turn["id"], "aborted", "racer")

    order = []
    cancel = AsyncMock(side_effect=lambda sid: order.append(("cancel", sid)) or 2)
    signal = AsyncMock(side_effect=lambda sid: order.append(("sdk", sid)))

    with patch("app.core.session_runtime._should_evict_after_escalation", return_value=False), \
            patch("app.core.session_runtime.record_interrupting", new=AsyncMock()), \
            patch("app.core.query_session_lifecycle.interrupt_session", new=signal), \
            patch.object(
                session_runtime.permission_handler, "cancel_all_requests", new=cancel
            ):
        await session_runtime.SessionRuntime(turn).interrupt(escalation_timeout=0.02)

    cancel.assert_awaited_once_with("s9d")
    assert order == [("cancel", "s9d"), ("sdk", "s9d")]


async def test_interrupt_survives_a_failing_prompt_cancel(mock_db):
    """A cancel failure must never break the interrupt path."""
    turn = turns_db.enqueue_turn("s9e", "user", prompt="hi")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.interrupt_turn("s9e", "stop")
    turns_db.settle_turn(turn["id"], "aborted", "racer")

    signal = AsyncMock()
    with _interrupt_env(evict=False), \
            patch("app.core.query_session_lifecycle.interrupt_session", new=signal), \
            patch.object(
                session_runtime.permission_handler, "cancel_all_requests",
                new=AsyncMock(side_effect=RuntimeError("db down")),
            ):
        state = await session_runtime.SessionRuntime(turn).interrupt(
            escalation_timeout=0.02
        )

    assert state == "aborted"
    signal.assert_awaited_once_with("s9e")


async def test_interrupt_cancels_the_durable_prompt_row(mock_db, monkeypatch):
    """End to end against the real handler: the pending row leaves ``pending``,
    which is what actually releases the detached waiter."""
    from app.db import interactions

    turn = turns_db.enqueue_turn("s9f", "user", prompt="hi")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.interrupt_turn("s9f", "stop")
    turns_db.settle_turn(turn["id"], "aborted", "racer")

    row = interactions.create_interaction(
        "s9f", "permission",
        {"request_id": "r-1", "tool_name": "Bash", "tool_input": {}}, 300,
    )

    with _interrupt_env(evict=False):
        await session_runtime.SessionRuntime(turn).interrupt(escalation_timeout=0.02)

    assert interactions.get_interaction(row["id"])["state"] == "cancelled"


async def test_interrupt_escalation_keep_noop_when_turn_already_settled(mock_db):
    """Escalation-keep race: the turn settled while interrupt() was deciding —
    escalate_to_draining no-ops and interrupt() reports the real state."""
    turn = turns_db.enqueue_turn("s9b", "user", prompt="hi")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.interrupt_turn("s9b", "stop")
    turns_db.settle_turn(turn["id"], "aborted", "racer")

    with _interrupt_env(evict=False):
        rt = session_runtime.SessionRuntime(turn)
        state = await rt.interrupt(escalation_timeout=0.02)

    assert state == "aborted"
    assert rt._drain_after_cancel is False


# ---------------------------------------------------------------------------
# 7. Exactly-once settlement when a racer settled first
# ---------------------------------------------------------------------------

async def test_finalize_does_not_refire_when_racer_settled_first(mock_db):
    """If another actor (interrupt escalation / reap) settled the turn first,
    finalize's no-op transition must NOT fire on_turn_settled again."""
    turn = turns_db.enqueue_turn("s10", "user", prompt="hi")

    async def _pump(**kwargs):
        # A racer settles the turn while the pump is still live.
        turns_db.settle_turn(turn["id"], "aborted", "racer")
        yield {"type": "interrupted"}

    with _runtime_env(pump=_pump) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "aborted"
    assert turns_db.get_turn(turn["id"])["stop_reason"] == "racer"
    env["scheduler"].assert_not_called()


# ---------------------------------------------------------------------------
# 8. Broadcast trim — render frames ride SSE only (legacy :4155 parity)
# ---------------------------------------------------------------------------

async def test_run_pump_broadcast_trims_render_frames(mock_db):
    """The recorder sees EVERY event; the broadcast adapter must only see the
    kept lifecycle/tool frames, each tagged with the cross-session guard."""
    turn = turns_db.enqueue_turn("s11", "user", prompt="hi")
    events = [
        {"type": "chunk", "content": "x"},
        {"type": "stream_delta", "delta": "y"},
        {"type": "subagent_chunk", "content": "z"},
        {"type": "usage_update", "usage": {}},
        {"type": "tool_use", "name": "Bash"},
        {"type": "subagent_tool_use", "name": "Read"},
        {"type": "done", "metadata": {}},
    ]

    with _runtime_env(pump=make_pump(events)) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    assert env["recorder"].on_event.await_count == len(events)
    broadcast_events = [c.args[1] for c in env["broadcaster"].await_args_list]
    assert [e["type"] for e in broadcast_events] == ["tool_use", "subagent_tool_use", "done"]
    assert all(e["session_id"] == "s11" for e in broadcast_events)


async def test_unpark_turn_threads_completion_frames_to_provider_overrides(mock_db):
    frames = [{
        "task_id": "worker-1",
        "status": "completed",
        "summary": "done",
        "origin": "provider_agent",
    }]
    turn = turns_db.enqueue_turn(
        "s12",
        "unpark",
        prompt="background task completed",
        payload={"pending_frames": frames, "task_ids": ["worker-1"]},
    )
    captured = {}

    async def pump(**kwargs):
        captured.update(kwargs)
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=pump):
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    assert captured["overrides"]["background_task_completions"] == frames


@pytest.mark.parametrize("source", ["user", "unpark", "agent_message", "wakeup", "promotion"])
async def test_turn_source_threads_to_provider_overrides(mock_db, source):
    """Fluid records the source on TurnStarted; it reaches the adapter here."""
    turn = turns_db.enqueue_turn("s12s", source, prompt="go")
    captured = {}

    async def pump(**kwargs):
        captured.update(kwargs)
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=pump):
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    assert captured["overrides"]["turn_source"] == source


async def test_a_failover_retry_of_a_wake_records_the_wake_source(mock_db):
    """#6: a failover retry is enqueued as source=failover with the retried
    turn as its parent. It used to record source='user', which skewed the
    wake metrics the field exists for; it now records the source of the turn
    it retries, on fluid's TurnStarted and on the app's turn.started."""
    wake = turns_db.enqueue_turn("s12f", "unpark", prompt="task done")
    first = turns_db.enqueue_turn("s12f", "failover", prompt="task done",
                                  parent_turn_id=wake["id"])
    retry = turns_db.enqueue_turn("s12f", "failover", prompt="task done",
                                  parent_turn_id=first["id"])
    captured = {}

    async def pump(**kwargs):
        captured.update(kwargs)
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=pump) as env:
        outcome = await session_runtime.SessionRuntime(retry).execute()

    assert outcome == "completed"
    assert captured["overrides"]["turn_source"] == "unpark"
    assert env["recorder"].start.await_args.kwargs["source"] == "unpark"


async def test_an_automation_turn_records_the_automation_source(mock_db):
    turn = turns_db.enqueue_turn("s12g", "automation", prompt="nightly report")

    async def pump(**kwargs):
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=pump) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    assert env["recorder"].start.await_args.kwargs["source"] == "automation"


async def test_unpark_turn_records_terminal_task_events_after_start(mock_db):
    """The wake turn is the delivery point for a completion that happened
    between turns, so it must write the terminal task.updated to the event log —
    right after turn.started, before the pump runs (the FE no longer
    force-settles background shells at turn end, #991)."""
    frames = [
        {"task_id": "shell-1", "status": "completed", "kind": "shell"},
        {"task_id": "shell-2", "status": "failed", "kind": "shell"},
    ]
    turn = turns_db.enqueue_turn(
        "s12t",
        "unpark",
        prompt="background task completed",
        payload={"pending_frames": frames, "task_ids": ["shell-1", "shell-2"]},
    )
    order = []

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])) as env:
        rec = env["recorder"]
        rec.start.side_effect = lambda *a, **k: order.append("start")
        rec.record_task_terminal.side_effect = lambda *a, **k: order.append("terminal")
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    rec.record_task_terminal.assert_awaited_once_with(frames)
    assert order == ["start", "terminal"]


async def test_plain_turn_records_no_terminal_task_events(mock_db):
    """No claimed completions on the payload → nothing to settle."""
    turn = turns_db.enqueue_turn("s12u", "user", prompt="hi")

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    env["recorder"].record_task_terminal.assert_not_awaited()


async def test_terminal_task_event_failure_never_kills_the_wake_turn(mock_db):
    """Best-effort: a recorder that blows up on the terminal append still lets
    the wake turn run and settle."""
    frames = [{"task_id": "shell-9", "status": "completed"}]
    turn = turns_db.enqueue_turn(
        "s12v",
        "unpark",
        prompt="background task completed",
        payload={"pending_frames": frames, "task_ids": ["shell-9"]},
    )

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])) as env:
        env["recorder"].record_task_terminal.side_effect = RuntimeError("boom")
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"


# ---------------------------------------------------------------------------
# W3 — WS-leg trim: redundant render content never reaches the broadcast leg
# ---------------------------------------------------------------------------

async def test_pump_does_not_broadcast_redundant_render_frames(mock_db):
    """The pump yields the COMPLETE assistant text block and a thinking marker
    after the deltas that already carried the same content. Neither has a
    frontend WS dispatch case, and both already ride the SSE feed — so they must
    reach the recorder (SSE) but NOT the broadcast leg (W3)."""
    turn = turns_db.enqueue_turn("s1w3", "user", prompt="hi")
    frames = [
        {"type": "stream_delta", "content": "he"},
        {"type": "text", "content": "hello world"},
        {"type": "thinking_block", "content": "hmm"},
        {"type": "tool_use", "id": "t1", "input": {"path": "a.py"}},
        {"type": "done", "metadata": {}},
    ]

    with _runtime_env(pump=make_pump(frames)) as env:
        await session_runtime.SessionRuntime(turn).execute()

    # Everything still reaches the recorder → session event log → SSE.
    recorded = [c.args[0]["type"] for c in env["recorder"].on_event.call_args_list]
    assert recorded == ["stream_delta", "text", "thinking_block", "tool_use", "done"]

    # Only the non-trimmed frames hit the WS/broadcast leg.
    broadcast = [c.args[1]["type"] for c in env["broadcaster"].await_args_list]
    assert broadcast == ["tool_use", "done"]
    assert "text" not in broadcast
    assert "thinking_block" not in broadcast
    assert "stream_delta" not in broadcast


async def test_pump_caps_oversized_tool_input_on_the_ws_leg_only(mock_db):
    """The tool_use FRAME is correctness-bearing (the client's pendingTools set)
    so it is still broadcast — but a multi-MB input, which no WS consumer reads,
    is replaced by a marker. The recorder's copy keeps the full input."""
    big = "x" * (session_runtime._WS_MAX_TOOL_INPUT_CHARS * 4)
    turn = turns_db.enqueue_turn("s1w3b", "user", prompt="hi")
    frames = [
        {"type": "tool_use", "id": "t1", "input": {"content": big}},
        {"type": "done", "metadata": {}},
    ]

    with _runtime_env(pump=make_pump(frames)) as env:
        await session_runtime.SessionRuntime(turn).execute()

    recorded = env["recorder"].on_event.call_args_list[0].args[0]
    assert recorded["input"] == {"content": big}  # SSE/persistence untouched

    broadcast = env["broadcaster"].await_args_list[0].args[1]
    assert broadcast["id"] == "t1"
    assert broadcast["input"]["_ws_truncated"] is True
    assert broadcast["input"]["_original_chars"] > session_runtime._WS_MAX_TOOL_INPUT_CHARS


# ---------------------------------------------------------------------------
# Mid-turn task notifications — the steer channel a background completion takes
# while the parent is still inside its turn
# ---------------------------------------------------------------------------

def _notification_frames():
    return [{"task_id": "bg-1", "status": "completed", "summary": "the report"}]


async def test_task_notification_steer_is_submitted_and_registered(mock_db):
    """The submit reaches the adapter under the scheduler's notification id, and
    the frames are held for whichever of the two receipts comes back — fluid's
    delivery, or the turn ending without one."""
    turn = turns_db.enqueue_turn("sn1", "user", prompt="a long turn")
    turns_db.start_turn(turn["id"], None, "runner")
    adapter = MagicMock(name="adapter")
    adapter.steer = AsyncMock()
    frames = _notification_frames()

    rt = session_runtime.SessionRuntime(turns_db.get_turn(turn["id"]))
    with patch.object(
        session_runtime.SessionRuntime, "_live_adapter", return_value=adapter,
    ):
        accepted = await rt.steer_task_notification(
            "<task-notification/>",
            notification_id="task-notify:abc",
            frames=frames,
            task_ids=["bg-1"],
        )

    assert accepted is True
    # System-origin: the notice must not arrive in the user's steering marker.
    adapter.steer.assert_awaited_once_with(
        "<task-notification/>", id="task-notify:abc", origin="system",
    )
    assert rt._task_steers["task-notify:abc"] == {
        "frames": frames, "task_ids": ["bg-1"],
    }


async def test_task_notification_steer_refused_leaves_nothing_behind(mock_db):
    """fluid raises when there is nothing live to steer into. The caller gets
    False (and enqueues the ordinary wake), and no frames are left registered to
    be handed back a second time at finalize."""
    turn = turns_db.enqueue_turn("sn2", "user", prompt="a long turn")
    turns_db.start_turn(turn["id"], None, "runner")
    adapter = MagicMock(name="adapter")
    adapter.steer = AsyncMock(side_effect=RuntimeError("no running turn"))

    rt = session_runtime.SessionRuntime(turns_db.get_turn(turn["id"]))
    with patch.object(
        session_runtime.SessionRuntime, "_live_adapter", return_value=adapter,
    ):
        accepted = await rt.steer_task_notification(
            "<task-notification/>",
            notification_id="task-notify:def",
            frames=_notification_frames(),
            task_ids=["bg-1"],
        )

    assert accepted is False
    assert rt._task_steers == {}


async def test_task_notification_steer_refused_on_a_dying_turn(mock_db):
    """A turn the user is already killing swallows the notification without
    answering it, and there is no queue row behind it to run instead — so the
    steer is refused and the at-rest wake keeps the delivery."""
    turn = turns_db.enqueue_turn("sn2b", "user", prompt="stop me")
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.interrupt_turn("sn2b", "stop")
    adapter = MagicMock(name="adapter")
    adapter.steer = AsyncMock()

    rt = session_runtime.SessionRuntime(turns_db.get_turn(turn["id"]))
    with patch.object(
        session_runtime.SessionRuntime, "_live_adapter", return_value=adapter,
    ):
        accepted = await rt.steer_task_notification(
            "<task-notification/>",
            notification_id="task-notify:ghi",
            frames=_notification_frames(),
            task_ids=["bg-1"],
        )

    assert accepted is False
    adapter.steer.assert_not_awaited()
    assert rt._task_steers == {}


async def test_task_notification_receipt_settles_the_card_and_latches(mock_db):
    """The delivery receipt is what commits the notification: the event log
    learns the task is done (so the frontend card stops running) and the
    scheduler latches terminal_notified. No queue row is touched — there is
    none."""
    frames = _notification_frames()
    rt = session_runtime.SessionRuntime(
        {"session_id": "sn3", "id": "turn-sn3", "source": "user"}
    )
    rt._task_steers["task-notify:jkl"] = {"frames": frames, "task_ids": ["bg-1"]}
    recorder = _recorder_mock()
    delivered = MagicMock(name="on_task_notification_delivered")

    with patch(
        "app.core.turn_scheduler.on_task_notification_delivered", new=delivered,
    ):
        await rt._on_steer_delivered(
            recorder, {"steers": [{"id": "task-notify:jkl"}]},
        )

    recorder.record_task_terminal.assert_awaited_once_with(frames)
    delivered.assert_called_once_with("sn3", ["bg-1"])
    assert rt._task_steers == {}


async def test_failed_task_receipt_remains_recoverable(mock_db):
    frames = _notification_frames()
    rt = session_runtime.SessionRuntime(
        {"session_id": "failed-task", "id": "failed-task-turn", "source": "user"}
    )
    entry = {"frames": frames, "task_ids": ["bg-1"]}
    rt._task_steers["task-notify:failed"] = entry
    recorder = _recorder_mock()
    recorder.recording_failed = False

    async def fail_recording(_frames):
        recorder.recording_failed = True
        rt._on_recorder_recording_failed()
        return 0

    recorder.record_task_terminal.side_effect = fail_recording
    with patch("app.core.query_session_lifecycle.interrupt_session", new_callable=AsyncMock), \
         patch("app.core.turn_scheduler.retire_task_terminals", new_callable=AsyncMock) as retire, \
         patch("app.core.turn_scheduler.on_task_notification_delivered") as delivered, \
         patch("app.core.turn_scheduler.on_task_notification_stranded", new_callable=AsyncMock) as stranded:
        await rt._on_steer_delivered(recorder, {"steers": [{"id": "task-notify:failed"}]})
        assert rt._abort_reason == "recording_failed"
        assert rt._task_steers == {"task-notify:failed": entry}
        retire.assert_not_awaited()
        delivered.assert_not_called()
        await rt._strand_task_steers()
        stranded.assert_awaited_once_with("failed-task", frames, ["bg-1"])
        await asyncio.sleep(0)


async def test_failed_user_receipt_keeps_queue_without_legacy_duplicate(mock_db):
    row = turns_db.enqueue_turn("failed-user", "user", prompt="follow-up")
    rt = session_runtime.SessionRuntime(
        {"session_id": "failed-user", "id": "failed-user-turn", "source": "user"}
    )
    recorder = _recorder_mock()

    async def fail_recording(*args, **kwargs):
        rt._on_recorder_recording_failed()
        return False

    recorder.record_steered_user_message = AsyncMock(side_effect=fail_recording)
    with patch("app.core.query_session_lifecycle.interrupt_session", new_callable=AsyncMock), \
         patch.object(rt, "_persist_user_message", new_callable=AsyncMock) as persist, \
         patch.object(rt, "_absorb_delivered_row", new_callable=AsyncMock) as absorb:
        await rt._on_steer_delivered(recorder, {"steers": [{"id": row["id"]}]})
        assert rt._abort_reason == "recording_failed"
        assert turns_db.get_turn(row["id"])["state"] == "queued"
        persist.assert_not_awaited()
        absorb.assert_not_awaited()
        await asyncio.sleep(0)


async def test_undelivered_task_notification_is_handed_back_at_finalize(mock_db):
    """fluid drops an unreceipted steer at turn end. A user's message survives
    that on its still-queued row; a notification has none, so the runtime hands
    it back to the scheduler, which enqueues the at-rest wake after all."""
    turn = turns_db.enqueue_turn("sn4", "user", prompt="hi")
    frames = _notification_frames()
    stranded = AsyncMock(name="on_task_notification_stranded")

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])):
        rt = session_runtime.SessionRuntime(turn)
        rt._task_steers["task-notify:mno"] = {
            "frames": frames, "task_ids": ["bg-1"],
        }
        with patch(
            "app.core.turn_scheduler.on_task_notification_stranded", new=stranded,
        ):
            outcome = await rt.execute()

    assert outcome == "completed"
    stranded.assert_awaited_once_with("sn4", frames, ["bg-1"])
    assert rt._task_steers == {}


async def test_wake_turn_latches_terminal_notified(mock_db):
    """The other delivery path: a wake turn carrying pending_frames IS the
    delivery of those completions, so it latches terminal_notified too. The flag
    used to stay False forever for every clean completion."""
    frames = [{"task_id": "shell-5", "status": "completed"}]
    turn = turns_db.enqueue_turn(
        "sn5", "unpark", prompt="background task completed",
        payload={"pending_frames": frames, "task_ids": ["shell-5"]},
    )
    delivered = MagicMock(name="on_task_notification_delivered")

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])):
        with patch(
            "app.core.turn_scheduler.on_task_notification_delivered", new=delivered,
        ):
            outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    delivered.assert_called_once_with("sn5", ["shell-5"])


async def test_plain_turn_latches_nothing(mock_db):
    """No claimed completions on the payload → nothing was delivered."""
    turn = turns_db.enqueue_turn("sn6", "user", prompt="hi")
    delivered = MagicMock(name="on_task_notification_delivered")

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])):
        with patch(
            "app.core.turn_scheduler.on_task_notification_delivered", new=delivered,
        ):
            outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    delivered.assert_not_called()


# ---------------------------------------------------------------------------
# Durable surface for an error terminal
# ---------------------------------------------------------------------------

def _seed_message_session(session_id):
    """session_messages has a FK onto sessions, so a durable system row needs a
    real session behind it. profile_id stays NULL for the same reason as
    ``_seed_session`` above: this module routes a bare shim connection with no
    seeded "default" profile."""
    from app.db import database

    database.create_session(session_id=session_id, profile_id=None)


def _system_rows(session_id):
    from app.db import sessions as sessions_db

    return [
        m for m in sessions_db.get_session_messages(session_id)
        if m.get("role") == "system"
    ]


async def test_error_terminal_persists_a_durable_system_message(mock_db):
    """The error frame reached the live WS and the turns row but never the
    message list, so a reload showed the user's prompt and nothing else."""
    _seed_message_session("se1")
    turn = turns_db.enqueue_turn("se1", "user", prompt="do the thing")
    pump = make_pump([
        {"type": "error", "message": "RuntimeError('client has been closed')"},
    ])

    with _runtime_env(pump=pump):
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "aborted"
    (row,) = _system_rows("se1")
    assert "client has been closed" in row["content"]
    assert row["metadata"]["subtype"] == "error"
    assert row["metadata"]["system_data"]["turn_id"] == turn["id"]


async def test_a_wake_turns_error_message_says_it_was_automatic(mock_db):
    _seed_message_session("se2")
    turn = turns_db.enqueue_turn("se2", "unpark", prompt="<task-notification/>")

    with _runtime_env(pump=make_pump([{"type": "error", "message": "boom"}])):
        await session_runtime.SessionRuntime(turn).execute()

    (row,) = _system_rows("se2")
    assert "started automatically" in row["content"]


async def test_a_clean_turn_writes_no_error_row(mock_db):
    _seed_message_session("se3")
    turn = turns_db.enqueue_turn("se3", "user", prompt="hi")

    with _runtime_env(pump=make_pump([{"type": "done", "metadata": {}}])):
        await session_runtime.SessionRuntime(turn).execute()

    assert _system_rows("se3") == []


# ---------------------------------------------------------------------------
# Per-turn progress watchdog
# ---------------------------------------------------------------------------

@contextmanager
def _progress_timeout(seconds):
    with patch.object(
        session_runtime.settings, "turn_progress_timeout_seconds", seconds,
        create=True,
    ):
        yield


async def test_watchdog_terminalizes_a_turn_that_stops_producing_events(mock_db):
    """A host tool call that never returns leaves the turn with no events at
    all; the cell's own timeout cannot interrupt it and nothing above bounded
    the turn, so a session sat 'running' for three hours."""
    _seed_message_session("wd1")
    turn = turns_db.enqueue_turn("wd1", "user", prompt="drive the browser")

    async def _wedged_pump(**kwargs):
        yield {"type": "tool_use", "name": "browser_eval"}
        await asyncio.sleep(30)  # the host call that never comes back
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=_wedged_pump), _progress_timeout(0.25):
        outcome = await asyncio.wait_for(
            session_runtime.SessionRuntime(turn).execute(), 10.0,
        )

    assert outcome == "aborted"
    row = turns_db.get_turn(turn["id"])
    assert row["stop_reason"] == "error"
    assert "progress watchdog" in (row["error"] or "")
    (message,) = _system_rows("wd1")
    assert "progress watchdog" in message["content"]


async def test_watchdog_leaves_a_turn_blocked_on_a_user_question_alone(mock_db):
    """An AskUser round-trip is legitimately silent for as long as the person
    takes; its deadline is user_question_timeout_seconds, not this one."""
    _seed_message_session("wd2")
    turn = turns_db.enqueue_turn("wd2", "user", prompt="ask me something")
    answered = asyncio.Event()

    async def _asking_pump(**kwargs):
        yield {"type": "tool_use", "name": "AskUser"}
        await answered.wait()
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=_asking_pump), _progress_timeout(0.25):
        with patch("app.core.interaction_wait.has_waiters", lambda sid: True):
            task = asyncio.create_task(session_runtime.SessionRuntime(turn).execute())
            # Six times the watchdog budget with no event at all — and the turn
            # is still alive, because the silence is a person thinking.
            await asyncio.sleep(1.5)
            assert not task.done()
            answered.set()
            outcome = await asyncio.wait_for(task, 5.0)

    assert outcome == "completed"
    assert _system_rows("wd2") == []


async def test_watchdog_does_not_fire_on_a_turn_that_keeps_producing_events(mock_db):
    turn = turns_db.enqueue_turn("wd3", "user", prompt="a long but live turn")

    async def _busy_pump(**kwargs):
        for _ in range(12):
            await asyncio.sleep(0.05)
            yield {"type": "text", "text": "still here"}
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=_busy_pump), _progress_timeout(0.25):
        outcome = await asyncio.wait_for(
            session_runtime.SessionRuntime(turn).execute(), 10.0,
        )

    assert outcome == "completed"


async def test_watchdog_is_off_when_the_timeout_is_zero(mock_db):
    turn = turns_db.enqueue_turn("wd4", "user", prompt="hi")

    async def _slow_pump(**kwargs):
        await asyncio.sleep(0.4)
        yield {"type": "done", "metadata": {}}

    with _runtime_env(pump=_slow_pump), _progress_timeout(0):
        rt = session_runtime.SessionRuntime(turn)
        outcome = await asyncio.wait_for(rt.execute(), 10.0)

    assert outcome == "completed"
    assert rt._progress_watchdog is None


# ---------------------------------------------------------------------------
# Lost run lease — the turn stops itself and the sweep never starts it twice
# (#1444)
# ---------------------------------------------------------------------------

def _break_lease(conn, run_id: str, failure: str) -> None:
    """Kill the run lease underneath a live turn, the way production loses it.

    ``fence``: another claim reaped the lapsed lease (the row left ``active``),
    so the next ``renew_run`` misses its guard. ``db_error``: the database was
    unreachable for longer than the TTL, so the lease lapsed in the DB while
    every renewal raised."""
    if failure == "fence":
        conn.execute(
            "UPDATE runs SET state='expired', "
            "lease_expires_at=datetime('now', '-120 seconds') WHERE id=?",
            (run_id,),
        )
    else:
        conn.execute(
            "UPDATE runs SET lease_expires_at=datetime('now', '-120 seconds') WHERE id=?",
            (run_id,),
        )
    conn.commit()


@contextmanager
def _count_starts():
    """Spy on ``turns_db.start_turn``: the ids of every turn it actually moved
    into the live slot. The session reader lock serializes pumps inside one
    process, so a second start is the first observable sign of a double run."""
    real_start = turns_db.start_turn
    started = []

    def _spy(turn_id, *args, **kwargs):
        row = real_start(turn_id, *args, **kwargs)
        if row is not None:
            started.append(turn_id)
        return row

    with patch.object(turns_db, "start_turn", side_effect=_spy):
        yield started


async def _sweep_and_drain_kicks(timeout: float = 1.0) -> list:
    """Run the real sweep's reap+kick once, then give every kick it fired (the
    claim pass into ``execute``) time to start its turn. Returns those kicks so
    the caller can cancel any that are still running."""
    from app.core import streaming_status, turn_scheduler

    await streaming_status._reap_and_kick_turns()
    pending = list(turn_scheduler._kick_tasks)
    if pending:
        await asyncio.wait(pending, timeout=timeout)
    return pending


@pytest.mark.parametrize("failure", ["fence", "db_error"])
async def test_lost_lease_stops_the_turn_and_the_sweep_never_starts_it_twice(
    mock_db, monkeypatch, failure,
):
    sid = f"lease-loss-{failure}"
    turn = turns_db.enqueue_turn(sid, "user", prompt="hello")
    pump_calls = []
    pump_entered = asyncio.Event()

    async def _generating_pump(**kwargs):
        # A provider turn that keeps generating until something stops it.
        pump_calls.append(kwargs)
        pump_entered.set()
        await asyncio.Event().wait()
        yield {"type": "done", "metadata": {}}

    monkeypatch.setattr(session_runtime, "RUN_LEASE_RENEW_SECONDS", 0.02)
    if failure == "db_error":
        # Shrink the TTL so "renewals failed for a whole TTL" fits in test
        # time. The lease row is backdated below either way.
        monkeypatch.setattr(session_runtime, "RUN_LEASE_TTL_SECONDS", 0.3)
    broken = asyncio.Event()
    renew_attempts = []
    real_renew = runs_db.renew_run

    def _renew(*args, **kwargs):
        renew_attempts.append(args)
        if failure == "db_error" and broken.is_set():
            raise RuntimeError("database unreachable")
        return real_renew(*args, **kwargs)

    tasks = []
    with _runtime_env(pump=_generating_pump) as env, patch.object(
        runs_db, "renew_run", side_effect=_renew,
    ), patch(
        "app.core.query_session_lifecycle.interrupt_session", new=AsyncMock(),
    ) as interrupt, _count_starts() as started:
        try:
            first = asyncio.create_task(session_runtime.execute(turn))
            tasks.append(first)
            await asyncio.wait_for(pump_entered.wait(), 2.0)
            run_id = turns_db.get_turn(turn["id"])["run_id"]
            assert run_id is not None

            _break_lease(mock_db, run_id, failure)
            broken.set()
            seen = len(renew_attempts)
            await asyncio.sleep(0.6 if failure == "db_error" else 0.2)
            assert len(renew_attempts) > seen, "the heartbeat never tried to renew"

            tasks.extend(await _sweep_and_drain_kicks())

            # The turn was started, and the provider called, exactly once.
            assert started == [turn["id"]]
            assert len(pump_calls) == 1

            outcome = await asyncio.wait_for(first, 2.0)
            assert outcome == "aborted"
            row = turns_db.get_turn(turn["id"])
            assert row["state"] == "aborted"
            assert row["stop_reason"] == "lease_lost"
            interrupt.assert_awaited_with(sid)
            env["scheduler"].assert_called_once()
            assert runs_db.get_active_run(sid) is None

            # Nothing is left for a later sweep either.
            tasks.extend(await _sweep_and_drain_kicks())
            assert started == [turn["id"]]
            assert len(pump_calls) == 1
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def test_sweep_skips_a_lapsed_lease_turn_still_running_in_this_process(mock_db):
    """The lease can lapse before the heartbeat's next beat notices (a stalled
    loop, a DB blip). The sweep reads only the DB, so it has to skip a turn
    this process is still executing instead of requeueing it for a second
    start."""
    sid = "lease-lapsed-alive"
    turn = turns_db.enqueue_turn(sid, "user", prompt="hello")
    pump_calls = []
    pump_entered = asyncio.Event()
    release = asyncio.Event()

    async def _pump(**kwargs):
        pump_calls.append(kwargs)
        pump_entered.set()
        await release.wait()
        yield {"type": "done", "metadata": {}}

    tasks = []
    with _runtime_env(pump=_pump), _count_starts() as started:
        try:
            first = asyncio.create_task(session_runtime.execute(turn))
            tasks.append(first)
            await asyncio.wait_for(pump_entered.wait(), 2.0)
            run_id = turns_db.get_turn(turn["id"])["run_id"]
            _break_lease(mock_db, run_id, "db_error")

            tasks.extend(await _sweep_and_drain_kicks())

            assert started == [turn["id"]]
            assert len(pump_calls) == 1
            row = turns_db.get_turn(turn["id"])
            assert row["state"] == "running"
            assert row["run_id"] == run_id

            release.set()
            assert await asyncio.wait_for(first, 2.0) == "completed"
            # A requeued copy would take the reader lock now and call the
            # provider again.
            await asyncio.sleep(0.2)
            assert started == [turn["id"]]
            assert len(pump_calls) == 1
        finally:
            release.set()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
