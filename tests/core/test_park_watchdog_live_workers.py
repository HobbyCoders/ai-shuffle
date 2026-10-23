"""Park watchdog vs. Task workers that are still working (#417).

A coordinator parked on background Task workers was force-settled after
``park_watchdog_force_seconds`` even though every worker was alive and
progressing: all three task rows went ``killed``, the workers' real reports
were never delivered and SendMessage refused them as not resumable.

The force stage now leaves a worker alone while its session shows progress
(a recent transcript event, or a park of its own on live work), still reaps
a worker that went quiet, and lets a forced worker's late report through.
These run the real watchdog, ``task_registry``, ``turn_scheduler`` and wake
path against the SQLite test DB; only the engine that would run the wake turn
(``session_runtime.execute``) is stubbed.
"""

import asyncio
import time
from unittest.mock import patch

import pytest

from app.core import provider_agent, session_runtime, turn_manager, turn_scheduler, wakeup_service
from app.core import startup_reconcile as sr
from app.core.task_registry import task_registry
from app.db import database, session_events
from app.db import turns as turns_db
from app.db.background_tasks import get_background_task, upsert_background_task
from app.db.base import get_db

_OLD_TS = "2000-01-01 00:00:00"
_WINDOW = 0.1
_WAKE_TIMEOUT = 2.0


@pytest.fixture(autouse=True)
def _state(monkeypatch):
    from app.core import provider_agent_supervisor

    monkeypatch.setattr(turn_scheduler, "TERMINAL_BATCH_WINDOW_SECONDS", _WINDOW)
    provider_agent_supervisor.reset()
    task_registry.reset_all()
    sr._park_warned.clear()
    sr._park_deferred.clear()
    session_runtime.register_scheduler(None)
    turn_manager.register_kick(turn_scheduler.kick)
    turn_scheduler._kick_tasks.clear()
    yield
    for task in list(wakeup_service._timers.values()):
        task.cancel()
    wakeup_service._timers.clear()
    turn_manager.register_kick(None)
    turn_scheduler._kick_tasks.clear()
    task_registry.reset_all()
    sr._park_warned.clear()
    sr._park_deferred.clear()


class _Engine:
    """Stands in for ``session_runtime.execute``; records the turns it ran."""

    def __init__(self):
        self.started = []

    async def __call__(self, turn, *args, **kwargs):
        self.started.append(turn)
        turns_db.start_turn(turn["id"], None, "runner")
        turns_db.settle_turn(turn["id"], "completed")
        return "completed"


async def _wait_for_starts(engine, count, *, timeout=_WAKE_TIMEOUT):
    deadline = time.monotonic() + timeout
    while len(engine.started) < count and time.monotonic() < deadline:
        await asyncio.sleep(0.01)
    # Give a wrongly duplicated wake the same chance to show up.
    await asyncio.sleep(_WINDOW * 3)
    return list(engine.started)


def _backdate_turn(turn_id):
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE turns SET created_at=%s, ended_at=%s WHERE id=%s",
            (_OLD_TS, _OLD_TS, turn_id),
        )


def _park(session_id, task_ids):
    """A session whose turn parked on ``task_ids`` long ago."""
    from app.core.config import settings

    turn = turns_db.enqueue_turn(session_id, "user", prompt="Delegate", profile_id="profile")
    # The watchdog only reads this install's turns.
    turns_db.start_turn(turn["id"], None, settings.effective_instance_id)
    turns_db.park_turn(turn["id"], list(task_ids))
    _backdate_turn(turn["id"])
    return turn


def _dispatch_worker(parent_id, worker_id, launch_key):
    """Register a background Task worker the way ``dispatch_worker`` does:
    a ``kind="agent"`` row keyed by the worker's session id, a recorded
    background launch, the worker's own session, and the durable mirror."""
    task_registry.start_task(parent_id, worker_id, "agent", "fix", tool_use_id=launch_key)
    task_registry.mark_background_launch(parent_id, launch_key)
    database.create_session(
        session_id=worker_id, profile_id=None, title="worker", parent_session_id=parent_id,
    )
    upsert_background_task({
        "task_id": worker_id, "session_id": parent_id, "kind": "agent",
        "tool_use_id": launch_key, "title": "fix", "status": "running",
        "started_at": _OLD_TS, "updated_at": _OLD_TS,
    })


