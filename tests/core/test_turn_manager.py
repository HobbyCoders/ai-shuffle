"""Unit tests for app/core/turn_manager.py — the command layer over turns."""

import asyncio
import threading
import time
from unittest.mock import patch

import pytest

from app.core import turn_manager
from app.db import turns

# Captured before the autouse fixture below stubs it out, so the two tests that
# cover the registration itself can still reach the real implementation.
_REAL_ENSURE_GATEWAY_LISTENER = turn_manager._ensure_gateway_listener


def _start_live_turn(turn_id):
    from app.db import runs
    sid = turns.get_turn(turn_id)["session_id"]
    run = runs.get_active_run(sid) or runs.claim_run(sid, "test-runner", 60)
    return turns.start_turn(turn_id, run_id=run["id"])


@pytest.fixture(autouse=True)
def reset_kick():
    """The kick + interrupt-executor seams are module globals — clear them
    around every test."""
    turn_manager.register_kick(None)
    turn_manager.register_interrupt_executor(None)
    yield
    turn_manager.register_kick(None)
    turn_manager.register_interrupt_executor(None)


@pytest.fixture(autouse=True)
def no_gateway_thread():
    """Keep the wait helpers' NOTIFY registry, drop the LISTEN thread.

    ``_ensure_gateway_listener`` would spin up the process-wide event-gateway
    daemon (a real ``psycopg2`` connect) the first time a test waits. The wake
    registry itself is pure in-process bookkeeping, so tests drive it by calling
    ``turn_manager._on_gateway_tick`` directly; the one test that covers the
    registration itself un-patches this. Also asserts no waiter leaks.
    """
    turn_manager._waiters.clear()
    with patch.object(turn_manager, "_ensure_gateway_listener", lambda: None):
        yield
    assert turn_manager._waiters == {}, "a wait helper leaked its NOTIFY waiter"


# ---------------------------------------------------------------------------
# submit + kick
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("async_call", [False, True])
@pytest.mark.parametrize("state", ["running", "parked", "completed"])
async def test_send_now_preserves_queue_and_kicks_even_without_live_turn(mock_db, async_call, state):
    first = turns.enqueue_turn("s-send-now", "user", prompt="first")
    turns.start_turn(first["id"], None, "runner")
    queued = turns.enqueue_turn("s-send-now", "user", prompt="must survive")
    if state == "parked":
        turns.park_turn(first["id"], ["background-task"])
    elif state == "completed":
        turns.settle_turn(first["id"], "completed")
    kicked, interrupted = [], []
    turn_manager.register_kick(kicked.append)
    turn_manager.register_interrupt_executor(lambda *args: interrupted.append(args))
    if async_call:
        await turn_manager.interrupt_async("s-send-now", "send_now", turn_id=first["id"])
    else:
        turn_manager.interrupt("s-send-now", "send_now", turn_id=first["id"])
    assert kicked == ["s-send-now"]
    assert bool(interrupted) == (state == "running")
    assert turns.get_turn(queued["id"])["state"] == "queued"
    assert not turns.get_turn(first["id"])["stop_background"]


def test_plain_stop_does_not_kick_parked_queue(mock_db):
    first = turns.enqueue_turn("s-stop-parked", "user", prompt="first")
    turns.start_turn(first["id"], None, "runner")
    turns.park_turn(first["id"], ["background-task"])
    kicked = []
    turn_manager.register_kick(kicked.append)
    turn_manager.interrupt("s-stop-parked", "stop")
    assert kicked == []


def test_submit_enqueues_and_kicks(mock_db):
    kicked = []
    turn_manager.register_kick(lambda sid: kicked.append(sid))

    turn_id = turn_manager.submit("s1", "user", prompt="hello", profile_id="p1")
    assert turn_id.startswith("turn-")

    row = turns.get_turn(turn_id)
    assert row["state"] == "queued"
    assert row["source"] == "user"
    assert row["prompt"] == "hello"
    assert row["profile_id"] == "p1"
    # The scheduler was kicked for this session exactly once.
    assert kicked == ["s1"]


