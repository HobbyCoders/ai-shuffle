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


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def test_db():
    from tests.pg_shim import new_sqlite_connection
    conn = new_sqlite_connection()
    yield conn
    conn.close()


@pytest.fixture
def mock_db(test_db):
    from tests.pg_shim import route_db_to
    with route_db_to(test_db):
        yield test_db


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

def _make_pump(events):
    """An async-generator stand-in for ``_stream_to_websocket_impl`` that yields
    ``events`` (accepts and ignores the impl's kwargs)."""
    async def _impl(**kwargs):
        for e in events:
            yield e
    return _impl


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
# ---------------------------------------------------------------------------

async def test_execute_requires_run_claim(mock_db):
    """No run claim (another runner owns the session) → the turn stays queued and
    execute returns None without ever starting it."""
    turn = turns_db.enqueue_turn("s1", "user", prompt="hi")
    # Someone else already owns the session's active run.
    other = runs_db.claim_run("s1", "other-runner", 60)
    assert other is not None

    with _runtime_env(pump=_make_pump([{"type": "done", "metadata": {}}])) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome is None
    # Turn untouched — still queued, no start race consumed.
    assert turns_db.get_turn(turn["id"])["state"] == "queued"
    env["scheduler"].assert_not_called()
    # A runtime that never won start_turn never registered itself.
    assert session_runtime.get_live_runtime("s1") is None


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

    with _runtime_env(pump=_make_pump([{"type": "done", "metadata": {}}])):
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

async def test_finalize_parks_when_background_work_survives(mock_db):
    turn = turns_db.enqueue_turn("s3", "user", prompt="hi")

    with _runtime_env(
        pump=_make_pump([{"type": "done", "metadata": {}}]),
        bg_work=[{"id": "task-1"}],
    ) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "parked"
    assert turns_db.get_turn(turn["id"])["state"] == "parked"
    # payload.task_ids recorded, turn.parked event tapped, idle reader started.
    assert turns_db.get_turn(turn["id"])["payload"]["task_ids"] == ["task-1"]
    env["record_parked"].assert_awaited_once()
    env["start_idle"].assert_called_once_with("s3")
    env["scheduler"].assert_called_once_with("s3", turn["id"], "parked")


async def test_finalize_completes_when_no_background_work(mock_db):
    turn = turns_db.enqueue_turn("s4", "user", prompt="hi")

    with _runtime_env(pump=_make_pump([{"type": "done", "metadata": {}}])) as env:
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "completed"
    assert turns_db.get_turn(turn["id"])["state"] == "completed"
    env["start_idle"].assert_not_called()
    env["record_parked"].assert_not_called()
    env["scheduler"].assert_called_once_with("s4", turn["id"], "completed")


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
    with _runtime_env(pump=_make_pump([{"type": "done", "metadata": {}}])):
        outcome = await session_runtime.SessionRuntime(fresh).execute()

    assert outcome == "completed"
    # The stale parked residue was self-healed to completed.
    assert turns_db.get_turn(stale["id"])["state"] == "completed"


async def test_turn_start_self_heal_spares_park_with_running_bg_work(mock_db):
    """FIX 2: the self-heal is GATED on has_active_background_work. A queued turn
    intentionally starts while a prior turn is parked on RUNNING work; that parked
    row is the live signal _cli_owns_at_rest_wake / _latest_context_turn rely on,
    so settling it early would risk a duplicate wake. With running bg work the
    stale-looking parked row must SURVIVE."""
    sid = "s4spare"
    stale = turns_db.enqueue_turn(sid, "user", prompt="old")
    turns_db.start_turn(stale["id"], run_id="run-old")
    turns_db.park_turn(stale["id"], ["task-live"])
    assert turns_db.get_turn(stale["id"])["state"] == "parked"

    fresh = turns_db.enqueue_turn(sid, "user", prompt="new")
    # bg_work present → has_active_background_work True → self-heal is skipped.
    with _runtime_env(
        pump=_make_pump([{"type": "done", "metadata": {}}]),
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

    with _runtime_env(pump=_make_pump([{"type": "done", "metadata": {}}])):
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

    with _runtime_env(pump=_make_pump([{"type": "done", "metadata": {}}])):
        await session_runtime.SessionRuntime(turn).execute()

    assert _status_of(sid) == "active"


async def test_turn_start_leaves_a_non_failed_status_alone(mock_db):
    """Archived is not a run verdict — the revive must not touch it."""
    sid = "s4archived"
    _seed_session(sid, "archived")
    turn = turns_db.enqueue_turn(sid, "user", prompt="hi")

    with _runtime_env(pump=_make_pump([{"type": "done", "metadata": {}}])):
        await session_runtime.SessionRuntime(turn).execute()

    assert _status_of(sid) == "archived"


async def test_queued_turn_that_never_starts_does_not_revive(mock_db):
    """The revive is keyed on the turn STARTING, not on it being enqueued: a
    turn that loses the run claim leaves the verdict standing."""
    sid = "s4failedqueued"
    _seed_session(sid, "failed")
    turn = turns_db.enqueue_turn(sid, "user", prompt="hi")
    # Another runner owns the session's active run → execute bails before 4c.
    assert runs_db.claim_run(sid, "other-runner", 60) is not None

    with _runtime_env(pump=_make_pump([{"type": "done", "metadata": {}}])):
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

    with _runtime_env(pump=_make_pump([{"type": "interrupted"}])):
        outcome = await session_runtime.SessionRuntime(turn).execute()

    assert outcome == "aborted"
    assert turns_db.get_turn(turn["id"])["state"] == "aborted"


# ---------------------------------------------------------------------------
# 5. on_turn_settled fired exactly once
# ---------------------------------------------------------------------------

async def test_on_turn_settled_fired_exactly_once(mock_db):
    turn = turns_db.enqueue_turn("s7", "user", prompt="hi")

    with _runtime_env(pump=_make_pump([{"type": "done", "metadata": {}}])) as env:
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

    with _runtime_env(pump=_make_pump(events)) as env:
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