def _worker_writes(worker_id, *, old=False):
    """The worker's transcript gets an event (now, or long ago)."""
    session_events.append_events(worker_id, [
        {"type": "message.assistant", "payload": {"text": "pushing the fix"}},
    ])
    if old:
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE session_events SET created_at=%s WHERE session_id=%s",
                (_OLD_TS, worker_id),
            )


def _coordinator(session_id, workers):
    database.create_session(session_id=session_id, profile_id=None, title="coordinator")
    for worker_id, launch_key in workers:
        _dispatch_worker(session_id, worker_id, launch_key)
    return _park(session_id, [w for w, _ in workers])


def _turn_state(turn_id):
    return (turns_db.get_turn(turn_id) or {}).get("state")


def _read_no_output_file():
    return patch("app.core.query_engine._read_task_output_file", return_value="")


async def _report(parent_id, worker_id, launch_key, summary):
    """The worker's real completion, as ``dispatch_worker``'s finally sends it."""
    task_registry.complete_task(parent_id, worker_id, "completed", summary=summary)
    await provider_agent._notify_caller_terminal(
        parent_id, worker_id, launch_key, "completed", summary,
    )


# =============================================================================
# Live, progressing workers are not killed
# =============================================================================

async def test_progressing_worker_is_not_forced_and_its_report_wakes_once(mock_db):
    parked = _coordinator("lw-coord", [("lw-worker", "tu-lw")])
    _worker_writes("lw-worker")
    engine = _Engine()

    with patch.object(session_runtime, "execute", new=engine), _read_no_output_file():
        forced = await sr.sweep_stalled_parks(warn_seconds=60, force_seconds=120)
        # Past the ceiling on the next tick too: still waiting, not forced.
        forced += await sr.sweep_stalled_parks(warn_seconds=60, force_seconds=120)

        assert forced == 0
        # The park IS past the ceiling: only the worker's progress spared it.
        assert [r["session_id"] for r in turns_db.list_stalled_parked_turns(120)] == ["lw-coord"]
        assert get_background_task("lw-worker", "lw-coord")["status"] == "running"
        assert task_registry.get_task("lw-coord", "lw-worker")["status"] == "running"
        assert _turn_state(parked["id"]) == "parked"
        assert engine.started == []

        # The worker finishes; a duplicate delivery must not wake twice.
        await _report("lw-coord", "lw-worker", "tu-lw", "PR #9 green")
        await _report("lw-coord", "lw-worker", "tu-lw", "PR #9 green")
        started = await _wait_for_starts(engine, 1)

    assert len(started) == 1, "the worker's report must wake the parent exactly once"
    wake = started[0]
    assert wake["source"] == "unpark"
    assert wake["parent_turn_id"] == parked["id"]
    assert "<task-id>lw-worker</task-id>" in wake["prompt"]
    assert "PR #9 green" in wake["prompt"]


async def test_worker_parked_on_its_own_live_work_is_not_forced(mock_db):
    """A worker watching CI in a Monitor writes nothing while it waits; its own
    park on running work is the progress evidence (and is bounded by the same
    watchdog on the worker's session)."""
    _coordinator("lw-coord2", [("lw-ci-worker", "tu-ci")])
    _worker_writes("lw-ci-worker", old=True)
    _park("lw-ci-worker", ["monitor-ci"])
    upsert_background_task({
        "task_id": "monitor-ci", "session_id": "lw-ci-worker", "kind": "shell",
        "title": "gh pr checks --watch", "status": "running",
        "started_at": _OLD_TS, "updated_at": _OLD_TS,
    })

    with patch("app.core.turn_scheduler.on_task_terminal") as wake:
        assert not await sr._force_stalled_park(
            "lw-coord2", "t", 999, progress_window=60,
        )

    wake.assert_not_called()
    assert get_background_task("lw-ci-worker", "lw-coord2")["status"] == "running"


# =============================================================================
# Dead or stalled workers are still settled
# =============================================================================

@pytest.mark.parametrize("has_session", [True, False])
async def test_quiet_or_missing_worker_is_still_forced(mock_db, has_session):
    parked = _coordinator("dw-coord", [("dw-worker", "tu-dw")])
    if has_session:
        _worker_writes("dw-worker", old=True)  # last wrote long ago, then nothing
    else:
        with get_db() as conn:
            conn.cursor().execute("DELETE FROM sessions WHERE id=%s", ("dw-worker",))
    engine = _Engine()

    with patch.object(session_runtime, "execute", new=engine), _read_no_output_file():
        forced = await sr.sweep_stalled_parks(warn_seconds=60, force_seconds=120)
        started = await _wait_for_starts(engine, 1)

    assert forced == 1
    assert get_background_task("dw-worker", "dw-coord")["status"] == "killed"
    assert task_registry.get_task("dw-coord", "dw-worker")["status"] == "killed"
    assert _turn_state(parked["id"]) == "completed"
    assert len(started) == 1
    assert "<task-id>dw-worker</task-id>" in started[0]["prompt"]
    assert "no progress" in started[0]["prompt"]