def test_submit_without_kick_is_safe(mock_db):
    # No kick registered: the turn must still durably enqueue.
    turn_id = turn_manager.submit("s1", "queue", prompt="q")
    assert turns.get_turn(turn_id)["state"] == "queued"


def test_kick_failure_does_not_lose_turn(mock_db):
    def boom(_sid):
        raise RuntimeError("kick exploded")

    turn_manager.register_kick(boom)
    # submit must not propagate the kick failure; the turn is still queued.
    turn_id = turn_manager.submit("s1", "user", prompt="p")
    assert turns.get_turn(turn_id)["state"] == "queued"


def test_submit_passes_through_all_fields(mock_db):
    turn_id = turn_manager.submit(
        "s1",
        "automation",
        prompt="do it",
        profile_id="prof",
        project_id="proj",
        worktree_id="wt",
        overrides={"model": "opus"},
        payload={"attempts": 1},
        command_id="cmd-9",
        parent_turn_id="turn-parent",
        instance_id="inst-2",
    )
    row = turns.get_turn(turn_id)
    assert row["source"] == "automation"
    assert row["project_id"] == "proj"
    assert row["worktree_id"] == "wt"
    assert row["overrides"] == {"model": "opus"}
    assert row["payload"] == {"attempts": 1}
    assert row["command_id"] == "cmd-9"
    assert row["parent_turn_id"] == "turn-parent"
    assert row["instance_id"] == "inst-2"


async def test_async_kick_is_scheduled(mock_db):
    ran = asyncio.Event()

    async def akick(_sid):
        ran.set()

    turn_manager.register_kick(akick)
    turn_manager.submit("s1", "user", prompt="p")
    # The coroutine kick was scheduled on the running loop, not dropped.
    await asyncio.wait_for(ran.wait(), timeout=1.0)


# ---------------------------------------------------------------------------
# interrupt
# ---------------------------------------------------------------------------

def test_interrupt_running_turn(mock_db):
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)
    row = turn_manager.interrupt("s1", "stop")
    assert row is not None
    assert row["state"] == "interrupting"
    assert row["interrupt_mode"] == "stop"
    assert row["stop_background"] in (False, 0)


@pytest.mark.asyncio
async def test_host_interrupt_persists_source_without_losing_remote_teardown(mock_db):
    tid = turn_manager.submit("source-worker", "provider_agent", prompt="work",
                              payload={"api_user_id": "owner"}, no_kick=True)
    _start_live_turn(tid)
    before = turns.get_turn(tid)["payload"]
    assert before["api_user_id"] == "owner"
    assert "execution_defaults" in before
    row = await turn_manager.interrupt_async("source-worker", "stop", True,
                                             source="session_closed")
    assert row["state"] == "interrupting"
    persisted = turns.get_turn(tid)
    assert persisted["stop_background"]
    assert persisted["payload"] == {**before, "interrupt_source": "session_closed"}


def test_interrupt_send_now_with_stop_background(mock_db):
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)
    row = turn_manager.interrupt("s1", "send_now", stop_background=True)
    assert row["state"] == "interrupting"
    assert row["interrupt_mode"] == "send_now"
    assert row["stop_background"] in (True, 1)


def test_interrupt_with_no_running_turn_returns_none(mock_db):
    # A queued-but-not-started turn is not interruptible.
    turn_manager.submit("s1", "user", prompt="p")
    assert turn_manager.interrupt("s1", "stop") is None


def test_interrupt_flips_row_before_firing_executor(mock_db):
    # The executor must observe the durable interrupting row (flip happens
    # FIRST, then the executor fires).
    seen = {}

    def executor(sid, mode, turn_id=None):
        seen["row"] = turns.get_live_turn(sid)
        seen["mode"] = mode
        seen["turn_id"] = turn_id

    turn_manager.register_interrupt_executor(executor)
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)

    row = turn_manager.interrupt("s1", "send_now")
    assert row["state"] == "interrupting"
    assert seen["mode"] == "send_now"
    assert seen["row"]["state"] == "interrupting"


