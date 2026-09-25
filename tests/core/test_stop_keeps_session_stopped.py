"""A stopped session stays stopped until the user speaks again (round 7).

The bug, in the user's words: "stop button not killing all work including
background tasks making sessions i stop constantly come back to life". Pressing
Stop killed the turn but left several roads open back into the scheduler — a
spared Monitor shell settling, a worker (or a NESTED worker registered under
the root session) reaching a terminal state, an armed wakeup row coming due, or
simply the settle path draining a turn that was queued behind the stopped one.
Any one of them started a fresh turn seconds after the button.

What is pinned here, end to end against the real turns/wakeups tables:

* Stop kills every task — Monitor shell, worker, nested worker — and each one
  reaches a terminal registry status AND emits its terminal frame, so nothing
  sits "running" forever and the model is never left waiting on a corpse;
* every one of those completions AFTER the stop enqueues nothing;
* armed wakeups are gone, and one that somehow survives is dropped at fire time
  rather than submitting a turn;
* the queue is emptied, so the settle drain has nothing to start;
* the user's NEXT message runs normally and re-opens all of it.
"""

from __future__ import annotations

from contextlib import ExitStack
from unittest.mock import AsyncMock, patch

import pytest

from app.core import turn_manager, turn_scheduler, wakeup_service
from app.core.config import settings
from app.core.query_session_lifecycle import stop_all_background_tasks
from app.core.task_registry import task_registry
from app.db import database
from app.db import turns as turns_db
from app.db import wakeups as wakeups_db
from tests.shared_fixtures import clean_task_registry  # noqa: F401  (autouse fixture)

SESSION = "stopped-stays-stopped"
SHELL = "shell-dev-server"
WORKER = "worker-top"
NESTED = "worker-nested"


def _make_session(session_id: str = SESSION) -> str:
    database.register_install(
        settings.effective_instance_id, "test-runner", None, "local", "1", {},
        approve_new=True,
    )
    database.create_session(
        session_id=session_id, profile_id="default", title="t",
    )
    return session_id


def _seed_tasks(session_id: str) -> None:
    """One Monitor shell, one background worker, one NESTED worker.

    Nested Task-tool workers register under the ROOT session id
    (``task_dispatch`` passes ``parent_session_id=root_session_id``), which is
    exactly why Stop has to reach them: they are in the root's registry bucket
    and their completions wake the root.
    """
    task_registry.start_task(session_id, SHELL, "shell", "npm run dev")
    task_registry.mark_monitor_shell_task(session_id, SHELL)

    for task_id, title in ((WORKER, "top-level worker"), (NESTED, "nested worker")):
        task_registry.start_task(
            session_id, task_id, "agent", title, tool_use_id=f"tu-{task_id}",
        )
        task_registry.mark_background_launch(session_id, f"tu-{task_id}")


class _KillSeams:
    """Stub the things that actually touch processes/clients, nothing else.

    ``stop_session_task`` is the per-task kill; the point of these tests is what
    happens AROUND it, so it answers success without a subprocess or an SDK
    client. Everything downstream — the terminal guarantee, the engine
    retirement, the latch, the durable quiesce — stays real.
    """

    def __enter__(self):
        self._stack = ExitStack()
        self.stopped: list[str] = []
        self.settled: list[tuple] = []
        self.retired: list[list] = []

        async def _stop(sid, task_id):
            self.stopped.append(task_id)
            return {"ok": True, "task_id": task_id}

        async def _retire(sid, frames):
            self.retired.append(list(frames))
            return True

        def _surface(sid, task):
            # Stands in for chat_ws_handler's terminal surface (chip + durable
            # row). Registered the same way the app registers the real one.
            self.settled.append((task.get("id"), task.get("status")))

        self._stack.enter_context(patch(
            "app.core.query_session_lifecycle.stop_session_task",
            new=AsyncMock(side_effect=_stop),
        ))
        self._stack.enter_context(patch(
            "app.core.turn_scheduler.retire_task_terminals",
            new=AsyncMock(side_effect=_retire),
        ))
        task_registry.set_terminal_notification_callback(_surface)
        return self

    def __exit__(self, *exc):
        task_registry.set_terminal_notification_callback(None)
        self._stack.close()
        return False


