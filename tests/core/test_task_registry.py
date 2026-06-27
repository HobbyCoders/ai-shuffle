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

from app.core.task_registry import TaskRegistry, to_wire, task_registry


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

    @pytest.mark.parametrize("status", ["completed", "error", "killed"])
    def test_complete_terminal_statuses(self, reg, status):
        reg.start_task("s1", "t1", "shell", "cmd")
        reg.complete_task("s1", "t1", status)
        assert reg.get_active("s1") == []
        assert reg.get_all("s1")[0]["status"] == status


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
        }
        # Exact key set — no snake_case leak, no extras.
        assert set(wire.keys()) == {
            "id", "subject", "status", "kind", "summary", "startedAt", "updatedAt"
        }


# =============================================================================
# clear_session / reset_all / unknown no-ops
# =============================================================================

class TestMarkNotified:
    def test_true_once_then_false_for_same_signature(self, reg):
        # First time a (task_id, uuid) is seen -> True; duplicate -> False.
        assert reg.mark_notified("s1", "t1", "uuid-1") is True
        assert reg.mark_notified("s1", "t1", "uuid-1") is False
        assert reg.mark_notified("s1", "t1", "uuid-1") is False

    def test_new_uuid_rearms_for_same_task(self, reg):
        assert reg.mark_notified("s1", "t1", "uuid-1") is True
        assert reg.mark_notified("s1", "t1", "uuid-1") is False
        # A NEW uuid (the task was resumed and notified again) re-arms -> True.
        assert reg.mark_notified("s1", "t1", "uuid-2") is True
        assert reg.mark_notified("s1", "t1", "uuid-2") is False

    def test_independent_per_session(self, reg):
        assert reg.mark_notified("s1", "t1", "uuid-1") is True
        # Same task_id + signature in a DIFFERENT session is independent.
        assert reg.mark_notified("s2", "t1", "uuid-1") is True

    def test_blank_ids_never_latch(self, reg):
        assert reg.mark_notified("", "t1", "sig") is False
        assert reg.mark_notified("s1", "", "sig") is False
        # No state stranded -> a real (session, task) still latches True.
        assert reg.mark_notified("s1", "t1", "sig") is True

    def test_cleared_by_clear_session(self, reg):
        assert reg.mark_notified("s1", "t1", "uuid-1") is True
        reg.clear_session("s1")
        # After clearing, the same signature re-arms (fresh session state).
        assert reg.mark_notified("s1", "t1", "uuid-1") is True

    def test_cleared_by_reset_all(self, reg):
        assert reg.mark_notified("s1", "t1", "uuid-1") is True
        reg.reset_all()
        assert reg.mark_notified("s1", "t1", "uuid-1") is True


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


class TestUnparkPending:
    """The latch bridging the unpark hand-off gap.

    Set the instant a parked session's background sub-agent completion is handed
    off to a follow-up turn (the task has already dropped out of get_active), and
    cleared the moment that turn starts streaming (or it fails). /live surfaces it
    so a polling client keeps its park-poll alive across the gap instead of seeing
    a transient (is_streaming=false, activeTasks=[]) and STOPPING.
    """

    def test_default_false(self, reg):
        assert reg.is_unpark_pending("s1") is False

    def test_mark_then_clear(self, reg):
        reg.mark_unpark_pending("s1")
        assert reg.is_unpark_pending("s1") is True
        reg.clear_unpark_pending("s1")
        assert reg.is_unpark_pending("s1") is False

    def test_independent_per_session(self, reg):
        reg.mark_unpark_pending("s1")
        assert reg.is_unpark_pending("s1") is True
        assert reg.is_unpark_pending("s2") is False

    def test_blank_id_is_noop(self, reg):
        reg.mark_unpark_pending("")
        assert reg.is_unpark_pending("") is False
        reg.clear_unpark_pending("")  # must not raise

    def test_cleared_by_clear_session(self, reg):
        reg.mark_unpark_pending("s1")
        reg.mark_unpark_pending("s2")
        reg.clear_session("s1")
        assert reg.is_unpark_pending("s1") is False
        assert reg.is_unpark_pending("s2") is True

    def test_cleared_by_reset_all(self, reg):
        reg.mark_unpark_pending("s1")
        reg.reset_all()
        assert reg.is_unpark_pending("s1") is False


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
# TaskList MCP tool envelope
# =============================================================================