def test_interrupt_executor_failure_does_not_raise(mock_db):
    def boom(_sid, _mode, _turn_id=None):
        raise RuntimeError("executor exploded")

    turn_manager.register_interrupt_executor(boom)
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)
    # The durable flip still happened; the executor failure was swallowed.
    row = turn_manager.interrupt("s1", "stop")
    assert row["state"] == "interrupting"


async def test_async_interrupt_executor_is_scheduled(mock_db):
    ran = asyncio.Event()

    async def aexec(_sid, _mode, _turn_id=None):
        ran.set()

    turn_manager.register_interrupt_executor(aexec)
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)
    turn_manager.interrupt("s1", "stop")
    await asyncio.wait_for(ran.wait(), timeout=1.0)


def test_interrupt_with_no_running_turn_does_not_fire_executor(mock_db):
    # No live turn: interrupt returns None AND must NOT fire the executor
    # (there is nothing to interrupt — firing would be a spurious no-op call).
    fired = []
    turn_manager.register_interrupt_executor(
        lambda sid, mode, turn_id=None: fired.append((sid, mode))
    )

    turn_manager.submit("s1", "user", prompt="p")  # queued, never started
    assert turn_manager.interrupt("s1", "stop") is None
    assert fired == []


# ---------------------------------------------------------------------------
# remove_queued
# ---------------------------------------------------------------------------

def test_remove_queued_aborts_queued_turn(mock_db):
    # Happy path: a still-queued turn is aborted with the removal stop_reason
    # and the history row is kept.
    tid = turn_manager.submit("s1", "user", prompt="p")
    row = turn_manager.remove_queued(tid)
    assert row is not None
    assert row["state"] == "aborted"
    assert row["stop_reason"] == "removed_from_queue"
    # Row kept (still fetchable), terminally aborted.
    assert turns.get_turn(tid)["state"] == "aborted"


def test_remove_queued_leaves_running_turn_alone(mock_db):
    # Race shape: the turn is promoted to running between the user's intent and
    # this call. The atomic state='queued' guard means remove_queued is a no-op
    # (None) and the running turn is untouched — never aborted mid-flight.
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)  # queued -> running
    assert turn_manager.remove_queued(tid) is None
    assert turns.get_turn(tid)["state"] == "running"


def test_remove_queued_unknown_turn_returns_none(mock_db):
    assert turn_manager.remove_queued("turn-does-not-exist") is None


# ---------------------------------------------------------------------------
# wait helpers
# ---------------------------------------------------------------------------

async def test_wait_settled_returns_terminal_row(mock_db):
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)
    turns.settle_turn(tid, "completed")
    settled = await turn_manager.wait_settled(tid, timeout=1.0)
    assert settled is not None
    assert settled["state"] == "completed"


async def test_wait_settled_times_out_on_live_turn(mock_db):
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)
    # Never settles -> None within the short timeout.
    got = await turn_manager.wait_settled(tid, timeout=0.15, poll_interval=0.02)
    assert got is None


async def test_wait_settled_unblocks_when_turn_settles_later(mock_db):
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)

    async def settle_soon():
        await asyncio.sleep(0.05)
        turns.settle_turn(tid, "aborted", stop_reason="user_stop")

    asyncio.create_task(settle_soon())
    settled = await turn_manager.wait_settled(tid, timeout=1.0, poll_interval=0.02)
    assert settled is not None
    assert settled["state"] == "aborted"


async def test_wait_session_idle_true_when_empty(mock_db):
    assert await turn_manager.wait_session_idle("s1", timeout=1.0) is True


async def test_wait_session_idle_false_while_live(mock_db):
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)
    assert await turn_manager.wait_session_idle("s1", timeout=0.15, poll_interval=0.02) is False


async def test_wait_session_idle_false_while_queued(mock_db):
    # A queued (not yet started) turn also counts as not-idle.
    turn_manager.submit("s1", "user", prompt="p")
    assert await turn_manager.wait_session_idle("s1", timeout=0.15, poll_interval=0.02) is False


