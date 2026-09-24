"""
Tests for the in-memory background-task registry (app/core/task_registry.py),
the TaskList MCP builtin tool, and the /live `activeTasks` exposure.

Covers:
- start/update/complete lifecycle
- get_active vs get_all filtering & ordering (newest-first)
- to_wire shape (exact camelCase keys)
- clear_session and reset_all
- unknown-task no-ops (update/complete on a missing task never raise)
- the TaskList tool result envelope (running_count + wire tasks)
- the /live response carrying a top-level `activeTasks` field

No Postgres is required: the /live test drives the in-memory live-store path
(which skips all DB message loading) with require_session/auth mocked.
"""

import json
from unittest.mock import patch, AsyncMock

import pytest

from app.core.task_registry import (
    TaskRegistry,
    to_wire,
    task_registry,
    _COMPLETED_VISIBLE_WINDOW_SECONDS,
    _RECENT_COMPLETED_WINDOW_SECONDS,
    _RECENT_TERMINAL_WINDOW_SECONDS,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def reg():
    """A fresh, isolated registry per test."""
    return TaskRegistry()


# =============================================================================
# Lifecycle: start / update / complete
# =============================================================================

class TestLifecycle:
    def test_start_creates_running_record(self, reg):
        reg.start_task("s1", "t1", "agent", "Build the thing")
        active = reg.get_active("s1")
        assert len(active) == 1
        rec = active[0]
        assert rec["id"] == "t1"
        assert rec["subject"] == "Build the thing"
        assert rec["status"] == "running"
        assert rec["kind"] == "agent"
        assert rec["summary"] is None
        assert rec["startedAt"] is not None
        assert rec["updatedAt"] is not None

    def test_start_is_idempotent_update(self, reg):
        reg.start_task("s1", "t1", "agent", "First title")
        first = reg.get_active("s1")[0]
        started_at = first["startedAt"]
        # Re-start with a new title + summary acts as an update, preserving started_at.
        reg.start_task("s1", "t1", "agent", "Second title", summary="halfway")
        recs = reg.get_active("s1")
        assert len(recs) == 1
        assert recs[0]["subject"] == "Second title"
        assert recs[0]["summary"] == "halfway"
        assert recs[0]["startedAt"] == started_at
        assert recs[0]["status"] == "running"

    def test_update_changes_fields(self, reg):
        reg.start_task("s1", "t1", "agent", "Initial")
        reg.update_task("s1", "t1", title="Progress 50%", summary="working")
        rec = reg.get_active("s1")[0]
        assert rec["subject"] == "Progress 50%"
        assert rec["summary"] == "working"
        assert rec["status"] == "running"

    def test_complete_sets_terminal_status(self, reg):
        reg.start_task("s1", "t1", "shell", "npm test")
        reg.complete_task("s1", "t1", "completed")
        # No longer active...
        assert reg.get_active("s1") == []
        # ...but present in get_all with terminal status.
        all_tasks = reg.get_all("s1")
        assert len(all_tasks) == 1
        assert all_tasks[0]["status"] == "completed"

    def test_complete_stamps_the_report_handle(self, reg):
        """A background worker's report is unreachable without this — by the
        time it finishes there is no caller frame left to hand it back to."""
        reg.start_task(
            "s1", "w-1", "agent", "Audit the retry path",
            summary="Audit the retry path", model="OpenRouter · gpt-5.6-luna",
        )
        reg.complete_task(
            "s1", "w-1", "completed",
            summary="Retry backoff is unbounded",
            report_path="/repo/.fluid/task-reports/w-1.md",
            report_chars=27_000,
        )

        record = reg.get_task("s1", "w-1")
        assert record["reportPath"] == "/repo/.fluid/task-reports/w-1.md"
        assert record["reportChars"] == 27_000
        assert record["summary"] == "Retry backoff is unbounded"
        assert record["model"] == "OpenRouter · gpt-5.6-luna"

    def test_complete_without_a_report_stamps_nothing(self, reg):
        reg.start_task("s1", "t1", "shell", "npm test")
        reg.complete_task("s1", "t1", "completed")

        record = reg.get_task("s1", "t1")
        assert "reportPath" not in record and "reportChars" not in record

    @pytest.mark.parametrize("status", ["completed", "error", "killed"])
    def test_complete_terminal_statuses(self, reg, status):
        reg.start_task("s1", "t1", "shell", "cmd")
        reg.complete_task("s1", "t1", status)
        assert reg.get_active("s1") == []
        assert reg.get_all("s1")[0]["status"] == status

    def test_resume_revives_completed_task_to_running(self, reg):
        # Regression (task #13, live 2026-07-08): a background sub-agent that
        # completed and is RESUMED via the SendMessage tool re-emits a
        # TaskStartedMessage under the SAME task_id. It must reappear as active
        # (running) — otherwise the resumed run is invisible in /live activeTasks
        # while genuinely executing, and Stop still kills the live SDK query.
        reg.start_task("s1", "t1", "agent", "background work")
        reg.complete_task("s1", "t1", "completed")
        assert reg.get_active("s1") == []  # completed -> not active (baseline)
        # Resume: fresh TaskStartedMessage for the same task_id.
        reg.start_task("s1", "t1", "agent", "background work")
        active = reg.get_active("s1")
        assert [t["id"] for t in active] == ["t1"]
        assert active[0]["status"] == "running"
        # And it shows on the DISPLAY view too (no stale "completed" snapshot).
        visible = reg.get_visible("s1")
        assert [t["id"] for t in visible] == ["t1"]
        assert visible[0]["status"] == "running"

    def test_resume_revival_preserves_started_at(self, reg):
        reg.start_task("s1", "t1", "agent", "work")
        started_at = reg.get_active("s1")[0]["startedAt"]
        reg.complete_task("s1", "t1", "completed")
        reg.start_task("s1", "t1", "agent", "work")
        assert reg.get_active("s1")[0]["startedAt"] == started_at

    def test_resume_does_not_revive_failed_task(self, reg):
        # A re-emitted start for a FAILED/killed task is the force-cleanup /
        # dead-pooled-client stale leak, NOT a resume — it must stay terminal.
        for status in ("error", "killed"):
            reg.start_task("s2", "t", "agent", "x")
            reg.complete_task("s2", "t", status)
            reg.start_task("s2", "t", "agent", "late start")
            assert reg.get_active("s2") == []
            assert reg.get_visible("s2")[0]["status"] == status
            reg.clear_session("s2")
            reg.reset_all()


# =============================================================================
# tool_use_id plumbing (subagent-card reconcile on /live fresh load)
# =============================================================================

class TestToolUseId:
    def test_start_stores_tool_use_id(self, reg):
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="toolu_abc")
        assert reg._tasks["s1"]["t1"]["tool_use_id"] == "toolu_abc"

    def test_to_wire_emits_tool_use_id(self, reg):
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="toolu_abc")
        wire = reg.get_active("s1")[0]
        assert wire["toolUseId"] == "toolu_abc"

    def test_tool_use_id_defaults_none(self, reg):
        # Not supplied -> record carries None, wire emits None (None-safe).
        reg.start_task("s1", "t1", "agent", "bg")
        assert reg._tasks["s1"]["t1"]["tool_use_id"] is None
        assert reg.get_active("s1")[0]["toolUseId"] is None

    def test_agent_name_rides_the_row_and_the_wire(self, reg):
        reg.start_task("s1", "t1", "agent", "bg", agent="Frontend Designer")
        assert reg.get_active("s1")[0]["agent"] == "Frontend Designer"
        # Re-registering (a resume) never clobbers an agent already recorded.
        reg.start_task("s1", "t1", "agent", "bg", agent="Something Else")
        assert reg._tasks["s1"]["t1"]["agent"] == "Frontend Designer"

    def test_reregister_binds_missing_tool_use_id(self, reg):
        # First start had no id; a later re-register carries it -> now bound.
        reg.start_task("s1", "t1", "agent", "bg")
        assert reg._tasks["s1"]["t1"]["tool_use_id"] is None
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="toolu_late")
        assert reg._tasks["s1"]["t1"]["tool_use_id"] == "toolu_late"

    def test_reregister_never_clobbers_existing_tool_use_id(self, reg):
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="toolu_first")
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="toolu_second")
        assert reg._tasks["s1"]["t1"]["tool_use_id"] == "toolu_first"

    def test_get_task_tool_use_id_live_record(self, reg):
        # A live (running) task resolves its launch tool_use_id.
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="toolu_live")
        assert reg.get_task_tool_use_id("s1", "t1") == "toolu_live"

    def test_get_task_tool_use_id_just_completed(self, reg):
        # complete_task keeps the live record, so a JUST-completed task still
        # resolves (chip enrichment runs AFTER complete_task).
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="toolu_done")
        reg.complete_task("s1", "t1", "killed")
        assert reg.get_task_tool_use_id("s1", "t1") == "toolu_done"

    def test_get_task_tool_use_id_from_recent_terminal_after_clear(self, reg):
        # After clear_session wipes the live map, the retention snapshot
        # (_recent_terminal, which survives clear) still resolves the id.
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="toolu_reaped")
        reg.complete_task("s1", "t1", "killed")
        reg.clear_session("s1")
        assert "s1" not in reg._tasks  # live map gone
        assert reg.get_task_tool_use_id("s1", "t1") == "toolu_reaped"

    def test_get_task_tool_use_id_unknown_returns_none(self, reg):
        assert reg.get_task_tool_use_id("s1", "nope") is None
        assert reg.get_task_tool_use_id("missing", "t1") is None
        # A task with no launch id recorded resolves to None (not a raise).
        reg.start_task("s1", "t1", "agent", "bg")
        assert reg.get_task_tool_use_id("s1", "t1") is None


