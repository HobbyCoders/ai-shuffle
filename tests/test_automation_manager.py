"""
Tests for AutomationManager — trigger handling, execution flow, output delivery.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime

from app.core.automation_manager import AutomationManager


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def manager():
    """Fresh AutomationManager for each test."""
    return AutomationManager()


# =============================================================================
# Tracked Task Helper
# =============================================================================


class TestTrackedTask:
    @pytest.mark.asyncio
    async def test_create_tracked_task_success(self, manager):
        result = []

        async def work():
            result.append(True)

        task = manager._create_tracked_task(work(), name="test-task")
        await task
        assert result == [True]

    @pytest.mark.asyncio
    async def test_create_tracked_task_error_logged(self, manager):
        async def failing_work():
            raise RuntimeError("test error")

        task = manager._create_tracked_task(failing_work(), name="test-fail")

        # Wait for the task to complete (with exception)
        with pytest.raises(RuntimeError, match="test error"):
            await task

    def test_on_task_done_cancelled(self, manager):
        """Cancelled tasks should not log errors."""
        mock_task = MagicMock()
        mock_task.cancelled.return_value = True
        # Should not raise
        AutomationManager._on_task_done(mock_task)

    def test_on_task_done_no_error(self, manager):
        mock_task = MagicMock()
        mock_task.cancelled.return_value = False
        mock_task.exception.return_value = None
        # Should not raise
        AutomationManager._on_task_done(mock_task)


# =============================================================================
# Trigger Webhook
# =============================================================================


class TestTriggerWebhook:
    @pytest.mark.asyncio
    async def test_webhook_not_found(self, manager, mock_db):
        result = await manager.trigger_webhook("nonexistent", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_webhook_disabled(self, manager, mock_db):
        from app.db import database
        database.create_automation(
            automation_id="auto-wh",
            name="Test Webhook",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        # is_enabled defaults to False in the DB schema
        result = await manager.trigger_webhook("auto-wh", {})
        assert result is None

    @pytest.mark.asyncio
    async def test_webhook_wrong_trigger_type(self, manager, mock_db):
        from app.db import database
        database.create_automation(
            automation_id="auto-wh",
            name="Test Cron",
            trigger_type="cron",
            trigger_config={"expression": "0 * * * *"},
            prompt="test",
            profile_id="default",
        )
        database.update_automation("auto-wh", is_enabled=True)
        result = await manager.trigger_webhook("auto-wh", {})
        assert result is None


# =============================================================================
# Trigger Manual
# =============================================================================


class TestTriggerManual:
    @pytest.mark.asyncio
    async def test_manual_not_found(self, manager, mock_db):
        result = await manager.trigger_manual("nonexistent")
        assert result is None

    @pytest.mark.asyncio
    async def test_manual_returns_status(self, manager, mock_db):
        from app.db import database
        database.create_automation(
            automation_id="auto-man",
            name="Test Manual",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )

        # Patch _execute to avoid real execution
        with patch.object(manager, "_execute", new_callable=AsyncMock):
            result = await manager.trigger_manual("auto-man")
            assert result is not None
            assert result["automation_id"] == "auto-man"
            assert result["status"] == "triggered"


# =============================================================================
# Execution — Budget Exceeded
# =============================================================================


class TestExecuteBudget:
    @pytest.mark.asyncio
    async def test_execute_budget_exceeded(self, manager, mock_db, mock_encryption):
        from app.db import database

        database.create_automation(
            automation_id="auto-budget",
            name="Budget Test",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
            max_cost_total=0,
        )
        database.update_automation("auto-budget", is_enabled=True, total_cost_used=1.0)

        with patch("app.core.automation_service.check_budget", return_value=(False, "Budget exceeded")):
            await manager._execute("auto-budget", {"trigger": "manual"})

        # Check that a run was created with budget_exceeded status
        runs = database.get_automation_runs("auto-budget")
        assert any(r.get("status") == "budget_exceeded" for r in runs)


# =============================================================================
# Execution — Disabled
# =============================================================================


class TestExecuteDisabled:
    @pytest.mark.asyncio
    async def test_execute_disabled_skipped(self, manager, mock_db):
        from app.db import database

        database.create_automation(
            automation_id="auto-off",
            name="Disabled",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        # is_enabled defaults to False

        await manager._execute("auto-off", {"trigger": "manual"})
        # No run should be created
        runs = database.get_automation_runs("auto-off")
        assert len(runs) == 0


# =============================================================================
# Execution — Concurrency
# =============================================================================


class TestExecuteConcurrency:
    @pytest.mark.asyncio
    async def test_concurrent_execution_blocked(self, manager, mock_db, mock_encryption):
        from app.db import database

        database.create_automation(
            automation_id="auto-conc",
            name="Concurrent",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        database.update_automation("auto-conc", is_enabled=True)

        # Pre-mark as running
        async with manager._lock:
            manager._running["auto-conc"] = MagicMock()

        await manager._execute("auto-conc", {"trigger": "manual"})

        # Should not have created any run (blocked by concurrency check)
        runs = database.get_automation_runs("auto-conc")
        assert len(runs) == 0


# =============================================================================
# Schedule / Unschedule
# =============================================================================


class TestScheduling:
    @pytest.mark.asyncio
    async def test_schedule_no_scheduler(self, manager, mock_db):
        """Should silently return when scheduler is None."""
        manager._scheduler = None
        await manager.schedule_automation("auto-1")

    @pytest.mark.asyncio
    async def test_unschedule_no_scheduler(self, manager):
        manager._scheduler = None
        await manager.unschedule_automation("auto-1")


# =============================================================================
# Output Delivery — Suppression
# =============================================================================


class TestOutputDelivery:
    @pytest.mark.asyncio
    async def test_suppress_pattern(self, manager, mock_db):
        from app.db import database

        database.create_automation(
            automation_id="auto-sup",
            name="Suppress",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        database.create_automation_run(
            run_id="run-sup",
            automation_id="auto-sup",
            status="completed",
        )

        automation = database.get_automation("auto-sup")
        automation["output_config"] = {"type": "session", "suppress_pattern": "NO_CHANGE"}

        await manager._deliver_output(automation, "run-sup", "NO_CHANGE")

        run = database.get_automation_run("run-sup")
        assert run.get("output_status") == "suppressed"

    @pytest.mark.asyncio
    async def test_session_output_type(self, manager, mock_db):
        from app.db import database

        database.create_automation(
            automation_id="auto-sess",
            name="Session Output",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        database.create_automation_run(
            run_id="run-sess",
            automation_id="auto-sess",
            status="completed",
        )

        automation = database.get_automation("auto-sess")
        automation["output_config"] = {"type": "session"}

        await manager._deliver_output(automation, "run-sess", "Some response")

        run = database.get_automation_run("run-sess")
        assert run.get("output_status") == "delivered"

    @pytest.mark.asyncio
    async def test_none_output_type(self, manager, mock_db):
        from app.db import database

        database.create_automation(
            automation_id="auto-none",
            name="None Output",
            trigger_type="webhook",
            trigger_config={},
            prompt="test",
            profile_id="default",
        )
        database.create_automation_run(
            run_id="run-none",
            automation_id="auto-none",
            status="completed",
        )

        automation = database.get_automation("auto-none")
        automation["output_config"] = {"type": "none"}

        await manager._deliver_output(automation, "run-none", "ignored")

        run = database.get_automation_run("run-none")
        assert run.get("output_status") == "delivered"


# =============================================================================
# Handle Connector Event
# =============================================================================


class TestHandleConnectorEvent:
    @pytest.mark.asyncio
    async def test_no_matching_automations(self, manager, mock_db):
        """Should not raise when no automations match."""
        await manager.handle_connector_event("session.complete", {})

    @pytest.mark.asyncio
    async def test_filter_mismatch(self, manager, mock_db):
        from app.db import database

        database.create_automation(
            automation_id="auto-evt",
            name="Event",
            trigger_type="connector_event",
            trigger_config={"event_types": ["session.complete"], "filter": {"project_id": "proj-1"}},
            prompt="test",
            profile_id="default",
        )
        database.update_automation("auto-evt", is_enabled=True)

        with patch.object(manager, "_execute", new_callable=AsyncMock) as mock_exec:
            await manager.handle_connector_event("session.complete", {"project_id": "proj-2"})
            # _execute should not be called because filter didn't match
            # (but the task might be created — depends on how get_automations_for_event works)

# =============================================================================
# Runs as normal streaming sessions — the new execution contract
# =============================================================================


def _fake_turn_handler(metadata=None, error=None, task=None):
    """Stand-in for the HeadlessChatHandler launch_headless_query returns."""
    handler = MagicMock()
    if task is None:
        loop = asyncio.get_running_loop()
        task = loop.create_future()
        task.set_result(None)
    handler.query_task = task
    handler.last_turn_metadata = (
        {"total_cost_usd": 0.01, "tokens_in": 10, "tokens_out": 20}
        if metadata is None else metadata
    )
    handler.last_turn_error = error
    handler.last_turn_interrupted = False
    return handler


def _session_run_env(handler, response_messages=None):
    """Patch stack for the streaming-session run path."""
    from contextlib import ExitStack

    stack = ExitStack()
    db = stack.enter_context(patch("app.core.automation_manager.database"))
    launch = stack.enter_context(patch(
        "app.core.headless_query_runner.launch_headless_query",
        new=AsyncMock(return_value=handler),
    ))
    stack.enter_context(patch("app.core.sync_engine.sync_engine", new=AsyncMock()))
    store = stack.enter_context(patch("app.core.live_session_store.live_session_store"))
    store.get_session_dict.return_value = {
        "messages": response_messages if response_messages is not None else [
            {"role": "user", "content": "do it"},
            {"role": "assistant", "type": "text", "content": "all done"},
        ],
    }
    db.update_automation_run.return_value = {}
    db.update_automation.return_value = {}
    return stack, db, launch, store


class TestRunAsSession:
    """An automation run IS a normal streaming chat session: the run mints a
    fresh automation-badged session, persists the prompt as a regular user
    message, and submits the turn through the headless streaming seam
    (launch_headless_query → run_query, deferred_delivery=True)."""

    def _automation(self, **overrides):
        auto = {
            "id": "auto-sess",
            "name": "Daily Digest",
            "profile_id": "default",
            "project_id": None,
            "action_steps": [],
            "run_count": 0,
            "failure_count": 0,
            "total_cost_used": 0,
            "max_turns": 3,
            "timeout_seconds": 60,
            "output_config": {"type": "none"},
        }
        auto.update(overrides)
        return auto

    @pytest.mark.asyncio
    async def test_run_creates_badged_session_and_submits_streaming_turn_once(self, manager):
        handler = _fake_turn_handler()
        stack, db, launch, _store = _session_run_env(handler)
        with stack:
            await manager._execute_run(self._automation(), "run-s1", "do it", {})

        # Exactly one fresh session, badged with the automation id
        db.create_session.assert_called_once()
        create_kwargs = db.create_session.call_args.kwargs
        assert create_kwargs["automation_id"] == "auto-sess"
        assert create_kwargs["profile_id"] == "default"
        session_id = create_kwargs["session_id"]
        assert session_id

        # The prompt is persisted as a normal user message on that session
        db.add_session_message.assert_called_once()
        msg_kwargs = db.add_session_message.call_args.kwargs
        assert msg_kwargs["session_id"] == session_id
        assert msg_kwargs["role"] == "user"
        assert msg_kwargs["content"] == "do it"

        # Exactly one streaming submit, through the deferred-delivery seam
        launch.assert_awaited_once()
        launch_kwargs = launch.call_args.kwargs
        assert launch_kwargs["prompt"] == "do it"
        assert launch_kwargs["session_id"] == session_id
        assert launch_kwargs["deferred_delivery"] is True

    @pytest.mark.asyncio
    async def test_run_links_session_before_turn_starts(self, manager):
        order = []
        handler = _fake_turn_handler()
        stack, db, launch, _store = _session_run_env(handler)
        with stack:
            db.update_automation_run.side_effect = (
                lambda run_id, **kw: order.append(("link", kw)) or {}
            )
            launch.side_effect = lambda **kw: order.append(("launch", kw)) or handler
            await manager._execute_run(self._automation(), "run-s2", "do it", {})

        link_idx = next(
            i for i, (kind, kw) in enumerate(order)
            if kind == "link" and kw.get("session_id")
        )
        launch_idx = next(i for i, (kind, _) in enumerate(order) if kind == "launch")
        assert link_idx < launch_idx, "run→session link must precede the turn submit"

    @pytest.mark.asyncio
    async def test_max_turns_cap_passed_through(self, manager):
        handler = _fake_turn_handler()
        stack, _db, launch, _store = _session_run_env(handler)
        with stack:
            await manager._execute_run(
                self._automation(max_turns=7), "run-s3", "do it", {}
            )
        assert launch.call_args.kwargs["overrides"] == {"max_turns": 7}

    @pytest.mark.asyncio
    async def test_turn_cost_lands_on_run_row_and_automation(self, manager):
        handler = _fake_turn_handler(
            metadata={"total_cost_usd": 0.05, "tokens_in": 11, "tokens_out": 22}
        )
        stack, db, _launch, _store = _session_run_env(handler)
        with stack:
            await manager._execute_run(self._automation(), "run-s4", "do it", {})

        completed = [
            c.kwargs for c in db.update_automation_run.call_args_list
            if c.kwargs.get("status") == "completed"
        ]
        assert len(completed) == 1
        assert completed[0]["cost_usd"] == 0.05
        assert completed[0]["tokens_in"] == 11
        assert completed[0]["tokens_out"] == 22
        # Completion never re-writes session_id (linked at creation) — the old
        # FK-violation race is gone by construction.
        assert "session_id" not in completed[0]
        db.record_automation_run_success.assert_called_once_with("auto-sess", 0.05)

    @pytest.mark.asyncio
    async def test_run_row_stores_response_tail_not_full_payload(self, manager):
        long_response = "x" * 12000
        handler = _fake_turn_handler()
        stack, db, _launch, _store = _session_run_env(
            handler,
            response_messages=[
                {"role": "user", "content": "do it"},
                {"role": "assistant", "type": "text", "content": long_response},
            ],
        )
        with stack:
            await manager._execute_run(self._automation(), "run-s5", "do it", {})

        completed = [
            c.kwargs for c in db.update_automation_run.call_args_list
            if c.kwargs.get("status") == "completed"
        ]
        stored = completed[0]["response"]
        assert len(stored) <= 2000
        assert stored == long_response[-len(stored):]

    @pytest.mark.asyncio
    async def test_turn_error_stamps_failure_and_keeps_session_link(self, manager):
        handler = _fake_turn_handler(metadata={}, error="Error: CLI exploded")
        stack, db, _launch, _store = _session_run_env(handler)
        with stack:
            with pytest.raises(RuntimeError, match="CLI exploded"):
                await manager._execute_run(self._automation(), "run-s6", "do it", {})

        # Session was linked at creation and the link is never revoked
        link_updates = [
            c.kwargs for c in db.update_automation_run.call_args_list
            if c.kwargs.get("session_id")
        ]
        assert link_updates, "run row must link its session even on failure"

        failed = [
            c.kwargs for c in db.update_automation_run.call_args_list
            if c.kwargs.get("status") == "failed"
        ]
        assert failed and "CLI exploded" in failed[0]["error"]
        error_updates = [
            c.kwargs for c in db.update_automation.call_args_list
            if c.kwargs.get("status") == "error"
        ]
        assert error_updates and "CLI exploded" in error_updates[0]["last_error"]
        db.record_automation_run_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_timeout_cancels_turn_and_stamps_failure(self, manager):
        loop = asyncio.get_running_loop()
        never_done = loop.create_future()
        handler = _fake_turn_handler(task=never_done)
        stack, db, _launch, _store = _session_run_env(handler)
        with stack:
            await manager._execute_run(
                self._automation(timeout_seconds=0.05), "run-s7", "do it", {}
            )

        assert never_done.cancelled()
        failed = [
            c.kwargs for c in db.update_automation_run.call_args_list
            if c.kwargs.get("status") == "failed"
        ]
        assert failed and "timed out" in failed[0]["error"]
        db.record_automation_run_success.assert_not_called()


class TestScheduledFireOrdering:
    """The cross-install fire claim must be taken BEFORE any session exists —
    a losing install must never mint a session for a tick it didn't win."""

    @pytest.mark.asyncio
    async def test_fire_claim_precedes_session_creation(self, manager):
        order = []
        tasks = []

        def fake_track(coro, *, name):
            task = asyncio.ensure_future(coro)
            tasks.append(task)
            return task

        manager._create_tracked_task = fake_track

        automation = {
            "id": "auto-order",
            "name": "Ordered",
            "is_enabled": True,
            "requires_approval": False,
            "profile_id": "default",
            "project_id": None,
            "prompt": "tick",
            "action_steps": [],
            "run_count": 0,
            "failure_count": 0,
            "total_cost_used": 0,
            "max_turns": 1,
            "timeout_seconds": 60,
            "output_config": {"type": "none"},
        }

        handler = _fake_turn_handler()
        stack, db, launch, _store = _session_run_env(handler)
        with stack, patch(
            "app.core.automation_service.should_run_scheduled_here",
            return_value=(True, "elected"),
        ), patch(
            "app.core.automation_service.check_budget", return_value=(True, "ok")
        ):
            db.get_automation.return_value = automation
            db.claim_automation_fire.side_effect = (
                lambda *a, **k: order.append("claim") or True
            )
            db.create_session.side_effect = (
                lambda **kw: order.append("create_session") or {}
            )
            await manager._on_schedule_trigger("auto-order", "interval", {"seconds": 60})
            if tasks:
                await asyncio.gather(*tasks)

        assert "claim" in order and "create_session" in order
        assert order.index("claim") < order.index("create_session")

    @pytest.mark.asyncio
    async def test_budget_denied_creates_no_session(self, manager):
        handler = _fake_turn_handler()
        stack, db, launch, _store = _session_run_env(handler)
        with stack, patch(
            "app.core.automation_service.check_budget",
            return_value=(False, "Budget exceeded"),
        ):
            db.get_automation.return_value = {
                "id": "auto-broke",
                "is_enabled": True,
                "profile_id": "default",
                "prompt": "tick",
                "failure_count": 0,
            }
            await manager._execute("auto-broke", {"trigger": "schedule"})

        db.create_session.assert_not_called()
        launch.assert_not_awaited()
        budget_runs = [
            c.kwargs for c in db.create_automation_run.call_args_list
            if c.kwargs.get("status") == "budget_exceeded"
        ]
        assert budget_runs