async def test_wait_session_idle_busy_in_every_non_terminal_state(mock_db):
    """EVERY non-terminal turn state keeps the session busy, and every terminal
    one releases it. The idle predicate is 'no turn row in ANY non-terminal
    state' — not a hand-maintained list of live/queued reads that a new (or
    overlooked) state could fall out of."""
    # queued
    tid = turn_manager.submit("s1", "user", prompt="p")
    assert await turn_manager.wait_session_idle("s1", timeout=0.0) is False
    # running
    _start_live_turn(tid)
    assert await turn_manager.wait_session_idle("s1", timeout=0.0) is False
    # interrupting
    turns.interrupt_turn("s1", "stop")
    assert await turn_manager.wait_session_idle("s1", timeout=0.0) is False
    # draining
    turns.escalate_to_draining(tid)
    assert await turn_manager.wait_session_idle("s1", timeout=0.0) is False
    # terminal (aborted) → idle
    turns.settle_turn(tid, "aborted", stop_reason="user_stop")
    assert await turn_manager.wait_session_idle("s1", timeout=0.0) is True

    # parked is terminal too — the park released the live slot.
    tid2 = turn_manager.submit("s1", "user", prompt="p2")
    turns.start_turn(tid2, run_id="run-2")
    turns.park_turn(tid2, ["task-1"])
    assert await turn_manager.wait_session_idle("s1", timeout=0.0) is True


async def test_wait_session_idle_not_fooled_by_queued_to_running_handoff(mock_db):
    """Regression for the 2026-08-28 automation race: the idle predicate used
    to be TWO reads — get_live_turn (none: still queued), then
    get_queued_turns (none: just started) — so a queued→running flip landing
    between them made a running session read as idle and an automation stamped
    an empty "completed" run 6 s before its turn actually finished.

    Reproduce that exact interleave: make the split live-read flip the row to
    running as its side effect (the scheduler's ``start_turn`` claim landing
    right after it). A predicate still built on the split readers sees neither
    set populated and returns idle=True; the atomic single-statement predicate
    never opens that window, so the wait stays blocked."""
    tid = turn_manager.submit("s1", "user", prompt="p")

    real_get_live = turns.get_live_turn

    def get_live_then_scheduler_claims(session_id):
        row = real_get_live(session_id)
        _start_live_turn(tid)  # the claim lands "between reads"
        return row

    with patch.object(turns, "get_live_turn", get_live_then_scheduler_claims):
        idle = await turn_manager.wait_session_idle(
            "s1", timeout=0.15, poll_interval=0.02
        )
    assert idle is False, (
        "wait_session_idle declared a session idle across the queued→running "
        "handoff — the idle check is not atomic"
    )


async def test_wait_session_idle_true_after_settle(mock_db):
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)

    async def settle_soon():
        await asyncio.sleep(0.05)
        turns.settle_turn(tid, "completed")

    asyncio.create_task(settle_soon())
    assert await turn_manager.wait_session_idle("s1", timeout=1.0, poll_interval=0.02) is True


# ---------------------------------------------------------------------------
# async command twins — submit_async / interrupt_async (perf review W6)
#
# The WS chat handlers call these on every send and every stop. The DB work must
# leave the event loop; the kick / interrupt executor must NOT (both need
# ``asyncio.get_running_loop()`` and silently drop their work without one).
# ---------------------------------------------------------------------------

async def test_submit_async_writes_off_the_loop_and_kicks_on_it(mock_db):
    loop_thread = threading.get_ident()
    seen = {}
    real_blocking = turn_manager._submit_blocking

    def spy(*args, **kwargs):
        seen["db_thread"] = threading.get_ident()
        return real_blocking(*args, **kwargs)

    kicked = []
    turn_manager.register_kick(
        lambda sid: kicked.append((sid, threading.get_ident()))
    )

    with patch.object(turn_manager, "_submit_blocking", spy):
        turn_id = await turn_manager.submit_async("s1", "user", prompt="hello")

    # Same durable outcome as the sync form...
    row = turns.get_turn(turn_id)
    assert row["state"] == "queued"
    assert (row["source"], row["prompt"]) == ("user", "hello")
    # ...with the DB writes off the loop thread...
    assert seen["db_thread"] != loop_thread
    # ...and the kick back ON it (a kick from a worker thread is dropped).
    assert kicked == [("s1", loop_thread)]