def _arm_wakeup(session_id: str, wakeup_id: str = "wake-1") -> dict:
    return wakeups_db.create_wakeup(
        id=wakeup_id,
        session_id=session_id,
        prompt="pick this back up",
        fire_at=0.0,
        profile_id="default",
        source="idle_watchdog",
        instance_id=settings.effective_instance_id,
    )


# ═══ The stop itself ═════════════════════════════════════════════════════════


async def test_stop_kills_every_task_and_quiesces_the_session(mock_db):
    _make_session()
    _seed_tasks(SESSION)
    live = turns_db.enqueue_turn(SESSION, "user", prompt="do the thing")["id"]
    turns_db.start_turn(live, run_id="run-1")
    behind = turns_db.enqueue_turn(SESSION, "queue", prompt="and then this")["id"]
    _arm_wakeup(SESSION)

    with _KillSeams() as seams:
        await stop_all_background_tasks(SESSION, user_stop=True)

    # 1. Every task killed — the shell included. No sparing by default.
    assert sorted(seams.stopped) == sorted([SHELL, WORKER, NESTED])
    # 2. Every task terminal in the registry, with a summary the chip can show.
    for task_id in (SHELL, WORKER, NESTED):
        record = task_registry.get_task(SESSION, task_id)
        assert record["status"] == "killed", task_id
        assert record["summary"], task_id
    # 3. Every task surfaced its terminal exactly once (087d3809 F2: the agent
    #    worker used to emit nothing at all while the shell got a proper event).
    assert sorted(t for t, _ in seams.settled) == sorted([SHELL, WORKER, NESTED])
    assert len(seams.settled) == 3
    # 4. The engine is told, so fluid stops parking on dead ids.
    assert len(seams.retired) == 1
    assert {f["task_id"] for f in seams.retired[0]} == {SHELL, WORKER, NESTED}
    # 5. Nothing left that could start a turn.
    assert task_registry.is_user_stopped(SESSION)
    assert turns_db.get_queued_turns(SESSION) == []
    assert turns_db.get_turn(behind)["state"] == "aborted"
    assert wakeups_db.get_pending_wakeups(settings.effective_instance_id) == []


async def test_terminal_surface_fires_exactly_once_per_task(mock_db):
    """Whether the kill path settled the row or the guarantee did, one frame.

    ``stop_session_task`` normally marks the row itself (that is what the shell
    branch has always done). The terminal guarantee must therefore be a
    no-op-if-already-done, not a second chip, and a second Stop click must add
    nothing at all.
    """
    _make_session()
    _seed_tasks(SESSION)

    async def _stop_and_settle(sid, task_id):
        # Stand in for the real kill paths, which complete the row themselves.
        task_registry.complete_task(
            sid, task_id, "killed", summary="Stopped by user",
            reason="user_stopped",
        )
        return {"ok": True, "task_id": task_id}

    with _KillSeams() as seams:
        with patch(
            "app.core.query_session_lifecycle.stop_session_task",
            new=AsyncMock(side_effect=_stop_and_settle),
        ):
            await stop_all_background_tasks(SESSION, user_stop=True)
            await stop_all_background_tasks(SESSION, user_stop=True)

    assert sorted(t for t, _ in seams.settled) == sorted([SHELL, WORKER, NESTED])
    assert len(seams.settled) == 3


# ═══ …and then everything completes ══════════════════════════════════════════


@pytest.mark.parametrize(
    "task_id,status,kind",
    [
        (SHELL, "completed", "shell"),
        (WORKER, "completed", "agent"),
        (NESTED, "error", "agent"),
    ],
)
async def test_a_completion_after_stop_enqueues_nothing(
    mock_db, task_id, status, kind
):
    """The race the per-task suppression alone could not close."""
    _make_session()
    _seed_tasks(SESSION)
    live = turns_db.enqueue_turn(SESSION, "user", prompt="p")["id"]
    turns_db.start_turn(live, run_id="run-1")

    with _KillSeams():
        await stop_all_background_tasks(SESSION, user_stop=True)

    before = len(turns_db.get_queued_turns(SESSION))
    with patch.object(turn_scheduler, "kick") as kick:
        await turn_scheduler.on_task_terminal(SESSION, {
            "task_id": task_id,
            "status": status,
            "summary": "here is what I did",
            "kind": kind,
        })
    assert turns_db.get_queued_turns(SESSION) == []
    assert before == 0
    kick.assert_not_called()