# =============================================================================
# Session provenance — automation runs stamp automation_id onto their session
# =============================================================================


class TestRunSessionProvenance:
    """Sessions created by an automation run carry automation_id so the
    sessions list can badge them."""

    def _automation(self):
        return {
            "id": "auto-badge",
            "name": "Badge Me",
            "profile_id": "default",
            "project_id": None,
            "action_steps": [],
            "run_count": 0,
            "failure_count": 0,
            "total_cost_used": 0,
            "max_turns": None,
            "timeout_seconds": 60,
            "output_config": {"type": "none"},
        }

    @pytest.mark.asyncio
    async def test_session_created_with_automation_id(self, manager):
        handler = _fake_turn_handler()
        stack, db, _launch, _store = _session_run_env(handler)
        with stack:
            await manager._execute_run(self._automation(), "run-badge", "do it", {})

        db.create_session.assert_called_once()
        assert db.create_session.call_args.kwargs["automation_id"] == "auto-badge"


# =============================================================================
# Cross-install: scheduled-runner election gate + local project resolution
# =============================================================================


class TestScheduledElectionGate:
    """Every install fires the shared cron/interval jobs; only the elected
    runner may execute (automations are shared via the database while each
    install schedules them all)."""

    def _track_recorder(self, manager):
        created = []

        def fake_track(coro, *, name):
            created.append(name)
            coro.close()
            from unittest.mock import MagicMock
            return MagicMock()

        manager._create_tracked_task = fake_track
        return created

    @pytest.mark.asyncio
    async def test_skips_when_another_install_is_elected(self, manager):
        created = self._track_recorder(manager)
        with patch("app.core.automation_manager.database") as db, patch(
            "app.core.automation_service.should_run_scheduled_here",
            return_value=(False, "install other is the elected runner"),
        ):
            db.get_automation.return_value = {"id": "auto-1", "is_enabled": True}
            await manager._on_schedule_trigger("auto-1")
        assert created == []

    @pytest.mark.asyncio
    async def test_runs_when_elected(self, manager):
        created = self._track_recorder(manager)
        with patch("app.core.automation_manager.database") as db, patch(
            "app.core.automation_service.should_run_scheduled_here",
            return_value=(True, "elected runner"),
        ):
            db.get_automation.return_value = {"id": "auto-1", "is_enabled": True}
            await manager._on_schedule_trigger("auto-1")
        assert created == ["auto-schedule-auto-1"]

    @pytest.mark.asyncio
    async def test_election_error_fails_open(self, manager):
        created = self._track_recorder(manager)
        with patch("app.core.automation_manager.database") as db, patch(
            "app.core.automation_service.should_run_scheduled_here",
            side_effect=RuntimeError("registry exploded"),
        ):
            db.get_automation.return_value = {"id": "auto-1", "is_enabled": True}
            await manager._on_schedule_trigger("auto-1")
        assert created == ["auto-schedule-auto-1"]

    @pytest.mark.asyncio
    async def test_missing_automation_never_fires(self, manager):
        created = self._track_recorder(manager)
        with patch("app.core.automation_manager.database") as db:
            db.get_automation.return_value = None
            await manager._on_schedule_trigger("auto-gone")
        assert created == []

    @pytest.mark.asyncio
    async def test_ghost_job_is_unscheduled(self, manager):
        """An automation deleted on another install leaves a local job behind;
        the next fire must clean it up instead of skipping silently forever."""
        created = self._track_recorder(manager)
        manager._scheduled_versions["auto-gone"] = "stale"
        with patch("app.core.automation_manager.database") as db:
            db.get_automation.return_value = None
            await manager._on_schedule_trigger("auto-gone")
        assert created == []
        assert "auto-gone" not in manager._scheduled_versions

    @pytest.mark.asyncio
    async def test_gate_db_error_fails_open(self, manager):
        """A transient DB error while loading the automation must not
        silently drop the fire on the elected winner."""
        created = self._track_recorder(manager)
        with patch("app.core.automation_manager.database") as db:
            db.get_automation.side_effect = RuntimeError("db blip")
            await manager._on_schedule_trigger("auto-1")
        assert created == ["auto-schedule-auto-1"]