async def test_submit_async_schedules_an_async_kick(mock_db):
    """The registered kick (``turn_scheduler.kick``) needs a running loop to
    schedule the start. Firing it from the worker thread would drop the nudge
    and the turn would sit queued until the next reap sweep."""
    ran = asyncio.Event()

    async def kick(_sid):
        ran.set()

    turn_manager.register_kick(kick)
    turn_id = await turn_manager.submit_async("s1", "user", prompt="p")
    await asyncio.wait_for(ran.wait(), timeout=1.0)
    assert turns.get_turn(turn_id)["state"] == "queued"


async def test_submit_async_no_kick_still_enqueues(mock_db):
    kicked = []
    turn_manager.register_kick(lambda sid: kicked.append(sid))
    turn_id = await turn_manager.submit_async(
        "s1", "user", prompt="p", no_kick=True
    )
    assert turns.get_turn(turn_id)["state"] == "queued"
    assert kicked == []


async def test_submit_async_passes_through_all_fields(mock_db):
    turn_id = await turn_manager.submit_async(
        "s1",
        "automation",
        prompt="do it",
        profile_id="prof",
        project_id="proj",
        worktree_id="wt",
        overrides={"model": "opus"},
        payload={"k": "v"},
        command_id="cmd-1",
        parent_turn_id="turn-parent",
        instance_id="inst-1",
    )
    row = turns.get_turn(turn_id)
    assert row["profile_id"] == "prof"
    assert row["project_id"] == "proj"
    assert row["worktree_id"] == "wt"
    assert row["overrides"] == {"model": "opus"}
    assert row["payload"] == {"k": "v"}
    assert row["command_id"] == "cmd-1"
    assert row["parent_turn_id"] == "turn-parent"
    assert row["instance_id"] == "inst-1"


async def test_interrupt_async_flips_off_the_loop_and_fires_executor(mock_db):
    loop_thread = threading.get_ident()
    seen = {}
    real_flip = turns.interrupt_turn

    def spy(*args, **kwargs):
        seen["db_thread"] = threading.get_ident()
        return real_flip(*args, **kwargs)

    fired = []
    turn_manager.register_interrupt_executor(
        lambda sid, mode, turn_id=None: fired.append((sid, mode, turn_id))
    )

    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)

    with patch.object(turns, "interrupt_turn", spy):
        row = await turn_manager.interrupt_async(
            "s1", "stop", stop_background=True, turn_id=tid
        )

    assert row is not None and row["state"] == "interrupting"
    assert turns.get_turn(tid)["state"] == "interrupting"
    assert seen["db_thread"] != loop_thread
    assert fired == [("s1", "stop", tid)]


async def test_interrupt_async_schedules_an_async_executor(mock_db):
    """The real executor is a coroutine function (the runtime's SDK interrupt);
    it must be scheduled on the loop, not dropped on a worker thread."""
    ran = asyncio.Event()

    async def executor(_sid, _mode, _turn_id=None):
        ran.set()

    turn_manager.register_interrupt_executor(executor)
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)

    await turn_manager.interrupt_async("s1", "stop")
    await asyncio.wait_for(ran.wait(), timeout=1.0)


async def test_interrupt_async_no_live_turn_returns_none_without_firing(mock_db):
    fired = []
    turn_manager.register_interrupt_executor(
        lambda sid, mode, turn_id=None: fired.append((sid, mode))
    )
    turn_manager.submit("s1", "user", prompt="p")  # queued, never started
    assert await turn_manager.interrupt_async("s1", "stop") is None
    assert fired == []


# ---------------------------------------------------------------------------
# wait helpers — NOTIFY wake + backing-off poll (perf review F2)
# ---------------------------------------------------------------------------

async def test_wait_settled_reads_off_the_event_loop(mock_db):
    loop_thread = threading.get_ident()
    threads = set()
    real_get = turns.get_turn

    def spy(turn_id):
        threads.add(threading.get_ident())
        return real_get(turn_id)

    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)
    turns.settle_turn(tid, "completed")

    with patch.object(turns, "get_turn", spy):
        assert (await turn_manager.wait_settled(tid, timeout=1.0))["state"] == "completed"

    assert threads and loop_thread not in threads


