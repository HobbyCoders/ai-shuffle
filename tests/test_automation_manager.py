"""
Tests for AutomationManager — trigger handling, execution flow, output delivery.
"""

from tests.conftest import async_twin
import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from datetime import datetime, timedelta, timezone

from app.core.automation_manager import AutomationManager, crontab_day_of_week


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

        with patch.object(manager, "_execute", new_callable=AsyncMock):
            await manager.handle_connector_event("session.complete", {"project_id": "proj-2"})
            # _execute should not be called because filter didn't match
            # (but the task might be created — depends on how get_automations_for_event works)

# =============================================================================
# Runs as normal streaming sessions — the new execution contract
# =============================================================================


def _fake_event_log(text, turn_id="t1", *, usage=None,
                    terminal="completed", reason=None, message=None):
    """A minimal v2 ``session_events`` log: one turn whose assistant text is
    ``text``, split across two ``assistant.text_delta`` events so the
    concatenation path is exercised, ending in a terminal turn event.

    ``terminal='completed'`` carries ``payload.usage`` (the run cost/tokens the
    automation derives from ``_final_turn_outcome``); ``terminal='aborted'``
    carries ``payload.reason``/``payload.message`` (the error/interrupt path)."""
    mid = len(text) // 2
    events = [
        {"seq": 1, "turn_id": turn_id, "event_type": "turn.started", "payload": {}},
        {"seq": 2, "turn_id": turn_id, "event_type": "message.user",
         "payload": {"content": "do it"}},
        {"seq": 3, "turn_id": turn_id, "event_type": "assistant.text_delta",
         "payload": {"text": text[:mid]}},
        {"seq": 4, "turn_id": turn_id, "event_type": "assistant.text_delta",
         "payload": {"text": text[mid:]}},
    ]
    if terminal == "aborted":
        events.append({
            "seq": 5, "turn_id": turn_id, "event_type": "turn.aborted",
            "payload": {"reason": reason, "message": message},
        })
    else:
        events.append({
            "seq": 5, "turn_id": turn_id, "event_type": "turn.completed",
            "payload": {"usage": usage or {}},
        })
    return events


def _session_run_env(*, log=None, idle=True):
    """Patch stack for the V2 streaming-session run path.

    The run submits a durable ``source=automation`` turn via
    ``turn_manager.submit`` and blocks on ``turn_manager.wait_session_idle``;
    the outcome (cost/tokens/error/interrupt) and response text are BOTH derived
    from the durable ``session_events`` log, so patching ``get_events`` drives
    both. ``idle=False`` simulates the automation timeout (the session never
    drains within the deadline) — the run then interrupts the wedged turn.

    Returns ``(stack, db, submit, wait_idle, interrupt)``.
    """
    from contextlib import ExitStack

    stack = ExitStack()
    db = stack.enter_context(patch("app.core.automation_manager.database"))
    submit = stack.enter_context(patch(
        "app.core.turn_manager.submit", MagicMock(return_value="turn-x"),
    ))
    stack.enter_context(patch(
        "app.core.turn_manager.submit_async", new=async_twin(submit),
    ))
    wait_idle = stack.enter_context(patch(
        "app.core.turn_manager.wait_session_idle",
        new=AsyncMock(return_value=idle),
    ))
    interrupt = stack.enter_context(patch(
        "app.core.turn_manager.interrupt", MagicMock(return_value={"id": "turn-x"}),
    ))
    stack.enter_context(patch(
        "app.core.turn_manager.interrupt_async", new=async_twin(interrupt),
    ))
    stack.enter_context(patch("app.core.sync_engine.sync_engine", new=AsyncMock()))
    stack.enter_context(patch(
        "app.db.session_events.get_events",
        return_value=log if log is not None else _fake_event_log("all done"),
    ))
    db.update_automation_run.return_value = {}
    db.update_automation.return_value = {}
    return stack, db, submit, wait_idle, interrupt