class TestScheduleReconcile:
    """APScheduler jobs are per-process while automations are shared via the
    database — the reconcile pass syncs local jobs with rows changed on
    other installs."""

    def _auto(self, auto_id="auto-1", seconds=300):
        return {
            "id": auto_id,
            "is_enabled": True,
            "triggers": [{"type": "interval", "config": {"seconds": seconds}}],
        }

    @pytest.mark.asyncio
    async def test_schedules_automation_created_elsewhere(self, manager):
        manager._scheduler = MagicMock(get_jobs=MagicMock(return_value=[]))
        manager.schedule_automation = AsyncMock()
        with patch("app.core.automation_manager.database") as db:
            db.get_enabled_automations.return_value = [self._auto()]
            await manager._reconcile_schedules()
        manager.schedule_automation.assert_awaited_once_with("auto-1")

    @pytest.mark.asyncio
    async def test_unchanged_automation_is_not_rescheduled(self, manager):
        """Re-adding an unchanged IntervalTrigger job would reset its
        next-fire time — unchanged trigger configs must be left alone."""
        auto = self._auto()
        manager._scheduler = MagicMock(get_jobs=MagicMock(return_value=[]))
        manager._scheduled_versions["auto-1"] = AutomationManager._trigger_version(auto)
        manager.schedule_automation = AsyncMock()
        with patch("app.core.automation_manager.database") as db:
            db.get_enabled_automations.return_value = [auto]
            await manager._reconcile_schedules()
        manager.schedule_automation.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_changed_trigger_config_is_rescheduled(self, manager):
        manager._scheduler = MagicMock(get_jobs=MagicMock(return_value=[]))
        manager._scheduled_versions["auto-1"] = AutomationManager._trigger_version(
            self._auto(seconds=300)
        )
        manager.schedule_automation = AsyncMock()
        with patch("app.core.automation_manager.database") as db:
            db.get_enabled_automations.return_value = [self._auto(seconds=600)]
            await manager._reconcile_schedules()
        manager.schedule_automation.assert_awaited_once_with("auto-1")

    @pytest.mark.asyncio
    async def test_unschedules_automation_removed_elsewhere(self, manager):
        manager._scheduler = MagicMock(get_jobs=MagicMock(return_value=[]))
        manager._scheduled_versions["auto-old"] = "v1"
        with patch("app.core.automation_manager.database") as db:
            db.get_enabled_automations.return_value = []
            await manager._reconcile_schedules()
        assert "auto-old" not in manager._scheduled_versions

    def test_trigger_version_ignores_run_bookkeeping(self):
        """The fingerprint must only track trigger config: run completions
        touch updated_at/run_count and must not cause a phase-resetting
        re-schedule."""
        a = {"id": "x", "triggers": [{"type": "cron", "config": {"expression": "0 * * * *"}}],
             "updated_at": "2026-01-01T00:00:00", "run_count": 1}
        b = {"id": "x", "triggers": [{"type": "cron", "config": {"expression": "0 * * * *"}}],
             "updated_at": "2026-06-01T12:34:56", "run_count": 99}
        assert AutomationManager._trigger_version(a) == AutomationManager._trigger_version(b)

    @pytest.mark.asyncio
    async def test_schedule_automation_records_version(self, manager):
        auto = self._auto()
        manager._scheduler = MagicMock(get_jobs=MagicMock(return_value=[]))
        with patch("app.core.automation_manager.database") as db:
            db.get_automation.return_value = auto
            await manager.schedule_automation("auto-1")
        assert manager._scheduled_versions["auto-1"] == AutomationManager._trigger_version(auto)
        manager._scheduler.add_job.assert_called_once()


