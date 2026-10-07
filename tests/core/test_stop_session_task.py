"""
Tests for stopping ONE in-flight background task from the UI.

Covers:
- app.mcp.builtin_tools.stop_background_shell_task — terminates a tracked
  Monitor subprocess, marks it killed in the registry, and returns None for an
  unknown id (so callers fall through to SDK agent-task handling).
- app.core.query_session_lifecycle.stop_session_task — routes shell tasks to the
  shell helper and reaps agent tasks in the registry; handles a missing /
  disconnected session gracefully (never raises).

No Postgres or live engine is needed: the session pool is mocked.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import app.mcp.builtin_tools as bt
from app.mcp.builtin_tools import stop_background_shell_task
from app.core.query_session_lifecycle import (
    stop_all_background_tasks,
    stop_session_task,
)
from app.core.task_registry import (
    USER_STOP_REASON,
    USER_STOP_SUMMARY,
    task_registry,
)


def _fake_shell_task(task_id: str) -> bt._BackgroundTask:
    """A _BackgroundTask whose process terminates cleanly when asked."""
    task = bt._BackgroundTask(task_id=task_id, command="sleep 999", cwd=None)
    proc = MagicMock()
    proc.returncode = None

    def _terminate():
        proc.returncode = -15

    proc.terminate.side_effect = _terminate
    proc.wait = AsyncMock(return_value=-15)
    proc.kill = MagicMock()
    task.process = proc
    return task


@pytest.fixture(autouse=True)
def _clean_state():
    task_registry.reset_all()
    bt._background_tasks.clear()
    yield
    task_registry.reset_all()
    bt._background_tasks.clear()


# =============================================================================
# stop_background_shell_task
# =============================================================================

class TestStopBackgroundShellTask:
    @pytest.mark.asyncio
    async def test_unknown_id_returns_none(self):
        assert await stop_background_shell_task("nope", session_id="s1") is None

    @pytest.mark.asyncio
    async def test_terminates_and_marks_killed(self):
        task_registry.start_task("s1", "sh1", "shell", "sleep 999")
        task = _fake_shell_task("sh1")
        bt._background_tasks["sh1"] = task

        result = await stop_background_shell_task("sh1", session_id="s1")

        assert result is not None
        assert result["kind"] == "shell"
        assert result["task_id"] == "sh1"
        # Process was asked to terminate and is no longer running.
        task.process.terminate.assert_called_once()
        assert result["running"] is False
        # Registry flipped to the terminal 'killed' status -> drops from active.
        assert task_registry.get_active("s1") == []
        rec = task_registry.get_task("s1", "sh1")
        assert rec is not None and rec["status"] == "killed"

    @pytest.mark.asyncio
    async def test_escalates_to_kill_on_timeout(self):
        task = _fake_shell_task("sh2")
        # wait() never returns -> wait_for raises TimeoutError -> kill() path.
        task.process.wait = AsyncMock(side_effect=[__import__("asyncio").TimeoutError(), -9])
        bt._background_tasks["sh2"] = task

        await stop_background_shell_task("sh2", session_id="s1")
        task.process.kill.assert_called_once()


# =============================================================================
# stop_session_task
# =============================================================================

class TestStopSessionTask:
    @pytest.mark.asyncio
    async def test_routes_shell_task_to_shell_helper(self):
        task_registry.start_task("s1", "sh1", "shell", "sleep 999")
        bt._background_tasks["sh1"] = _fake_shell_task("sh1")

        result = await stop_session_task("s1", "sh1")

        assert result["ok"] is True
        assert result["kind"] == "shell"

    @pytest.mark.asyncio
    async def test_live_agent_task_is_reaped_in_the_registry(self):
        """The adapter has no per-task stop: a running agent row on a live
        session is settled ``killed`` in the registry, attributed to the user."""
        task_registry.start_task("s1", "agent-123", "agent", "bg work")
        fake_pool = MagicMock()
        fake_pool.peek.return_value = MagicMock(is_connected=True)

        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-123")

        assert result["ok"] is True
        assert result["kind"] == "agent"
        rec = task_registry.get_task("s1", "agent-123")
        assert rec["status"] == "killed"
        assert rec["summary"] == USER_STOP_SUMMARY
        assert rec["reason"] == USER_STOP_REASON

    @pytest.mark.asyncio
    async def test_host_stop_of_live_agent_task_names_the_host(self):
        task_registry.start_task("s1", "agent-456", "agent", "bg work")
        fake_pool = MagicMock()
        fake_pool.peek.return_value = MagicMock(is_connected=True)

        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-456", source="parent_turn_ended")

        assert result["ok"] is True
        rec = task_registry.get_task("s1", "agent-456")
        assert rec["status"] == "killed"
        assert rec["reason"] == "parent_turn_ended"
        assert rec["summary"] == "Stopped by host: parent_turn_ended"

    @pytest.mark.asyncio
    async def test_agent_task_on_a_gone_session_is_reaped_unattributed(self):
        task_registry.start_task("s1", "agent-789", "agent", "bg work")
        fake_pool = MagicMock()
        fake_pool.peek.return_value = None

        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-789")

        assert result["ok"] is True
        assert result["detail"] == "reaped (session gone)"
        rec = task_registry.get_task("s1", "agent-789")
        assert rec["status"] == "killed"
        assert rec.get("reason") is None

    @pytest.mark.asyncio
    async def test_agent_task_session_not_active(self):
        fake_pool = MagicMock()
        fake_pool.peek.return_value = None
        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-x")
        assert result["ok"] is False
        assert "not active" in result["detail"]

    @pytest.mark.asyncio
    async def test_agent_task_session_not_connected(self):
        fake_state = MagicMock(is_connected=False)
        fake_pool = MagicMock()
        fake_pool.peek.return_value = fake_state
        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-x")
        assert result["ok"] is False
        assert "not connected" in result["detail"]

    @pytest.mark.asyncio
    async def test_unknown_agent_task_on_live_session(self):
        fake_pool = MagicMock()
        fake_pool.peek.return_value = MagicMock(is_connected=True)
        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-x")
        assert result["ok"] is False
        assert result["detail"] == "task not found"


# =============================================================================
# stop_session_task — "ghost" task reaping (Bug 4)
#
# Tasks terminated by outside forces (idle eviction, process death, session
# teardown) can be left parked "running" in the registry with no live client to
# stop them. Clicking the UI's "x" must REAP such ghosts — mark them killed so
# they drop off the next /live poll — and report success, rather than returning
# ok:false and leaving the strip showing a task whose close button is stuck
# disabled forever.
# =============================================================================

class TestStopSessionTaskReapsGhosts:
    @pytest.mark.asyncio
    async def test_agent_ghost_reaped_when_session_gone(self):
        # A backgrounded sub-agent still 'running' in the registry, but the live
        # session is gone from the pool (peek -> None).
        task_registry.start_task("s1", "agent-ghost", "agent", "bg work")
        fake_pool = MagicMock()
        fake_pool.peek.return_value = None
        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-ghost")

        assert result["ok"] is True
        assert result["kind"] == "agent"
        assert "reaped" in result["detail"]
        # Marked terminal -> drops off the next /live poll.
        assert task_registry.get_active("s1") == []
        rec = task_registry.get_task("s1", "agent-ghost")
        assert rec is not None and rec["status"] == "killed"

    @pytest.mark.asyncio
    async def test_agent_ghost_reaped_when_session_disconnected(self):
        task_registry.start_task("s1", "agent-ghost", "agent", "bg work")
        fake_state = MagicMock(is_connected=False)
        fake_pool = MagicMock()
        fake_pool.peek.return_value = fake_state
        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-ghost")

        assert result["ok"] is True
        assert "reaped" in result["detail"]
        assert task_registry.get_active("s1") == []

    @pytest.mark.asyncio
    async def test_shell_ghost_reaped_when_subprocess_untracked(self):
        # A registry shell task whose subprocess already exited (not in
        # _background_tasks) — the shell helper returns None and we must reap it
        # rather than mis-route a shell id to the SDK's agent stop.
        task_registry.start_task("s1", "sh-ghost", "shell", "sleep 999")
        # Deliberately NOT adding to bt._background_tasks -> untracked ghost.
        result = await stop_session_task("s1", "sh-ghost")

        assert result["ok"] is True
        assert result["kind"] == "shell"
        assert "reaped" in result["detail"]
        rec = task_registry.get_task("s1", "sh-ghost")
        assert rec is not None and rec["status"] == "killed"

    @pytest.mark.asyncio
    async def test_already_terminal_task_is_idempotent_success(self):
        # The registry already has the task in a terminal state (e.g. a prior
        # kill that the UI hasn't polled yet). Closing it again must succeed so
        # the stale row clears — without touching the session pool at all.
        task_registry.start_task("s1", "agent-done", "agent", "bg work")
        task_registry.complete_task("s1", "agent-done", "killed")

        called = {"peek": False}
        fake_pool = MagicMock()

        def _peek(*a, **k):
            called["peek"] = True
            return None

        fake_pool.peek.side_effect = _peek
        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "agent-done")

        assert result["ok"] is True
        assert "already finished" in result["detail"]
        # Short-circuited before any pool/SDK interaction.
        assert called["peek"] is False

    @pytest.mark.asyncio
    async def test_unknown_task_with_dead_session_still_reports_honestly(self):
        # No registry record AND no live session AND no durable truth -> nothing
        # to reap, nothing the UI is showing. Preserve the honest ok:false so
        # callers aren't misled.
        fake_pool = MagicMock()
        fake_pool.peek.return_value = None
        with patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=fake_pool,
        ):
            result = await stop_session_task("s1", "never-seen")
        assert result["ok"] is False
        assert "not active" in result["detail"]


# =============================================================================
# stop_session_task — durable force-settle fallback (post-restart)
#
# After a restart the in-memory registry is EMPTY, so a task the UI still shows
# "running" has no registry row to reap and the old code returned a hard
# ok:false "session not active" — leaving the strip stuck forever. The fallback
# settles the task from durable truth (background_tasks row, else a
# subagent.started event) AND emits the terminal task.updated frame the FE
# replays to settle the chip/card.
# =============================================================================

class TestStopSessionTaskDurableFallback:
    def _dead_pool(self):
        fake_pool = MagicMock()
        fake_pool.peek.return_value = None
        return fake_pool

    def _seed_bg(self, session_id, task_id, status, *, kind="agent",
                 tool_use_id=None):
        from app.db.background_tasks import upsert_background_task
        upsert_background_task({
            "task_id": task_id,
            "session_id": session_id,
            "kind": kind,
            "tool_use_id": tool_use_id,
            "title": task_id,
            "status": status,
        })

    @pytest.mark.asyncio
    async def test_nonterminal_durable_row_reaped_and_frame_emitted(self, mock_db):
        import app.core.turn_recorder as tr
        from app.db.background_tasks import get_background_task

        self._seed_bg("s1", "d-1", "running", tool_use_id="toolu_d1")

        calls = []

        async def _capture(session_id, event):
            calls.append((session_id, event))

        with patch.object(tr, "record_drained_event", _capture), patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=self._dead_pool(),
        ):
            result = await stop_session_task("s1", "d-1")

        assert result["ok"] is True
        assert result["kind"] == "agent"
        assert "reaped (stale row)" in result["detail"]
        # Row flipped to killed.
        assert get_background_task("d-1", "s1")["status"] == "killed"
        # Terminal frame emitted with the join key.
        assert len(calls) == 1
        sid_arg, ev = calls[0]
        assert sid_arg == "s1"
        assert ev["subtype"] == "task_updated"
        assert ev["data"]["task_id"] == "d-1"
        assert ev["data"]["status"] == "killed"
        assert ev["data"]["patch"] == {"status": "killed"}
        assert ev["data"]["reason"] == "stale_reap"
        assert ev["data"]["tool_use_id"] == "toolu_d1"

    @pytest.mark.asyncio
    async def test_already_terminal_row_not_reupdated_frame_uses_truth(self, mock_db):
        import app.core.turn_recorder as tr
        import app.db.background_tasks as bt
        from app.db.background_tasks import get_background_task

        self._seed_bg("s1", "d-2", "error", tool_use_id="toolu_d2")

        calls = []

        async def _capture(session_id, event):
            calls.append(event)

        with patch.object(tr, "record_drained_event", _capture), patch.object(
            bt, "mark_background_task_killed"
        ) as mk, patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=self._dead_pool(),
        ):
            result = await stop_session_task("s1", "d-2")

        assert result["ok"] is True
        # No re-update of an already-terminal row.
        mk.assert_not_called()
        assert get_background_task("d-2", "s1")["status"] == "error"
        # Frame settles the UI to the row's actual terminal status.
        assert len(calls) == 1
        assert calls[0]["data"]["status"] == "error"
        assert calls[0]["data"]["patch"] == {"status": "error"}

    @pytest.mark.asyncio
    async def test_no_row_no_event_reports_honest_failure(self, mock_db):
        import app.core.turn_recorder as tr

        calls = []

        async def _capture(session_id, event):
            calls.append(event)

        with patch.object(tr, "record_drained_event", _capture), patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=self._dead_pool(),
        ):
            result = await stop_session_task("s1", "ghost-x")

        assert result["ok"] is False
        assert "not active" in result["detail"]
        assert calls == []

    @pytest.mark.asyncio
    async def test_orphan_subagent_from_event_log_reaped(self, mock_db):
        import app.core.turn_recorder as tr
        from app.db import session_events as se

        se.append_events("s1", [
            {"type": "subagent.started", "payload": {"task_id": "sub-1"}},
        ])

        calls = []

        async def _capture(session_id, event):
            calls.append((session_id, event))

        with patch.object(tr, "record_drained_event", _capture), patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=self._dead_pool(),
        ):
            result = await stop_session_task("s1", "sub-1")

        assert result["ok"] is True
        assert result["kind"] == "agent"
        assert "reaped (orphan subagent)" in result["detail"]
        assert len(calls) == 1
        _sid, ev = calls[0]
        assert ev["subtype"] == "task_updated"
        assert ev["data"]["task_id"] == "sub-1"
        # Subagent cards join on tool_use_id == launch task_id.
        assert ev["data"]["tool_use_id"] == "sub-1"
        assert ev["data"]["status"] == "killed"
        assert ev["data"]["reason"] == "stale_reap"

    @pytest.mark.asyncio
    async def test_double_stop_row_emits_frame_once(self, mock_db):
        """Repeated stop clicks on a durable row settle the UI exactly once:
        the second call emits nothing and reports 'already settled'."""
        import app.core.turn_recorder as tr

        self._seed_bg("s1", "d-idem", "running", tool_use_id="toolu_idem")
        task_registry.clear_session("s1")

        calls = []

        async def _capture(session_id, event):
            calls.append(event)

        with patch.object(tr, "record_drained_event", _capture), patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=self._dead_pool(),
        ):
            first = await stop_session_task("s1", "d-idem")
            second = await stop_session_task("s1", "d-idem")

        assert first["ok"] is True
        assert "reaped (stale row)" in first["detail"]
        assert second["ok"] is True
        assert second["kind"] == "agent"
        assert second["detail"] == "already settled"
        # The frame was emitted exactly once across both stop clicks.
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_double_stop_orphan_emits_frame_once(self, mock_db):
        """Same once-only settle for the orphan-subagent (event-log) branch."""
        import app.core.turn_recorder as tr
        from app.db import session_events as se

        se.append_events("s1", [
            {"type": "subagent.started", "payload": {"task_id": "sub-idem"}},
        ])
        task_registry.clear_session("s1")

        calls = []

        async def _capture(session_id, event):
            calls.append(event)

        with patch.object(tr, "record_drained_event", _capture), patch(
            "app.core.query_session_lifecycle.get_session_pool",
            return_value=self._dead_pool(),
        ):
            first = await stop_session_task("s1", "sub-idem")
            second = await stop_session_task("s1", "sub-idem")

        assert first["ok"] is True
        assert "reaped (orphan subagent)" in first["detail"]
        assert second["ok"] is True
        assert second["detail"] == "already settled"
        assert len(calls) == 1


class TestTerminalTaskStop:
    def _child(self):
        from app.core.config import settings
        from app.db import database, turns

        database.register_install(settings.effective_instance_id, "test", None, "local", "1", {}, approve_new=True)
        database.create_session(session_id="parent", profile_id="default")
        database.create_session(session_id="child", profile_id="default", parent_session_id="parent", is_worker=True)
        task_registry.start_task("parent", "child", "agent", "original task")
        original = turns.enqueue_turn("child", "provider_agent", prompt="original")
        assert turns.start_turn(original["id"])
        assert turns.settle_turn(original["id"], "completed")
        task_registry.complete_task("parent", "child", "completed", summary="Finished report", report_path="/tmp/report")
        return turns

    @pytest.mark.asyncio
    @pytest.mark.parametrize("state", ["queued", "running", "parked", "interrupting", "draining"])
    async def test_terminal_report_with_real_automatic_work_is_stoppable(self, mock_db, state):
        from app.core.query_session_lifecycle import terminal_task_stop_state
        from app.core import turn_manager
        turns = self._child()
        report = task_registry.get_task("parent", "child")
        wake = turns.enqueue_turn("child", "unpark", prompt="a sibling finished")
        if state != "queued":
            assert turns.start_turn(wake["id"])
            if state == "parked":
                assert turns.park_turn(wake["id"], task_ids=["sibling"])
            elif state in ("interrupting", "draining"):
                assert turns.interrupt_turn("child", "send_now", turn_id=wake["id"])
                if state == "draining":
                    assert turns.escalate_to_draining(wake["id"])
        assert await terminal_task_stop_state("parent", "child") == {"eligible": True, "turn_id": wake["id"]}
        with patch.object(turn_manager, "_fire_interrupt_executor") as fire:
            result = await stop_session_task("parent", "child", expected_turn_id=wake["id"])
        assert result["ok"]
        assert task_registry.get_task("parent", "child") == report
        assert task_registry.is_user_stopped("child")
        live = state in ("running", "interrupting", "draining")
        expected_state = "draining" if state == "draining" else ("interrupting" if live else "aborted")
        assert turns.get_turn(wake["id"])["state"] == expected_state
        if live:
            fire.assert_called_once_with("child", "stop", wake["id"])
            assert turns.get_turn(wake["id"])["interrupt_mode"] == "stop"
        else:
            fire.assert_not_called()
        assert turns.enqueue_turn("child", "unpark", prompt="late terminal")["id"] is None
        # The durable guard still refuses a late producer after memory reset.
        task_registry.reset_all()
        assert turns.enqueue_turn("child", "retry", prompt="late retry")["id"] is None
        resumed = turns.enqueue_turn("child", "user", prompt="try again")
        assert resumed["id"]
        assert not task_registry.is_user_stopped("child")
        assert turns.enqueue_turn("child", "unpark", prompt="new work")["id"]

    @pytest.mark.asyncio
    async def test_actual_failed_wake_retry_producer_is_stoppable(self, mock_db):
        from app.core import turn_scheduler
        from app.core.query_session_lifecycle import terminal_task_stop_state
        turns = self._child()
        report = task_registry.get_task("parent", "child")
        dead = turns.enqueue_turn("child", "unpark", prompt="read the finished report")
        assert turns.start_turn(dead["id"])
        assert turns.settle_turn(dead["id"], "aborted", "error", error="fixture reader failure")
        with (
            patch.object(turn_scheduler, "_clear_wake_pending"),
            patch.object(turn_scheduler, "_drop_pooled_client", new=AsyncMock()),
            patch.object(turn_scheduler, "note_wake_pending"),
            patch.object(turn_scheduler, "kick") as kick,
        ):
            await turn_scheduler.on_turn_settled("child", dead["id"], "aborted")
            (retry,) = turns.get_queued_turns("child")
            assert retry["parent_turn_id"] == dead["id"]
            assert retry["payload"][turn_scheduler.WAKE_RETRY_KEY]
            assert retry["position"] < dead["position"]
            assert await terminal_task_stop_state("parent", "child") == {"eligible": True, "turn_id": retry["id"]}
            assert (await stop_session_task("parent", "child", expected_turn_id=retry["id"]))["ok"]
            assert turns.get_turn(retry["id"])["state"] == "aborted"
            assert not await turn_scheduler._maybe_recover_failed_wake("child", turns.get_turn(dead["id"]))
            kick.assert_called_once_with("child")
        assert turns.get_queued_turns("child") == []
        assert task_registry.get_task("parent", "child") == report

    @pytest.mark.asyncio
    async def test_front_retry_is_stoppable_and_late_stop_spares_its_successor(self, mock_db):
        from app.core.query_session_lifecycle import terminal_task_stop_state
        turns = self._child()
        retry = turns.enqueue_turn("child", "unpark", at_front=True)
        assert await terminal_task_stop_state("parent", "child") == {"eligible": True, "turn_id": retry["id"]}
        assert turns.start_turn(retry["id"])
        assert turns.settle_turn(retry["id"], "completed")
        successor = turns.enqueue_turn("child", "unpark", at_front=True)
        assert turns.start_turn(successor["id"])
        assert not (await stop_session_task("parent", "child", expected_turn_id=retry["id"]))["ok"]
        assert turns.get_turn(successor["id"])["state"] == "running"
        assert not task_registry.is_user_stopped("child")
        assert await terminal_task_stop_state("parent", "child") == {"eligible": True, "turn_id": successor["id"]}

    @pytest.mark.asyncio
    async def test_parked_snapshot_does_not_target_an_at_front_live_successor(self, mock_db):
        from app.core.query_session_lifecycle import terminal_task_stop_state
        turns = self._child()
        parked = turns.enqueue_turn("child", "unpark")
        assert turns.start_turn(parked["id"])
        assert turns.park_turn(parked["id"], task_ids=["sibling"])
        snapshot = await terminal_task_stop_state("parent", "child")
        successor = turns.enqueue_turn("child", "unpark", at_front=True)
        assert turns.start_turn(successor["id"])
        assert not (await stop_session_task("parent", "child", expected_turn_id=snapshot["turn_id"]))["ok"]
        assert turns.get_turn(successor["id"])["state"] == "running"
        assert not task_registry.is_user_stopped("child")
        assert await terminal_task_stop_state("parent", "child") == {"eligible": True, "turn_id": successor["id"]}

    @pytest.mark.asyncio
    async def test_genuinely_done_is_idempotent_and_wrong_parent_cannot_stop(self, mock_db):
        from app.core.query_session_lifecycle import terminal_task_stop_state
        turns = self._child()
        assert await terminal_task_stop_state("parent", "child") == {"eligible": False}
        result = await stop_session_task("parent", "child")
        assert result["ok"] and result["detail"] == "already finished"
        assert not task_registry.is_user_stopped("child")
        wake = turns.enqueue_turn("child", "unpark")
        assert await terminal_task_stop_state("other", "child") == {"eligible": False}
        assert not (await stop_session_task("other", "child", expected_turn_id=wake["id"]))["ok"]
        assert turns.get_turn(wake["id"])["state"] == "queued"

    @pytest.mark.asyncio
    async def test_stale_capability_and_new_owner_work_are_spared(self, mock_db):
        from app.core.query_session_lifecycle import terminal_task_stop_state
        turns = self._child()
        wake = turns.enqueue_turn("child", "unpark")
        newer = turns.enqueue_turn("child", "unpark")
        assert not (await stop_session_task("parent", "child", expected_turn_id=wake["id"]))["ok"]
        assert not task_registry.is_user_stopped("child")
        owner = turns.enqueue_turn("child", "user", prompt="unrelated next work")
        assert await terminal_task_stop_state("parent", "child") == {"eligible": False}
        assert not (await stop_session_task("parent", "child", expected_turn_id=newer["id"]))["ok"]
        assert turns.get_turn(owner["id"])["state"] == "queued"
        # Even a later automatic wake is not the historical task's authority.
        turns.enqueue_turn("child", "unpark")
        assert await terminal_task_stop_state("parent", "child") == {"eligible": False}

    @pytest.mark.asyncio
    async def test_owner_send_between_atomic_stop_and_executor_survives(self, mock_db, monkeypatch):
        from app.core import turn_manager
        turns = self._child()
        wake = turns.enqueue_turn("child", "unpark")
        assert turns.start_turn(wake["id"])
        original_stop = turns.stop_terminal_child
        owner = []

        def stop_then_owner(*args):
            result = original_stop(*args)
            owner.append(turns.enqueue_turn("child", "user", prompt="new instruction"))
            return result

        monkeypatch.setattr(turns, "stop_terminal_child", stop_then_owner)
        with patch.object(turn_manager, "_fire_interrupt_executor") as fire:
            assert (await stop_session_task("parent", "child", expected_turn_id=wake["id"]))["ok"]
        fire.assert_called_once_with("child", "stop", wake["id"])
        assert turns.get_turn(owner[0]["id"])["state"] == "queued"
        assert not task_registry.is_user_stopped("child")

    @pytest.mark.asyncio
    async def test_blocked_cwd_terminal_child_remains_stoppable(self, mock_db, tmp_path):
        from app.core.query_session_lifecycle import terminal_task_stop_state
        turns = self._child()
        wake = turns.enqueue_turn("child", "unpark")
        assert turns.start_turn(wake["id"])
        assert turns.block_missing_cwd("child", wake["id"], str(tmp_path / "gone"), "missing")
        turns.settle_turn(wake["id"], "aborted")
        assert (await terminal_task_stop_state("parent", "child"))["eligible"]
        assert (await stop_session_task("parent", "child", expected_turn_id=wake["id"]))["ok"]
        assert task_registry.get_task("parent", "child")["summary"] == "Finished report"


    def test_terminal_stop_http_contract_and_foreign_owner_denial(self, mock_db):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.auth import require_auth
        from app.api.sessions import router
        from app.db.base import get_db

        turns = self._child()
        wake = turns.enqueue_turn("child", "unpark")
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_auth] = lambda: "test"
        with TestClient(app) as client, patch(
            "app.api.sessions_helpers.get_api_user_from_request", return_value=None,
        ):
            url = "/api/v1/sessions/parent/tasks/child"
            assert client.get(url + "/stop-state").json() == {"eligible": True, "turn_id": wake["id"]}
            from app.db import database
            database.create_api_user("foreign", "Foreign", "fixture-hash")
            with get_db() as conn:
                conn.cursor().execute("UPDATE sessions SET api_user_id=%s WHERE id=%s", ("foreign", "child"))
            assert client.get(url + "/stop-state").json() == {"eligible": False}
            assert not client.post(url + "/stop", params={"expected_turn_id": wake["id"]}).json()["ok"]
            with get_db() as conn:
                conn.cursor().execute("UPDATE sessions SET api_user_id=NULL WHERE id=%s", ("child",))
            with patch("app.api.sessions_helpers.get_api_user_from_request", return_value={"id": "foreign"}):
                assert client.get(url + "/stop-state").status_code == 403
                assert client.post(url + "/stop", params={"expected_turn_id": wake["id"]}).status_code == 403
            assert turns.get_turn(wake["id"])["state"] == "queued"
            assert client.post(url + "/stop", params={"expected_turn_id": wake["id"]}).json()["ok"]
            assert turns.get_turn(wake["id"])["state"] == "aborted"

    @pytest.mark.asyncio
    async def test_worker_inherits_parent_api_owner_and_older_cwd_marker(self, mock_db, tmp_path):
        from app.core.query_session_lifecycle import terminal_task_stop_state
        from app.db.base import get_db
        turns = self._child()
        from app.db import database
        database.create_api_user("parent-key", "Parent", "fixture-hash")
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET api_user_id=%s WHERE id=%s", ("parent-key", "parent"))
        failing = turns.enqueue_turn("child", "unpark")
        assert turns.start_turn(failing["id"])
        later = turns.enqueue_turn("child", "unpark")
        assert turns.block_missing_cwd("child", failing["id"], str(tmp_path / "missing"), "missing")
        turns.settle_turn(failing["id"], "aborted")
        turns.remove_queued_turn(later["id"])
        assert await terminal_task_stop_state("parent", "child") == {"eligible": True, "turn_id": later["id"]}
        assert (await stop_session_task("parent", "child", expected_turn_id=later["id"]))["ok"]


class TestStopAllBackgroundTasks:
    async def test_suppresses_every_wake_before_first_cancellation(self):
        session_id = "stop-all-race"
        for task_id in ("agent-1", "shell-1"):
            task_registry.start_task(
                session_id, task_id,
                "shell" if task_id.startswith("shell") else "agent",
                task_id,
            )

        seen = []

        async def _stop(sid, task_id):
            # The whole snapshot must be suppressed before cancellation begins,
            # not one-at-a-time across awaits.
            seen.append((
                task_id,
                task_registry.is_wake_suppressed(sid, "agent-1"),
                task_registry.is_wake_suppressed(sid, "shell-1"),
            ))
            return {"ok": True, "task_id": task_id}

        try:
            with patch(
                "app.core.turn_scheduler.discard_queued_task_wakes",
                return_value=0,
            ) as discard, patch(
                "app.core.query_session_lifecycle.stop_session_task",
                new=AsyncMock(side_effect=_stop),
            ) as stop_one:
                result = await stop_all_background_tasks(session_id)

            assert {row[0] for row in seen} == {"agent-1", "shell-1"}
            assert all(
                agent_suppressed and shell_suppressed
                for _, agent_suppressed, shell_suppressed in seen
            )
            discard.assert_called_once()
            assert discard.call_args.args[0] == session_id
            assert set(discard.call_args.args[1]) == {"agent-1", "shell-1"}
            assert stop_one.await_count == 2
            assert {row["task_id"] for row in result} == {"agent-1", "shell-1"}
        finally:
            task_registry.clear_session(session_id)