# =============================================================================
# Filtering & ordering
# =============================================================================

class TestFilteringOrdering:
    def test_get_active_excludes_finished(self, reg):
        reg.start_task("s1", "running", "agent", "still going")
        reg.start_task("s1", "done", "agent", "finished")
        reg.complete_task("s1", "done", "completed")
        active = reg.get_active("s1")
        assert [t["id"] for t in active] == ["running"]

    def test_get_active_newest_first(self, reg):
        # Distinct started_at via explicit ordering — newest-first by started_at.
        reg.start_task("s1", "old", "agent", "old")
        reg.start_task("s1", "new", "agent", "new")
        # Force a deterministic started_at ordering.
        reg._tasks["s1"]["old"]["started_at"] = "2024-01-01T00:00:00+00:00"
        reg._tasks["s1"]["new"]["started_at"] = "2024-01-02T00:00:00+00:00"
        ids = [t["id"] for t in reg.get_active("s1")]
        assert ids == ["new", "old"]

    def test_get_all_includes_finished_newest_first(self, reg):
        reg.start_task("s1", "a", "agent", "a")
        reg.start_task("s1", "b", "shell", "b")
        reg.complete_task("s1", "a", "completed")
        # 'a' was just updated (completed), so it sorts first by updated_at.
        reg._tasks["s1"]["b"]["updated_at"] = "2024-01-01T00:00:00+00:00"
        reg._tasks["s1"]["a"]["updated_at"] = "2024-01-02T00:00:00+00:00"
        ids = [t["id"] for t in reg.get_all("s1")]
        assert ids == ["a", "b"]

    def test_get_all_respects_limit(self, reg):
        for i in range(10):
            reg.start_task("s1", f"t{i}", "agent", f"task {i}")
        assert len(reg.get_all("s1", limit=3)) == 3

    def test_empty_session_returns_empty_lists(self, reg):
        assert reg.get_active("missing") == []
        assert reg.get_all("missing") == []


# =============================================================================
# has_active_background_agent (the session-pool busy signal)
# =============================================================================

class TestHasActiveBackgroundAgent:
    def test_false_for_unknown_session(self, reg):
        assert reg.has_active_background_agent("missing") is False

    def test_true_while_agent_running(self, reg):
        reg.start_task("s1", "t1", "agent", "Background work")
        assert reg.has_active_background_agent("s1") is True

    def test_false_after_agent_completes(self, reg):
        reg.start_task("s1", "t1", "agent", "Background work")
        reg.complete_task("s1", "t1", "completed")
        assert reg.has_active_background_agent("s1") is False

    def test_false_after_agent_errors_or_killed(self, reg):
        reg.start_task("s1", "t1", "agent", "Background work")
        reg.complete_task("s1", "t1", "killed")
        assert reg.has_active_background_agent("s1") is False

    def test_shell_task_does_not_count(self, reg):
        # Only kind=="agent" pins a session in the pool; a running shell
        # subprocess (the Monitor tool family) is independent of the SDK client.
        reg.start_task("s1", "sh1", "shell", "tail -f log")
        assert reg.has_active_background_agent("s1") is False

    def test_true_if_any_agent_running_among_finished(self, reg):
        reg.start_task("s1", "done", "agent", "First")
        reg.complete_task("s1", "done", "completed")
        reg.start_task("s1", "live", "agent", "Second")
        assert reg.has_active_background_agent("s1") is True

    def test_isolated_per_session(self, reg):
        reg.start_task("s1", "t1", "agent", "Background work")
        assert reg.has_active_background_agent("s2") is False

    def test_cleared_by_clear_session(self, reg):
        reg.start_task("s1", "t1", "agent", "Background work")
        reg.clear_session("s1")
        assert reg.has_active_background_agent("s1") is False


# =============================================================================
# SDK-backgrounded shells: _sdk_background_tasks + get/has_active_background_work
# =============================================================================

class TestSdkBackgroundWork:
    """A backgrounded local_bash shell parks its parent and unparks via a terminal
    notification on the pooled SDK client — exactly like a background sub-agent — so
    it MUST pin the session and drive an unpark. ``mark_sdk_background_task`` records
    it; ``get/has_active_background_work`` widen the busy signal to cover it while the
    agent-only ``has_active_background_agent`` stays unchanged. App-managed Monitor
    shells (registered via ``start_task`` but NEVER marked) must NOT pin.
    """

    # --- is_sdk_background_task -------------------------------------------------
    def test_is_sdk_background_false_for_unmarked(self, reg):
        reg.start_task("s1", "sh1", "shell", "tail -f log")
        assert reg.is_sdk_background_task("s1", "sh1") is False

    def test_mark_then_is_sdk_background_true(self, reg):
        reg.start_task("s1", "sh1", "shell", "npm run build")
        reg.mark_sdk_background_task("s1", "sh1")
        assert reg.is_sdk_background_task("s1", "sh1") is True

    def test_is_sdk_background_independent_per_session(self, reg):
        reg.mark_sdk_background_task("s1", "sh1")
        assert reg.is_sdk_background_task("s2", "sh1") is False

    def test_mark_blank_ids_never_record(self, reg):
        reg.mark_sdk_background_task("", "sh1")
        reg.mark_sdk_background_task("s1", "")
        assert reg.is_sdk_background_task("", "sh1") is False
        assert reg.is_sdk_background_task("s1", "") is False
        # No state stranded -> a real (session, task) still records.
        reg.mark_sdk_background_task("s1", "sh1")
        assert reg.is_sdk_background_task("s1", "sh1") is True

    # --- get/has_active_background_work ----------------------------------------
    def test_agent_counts_without_marking(self, reg):
        # A sub-agent pins regardless of the sdk-background marking (kind=="agent").
        reg.start_task("s1", "a1", "agent", "Background agent")
        assert reg.has_active_background_work("s1") is True
        assert [t["id"] for t in reg.get_active_background_work("s1")] == ["a1"]

    def test_marked_shell_counts(self, reg):
        # An SDK-backgrounded local_bash shell pins ONLY once marked.
        reg.start_task("s1", "sh1", "shell", "long build")
        assert reg.has_active_background_work("s1") is False
        reg.mark_sdk_background_task("s1", "sh1")
        assert reg.has_active_background_work("s1") is True
        assert [t["id"] for t in reg.get_active_background_work("s1")] == ["sh1"]

    def test_unmarked_shell_does_not_count(self, reg):
        # App Monitor shell: registered but never marked -> must NOT pin the pool.
        reg.start_task("s1", "mon1", "shell", "tail -f app.log")
        assert reg.has_active_background_work("s1") is False
        assert reg.get_active_background_work("s1") == []

    def test_completed_marked_shell_excluded(self, reg):
        reg.start_task("s1", "sh1", "shell", "build")
        reg.mark_sdk_background_task("s1", "sh1")
        reg.complete_task("s1", "sh1", "completed")
        # Status is checked live, so a finished marked shell no longer pins.
        assert reg.has_active_background_work("s1") is False
        assert reg.get_active_background_work("s1") == []

    def test_mixed_agent_and_shell_newest_first(self, reg):
        reg.start_task("s1", "a1", "agent", "agent")
        reg.start_task("s1", "sh1", "shell", "shell")
        reg.mark_sdk_background_task("s1", "sh1")
        reg._tasks["s1"]["a1"]["started_at"] = "2024-01-01T00:00:00+00:00"
        reg._tasks["s1"]["sh1"]["started_at"] = "2024-01-02T00:00:00+00:00"
        ids = [t["id"] for t in reg.get_active_background_work("s1")]
        assert ids == ["sh1", "a1"]

    def test_empty_for_unknown_session(self, reg):
        assert reg.has_active_background_work("missing") is False
        assert reg.get_active_background_work("missing") == []

    def test_does_not_affect_agent_only_signal(self, reg):
        # A marked shell pins background_work but NOT the agent-only predicate.
        reg.start_task("s1", "sh1", "shell", "build")
        reg.mark_sdk_background_task("s1", "sh1")
        assert reg.has_active_background_work("s1") is True
        assert reg.has_active_background_agent("s1") is False

    # --- lifecycle clears ------------------------------------------------------
    def test_cleared_by_clear_session(self, reg):
        reg.start_task("s1", "sh1", "shell", "build")
        reg.mark_sdk_background_task("s1", "sh1")
        reg.mark_sdk_background_task("s2", "sh9")
        reg.clear_session("s1")
        assert reg.is_sdk_background_task("s1", "sh1") is False
        assert reg.is_sdk_background_task("s2", "sh9") is True

    def test_cleared_by_reset_all(self, reg):
        reg.mark_sdk_background_task("s1", "sh1")
        reg.reset_all()
        assert reg.is_sdk_background_task("s1", "sh1") is False