def _build_task_list_handler(session_id):
    """Build the builtins MCP server and return the TaskList tool's async handler.

    Captures the tools list passed to create_sdk_mcp_server so we can invoke the
    real handler directly without standing up an MCP transport. create_builtin_tools_server
    imports create_sdk_mcp_server lazily from claude_agent_sdk, so we patch it there.
    """
    import claude_agent_sdk as sdk
    import app.mcp.builtin_tools as bt

    captured = {}
    real_create = sdk.create_sdk_mcp_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_sdk_mcp_server", side_effect=_spy):
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
            "id", "subject", "status", "kind", "summary", "startedAt", "updatedAt"
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
    @pytest.mark.asyncio
    async def test_live_includes_active_tasks_for_streaming_session(self):
        """The /live endpoint attaches a top-level activeTasks field.

        Drives the in-memory live-store path (session present + since_update_id=0),
        which returns get_session_dict without touching the DB message loaders.
        require_session and the live store are stubbed so no Postgres is needed.
        """
        from app.api import sessions as sessions_api
        from app.core.live_session_store import live_session_store
        from app.core.task_registry import task_registry as tr

        session_id = "live-sess"
        tr.reset_all()
        tr.start_task(session_id, "bg1", "shell", "tail -f log")

        # Seed a streaming live session so the in-memory path is taken.
        await live_session_store.start_streaming(session_id, initial_messages=[])

        fake_session = {"id": session_id, "title": "T"}

        with patch.object(
            sessions_api, "require_session", new=AsyncMock(return_value=fake_session)
        ):
            result = await sessions_api.get_live_session(
                request=object(),
                session_id=session_id,
                since_update_id=0,
                token="tok",
            )

        assert isinstance(result, dict)
        assert "activeTasks" in result
        assert [t["id"] for t in result["activeTasks"]] == ["bg1"]
        assert result["activeTasks"][0]["kind"] == "shell"

        await live_session_store.cleanup_session(session_id)
        tr.reset_all()

    @pytest.mark.asyncio
    async def test_live_active_tasks_empty_when_none(self):
        from app.api import sessions as sessions_api
        from app.core.live_session_store import live_session_store
        from app.core.task_registry import task_registry as tr

        session_id = "live-sess-empty"
        tr.reset_all()
        await live_session_store.start_streaming(session_id, initial_messages=[])
        fake_session = {"id": session_id, "title": "T"}

        with patch.object(
            sessions_api, "require_session", new=AsyncMock(return_value=fake_session)
        ):
            result = await sessions_api.get_live_session(
                request=object(),
                session_id=session_id,
                since_update_id=0,
                token="tok",
            )

        assert result["activeTasks"] == []
        # unpark_pending defaults to False and is always present.
        assert result["unpark_pending"] is False
        await live_session_store.cleanup_session(session_id)
        tr.reset_all()

    @pytest.mark.asyncio
    async def test_live_surfaces_then_clears_unpark_pending(self):
        """/live carries the unpark-pending latch; start_streaming clears it.

        Reproduces the hand-off gap: a parked session's sub-agent completed (so it
        has no activeTasks) but the follow-up turn hasn't flipped is_streaming yet.
        The latch must surface on /live so the poller keeps park-polling, and must
        clear the instant the follow-up turn starts streaming (the wake).
        """
        from app.api import sessions as sessions_api
        from app.core.live_session_store import live_session_store
        from app.core.task_registry import task_registry as tr

        session_id = "live-unpark"
        tr.reset_all()
        await live_session_store.start_streaming(session_id, initial_messages=[])

        # The unpark hand-off latches "pending" after the parked turn, before the
        # follow-up streams. (No active tasks — the sub-agent already completed.)
        tr.mark_unpark_pending(session_id)
        assert tr.is_unpark_pending(session_id) is True

        fake_session = {"id": session_id, "title": "T"}
        with patch.object(
            sessions_api, "require_session", new=AsyncMock(return_value=fake_session)
        ):
            result = await sessions_api.get_live_session(
                request=object(),
                session_id=session_id,
                since_update_id=0,
                token="tok",
            )
        assert result["unpark_pending"] is True
        assert result["activeTasks"] == []

        # The follow-up turn starting to stream is the wake — it clears the latch
        # (it flips together with is_streaming, so the poller upshifts on its own).
        await live_session_store.start_streaming(session_id, initial_messages=[])
        assert tr.is_unpark_pending(session_id) is False

        await live_session_store.cleanup_session(session_id)
        tr.reset_all()

    @pytest.mark.asyncio
    async def test_end_streaming_clears_latch_even_when_session_absent(self):
        """end_streaming clears the unpark latch ABOVE its not-found guard.

        Backstop hardening: if a follow-up unpark turn dies/cancels before its
        start_streaming (the primary clear) AND its session buffer was already
        evicted, the error-path end_streaming finds no session. The clear must
        still fire (it sits above the `if not session: return None` guard) so the
        latch can't strand a polling client until the ~30s reaper.
        """
        from app.core.live_session_store import live_session_store
        from app.core.task_registry import task_registry as tr

        session_id = "live-unpark-evicted"
        tr.reset_all()

        # Latch set, but the session is NOT in the live store (already reaped).
        tr.mark_unpark_pending(session_id)
        assert live_session_store.get_session(session_id) is None
        assert tr.is_unpark_pending(session_id) is True

        # end_streaming returns None (session not found) yet still clears the latch.
        result = await live_session_store.end_streaming(
            session_id=session_id, metadata={}, interrupted=True
        )
        assert result is None
        assert tr.is_unpark_pending(session_id) is False

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
            "id", "subject", "status", "kind", "summary", "startedAt", "updatedAt"
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
    import claude_agent_sdk as sdk
    import app.mcp.builtin_tools as bt

    captured = {}
    real_create = sdk.create_sdk_mcp_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_sdk_mcp_server", side_effect=_spy):
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
