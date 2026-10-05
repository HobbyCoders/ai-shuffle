"""Background-task terminals: retirement, hand-off and wake churn.

Round-6 audit fixes. Four failures, all of them about what happens to a
background task the moment it stops running:

* a completion delivered MID-TURN (as a steer) was never submitted to the
  engine, so fluid kept its id in ``_background_pending`` and listed it on every
  later ``SessionParked`` — one audited session was "parked" on a task twelve
  hours dead;
* a parent woken about its own worker could not read the report: the record had
  already aged out of the registry, and the "report" it would have found was the
  worker's whole between-tools monologue with its OPENING line as the headline;
* a worker that ended its turn to park on its own Monitor was booked
  ``completed`` with a half-written brief;
* deliberate cancellations woke whole turns to say "noted", one per task.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.core import provider_agent, provider_agent_supervisor, turn_scheduler
from app.core.task_registry import TaskRegistry, task_registry
from app.db import turns as turns_db


# ---------------------------------------------------------------------------
# Fixtures / doubles
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _short_terminal_batch_window(monkeypatch):
    """The real 2s terminal-batch window made every on_task_terminal test wait
    it out; keep a short (non-zero, so batching still happens) window."""
    monkeypatch.setattr(turn_scheduler, "TERMINAL_BATCH_WINDOW_SECONDS", 0.1)


@pytest.fixture(autouse=True)
def _clean_module_state():
    from app.core import session_runtime, turn_manager

    session_runtime.register_scheduler(None)
    turn_manager.register_kick(None)
    turn_scheduler._kick_tasks.clear()
    turn_scheduler._batch_kick_tasks.clear()
    task_registry.reset_all()
    yield
    session_runtime.register_scheduler(None)
    turn_manager.register_kick(None)
    turn_scheduler._kick_tasks.clear()
    turn_scheduler._batch_kick_tasks.clear()
    task_registry.reset_all()
    provider_agent_supervisor.reset()


class _RecordingAdapter:
    """The one adapter method the retirement path uses."""

    def __init__(self) -> None:
        self.submitted: list = []

    async def prepare_background_completions(self, frames) -> None:
        self.submitted.append(list(frames or []))


def _pool_with(adapter):
    """Patch the session pool so ``peek`` answers with ``adapter``."""
    pool = SimpleNamespace(peek=lambda sid: SimpleNamespace(adapter=adapter))
    return patch("app.core.session_pool.get_session_pool", lambda: pool)


async def _drain_kicks():
    for _ in range(10):
        tasks = list(turn_scheduler._kick_tasks)
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


def _seed_own_background_task(session_id: str, task_id: str, **kwargs) -> None:
    """Register ``task_id`` as the session's OWN background work."""
    task_registry.start_task(
        session_id, task_id, kwargs.pop("kind", "shell"),
        kwargs.pop("title", "watch the build"), tool_use_id=task_id,
    )
    task_registry.mark_background_launch(session_id, task_id)


def _user_turn(session_id: str) -> dict:
    return turns_db.enqueue_turn(
        session_id, "user", prompt="do the thing", profile_id="p1",
    )


# ===========================================================================
# FIX 1 — a mid-turn completion is retired in the engine
# ===========================================================================

class TestEngineRetirement:
    @pytest.mark.asyncio
    async def test_retire_submits_the_frames_to_the_pooled_adapter(self):
        adapter = _RecordingAdapter()
        with _pool_with(adapter):
            assert await turn_scheduler.retire_task_terminals(
                "sess-1", [{"task_id": "t-1", "status": "completed"}]
            ) is True
        assert adapter.submitted == [[{"task_id": "t-1", "status": "completed"}]]

    @pytest.mark.asyncio
    async def test_frames_with_no_task_id_are_not_submitted(self):
        adapter = _RecordingAdapter()
        with _pool_with(adapter):
            assert await turn_scheduler.retire_task_terminals(
                "sess-1", [{"status": "completed"}, "junk"]
            ) is False
        assert adapter.submitted == []

    @pytest.mark.asyncio
    async def test_a_backend_that_cannot_retire_is_not_an_error(self):
        with _pool_with(object()):
            assert await turn_scheduler.retire_task_terminals(
                "sess-1", [{"task_id": "t-1"}]
            ) is False

    @pytest.mark.asyncio
    async def test_an_adapter_failure_never_propagates(self):
        class _Boom:
            async def prepare_background_completions(self, frames):
                raise RuntimeError("engine gone")

        with _pool_with(_Boom()):
            assert await turn_scheduler.retire_task_terminals(
                "sess-1", [{"task_id": "t-1"}]
            ) is False


