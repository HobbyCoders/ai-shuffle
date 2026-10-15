"""Monitor completion/continued-turn regressions, using real turns and registry."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core import provider_agent as pa, session_runtime as sr, turn_scheduler as ts
from app.core import query_engine
from app.core.task_registry import TaskRegistry
from app.db import turns
from tests.core.helpers import make_pump
from tests.core.test_provider_agent import dispatch_env  # noqa: F401
from tests.core.test_session_runtime import _runtime_env, _clean_module_state  # noqa: F401

REAL_TERMINAL = ts.on_task_terminal
REAL_REPORT = pa.final_assistant_text
REAL_OUTCOME = pa.final_turn_outcome
REAL_RECORDER = sr.TurnRecorder.create_for_run
REPORT = "Finished the requested work; all checks passed."


def test_late_monitor_start_does_not_revive_terminal_or_clear_delivery(monkeypatch):
    reg = TaskRegistry()
    monkeypatch.setattr(reg, "_persist_task_state", lambda *a: None)
    reg.start_task("worker", "monitor", "shell", "checks")
    reg.mark_monitor_shell_task("worker", "monitor")
    reg.complete_task("worker", "monitor", "completed")
    reg.mark_woken("worker", "monitor")
    reg.mark_background_notification_delivered("worker", "monitor")
    # The host spawner registers first; the streamed Fluid start receipt can
    # be consumed after the independent completion watcher has finished.
    reg.start_task("worker", "monitor", "shell", "checks", receipt_only=True)
    assert reg.get_active("worker") == []
    assert reg.is_woken("worker", "monitor")
    assert reg.finished_awaiting_report("worker") == []


async def test_continued_worker_consumes_monitor_and_dispatches_final_report(
    mock_db, dispatch_env, monkeypatch,
):
    reg = TaskRegistry()
    monkeypatch.setattr("app.core.placement.resolve_placement", lambda row: None)
    monkeypatch.setattr("app.core.task_registry.task_registry", reg)
    monkeypatch.setattr(query_engine, "task_registry", reg)
    monkeypatch.setattr(pa, "PARK_RECHECK_SECONDS", 0)
    monkeypatch.setattr(pa, "final_assistant_text", REAL_REPORT)
    monkeypatch.setattr(pa, "final_turn_outcome", REAL_OUTCOME)
    waits = 0
    first = None
    retired = []
    adapter = SimpleNamespace(steer=AsyncMock(), prepare_background_completions=AsyncMock(
        side_effect=lambda frames: retired.extend(frames)))
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: SimpleNamespace(
        peek=lambda sid: SimpleNamespace(adapter=adapter, is_connected=False)))

    async def idle(sid, *args, **kwargs):
        nonlocal waits, first
        waits += 1
        # Do not conceal a stuck dispatch with another synthetic completion.
        if waits > 2:
            raise AssertionError("dispatch still waiting after the final report")
        if waits == 1:
            reg.start_task(sid, "monitor", "shell", "checks")
            reg.mark_monitor_shell_task(sid, "monitor")
            first = turns.enqueue_turn(sid, "user", prompt="run checks")
            with _runtime_env(pump=make_pump([{"type": "done"}])):
                with patch("app.core.task_registry.task_registry", reg), patch.object(
                    sr.TurnRecorder, "create_for_run", REAL_RECORDER,
                ):
                    assert await sr.SessionRuntime(first).execute() == "parked"
            assert turns.get_turn(first["id"])["payload"]["task_ids"] == ["monitor"]
            return None

        continued = turns.enqueue_turn(sid, "user", prompt="continue the review")
        async def pump(**kwargs):
            runtime = sr.get_live_runtime(sid)
            assert runtime.turn_id == first["id"]
            assert runtime.turn["payload"]["continued_turn"] is True
            reg.complete_task(sid, "monitor", "completed")
            await REAL_TERMINAL(sid, {
                "task_id": "monitor", "status": "completed", "kind": "shell",
                "summary": "checks passed",
            })
            notification_id = adapter.steer.call_args.kwargs["id"]
            yield {"type": "steer_delivered", "steers": [{"id": notification_id}]}
            # A delayed start receipt must not resurrect this completed process.
            query_engine._record_task_message(sid, query_engine.TaskStartedMessage(
                subtype="task_started", data={}, task_id="monitor",
                description="checks", task_type="local_bash",
            ))
            yield {"type": "stream_delta", "delta_type": "text", "content": REPORT}
            yield {"type": "done"}

        with _runtime_env(pump=pump):
            with patch("app.core.task_registry.task_registry", reg), patch.object(
                sr.TurnRecorder, "create_for_run", REAL_RECORDER,
            ):
                assert await sr.SessionRuntime(continued).execute() == "completed"
        assert turns.get_turn(first["id"])["state"] == "completed"
        assert not turns.session_has_unsettled_turns(sid)
        return None

    monkeypatch.setattr(pa, "_await_worker_idle", idle)
    outcome = await asyncio.wait_for(pa.dispatch_worker(
        parent_session_id="parent-1", profile_id="default", provider_id="prov-openai",
        model_id="gpt-5.5", prompt="check", background=True,
    ), 10)
    assert outcome.ok and outcome.report == REPORT
    assert retired and retired[0]["task_id"] == "monitor"
    assert reg.get_active(outcome.session_id) == []
    task = reg.get_task("parent-1", outcome.session_id)
    assert task["status"] == "completed" and task["reportPath"]
    assert dispatch_env.wakes[-1][0] == "parent-1"
    assert dispatch_env.wakes[-1][1]["status"] == "completed"


@pytest.mark.parametrize("retired", [False, True])
async def test_stranded_park_dispatches_last_report_without_another_wait(
    mock_db, dispatch_env, monkeypatch, retired,
):
    """Safety net for legacy parks: no live work and the wake already landed."""
    reg = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", reg)
    monkeypatch.setattr("app.core.placement.resolve_placement", lambda row: None)
    monkeypatch.setattr(pa, "final_assistant_text", REAL_REPORT)
    monkeypatch.setattr(pa, "final_turn_outcome", REAL_OUTCOME)
    waits = 0
    first = None

    async def idle(sid, *args, **kwargs):
        nonlocal waits, first
        waits += 1
        assert waits == 1, "a stranded park must not poll forever"
        reg.start_task(sid, "monitor", "shell", "checks")
        reg.mark_monitor_shell_task(sid, "monitor")
        first = turns.enqueue_turn(sid, "user", prompt="check")
        pump = make_pump([
            {"type": "stream_delta", "delta_type": "text", "content": REPORT},
            {"type": "done"},
        ])
        with _runtime_env(pump=pump):
            with patch("app.core.task_registry.task_registry", reg), patch.object(
                sr.TurnRecorder, "create_for_run", REAL_RECORDER,
            ):
                assert await sr.SessionRuntime(first).execute() == "parked"
        reg.complete_task(sid, "monitor", "completed")
        reg.mark_woken(sid, "monitor")
        reg.mark_background_notification_delivered(sid, "monitor")
        if retired:
            reg.clear_session(sid)
        return None

    monkeypatch.setattr(pa, "_await_worker_idle", idle)
    outcome = await asyncio.wait_for(pa.dispatch_worker(
        parent_session_id="parent-1", profile_id="default", provider_id="prov-openai",
        model_id="gpt-5.5", prompt="check", background=True,
    ), 10)
    assert outcome.ok and outcome.report == REPORT
    assert turns.get_turn(first["id"])["state"] == "completed"
    assert reg.get_task("parent-1", outcome.session_id)["status"] == "completed"
    assert dispatch_env.wakes[-1][0] == "parent-1"
    assert dispatch_env.wakes[-1][1]["status"] == "completed"


@pytest.mark.parametrize("work", ["live", "queued", "unpublished", "lost"])
def test_park_recovery_preserves_live_work_and_lost_wake_diagnosis(
    mock_db, monkeypatch, work,
):
    sid = "park-safety"
    reg = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", reg)
    turn = turns.enqueue_turn(sid, "user", prompt="checks")
    turns.start_turn(turn["id"])
    turns.park_turn(turn["id"], ["monitor"])
    reg.start_task(sid, "monitor", "shell", "checks")
    reg.mark_monitor_shell_task(sid, "monitor")
    if work != "live":
        reg.complete_task(sid, "monitor", "completed")
    if work == "queued":
        turns.enqueue_turn(sid, "unpark", prompt="checks finished")
    if work == "lost":
        monkeypatch.setattr(pa, "PENDING_WAKE_LIMIT_SECONDS", 0)
    assert pa._worker_still_working(sid) is (work != "lost")
    assert turns.get_turn(turn["id"])["state"] == "parked"
    if work == "lost":
        assert pa._lost_monitor_wakes(sid) == ["monitor"]


def test_park_recovery_rechecks_admission_before_settlement(mock_db, monkeypatch):
    sid = "racing-wake"
    reg = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", reg)
    turn = turns.enqueue_turn(sid, "user", prompt="checks")
    turns.start_turn(turn["id"])
    turns.park_turn(turn["id"], ["retired"])
    settle = turns.settle_parked_turns

    def racing_settle(*args, **kwargs):
        turns.enqueue_turn(sid, "unpark", prompt="checks finished")
        assert settle(*args, **kwargs) == 0

    monkeypatch.setattr(turns, "settle_parked_turns", racing_settle)
    assert pa._worker_still_working(sid) is True
    assert turns.get_turn(turn["id"])["state"] == "parked"


@pytest.mark.parametrize("new_work", [True, False])
def test_park_recovery_does_not_settle_a_new_incarnation(mock_db, monkeypatch, new_work):
    """The same logical turn can resume and repark while recovery waits."""
    sid = "repark-race"
    reg = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", reg)
    first = turns.enqueue_turn(sid, "user", prompt="checks")
    turns.start_turn(first["id"], run_id="old-run")
    turns.park_turn(first["id"], ["retired"])
    settle = turns.settle_parked_turns
    repaired = []

    def racing_settle(*args, **kwargs):
        queued = turns.enqueue_turn(sid, "user", prompt="more checks")
        resumed = turns.start_turn(queued["id"], run_id="new-run", can_resume=lambda *a: True)
        assert resumed["id"] == first["id"]
        if new_work:
            reg.start_task(sid, "new-monitor", "shell", "new checks")
            reg.mark_monitor_shell_task(sid, "new-monitor")
        turns.park_turn(first["id"], ["new-monitor"])
        repaired.append(settle(*args, **kwargs))

    monkeypatch.setattr(turns, "settle_parked_turns", racing_settle)
    still_working = pa._worker_still_working(sid)
    assert repaired == [0], "recovery must compare the observed park incarnation"
    assert still_working is True, "changed park must be retried, not dispatched as finished"
    row = turns.get_turn(first["id"])
    assert row["state"] == "parked"
    assert row["payload"]["task_ids"] == ["new-monitor"]
    if new_work:
        assert [r["id"] for r in reg.get_active(sid)] == ["new-monitor"]



def test_park_recovery_rechecks_new_registry_work_under_admission(mock_db, monkeypatch):
    """Late registration must veto repair even if the park interval is unchanged."""
    sid = "late-monitor-registration"
    reg = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", reg)
    turn = turns.enqueue_turn(sid, "user", prompt="checks")
    turns.start_turn(turn["id"], run_id="same-run")
    parked = turns.park_turn(turn["id"], ["retired"])
    settle = turns.settle_parked_turns
    repaired = []

    def racing_settle(*args, **kwargs):
        reg.start_task(sid, "new-monitor", "shell", "new checks")
        reg.mark_monitor_shell_task(sid, "new-monitor")
        repaired.append(settle(*args, **kwargs))

    monkeypatch.setattr(turns, "settle_parked_turns", racing_settle)
    assert pa._worker_still_working(sid) is True
    assert repaired == [0]
    assert turns.get_turn(turn["id"])["state"] == "parked"
    assert turns.get_turn(turn["id"])["ended_at"] == parked["ended_at"]
    assert reg.get_task(sid, "new-monitor")["status"] == "running"


@pytest.mark.parametrize("serves", [False, True])
async def test_explicit_monitor_restart_needs_its_own_wake_and_delivery(
    mock_db, monkeypatch, serves,
):
    """A mirrored start is not a restart; a real restart owns fresh receipts."""
    sid, tid = "monitor-incarnations", "job"
    reg = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", reg)
    monkeypatch.setattr(query_engine, "task_registry", reg)
    # Even equal wall-clock readings must produce distinct start identities.
    monkeypatch.setattr("app.core.task_registry._now_iso", lambda: "2026-01-01T00:00:00+00:00")
    adapter = SimpleNamespace(steer=AsyncMock(), prepare_background_completions=AsyncMock())
    monkeypatch.setattr("app.core.session_pool.get_session_pool", lambda: SimpleNamespace(
        peek=lambda sid: SimpleNamespace(adapter=adapter)))
    runtime = sr.SessionRuntime(turns.enqueue_turn(sid, "user", prompt="checks"))
    monkeypatch.setattr(runtime, "_turn_is_dying", AsyncMock(return_value=False))
    sr._live_runtimes[sid] = runtime
    recorder = SimpleNamespace(record_task_terminal=AsyncMock(), recording_failed=False)

    reg.start_task(sid, tid, "shell", "first")
    reg.mark_monitor_shell_task(sid, tid, serves=serves)
    reg.complete_task(sid, tid, "completed")
    first = {"task_id": tid, "status": "completed", "summary": "first finished"}
    await REAL_TERMINAL(sid, first)
    first_entry = runtime._task_steers[adapter.steer.call_args.kwargs["id"]]
    first = first_entry["frames"][0]  # The stamped, in-flight receipt, not a new event.
    assert await runtime._on_task_notification_delivered(recorder, first_entry)
    assert reg.finished_awaiting_report(sid) == []

    reg.start_task(sid, tid, "shell", "second")
    assert reg.get_task(sid, tid)["status"] == "running"
    assert reg.task_incarnation(sid, tid) != first["task_incarnation"]
    assert not reg.is_woken(sid, tid)
    assert tid not in reg._terminal_notified.get(sid, set())
    # Late receipt for the first completion cannot spend the second wake.
    await REAL_TERMINAL(sid, first)
    assert adapter.steer.await_count == 1
    assert not reg.is_woken(sid, tid)
    query_engine._record_task_message(sid, query_engine.TaskStartedMessage(
        subtype="task_started", data={}, task_id=tid,
        description="old mirrored start", task_type="local_bash",
    ))
    assert reg.get_task(sid, tid)["subject"] == "second"

    reg.complete_task(sid, tid, "completed")
    second = {"task_id": tid, "status": "completed", "summary": "second finished"}
    await REAL_TERMINAL(sid, second)
    assert adapter.steer.await_count == 2
    assert reg.is_woken(sid, tid)
    assert [r["id"] for r in reg.finished_awaiting_report(sid)] == [tid]
    # An in-flight first-run delivery receipt must not satisfy the second run.
    assert await runtime._on_task_notification_delivered(recorder, first_entry)
    assert adapter.prepare_background_completions.await_count == 1
    assert tid not in reg._terminal_notified.get(sid, set())
    assert [r["id"] for r in reg.finished_awaiting_report(sid)] == [tid]
    second_entry = runtime._task_steers[adapter.steer.call_args.kwargs["id"]]
    assert await runtime._on_task_notification_delivered(recorder, second_entry)
    assert adapter.prepare_background_completions.await_count == 2
    assert reg.finished_awaiting_report(sid) == []
    assert tid in reg._terminal_notified[sid]



async def test_queued_monitor_completions_keep_incarnations_separate(mock_db, monkeypatch):
    sid, tid = "queued-monitor-incarnations", "job"
    reg = TaskRegistry()
    monkeypatch.setattr("app.core.task_registry.task_registry", reg)
    monkeypatch.setattr(ts, "_kick_after_batch_window", lambda sid: None)
    reg.start_task(sid, tid, "shell", "first")
    reg.mark_monitor_shell_task(sid, tid)
    reg.complete_task(sid, tid, "completed")
    await REAL_TERMINAL(sid, {"task_id": tid, "status": "completed", "summary": "first done"})
    first_wake = ts._queued_unpark_turn(sid)
    first_frame = first_wake["payload"]["pending_frames"][0]

    reg.start_task(sid, tid, "shell", "second")
    reg.complete_task(sid, tid, "completed")
    await REAL_TERMINAL(sid, {"task_id": tid, "status": "completed", "summary": "second done"})
    wake = ts._queued_unpark_turn(sid)
    assert wake["id"] == first_wake["id"]
    frames = wake["payload"]["pending_frames"]
    assert len(frames) == 2  # Same task id does not deduplicate different starts.
    assert frames[0] == first_frame
    assert frames[1]["task_incarnation"] != first_frame["task_incarnation"]
    assert [r["id"] for r in reg.finished_awaiting_report(sid)] == [tid]

    with _runtime_env(pump=make_pump([{"type": "done"}])) as env:
        with patch("app.core.task_registry.task_registry", reg):
            assert await sr.SessionRuntime(wake).execute() == "completed"
    # Only this incarnation can settle the card, retire engine work and latch
    # model delivery; the stale queued first-run frame never reaches those seams.
    env["recorder"].record_task_terminal.assert_awaited_once_with([frames[1]])
    assert reg.finished_awaiting_report(sid) == []
