"""A parked parent is woken by the app, promptly, on every leg.

The parent parks when its turn ends with background work still running (an
app ``Task`` worker, a Monitor shell). On the delegated (Claude CLI) leg the
CLI's own ``Task``/``Agent`` tools are banned and its background tasks are
disabled, so no CLI-authored ``<task-notification>`` turn is ever coming: the
app's ``source=unpark`` wake is the only way the parent learns the work is
done. Nothing on the scheduler side depends on the leg, so these run the real
``turn_scheduler`` + ``task_registry`` + ``wakeup_service`` against the SQLite
test DB with only ``session_runtime.execute`` (the engine) stubbed, and time
the gap between the completion and the wake turn starting.
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from app.core import provider_agent, session_runtime, turn_manager, turn_scheduler, wakeup_service
from app.core.task_registry import task_registry
from app.db import turns as turns_db

#: The scheduler batches near-simultaneous completions for this long before it
#: kicks (shortened from the real 2 s). A wake is "prompt" when it starts well
#: inside a few windows — nowhere near the 10 minute held-peer promotion.
_WINDOW = 0.1
_PROMPT_WAKE_SECONDS = 2.0


@pytest.fixture(autouse=True)
def _scheduler_state(monkeypatch):
    from app.core import provider_agent_supervisor

    monkeypatch.setattr(turn_scheduler, "TERMINAL_BATCH_WINDOW_SECONDS", _WINDOW)
    provider_agent_supervisor.reset()
    session_runtime.register_scheduler(None)
    turn_manager.register_kick(turn_scheduler.kick)
    turn_scheduler._kick_tasks.clear()
    getattr(turn_scheduler, "_delivered_held", {}).clear()
    yield
    for task in list(wakeup_service._timers.values()):
        task.cancel()
    wakeup_service._timers.clear()
    turn_manager.register_kick(None)
    turn_scheduler._kick_tasks.clear()
    getattr(turn_scheduler, "_delivered_held", {}).clear()


class _Engine:
    """Stands in for ``session_runtime.execute``: records which turn started and
    when, and settles it the way a real run would."""

    def __init__(self):
        self.started = []

    async def __call__(self, turn, *args, **kwargs):
        self.started.append((time.monotonic(), turn))
        turns_db.start_turn(turn["id"], None, "runner")
        turns_db.settle_turn(turn["id"], "completed")
        return "completed"


async def _wait_for_start(engine, *, timeout):
    deadline = time.monotonic() + timeout
    while not engine.started and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    return engine.started[0] if engine.started else None


def _seed_session(session_id):
    from app.db import database

    database.create_session(session_id=session_id, profile_id=None, title="t")


def _park_on_worker(session_id, worker_id, launch_key):
    """A delegated parent that dispatched ``worker_id`` in the background and
    ended its turn: registered exactly as ``provider_agent.dispatch_worker``
    registers it (``kind="agent"`` + a recorded background launch)."""
    _seed_session(session_id)
    turn = turns_db.enqueue_turn(
        session_id, "user", prompt="Delegate the audit", profile_id="profile",
    )
    turns_db.start_turn(turn["id"], None, "runner")
    turns_db.park_turn(turn["id"], [worker_id])
    task_registry.start_task(session_id, worker_id, "agent", "audit", tool_use_id=launch_key)
    task_registry.mark_background_launch(session_id, launch_key)
    return turn


def _read_no_output_file():
    return patch("app.core.query_engine._read_task_output_file", return_value="")


async def test_a_finished_task_worker_wakes_its_parked_parent_promptly(mock_db):
    """The app Task tool's worker finishes: the parent's unpark turn starts
    within the batch window, carrying the worker's report."""
    parked = _park_on_worker("dp1", "worker-1", "tu-1")
    engine = _Engine()

    with patch.object(session_runtime, "execute", new=engine), _read_no_output_file():
        finished_at = time.monotonic()
        task_registry.complete_task("dp1", "worker-1", "completed", summary="audit done")
        await provider_agent._notify_caller_terminal(
            "dp1", "worker-1", "tu-1", "completed", "audit done",
        )
        started = await _wait_for_start(engine, timeout=_PROMPT_WAKE_SECONDS)

    assert started is not None, "the parked parent was never woken"
    started_at, wake = started
    assert started_at - finished_at < _PROMPT_WAKE_SECONDS
    assert wake["source"] == "unpark"
    assert wake["parent_turn_id"] == parked["id"]
    assert "<task-id>worker-1</task-id>" in wake["prompt"]
    assert "audit done" in wake["prompt"]