class TestMidTurnDeliveryRetires:
    """The steer RECEIPT is what retires the task — the audited hole."""

    @pytest.mark.asyncio
    async def test_delivered_notification_retires_and_latches(self):
        from app.core.session_runtime import SessionRuntime

        runtime = SessionRuntime.__new__(SessionRuntime)
        runtime.session_id = "sess-1"
        recorder = SimpleNamespace(record_task_terminal=AsyncMock())
        frames = [{"task_id": "t-1", "status": "completed", "summary": "done"}]

        with patch.object(
            turn_scheduler, "retire_task_terminals", new=AsyncMock(return_value=True)
        ) as retire, patch.object(
            turn_scheduler, "on_task_notification_delivered"
        ) as latch:
            await runtime._on_task_notification_delivered(
                recorder, {"frames": frames, "task_ids": ["t-1"]},
            )

        retire.assert_awaited_once_with("sess-1", frames)
        latch.assert_called_once_with("sess-1", ["t-1"])
        recorder.record_task_terminal.assert_awaited_once_with(frames)


class TestFluidDropsTheRetiredId:
    """End to end against fluid's own background lifecycle.

    The submission only matters because of what fluid does with it: a
    ``BackgroundTaskFinished`` pops the id out of ``_background_pending``, and
    ``_background_pending`` is what every ``SessionParked`` lists.
    """

    @staticmethod
    def _engine():
        from fluid.engine.background import BackgroundLifecycleMixin

        class _Engine(BackgroundLifecycleMixin):
            def __init__(self) -> None:
                self.ctx = SimpleNamespace(extra={})
                self._init_background_lifecycle()
                self._background_rehydrated = True  # no store to replay
                self._active_turn_id = "turn-1"
                self.emitted: list = []
                self.wakes = 0

            async def _ensure_session(self):
                return SimpleNamespace(session_id="sess-1")

            async def _emit(self, event):
                self.emitted.append(event)
                return event

            def _kick_background_wake(self) -> None:
                self.wakes += 1

            async def submit(self, op) -> None:
                await self._finish_background_task(op)

        return _Engine()

    @pytest.mark.asyncio
    async def test_a_steered_terminal_leaves_the_next_park_empty(self):
        from fluid.events.model import SessionParked, TurnComplete
        from fluid.tools.registry import BackgroundTaskReceipt

        from app.core.provider.harness_adapter import HarnessAdapter

        engine = self._engine()
        await engine._background_task_started(
            BackgroundTaskReceipt(task_id="t-1", description="watch CI")
        )
        assert engine._park_pending_ids() == ("t-1",)

        adapter = HarnessAdapter()
        adapter._engine = engine
        adapter._connected = True

        # The mid-turn delivery path, exactly as the receipt handler drives it.
        with _pool_with(adapter):
            assert await turn_scheduler.retire_task_terminals(
                "sess-1", [{"task_id": "t-1", "status": "completed"}]
            ) is True

        assert engine._park_pending_ids() == ()
        assert engine.wakes == 0  # wake=False — the host owns wake admission

        engine.emitted.clear()
        await engine._emit_terminal_with_park(
            TurnComplete(
                session_id="sess-1", turn_id="turn-2",
                stop_reason="end_turn", num_model_calls=1,
            )
        )
        assert not [e for e in engine.emitted if isinstance(e, SessionParked)]


# ===========================================================================
# FIX 2a — a woken parent can still read the record
# ===========================================================================