class TestRunAsSession:
    """An automation run IS a normal streaming chat session: the run mints a
    fresh automation-badged session, then submits a durable ``source=automation``
    turn through ``turn_manager.submit`` (the ONE spawn path via SessionRuntime)
    and blocks on ``wait_session_idle``. Cost/tokens/response are derived from
    the durable session-event log."""

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
        stack, db, submit, _wait, _int = _session_run_env()
        with stack:
            await manager._execute_run(self._automation(), "run-s1", "do it", {})

        # Exactly one fresh session, badged with the automation id
        db.create_session.assert_called_once()
        create_kwargs = db.create_session.call_args.kwargs
        assert create_kwargs["automation_id"] == "auto-sess"
        assert create_kwargs["profile_id"] == "default"
        session_id = create_kwargs["session_id"]
        assert session_id

        # V2: the prompt is NOT pre-seeded into legacy session_messages — the
        # turn recorder persists it as a message.user event at turn start.
        db.add_session_message.assert_not_called()

        # Exactly one server-originated automation turn submitted for this session
        submit.assert_called_once()
        assert submit.call_args.args[0] == session_id
        assert submit.call_args.args[1] == "automation"
        assert submit.call_args.kwargs["prompt"] == "do it"

    @pytest.mark.asyncio
    async def test_run_links_session_before_turn_starts(self, manager):
        order = []
        stack, db, submit, _wait, _int = _session_run_env()
        with stack:
            db.update_automation_run.side_effect = (
                lambda run_id, **kw: order.append(("link", kw)) or {}
            )
            submit.side_effect = lambda *a, **kw: order.append(("submit", a)) or "turn-x"
            await manager._execute_run(self._automation(), "run-s2", "do it", {})

        link_idx = next(
            i for i, (kind, kw) in enumerate(order)
            if kind == "link" and kw.get("session_id")
        )
        submit_idx = next(i for i, (kind, _) in enumerate(order) if kind == "submit")
        assert link_idx < submit_idx, "run→session link must precede the turn submit"

    @pytest.mark.asyncio
    async def test_max_turns_cap_passed_through(self, manager):
        stack, _db, submit, _wait, _int = _session_run_env()
        with stack:
            await manager._execute_run(
                self._automation(max_turns=7), "run-s3", "do it", {}
            )
        assert submit.call_args.kwargs["overrides"] == {"max_turns": 7}

    @pytest.mark.asyncio
    async def test_turn_cost_lands_on_run_row_and_automation(self, manager):
        log = _fake_event_log(
            "all done",
            usage={"total_cost_usd": 0.05, "tokens_in": 11, "tokens_out": 22},
        )
        stack, db, _submit, _wait, _int = _session_run_env(log=log)
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
        stack, db, _submit, _wait, _int = _session_run_env(
            log=_fake_event_log(long_response),
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
        # An errored turn settles ``aborted`` with a non-interrupt reason and a
        # message; ``_final_turn_outcome`` surfaces that as the run error.
        log = _fake_event_log(
            "", terminal="aborted", reason="error", message="Error: CLI exploded",
        )
        stack, db, _submit, _wait, _int = _session_run_env(log=log)
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
    async def test_timeout_interrupts_turn_and_stamps_failure(self, manager):
        # The session never drains within the deadline (wait_session_idle False):
        # the run interrupts the wedged turn and stamps the timeout.
        stack, db, _submit, _wait, interrupt = _session_run_env(idle=False)
        with stack:
            await manager._execute_run(
                self._automation(timeout_seconds=0.05), "run-s7", "do it", {}
            )

        interrupt.assert_called_once_with(interrupt.call_args.args[0], "stop")
        failed = [
            c.kwargs for c in db.update_automation_run.call_args_list
            if c.kwargs.get("status") == "failed"
        ]
        assert failed and "timed out" in failed[0]["error"]
        db.record_automation_run_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_unterminated_log_fails_run_instead_of_empty_success(self, manager):
        """Regression for the 2026-08-28 race: the wait returned while the turn
        was still running, so the event log had NO terminal turn event yet —
        and ``_final_turn_outcome`` shaped that exactly like a clean empty
        completion, stamping the run "completed" with an empty response, 0
        tokens and $0 while the real turn kept streaming. A log without a
        terminal event for the submitted turn must fail the run, never
        succeed it silently."""
        # A turn that has started and streamed text but has not settled: the
        # fake log minus its trailing terminal event.
        log = _fake_event_log("half an answer")[:-1]
        assert not any(
            ev["event_type"].startswith("turn.")
            and ev["event_type"] != "turn.started"
            for ev in log
        )
        stack, db, _submit, _wait, _int = _session_run_env(log=log)
        with stack:
            with pytest.raises(RuntimeError, match="no terminal event"):
                await manager._execute_run(self._automation(), "run-s9", "do it", {})

        completed = [
            c.kwargs for c in db.update_automation_run.call_args_list
            if c.kwargs.get("status") == "completed"
        ]
        assert not completed, "an unterminated log must never stamp completed"
        failed = [
            c.kwargs for c in db.update_automation_run.call_args_list
            if c.kwargs.get("status") == "failed"
        ]
        assert failed and "no terminal event" in failed[0]["error"]
        db.record_automation_run_success.assert_not_called()

    @pytest.mark.asyncio
    async def test_handoff_successor_outcome_wins_via_last_turn(self, manager):
        """A hand-off (failover retry / auto-continue) is enqueued by the
        scheduler as a NEW turn; ``wait_session_idle`` only returns once the
        whole queue drains, and the outcome is read from the LAST terminal turn.
        So the run is stamped from the SUCCESSOR's cost, not the first
        attempt's (#560 follow-up 1, re-expressed durably)."""
        log = (
            _fake_event_log(
                "first", turn_id="t1",
                usage={"total_cost_usd": 0.02, "tokens_in": 1, "tokens_out": 1},
            )
            + _fake_event_log(
                "second", turn_id="t2",
                usage={"total_cost_usd": 0.07, "tokens_in": 5, "tokens_out": 6},
            )
        )
        for i, ev in enumerate(log, start=1):
            ev["seq"] = i
        stack, db, _submit, _wait, _int = _session_run_env(log=log)
        with stack:
            await manager._execute_run(self._automation(), "run-s8", "do it", {})

        completed = [
            c.kwargs for c in db.update_automation_run.call_args_list
            if c.kwargs.get("status") == "completed"
        ]
        assert len(completed) == 1
        assert completed[0]["cost_usd"] == 0.07
        db.record_automation_run_success.assert_called_once_with("auto-sess", 0.07)

    @pytest.mark.asyncio
    async def test_interrupt_stamps_interruption_not_timeout(self, manager):
        """A genuine interrupt settles the turn ``aborted`` with reason
        ``interrupt`` and the session drains cleanly (wait returns True) — the
        run is stamped as an interruption, never a timeout."""
        log = _fake_event_log("", terminal="aborted", reason="interrupt")
        stack, db, _submit, _wait, _int = _session_run_env(log=log)
        with stack:
            with pytest.raises(RuntimeError, match="interrupted"):
                await manager._execute_run(
                    self._automation(timeout_seconds=60), "run-s10", "do it", {}
                )

        failed = [
            c.kwargs for c in db.update_automation_run.call_args_list
            if c.kwargs.get("status") == "failed"
        ]
        assert failed and "interrupted" in failed[0]["error"]
        assert "timed out" not in failed[0]["error"]
        db.record_automation_run_success.assert_not_called()


class TestExtractFinalAssistantText:
    """The run response is derived from the V2 session event log — the last
    completed turn's assistant.text_delta payloads, concatenated in seq order."""

    def test_concatenates_last_turn_text_deltas(self):
        events = _fake_event_log("hello world")
        with patch("app.db.session_events.get_events", return_value=events):
            out = AutomationManager._extract_final_assistant_text("sess-1")
        assert out == "hello world"

    def test_picks_last_completed_turn(self):
        events = (
            _fake_event_log("first turn", turn_id="t1")
            + _fake_event_log("second turn", turn_id="t2")
        )
        # Reallocate seqs so they are ascending across the two turns.
        for i, ev in enumerate(events, start=1):
            ev["seq"] = i
        with patch("app.db.session_events.get_events", return_value=events):
            out = AutomationManager._extract_final_assistant_text("sess-2")
        assert out == "second turn"

    def test_empty_log_returns_empty_string(self):
        with patch("app.db.session_events.get_events", return_value=[]):
            out = AutomationManager._extract_final_assistant_text("sess-3")
        assert out == ""

    def test_reader_error_is_swallowed(self):
        with patch("app.db.session_events.get_events", side_effect=RuntimeError("boom")):
            out = AutomationManager._extract_final_assistant_text("sess-4")
        assert out == ""


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

        stack, db, _submit, _wait, _int = _session_run_env()
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
        stack, db, submit, _wait, _int = _session_run_env()
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
        submit.assert_not_called()
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
        stack, db, _submit, _wait, _int = _session_run_env()
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


class TestCronDayOfWeek:
    """Cron day-of-week follows crontab semantics (0 and 7 = Sunday), not
    APScheduler's Monday=0 numbering."""

    async def _fire_days(self, manager, expression, start):
        """Weekday names of the next 7 fire times of a scheduled cron trigger."""
        auto = {
            "id": "auto-1",
            "is_enabled": True,
            "triggers": [{"type": "cron", "config": {"expression": expression, "timezone": "UTC"}}],
        }
        manager._scheduler = MagicMock(get_jobs=MagicMock(return_value=[]))
        with patch("app.core.automation_manager.database") as db:
            db.get_automation.return_value = auto
            await manager.schedule_automation("auto-1")
        trigger = manager._scheduler.add_job.call_args.args[1]
        days, prev, now = [], None, start
        for _ in range(7):
            nxt = trigger.get_next_fire_time(prev, now)
            days.append(nxt.strftime("%a"))
            prev, now = nxt, nxt + timedelta(minutes=1)
        return days

    @pytest.mark.asyncio
    @pytest.mark.parametrize("expression,expected", [
        ("0 9 * * 1-5", ["Mon", "Tue", "Wed", "Thu", "Fri", "Mon", "Tue"]),
        ("0 9 * * 0", ["Sun"] * 7),
        ("0 9 * * 7", ["Sun"] * 7),
        ("0 9 * * 5", ["Fri"] * 7),
        ("0 9 * * 0,6", ["Sat", "Sun", "Sat", "Sun", "Sat", "Sun", "Sat"]),
        ("0 9 * * mon-fri", ["Mon", "Tue", "Wed", "Thu", "Fri", "Mon", "Tue"]),
        ("0 9 * * sun-thu", ["Sun", "Mon", "Tue", "Wed", "Thu", "Sun", "Mon"]),
        ("0 9 * * fri-mon", ["Sat", "Sun", "Mon", "Fri", "Sat", "Sun", "Mon"]),
    ])
    async def test_weekday_numbering_is_crontab(self, manager, expression, expected):
        # 2026-09-26 is a Saturday.
        start = datetime(2026, 9, 26, 0, 0, tzinfo=timezone.utc)
        assert await self._fire_days(manager, expression, start) == expected

    @pytest.mark.parametrize("field,expected", [
        ("*", "*"),
        ("1-5", "mon,tue,wed,thu,fri"),
        ("0", "sun"),
        ("7", "sun"),
        ("5-7", "fri,sat,sun"),
        ("*/2", "sun,tue,thu,sat"),
        ("1-5/2", "mon,wed,fri"),
        ("mon-fri", "mon,tue,wed,thu,fri"),
        ("sun-thu", "sun,mon,tue,wed,thu"),
        ("Sun-Tue", "sun,mon,tue"),
        ("sat-sun", "sat,sun"),
        ("fri-mon", "fri,sat,sun,mon"),
        ("sun-sat/2", "sun,tue,thu,sat"),
        ("mon", "mon"),
        ("sat,0", "sat,sun"),
    ])
    def test_crontab_day_of_week_translation(self, field, expected):
        assert crontab_day_of_week(field) == expected

    @pytest.mark.parametrize("field", ["8", "5-1", "xyz", "mon-xyz"])
    def test_out_of_range_day_rejected(self, field):
        with pytest.raises(ValueError):
            crontab_day_of_week(field)


class TestExecuteRunProjectResolution:
    """PR-B: the origin/checkout resolver is the ONLY project-resolution path for
    an automation run — the legacy link-group heuristic (``resolve_local_project``)
    is gone.

    - the automation's legacy ``project_id`` maps to a v2 (origin-keyed) project
      via ``resolve_project_v2`` (which self-seeds any live legacy row, F4);
    - the per-runner ``checkouts`` row must already exist HERE — a v2 project whose
      checkout lives only on another runner is "not runnable here"
      (``resolve_local_checkout`` never auto-materializes), the SAME failure as a
      legacy missing clone;
    - ``automations.project_id`` / the downstream legacy ``project_id`` is left
      intact (hazard 7) — the resolver derives project_v2 from it;
    - no v2 project maps to the legacy id (no live legacy row on this shared DB) →
      the run FAILS with a clear missing-project error (no legacy fallback).
    """

    def _automation(self):
        return {
            "id": "auto-v2",
            "name": "V2 Projects",
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
    async def test_v2_project_here_keeps_project_id_and_binds_pv2(self, manager):
        stack, db, submit, _wait, _int = _session_run_env()
        with stack, patch(
            "app.core.project_resolver.resolve_project_v2", return_value={"id": "pv2-A"},
        ) as rpv2, patch(
            "app.core.project_resolver.resolve_local_checkout",
            return_value={"id": "ck-1", "path": "/runner/clone", "runner_id": "this"},
        ) as rlc, patch(
            "app.core.project_resolver._persist_session_project_v2"
        ) as persist:
            await manager._execute_run(self._automation(), "run-v1", "do it", {})

        rpv2.assert_called_once()
        rlc.assert_called_once()
        # project_id is NEVER rewritten (hazard 7): the legacy id flows downstream.
        assert submit.call_args.kwargs["project_id"] == "proj-A"
        assert db.create_session.call_args.kwargs["project_id"] == "proj-A"
        # create_session does NOT take the column, so the minted session is bound
        # to its project_v2 via the NULL-guarded self-heal write.
        persist.assert_called_once()
        assert persist.call_args.args[1] == "pv2-A"

    @pytest.mark.asyncio
    async def test_minted_session_bound_to_project_v2(self, manager):
        """The session minted for a run carries its project_v2_id: the run resolves
        the pv2 id and persists it onto the fresh session row (the SPAWN artifact
        for later resolve_cwd hits)."""
        stack, db, submit, _wait, _int = _session_run_env()
        db.create_session.return_value = {"id": "sess-x"}
        with stack, patch(
            "app.core.project_resolver.resolve_project_v2", return_value={"id": "pv2-A"},
        ), patch(
            "app.core.project_resolver.resolve_local_checkout",
            return_value={"id": "ck-1", "path": "/runner/clone", "runner_id": "this"},
        ), patch(
            "app.core.project_resolver._persist_session_project_v2"
        ) as persist:
            await manager._execute_run(self._automation(), "run-v5", "do it", {})

        minted_session_id = db.create_session.call_args.kwargs["session_id"]
        persist.assert_called_once()
        assert persist.call_args.args[0] == minted_session_id
        assert persist.call_args.args[1] == "pv2-A"

    @pytest.mark.asyncio
    async def test_v2_project_without_checkout_here_fails_like_missing_clone(self, manager):
        # No checkout here AND self-seed cannot materialize one (flag/opt-out off, or
        # no cloneable URL) -> the run fails with the missing-project error, exactly
        # as before self-seed existed (#607).
        stack, db, submit, _wait, _int = _session_run_env()
        with stack, patch(
            "app.core.project_resolver.resolve_project_v2", return_value={"id": "pv2-A"},
        ), patch(
            "app.core.project_resolver.resolve_local_checkout", return_value=None,
        ), patch(
            "app.core.checkout_manager.ensure_checkout", return_value=None,
        ) as seed:
            db.get_project.return_value = {
                "id": "proj-A", "name": "My Repo", "instance_id": "install-other",
            }
            db.get_install.return_value = {"device_name": "Office PC"}
            with pytest.raises(ValueError, match="Office PC"):
                await manager._execute_run(self._automation(), "run-v2", "do it", {})

        seed.assert_called_once()  # a self-seed WAS attempted before giving up
        submit.assert_not_called()
        db.create_session.assert_not_called()
        failed = [
            c.kwargs for c in db.update_automation_run.call_args_list
            if c.kwargs.get("status") == "failed"
        ]
        assert failed and "My Repo" in failed[0]["error"]

    @pytest.mark.asyncio
    async def test_v2_project_self_seeds_checkout_then_runs(self, manager):
        # No checkout here at first, but self-seed clones the base on demand (#607):
        # the run proceeds and binds the minted session to the project_v2.
        stack, db, submit, _wait, _int = _session_run_env()
        # resolve_local_checkout: absent first (drives the seed), present after.
        rlc_returns = iter([None, {"id": "ck-1", "path": "/runner/seeded", "runner_id": "this"}])
        with stack, patch(
            "app.core.project_resolver.resolve_project_v2", return_value={"id": "pv2-A"},
        ), patch(
            "app.core.project_resolver.resolve_local_checkout",
            side_effect=lambda *a, **k: next(rlc_returns),
        ), patch(
            "app.core.checkout_manager.ensure_checkout", return_value="/runner/seeded",
        ) as seed, patch(
            "app.core.project_resolver._persist_session_project_v2"
        ) as persist:
            await manager._execute_run(self._automation(), "run-v-seed", "do it", {})

        seed.assert_called_once()  # self-seed materialized the base checkout
        # The run proceeded: session minted, turn submitted, pv2 bound.
        submit.assert_called_once()
        assert submit.call_args.kwargs["project_id"] == "proj-A"
        persist.assert_called_once()
        assert persist.call_args.args[1] == "pv2-A"

    @pytest.mark.asyncio
    async def test_no_v2_project_fails_run_with_clear_error(self, manager):
        """No v2 project maps to the legacy id (no live legacy row on this shared
        DB) -> the run FAILS with the missing-project error; there is no legacy
        link-group fallback anymore (deleted in PR-B)."""
        stack, db, submit, _wait, _int = _session_run_env()
        with stack, patch(
            "app.core.project_resolver.resolve_project_v2", return_value=None,
        ) as rpv2, patch(
            "app.core.project_resolver.resolve_local_checkout"
        ) as rlc:
            db.get_project.return_value = {
                "id": "proj-A", "name": "My Repo", "instance_id": "install-other",
            }
            db.get_install.return_value = {"device_name": "Office PC"}
            with pytest.raises(ValueError, match="Office PC"):
                await manager._execute_run(self._automation(), "run-v3", "do it", {})

        rpv2.assert_called_once()
        rlc.assert_not_called()  # no v2 project -> checkout lookup never happens
        submit.assert_not_called()
        db.create_session.assert_not_called()
        failed = [
            c.kwargs for c in db.update_automation_run.call_args_list
            if c.kwargs.get("status") == "failed"
        ]
        assert failed and "My Repo" in failed[0]["error"]


# =============================================================================
# Hermetic harness for the scheduling / execution paths below
#
# Real in-memory DB (tests/conftest.py), a real APScheduler started *paused*
# (jobs register but never fire), a frozen clock where fire times matter, and
# a fake prompt runner standing in for ``_run_prompt_as_session`` so no model,
# session runtime or network is involved.
# =============================================================================

import hashlib  # noqa: E402
import hmac  # noqa: E402
import json  # noqa: E402
import types  # noqa: E402

import app.core.automation_manager as am  # noqa: E402

# 2026-09-26 is a Saturday.
_CLOCK = [datetime(2026, 9, 26, 8, 59, 45, tzinfo=timezone.utc)]


class _FrozenDatetime(datetime):
    """``datetime`` whose ``now()`` reads ``_CLOCK[0]``."""

    @classmethod
    def now(cls, tz=None):
        now = _CLOCK[0]
        return now.astimezone(tz) if tz else now.replace(tzinfo=None)


@pytest.fixture
def frozen_clock(monkeypatch):
    start = _CLOCK[0]
    monkeypatch.setattr(am, "datetime", _FrozenDatetime)
    yield _CLOCK
    _CLOCK[0] = start


@pytest.fixture
async def live_scheduler(manager):
    """A real AsyncIOScheduler, started paused, attached to ``manager``."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    sched = AsyncIOScheduler(timezone="UTC")
    sched.start(paused=True)
    manager._scheduler = sched
    yield sched
    sched.shutdown(wait=False)


def _make_automation(auto_id, *, triggers, enabled=True, prompt="do it", **kwargs):
    from app.db import database

    return database.create_automation(
        automation_id=auto_id,
        name=f"Automation {auto_id}",
        trigger_type=triggers[0]["type"],
        trigger_config=triggers[0].get("config", {}),
        triggers=triggers,
        prompt=prompt,
        profile_id="default",
        enabled=enabled,
        **kwargs,
    )


_WEBHOOK_NO_SECRET = [{"type": "webhook", "config": {}}]


def _job_ids(sched):
    return sorted(j.id for j in sched.get_jobs())


def _capture_tasks(manager):
    """Record every background task the manager spawns so a test can drain them."""
    created = []
    original = manager._create_tracked_task

    def track(coro, *, name):
        task = original(coro, name=name)
        created.append(task)
        return task

    manager._create_tracked_task = track
    return created


async def _drain(tasks):
    # Tasks may spawn tasks; loop until the list stops growing.
    seen = 0
    while seen < len(tasks):
        batch = tasks[seen:]
        seen = len(tasks)
        await asyncio.gather(*batch, return_exceptions=True)


async def _until(predicate, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


class _FakeRunner:
    """Stands in for ``_run_prompt_as_session`` (the model-backed query runner).

    ``gates[i]`` (an ``asyncio.Event``), when present, holds call ``i`` until
    set, so a test can keep a run "in progress".
    """

    def __init__(self):
        self.calls = []
        self.response = "done"
        self.cost = 0.25
        self.exc = None
        self.gates = []

    async def __call__(self, automation, run_id, rendered_prompt, project_id, project_v2_id=None):
        idx = len(self.calls)
        self.calls.append({
            "automation_id": automation["id"], "run_id": run_id,
            "prompt": rendered_prompt, "project_id": project_id,
        })
        if idx < len(self.gates):
            await self.gates[idx].wait()
        if self.exc is not None:
            raise self.exc
        return {
            "session_id": f"sess-{idx}", "response": self.response,
            "cost_usd": self.cost, "tokens_in": 10, "tokens_out": 20,
        }


@pytest.fixture
def runner(manager):
    fake = _FakeRunner()
    manager._run_prompt_as_session = fake
    return fake


class _FakeConnector:
    def __init__(self, fail_actions=False, fail_notify=False):
        self.actions = []
        self.messages = []
        self.notifications = []
        self.fail_actions = fail_actions
        self.fail_notify = fail_notify

    async def execute_action(self, action_id, params):
        if self.fail_actions:
            raise RuntimeError("action boom")
        self.actions.append((action_id, params))
        return {"ok": True}

    async def send_message(self, message):
        self.messages.append(message)

    async def send_notification(self, chat_id, kind, data):
        if self.fail_notify:
            raise RuntimeError("notify boom")
        self.notifications.append((chat_id, kind, data))


@pytest.fixture
def connectors(monkeypatch):
    """Running-connector registry: ``connectors[id] = _FakeConnector()``."""
    from app.core.connectors.manager import connector_manager

    registry = {}

    async def get_running(connector_id):
        return registry.get(connector_id)

    monkeypatch.setattr(connector_manager, "get_running_connector", get_running)
    return registry


class _FakeResponse:
    def __init__(self, status_code=200):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class _FakeHttpClient:
    def __init__(self, status_code=200):
        self.requests = []
        self.posts = []
        self.status_code = status_code

    async def request(self, method, url, content=None, headers=None):
        self.requests.append({"method": method, "url": url, "content": content, "headers": headers})
        return _FakeResponse(self.status_code)

    async def post(self, url, content=None, headers=None):
        self.posts.append({"url": url, "content": content, "headers": headers})
        return _FakeResponse(self.status_code)


@pytest.fixture
def http_client(monkeypatch):
    client = _FakeHttpClient()
    monkeypatch.setattr("app.core.http_client.get_http_client", lambda: client)
    return client


def _runs(auto_id):
    from app.db import database
    return database.get_automation_runs(auto_id)


def _auto(auto_id):
    from app.db import database
    return database.get_automation(auto_id)


# =============================================================================
# Schedule registration against a real (paused) APScheduler
# =============================================================================


class TestScheduleRegistrationLive:
    async def test_cron_trigger_registers_one_job_with_crontab_weekdays(
        self, manager, mock_db, live_scheduler
    ):
        from apscheduler.triggers.cron import CronTrigger

        config = {"expression": "30 9 * * 1-5", "timezone": "America/New_York"}
        auto = _make_automation("auto-cron", triggers=[{"type": "cron", "config": config}])

        await manager.schedule_automation("auto-cron")

        assert _job_ids(live_scheduler) == ["auto-cron__trigger_0"]
        job = live_scheduler.get_job("auto-cron__trigger_0")
        assert isinstance(job.trigger, CronTrigger)
        assert job.args == ("auto-cron", "cron", config)
        assert job.func == manager._on_schedule_trigger
        assert str(job.trigger.timezone) == "America/New_York"
        # Crontab 1-5 is Monday-Friday (APScheduler alone would read Tue-Sat):
        # from Saturday morning the next fire is Monday 09:30 New York time.
        nxt = job.trigger.get_next_fire_time(None, _CLOCK[0])
        assert nxt.strftime("%a %H:%M") == "Mon 09:30"
        assert manager._scheduled_versions["auto-cron"] == AutomationManager._trigger_version(auto)

    @pytest.mark.parametrize("dow,expected_first", [
        ("sun-thu", "Sun"),   # named range, Sunday=0
        ("fri-mon", "Sat"),   # named range wrapping past Saturday
        ("0", "Sun"),
        ("7", "Sun"),
    ])
    async def test_named_and_numeric_weekday_ranges_on_live_job(
        self, manager, mock_db, live_scheduler, dow, expected_first
    ):
        _make_automation("auto-dow", triggers=[
            {"type": "cron", "config": {"expression": f"0 9 * * {dow}"}},
        ])
        await manager.schedule_automation("auto-dow")
        job = live_scheduler.get_job("auto-dow__trigger_0")
        assert str(job.trigger.timezone) == "UTC"  # default timezone
        nxt = job.trigger.get_next_fire_time(None, _CLOCK[0])
        assert nxt.strftime("%a") == expected_first

    async def test_interval_trigger_registers_interval_job(self, manager, mock_db, live_scheduler):
        from apscheduler.triggers.interval import IntervalTrigger

        _make_automation("auto-int", triggers=[{"type": "interval", "config": {"seconds": 900}}])
        await manager.schedule_automation("auto-int")

        job = live_scheduler.get_job("auto-int__trigger_0")
        assert isinstance(job.trigger, IntervalTrigger)
        assert job.trigger.interval == timedelta(seconds=900)
        assert job.args == ("auto-int", "interval", {"seconds": 900})

    async def test_interval_without_seconds_defaults_to_hourly(self, manager, mock_db, live_scheduler):
        _make_automation("auto-int", triggers=[{"type": "interval", "config": {}}])
        await manager.schedule_automation("auto-int")
        assert live_scheduler.get_job("auto-int__trigger_0").trigger.interval == timedelta(hours=1)

    async def test_event_driven_triggers_register_no_jobs(self, manager, mock_db, live_scheduler):
        _make_automation("auto-evt", triggers=[
            {"type": "webhook", "config": {}},
            {"type": "connector_event", "config": {"event_type": "session.complete"}},
            {"type": "connector_trigger", "config": {"trigger_id": "new_email"}},
        ])
        await manager.schedule_automation("auto-evt")
        assert live_scheduler.get_jobs() == []
        assert "auto-evt" not in manager._scheduled_versions

    async def test_mixed_triggers_keep_their_array_index(self, manager, mock_db, live_scheduler):
        _make_automation("auto-mix", triggers=[
            {"type": "cron", "config": {"expression": "0 * * * *"}},
            {"type": "webhook", "config": {}},
            {"type": "interval", "config": {"seconds": 600}},
        ])
        await manager.schedule_automation("auto-mix")
        assert _job_ids(live_scheduler) == ["auto-mix__trigger_0", "auto-mix__trigger_2"]
        assert live_scheduler.get_job("auto-mix__trigger_2").args[1] == "interval"

    async def test_invalid_cron_triggers_are_skipped_not_fatal(self, manager, mock_db, live_scheduler):
        _make_automation("auto-bad", triggers=[
            {"type": "cron", "config": {"expression": "0 9 * *"}},      # 4 fields
            {"type": "cron", "config": {"expression": "0 9 * * 8"}},    # day-of-week out of range
            {"type": "cron", "config": {"expression": "0 9 * * mon", "timezone": "Mars/Base"}},
            {"type": "interval", "config": {"seconds": 600}},
        ])
        await manager.schedule_automation("auto-bad")
        assert _job_ids(live_scheduler) == ["auto-bad__trigger_3"]

    async def test_invalid_interval_is_skipped(self, manager, mock_db, live_scheduler):
        _make_automation("auto-bad-int", triggers=[
            {"type": "interval", "config": {"seconds": "soon"}},
            {"type": "cron", "config": {"expression": "0 9 * * *"}},
        ])
        await manager.schedule_automation("auto-bad-int")
        assert _job_ids(live_scheduler) == ["auto-bad-int__trigger_1"]

    async def test_missing_automation_schedules_nothing(self, manager, mock_db, live_scheduler):
        await manager.schedule_automation("auto-ghost")
        assert live_scheduler.get_jobs() == []

    async def test_rescheduling_does_not_duplicate_jobs(self, manager, mock_db, live_scheduler):
        _make_automation("auto-twice", triggers=[
            {"type": "cron", "config": {"expression": "0 9 * * *"}},
            {"type": "interval", "config": {"seconds": 600}},
        ])
        for _ in range(3):
            await manager.schedule_automation("auto-twice")
        assert _job_ids(live_scheduler) == ["auto-twice__trigger_0", "auto-twice__trigger_1"]

    async def test_update_while_scheduled_removes_the_dropped_trigger_job(
        self, manager, mock_db, live_scheduler
    ):
        """Mutation-checked: with ``_remove_automation_jobs`` stubbed out the
        stale ``__trigger_1`` job survives and this test fails."""
        from app.db import database

        _make_automation("auto-upd", triggers=[
            {"type": "cron", "config": {"expression": "0 9 * * *"}},
            {"type": "interval", "config": {"seconds": 600}},
        ])
        await manager.schedule_automation("auto-upd")
        assert _job_ids(live_scheduler) == ["auto-upd__trigger_0", "auto-upd__trigger_1"]

        database.update_automation("auto-upd", triggers=[
            {"type": "interval", "config": {"seconds": 1200}},
        ])
        await manager.schedule_automation("auto-upd")

        assert _job_ids(live_scheduler) == ["auto-upd__trigger_0"]
        job = live_scheduler.get_job("auto-upd__trigger_0")
        assert job.args[1] == "interval"
        assert job.trigger.interval == timedelta(seconds=1200)

    async def test_switching_to_event_only_triggers_clears_jobs_and_version(
        self, manager, mock_db, live_scheduler
    ):
        from app.db import database

        _make_automation("auto-sw", triggers=[{"type": "cron", "config": {"expression": "0 9 * * *"}}])
        await manager.schedule_automation("auto-sw")
        database.update_automation("auto-sw", triggers=_WEBHOOK_NO_SECRET)
        await manager.schedule_automation("auto-sw")
        assert live_scheduler.get_jobs() == []
        assert "auto-sw" not in manager._scheduled_versions

    async def test_disable_while_scheduled_removes_jobs(self, manager, mock_db, live_scheduler):
        from app.db import database

        _make_automation("auto-off", triggers=[{"type": "interval", "config": {"seconds": 600}}])
        await manager.schedule_automation("auto-off")
        assert _job_ids(live_scheduler) == ["auto-off__trigger_0"]

        database.update_automation("auto-off", is_enabled=False)
        await manager.schedule_automation("auto-off")
        assert live_scheduler.get_jobs() == []
        assert "auto-off" not in manager._scheduled_versions

    async def test_unschedule_leaves_prefix_sibling_alone(self, manager, mock_db, live_scheduler):
        for auto_id in ("auto-1", "auto-10"):
            _make_automation(auto_id, triggers=[{"type": "interval", "config": {"seconds": 600}}])
            await manager.schedule_automation(auto_id)

        await manager.unschedule_automation("auto-1")

        assert _job_ids(live_scheduler) == ["auto-10__trigger_0"]
        assert set(manager._scheduled_versions) == {"auto-10"}

    async def test_job_removal_tolerates_a_vanished_job(self, manager, mock_db):
        job = types.SimpleNamespace(id="auto-x__trigger_0")
        sched = MagicMock()
        sched.get_jobs.return_value = [job]
        sched.remove_job.side_effect = KeyError("gone")
        manager._scheduler = sched
        await manager.unschedule_automation("auto-x")  # must not raise
        sched.remove_job.assert_called_once_with("auto-x__trigger_0")


class TestServiceChangesReachTheScheduler:
    """automation_service create/update/delete drive the singleton's jobs."""

    @pytest.fixture
    async def singleton_scheduler(self):
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from app.core.automation_manager import automation_manager

        sched = AsyncIOScheduler(timezone="UTC")
        sched.start(paused=True)
        saved = (automation_manager._scheduler, dict(automation_manager._scheduled_versions))
        automation_manager._scheduler = sched
        automation_manager._scheduled_versions.clear()
        yield sched
        sched.shutdown(wait=False)
        automation_manager._scheduler = saved[0]
        automation_manager._scheduled_versions.clear()
        automation_manager._scheduled_versions.update(saved[1])

    async def test_create_update_delete_while_scheduled(self, mock_db, singleton_scheduler):
        from app.core import automation_service

        auto = await automation_service.create_automation(
            name="svc", trigger_type="cron", trigger_config={"expression": "0 9 * * 1-5"},
            profile_id="default", prompt="hi", enabled=True,
        )
        aid = auto["id"]
        assert _job_ids(singleton_scheduler) == [f"{aid}__trigger_0"]

        await automation_service.update_automation(aid, triggers=[
            {"type": "interval", "config": {"seconds": 300}},
        ])
        assert _job_ids(singleton_scheduler) == [f"{aid}__trigger_0"]
        job = singleton_scheduler.get_job(f"{aid}__trigger_0")
        assert job.args[1] == "interval" and job.trigger.interval == timedelta(seconds=300)

        # A non-trigger edit leaves the job (and its next-fire phase) alone.
        before = job.next_run_time
        await automation_service.update_automation(aid, name="renamed")
        assert singleton_scheduler.get_job(f"{aid}__trigger_0").next_run_time == before

        assert await automation_service.delete_automation(aid) is True
        assert singleton_scheduler.get_jobs() == []

    async def test_trigger_change_on_disabled_automation_schedules_nothing(
        self, mock_db, singleton_scheduler
    ):
        from app.core import automation_service

        auto = await automation_service.create_automation(
            name="svc-off", trigger_type="interval", trigger_config={"seconds": 600},
            profile_id="default", prompt="hi",
        )
        await automation_service.update_automation(auto["id"], triggers=[
            {"type": "cron", "config": {"expression": "0 9 * * *"}},
        ])
        assert singleton_scheduler.get_jobs() == []


# =============================================================================
# Startup reconciliation
# =============================================================================


@pytest.fixture
def paused_scheduler_class(monkeypatch):
    """Make ``start()`` build a real AsyncIOScheduler that never fires jobs."""
    from apscheduler.schedulers import asyncio as aps_asyncio

    class PausedScheduler(aps_asyncio.AsyncIOScheduler):
        def start(self, paused=False):
            super().start(paused=True)

    monkeypatch.setattr(aps_asyncio, "AsyncIOScheduler", PausedScheduler)
    return PausedScheduler


class TestStartupReconciliation:
    def _seed(self):
        _make_automation("auto-a", triggers=[{"type": "cron", "config": {"expression": "0 9 * * *"}}])
        _make_automation("auto-b", triggers=[{"type": "interval", "config": {"seconds": 600}}])
        _make_automation("auto-c", enabled=False,
                         triggers=[{"type": "cron", "config": {"expression": "0 9 * * *"}}])
        _make_automation("auto-d", triggers=_WEBHOOK_NO_SECRET)

    async def test_start_schedules_enabled_time_triggers_only(
        self, manager, mock_db, paused_scheduler_class
    ):
        self._seed()
        await manager.start()
        try:
            assert isinstance(manager._scheduler, paused_scheduler_class)
            assert _job_ids(manager._scheduler) == ["auto-a__trigger_0", "auto-b__trigger_0"]
            assert set(manager._scheduled_versions) == {"auto-a", "auto-b"}
            assert manager._reconcile_task is not None and not manager._reconcile_task.done()

            scheduler = manager._scheduler
            await manager.start()  # idempotent
            assert manager._scheduler is scheduler
        finally:
            await manager.shutdown()
        assert manager._reconcile_task is None

    async def test_reconcile_applies_changes_made_elsewhere(
        self, manager, mock_db, paused_scheduler_class
    ):
        from app.db import database

        self._seed()
        await manager.start()
        try:
            database.update_automation("auto-a", is_enabled=False)
            database.update_automation("auto-b", triggers=[{"type": "interval", "config": {"seconds": 1200}}])
            database.update_automation("auto-c", is_enabled=True)
            database.delete_automation("auto-d")

            await manager._reconcile_schedules()

            assert _job_ids(manager._scheduler) == ["auto-b__trigger_0", "auto-c__trigger_0"]
            assert manager._scheduler.get_job("auto-b__trigger_0").trigger.interval == timedelta(seconds=1200)
            assert set(manager._scheduled_versions) == {"auto-b", "auto-c"}
        finally:
            await manager.shutdown()

    async def test_start_survives_a_failing_initial_reconcile(
        self, manager, mock_db, paused_scheduler_class, monkeypatch
    ):
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(am.database, "get_enabled_automations", boom)
        await manager.start()
        try:
            assert manager._initialized is True
            assert manager._scheduler.get_jobs() == []
            assert manager._reconcile_task is not None
        finally:
            await manager.shutdown()

    async def test_start_without_apscheduler_disables_scheduling(self, manager, mock_db, monkeypatch):
        import sys

        monkeypatch.setitem(sys.modules, "apscheduler.schedulers.asyncio", None)
        await manager.start()
        assert manager._initialized is True
        assert manager._scheduler is None
        assert manager._reconcile_task is None
        await manager._reconcile_schedules()  # no scheduler: no-op, no error

    async def test_reconcile_continues_past_one_failing_automation(self, manager, mock_db):
        _make_automation("auto-a", triggers=[{"type": "interval", "config": {"seconds": 600}}])
        _make_automation("auto-b", triggers=[{"type": "interval", "config": {"seconds": 600}}])
        manager._scheduler = MagicMock(get_jobs=MagicMock(return_value=[]))
        manager.schedule_automation = AsyncMock(side_effect=[RuntimeError("bad row"), None])
        await manager._reconcile_schedules()
        assert sorted(c.args[0] for c in manager.schedule_automation.await_args_list) == ["auto-a", "auto-b"]

    async def test_reconcile_loop_survives_errors(self, manager, monkeypatch):
        monkeypatch.setattr(am, "SCHEDULE_RECONCILE_SECONDS", 0)
        manager._reconcile_schedules = AsyncMock(
            side_effect=[RuntimeError("blip"), None, asyncio.CancelledError()]
        )
        with pytest.raises(asyncio.CancelledError):
            await manager._reconcile_loop()
        assert manager._reconcile_schedules.await_count == 3

    async def test_shutdown_cancels_running_runs(self, manager):
        async def forever():
            await asyncio.Event().wait()

        task = asyncio.create_task(forever())
        manager._running["auto-x"] = task
        await manager.shutdown()
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()
        assert manager._running == {}


# =============================================================================
# Scheduled fires: claim once per tick, fixed clock
# =============================================================================


class TestScheduledFireClaims:
    @pytest.fixture
    def fire_env(self, manager, mock_db, frozen_clock, monkeypatch):
        monkeypatch.setattr(
            "app.core.automation_service.should_run_scheduled_here",
            lambda automation: (True, "elected"),
        )
        executed = []

        async def fake_execute(automation_id, trigger_data):
            executed.append((automation_id, trigger_data))

        monkeypatch.setattr(manager, "_execute", fake_execute)
        tasks = _capture_tasks(manager)
        return executed, tasks

    async def test_each_tick_executes_once(self, manager, fire_env, frozen_clock):
        executed, tasks = fire_env
        _make_automation("auto-tick", triggers=[{"type": "interval", "config": {"seconds": 300}}])

        # The same tick delivered twice (e.g. two installs) → one execution.
        await manager._on_schedule_trigger("auto-tick", "interval", {"seconds": 300})
        await manager._on_schedule_trigger("auto-tick", "interval", {"seconds": 300})
        await _drain(tasks)
        assert executed == [(
            "auto-tick", {"trigger": "schedule", "fired_at": frozen_clock[0].isoformat()},
        )]

        # Next window → a new tick.
        frozen_clock[0] = frozen_clock[0] + timedelta(seconds=300)
        await manager._on_schedule_trigger("auto-tick", "interval", {"seconds": 300})
        await _drain(tasks)
        assert len(executed) == 2

    async def test_claim_error_fails_open(self, manager, fire_env, monkeypatch):
        executed, tasks = fire_env
        _make_automation("auto-claim", triggers=[{"type": "cron", "config": {"expression": "* * * * *"}}])

        def broken_claim(*a, **k):
            raise RuntimeError("claims table missing")

        monkeypatch.setattr(am.database, "claim_automation_fire", broken_claim)
        await manager._on_schedule_trigger("auto-claim", "cron", {})
        await _drain(tasks)
        assert [e[0] for e in executed] == ["auto-claim"]

    async def test_prune_error_does_not_block_the_fire(self, manager, fire_env, monkeypatch):
        executed, tasks = fire_env
        _make_automation("auto-prune", triggers=[{"type": "cron", "config": {"expression": "* * * * *"}}])

        def broken_prune(*a, **k):
            raise RuntimeError("prune failed")

        monkeypatch.setattr(am.database, "prune_automation_fires", broken_prune)
        await manager._on_schedule_trigger("auto-prune", "cron", {})
        await _drain(tasks)
        assert [e[0] for e in executed] == ["auto-prune"]

    async def test_tick_after_the_app_began_shutting_down_is_not_claimed(
        self, manager, fire_env, monkeypatch
    ):
        """R10-14: the app flags shutdown long before it reaches the manager's
        own ``shutdown()``; a tick in between neither runs nor spends the
        cross-install fire claim a live install could still take."""
        from app.core import startup

        executed, tasks = fire_env
        _make_automation("auto-sd-tick", triggers=[{"type": "cron", "config": {"expression": "* * * * *"}}])
        claims = []
        monkeypatch.setattr(am.database, "claim_automation_fire",
                            lambda *a, **k: claims.append(a) or True)
        monkeypatch.setattr(startup, "is_shutting_down", True)

        await manager._on_schedule_trigger("auto-sd-tick", "cron", {})
        await _drain(tasks)
        assert executed == [] and claims == []

    def test_fire_bucket_identity(self):
        t = datetime(2026, 9, 26, 9, 0, 20, tzinfo=timezone.utc)
        # cron: rounds to the nearest minute (late or early callbacks agree).
        assert AutomationManager._fire_bucket("cron", {}, t) == "cron:2026-09-26T09:00Z"
        assert AutomationManager._fire_bucket("cron", {}, t - timedelta(seconds=45)) == "cron:2026-09-26T09:00Z"
        # interval: epoch-aligned window; sub-minute configs clamp to 60s.
        assert AutomationManager._fire_bucket("interval", {"seconds": 3600}, t) == \
            "interval:3600:2026-09-26T09:00:00Z"
        assert AutomationManager._fire_bucket("interval", {"seconds": 5}, t) == \
            "interval:60:2026-09-26T09:00:00Z"
        assert AutomationManager._fire_bucket("interval", None, t) == \
            "interval:3600:2026-09-26T09:00:00Z"


# =============================================================================
# Run execution: prompt vs action steps, run rows and last-run fields
# =============================================================================


class TestRunExecution:
    async def test_prompt_run_success_stamps_run_row_and_last_run_fields(
        self, manager, mock_db, runner
    ):
        _make_automation("auto-ok", triggers=_WEBHOOK_NO_SECRET, prompt="Summarize {{payload.repo}}")

        await manager._execute("auto-ok", {"trigger": "webhook", "payload": {"repo": "shuffle"}})

        assert [c["prompt"] for c in runner.calls] == ["Summarize shuffle"]
        (run,) = _runs("auto-ok")
        assert run["status"] == "completed"
        assert run["prompt_rendered"] == "Summarize shuffle"
        assert run["trigger_data"] == {"trigger": "webhook", "payload": {"repo": "shuffle"}}
        assert run["response"] == "done"
        assert run["cost_usd"] == 0.25
        assert (run["tokens_in"], run["tokens_out"]) == (10, 20)
        assert run["started_at"] and run["completed_at"]
        assert run["duration_ms"] is not None and run["duration_ms"] >= 0
        assert run["output_status"] == "delivered"  # default session output

        auto = _auto("auto-ok")
        assert auto["status"] == "idle"
        assert auto["run_count"] == 1
        assert auto["total_cost_used"] == 0.25
        assert auto["last_run_at"]
        assert auto["last_error"] is None
        assert auto["failure_count"] == 0
        assert manager._running == {}

    async def test_per_run_cost_overrun_is_logged_but_run_completes(
        self, manager, mock_db, runner, caplog
    ):
        _make_automation("auto-pricey", triggers=_WEBHOOK_NO_SECRET, max_cost_per_run=0.1)
        runner.cost = 0.5
        with caplog.at_level("WARNING", logger="app.core.automation_manager"):
            await manager._execute("auto-pricey", {"trigger": "manual"})
        assert _runs("auto-pricey")[0]["status"] == "completed"
        assert any("exceeded per-run cost" in r.message for r in caplog.records)

    async def test_no_prompt_and_no_steps_completes_without_calling_runner(
        self, manager, mock_db, runner
    ):
        _make_automation("auto-empty", triggers=_WEBHOOK_NO_SECRET, prompt="")
        await manager._execute("auto-empty", {"trigger": "manual"})
        assert runner.calls == []
        (run,) = _runs("auto-empty")
        assert run["status"] == "completed"
        assert _auto("auto-empty")["run_count"] == 1

    async def test_action_steps_only_run_without_the_model(
        self, manager, mock_db, runner, connectors
    ):
        connectors["c-1"] = _FakeConnector()
        connectors["c-bad"] = _FakeConnector(fail_actions=True)
        _make_automation("auto-steps", triggers=_WEBHOOK_NO_SECRET, prompt="", action_steps=[
            {"type": "connector_action", "connector_id": "c-1", "action_id": "post",
             "params": {"text": "PR {{payload.pr}} merged", "count": 3}},
            {"type": "connector_action", "connector_id": "c-offline", "action_id": "post"},
            {"type": "connector_action", "action_id": "post"},  # no connector_id
            {"type": "connector_action", "connector_id": "c-bad", "action_id": "post", "params": {}},
            {"type": "send_prompt", "prompt": "   "},
            {"type": "webhook_call"},  # no url
            {"type": "delay", "seconds": 0},
            {"type": "mystery"},
            {"type": "connector_action", "connector_id": "c-1", "action_id": "after", "params": {}},
        ])

        await manager._execute("auto-steps", {"trigger": "webhook", "payload": {"pr": 7}})

        assert runner.calls == []
        assert connectors["c-1"].actions == [
            ("post", {"text": "PR 7 merged", "count": 3}),
            ("after", {}),
        ]
        (run,) = _runs("auto-steps")
        assert run["status"] == "completed"
        assert _auto("auto-steps")["run_count"] == 1

    async def test_prompt_response_feeds_action_steps_and_send_prompt_chains(
        self, manager, mock_db, runner, connectors, monkeypatch
    ):
        connectors["c-1"] = _FakeConnector()
        runner.response = "LGTM"
        failover = AsyncMock(side_effect=[{"response": "Refined LGTM"}, RuntimeError("provider down")])
        monkeypatch.setattr("app.core.account_failover.execute_query_with_failover", failover)
        _make_automation("auto-chain", triggers=_WEBHOOK_NO_SECRET, prompt="Review", action_steps=[
            {"type": "connector_action", "connector_id": "c-1", "action_id": "say",
             "params": {"text": "{{response}}"}},
            {"type": "send_prompt", "prompt": "Polish: {{response}}"},
            {"type": "send_prompt", "prompt": "Again: {{response}}", "profile_id": "other", "max_turns": 4},
            {"type": "connector_action", "connector_id": "c-1", "action_id": "say",
             "params": {"text": "{{response}}"}},
        ])

        await manager._execute("auto-chain", {"trigger": "manual"})

        assert connectors["c-1"].actions == [
            ("say", {"text": "LGTM"}),
            ("say", {"text": "Refined LGTM"}),  # the failed second send_prompt left it unchanged
        ]
        first, second = failover.await_args_list
        assert first.kwargs == {
            "prompt": "Polish: LGTM", "profile_id": "default",
            "overrides": {"max_turns": 1}, "automation_id": "auto-chain",
        }
        assert second.kwargs["profile_id"] == "other"
        assert second.kwargs["overrides"] == {"max_turns": 4}
        assert _runs("auto-chain")[0]["status"] == "completed"

    async def test_webhook_call_steps(self, manager, mock_db, runner, http_client):
        runner.response = "ok!"
        _make_automation("auto-hook", triggers=_WEBHOOK_NO_SECRET, prompt="go", action_steps=[
            {"type": "webhook_call", "url": "https://example.test/a",
             "headers": {"X-Key": "k"}, "body": '{"said": "{{response}}"}'},
            {"type": "webhook_call", "url": "https://example.test/b", "method": "get"},
        ])
        await manager._execute("auto-hook", {"trigger": "manual"})
        assert http_client.requests == [
            {"method": "POST", "url": "https://example.test/a",
             "content": '{"said": "ok!"}', "headers": {"X-Key": "k"}},
            {"method": "GET", "url": "https://example.test/b", "content": None, "headers": {}},
        ]
        assert _runs("auto-hook")[0]["status"] == "completed"

    async def test_failing_webhook_call_step_does_not_fail_the_run(
        self, manager, mock_db, runner, http_client
    ):
        http_client.status_code = 500
        _make_automation("auto-hook500", triggers=_WEBHOOK_NO_SECRET, prompt="go", action_steps=[
            {"type": "webhook_call", "url": "https://example.test/a"},
        ])
        await manager._execute("auto-hook500", {"trigger": "manual"})
        assert len(http_client.requests) == 1
        assert _runs("auto-hook500")[0]["status"] == "completed"

    async def test_delay_steps_are_capped_and_zero_skipped(self, manager, monkeypatch):
        slept = []

        async def fake_sleep(seconds):
            slept.append(seconds)

        monkeypatch.setattr(am, "asyncio", types.SimpleNamespace(sleep=fake_sleep))
        await manager._execute_action_steps("auto-d", "run-d", [
            {"type": "delay", "seconds": 0},
            {"type": "delay", "seconds": 900},
            {"type": "delay", "seconds": 5},
        ], {}, "")
        assert slept == [300, 5]


# =============================================================================
# Template variables per trigger type, through the real entry points
# =============================================================================


class TestTemplateVariablesPerTrigger:
    async def test_manual_trigger_renders_trigger_and_fired_at(
        self, manager, mock_db, runner, frozen_clock
    ):
        tasks = _capture_tasks(manager)
        _make_automation("auto-man", triggers=_WEBHOOK_NO_SECRET, prompt="{{trigger}} run at {{fired_at}}")
        assert await manager.trigger_manual("auto-man") == {"automation_id": "auto-man", "status": "triggered"}
        await _drain(tasks)
        expected = f"manual run at {frozen_clock[0].isoformat()}"
        assert [c["prompt"] for c in runner.calls] == [expected]
        assert _runs("auto-man")[0]["prompt_rendered"] == expected

    async def test_schedule_trigger_renders_trigger_and_fired_at(
        self, manager, mock_db, runner, frozen_clock, monkeypatch
    ):
        monkeypatch.setattr(
            "app.core.automation_service.should_run_scheduled_here", lambda a: (True, "elected"),
        )
        tasks = _capture_tasks(manager)
        _make_automation("auto-sch", prompt="{{trigger}} tick {{fired_at}} {{payload.x}}",
                         triggers=[{"type": "cron", "config": {"expression": "0 9 * * *"}}])
        await manager._on_schedule_trigger("auto-sch", "cron", {"expression": "0 9 * * *"})
        await _drain(tasks)
        # Unknown variables for this trigger type stay literal.
        assert [c["prompt"] for c in runner.calls] == [
            f"schedule tick {frozen_clock[0].isoformat()} {{{{payload.x}}}}"
        ]

    async def test_webhook_trigger_renders_payload(self, manager, mock_db, runner):
        tasks = _capture_tasks(manager)
        _make_automation("auto-wh", triggers=_WEBHOOK_NO_SECRET,
                         prompt="{{trigger}}: {{payload.repo}} #{{payload.pr.number}} {{event.x}}")
        payload = {"repo": "shuffle", "pr": {"number": 7}}
        assert await manager.trigger_webhook("auto-wh", payload) is not None
        await _drain(tasks)
        assert [c["prompt"] for c in runner.calls] == ["webhook: shuffle #7 {{event.x}}"]
        assert _runs("auto-wh")[0]["trigger_data"] == {"trigger": "webhook", "payload": payload}

    async def test_connector_event_renders_event_and_event_type(self, manager, mock_db, runner):
        tasks = _capture_tasks(manager)
        _make_automation("auto-ce", prompt="{{event_type}} in {{event.project_id}} ({{event.session_id}})",
                         triggers=[{"type": "connector_event",
                                    "config": {"event_type": "session.complete", "connector_id": "c-1"}}])
        await manager.handle_connector_event(
            "session.complete", {"connector_id": "c-1", "project_id": "proj-1", "session_id": "s-9"},
        )
        await _drain(tasks)
        assert [c["prompt"] for c in runner.calls] == ["session.complete in proj-1 (s-9)"]

    async def test_connector_trigger_renders_data(self, manager, mock_db, runner):
        tasks = _capture_tasks(manager)
        _make_automation("auto-ct", prompt="{{trigger_id}} from {{connector_id}}: {{data.subject}}",
                         triggers=[{"type": "connector_trigger",
                                    "config": {"trigger_id": "new_email", "connector_id": "c-1"}}])
        await manager.handle_connector_trigger("c-1", "new_email", {"subject": "Hi"})
        await manager.handle_connector_trigger("c-2", "new_email", {"subject": "not mine"})
        await manager.handle_connector_trigger("c-1", "other_trigger", {"subject": "nope"})
        await _drain(tasks)
        assert [c["prompt"] for c in runner.calls] == ["new_email from c-1: Hi"]


# =============================================================================
# Connector-event filtering
# =============================================================================


class TestConnectorEventFiltering:
    TRIGGER = {"type": "connector_event", "config": {
        "event_type": "session.complete", "connector_id": "c-1", "filter": {"project_id": "proj-1"},
    }}

    async def _fire(self, manager, event_type, data):
        tasks = _capture_tasks(manager)
        await manager.handle_connector_event(event_type, data)
        await _drain(tasks)

    async def test_matching_event_starts_one_run(self, manager, mock_db, runner):
        _make_automation("auto-f", triggers=[self.TRIGGER])
        await self._fire(manager, "session.complete", {"connector_ids": ["c-9", "c-1"], "project_id": "proj-1"})
        assert len(runner.calls) == 1
        (run,) = _runs("auto-f")
        assert run["trigger_data"]["trigger"] == "connector_event"
        assert run["trigger_data"]["event_type"] == "session.complete"

    @pytest.mark.parametrize("event_type,data", [
        ("session.complete", {"connector_id": "c-2", "project_id": "proj-1"}),   # other connector
        ("session.complete", {"project_id": "proj-1"}),                          # no connector at all
        ("session.complete", {"connector_id": "c-1", "project_id": "proj-2"}),   # filter mismatch
        ("session.error", {"connector_id": "c-1", "project_id": "proj-1"}),      # other event
        ("session.complete", {"connector_id": "c-1", "project_id": "proj-1", "automation_id": "auto-x"}),
        ("session.complete", {"connector_id": "c-1", "project_id": "proj-1", "source": "automation_step"}),
    ])
    async def test_non_matching_events_start_nothing(self, manager, mock_db, runner, event_type, data):
        _make_automation("auto-f", triggers=[self.TRIGGER])
        await self._fire(manager, event_type, data)
        assert runner.calls == []
        assert _runs("auto-f") == []

    async def test_disabled_automation_is_not_fired(self, manager, mock_db, runner):
        _make_automation("auto-f", enabled=False, triggers=[self.TRIGGER])
        await self._fire(manager, "session.complete", {"connector_id": "c-1", "project_id": "proj-1"})
        assert runner.calls == []

    async def test_two_matching_triggers_start_one_run(self, manager, mock_db, runner):
        loose = {"type": "connector_event", "config": {"event_type": "session.complete"}}
        _make_automation("auto-f2", triggers=[self.TRIGGER, loose])
        await self._fire(manager, "session.complete", {"connector_id": "c-1", "project_id": "proj-1"})
        assert len(runner.calls) == 1


# =============================================================================
# Budget and approval requirements
# =============================================================================


class TestBudgetAndApproval:
    async def test_daily_run_limit_blocks_with_budget_exceeded_row(self, manager, mock_db, runner):
        from app.db import database

        _make_automation("auto-cap", triggers=_WEBHOOK_NO_SECRET, max_runs_per_day=1)
        database.create_automation_run(run_id="run-earlier", automation_id="auto-cap", status="completed")

        await manager._execute("auto-cap", {"trigger": "schedule"})

        assert runner.calls == []
        statuses = sorted(r["status"] for r in _runs("auto-cap"))
        assert statuses == ["budget_exceeded", "completed"]
        auto = _auto("auto-cap")
        assert "Daily run limit" in auto["last_error"]
        assert auto["failure_count"] == 1
        assert manager._running == {}

    async def test_daily_cost_limit_blocks(self, manager, mock_db, runner):
        from app.db import database

        _make_automation("auto-spend", triggers=_WEBHOOK_NO_SECRET, max_cost_per_day=0.5)
        database.create_automation_run(run_id="run-spent", automation_id="auto-spend", status="completed")
        database.update_automation_run("run-spent", cost_usd=0.75)

        await manager._execute("auto-spend", {"trigger": "schedule"})

        assert runner.calls == []
        assert "Daily cost limit" in _auto("auto-spend")["last_error"]

    async def test_approval_parks_the_run_and_notifies_connector_chats(
        self, manager, mock_db, runner, connectors
    ):
        from app.db import database

        database.create_connector("c-1", "telegram", "TG", {}, [], is_active=True)
        database.create_connector("c-2", "slack", "Slack", {}, [], is_active=True)  # not running
        database.create_connector_session("cs-1", "c-1", "chat-9")
        connectors["c-1"] = _FakeConnector()
        _make_automation("auto-appr", triggers=_WEBHOOK_NO_SECRET,
                         prompt="Deploy {{payload.env}}", requires_approval=True)

        await manager._execute("auto-appr", {"trigger": "webhook", "payload": {"env": "prod"}})

        assert runner.calls == []
        (run,) = _runs("auto-appr")
        assert run["status"] == "pending"
        assert run["prompt_rendered"] == "Deploy prod"
        (approval,) = database.get_pending_automation_approvals()
        assert approval["run_id"] == run["id"]
        assert approval["prompt_preview"] == "Deploy prod"
        assert connectors["c-1"].notifications == [(
            "chat-9", "automation.approval_needed",
            {"automation_name": "Automation auto-appr", "approval_id": approval["id"],
             "prompt_preview": "Deploy prod"},
        )]
        assert _auto("auto-appr")["status"] == "idle"
        assert manager._running == {}

        # Approve (both writes, as the approve route makes them) → the parked
        # run executes with the rendered prompt.
        tasks = _capture_tasks(manager)
        database.update_automation_approval(approval["id"], status="approved")
        database.update_automation_run(run["id"], status="approved")
        resumed = await manager.execute_approved_run(approval["id"])
        assert resumed["id"] == run["id"]
        await _drain(tasks)
        assert [c["prompt"] for c in runner.calls] == ["Deploy prod"]
        assert _runs("auto-appr")[0]["status"] == "completed"
        assert manager._running == {}

    async def test_notification_failures_do_not_break_the_approval(
        self, manager, mock_db, runner, connectors
    ):
        from app.db import database

        database.create_connector("c-1", "telegram", "TG", {}, [], is_active=True)
        database.create_connector_session("cs-1", "c-1", "chat-9")
        connectors["c-1"] = _FakeConnector(fail_notify=True)
        _make_automation("auto-appr2", triggers=_WEBHOOK_NO_SECRET, requires_approval=True)

        await manager._execute("auto-appr2", {"trigger": "manual"})

        assert _runs("auto-appr2")[0]["status"] == "pending"
        assert len(database.get_pending_automation_approvals()) == 1

    async def test_notification_lookup_failure_is_swallowed(self, manager, monkeypatch):
        def boom():
            raise RuntimeError("connectors table down")

        monkeypatch.setattr(am.database, "get_active_connectors", boom)
        await manager._notify_approval_needed({"name": "x"}, "appr-1", "preview")

    async def test_execute_approved_run_requires_an_approved_approval(self, manager, mock_db, runner):
        from app.db import database

        _make_automation("auto-ap", triggers=_WEBHOOK_NO_SECRET)
        database.create_automation_run(run_id="run-ap", automation_id="auto-ap", status="pending")
        database.create_automation_approval(approval_id="appr-p", run_id="run-ap", automation_id="auto-ap")

        assert await manager.execute_approved_run("appr-p") is None       # still pending
        assert await manager.execute_approved_run("appr-missing") is None  # unknown
        database.update_automation_approval("appr-p", status="denied")
        assert await manager.execute_approved_run("appr-p") is None
        assert runner.calls == []


# =============================================================================
# Failure handling: what the run record and last-run fields show
# =============================================================================


class TestRunFailures:
    async def test_runner_error_fails_run_and_records_last_error(self, manager, mock_db, runner):
        runner.exc = RuntimeError("provider unavailable: no API key configured")
        _make_automation("auto-err", triggers=_WEBHOOK_NO_SECRET)

        await manager._execute("auto-err", {"trigger": "manual"})

        (run,) = _runs("auto-err")
        assert run["status"] == "failed"
        assert run["error"] == "provider unavailable: no API key configured"
        assert run["completed_at"] and run["duration_ms"] is not None
        auto = _auto("auto-err")
        assert auto["status"] == "error"
        assert auto["last_error"] == "provider unavailable: no API key configured"
        assert auto["failure_count"] == 1
        assert auto["run_count"] == 0
        assert auto["last_run_at"] is None
        assert manager._running == {}

    async def test_timeout_fails_run_with_timeout_message(self, manager, mock_db, runner):
        runner.exc = asyncio.TimeoutError()
        _make_automation("auto-slow", triggers=_WEBHOOK_NO_SECRET, timeout_seconds=42)

        await manager._execute("auto-slow", {"trigger": "manual"})

        (run,) = _runs("auto-slow")
        assert run["status"] == "failed"
        assert run["error"] == "Execution timed out after 42s"
        auto = _auto("auto-slow")
        assert auto["status"] == "error"
        assert auto["last_error"] == "Execution timed out after 42s"
        assert auto["failure_count"] == 1
        assert manager._running == {}

    async def test_exception_group_records_the_underlying_error(self, manager, mock_db, runner):
        """Regression: ``_execute_run`` unwraps an ExceptionGroup, but ``_execute``
        then re-recorded ``str(group)`` ("unhandled errors in a TaskGroup")
        over it on both the run row and ``last_error``."""
        import builtins  # ExceptionGroup is a 3.11 builtin; ruff has no target-version here

        runner.exc = builtins.ExceptionGroup("unhandled errors in a TaskGroup", [RuntimeError("real cause")])
        _make_automation("auto-grp", triggers=_WEBHOOK_NO_SECRET)

        await manager._execute("auto-grp", {"trigger": "manual"})

        assert _runs("auto-grp")[0]["error"] == "real cause"
        assert _auto("auto-grp")["last_error"] == "real cause"

    async def test_provider_unavailable_through_the_real_session_path(
        self, manager, mock_db, monkeypatch
    ):
        """No fake runner: the real ``_run_prompt_as_session`` mints and links a
        session, then the turn submit fails (no usable provider)."""
        from app.db import database

        submit = AsyncMock(side_effect=RuntimeError("No provider configured for profile default"))
        monkeypatch.setattr("app.core.turn_manager.submit_async", submit)
        monkeypatch.setattr("app.core.sync_engine.sync_engine", AsyncMock())
        _make_automation("auto-noprov", triggers=_WEBHOOK_NO_SECRET)

        await manager._execute("auto-noprov", {"trigger": "manual"})

        submit.assert_awaited_once()
        (run,) = _runs("auto-noprov")
        assert run["status"] == "failed"
        assert run["error"] == "No provider configured for profile default"
        # The session was minted and linked before the turn, and it is kept.
        session = database.get_session(run["session_id"])
        assert session is not None and session["automation_id"] == "auto-noprov"
        assert _auto("auto-noprov")["last_error"] == "No provider configured for profile default"

    async def test_session_broadcast_failure_does_not_fail_the_run(self, manager, mock_db, monkeypatch):
        monkeypatch.setattr("app.core.turn_manager.submit_async", AsyncMock(return_value="turn-1"))
        monkeypatch.setattr("app.core.turn_manager.wait_session_idle", AsyncMock(return_value=True))
        sync = MagicMock()
        sync.broadcast_user_session_created = AsyncMock(side_effect=RuntimeError("no devices"))
        monkeypatch.setattr("app.core.sync_engine.sync_engine", sync)
        monkeypatch.setattr(AutomationManager, "_final_turn_outcome", classmethod(
            lambda cls, sid: {"error": None, "interrupted": False, "terminal_found": True,
                              "usage": {"total_cost_usd": 0.1, "tokens_in": 1, "tokens_out": 2}}))
        monkeypatch.setattr(AutomationManager, "_extract_final_assistant_text",
                            staticmethod(lambda sid: "final words"))
        _make_automation("auto-bc", triggers=_WEBHOOK_NO_SECRET)

        await manager._execute("auto-bc", {"trigger": "manual"})

        (run,) = _runs("auto-bc")
        assert run["status"] == "completed"
        assert run["response"] == "final words"
        assert run["session_id"]

    async def test_admission_error_is_recorded_on_the_automation(self, manager, mock_db, runner, monkeypatch):
        def broken_budget(automation):
            raise RuntimeError("stats query failed")

        monkeypatch.setattr("app.core.automation_service.check_budget", broken_budget)
        _make_automation("auto-adm", triggers=_WEBHOOK_NO_SECRET)

        await manager._execute("auto-adm", {"trigger": "manual"})

        assert runner.calls == []
        auto = _auto("auto-adm")
        assert auto["status"] == "error"
        assert auto["last_error"] == "stats query failed"
        assert auto["failure_count"] == 1
        assert manager._running == {}

    async def test_failure_to_record_a_failure_is_contained(self, manager, mock_db, runner, monkeypatch):
        def broken_budget(automation):
            raise RuntimeError("stats query failed")

        def broken_update(*a, **k):
            raise RuntimeError("db gone")

        monkeypatch.setattr("app.core.automation_service.check_budget", broken_budget)
        _make_automation("auto-adm2", triggers=_WEBHOOK_NO_SECRET)
        monkeypatch.setattr(am.database, "update_automation", broken_update)

        await manager._execute("auto-adm2", {"trigger": "manual"})  # must not raise
        assert manager._running == {}

    async def test_missing_automation_is_a_no_op(self, manager, mock_db, runner):
        await manager._execute("auto-nope", {"trigger": "manual"})
        assert runner.calls == []
        assert manager._running == {}


# =============================================================================
# Concurrency: one run at a time per automation
# =============================================================================


class TestRunConcurrency:
    async def test_fire_during_a_run_is_skipped_and_latch_released_after(self, manager, mock_db, runner):
        """Mutation-checked: with the ``_running`` membership test defeated the
        second fire starts a second run and this test fails."""
        gate = asyncio.Event()
        runner.gates = [gate]
        _make_automation("auto-busy", triggers=_WEBHOOK_NO_SECRET)

        first = asyncio.create_task(manager._execute("auto-busy", {"trigger": "manual"}))
        await _until(lambda: len(runner.calls) == 1)

        await manager._execute("auto-busy", {"trigger": "schedule"})  # next fire arrives
        assert len(runner.calls) == 1
        assert len(_runs("auto-busy")) == 1

        gate.set()
        await first
        assert "auto-busy" not in manager._running

        await manager._execute("auto-busy", {"trigger": "schedule"})
        assert len(runner.calls) == 2
        assert sorted(r["status"] for r in _runs("auto-busy")) == ["completed", "completed"]

    async def test_latch_released_after_a_failed_run(self, manager, mock_db, runner):
        runner.exc = RuntimeError("boom")
        _make_automation("auto-flaky", triggers=_WEBHOOK_NO_SECRET)
        await manager._execute("auto-flaky", {"trigger": "manual"})
        runner.exc = None
        await manager._execute("auto-flaky", {"trigger": "manual"})
        assert [r["status"] for r in sorted(_runs("auto-flaky"), key=lambda r: r["created_at"])] == \
            ["failed", "completed"]

    async def test_different_automations_run_concurrently(self, manager, mock_db, runner):
        g1, g2 = asyncio.Event(), asyncio.Event()
        runner.gates = [g1, g2]
        _make_automation("auto-p", triggers=_WEBHOOK_NO_SECRET)
        _make_automation("auto-q", triggers=_WEBHOOK_NO_SECRET)
        t1 = asyncio.create_task(manager._execute("auto-p", {"trigger": "manual"}))
        t2 = asyncio.create_task(manager._execute("auto-q", {"trigger": "manual"}))
        await _until(lambda: len(runner.calls) == 2)
        g1.set()
        g2.set()
        await asyncio.gather(t1, t2)
        assert {c["automation_id"] for c in runner.calls} == {"auto-p", "auto-q"}

    async def test_approved_run_queues_behind_an_in_flight_run(self, manager, mock_db, runner):
        """Regression: an approved run used to start beside an in-flight run,
        overwrite its latch entry, and — when either finished — pop the latch
        out from under the other, so a later fire ran concurrently too."""
        from app.db import database

        g_first, g_approved = asyncio.Event(), asyncio.Event()
        runner.gates = [g_first, g_approved]
        _make_automation("auto-q", triggers=_WEBHOOK_NO_SECRET)
        database.create_automation_run(run_id="run-appr", automation_id="auto-q",
                                       status="approved", prompt_rendered="approved prompt")
        database.create_automation_approval(approval_id="appr-1", run_id="run-appr", automation_id="auto-q")
        database.update_automation_approval("appr-1", status="approved")
        tasks = _capture_tasks(manager)

        first = asyncio.create_task(manager._execute("auto-q", {"trigger": "manual"}))
        await _until(lambda: len(runner.calls) == 1)
        first_task_latch = manager._running["auto-q"]

        assert (await manager.execute_approved_run("appr-1"))["id"] == "run-appr"
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(runner.calls) == 1, "approved run must wait for the in-flight run"
        assert manager._running["auto-q"] is first_task_latch

        g_first.set()
        await first
        await _until(lambda: len(runner.calls) == 2)
        assert runner.calls[1]["prompt"] == "approved prompt"

        # The approved run now holds the latch: a fire arriving now is skipped.
        await manager._execute("auto-q", {"trigger": "schedule"})
        assert len(runner.calls) == 2

        g_approved.set()
        await _drain(tasks)
        assert manager._running == {}
        assert database.get_automation_run("run-appr")["status"] == "completed"

    async def test_approved_run_waiting_behind_the_latch_is_cancelled_by_shutdown(
        self, manager, mock_db, runner
    ):
        """R6-3: shutdown cancelled the in-flight run, the waiting approved run
        then woke to a free latch and started while the app was going down."""
        from app.db import database

        g_first = asyncio.Event()
        runner.gates = [g_first]
        _make_automation("auto-sd", triggers=_WEBHOOK_NO_SECRET)
        database.create_automation_run(run_id="run-sd", automation_id="auto-sd",
                                       status="approved", prompt_rendered="approved prompt")
        database.create_automation_approval(approval_id="appr-sd", run_id="run-sd", automation_id="auto-sd")
        database.update_automation_approval("appr-sd", status="approved")
        tasks = _capture_tasks(manager)

        first = asyncio.create_task(manager._execute("auto-sd", {"trigger": "manual"}))
        await _until(lambda: len(runner.calls) == 1)
        assert (await manager.execute_approved_run("appr-sd"))["id"] == "run-sd"
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(runner.calls) == 1

        await manager.shutdown()
        await asyncio.gather(first, return_exceptions=True)
        await _drain(tasks)
        for _ in range(20):
            await asyncio.sleep(0)
        assert len(runner.calls) == 1, "the approved run must not start during shutdown"
        assert all(t.done() for t in tasks)
        assert manager._running == {}
        assert database.get_automation_run("run-sd")["status"] == "approved"

    async def test_approved_run_cancelled_by_shutdown_runs_after_the_next_start(
        self, mock_db, paused_scheduler_class, monkeypatch
    ):
        """R8-12: the approved run shutdown cancelled stayed ``approved`` for
        good (approving again is refused); the next start re-queues it."""
        from app.db import database

        monkeypatch.setattr(
            "app.core.automation_service.should_run_scheduled_here", lambda a: (True, "elected"),
        )
        _make_automation("auto-rq", triggers=_WEBHOOK_NO_SECRET)
        database.create_automation_run(run_id="run-rq", automation_id="auto-rq",
                                       status="approved", prompt_rendered="approved prompt")
        database.create_automation_approval(approval_id="appr-rq", run_id="run-rq", automation_id="auto-rq")
        database.update_automation_approval("appr-rq", status="approved")
        # A pending approval and a finished approved run are left alone.
        database.create_automation_run(run_id="run-pend", automation_id="auto-rq", status="pending")
        database.create_automation_approval(approval_id="appr-pend", run_id="run-pend", automation_id="auto-rq")
        database.create_automation_run(run_id="run-done", automation_id="auto-rq", status="completed")
        database.create_automation_approval(approval_id="appr-done", run_id="run-done", automation_id="auto-rq")
        database.update_automation_approval("appr-done", status="approved")

        restarted = AutomationManager()
        fake = _FakeRunner()
        restarted._run_prompt_as_session = fake
        tasks = _capture_tasks(restarted)
        await restarted.start()
        try:
            await _until(lambda: database.get_automation_run("run-rq")["status"] == "completed")
        finally:
            await restarted.shutdown()
            await _drain(tasks)
        assert [c["run_id"] for c in fake.calls] == ["run-rq"]
        assert fake.calls[0]["prompt"] == "approved prompt"
        assert database.get_automation_run("run-pend")["status"] == "pending"

        # Started once: this install restarting does not run it again.
        again = AutomationManager()
        again._run_prompt_as_session = fake
        await again.start()
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            if again._requeue_task is not None:
                await asyncio.gather(again._requeue_task, return_exceptions=True)
        finally:
            await again.shutdown()
        assert len(fake.calls) == 1

    async def test_requeued_run_is_not_run_again_by_the_install_still_holding_it(
        self, mock_db, paused_scheduler_class, monkeypatch
    ):
        """R9-7: install A still holds an approved run queued in memory behind
        an in-flight run; install B starts, re-queues and runs it. When A's
        in-flight run ends, A must not run it a second time — the row moved
        ``approved -> running`` and that compare-and-set is the claim."""
        from app.db import database

        monkeypatch.setattr(
            "app.core.automation_service.should_run_scheduled_here", lambda a: (True, "elected"),
        )
        _make_automation("auto-2x", triggers=_WEBHOOK_NO_SECRET)
        database.create_automation_run(run_id="run-2x", automation_id="auto-2x",
                                       status="approved", prompt_rendered="approved prompt")
        database.create_automation_approval(approval_id="appr-2x", run_id="run-2x", automation_id="auto-2x")
        database.update_automation_approval("appr-2x", status="approved")

        install_a = AutomationManager()
        runner_a = _FakeRunner()
        g_first = asyncio.Event()
        runner_a.gates = [g_first]
        install_a._run_prompt_as_session = runner_a
        tasks_a = _capture_tasks(install_a)
        first = asyncio.create_task(install_a._execute("auto-2x", {"trigger": "manual"}))
        await _until(lambda: len(runner_a.calls) == 1)
        assert (await install_a.execute_approved_run("appr-2x"))["id"] == "run-2x"

        install_b = AutomationManager()
        runner_b = _FakeRunner()
        install_b._run_prompt_as_session = runner_b
        tasks_b = _capture_tasks(install_b)
        await install_b.start()
        try:
            await _until(lambda: database.get_automation_run("run-2x")["status"] == "completed")
            g_first.set()
            await first
            await _drain(tasks_a)
        finally:
            await install_b.shutdown()
            await _drain(tasks_b)
        assert [c["run_id"] for c in runner_b.calls] == ["run-2x"]
        assert [c["run_id"] for c in runner_a.calls] == [runner_a.calls[0]["run_id"]]
        assert runner_a.calls[0]["run_id"] != "run-2x", "install A ran the run a second time"

    async def test_requeue_skips_disabled_automations_and_expires_old_approvals(
        self, mock_db, paused_scheduler_class, monkeypatch
    ):
        """R9-7: the re-queue ran every run ever left ``approved``, including
        ones of disabled automations and approvals days old."""
        from datetime import datetime, timedelta, timezone

        from app.db import database
        from app.db.base import get_db

        monkeypatch.setattr(
            "app.core.automation_service.should_run_scheduled_here", lambda a: (True, "elected"),
        )
        _make_automation("auto-off", triggers=_WEBHOOK_NO_SECRET, enabled=False)
        _make_automation("auto-old", triggers=_WEBHOOK_NO_SECRET)
        for auto_id, run_id, appr_id in (("auto-off", "run-off", "appr-off"),
                                         ("auto-old", "run-old", "appr-old")):
            database.create_automation_run(run_id=run_id, automation_id=auto_id,
                                           status="approved", prompt_rendered="p")
            database.create_automation_approval(approval_id=appr_id, run_id=run_id, automation_id=auto_id)
            database.update_automation_approval(appr_id, status="approved")
        stale = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        with get_db() as conn:
            conn.cursor().execute(
                "UPDATE automation_approvals SET decided_at = %s WHERE id = %s", (stale, "appr-old"),
            )

        restarted = AutomationManager()
        fake = _FakeRunner()
        restarted._run_prompt_as_session = fake
        tasks = _capture_tasks(restarted)
        await restarted.start()
        try:
            if restarted._requeue_task is not None:
                await asyncio.gather(restarted._requeue_task, return_exceptions=True)
            # Any re-queued run finishes before shutdown.
            await asyncio.gather(*[t for t in tasks if t.get_name().startswith("auto-approved-")],
                                 return_exceptions=True)
        finally:
            await restarted.shutdown()
            await _drain(tasks)
        assert fake.calls == []
        assert database.get_automation_run("run-off")["status"] == "approved"
        old = database.get_automation_run("run-old")
        assert old["status"] == "cancelled"
        assert "expired" in (old.get("error") or "")

    async def test_approval_after_the_app_began_shutting_down_starts_nothing(
        self, manager, mock_db, runner, monkeypatch
    ):
        """R10-14: the approve route's background start checked only the
        manager's own flag, set last in shutdown; an approval in the window
        after ``startup.is_shutting_down`` minted a session and a queued turn
        nobody would run. The row stays ``approved`` for the next boot."""
        from app.core import startup
        from app.db import database

        _make_automation("auto-ap", triggers=_WEBHOOK_NO_SECRET)
        database.create_automation_run(run_id="run-ap", automation_id="auto-ap",
                                       status="approved", prompt_rendered="approved prompt")
        database.create_automation_approval(approval_id="appr-ap", run_id="run-ap", automation_id="auto-ap")
        database.update_automation_approval("appr-ap", status="approved")
        tasks = _capture_tasks(manager)
        monkeypatch.setattr(startup, "is_shutting_down", True)

        await manager.execute_approved_run("appr-ap")
        await _drain(tasks)
        for _ in range(20):
            await asyncio.sleep(0)

        assert runner.calls == []
        assert database.get_automation_run("run-ap")["status"] == "approved"
        assert manager._running == {}

    async def test_approved_run_queued_behind_the_latch_stays_put_once_the_app_shuts_down(
        self, manager, mock_db, runner, monkeypatch
    ):
        """R10-14: the queued approved run woke to a free latch after the app
        flagged shutdown (the manager's own flag not yet set) and started."""
        from app.core import startup
        from app.db import database

        g_first = asyncio.Event()
        runner.gates = [g_first]
        _make_automation("auto-aq", triggers=_WEBHOOK_NO_SECRET)
        database.create_automation_run(run_id="run-aq", automation_id="auto-aq",
                                       status="approved", prompt_rendered="approved prompt")
        database.create_automation_approval(approval_id="appr-aq", run_id="run-aq", automation_id="auto-aq")
        database.update_automation_approval("appr-aq", status="approved")
        tasks = _capture_tasks(manager)

        first = asyncio.create_task(manager._execute("auto-aq", {"trigger": "manual"}))
        await _until(lambda: len(runner.calls) == 1)
        assert (await manager.execute_approved_run("appr-aq"))["id"] == "run-aq"
        for _ in range(20):
            await asyncio.sleep(0)
        monkeypatch.setattr(startup, "is_shutting_down", True)
        g_first.set()
        await first
        await _drain(tasks)
        for _ in range(20):
            await asyncio.sleep(0)

        assert len(runner.calls) == 1, "the approved run must not start during shutdown"
        assert database.get_automation_run("run-aq")["status"] == "approved"
        assert manager._running == {}

    async def test_execute_run_claiming_an_approved_row_refuses_during_shutdown(
        self, manager, mock_db, runner, monkeypatch
    ):
        """R10-14: ``_execute_run(claim_from="approved")`` is the last gate
        before the run moves to ``running``; it refuses once shutdown began."""
        from app.core import startup
        from app.db import database

        _make_automation("auto-ex", triggers=_WEBHOOK_NO_SECRET)
        database.create_automation_run(run_id="run-ex", automation_id="auto-ex",
                                       status="approved", prompt_rendered="approved prompt")
        monkeypatch.setattr(startup, "is_shutting_down", True)

        await manager._execute_run(database.get_automation("auto-ex"), "run-ex",
                                   "approved prompt", {}, claim_from="approved")

        assert runner.calls == []
        assert database.get_automation_run("run-ex")["status"] == "approved"

    async def test_requeue_starts_nothing_once_the_app_began_shutting_down(
        self, mock_db, paused_scheduler_class, monkeypatch
    ):
        """R10-14: the start-up re-queue checked only the manager's flag."""
        from app.core import startup
        from app.db import database

        monkeypatch.setattr(
            "app.core.automation_service.should_run_scheduled_here", lambda a: (True, "elected"),
        )
        _make_automation("auto-rs", triggers=_WEBHOOK_NO_SECRET)
        database.create_automation_run(run_id="run-rs", automation_id="auto-rs",
                                       status="approved", prompt_rendered="p")
        database.create_automation_approval(approval_id="appr-rs", run_id="run-rs", automation_id="auto-rs")
        database.update_automation_approval("appr-rs", status="approved")
        monkeypatch.setattr(startup, "is_shutting_down", True)

        restarted = AutomationManager()
        fake = _FakeRunner()
        restarted._run_prompt_as_session = fake
        tasks = _capture_tasks(restarted)
        await restarted._requeue_approved_runs()
        await _drain(tasks)
        for _ in range(20):
            await asyncio.sleep(0)

        assert fake.calls == []
        assert database.get_automation_run("run-rs")["status"] == "approved"

    async def test_update_automation_run_compare_and_set(self, mock_db):
        from app.db import database

        _make_automation("auto-cas", triggers=_WEBHOOK_NO_SECRET)
        database.create_automation_run(run_id="run-cas", automation_id="auto-cas", status="approved")
        assert database.update_automation_run("run-cas", status="running", expected_status="approved")
        assert database.update_automation_run("run-cas", status="running", expected_status="approved") is None
        assert database.get_automation_run("run-cas")["status"] == "running"


# =============================================================================
# Webhook signature verification
# =============================================================================


WEBHOOK_SECRET = "s3cret-value"


def _sign(payload, secret=WEBHOOK_SECRET):
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return "sha256=" + hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


@pytest.fixture
def enc_webhook():
    """Reversible fake encryption that uses the real ``enc:`` prefix."""
    with patch("app.core.encryption.encrypt_value", side_effect=lambda v: f"enc:{v[::-1]}"), \
         patch("app.core.encryption.decrypt_value", side_effect=lambda v: v[len("enc:"):][::-1]), \
         patch("app.core.encryption.is_encryption_ready", return_value=True) as ready:
        yield ready


class TestWebhookSignature:
    @pytest.fixture
    async def signed_automation(self, mock_db, enc_webhook):
        from app.core import automation_service

        auto = await automation_service.create_automation(
            name="Signed", trigger_type="webhook", trigger_config={"secret": WEBHOOK_SECRET},
            profile_id="default", prompt="Build {{payload.ref}}", enabled=True,
        )
        # Stored encrypted, never in plaintext.
        assert auto["triggers"][0]["config"]["secret"] == f"enc:{WEBHOOK_SECRET[::-1]}"
        return auto["id"]

    async def test_valid_signature_starts_a_run(self, manager, runner, signed_automation):
        tasks = _capture_tasks(manager)
        payload = {"ref": "main", "after": "abc123"}
        result = await manager.trigger_webhook(signed_automation, payload, _sign(payload))
        assert result == {"automation_id": signed_automation, "status": "triggered"}
        await _drain(tasks)
        assert [c["prompt"] for c in runner.calls] == ["Build main"]
        (run,) = _runs(signed_automation)
        assert run["trigger_data"] == {"trigger": "webhook", "payload": payload}

    async def test_signature_is_over_sorted_compact_json(self, manager, runner, signed_automation):
        tasks = _capture_tasks(manager)
        signature = _sign({"b": 2, "a": 1})
        assert await manager.trigger_webhook(signed_automation, {"a": 1, "b": 2}, signature) is not None
        await _drain(tasks)
        assert len(runner.calls) == 1

    @pytest.mark.parametrize("make_signature", [
        pytest.param(lambda p: _sign(p, secret="wrong-secret"), id="wrong-secret"),
        pytest.param(lambda p: _sign({**p, "ref": "evil"}), id="signed-other-payload"),
        pytest.param(lambda p: _sign(p)[len("sha256="):], id="missing-sha256-prefix"),
        pytest.param(lambda p: None, id="missing-header"),
        pytest.param(lambda p: "", id="empty-header"),
    ])
    async def test_bad_signatures_are_rejected_without_a_run(
        self, manager, runner, signed_automation, make_signature
    ):
        """Mutation-checked: with ``hmac.compare_digest`` forced to True the
        wrong-secret / other-payload / prefix cases are accepted and fail."""
        tasks = _capture_tasks(manager)
        payload = {"ref": "main"}
        assert await manager.trigger_webhook(signed_automation, payload, make_signature(payload)) is None
        await _drain(tasks)
        assert tasks == []
        assert runner.calls == []
        assert _runs(signed_automation) == []

    async def test_undecryptable_secret_fails_closed(self, manager, runner, signed_automation, enc_webhook):
        enc_webhook.return_value = False  # key unavailable: secret stays ciphertext
        payload = {"ref": "main"}
        assert await manager.trigger_webhook(signed_automation, payload, _sign(payload)) is None
        assert await manager.trigger_webhook(signed_automation, payload, None) is None
        assert runner.calls == []

    async def test_webhook_without_a_secret_accepts_unsigned_calls(self, manager, mock_db, runner):
        tasks = _capture_tasks(manager)
        _make_automation("auto-open", triggers=_WEBHOOK_NO_SECRET)
        assert await manager.trigger_webhook("auto-open", {"x": 1}) is not None
        await _drain(tasks)
        assert len(runner.calls) == 1

    @pytest.mark.xfail(strict=True, reason=(
        "No replay protection: the signature covers only the JSON body (no "
        "timestamp/nonce), so a captured request can be re-sent verbatim."
    ))
    async def test_replayed_request_is_rejected(self, manager, runner, signed_automation):
        tasks = _capture_tasks(manager)
        payload = {"ref": "main"}
        signature = _sign(payload)
        assert await manager.trigger_webhook(signed_automation, payload, signature) is not None
        await _drain(tasks)
        assert await manager.trigger_webhook(signed_automation, payload, signature) is None


# =============================================================================
# Output routing
# =============================================================================


class TestOutputRouting:
    async def test_connector_output_sends_the_response(self, manager, mock_db, runner, connectors):
        from app.core.connectors.base import OutgoingMessage

        connectors["c-1"] = _FakeConnector()
        runner.response = "Daily digest"
        _make_automation("auto-out", triggers=_WEBHOOK_NO_SECRET, output_config={
            "type": "connector", "connector_id": "c-1", "platform_chat_id": "chat-1",
        })
        await manager._execute("auto-out", {"trigger": "manual"})
        assert connectors["c-1"].messages == [OutgoingMessage(platform_chat_id="chat-1", text="Daily digest")]
        (run,) = _runs("auto-out")
        assert (run["status"], run["output_status"]) == ("completed", "delivered")

    @pytest.mark.parametrize("output_config,error_fragment", [
        ({"type": "connector", "connector_id": "c-off", "platform_chat_id": "chat-1"}, "not running"),
        ({"type": "connector", "connector_id": "c-1"}, "missing connector_id or platform_chat_id"),
        ({"type": "webhook"}, "missing url"),
    ])
    async def test_delivery_failure_marks_output_failed_but_run_completed(
        self, manager, mock_db, runner, connectors, output_config, error_fragment
    ):
        connectors["c-1"] = _FakeConnector()
        _make_automation("auto-out", triggers=_WEBHOOK_NO_SECRET, output_config=output_config)
        await manager._execute("auto-out", {"trigger": "manual"})
        (run,) = _runs("auto-out")
        assert run["status"] == "completed"
        assert run["output_status"] == "failed"
        assert error_fragment in run["output_error"]
        assert _auto("auto-out")["last_error"] is None

    async def test_webhook_output_posts_json(self, manager, mock_db, runner, http_client):
        runner.response = "report body"
        _make_automation("auto-post", triggers=_WEBHOOK_NO_SECRET, output_config={
            "type": "webhook", "url": "https://hooks.example.test/in", "headers": {"X-Token": "t"},
        })
        await manager._execute("auto-post", {"trigger": "manual"})
        (post,) = http_client.posts
        assert post["url"] == "https://hooks.example.test/in"
        assert post["headers"] == {"X-Token": "t", "Content-Type": "application/json"}
        body = json.loads(post["content"])
        assert body["automation_id"] == "auto-post"
        assert body["automation_name"] == "Automation auto-post"
        assert body["response"] == "report body"
        assert _runs("auto-post")[0]["output_status"] == "delivered"

    async def test_webhook_output_http_error_marks_output_failed(self, manager, mock_db, runner, http_client):
        http_client.status_code = 502
        _make_automation("auto-post", triggers=_WEBHOOK_NO_SECRET,
                         output_config={"type": "webhook", "url": "https://hooks.example.test/in"})
        await manager._execute("auto-post", {"trigger": "manual"})
        (run,) = _runs("auto-post")
        assert run["output_status"] == "failed"
        assert "HTTP 502" in run["output_error"]

    async def test_suppress_pattern_skips_delivery(self, manager, mock_db, runner, connectors):
        connectors["c-1"] = _FakeConnector()
        runner.response = "  heartbeat_ok \n"
        _make_automation("auto-hb", triggers=_WEBHOOK_NO_SECRET, output_config={
            "type": "connector", "connector_id": "c-1", "platform_chat_id": "chat-1",
            "suppress_pattern": "HEARTBEAT_OK",
        })
        await manager._execute("auto-hb", {"trigger": "manual"})
        assert connectors["c-1"].messages == []
        assert _runs("auto-hb")[0]["output_status"] == "suppressed"

    @pytest.mark.parametrize("response", [
        pytest.param("same answer", id="short"),
        pytest.param("x" * 3000, id="longer-than-stored-tail"),
    ])
    async def test_identical_response_within_window_is_deduplicated(
        self, manager, mock_db, runner, connectors, response
    ):
        """Regression: the dedup window never fired. The stored completion time
        was parsed as a naive datetime and subtracted from an aware ``now``
        (TypeError, swallowed), and the comparison used the full response
        while the run row stores only its 2000-char tail."""
        connectors["c-1"] = _FakeConnector()
        runner.response = response
        _make_automation("auto-dd", triggers=_WEBHOOK_NO_SECRET, output_config={
            "type": "connector", "connector_id": "c-1", "platform_chat_id": "chat-1",
            "deduplicate_window_seconds": 3600,
        })
        await manager._execute("auto-dd", {"trigger": "manual"})
        await manager._execute("auto-dd", {"trigger": "manual"})

        assert len(connectors["c-1"].messages) == 1
        statuses = [r["output_status"] for r in sorted(_runs("auto-dd"), key=lambda r: r["created_at"])]
        assert statuses == ["delivered", "deduplicated"]

    async def test_dedup_ignores_old_or_different_responses(self, manager, mock_db):
        from app.db import database

        _make_automation("auto-dd2", triggers=_WEBHOOK_NO_SECRET)
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        database.create_automation_run(run_id="run-old", automation_id="auto-dd2", status="completed")
        database.update_automation_run("run-old", response="same", output_status="delivered", completed_at=old)
        database.create_automation_run(run_id="run-other", automation_id="auto-dd2", status="completed")
        database.update_automation_run("run-other", response="different", output_status="delivered",
                                       completed_at=datetime.now(timezone.utc).isoformat())
        database.create_automation_run(run_id="run-bad-ts", automation_id="auto-dd2", status="completed")
        database.update_automation_run("run-bad-ts", response="same", output_status="delivered",
                                       completed_at="not a timestamp")
        database.create_automation_run(run_id="run-new", automation_id="auto-dd2", status="completed")

        automation = _auto("auto-dd2")
        automation["output_config"] = {"type": "none", "deduplicate_window_seconds": 3600}
        await manager._deliver_output(automation, "run-new", "same")

        assert database.get_automation_run("run-new")["output_status"] == "delivered"