async def test_a_completion_frame_without_the_worker_marker_still_wakes(mock_db):
    """Nothing but the app delivers an at-rest completion now, so a completed
    ``kind="agent"`` frame is never left for the CLI to deliver, marker or not.

    This was the stranding shape: the old CLI-owned suppression swallowed an
    unmarked completed agent frame whenever the parent was parked, latched the
    task as woken, and waited for a CLI follow-up turn that no leg produces."""
    parked = _park_on_worker("dp2", "agent-2", "tu-2")
    engine = _Engine()
    frame = {"task_id": "agent-2", "status": "completed", "tool_use_id": "tu-2",
             "summary": "agent finished"}

    with patch.object(session_runtime, "execute", new=engine), _read_no_output_file():
        finished_at = time.monotonic()
        signal = await turn_scheduler.on_task_terminal("dp2", frame)
        started = await _wait_for_start(engine, timeout=_PROMPT_WAKE_SECONDS)

    assert signal is None
    assert started is not None, "the parked parent was never woken"
    started_at, wake = started
    assert started_at - finished_at < _PROMPT_WAKE_SECONDS
    assert wake["source"] == "unpark"
    assert wake["parent_turn_id"] == parked["id"]
    assert "<task-id>agent-2</task-id>" in wake["prompt"]


async def test_a_held_peer_message_rides_the_workers_wake(mock_db):
    """The worker's progress note is held while the parent is parked on it,
    and is delivered with the completion wake, not at the promotion deadline."""
    _park_on_worker("dp3", "worker-3", "tu-3")
    assert await turn_scheduler.on_agent_message(
        "dp3", "worker-3", "halfway through the audit",
    ) == turn_scheduler.HELD

    enqueued = []
    real_enqueue = turn_scheduler._enqueue_context_wake

    async def _spy(*args, **kwargs):
        turn = await real_enqueue(*args, **kwargs)
        enqueued.append(turn)
        return turn

    with patch.object(turn_scheduler, "_enqueue_context_wake", new=_spy), \
            patch.object(turn_scheduler, "kick", new=lambda sid: None), \
            _read_no_output_file():
        await provider_agent._notify_caller_terminal(
            "dp3", "worker-3", "tu-3", "completed", "audit done",
        )

    (wake,) = enqueued
    assert wake["source"] == "unpark"
    assert "<task-id>worker-3</task-id>" in wake["prompt"]
    assert "halfway through the audit" in wake["prompt"]
    assert "halfway through the audit" in wake["payload"]["held_peer_messages"]
    # Taken off the held row, so the promotion deadline has nothing to deliver.
    assert _held_rows("dp3") == []


def _held_rows(session_id):
    from app.db import wakeups as wakeups_db

    return wakeups_db.get_session_wakeups(session_id, wakeup_service.HELD_PEER_SOURCE)


async def test_a_scheduled_wakeup_wakes_a_parked_parent(mock_db):
    """ScheduleWakeup's timer fires for a parent parked on other work: the
    ``wakeup`` turn starts at once — a park does not hold it back."""
    _park_on_worker("dp4", "worker-4", "tu-4")
    engine = _Engine()
    row = {"id": "wk-1", "session_id": "dp4", "source": "wakeup",
           "prompt": "check the deploy", "profile_id": "profile"}

    with patch.object(session_runtime, "execute", new=engine), \
            patch("app.db.wakeups.claim_wakeup", return_value=True):
        fired_at = time.monotonic()
        await wakeup_service._fire(row)
        started = await _wait_for_start(engine, timeout=_PROMPT_WAKE_SECONDS)

    assert started is not None, "the scheduled wakeup never ran"
    started_at, wake = started
    assert started_at - fired_at < _PROMPT_WAKE_SECONDS
    assert wake["source"] == "wakeup"
    assert wake["prompt"] == "check the deploy"


async def test_a_connector_message_wakes_a_parked_parent(mock_db):
    """An inbound connector message for a parked parent is enqueued and started
    the same way (``connectors.manager`` submits a ``connector`` turn)."""
    _park_on_worker("dp5", "worker-5", "tu-5")
    engine = _Engine()

    with patch.object(session_runtime, "execute", new=engine):
        sent_at = time.monotonic()
        await turn_manager.submit_async(
            "dp5", "connector", prompt="status?", profile_id="profile",
        )
        started = await _wait_for_start(engine, timeout=_PROMPT_WAKE_SECONDS)

    assert started is not None, "the connector message never ran"
    started_at, turn = started
    assert started_at - sent_at < _PROMPT_WAKE_SECONDS
    assert turn["source"] == "connector"