class TestFinishedTaskStaysReachable:
    @pytest.fixture
    def reg(self):
        return TaskRegistry()

    def test_get_task_falls_back_to_the_retention_snapshot(self, reg):
        reg.start_task("s1", "w-1", "agent", "audit the retry path")
        reg.complete_task(
            "s1", "w-1", "completed",
            summary="Found it in retry.py", report_path="/tmp/w-1.md",
            report_chars=4200,
        )
        reg.clear_session("s1")  # the pooled client was evicted

        record = reg.get_task("s1", "w-1")
        assert record is not None
        assert record["status"] == "completed"
        # The report handles round-5 added must survive the fallback.
        assert record["reportPath"] == "/tmp/w-1.md"
        assert record["reportChars"] == 4200
        assert record["summary"] == "Found it in retry.py"

    def test_the_snapshot_is_a_copy(self, reg):
        reg.start_task("s1", "w-1", "agent", "x")
        reg.complete_task("s1", "w-1", "completed")
        reg.clear_session("s1")
        reg.get_task("s1", "w-1")["status"] = "mutated"
        assert reg.get_task("s1", "w-1")["status"] == "completed"

    def test_task_list_still_shows_the_finished_worker(self, reg):
        reg.start_task("s1", "w-1", "agent", "audit the retry path")
        reg.complete_task("s1", "w-1", "completed", summary="done")
        reg.clear_session("s1")
        listed = reg.get_all("s1")
        assert [t["id"] for t in listed] == ["w-1"]
        assert listed[0]["status"] == "completed"

    def test_a_live_record_wins_over_its_snapshot(self, reg):
        reg.start_task("s1", "w-1", "agent", "x")
        reg.complete_task("s1", "w-1", "completed")
        reg.update_task("s1", "w-1", summary="later truth")
        assert reg.get_task("s1", "w-1")["summary"] == "later truth"
        assert len(reg.get_all("s1")) == 1

    def test_the_hand_off_window_is_minutes_not_seconds(self):
        from app.core.task_registry import (
            _COMPLETED_VISIBLE_WINDOW_SECONDS,
            _RECENT_COMPLETED_WINDOW_SECONDS,
        )

        # The audited TaskGet came 35 seconds after the worker finished.
        assert _RECENT_COMPLETED_WINDOW_SECONDS >= 300
        assert _COMPLETED_VISIBLE_WINDOW_SECONDS < _RECENT_COMPLETED_WINDOW_SECONDS

    def test_durable_row_is_the_last_resort(self, reg, monkeypatch):
        from app.db import background_tasks as bt_db

        monkeypatch.setattr(
            bt_db, "get_background_task",
            lambda tid, sid: {
                "task_id": tid, "session_id": sid, "kind": "agent",
                "tool_use_id": tid, "title": "audit the retry path",
                "status": "completed", "started_at": "2026-09-04T10:00:00+00:00",
                "updated_at": "2026-09-04T10:09:00+00:00",
            },
        )
        assert reg.get_task("s1", "w-1") is None  # opt-in, not on every miss
        record = reg.get_task("s1", "w-1", durable=True)
        assert record["status"] == "completed"
        assert record["kind"] == "agent"
        assert record["startedAt"] == "2026-09-04T10:00:00+00:00"

    def test_a_durable_read_failure_is_just_a_miss(self, reg, monkeypatch):
        from app.db import background_tasks as bt_db

        def _boom(tid, sid):
            raise RuntimeError("no db")

        monkeypatch.setattr(bt_db, "get_background_task", _boom)
        assert reg.get_task("s1", "w-1", durable=True) is None


# ===========================================================================
# FIX 2b — the report is what the worker wrote AFTER its last tool
# ===========================================================================

