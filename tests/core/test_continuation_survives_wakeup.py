"""A SendMessage continuation still reports when a wakeup turn parks beside it (#123).

The sequence that stranded four workers: a continuation parks on a Monitor; a
synthetic wakeup turn (the idle-at-edge watchdog's ``/compact``, or the
agent's own ScheduleWakeup) starts while it is parked, carries no
``agent_message_reply_to`` and parks too; the Monitor's unpark wake resumes
that newest parked turn; the turn-start self-heal settles the continuation
without its completion hook; and the final turn completed with no parent, so
the task stayed "running" and the parent was never woken.

Real turns, registry, runtime, recorder and settlement hook; only the provider
pump (and the parent's kick) are doubles.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core import agent_messaging, provider_agent_supervisor as supervisor
from app.core import query_engine, session_runtime as sr, turn_scheduler as ts
from app.core.task_registry import TaskRegistry
from app.db import database, turns
from tests.core.helpers import make_pump
from tests.core.test_session_runtime import _runtime_env, _clean_module_state  # noqa: F401

REAL_TERMINAL = ts.on_task_terminal
REAL_RECORDER = sr.TurnRecorder.create_for_run
PARENT, WORKER = "coord", "worker"
FINAL = "Pushed the fix; draft PR is up."


def _text(content):
    return [{"type": "stream_delta", "delta_type": "text", "content": content},
            {"type": "done", "metadata": {}}]


@pytest.fixture
def env(mock_db, monkeypatch, tmp_path):
    reg = TaskRegistry()
    monkeypatch.setattr(reg, "_persist_task_state", lambda *a: None)
    monkeypatch.setattr("app.core.task_registry.task_registry", reg)
    monkeypatch.setattr(query_engine, "task_registry", reg)
    monkeypatch.setattr("app.core.placement.resolve_placement", lambda row: None)
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: SimpleNamespace(
        peek=lambda sid: None, get=lambda sid: None))
    monkeypatch.setattr(ts, "kick", lambda sid: None)
    monkeypatch.setattr(ts, "_kick_after_batch_window", lambda sid: None)
    monkeypatch.setattr("app.core.task_dispatch.task_report_dir", lambda row: tmp_path / "reports")
    monkeypatch.setattr("app.core.worker_checkout.restore_reclaimed_worker_checkout", lambda sid: True)
    monkeypatch.setattr("app.core.worker_checkout.reclaim_untouched_worker_checkout", lambda sid: None)
    terminals = []

    async def on_task_terminal(session_id, frame, *args, **kwargs):
        terminals.append((session_id, dict(frame)))
        return await REAL_TERMINAL(session_id, frame, *args, **kwargs)

    monkeypatch.setattr(ts, "on_task_terminal", on_task_terminal)
    supervisor.reset()
    for sid in (PARENT, WORKER):
        database.create_session(session_id=sid, profile_id="default")
    yield SimpleNamespace(reg=reg, terminals=terminals)
    supervisor.reset()


async def _run(reg, row, events, *, expect):
    with _runtime_env(pump=make_pump(events)):
        with patch("app.core.task_registry.task_registry", reg), patch.object(
            sr.TurnRecorder, "create_for_run", REAL_RECORDER,
        ):
            # The production settlement hook, not the fixture's spy.
            sr.register_scheduler(ts.on_turn_settled)
            assert await sr.SessionRuntime(row).execute() == expect


async def _parked_continuation(env):
    """Round one is done; SendMessage continues the worker; it parks on a Monitor."""
    reg = env.reg
    parent_turn = turns.enqueue_turn(PARENT, "user", prompt="dispatch the fix", profile_id="default")
    turns.start_turn(parent_turn["id"], None, "runner")
    turns.settle_turn(parent_turn["id"], "completed")
    reg.start_task(PARENT, WORKER, "agent", "Fix the updater", tool_use_id=WORKER)
    first = turns.enqueue_turn(WORKER, "provider_agent", prompt="fix it", profile_id="default")
    await _run(reg, first, _text("Round one done."), expect="completed")
    reg.complete_task(PARENT, WORKER, "completed", summary="Round one done.")

    turn_id = await ts.on_agent_message(WORKER, PARENT, "Now push it", reply_to=PARENT)
    continuation = turns.get_turn(turn_id)
    assert continuation["payload"]["agent_message_reply_to"] == PARENT
    assert reg.get_task(PARENT, WORKER)["status"] == "running"

    reg.start_task(WORKER, "monitor", "shell", "ci")
    reg.mark_monitor_shell_task(WORKER, "monitor")
    await _run(reg, continuation, _text("Watching CI."), expect="parked")
    return continuation


async def _finish_monitor(env):
    env.reg.complete_task(WORKER, "monitor", "completed")
    await REAL_TERMINAL(WORKER, {
        "task_id": "monitor", "status": "completed", "kind": "shell", "summary": "CI green",
    })
    (wake,) = turns.get_queued_turns(WORKER)
    assert wake["source"] == "unpark"
    return wake


def _parent_completions(env):
    return [frame for sid, frame in env.terminals
            if sid == PARENT and frame.get("task_id") == WORKER]


@pytest.mark.parametrize("state", ["running", "parked"])
async def test_restart_closes_continuation_and_tells_parent(env, monkeypatch, state):
    from app.core import startup_reconcile, task_persistence
    from app.core.config import settings
    from app.core.task_registry import SERVER_RESTART_REASON
    from app.db import background_tasks

    monkeypatch.setattr(settings, "durable_task_state", True)
    # Unlike the runtime fixture, exercise the actual durable mirror producer.
    monkeypatch.setattr(env.reg, "_persist_task_state",
                        TaskRegistry._persist_task_state.__get__(env.reg))
    env.reg.start_task(PARENT, WORKER, "agent", "Round one", tool_use_id=WORKER)
    env.reg.complete_task(PARENT, WORKER, "completed", summary="Old report, not the continuation")
    env.reg.mark_terminal_notified(PARENT, WORKER)
    await task_persistence.flush()
    tid = await ts.on_agent_message(WORKER, PARENT, "Continue", reply_to=PARENT)
    turns.start_turn(tid, None, "runner")
    if state == "parked":
        turns.park_turn(tid, ["monitor"])
    await task_persistence.flush()
    row = background_tasks.get_background_task(WORKER, PARENT)
    assert row["status"] == "running"
    with database.get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT terminal_notified FROM background_tasks WHERE session_id=%s AND task_id=%s",
                       (PARENT, WORKER))
        assert not cursor.fetchone()["terminal_notified"]

    # Abrupt restart: no WorkerRecord, delivery latch, pool, or live registry.
    env.reg.reset_all()
    supervisor.reset()
    task_persistence.reset()
    assert agent_messaging.live_continuation_owner(WORKER) is None
    assert await startup_reconcile._mark_stale_background_task_rows() == 1
    row = background_tasks.get_background_task(WORKER, PARENT)
    assert row["status"] == "killed" and row["reason"] == SERVER_RESTART_REASON
    assert "restart" in row["summary"]
    assert env.reg.get_task(PARENT, WORKER)["status"] == "interrupted"
    (wake,) = turns.get_queued_turns(PARENT)
    assert "restarted" in wake["prompt"]
    assert "Old report" not in wake["prompt"]
    assert await startup_reconcile._mark_stale_background_task_rows() == 0
    assert len(turns.get_queued_turns(PARENT)) == 1


@pytest.mark.parametrize("park_again", [False, True])
async def test_cross_parent_self_heal_fails_dropped_parent_only(env, park_again):
    continuation = await _parked_continuation(env)
    other = "other-parent"
    database.create_session(session_id=other, profile_id="default")
    env.reg.start_task(other, WORKER, "agent", "Other parent's work", tool_use_id=WORKER)
    later = turns.enqueue_turn(WORKER, "agent_message", prompt="Other work", profile_id="default",
                               payload={"agent_message_reply_to": other})
    env.reg.complete_task(WORKER, "monitor", "completed")
    if park_again:
        env.reg.start_task(WORKER, "second-monitor", "shell", "Other CI")
        env.reg.mark_monitor_shell_task(WORKER, "second-monitor")
        # Production self-heal runs only when no background work remains at start.
        # Settle the old parked continuation before the new Monitor starts.
        turns.settle_parked_turns(WORKER, later["id"])
    await _run(env.reg, later, _text("Private result for other parent"),
               expect="parked" if park_again else "completed")

    assert turns.get_turn(continuation["id"])["state"] == "completed"
    assert turns.get_turn(later["id"])["payload"]["agent_message_reply_to"] == other
    task = env.reg.get_task(PARENT, WORKER)
    assert task["status"] == "error"
    assert "different parent" in task["summary"]
    assert "Private result" not in task["summary"]
    (failure,) = _parent_completions(env)
    assert failure["status"] == "error"
    assert failure["agent_message_turn_id"] == continuation["id"]
    (wake,) = turns.get_queued_turns(PARENT)
    assert "different parent" in wake["prompt"]
    assert "Private result" not in wake["prompt"]
    assert env.reg.get_task(other, WORKER)["status"] == ("running" if park_again else "completed")
    # Replay the settled hook; a durable displacement is not a second completion.
    await agent_messaging.finish_message_resume(WORKER, turns.get_turn(later["id"]), "parked")
    assert len(_parent_completions(env)) == 1


@pytest.mark.parametrize("stopping", ["user_stop", "torn_down"])
async def test_stop_prevents_displaced_parent_wake(env, stopping):
    from app.core.query_session_lifecycle import enter_user_stop

    await _parked_continuation(env)
    other = "other-parent"
    database.create_session(session_id=other, profile_id="default")
    env.reg.start_task(other, WORKER, "agent", "Other work", tool_use_id=WORKER)
    env.reg.mark_background_launch(other, WORKER)
    later = turns.enqueue_turn(WORKER, "agent_message", prompt="Other work", profile_id="default",
                               payload={"agent_message_reply_to": other})
    turns.start_turn(later["id"], None, "runner")
    turns.settle_parked_turns(WORKER, later["id"])
    assert turns.get_turn(later["id"])["payload"]["agent_message_displaced"]
    if stopping == "user_stop":
        await enter_user_stop(WORKER)
    else:
        supervisor.get_worker(PARENT, WORKER).torn_down = True
        supervisor.register_worker(supervisor.WorkerRecord(
            parent_session_id=other, worker_session_id=WORKER, task_id=WORKER,
            tool_use_id=WORKER, background=True, continuation_turn_id=later["id"], torn_down=True,
        ))
    turns.settle_turn(later["id"], "aborted")
    await ts.on_turn_settled(WORKER, later["id"], "aborted")
    assert env.terminals == []
    assert turns.get_queued_turns(PARENT) == []
    assert turns.get_queued_turns(other) == []


@pytest.mark.parametrize("failure_at", ["settle_frame", "terminal_wake"])
async def test_displaced_failure_never_drops_kept_parent_report(env, monkeypatch, caplog, failure_at):
    from app.core import turn_recorder

    await _parked_continuation(env)
    other = "other-parent"
    database.create_session(session_id=other, profile_id="default")
    env.reg.start_task(other, WORKER, "agent", "Other work", tool_use_id=WORKER)
    env.reg.mark_background_launch(other, WORKER)
    later = turns.enqueue_turn(WORKER, "agent_message", prompt="Other work", profile_id="default",
                               payload={"agent_message_reply_to": other})
    env.reg.complete_task(WORKER, "monitor", "completed")
    module, name = (turn_recorder, "record_task_settle_frame") if failure_at == "settle_frame" else (ts, "on_task_terminal")
    real = getattr(module, name)

    async def fail_displaced(owner, *args, **kwargs):
        if owner == PARENT:
            raise RuntimeError("Displaced path unavailable")
        return await real(owner, *args, **kwargs)

    monkeypatch.setattr(module, name, fail_displaced)
    await _run(env.reg, later, _text("Kept parent's final report"), expect="completed")
    task = env.reg.get_task(other, WORKER)
    assert task["status"] == "completed"
    assert task["summary"] == "Kept parent's final report"
    (wake,) = turns.get_queued_turns(other)
    assert "Kept parent's final report" in wake["prompt"]
    assert "displaced continuation" in caplog.text


@pytest.mark.parametrize("wakeup", ["idle_watchdog", "schedule_wakeup"])
async def test_continuation_reports_after_a_wakeup_parks_beside_it(env, wakeup):
    continuation = await _parked_continuation(env)

    if wakeup == "idle_watchdog":
        # Layer 1: a parked session is not idle, so the watchdog never picks it.
        from app.core.config import settings
        from app.db import wakeups as wakeups_db
        from app.db.database import get_db

        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE sessions SET instance_id=%s, context_usage_json=%s WHERE id=%s",
                (settings.effective_instance_id, '{"used": 9}', WORKER),
            )
        assert WORKER not in {
            c["id"] for c in wakeups_db.idle_edge_candidates(settings.effective_instance_id)
        }
    # The wake fires anyway (a nudge armed before the park, or the agent's own
    # ScheduleWakeup): a new turn with no parent link that parks on the same
    # still-running Monitor.
    prompt = "/compact" if wakeup == "idle_watchdog" else "Check on CI."
    nudge = turns.enqueue_turn(WORKER, "wakeup", prompt=prompt, profile_id="default")
    assert "agent_message_reply_to" not in nudge["payload"]
    await _run(env.reg, nudge, _text("Compacted."), expect="parked")
    assert turns.get_turn(continuation["id"])["state"] == "parked"
    assert turns.get_turn(nudge["id"])["state"] == "parked"
    assert env.reg.get_task(PARENT, WORKER)["status"] == "running"
    assert _parent_completions(env) == []

    wake = await _finish_monitor(env)
    await _run(env.reg, wake, _text(FINAL), expect="completed")

    # The unpark resumed the wakeup's row; the continuation was self-healed.
    assert turns.get_turn(continuation["id"])["stop_reason"] == "parked_settled"
    final = turns.get_turn(nudge["id"])
    assert final["state"] == "completed"
    assert final["payload"]["agent_message_reply_to"] == PARENT
    task = env.reg.get_task(PARENT, WORKER)
    assert task["status"] == "completed"
    assert task["reportPath"]
    from pathlib import Path
    assert Path(task["reportPath"]).read_text() == FINAL
    (completion,) = _parent_completions(env)
    assert completion["status"] == "completed"
    assert completion["summary"] == FINAL
    assert completion["agent_message_turn_id"] == nudge["id"]
    (parent_wake,) = turns.get_queued_turns(PARENT)
    assert FINAL in parent_wake["prompt"]
    assert supervisor.get_worker(PARENT, WORKER) is None


async def test_reply_to_less_turn_after_a_continuation_inherits_its_owner(env):
    """Layer 2 alone: no self-heal hand-off, the live continuation owner still gets it."""
    continuation = await _parked_continuation(env)
    env.reg.complete_task(WORKER, "monitor", "completed")
    # The park resolved with no hand-off (no exclude_turn_id); a later turn
    # with no parent link then runs the last step.
    turns.settle_parked_turns(WORKER, turn_ids=[continuation["id"]])
    later = turns.enqueue_turn(WORKER, "wakeup", prompt="Check on CI.", profile_id="default")
    await _run(env.reg, later, _text(FINAL), expect="completed")
    assert "agent_message_reply_to" not in turns.get_turn(later["id"])["payload"]
    task = env.reg.get_task(PARENT, WORKER)
    assert task["status"] == "completed" and task["reportPath"]
    (completion,) = _parent_completions(env)
    assert completion["summary"] == FINAL


async def test_turn_queued_before_the_continuation_does_not_report_it(env, monkeypatch):
    """A turn that ran before SendMessage queued the continuation is not its result."""
    reg = env.reg
    reg.start_task(PARENT, WORKER, "agent", "Fix the updater", tool_use_id=WORKER)
    earlier = turns.enqueue_turn(WORKER, "user", prompt="earlier", profile_id="default")
    turns.start_turn(earlier["id"], None, "runner")
    continuation = turns.enqueue_turn(
        WORKER, "agent_message", prompt="continue", profile_id="default",
        payload={"agent_message_reply_to": PARENT},
    )
    supervisor.register_worker(supervisor.WorkerRecord(
        parent_session_id=PARENT, worker_session_id=WORKER, task_id=WORKER,
        tool_use_id=WORKER, background=True, continuation_turn_id=continuation["id"],
    ))
    settled = turns.settle_turn(earlier["id"], "completed")
    await agent_messaging.finish_message_resume(WORKER, settled, "completed")
    assert reg.get_task(PARENT, WORKER)["status"] == "running"
    assert _parent_completions(env) == []


async def test_first_dispatch_turn_without_continuation_is_left_to_its_dispatch(env):
    """A first Task dispatch has no continuation record: its dispatch tail completes it."""
    reg = env.reg
    reg.start_task(PARENT, WORKER, "agent", "Fix the updater", tool_use_id=WORKER)
    supervisor.register_worker(supervisor.WorkerRecord(
        parent_session_id=PARENT, worker_session_id=WORKER, task_id=WORKER,
        tool_use_id=WORKER, background=True,
    ))
    nudge = turns.enqueue_turn(WORKER, "wakeup", prompt="/compact", profile_id="default")
    await _run(reg, nudge, _text(FINAL), expect="completed")
    assert reg.get_task(PARENT, WORKER)["status"] == "running"
    assert _parent_completions(env) == []


def test_self_heal_hands_the_parent_link_to_the_turn_going_live(mock_db):
    parked = turns.enqueue_turn("s", "agent_message", prompt="continue",
                                payload={"agent_message_reply_to": PARENT})
    turns.start_turn(parked["id"], None, "runner")
    turns.park_turn(parked["id"], ["monitor"])
    live = turns.enqueue_turn("s", "wakeup", prompt="/compact")
    turns.start_turn(live["id"], None, "runner")
    assert turns.settle_parked_turns("s", live["id"]) == 1
    assert turns.get_turn(live["id"])["payload"]["agent_message_reply_to"] == PARENT


def test_self_heal_keeps_a_live_turns_own_parent(mock_db):
    parked = turns.enqueue_turn("s", "agent_message", prompt="continue",
                                payload={"agent_message_reply_to": "other-parent"})
    turns.start_turn(parked["id"], None, "runner")
    turns.park_turn(parked["id"], ["monitor"])
    live = turns.enqueue_turn("s", "agent_message", prompt="again",
                              payload={"agent_message_reply_to": PARENT})
    turns.start_turn(live["id"], None, "runner")
    assert turns.settle_parked_turns("s", live["id"]) == 1
    assert turns.get_turn(live["id"])["payload"]["agent_message_reply_to"] == PARENT
    assert turns.get_turn(live["id"])["payload"]["agent_message_displaced"] == [{
        "owner": "other-parent", "turn_id": parked["id"], "position": float(parked["position"]),
    }]


def test_sweeps_without_a_live_turn_change_no_payload(mock_db):
    parked = turns.enqueue_turn("s", "agent_message", prompt="continue",
                                payload={"agent_message_reply_to": PARENT})
    turns.start_turn(parked["id"], None, "runner")
    turns.park_turn(parked["id"], ["monitor"])
    assert turns.settle_parked_turns("s") == 1
    assert turns.get_turn(parked["id"])["state"] == "completed"


async def _continued(env):
    """Round one done; SendMessage queued a continuation (not yet run)."""
    reg = env.reg
    parent_turn = turns.enqueue_turn(PARENT, "user", prompt="dispatch the fix", profile_id="default")
    turns.start_turn(parent_turn["id"], None, "runner")
    turns.settle_turn(parent_turn["id"], "completed")
    reg.start_task(PARENT, WORKER, "agent", "Fix the updater", tool_use_id=WORKER)
    first = turns.enqueue_turn(WORKER, "provider_agent", prompt="fix it", profile_id="default")
    await _run(reg, first, _text("Round one done."), expect="completed")
    reg.complete_task(PARENT, WORKER, "completed", summary="Round one done.")
    turn_id = await ts.on_agent_message(WORKER, PARENT, "Now push it", reply_to=PARENT)
    return turns.get_turn(turn_id)


async def test_a_turn_behind_a_delivered_continuation_never_reports_again(env):
    """The continuation completed with a turn queued behind it: its report went
    to the parent but the task stayed running (busy). That later turn, with no
    reply_to, must not send a second completion with its own text; it only
    closes the task row with the continuation's report, quietly."""
    continuation = await _continued(env)
    later = turns.enqueue_turn(WORKER, "user", prompt="unrelated chat", profile_id="default")
    await _run(env.reg, continuation, _text("CONTINUATION RESULT"), expect="completed")
    assert env.reg.get_task(PARENT, WORKER)["status"] == "running"  # busy: `later` queued
    assert [f["summary"] for f in _parent_completions(env)] == ["CONTINUATION RESULT"]

    await _run(env.reg, later, _text("USER CHAT ANSWER"), expect="completed")

    assert [f["summary"] for f in _parent_completions(env)] == ["CONTINUATION RESULT"]
    task = env.reg.get_task(PARENT, WORKER)
    assert task["status"] == "completed"
    assert task["summary"] == "CONTINUATION RESULT"
    from pathlib import Path
    assert Path(task["reportPath"]).read_text() == "CONTINUATION RESULT"
    assert supervisor.get_worker(PARENT, WORKER) is None
    (parent_wake,) = turns.get_queued_turns(PARENT)
    assert "USER CHAT ANSWER" not in parent_wake["prompt"]