# =============================================================================
# Subagent-lane background markers: _subagent_background_launches/_tasks
# =============================================================================

class TestSubagentBackgroundMarkers:
    """A background task launched FROM a sub-agent sidechain (run_in_background,
    parent_tool_id set) gets a DEDICATED marker pair so the unpark gates can
    deliver its terminal notification (2026-07-10 stranded-subagent incident) —
    WITHOUT joining ``_background_launches``/``_sdk_background_tasks``, which
    feed pool pinning and chip scoping the grandchild must never touch.
    """

    # --- launch marker ----------------------------------------------------------
    def test_launch_marker_false_when_unmarked(self, reg):
        assert reg.was_subagent_background_launch("s1", "tu-gc") is False

    def test_mark_then_was_subagent_background_launch_true(self, reg):
        reg.mark_subagent_background_launch("s1", "tu-gc")
        assert reg.was_subagent_background_launch("s1", "tu-gc") is True
        # The parent's own launch gate is untouched.
        assert reg.was_background_launch("s1", "tu-gc") is False

    # --- task marker ------------------------------------------------------------
    def test_task_marker_false_when_unmarked(self, reg):
        reg.start_task("s1", "gc1", "shell", "monitor loop")
        assert reg.is_subagent_background_task("s1", "gc1") is False

    def test_mark_then_is_subagent_background_task_true(self, reg):
        reg.start_task("s1", "gc1", "shell", "monitor loop")
        reg.mark_subagent_background_task("s1", "gc1")
        assert reg.is_subagent_background_task("s1", "gc1") is True

    def test_markers_isolated_per_session(self, reg):
        reg.mark_subagent_background_launch("s1", "tu-gc")
        reg.mark_subagent_background_task("s1", "gc1")
        assert reg.was_subagent_background_launch("s2", "tu-gc") is False
        assert reg.is_subagent_background_task("s2", "gc1") is False

    def test_blank_ids_never_record(self, reg):
        reg.mark_subagent_background_launch("", "tu-gc")
        reg.mark_subagent_background_launch("s1", "")
        reg.mark_subagent_background_task("", "gc1")
        reg.mark_subagent_background_task("s1", "")
        assert reg.was_subagent_background_launch("s1", "") is False
        assert reg.is_subagent_background_task("s1", "") is False
        # No state stranded — real ids still record.
        reg.mark_subagent_background_launch("s1", "tu-gc")
        reg.mark_subagent_background_task("s1", "gc1")
        assert reg.was_subagent_background_launch("s1", "tu-gc") is True
        assert reg.is_subagent_background_task("s1", "gc1") is True

    # --- blast-radius pins: NO pool pinning, NO chip eligibility -----------------
    def test_marked_task_does_not_pin_pool(self, reg):
        # A subagent-lane backgrounded shell must NOT widen the pool busy signal
        # (the launching sub-agent's own kind=="agent" record is what pins).
        reg.start_task("s1", "gc1", "shell", "monitor loop")
        reg.mark_subagent_background_task("s1", "gc1")
        assert reg.has_active_background_work("s1") is False
        assert reg.get_active_background_work("s1") == []

    def test_marked_task_is_not_own_background_task(self, reg):
        # Chip scoping unchanged: a grandchild never chips as the parent's own.
        reg.start_task("s1", "gc1", "shell", "monitor loop", tool_use_id="tu-gc")
        reg.mark_subagent_background_launch("s1", "tu-gc")
        reg.mark_subagent_background_task("s1", "gc1")
        assert reg.is_own_background_task("s1", "gc1") is False

    # --- lifecycle clears --------------------------------------------------------
    def test_cleared_by_clear_session(self, reg):
        reg.mark_subagent_background_launch("s1", "tu-gc")
        reg.mark_subagent_background_task("s1", "gc1")
        reg.mark_subagent_background_task("s2", "gc9")
        reg.clear_session("s1")
        assert reg.was_subagent_background_launch("s1", "tu-gc") is False
        assert reg.is_subagent_background_task("s1", "gc1") is False
        assert reg.is_subagent_background_task("s2", "gc9") is True

    def test_cleared_by_reset_all(self, reg):
        reg.mark_subagent_background_launch("s1", "tu-gc")
        reg.mark_subagent_background_task("s1", "gc1")
        reg.reset_all()
        assert reg.was_subagent_background_launch("s1", "tu-gc") is False
        assert reg.is_subagent_background_task("s1", "gc1") is False


# =============================================================================
# to_wire shape
# =============================================================================

class TestToWire:
    def test_exact_keys(self):
        record = {
            "task_id": "t1",
            "kind": "agent",
            "title": "Do work",
            "status": "running",
            "summary": "in progress",
            "tool_use_id": "toolu_abc",
            "started_at": "2024-01-01T00:00:00+00:00",
            "updated_at": "2024-01-01T00:05:00+00:00",
        }
        wire = to_wire(record)
        assert wire == {
            "id": "t1",
            "subject": "Do work",
            "status": "running",
            "kind": "agent",
            "summary": "in progress",
            "startedAt": "2024-01-01T00:00:00+00:00",
            "updatedAt": "2024-01-01T00:05:00+00:00",
            "toolUseId": "toolu_abc",
        }
        # Exact key set — no snake_case leak, no extras.
        assert set(wire.keys()) == {
            "id", "subject", "status", "kind", "summary", "startedAt",
            "updatedAt", "toolUseId",
        }

    def test_agent_name_is_additive(self):
        """Task-tool workers carry the dispatched agent's display NAME; every
        other task kind keeps the exact shape above."""
        record = {"task_id": "t1", "kind": "agent", "status": "running"}
        assert "agent" not in to_wire(record)
        record["agent"] = "Frontend Designer"
        assert to_wire(record)["agent"] == "Frontend Designer"

    def test_model_label_and_report_handle_are_additive(self):
        """The model label has its own field so ``summary`` can say what the
        task IS, and a completed worker's report is reachable by path."""
        record = {"task_id": "t1", "kind": "agent", "status": "completed"}
        wire = to_wire(record)
        assert "model" not in wire
        assert "reportPath" not in wire and "reportChars" not in wire

        record["model"] = "OpenRouter · openai/gpt-5.6-luna"
        record["report_path"] = "/repo/.fluid/task-reports/t1.md"
        record["report_chars"] = 27_000
        wire = to_wire(record)
        assert wire["model"] == "OpenRouter · openai/gpt-5.6-luna"
        assert wire["reportPath"] == "/repo/.fluid/task-reports/t1.md"
        assert wire["reportChars"] == 27_000
        # A zero-length report still reports its size rather than vanishing.
        record["report_chars"] = 0
        assert to_wire(record)["reportChars"] == 0