async def test_only_the_stalled_worker_of_a_mixed_park_is_settled(mock_db):
    _coordinator("mx-coord", [("mx-live", "tu-live"), ("mx-dead", "tu-dead")])
    _worker_writes("mx-live")
    _worker_writes("mx-dead", old=True)

    with patch("app.core.turn_recorder.record_task_settle_frame"), \
            patch("app.core.turn_scheduler.on_task_terminal") as wake:
        forced = await sr.sweep_stalled_parks(warn_seconds=60, force_seconds=120)

    assert forced == 1
    assert get_background_task("mx-dead", "mx-coord")["status"] == "killed"
    assert get_background_task("mx-live", "mx-coord")["status"] == "running"
    assert task_registry.get_task("mx-coord", "mx-live")["status"] == "running"
    assert [c.args[1]["task_id"] for c in wake.call_args_list] == ["mx-dead"]


async def test_shell_tasks_keep_the_original_force(mock_db):
    """Only a Task worker has a session to read; a shell row with a session-like
    id that happens to have fresh events is still forced as before."""
    database.create_session(session_id="sh-coord", profile_id=None, title="c")
    _park("sh-coord", ["sh-task"])
    upsert_background_task({
        "task_id": "sh-task", "session_id": "sh-coord", "kind": "shell",
        "title": "sleep", "status": "running",
        "started_at": _OLD_TS, "updated_at": _OLD_TS,
    })
    _worker_writes("sh-task")

    with patch("app.core.turn_recorder.record_task_settle_frame"), \
            patch("app.core.turn_scheduler.on_task_terminal"):
        assert await sr.sweep_stalled_parks(warn_seconds=60, force_seconds=120) == 1
    assert get_background_task("sh-task", "sh-coord")["status"] == "killed"


# =============================================================================
# A report that arrives after a forced settle is still delivered
# =============================================================================

async def test_late_report_after_a_forced_settle_is_delivered_once(mock_db):
    _coordinator("lt-coord", [("lt-worker", "tu-lt")])
    _worker_writes("lt-worker", old=True)
    engine = _Engine()

    with patch.object(session_runtime, "execute", new=engine), _read_no_output_file():
        assert await sr.sweep_stalled_parks(warn_seconds=60, force_seconds=120) == 1
        forced_wake = await _wait_for_starts(engine, 1)
        assert len(forced_wake) == 1

        # The worker was only quiet. Its real report arrives afterwards.
        await _report("lt-coord", "lt-worker", "tu-lt", "late but done: PR #12")
        await _report("lt-coord", "lt-worker", "tu-lt", "late but done: PR #12")
        started = await _wait_for_starts(engine, 2)

    assert len(started) == 2, "the late report wakes the parent once, no more"
    assert "late but done: PR #12" in started[1]["prompt"]
    assert get_background_task("lt-worker", "lt-coord")["status"] == "completed"
    assert task_registry.get_task("lt-coord", "lt-worker")["status"] == "completed"


async def test_late_report_replaces_a_forced_frame_still_queued(mock_db):
    """The forced wake has not run yet when the real report lands: the queued
    wake carries the real report instead of the app's stand-in."""
    _coordinator("lq-coord", [("lq-worker", "tu-lq")])
    _worker_writes("lq-worker", old=True)

    with patch.object(turn_scheduler, "_kick_after_batch_window"), _read_no_output_file():
        assert await sr.sweep_stalled_parks(warn_seconds=60, force_seconds=120) == 1
        queued = turn_scheduler._queued_unpark_turn("lq-coord")
        assert queued is not None and "no progress" in queued["prompt"]

        await _report("lq-coord", "lq-worker", "tu-lq", "real report: all green")

    queued = turn_scheduler._queued_unpark_turn("lq-coord")
    assert "real report: all green" in queued["prompt"]
    assert "no progress" not in queued["prompt"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS n FROM turns WHERE session_id=%s AND source='unpark'",
            ("lq-coord",),
        )
        assert cur.fetchone()["n"] == 1