async def test_a_stand_in_report_is_delivered_once_per_continuation(env):
    """A turn standing in for an unreported continuation reports it once; the
    next reply_to-less turn finds that delivery and does not report again."""
    continuation = await _parked_continuation(env)
    env.reg.complete_task(WORKER, "monitor", "completed")
    turns.settle_parked_turns(WORKER, turn_ids=[continuation["id"]])
    stand_in = turns.enqueue_turn(WORKER, "wakeup", prompt="Check on CI.", profile_id="default")
    after = turns.enqueue_turn(WORKER, "user", prompt="and another thing", profile_id="default")
    await _run(env.reg, stand_in, _text(FINAL), expect="completed")
    assert env.reg.get_task(PARENT, WORKER)["status"] == "running"  # busy: `after` queued
    (completion,) = _parent_completions(env)
    assert completion["summary"] == FINAL
    assert completion["agent_message_turn_id"] == continuation["id"]

    await _run(env.reg, after, _text("UNRELATED"), expect="completed")

    assert len(_parent_completions(env)) == 1
    task = env.reg.get_task(PARENT, WORKER)
    assert task["status"] == "completed"
    assert task["summary"] == FINAL


@pytest.mark.parametrize("ending", ["stop", "archive"])
async def test_a_turn_after_stop_or_archive_never_stands_in(env, ending):
    """Stop and archive settle the parked continuation 'parked_settled' too, and
    the user's next message (or a restore) clears the latch. What the user says
    afterwards is not the continuation's result: the parent hears nothing."""
    from app.core import archive_guard
    from app.core.query_session_lifecycle import enter_user_stop

    continuation = await _parked_continuation(env)
    env.reg.complete_task(WORKER, "monitor", "killed")
    if ending == "stop":
        await enter_user_stop(WORKER)
    else:
        await archive_guard.stop_work(WORKER)
    assert turns.get_turn(continuation["id"])["stop_reason"] == "parked_settled"
    if ending == "stop":
        env.reg.clear_user_stop(WORKER)  # the user's next message
    else:
        archive_guard.release_after_restore(WORKER)

    chat = turns.enqueue_turn(WORKER, "user", prompt="what's up?", profile_id="default")
    await _run(env.reg, chat, _text("USER CHAT ANSWER"), expect="completed")

    assert _parent_completions(env) == []
    assert turns.get_queued_turns(PARENT) == []