# =============================================================================
# clear_session / reset_all / unknown no-ops
# =============================================================================

class TestMarkWoken:
    """A-3 late-frame latch: a task drives AT MOST ONE unpark/pump wake per
    lifetime, re-armed only by a genuine resume (completed→running revival)."""

    def test_true_once_then_false_for_task(self, reg):
        # First time a task is woken -> True; a later (late) frame -> False.
        assert reg.mark_woken("s1", "t1") is True
        assert reg.mark_woken("s1", "t1") is False
        assert reg.is_woken("s1", "t1") is True

    def test_independent_per_task_and_session(self, reg):
        assert reg.mark_woken("s1", "t1") is True
        assert reg.mark_woken("s1", "t2") is True  # a different task is independent
        assert reg.mark_woken("s2", "t1") is True  # a different session is independent

    def test_blank_ids_never_latch(self, reg):
        assert reg.mark_woken("", "t1") is False
        assert reg.mark_woken("s1", "") is False
        assert reg.is_woken("", "t1") is False
        # No state stranded -> a real (session, task) still latches True.
        assert reg.mark_woken("s1", "t1") is True

    def test_cleared_by_clear_session(self, reg):
        assert reg.mark_woken("s1", "t1") is True
        reg.clear_session("s1")
        assert reg.is_woken("s1", "t1") is False
        # After clearing, the task re-arms (fresh session state).
        assert reg.mark_woken("s1", "t1") is True

    def test_cleared_by_reset_all(self, reg):
        assert reg.mark_woken("s1", "t1") is True
        reg.reset_all()
        assert reg.mark_woken("s1", "t1") is True

    def test_cleared_on_resume_revival(self, reg):
        # A completed->running revival (SendMessage resume under the same task_id)
        # re-arms the wake latch so the resumed run can drive a fresh wake.
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="tu-1")
        assert reg.mark_woken("s1", "t1") is True
        assert reg.mark_woken("s1", "t1") is False
        reg.complete_task("s1", "t1", "completed")
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="tu-1")  # revival
        assert reg.is_woken("s1", "t1") is False
        assert reg.mark_woken("s1", "t1") is True

    def test_failure_terminal_not_revived_keeps_latch(self, reg):
        # A killed task is NOT revived by a re-emitted start (stale leak) — so its
        # wake latch must NOT clear.
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="tu-1")
        assert reg.mark_woken("s1", "t1") is True
        reg.complete_task("s1", "t1", "killed")
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="tu-1")  # stale re-start
        assert reg.is_woken("s1", "t1") is True
        assert reg.mark_woken("s1", "t1") is False


class TestWakeSuppressed:
    """Explicit cancellation blocks wakes without overloading duplicate dedupe."""

    def test_independent_per_task_and_session(self, reg):
        assert reg.suppress_wake("s1", "t1") is True
        assert reg.suppress_wake("s1", "t1") is False
        assert reg.is_wake_suppressed("s1", "t1") is True
        assert reg.is_wake_suppressed("s1", "t2") is False
        assert reg.is_wake_suppressed("s2", "t1") is False

    def test_blank_ids_never_latch(self, reg):
        assert reg.suppress_wake("", "t1") is False
        assert reg.suppress_wake("s1", "") is False

    def test_cleared_by_session_and_reset(self, reg):
        reg.suppress_wake("s1", "t1")
        reg.clear_session("s1")
        assert reg.is_wake_suppressed("s1", "t1") is False
        reg.suppress_wake("s1", "t1")
        reg.reset_all()
        assert reg.is_wake_suppressed("s1", "t1") is False

    def test_genuine_completed_task_revival_rearms_wake(self, reg):
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="tu-1")
        reg.suppress_wake("s1", "t1")
        reg.complete_task("s1", "t1", "completed")
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="tu-1")
        assert reg.is_wake_suppressed("s1", "t1") is False


class TestChildOwnerLinkage:
    """B-1: resolve a terminal grandchild task to its running owner sub-agent."""

    def _wire(self, reg):
        # Owner sub-agent A (its own launch tuid = tu-A) and a grandchild B it
        # launched (child launch tuid = tu-B), linked child⇐owner.
        reg.start_task("s1", "owner-A", "agent", "sub", tool_use_id="tu-A")
        reg.start_task("s1", "child-B", "agent", "gc", tool_use_id="tu-B")
        reg.link_child_to_owner("s1", "tu-B", "tu-A")

    def test_resolves_owner_task(self, reg):
        self._wire(reg)
        assert reg.owner_task_for_child("s1", "child-B") == "owner-A"

    def test_missing_linkage_returns_none(self, reg):
        # child record exists but no linkage armed → unresolvable.
        reg.start_task("s1", "child-B", "agent", "gc", tool_use_id="tu-B")
        assert reg.owner_task_for_child("s1", "child-B") is None

    def test_missing_child_record_returns_none(self, reg):
        reg.link_child_to_owner("s1", "tu-B", "tu-A")
        assert reg.owner_task_for_child("s1", "child-B") is None

    def test_missing_owner_record_returns_none(self, reg):
        # Linkage present + child record present, but no task carries tu-A.
        reg.start_task("s1", "child-B", "agent", "gc", tool_use_id="tu-B")
        reg.link_child_to_owner("s1", "tu-B", "tu-A")
        assert reg.owner_task_for_child("s1", "child-B") is None

    def test_blank_and_self_link_ignored(self, reg):
        reg.link_child_to_owner("", "tu-B", "tu-A")
        reg.link_child_to_owner("s1", "tu-B", "tu-B")  # self-link ignored
        reg.start_task("s1", "child-B", "agent", "gc", tool_use_id="tu-B")
        assert reg.owner_task_for_child("s1", "child-B") is None

    def test_isolated_per_session(self, reg):
        self._wire(reg)
        assert reg.owner_task_for_child("s2", "child-B") is None

    def test_cleared_by_clear_session(self, reg):
        self._wire(reg)
        reg.clear_session("s1")
        assert reg.owner_task_for_child("s1", "child-B") is None

    def test_cleared_by_reset_all(self, reg):
        self._wire(reg)
        reg.reset_all()
        assert reg.owner_task_for_child("s1", "child-B") is None


class TestOwnerActivity:
    """B-prime item b: monotonic activity stamp used by the deferred-pump
    quiescence discriminator."""

    def test_unseen_owner_is_none(self, reg):
        assert reg.owner_activity_ts("s1", "tu-A") is None

    def test_note_then_read_is_monotonic(self, reg):
        reg.note_owner_activity("s1", "tu-A")
        first = reg.owner_activity_ts("s1", "tu-A")
        assert first is not None
        reg.note_owner_activity("s1", "tu-A")
        assert reg.owner_activity_ts("s1", "tu-A") >= first

    def test_blank_ids_never_record(self, reg):
        reg.note_owner_activity("", "tu-A")
        reg.note_owner_activity("s1", "")
        assert reg.owner_activity_ts("s1", "tu-A") is None

    def test_isolated_per_session_and_owner(self, reg):
        reg.note_owner_activity("s1", "tu-A")
        assert reg.owner_activity_ts("s2", "tu-A") is None
        assert reg.owner_activity_ts("s1", "tu-B") is None

    def test_cleared_by_clear_session(self, reg):
        reg.note_owner_activity("s1", "tu-A")
        reg.clear_session("s1")
        assert reg.owner_activity_ts("s1", "tu-A") is None

    def test_cleared_by_reset_all(self, reg):
        reg.note_owner_activity("s1", "tu-A")
        reg.reset_all()
        assert reg.owner_activity_ts("s1", "tu-A") is None