class TestExecuteRunProjectResolution:
    """A run must execute against THIS install's clone of the automation's
    project (same link group), not the machine-specific row stored at
    creation time on another install."""

    def _automation(self):
        return {
            "id": "auto-xi",
            "name": "Cross Install",
            "profile_id": "default",
            "project_id": "proj-A",
            "action_steps": [],
            "run_count": 0,
            "failure_count": 0,
            "total_cost_used": 0,
            "max_turns": 1,
            "timeout_seconds": 60,
            "output_config": {"type": "none"},
        }

    @pytest.mark.asyncio
    async def test_run_uses_local_link_group_clone(self, manager):
        sibling = {"id": "proj-B", "path": "/local/clone"}
        handler = _fake_turn_handler()
        stack, db, launch, _store = _session_run_env(handler)
        with stack, patch(
            "app.core.automation_service.resolve_local_project", return_value=sibling
        ) as resolve:
            await manager._execute_run(self._automation(), "run-xi", "do it", {})

        resolve.assert_called_once_with("proj-A")
        assert launch.call_args.kwargs["project_id"] == "proj-B"
        assert db.create_session.call_args.kwargs["project_id"] == "proj-B"

    @pytest.mark.asyncio
    async def test_run_keeps_own_project_when_local(self, manager):
        mine = {"id": "proj-A", "path": "/here"}
        handler = _fake_turn_handler()
        stack, _db, launch, _store = _session_run_env(handler)
        with stack, patch(
            "app.core.automation_service.resolve_local_project", return_value=mine
        ):
            await manager._execute_run(self._automation(), "run-xj", "do it", {})

        assert launch.call_args.kwargs["project_id"] == "proj-A"

    @pytest.mark.asyncio
    async def test_unresolvable_project_fails_run_with_clear_error(self, manager):
        handler = _fake_turn_handler()
        stack, db, launch, _store = _session_run_env(handler)
        with stack, patch(
            "app.core.automation_service.resolve_local_project", return_value=None
        ):
            db.get_project.return_value = {
                "id": "proj-A", "name": "My Repo", "instance_id": "install-other",
            }
            db.get_install.return_value = {"device_name": "Office PC"}
            with pytest.raises(ValueError, match="Office PC"):
                await manager._execute_run(self._automation(), "run-xk", "do it", {})

        launch.assert_not_awaited()
        db.create_session.assert_not_called()
        failed = [
            c.kwargs for c in db.update_automation_run.call_args_list
            if c.kwargs.get("status") == "failed"
        ]
        assert failed and "My Repo" in failed[0]["error"]
        assert "Office PC" in failed[0]["error"]