async def test_a_task_the_registry_never_saw_also_enqueues_nothing(mock_db):
    """A worker that registers AFTER the sweep is covered by the session latch.

    Per-task ``suppress_wake`` can only cover the tasks that existed when Stop
    ran. This is the one it cannot: a nested worker whose registration lands a
    beat later, then completes.
    """
    _make_session()
    _seed_tasks(SESSION)
    with _KillSeams():
        await stop_all_background_tasks(SESSION, user_stop=True)

    latecomer = "worker-registered-after-the-stop"
    task_registry.start_task(
        SESSION, latecomer, "agent", "late", tool_use_id="tu-late",
    )
    task_registry.mark_background_launch(SESSION, "tu-late")
    assert not task_registry.is_wake_suppressed(SESSION, latecomer)

    await turn_scheduler.on_task_terminal(SESSION, {
        "task_id": latecomer,
        "status": "completed",
        "summary": "done",
        "kind": "agent",
    })
    assert turns_db.get_queued_turns(SESSION) == []


async def test_enqueue_context_wake_refuses_a_stopped_session(mock_db):
    """The last gate before a row is written refuses too (belt and braces)."""
    _make_session()
    task_registry.mark_user_stopped(SESSION)

    turn = await turn_scheduler._enqueue_context_wake(
        SESSION, "unpark", [{"task_id": "t", "status": "completed"}], ["t"], "p",
    )
    assert turn["id"] is None
    assert turns_db.get_queued_turns(SESSION) == []


# ═══ …and the wakeup that was already in flight ══════════════════════════════


async def test_a_wakeup_that_survives_the_stop_is_dropped_not_submitted(mock_db):
    _make_session()
    with _KillSeams():
        await stop_all_background_tasks(SESSION, user_stop=True)

    # Armed AFTER the sweep deleted the session's rows — the timer-already-
    # fired race the row deletion cannot reach.
    row = _arm_wakeup(SESSION, "wake-late")

    with patch.object(
        turn_manager, "submit_async", new=AsyncMock(return_value="t-x")
    ) as submit:
        await wakeup_service._fire(row)

    submit.assert_not_awaited()
    # Claimed on the way out, so no rehydrate re-arms it later.
    assert wakeups_db.get_pending_wakeups(settings.effective_instance_id) == []


async def test_server_sources_cannot_arm_a_new_wake_on_a_stopped_session(mock_db):
    _make_session()
    task_registry.mark_user_stopped(SESSION)

    row = await wakeup_service.schedule(
        session_id=SESSION,
        profile_id="default",
        project_id=None,
        prompt="nudge",
        delay_seconds=60,
        source="idle_watchdog",
    )
    assert row == {}
    assert wakeups_db.get_pending_wakeups(settings.effective_instance_id) == []


# ═══ …until the user speaks again ════════════════════════════════════════════


async def test_the_next_user_message_starts_a_turn_and_reopens_the_wakes(mock_db):
    _make_session()
    _seed_tasks(SESSION)
    live = turns_db.enqueue_turn(SESSION, "user", prompt="p")["id"]
    turns_db.start_turn(live, run_id="run-1")
    with _KillSeams():
        await stop_all_background_tasks(SESSION, user_stop=True)
    turns_db.settle_turn(live, "aborted")

    turn_id = turn_manager.submit(
        SESSION, "user", prompt="ok, carry on", profile_id="default",
    )

    assert turn_id
    assert [t["id"] for t in turns_db.get_queued_turns(SESSION)] == [turn_id]
    assert not task_registry.is_user_stopped(SESSION)

    # ...and the wake machinery is live again for work started from here.
    fresh = "worker-after-the-new-message"
    task_registry.start_task(
        SESSION, fresh, "agent", "fresh", tool_use_id="tu-fresh",
    )
    task_registry.mark_background_launch(SESSION, "tu-fresh")
    with patch.object(turn_scheduler, "_kick_after_batch_window"), patch.object(
        turn_scheduler, "_try_steer_terminal_notification",
        new=AsyncMock(return_value=False),
    ), patch.object(
        turn_scheduler, "_cli_owns_at_rest_wake", return_value=False
    ):
        await turn_scheduler.on_task_terminal(SESSION, {
            "task_id": fresh,
            "status": "completed",
            "summary": "finished",
            "kind": "agent",
        })
    assert any(
        t.get("source") == "unpark" for t in turns_db.get_queued_turns(SESSION)
    )