async def test_wait_session_idle_reads_off_the_event_loop(mock_db):
    loop_thread = threading.get_ident()
    threads = set()
    real_busy = turns.session_has_unsettled_turns

    def spy(session_id):
        threads.add(threading.get_ident())
        return real_busy(session_id)

    with patch.object(turns, "session_has_unsettled_turns", spy):
        assert await turn_manager.wait_session_idle("s1", timeout=1.0) is True

    assert threads and loop_thread not in threads


async def test_wait_settled_backs_off_instead_of_hot_polling(mock_db):
    """The old 50 ms fixed cadence cost ~20 queries per second of waiting; the
    backoff turns a 1 s wait into a handful."""
    calls = []
    live_row = {"id": "turn-x", "session_id": "s1", "state": "running"}

    def counting_get_turn(turn_id):
        calls.append(turn_id)
        return live_row

    with patch.object(turns, "get_turn", counting_get_turn):
        assert await turn_manager.wait_settled(
            "turn-x", timeout=1.0, poll_interval=0.05
        ) is None

    # 0.05 -> 0.1 -> 0.2 -> 0.4 -> remainder: 6 reads, where the old fixed 50 ms
    # poll would have issued ~20.
    assert len(calls) <= 8, f"expected a handful of reads, got {len(calls)}"
    assert len(calls) < 20


async def test_wait_session_idle_backs_off_instead_of_hot_polling(mock_db):
    busy_calls = []

    def counting_busy(session_id):
        busy_calls.append(session_id)
        return True  # forever busy — the wait must time out on backoff alone

    with patch.object(turns, "session_has_unsettled_turns", counting_busy):
        assert await turn_manager.wait_session_idle(
            "s1", timeout=1.0, poll_interval=0.05
        ) is False

    assert len(busy_calls) <= 8, f"expected a handful of reads, got {len(busy_calls)}"


async def test_wait_settled_backoff_is_capped(mock_db):
    """Backoff must stop growing at MAX_POLL_INTERVAL so a long wait still
    notices a missed NOTIFY within a couple of seconds."""
    assert turn_manager._next_interval(0.05, 0.05) == 0.1
    assert turn_manager._next_interval(1.5, 0.05) == turn_manager.MAX_POLL_INTERVAL
    assert turn_manager._next_interval(
        turn_manager.MAX_POLL_INTERVAL, 0.05
    ) == turn_manager.MAX_POLL_INTERVAL
    # A caller asking for a SLOWER cadence than the ceiling keeps its own.
    assert turn_manager._next_interval(5.0, 5.0) == 5.0


async def test_wait_settled_wakes_on_the_gateway_tick(mock_db):
    """The NOTIFY path, not the poll, is what returns in the common case: with a
    5 s poll interval only a gateway tick can settle this wait in time."""
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)

    async def settle_and_notify():
        await asyncio.sleep(0.05)
        turns.settle_turn(tid, "completed")
        turn_manager._on_gateway_tick("s1")  # what the LISTEN thread does

    asyncio.create_task(settle_and_notify())
    started = time.monotonic()
    row = await turn_manager.wait_settled(tid, timeout=3.0, poll_interval=5.0)
    elapsed = time.monotonic() - started

    assert row is not None and row["state"] == "completed"
    assert elapsed < 1.0, f"woke on the poll ({elapsed:.2f}s), not on the tick"


async def test_wait_session_idle_wakes_on_the_gateway_tick(mock_db):
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)

    async def settle_and_notify():
        await asyncio.sleep(0.05)
        turns.settle_turn(tid, "completed")
        turn_manager._on_gateway_tick("s1")

    asyncio.create_task(settle_and_notify())
    started = time.monotonic()
    idle = await turn_manager.wait_session_idle(
        "s1", timeout=3.0, poll_interval=5.0
    )
    elapsed = time.monotonic() - started

    assert idle is True
    assert elapsed < 1.0, f"woke on the poll ({elapsed:.2f}s), not on the tick"