class TestPumpCleanupCallback:
    """B-prime item f: the registry fires the scheduler's deferred-pump cleanup
    on clear_session (with the id) and reset_all (with None)."""

    def test_clear_session_fires_with_id(self, reg):
        seen = []
        reg.set_pump_cleanup_callback(lambda sid: seen.append(sid))
        reg.clear_session("s1")
        assert seen == ["s1"]

    def test_reset_all_fires_with_none_then_deregisters(self, reg):
        seen = []
        reg.set_pump_cleanup_callback(lambda sid: seen.append(sid))
        reg.reset_all()
        assert seen == [None]
        # Deregistered — a later clear does not re-invoke it.
        reg.clear_session("s1")
        assert seen == [None]

    def test_raising_callback_is_swallowed(self, reg):
        def _boom(_sid):
            raise RuntimeError("nope")
        reg.set_pump_cleanup_callback(_boom)
        reg.clear_session("s1")  # must not raise


class TestStartedThisTurn:
    """The per-turn scope set that suppresses spurious in-stream unparks.

    A background agent's TaskStartedMessage in the CURRENT turn marks it
    "started this turn"; a terminal notification for a task NOT in this set is a
    prior-turn orphan and must NOT fire an in-stream unpark. The set is reset at
    the start of every turn.
    """

    def test_mark_then_query_true(self, reg):
        reg.mark_started_this_turn("s1", "t1")
        assert reg.was_started_this_turn("s1", "t1") is True

    def test_unknown_task_is_false(self, reg):
        # Never marked -> a prior-turn orphan.
        assert reg.was_started_this_turn("s1", "t1") is False
        reg.mark_started_this_turn("s1", "t1")
        # A DIFFERENT task in the same session is still unknown.
        assert reg.was_started_this_turn("s1", "t2") is False

    def test_reset_clears_only_that_session(self, reg):
        reg.mark_started_this_turn("s1", "t1")
        reg.mark_started_this_turn("s2", "t9")
        reg.reset_started_this_turn("s1")
        # s1 cleared (turn boundary), s2 untouched.
        assert reg.was_started_this_turn("s1", "t1") is False
        assert reg.was_started_this_turn("s2", "t9") is True

    def test_blank_ids_never_record(self, reg):
        reg.mark_started_this_turn("", "t1")
        reg.mark_started_this_turn("s1", "")
        assert reg.was_started_this_turn("", "t1") is False
        assert reg.was_started_this_turn("s1", "") is False
        # No state stranded -> a real (session, task) still records.
        reg.mark_started_this_turn("s1", "t1")
        assert reg.was_started_this_turn("s1", "t1") is True

    def test_reset_missing_session_is_noop(self, reg):
        # Should not raise.
        reg.reset_started_this_turn("nope")

    def test_cleared_by_clear_session(self, reg):
        reg.mark_started_this_turn("s1", "t1")
        reg.clear_session("s1")
        assert reg.was_started_this_turn("s1", "t1") is False

    def test_cleared_by_reset_all(self, reg):
        reg.mark_started_this_turn("s1", "t1")
        reg.reset_all()
        assert reg.was_started_this_turn("s1", "t1") is False


class TestBackgroundLaunch:
    """The cross-turn set gating the unpark to genuinely-backgrounded sub-agents.

    A Task/Agent tool_use launched with run_in_background=true records its launch
    tool_use_id; the at-rest TaskNotificationMessage's tool_use_id equals that id,
    so the unpark paths only fire for recorded launches. SYNCHRONOUS sub-agents
    (result already inline) and sub-agent-launched grandchildren (never recorded)
    return False. UNLIKE started_this_turn, this set is NOT reset per turn — only
    cleared on clear_session — because a turn-N launch notifies between turn N/N+1.
    """

    def test_mark_then_query_true(self, reg):
        reg.mark_background_launch("s1", "tool-1")
        assert reg.was_background_launch("s1", "tool-1") is True

    def test_unknown_tool_use_is_false(self, reg):
        # Never recorded -> synchronous sub-agent or untracked grandchild.
        assert reg.was_background_launch("s1", "tool-1") is False
        reg.mark_background_launch("s1", "tool-1")
        # A DIFFERENT launch id in the same session is still unrecorded.
        assert reg.was_background_launch("s1", "tool-2") is False

    def test_independent_per_session(self, reg):
        reg.mark_background_launch("s1", "tool-1")
        # Same launch id in a DIFFERENT session is independent.
        assert reg.was_background_launch("s2", "tool-1") is False

    def test_blank_ids_never_record(self, reg):
        reg.mark_background_launch("", "tool-1")
        reg.mark_background_launch("s1", "")
        assert reg.was_background_launch("", "tool-1") is False
        assert reg.was_background_launch("s1", "") is False
        # Defensive False on missing args even when nothing was recorded.
        assert reg.was_background_launch("", "") is False
        # No state stranded -> a real (session, tool_use_id) still records.
        reg.mark_background_launch("s1", "tool-1")
        assert reg.was_background_launch("s1", "tool-1") is True

    def test_not_reset_per_turn(self, reg):
        # The legit slow path: a launch in turn N must still be recognized when its
        # notification arrives between turn N and N+1. reset_started_this_turn (the
        # per-turn scope reset) must NOT touch the background-launch set.
        reg.mark_background_launch("s1", "tool-1")
        reg.reset_started_this_turn("s1")
        assert reg.was_background_launch("s1", "tool-1") is True

    def test_cleared_by_clear_session(self, reg):
        reg.mark_background_launch("s1", "tool-1")
        reg.mark_background_launch("s2", "tool-9")
        reg.clear_session("s1")
        # s1 cleared, s2 untouched.
        assert reg.was_background_launch("s1", "tool-1") is False
        assert reg.was_background_launch("s2", "tool-9") is True

    def test_cleared_by_reset_all(self, reg):
        reg.mark_background_launch("s1", "tool-1")
        reg.reset_all()
        assert reg.was_background_launch("s1", "tool-1") is False


class TestParkGateLaunchResolution:
    """The ``_finalize`` park gate reads ``has_unresolved_background_launches``
    instead of ORing the raw cumulative ``_background_launches`` set (the
    2026-08-08 amendment to the stuck-parked fix). A launch whose task reached a
    TERMINAL registry status is RESOLVED — the gate stops counting it, so a
    finished task no longer re-parks every later turn. A launch with NO registry
    record yet is UNRESOLVED (the launch-before-registration race it exists for).
    The launch SET itself stays cumulative and untouched (PR #765 ruling intact):
    ``complete_task`` no longer discards anything, so ownership signals that read
    ``was_background_launch`` survive completion."""

    def test_launch_with_terminal_record_is_resolved(self, reg):
        reg.mark_background_launch("s1", "tu-1")
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="tu-1")
        assert reg.has_unresolved_background_launches("s1") is True  # running
        reg.complete_task("s1", "t1", "completed")
        # Finished -> gate no longer counts it, so a later turn won't re-park.
        assert reg.has_unresolved_background_launches("s1") is False
        # The launch SET is UNCHANGED — only the GATE ignores it (PR #765 intact).
        assert reg.get_background_launches("s1") == {"tu-1"}
        assert reg.was_background_launch("s1", "tu-1") is True

    def test_launch_with_no_record_is_unresolved(self, reg):
        # The launch-before-registration race: a bare launch id with no task row
        # keeps the gate armed (True) so the park it exists to hold still fires.
        reg.mark_background_launch("s1", "tu-pending")
        assert reg.has_unresolved_background_launches("s1") is True

    def test_launch_with_running_record_keeps_bg_gate_armed(self, reg):
        # A still-RUNNING task's launch: the overall gate stays armed via
        # has_active_background_work (bg_work arm), which is what the park gate
        # ORs alongside the launch arm.
        reg.mark_background_launch("s1", "tu-1")
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="tu-1")
        assert reg.has_active_background_work("s1") is True

    def test_ownership_survives_completion_for_ack_sniff_launch(self, reg):
        # ORDERING-HAZARD REGRESSION. A launch armed ONLY via mark_background_launch
        # (the ACK-sniff path — is_sdk_background_task NOT armed at TaskStarted):
        # after complete_task, was_background_launch is STILL True and
        # is_own_background_task STILL True, so the between-turns terminal handoff
        # still classifies it OWN and delivers the wake. (The removed LEG 3 discard
        # destroyed this and dropped the wake.)
        reg.mark_background_launch("s1", "tu-1")
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="tu-1")
        reg.complete_task("s1", "t1", "completed")
        assert reg.was_background_launch("s1", "tu-1") is True
        assert reg.is_own_background_task("s1", "t1") is True

    def test_completion_still_fires_the_failure_chip(self, reg):
        # is_own_background_task (which reads was_background_launch) is True during
        # the guaranteed-notification decision, and stays True afterward.
        chips = []
        reg.set_terminal_notification_callback(
            lambda sid, task: chips.append(task["id"])
        )
        reg.mark_background_launch("s1", "t-tu")
        reg.start_task("s1", "bg1", "agent", "bg", tool_use_id="t-tu")
        reg.complete_task("s1", "bg1", "killed")  # failure of own bg work
        assert chips == ["bg1"]                    # chip fired
        assert reg.get_background_launches("s1") == {"t-tu"}  # set unchanged

    def test_launches_survive_complete_task(self, reg):
        # Belt-and-braces on the #765 ruling: multiple launches, one task
        # completing does not remove ANY launch entry.
        reg.mark_background_launch("s1", "tu-1")
        reg.mark_background_launch("s1", "tu-2")
        reg.start_task("s1", "t1", "agent", "bg", tool_use_id="tu-1")
        reg.complete_task("s1", "t1", "completed")
        assert reg.get_background_launches("s1") == {"tu-1", "tu-2"}
        # tu-1 resolved (terminal record) but tu-2 has no record -> still unresolved.
        assert reg.has_unresolved_background_launches("s1") is True