async def test_a_server_sourced_turn_does_not_lift_the_stop(mock_db):
    """Only a PERSON releases the latch. An automation/wakeup turn must not."""
    _make_session()
    task_registry.mark_user_stopped(SESSION)

    turn_manager.submit(
        SESSION, "wakeup", prompt="server nudge", profile_id="default",
    )
    assert task_registry.is_user_stopped(SESSION)

    turn_manager.submit(SESSION, "user", prompt="hello", profile_id="default")
    assert not task_registry.is_user_stopped(SESSION)


# ═══ The cross-runner durable bit ════════════════════════════════════════════


async def test_the_owning_runner_honours_the_durable_stop_background_bit():
    """The WebSocket that wrote the stop may live on another install.

    ``turns.stop_background`` is the durable carrier, and the runner that
    actually holds the session's tasks must act on it with the SAME semantics —
    otherwise the latch lands on an install with an empty registry while the
    real work keeps running (and waking) on this one.
    """
    from app.core.session_runtime import SessionRuntime

    rt = SessionRuntime.__new__(SessionRuntime)
    rt.session_id = "cross-runner"
    rt.turn_id = "t-1"
    rt._interrupt_driving = False
    rt._interrupt_requested_at = None
    rt._abort_reason = None
    rt._drain_after_cancel = False
    rt._pump_task = None
    rt.source = "user"
    rt.turn = {"command_id": None}

    from app.core.session_runtime import permission_handler as _ph

    with patch.object(_ph, "cancel_all_requests", new=AsyncMock()), patch(
        "app.core.session_runtime.record_interrupting", new=AsyncMock()
    ), patch(
        "app.core.query_session_lifecycle.interrupt_session", new=AsyncMock()
    ), patch(
        "app.core.query_session_lifecycle.stop_all_background_tasks",
        new=AsyncMock(return_value=[]),
    ) as stop_bg, patch(
        "app.core.session_runtime.turns_db"
    ) as turns_mod:
        turns_mod.get_turn.return_value = {
            "state": "aborted", "stop_background": True,
        }
        turns_mod.TERMINAL_STATES = ("completed", "aborted", "parked")
        await rt.interrupt("stop")

    stop_bg.assert_awaited_once_with("cross-runner", user_stop=True)


async def test_send_now_never_stops_background_work():
    """Typing mid-stream is a steer, not a Stop — semantics unchanged."""
    from app.core.session_runtime import SessionRuntime

    rt = SessionRuntime.__new__(SessionRuntime)
    rt.session_id = "steered"
    rt.turn_id = "t-1"
    rt._interrupt_driving = False
    rt._interrupt_requested_at = None
    rt._abort_reason = None
    rt._drain_after_cancel = False
    rt._pump_task = None
    rt.source = "user"
    rt.turn = {"command_id": None}

    from app.core.session_runtime import permission_handler as _ph

    with patch.object(_ph, "cancel_all_requests", new=AsyncMock()), patch(
        "app.core.session_runtime.record_interrupting", new=AsyncMock()
    ), patch(
        "app.core.query_session_lifecycle.interrupt_session", new=AsyncMock()
    ), patch(
        "app.core.query_session_lifecycle.stop_all_background_tasks",
        new=AsyncMock(return_value=[]),
    ) as stop_bg, patch(
        "app.core.session_runtime.turns_db"
    ) as turns_mod:
        turns_mod.get_turn.return_value = {
            "state": "aborted", "stop_background": False,
        }
        turns_mod.TERMINAL_STATES = ("completed", "aborted", "parked")
        await rt.interrupt("send_now")

    stop_bg.assert_not_awaited()
    assert not task_registry.is_user_stopped("steered")