async def test_gateway_tick_for_another_session_does_not_wake(mock_db):
    """A tick is per-session: a busy neighbour must not spin this waiter."""
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)

    calls = []
    real_get = turns.get_turn

    def counting(turn_id):
        calls.append(turn_id)
        return real_get(turn_id)

    async def tick_the_neighbour():
        for _ in range(5):
            await asyncio.sleep(0.01)
            turn_manager._on_gateway_tick("other-session")

    asyncio.create_task(tick_the_neighbour())
    with patch.object(turns, "get_turn", counting):
        assert await turn_manager.wait_settled(
            tid, timeout=0.3, poll_interval=0.2
        ) is None
    # Poll-only cadence: the initial read plus the 0.2 s re-check, nothing more.
    assert len(calls) <= 3


async def test_wait_helpers_register_with_the_real_gateway():
    """The wake path is wired to the event gateway's global-listener seam."""
    listeners = []

    class _FakeGateway:
        def add_global_listener(self, fn):
            listeners.append(fn)

    turn_manager._gateway_listener_registered = False
    try:
        with patch("app.core.event_gateway.get_gateway", lambda: _FakeGateway()):
            _REAL_ENSURE_GATEWAY_LISTENER()
            _REAL_ENSURE_GATEWAY_LISTENER()  # idempotent
    finally:
        registered = turn_manager._gateway_listener_registered
        turn_manager._gateway_listener_registered = False

    assert registered is True
    assert listeners == [turn_manager._on_gateway_tick]


async def test_ensure_gateway_listener_survives_an_unavailable_gateway():
    """No gateway (PG unreachable) must degrade to poll-only, never raise."""
    turn_manager._gateway_listener_registered = False
    try:
        def boom():
            raise RuntimeError("no gateway")

        with patch("app.core.event_gateway.get_gateway", boom):
            _REAL_ENSURE_GATEWAY_LISTENER()  # must not raise
        assert turn_manager._gateway_listener_registered is False
    finally:
        turn_manager._gateway_listener_registered = False


async def test_wait_settled_timeout_zero_checks_once(mock_db):
    """Timeout semantics preserved: the state is read once BEFORE any sleep, so
    an already-terminal turn returns even with no time budget."""
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)
    turns.settle_turn(tid, "completed")
    assert (await turn_manager.wait_settled(tid, timeout=0.0))["state"] == "completed"

    tid2 = turn_manager.submit("s1", "user", prompt="p2")
    turns.start_turn(tid2, run_id="run-2")
    calls = []
    real_get = turns.get_turn

    def counting(turn_id):
        calls.append(turn_id)
        return real_get(turn_id)

    with patch.object(turns, "get_turn", counting):
        assert await turn_manager.wait_settled(tid2, timeout=0.0) is None
    assert len(calls) == 1


async def test_wait_settled_unknown_turn_times_out_to_none(mock_db):
    """An id that never appears keeps being retried until the deadline (it may
    be a turn another runner has not committed yet), then returns None."""
    assert await turn_manager.wait_settled(
        "turn-never", timeout=0.15, poll_interval=0.02
    ) is None


async def test_wait_session_idle_timeout_zero_checks_once(mock_db):
    assert await turn_manager.wait_session_idle("s1", timeout=0.0) is True
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)
    assert await turn_manager.wait_session_idle("s1", timeout=0.0) is False


async def test_wait_settled_cancellation_unregisters_the_waiter(mock_db):
    """A cancelled wait must not leave a registration behind (the autouse
    fixture asserts the registry is empty, but be explicit here)."""
    tid = turn_manager.submit("s1", "user", prompt="p")
    _start_live_turn(tid)

    task = asyncio.create_task(
        turn_manager.wait_settled(tid, timeout=30.0, poll_interval=5.0)
    )
    # Let it reach the wait (registered on "s1").
    for _ in range(50):
        await asyncio.sleep(0.01)
        if turn_manager._waiters.get("s1"):
            break
    assert turn_manager._waiters.get("s1"), "waiter never registered"

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert turn_manager._waiters == {}