class TestRunningBackgroundTaskForToolUse:
    """Reverse lookup (launch tool_use_id -> is there a RUNNING background task?),
    the registry evidence powering the #660 defer classifier. True ONLY for a
    task that is (a) running, (b) launched by the matching tool_use_id, and (c)
    background per is_own_background_task / is_subagent_background_task."""

    def test_hit_via_sdk_background_marker(self, reg):
        reg.start_task("s1", "task-1", "agent", "bg", tool_use_id="tu-1")
        reg.mark_sdk_background_task("s1", "task-1")
        # is_own_background_task is True via the SDK marker, even though
        # was_background_launch("tu-1") was NEVER recorded — isolates the reverse
        # lookup as the sole signal.
        assert reg.was_background_launch("s1", "tu-1") is False
        assert reg.running_background_task_for_tool_use("s1", "tu-1") is True

    def test_hit_via_recorded_background_launch(self, reg):
        reg.mark_background_launch("s1", "tu-1")
        reg.start_task("s1", "task-1", "agent", "bg", tool_use_id="tu-1")
        assert reg.running_background_task_for_tool_use("s1", "tu-1") is True

    def test_hit_via_subagent_lane_marker(self, reg):
        reg.start_task("s1", "task-1", "agent", "bg", tool_use_id="tu-1")
        reg.mark_subagent_background_task("s1", "task-1")
        assert reg.running_background_task_for_tool_use("s1", "tu-1") is True

    def test_miss_unknown_tool_use(self, reg):
        reg.start_task("s1", "task-1", "agent", "bg", tool_use_id="tu-1")
        reg.mark_sdk_background_task("s1", "task-1")
        assert reg.running_background_task_for_tool_use("s1", "tu-other") is False

    def test_foreground_running_task_not_counted(self, reg):
        # Running and the tool_use_id matches, but the task is NOT background
        # (no marker, no recorded launch) -> a synchronous foreground sub-agent.
        reg.start_task("s1", "task-1", "agent", "fg", tool_use_id="tu-1")
        assert reg.is_own_background_task("s1", "task-1") is False
        assert reg.running_background_task_for_tool_use("s1", "tu-1") is False

    def test_terminal_status_record_not_counted(self, reg):
        # Background and matching, but COMPLETED (not running) -> not counted; the
        # record is kept by complete_task, so this exercises the status filter.
        reg.start_task("s1", "task-1", "agent", "bg", tool_use_id="tu-1")
        reg.mark_sdk_background_task("s1", "task-1")
        reg.complete_task("s1", "task-1", "completed")
        assert reg.running_background_task_for_tool_use("s1", "tu-1") is False

    def test_blank_ids_false(self, reg):
        assert reg.running_background_task_for_tool_use("", "tu-1") is False
        assert reg.running_background_task_for_tool_use("s1", "") is False

    def test_independent_per_session(self, reg):
        reg.start_task("s1", "task-1", "agent", "bg", tool_use_id="tu-1")
        reg.mark_sdk_background_task("s1", "task-1")
        assert reg.running_background_task_for_tool_use("s2", "tu-1") is False


class TestClearAndNoOps:
    def test_clear_session(self, reg):
        reg.start_task("s1", "t1", "agent", "x")
        reg.start_task("s2", "t2", "agent", "y")
        reg.clear_session("s1")
        assert reg.get_all("s1") == []
        # Other sessions untouched.
        assert len(reg.get_all("s2")) == 1

    def test_clear_unknown_session_is_noop(self, reg):
        reg.clear_session("never-existed")  # must not raise

    def test_reset_all(self, reg):
        reg.start_task("s1", "t1", "agent", "x")
        reg.start_task("s2", "t2", "agent", "y")
        reg.reset_all()
        assert reg.get_all("s1") == []
        assert reg.get_all("s2") == []

    def test_update_unknown_task_is_noop(self, reg):
        reg.update_task("s1", "ghost", status="error")  # no record yet
        assert reg.get_all("s1") == []

    def test_complete_unknown_task_is_noop(self, reg):
        reg.complete_task("s1", "ghost", "completed")
        assert reg.get_all("s1") == []

    def test_start_ignores_blank_ids(self, reg):
        reg.start_task("", "t1", "agent", "x")
        reg.start_task("s1", "", "agent", "x")
        assert reg.get_all("") == []
        assert reg.get_all("s1") == []


# =============================================================================
# Failure retention: get_visible surfaces reaped/dead tasks past clear_session
# =============================================================================

