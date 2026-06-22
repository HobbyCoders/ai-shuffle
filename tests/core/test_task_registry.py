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

        fake_session = {"id": session_id, "title": "T", "cloud_remote_url": None}

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
        fake_session = {"id": session_id, "title": "T", "cloud_remote_url": None}

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
        await live_session_store.cleanup_session(session_id)
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