class TestWorkerReportIsTheClosingStatement:
    @staticmethod
    def _events(*rows):
        return [
            {"seq": i, "turn_id": "t-1", "event_type": etype,
             "payload": payload}
            for i, (etype, payload) in enumerate(rows, start=1)
        ]

    def _log(self, monkeypatch, events):
        monkeypatch.setattr(
            provider_agent, "_load_session_events", lambda sid: events
        )

    def test_between_tool_narration_is_not_the_report(self, monkeypatch):
        self._log(monkeypatch, self._events(
            ("assistant.text_delta", {"text": "I'll start by mapping the sites."}),
            ("tool.use", {"name": "Grep"}),
            ("tool.result", {"content": "3 hits"}),
            ("assistant.text_delta", {"text": "Next I'll read the adapter."}),
            ("tool.use", {"name": "Read"}),
            ("tool.result", {"content": "..."}),
            ("assistant.text_delta", {"text": "PR #1084 is up. "}),
            ("assistant.text_delta", {"text": "Parking on CI."}),
            ("turn.completed", {"usage": {}}),
        ))
        assert provider_agent.final_assistant_text("w-1") == (
            "PR #1084 is up. Parking on CI."
        )

    def test_the_summary_follows_the_report(self, monkeypatch):
        self._log(monkeypatch, self._events(
            ("assistant.text_delta", {"text": "I'll start by mapping the sites."}),
            ("tool.result", {"content": "ok"}),
            ("assistant.text_delta", {"text": "Fixed the retry path in retry.py."}),
            ("turn.completed", {"usage": {}}),
        ))
        report = provider_agent.final_assistant_text("w-1")
        assert provider_agent._completion_summary(report) == (
            "Fixed the retry path in retry.py."
        )

    def test_a_turn_that_ends_on_a_tool_result_falls_back(self, monkeypatch):
        self._log(monkeypatch, self._events(
            ("assistant.text_delta", {"text": "Everything I have to say."}),
            ("tool.use", {"name": "Bash"}),
            ("tool.result", {"content": "done"}),
            ("turn.completed", {"usage": {}}),
        ))
        assert provider_agent.final_assistant_text("w-1") == (
            "Everything I have to say."
        )

    def test_a_worker_with_no_tools_is_unchanged(self, monkeypatch):
        self._log(monkeypatch, self._events(
            ("assistant.text_delta", {"text": "One "}),
            ("assistant.text_delta", {"text": "answer."}),
            ("turn.completed", {"usage": {}}),
        ))
        assert provider_agent.final_assistant_text("w-1") == "One answer."

    def test_an_earlier_turn_is_never_mixed_in(self, monkeypatch):
        events = [
            {"seq": 1, "turn_id": "t-0", "event_type": "assistant.text_delta",
             "payload": {"text": "old turn"}},
            {"seq": 2, "turn_id": "t-0", "event_type": "turn.completed",
             "payload": {}},
            {"seq": 3, "turn_id": "t-1", "event_type": "tool.result",
             "payload": {}},
            {"seq": 4, "turn_id": "t-1", "event_type": "assistant.text_delta",
             "payload": {"text": "new turn"}},
            {"seq": 5, "turn_id": "t-1", "event_type": "turn.completed",
             "payload": {}},
        ]
        self._log(monkeypatch, events)
        assert provider_agent.final_assistant_text("w-1") == "new turn"


# ===========================================================================
# FIX 2c — teardown actually reaches the parent
# ===========================================================================

class TestTeardownReachesTheParent:
    @pytest.mark.asyncio
    async def test_turn_end_teardown_notes_and_retires(self, mock_db):
        adapter = _RecordingAdapter()
        record = provider_agent_supervisor.register_worker(
            provider_agent_supervisor.WorkerRecord(
                parent_session_id="parent-1",
                worker_session_id="w-1",
                task_id="w-1",
                tool_use_id="w-1",
            )
        )
        task_registry.start_task("parent-1", "w-1", "agent", "ship the fix")

        notes: list = []
        with patch.object(
            provider_agent_supervisor, "_kill_worker_session",
            new=AsyncMock(return_value="killed"),
        ), patch.object(
            provider_agent_supervisor, "settle_killed_worker", new=AsyncMock(),
        ), patch.object(
            provider_agent_supervisor, "_cancel_worker_prompts", new=AsyncMock(),
        ), patch.object(
            provider_agent_supervisor, "_note_parent",
            new=AsyncMock(side_effect=lambda *a, **k: notes.append(a)),
        ), _pool_with(adapter):
            results = await provider_agent_supervisor.teardown_workers("parent-1")

        assert len(results) == 1
        assert record.torn_down is True
        # The coalesced transcript line still goes out...
        assert notes and "because the turn ended" in notes[0][1]
        # ...and so does the half the MODEL can read.
        assert adapter.submitted == [[{
            "task_id": "w-1",
            "status": "killed",
            "summary": provider_agent_supervisor.TURN_END_SUMMARY,
            "reason": provider_agent_supervisor.TURN_END_REASON,
        }]]

    @pytest.mark.asyncio
    async def test_a_dead_pool_never_fails_the_teardown(self, mock_db):
        provider_agent_supervisor.register_worker(
            provider_agent_supervisor.WorkerRecord(
                parent_session_id="parent-1",
                worker_session_id="w-1",
                task_id="w-1",
                tool_use_id="w-1",
            )
        )
        with patch.object(
            provider_agent_supervisor, "_kill_worker_session",
            new=AsyncMock(return_value="killed"),
        ), patch.object(
            provider_agent_supervisor, "settle_killed_worker", new=AsyncMock(),
        ), patch.object(
            provider_agent_supervisor, "_cancel_worker_prompts", new=AsyncMock(),
        ), patch.object(
            provider_agent_supervisor, "_note_parent", new=AsyncMock(),
        ), patch(
            "app.core.session_pool.get_session_pool",
            side_effect=RuntimeError("no pool"),
        ):
            results = await provider_agent_supervisor.teardown_workers("parent-1")
        assert len(results) == 1