class TestRecentTerminalRetention:
    """The fix for "a background task vanished from the UI with no failed
    indication".

    When the pooled SDK client dies, force-cleanup reaps the session's running
    tasks to "killed" (``complete_task``) and then immediately ``clear_session``
    wipes the live ``_tasks`` map — so the very next ``/live`` poll saw no tasks
    and the taskbar row dropped silently. A FAILURE terminal status ("error"/
    "killed") is now retained for a short window in ``_recent_terminal`` and
    surfaced by ``get_visible`` (the ``/live`` DISPLAY view) even after
    ``clear_session``. ``get_active`` (the running-only control-flow view) is
    unchanged. A normal "completed" is retained too, but only for the much
    shorter ``_RECENT_COMPLETED_WINDOW_SECONDS`` — just long enough to bridge
    the completion -> ``unpark_pending`` hand-off gap so ``/live`` never reads
    fully idle mid-hand-off (which stopped the poller / classified a loading
    session as idle and stranded the unpark follow-up in a dead UI).
    """

    def test_killed_survives_clear_session_in_get_visible(self, reg):
        # Reproduces the exact force-cleanup sequence at the registry level.
        reg.start_task("s1", "bg1", "agent", "background work")
        assert [t["id"] for t in reg.get_active("s1")] == ["bg1"]
        # Reap to killed (what force-cleanup does), then clear the live bucket.
        reg.complete_task("s1", "bg1", "killed")
        reg.clear_session("s1")
        # Running-only view is empty (task is gone from _tasks)...
        assert reg.get_active("s1") == []
        # ...but the DISPLAY view still surfaces the failure for the window.
        visible = reg.get_visible("s1")
        assert [t["id"] for t in visible] == ["bg1"]
        assert visible[0]["status"] == "killed"
        assert visible[0]["kind"] == "agent"

    def test_error_status_also_retained(self, reg):
        reg.start_task("s1", "bg1", "shell", "build")
        reg.complete_task("s1", "bg1", "error")
        reg.clear_session("s1")
        visible = reg.get_visible("s1")
        assert [t["id"] for t in visible] == ["bg1"]
        assert visible[0]["status"] == "error"

    def test_completed_retained_briefly_for_unpark_handoff(self, reg):
        # Regression: unparked-while-away session loaded as idle. In the gap
        # between complete_task (task drops from get_active) and the follow-up
        # turn's unpark_pending latch, /live read (is_streaming=false,
        # activeTasks=[], unpark_pending=false) — a poll landing there stopped
        # the poller and a session load classified the session idle, so the
        # unpark follow-up streamed into a dead UI until a manual refresh.
        # A clean completion must therefore stay visible briefly.
        reg.start_task("s1", "bg1", "agent", "done well")
        reg.complete_task("s1", "bg1", "completed")
        reg.clear_session("s1")
        assert reg.get_active("s1") == []  # control flow: gone immediately
        visible = reg.get_visible("s1")
        assert [t["id"] for t in visible] == ["bg1"]
        assert visible[0]["status"] == "completed"

    def test_completed_leaves_the_taskbar_but_stays_readable(self, reg):
        # The two windows are different lengths on purpose. Past the DISPLAY
        # window the finished row is off /live; the RECORD is still there for
        # the parent that gets woken about it.
        reg.start_task("s1", "bg1", "agent", "done well")
        reg.complete_task("s1", "bg1", "completed")
        entry = reg._recent_terminal["s1"]["bg1"]
        assert entry["ttl"] == _RECENT_COMPLETED_WINDOW_SECONDS
        assert _COMPLETED_VISIBLE_WINDOW_SECONDS < _RECENT_COMPLETED_WINDOW_SECONDS
        reg.clear_session("s1")
        entry["recorded_at"] -= _COMPLETED_VISIBLE_WINDOW_SECONDS + 1.0
        assert reg.get_visible("s1") == []
        assert reg.get_task("s1", "bg1")["status"] == "completed"

    def test_completed_prunes_once_the_hand_off_window_expires(self, reg):
        reg.start_task("s1", "bg1", "agent", "done well")
        reg.complete_task("s1", "bg1", "completed")
        reg.clear_session("s1")
        reg._recent_terminal["s1"]["bg1"]["recorded_at"] -= (
            _RECENT_COMPLETED_WINDOW_SECONDS + 1.0
        )
        assert reg.get_task("s1", "bg1") is None

    def test_failure_stays_visible_past_the_completed_display_window(self, reg):
        # A failure aged past the SHORT display window is still on the taskbar
        # — it gets the long display window so the user actually sees it.
        reg.start_task("s1", "bg1", "agent", "boom")
        reg.complete_task("s1", "bg1", "killed")
        entry = reg._recent_terminal["s1"]["bg1"]
        assert entry["ttl"] == _RECENT_TERMINAL_WINDOW_SECONDS
        entry["recorded_at"] -= _COMPLETED_VISIBLE_WINDOW_SECONDS + 1.0
        assert [t["id"] for t in reg.get_visible("s1")] == ["bg1"]

    def test_get_visible_merges_running_and_failed(self, reg):
        reg.start_task("s1", "live", "agent", "still going")
        reg.start_task("s1", "dead", "shell", "crashed")
        reg.complete_task("s1", "dead", "killed")
        # Deterministic newest-first ordering by startedAt.
        reg._tasks["s1"]["live"]["started_at"] = "2024-01-02T00:00:00+00:00"
        reg._recent_terminal["s1"]["dead"]["task"]["startedAt"] = "2024-01-01T00:00:00+00:00"
        visible = reg.get_visible("s1")
        assert [t["id"] for t in visible] == ["live", "dead"]
        # get_active stays running-only — control flow is unaffected.
        assert [t["id"] for t in reg.get_active("s1")] == ["live"]

    def test_get_visible_dedupes_live_over_retained(self, reg):
        # Defensive: a task id present in BOTH the live map (running) and the
        # retention store must appear once, with the live record winning.
        reg.start_task("s1", "t1", "agent", "live")
        reg._recent_terminal.setdefault("s1", {})["t1"] = {
            "task": {"id": "t1", "subject": "stale", "status": "killed",
                     "kind": "agent", "summary": None,
                     "startedAt": "2024-01-01T00:00:00+00:00",
                     "updatedAt": "2024-01-01T00:00:00+00:00"},
            "recorded_at": 1.0e12,  # far future -> not pruned
        }
        visible = reg.get_visible("s1")
        assert [t["id"] for t in visible] == ["t1"]
        assert visible[0]["status"] == "running"  # live record wins

    def test_retention_expires_after_window(self, reg):
        reg.start_task("s1", "bg1", "agent", "background work")
        reg.complete_task("s1", "bg1", "killed")
        reg.clear_session("s1")
        assert [t["id"] for t in reg.get_visible("s1")] == ["bg1"]
        # Age the retained entry past the window — the next read prunes it.
        reg._recent_terminal["s1"]["bg1"]["recorded_at"] = 0.0
        assert reg.get_visible("s1") == []
        # The empty bucket is dropped rather than lingering forever.
        assert "s1" not in reg._recent_terminal

    def test_get_visible_unknown_session_empty(self, reg):
        assert reg.get_visible("missing") == []

    def test_retention_independent_per_session(self, reg):
        reg.start_task("s1", "a", "agent", "a")
        reg.start_task("s2", "b", "agent", "b")
        reg.complete_task("s1", "a", "killed")
        reg.complete_task("s2", "b", "killed")
        reg.clear_session("s1")
        # s1's failure survives clear; s2 is untouched and still surfaces too.
        assert [t["id"] for t in reg.get_visible("s1")] == ["a"]
        assert [t["id"] for t in reg.get_visible("s2")] == ["b"]
        # s1's retained failure never leaks into s2.
        assert {t["id"] for t in reg.get_visible("s2")} == {"b"}

    def test_reset_all_clears_retention(self, reg):
        reg.start_task("s1", "bg1", "agent", "x")
        reg.complete_task("s1", "bg1", "killed")
        reg.reset_all()
        assert reg.get_visible("s1") == []
        assert reg._recent_terminal == {}

    def test_reaped_failure_resnapshots_status(self, reg):
        # The retention snapshot reflects the status at completion time, not a
        # later re-registration (start_task never flips a terminal task running).
        reg.start_task("s1", "bg1", "agent", "x")
        reg.complete_task("s1", "bg1", "killed")
        reg.start_task("s1", "bg1", "agent", "late start")  # must NOT revive it
        assert reg.get_active("s1") == []
        assert reg.get_visible("s1")[0]["status"] == "killed"


# =============================================================================
# TaskList MCP tool envelope
# =============================================================================

def _build_task_list_handler(session_id):
    """Build the builtins MCP server and return the TaskList tool's async handler.

    Captures the tools list passed to create_tool_server so we can invoke the
    real handler directly without standing up an MCP transport. create_builtin_tools_server
    imports create_tool_server lazily from app.mcp.inproc, so we patch it there.
    """
    import app.mcp.inproc as sdk
    import app.mcp.builtin_tools as bt

    captured = {}
    real_create = sdk.create_tool_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_tool_server", side_effect=_spy):
        bt.create_builtin_tools_server(session_id=session_id, profile_id="p", project_id=None)

    tools = {t.name: t for t in captured["tools"]}
    assert "TaskList" in tools, "TaskList tool was not registered"
    return tools["TaskList"].handler


def _parse_envelope(result):
    """Pull the JSON payload out of the SDK MCP _ok/_err envelope."""
    return json.loads(result["content"][0]["text"])


class TestTaskListTool:
    @pytest.mark.asyncio
    async def test_requires_session(self):
        handler = _build_task_list_handler(session_id=None)
        result = await handler({})
        payload = _parse_envelope(result)
        assert payload["success"] is False
        assert "active session" in payload["error"]

    @pytest.mark.asyncio
    async def test_returns_running_and_count(self):
        task_registry.reset_all()
        task_registry.start_task("sess", "t1", "agent", "live one")
        task_registry.start_task("sess", "t2", "shell", "finished one")
        task_registry.complete_task("sess", "t2", "completed")

        handler = _build_task_list_handler(session_id="sess")

        # Default include_finished=True -> both tasks, running_count counts only running.
        payload = _parse_envelope(await handler({}))
        assert payload["success"] is True
        assert payload["running_count"] == 1
        ids = {t["id"] for t in payload["tasks"]}
        assert ids == {"t1", "t2"}
        # Each task uses the camelCase wire shape.
        sample = payload["tasks"][0]
        assert set(sample.keys()) == {
            "id", "subject", "status", "kind", "summary", "startedAt",
            "updatedAt", "toolUseId",
        }

        # include_finished=False -> only running tasks.
        payload2 = _parse_envelope(await handler({"include_finished": False}))
        assert payload2["running_count"] == 1
        assert [t["id"] for t in payload2["tasks"]] == ["t1"]

        task_registry.reset_all()


