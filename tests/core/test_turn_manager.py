"""Unit tests for app/core/turn_manager.py — the command layer over turns."""

import asyncio

import pytest

from app.core import turn_manager
from app.db import turns


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
def reset_kick():
    """The kick + interrupt-executor seams are module globals — clear them
    around every test."""
    turn_manager.register_kick(None)
    turn_manager.register_interrupt_executor(None)
    yield
    turn_manager.register_kick(None)
    turn_manager.register_interrupt_executor(None)


# ---------------------------------------------------------------------------
# submit + kick
# ---------------------------------------------------------------------------

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
    turns.start_turn(tid, run_id="run-1")
    row = turn_manager.interrupt("s1", "stop")
    assert row is not None
    assert row["state"] == "interrupting"
    assert row["interrupt_mode"] == "stop"
    assert row["stop_background"] in (False, 0)


def test_interrupt_send_now_with_stop_background(mock_db):
    tid = turn_manager.submit("s1", "user", prompt="p")
    turns.start_turn(tid, run_id="run-1")
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

    def executor(sid, mode):
        seen["row"] = turns.get_live_turn(sid)
        seen["mode"] = mode

    turn_manager.register_interrupt_executor(executor)
    tid = turn_manager.submit("s1", "user", prompt="p")
    turns.start_turn(tid, run_id="run-1")

    row = turn_manager.interrupt("s1", "send_now")
    assert row["state"] == "interrupting"
    assert seen["mode"] == "send_now"
    assert seen["row"]["state"] == "interrupting"


def test_interrupt_executor_failure_does_not_raise(mock_db):
    def boom(_sid, _mode):
        raise RuntimeError("executor exploded")

    turn_manager.register_interrupt_executor(boom)
    tid = turn_manager.submit("s1", "user", prompt="p")
    turns.start_turn(tid, run_id="run-1")
    # The durable flip still happened; the executor failure was swallowed.
    row = turn_manager.interrupt("s1", "stop")
    assert row["state"] == "interrupting"


async def test_async_interrupt_executor_is_scheduled(mock_db):
    ran = asyncio.Event()

    async def aexec(_sid, _mode):
        ran.set()

    turn_manager.register_interrupt_executor(aexec)
    tid = turn_manager.submit("s1", "user", prompt="p")
    turns.start_turn(tid, run_id="run-1")
    turn_manager.interrupt("s1", "stop")
    await asyncio.wait_for(ran.wait(), timeout=1.0)


def test_interrupt_with_no_running_turn_does_not_fire_executor(mock_db):
    # No live turn: interrupt returns None AND must NOT fire the executor
    # (there is nothing to interrupt — firing would be a spurious no-op call).
    fired = []
    turn_manager.register_interrupt_executor(lambda sid, mode: fired.append((sid, mode)))

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
    turns.start_turn(tid, run_id="run-1")  # queued -> running
    assert turn_manager.remove_queued(tid) is None
    assert turns.get_turn(tid)["state"] == "running"


def test_remove_queued_unknown_turn_returns_none(mock_db):
    assert turn_manager.remove_queued("turn-does-not-exist") is None


# ---------------------------------------------------------------------------
# wait helpers
# ---------------------------------------------------------------------------

async def test_wait_settled_returns_terminal_row(mock_db):
    tid = turn_manager.submit("s1", "user", prompt="p")
    turns.start_turn(tid, run_id="run-1")
    turns.settle_turn(tid, "completed")
    settled = await turn_manager.wait_settled(tid, timeout=1.0)
    assert settled is not None
    assert settled["state"] == "completed"


async def test_wait_settled_times_out_on_live_turn(mock_db):
    tid = turn_manager.submit("s1", "user", prompt="p")
    turns.start_turn(tid, run_id="run-1")
    # Never settles -> None within the short timeout.
    got = await turn_manager.wait_settled(tid, timeout=0.15, poll_interval=0.02)
    assert got is None


async def test_wait_settled_unblocks_when_turn_settles_later(mock_db):
    tid = turn_manager.submit("s1", "user", prompt="p")
    turns.start_turn(tid, run_id="run-1")

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
    turns.start_turn(tid, run_id="run-1")
    assert await turn_manager.wait_session_idle("s1", timeout=0.15, poll_interval=0.02) is False


async def test_wait_session_idle_false_while_queued(mock_db):
    # A queued (not yet started) turn also counts as not-idle.
    turn_manager.submit("s1", "user", prompt="p")
    assert await turn_manager.wait_session_idle("s1", timeout=0.15, poll_interval=0.02) is False


async def test_wait_session_idle_true_after_settle(mock_db):
    tid = turn_manager.submit("s1", "user", prompt="p")
    turns.start_turn(tid, run_id="run-1")

    async def settle_soon():
        await asyncio.sleep(0.05)
        turns.settle_turn(tid, "completed")

    asyncio.create_task(settle_soon())
    assert await turn_manager.wait_session_idle("s1", timeout=1.0, poll_interval=0.02) is True