def _worker_task_states(session_id=PARENT):
    from app.db import session_events
    return [row["payload"].get("state") for row in session_events.get_events(session_id)
            if row["event_type"] == "task.updated" and row["payload"].get("task_id") == WORKER]


async def test_a_report_delivered_while_the_worker_keeps_going_leaves_it_running(env):
    """#320: the continuation's turn ends while the worker's own ScheduleWakeup
    turn is already queued. The parent is sent that round's report and wakes,
    but the worker is still at work, so the task row stays running. The
    parent's wake turn must not write a terminal task.updated for it: that frame
    is what emptied the running agents panel while TaskList still said running.
    The worker's last turn then closes the row and that settle is recorded."""
    continuation = await _continued(env)
    wakeup = turns.enqueue_turn(WORKER, "wakeup", prompt="Check the survey.", profile_id="default")
    await _run(env.reg, continuation, _text("Survey started; checking back."), expect="completed")
    assert env.reg.get_task(PARENT, WORKER)["status"] == "running"  # busy: wakeup queued
    assert [f["summary"] for f in _parent_completions(env)] == ["Survey started; checking back."]
    (parent_wake,) = turns.get_queued_turns(PARENT)
    states_before_wake = _worker_task_states()

    await _run(env.reg, parent_wake, _text("Noted, it is still going."), expect="parked")

    # The wake delivered the report, but the log never says the worker stopped.
    assert _worker_task_states() == states_before_wake
    assert states_before_wake[-1] == "running"
    assert env.reg.get_task(PARENT, WORKER)["status"] == "running"
    # ...and the parent parked on it (the frontend's turn.parked task_ids).
    assert turns.get_turn(parent_wake["id"])["state"] == "parked"
    assert WORKER in turns.get_turn(parent_wake["id"])["payload"]["task_ids"]

    await _run(env.reg, wakeup, _text("Survey finished."), expect="completed")

    # The worker's last turn closes the row; that settle reaches the panel.
    assert env.reg.get_task(PARENT, WORKER)["status"] == "completed"
    assert _worker_task_states()[-1] == "completed"