# ===========================================================================
# FIX 2d — a worker parked on its own work has not finished
# ===========================================================================

class TestParkedWorkerIsNotComplete:
    def test_parked_with_live_tasks_is_still_working(self, mock_db):
        turns_db.enqueue_turn("w-1", "user", prompt="go", profile_id="p1")
        row = turns_db.get_latest_turn("w-1")
        turns_db.start_turn(row["id"], "run-1", "inst-1")
        turns_db.park_turn(row["id"], ["mon-1"])
        task_registry.start_task("w-1", "mon-1", "shell", "npm test")
        assert provider_agent._worker_still_working("w-1") is True

    def test_a_stranded_park_settles(self, mock_db):
        turns_db.enqueue_turn("w-1", "user", prompt="go", profile_id="p1")
        row = turns_db.get_latest_turn("w-1")
        turns_db.start_turn(row["id"], "run-1", "inst-1")
        turns_db.park_turn(row["id"], [])
        assert provider_agent._worker_still_working("w-1") is False

    def test_a_completed_turn_is_finished(self, mock_db):
        turns_db.enqueue_turn("w-1", "user", prompt="go", profile_id="p1")
        row = turns_db.get_latest_turn("w-1")
        turns_db.start_turn(row["id"], "run-1", "inst-1")
        turns_db.settle_turn(row["id"], "completed")
        task_registry.start_task("w-1", "mon-1", "shell", "npm test")
        assert provider_agent._worker_still_working("w-1") is False

    def test_a_wake_queued_since_the_idle_read_is_still_working(self, mock_db):
        # _await_worker_idle may have returned just before the Monitor's
        # scheduler handoff published this wake. Dispatch must wait again.
        turns_db.enqueue_turn("w-1", "unpark", prompt="tests finished", profile_id="p1")
        assert provider_agent._worker_still_working("w-1") is True

    def test_a_message_steered_in_before_the_park_does_not_hide_it(self, mock_db):
        # A message steered into the worker's running turn settles its own
        # queue row ``completed`` at once; that row is newer than the turn that
        # later parks, and reading it as "the latest turn" settled a worker
        # still waiting on its CI run.
        turns_db.enqueue_turn("w-1", "user", prompt="go", profile_id="p1")
        row = turns_db.get_latest_turn("w-1")
        turns_db.start_turn(row["id"], "run-1", "inst-1")
        # Older than the steered row for certain (the shim's clock is coarse).
        mock_db.execute("UPDATE turns SET created_at='2000-01-01' WHERE id=?", (row["id"],))
        steered = turns_db.enqueue_turn("w-1", "user", prompt="also this", profile_id="p1")
        assert turns_db.absorb_queued_turn(steered["id"]) is True
        turns_db.park_turn(row["id"], ["mon-1"])
        task_registry.start_task("w-1", "mon-1", "shell", "npm test")
        assert provider_agent._worker_still_working("w-1") is True

    def test_an_unreadable_session_settles(self, monkeypatch):
        monkeypatch.setattr(
            turns_db, "get_latest_run_turn",
            lambda sid: (_ for _ in ()).throw(RuntimeError("no db")),
        )
        assert provider_agent._worker_still_working("w-1") is False


# ===========================================================================
# FIX 3a — a deliberate cancellation retires without a wake
# ===========================================================================