# =============================================================================
# /live response carries activeTasks
# =============================================================================

class TestLiveActiveTasks:
    """V2: /live serves the persisted transcript plus lifecycle fields.

    ``activeTasks`` / ``unpark_pending`` are computed by
    ``lifecycle_state.compute`` straight from the task registry (the old
    in-memory live-store path is deleted) and attached to every local
    response. These tests drive the REAL endpoint with only ``require_session``
    and the cloud-relay check stubbed — registry, lifecycle compute and the
    (empty) DB loaders all run for real.
    """

    @staticmethod
    async def _call_live(session_id):
        from app.api import sessions as sessions_api

        fake_session = {"id": session_id, "title": "T"}
        with patch.object(
            sessions_api, "require_session", new=AsyncMock(return_value=fake_session)
        ):
            return await sessions_api.get_live_session(
                request=object(),
                session_id=session_id,
                token="tok",
            )

    @pytest.mark.asyncio
    async def test_live_includes_active_tasks_for_session(self):
        """The /live endpoint attaches a top-level activeTasks field."""
        from app.core.task_registry import task_registry as tr

        session_id = "live-sess"
        tr.reset_all()
        tr.start_task(session_id, "bg1", "shell", "tail -f log")

        result = await self._call_live(session_id)

        assert isinstance(result, dict)
        assert "activeTasks" in result
        assert [t["id"] for t in result["activeTasks"]] == ["bg1"]
        assert result["activeTasks"][0]["kind"] == "shell"

        tr.reset_all()

    @pytest.mark.asyncio
    async def test_live_active_tasks_empty_when_none(self):
        from app.core.task_registry import task_registry as tr

        session_id = "live-sess-empty"
        tr.reset_all()

        result = await self._call_live(session_id)

        assert result["activeTasks"] == []
        # unpark_pending defaults to False and is always present.
        assert result["unpark_pending"] is False
        tr.reset_all()

    @pytest.mark.asyncio
    async def test_live_surfaces_reaped_failure_after_clear_session(self):
        """/live surfaces a dead background task as failed instead of vanishing.

        Reproduces the silent-vanish bug end-to-end at the endpoint: a task is
        reaped to "killed" and the registry's live bucket is cleared (what
        force-cleanup does), yet /live still carries the failed row in
        activeTasks because the endpoint reads the DISPLAY view (get_visible),
        which retains the failure for a window.
        """
        from app.core.task_registry import task_registry as tr

        session_id = "live-reaped"
        tr.reset_all()

        # Background task runs, then its SDK client dies: reap to killed + clear.
        tr.start_task(session_id, "bg1", "agent", "doomed work")
        tr.complete_task(session_id, "bg1", "killed")
        tr.clear_session(session_id)
        assert tr.get_active(session_id) == []  # gone from the running-only view

        result = await self._call_live(session_id)

        # The failed row is surfaced (not silently dropped) with its killed status.
        assert [t["id"] for t in result["activeTasks"]] == ["bg1"]
        assert result["activeTasks"][0]["status"] == "killed"

        # Once the retention window lapses, the row ages out of activeTasks.
        tr._recent_terminal[session_id]["bg1"]["recorded_at"] = 0.0
        result2 = await self._call_live(session_id)
        assert result2["activeTasks"] == []

        tr.reset_all()


# =============================================================================
# get_task accessor
# =============================================================================

class TestGetTask:
    def test_returns_wire_dict_for_known_task(self, reg):
        reg.start_task("s1", "t1", "agent", "do work", summary="halfway")
        wire = reg.get_task("s1", "t1")
        assert wire is not None
        assert wire["id"] == "t1"
        assert wire["kind"] == "agent"
        assert wire["subject"] == "do work"
        assert set(wire.keys()) == {
            "id", "subject", "status", "kind", "summary", "startedAt",
            "updatedAt", "toolUseId",
        }

    def test_returns_none_for_unknown(self, reg):
        assert reg.get_task("s1", "missing") is None
        reg.start_task("s1", "t1", "shell", "x")
        assert reg.get_task("s1", "other") is None
        assert reg.get_task("other-sess", "t1") is None


# =============================================================================
# TaskGet tool + registry-aware TaskOutput / TaskStop
# =============================================================================

def _build_builtin_handlers(session_id):
    """Build the builtins server and return {tool_name: async handler}."""
    import app.mcp.inproc as sdk
    import app.mcp.builtin_tools as bt

    captured = {}
    real_create = sdk.create_tool_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_tool_server", side_effect=_spy):
        bt.create_builtin_tools_server(
            session_id=session_id, profile_id="p", project_id=None
        )
    return {t.name: t.handler for t in captured["tools"]}


class TestTaskGetTool:
    @pytest.mark.asyncio
    async def test_requires_session(self):
        handlers = _build_builtin_handlers(session_id=None)
        payload = _parse_envelope(await handlers["TaskGet"]({"task_id": "t1"}))
        assert payload["success"] is False
        assert "active session" in payload["error"]

    @pytest.mark.asyncio
    async def test_requires_task_id(self):
        handlers = _build_builtin_handlers(session_id="sess")
        payload = _parse_envelope(await handlers["TaskGet"]({}))
        assert payload["success"] is False
        assert "task_id is required" in payload["error"]

    @pytest.mark.asyncio
    async def test_returns_agent_task_record(self):
        task_registry.reset_all()
        task_registry.start_task("sess", "a1", "agent", "background agent", summary="working")
        handlers = _build_builtin_handlers(session_id="sess")
        payload = _parse_envelope(await handlers["TaskGet"]({"task_id": "a1"}))
        assert payload["success"] is True
        assert payload["task"]["id"] == "a1"
        assert payload["task"]["kind"] == "agent"
        # No Monitor shell process backs this id -> no live-process fields.
        assert "running" not in payload
        task_registry.reset_all()

    @pytest.mark.asyncio
    async def test_unknown_task_not_found(self):
        task_registry.reset_all()
        handlers = _build_builtin_handlers(session_id="sess")
        payload = _parse_envelope(await handlers["TaskGet"]({"task_id": "nope"}))
        assert payload["success"] is False
        assert "not found" in payload["error"]
        task_registry.reset_all()


class TestRegistryAwareOutputStop:
    @pytest.mark.asyncio
    async def test_task_output_falls_back_to_agent_record(self):
        task_registry.reset_all()
        task_registry.start_task("sess", "a1", "agent", "agent task", summary="mid")
        handlers = _build_builtin_handlers(session_id="sess")
        payload = _parse_envelope(await handlers["TaskOutput"]({"task_id": "a1"}))
        assert payload["success"] is True
        assert payload["kind"] == "agent"
        assert payload["status"] == "running"
        assert payload["lines"] == []
        assert "note" in payload
        task_registry.reset_all()

    @pytest.mark.asyncio
    async def test_task_output_unknown_still_not_found(self):
        task_registry.reset_all()
        handlers = _build_builtin_handlers(session_id="sess")
        payload = _parse_envelope(await handlers["TaskOutput"]({"task_id": "ghost"}))
        assert payload["success"] is False
        assert "not found" in payload["error"]
        task_registry.reset_all()

    @pytest.mark.asyncio
    async def test_task_stop_agent_task_reports_unsupported(self):
        task_registry.reset_all()
        task_registry.start_task("sess", "a1", "agent", "agent task")
        handlers = _build_builtin_handlers(session_id="sess")
        payload = _parse_envelope(await handlers["TaskStop"]({"task_id": "a1"}))
        assert payload["success"] is False
        assert "cannot" in payload["error"].lower()
        assert payload.get("kind") == "agent"
        task_registry.reset_all()