class TestCancellationsDoNotWake:
    @pytest.mark.asyncio
    async def test_an_unattributed_kill_queues_a_wake(self, mock_db):
        _user_turn("sess-1")
        _seed_own_background_task("sess-1", "sh-1")
        task_registry.complete_task("sess-1", "sh-1", "killed")

        adapter = _RecordingAdapter()
        with _pool_with(adapter), patch.object(
            turn_scheduler, "_try_steer_terminal_notification",
            new=AsyncMock(return_value=False),
        ), patch.object(turn_scheduler, "kick"):
            await turn_scheduler.on_task_terminal("sess-1", {
                "task_id": "sh-1",
                "status": "cancelled",
                "tool_use_id": "sh-1",
                "kind": "shell",
                "summary": "Stopped deliberately by the app or by an agent.",
            })
            await _drain_kicks()

        assert [t for t in turns_db.get_queued_turns("sess-1")
                if t["source"] == "unpark"]
        # Retirement happens when the queued notification is consumed.
        assert not adapter.submitted
        assert task_registry.is_woken("sess-1", "sh-1") is True

    @pytest.mark.asyncio
    async def test_an_attributed_kill_still_wakes(self, mock_db):
        from app.core.task_registry import (
            SERVER_SHUTDOWN_REASON, SERVER_SHUTDOWN_SUMMARY,
        )

        _user_turn("sess-1")
        _seed_own_background_task("sess-1", "sh-1")
        task_registry.complete_task(
            "sess-1", "sh-1", "killed",
            summary=SERVER_SHUTDOWN_SUMMARY, reason=SERVER_SHUTDOWN_REASON,
        )

        with patch.object(
            turn_scheduler, "_try_steer_terminal_notification",
            new=AsyncMock(return_value=False),
        ), patch.object(turn_scheduler, "kick"):
            await turn_scheduler.on_task_terminal("sess-1", {
                "task_id": "sh-1",
                "status": "cancelled",
                "tool_use_id": "sh-1",
                "kind": "shell",
                "summary": SERVER_SHUTDOWN_SUMMARY,
            })
            await _drain_kicks()

        assert [t for t in turns_db.get_queued_turns("sess-1")
                if t["source"] == "unpark"]

    @pytest.mark.asyncio
    async def test_a_running_turn_gets_the_line_as_a_steer(self, mock_db):
        _user_turn("sess-1")
        _seed_own_background_task("sess-1", "sh-1")
        task_registry.complete_task("sess-1", "sh-1", "killed")

        steer = AsyncMock(return_value=True)
        with _pool_with(_RecordingAdapter()), patch.object(
            turn_scheduler, "_try_steer_terminal_notification", new=steer,
        ):
            await turn_scheduler.on_task_terminal("sess-1", {
                "task_id": "sh-1", "status": "cancelled",
                "tool_use_id": "sh-1", "kind": "shell",
            })
            await _drain_kicks()

        steer.assert_awaited_once()
        # Unexpected cancellation must be recoverable if the steer is stranded.
        assert steer.await_args.args[2][0]["task_id"] == "sh-1"


# ===========================================================================
# FIX 3b — near-simultaneous terminals batch into ONE wake
# ===========================================================================

class TestTerminalsBatch:
    @pytest.mark.asyncio
    async def test_three_cancellations_in_a_breath_make_one_wake(self, mock_db):
        _user_turn("sess-1")
        for tid in ("t-1", "t-2", "t-3"):
            _seed_own_background_task("sess-1", tid, kind="agent")

        with patch.object(
            turn_scheduler, "_try_steer_terminal_notification",
            new=AsyncMock(return_value=False),
        ), _pool_with(_RecordingAdapter()):
            for tid in ("t-1", "t-2", "t-3"):
                await turn_scheduler.on_task_terminal("sess-1", {
                    "task_id": tid, "status": "error",
                    "tool_use_id": tid, "kind": "agent",
                    "summary": f"{tid} blew up",
                })

            wakes = [t for t in turns_db.get_queued_turns("sess-1")
                     if t["source"] == "unpark"]
            assert len(wakes) == 1
            payload = wakes[0]["payload"] or {}
            assert sorted(payload["task_ids"]) == ["t-1", "t-2", "t-3"]
            assert len(payload["pending_frames"]) == 3

            # One batch window, one kick — not three.
            assert len(turn_scheduler._batch_kick_tasks) == 1
            await _drain_kicks()

    @pytest.mark.asyncio
    async def test_the_window_holds_the_kick_open(self, mock_db):
        _user_turn("sess-1")
        _seed_own_background_task("sess-1", "t-1", kind="agent")

        kicked: list = []
        with patch.object(
            turn_scheduler, "_try_steer_terminal_notification",
            new=AsyncMock(return_value=False),
        ), patch.object(
            turn_scheduler, "TERMINAL_BATCH_WINDOW_SECONDS", 0.05,
        ), patch.object(
            turn_scheduler, "kick", side_effect=kicked.append,
        ), _pool_with(_RecordingAdapter()):
            await turn_scheduler.on_task_terminal("sess-1", {
                "task_id": "t-1", "status": "error",
                "tool_use_id": "t-1", "kind": "agent", "summary": "boom",
            })
            assert kicked == []  # still inside the window
            await _drain_kicks()

        assert kicked == ["sess-1"]
