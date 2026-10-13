"""The app-config MCP tools must not do blocking DB work on the event loop.

These are the tools Claude itself calls to read and change configuration
*mid-turn*. A synchronous psycopg2 call made directly from one of their
handlers freezes every other WebSocket, SSE generator and HTTP request served
by the same worker for the duration of the query — at the worst possible
moment, while a turn is in flight.

The tests pin the fix by identity rather than by wire shape:

* every DB call a handler makes must run on a thread that is NOT the loop
  thread, and
* the DB work of one tool call must cost ONE thread hop, not one hop per query
  (61 hops and the pool churn that comes with them is not a fix), and
* a read-modify-write tool (updateProfile, updateHook, updateAutomation,
  toggleHook, toggleAutomation, updateSubagent) must do its read and its write
  inside the SAME hop — with no event-loop suspension point between them, so
  the loop cannot run another turn's tool call in the gap and let the two
  writes clobber each other.

Companion behavioural coverage lives in test_config_profiles.py /
test_config_subagents.py; this file only asserts where the work runs.
"""

import threading
from unittest.mock import patch

import pytest

from app.db import async_database
from app.mcp.app_config_sdk import _create_discovery_tools
from app.mcp.config_automations import _create_automation_tools
from app.mcp.config_connectors import _create_connector_tools
from app.mcp.config_hooks import _create_hook_tools
from app.mcp.config_profiles import _create_profile_tools
from app.mcp.config_subagents import _create_subagent_tools
from tests.mcp.tool_helpers import get_tool, tool_result_json

# =============================================================================
# Helpers
# =============================================================================

class _ThreadSpy:
    """Stand in for a DB function, recording the thread each call ran on."""

    def __init__(self, return_value=None, log=None, label=None):
        self._return_value = return_value
        self.threads: list[int] = []
        self.calls: list[tuple] = []
        self._log = log
        self._label = label

    def __call__(self, *args, **kwargs):
        self.threads.append(threading.get_ident())
        self.calls.append((args, kwargs))
        if self._log is not None:
            self._log.append(self._label)
        return self._return_value


class _HopCounter:
    """Count the ``run_sync`` offloads a tool handler performs.

    The tool factories bind ``run_sync`` as a local at factory-call time, so the
    tools must be BUILT inside ``install()`` for the counting wrapper to be the
    one they use.
    """

    def __init__(self):
        self.hops = 0

    def install(self):
        real = async_database.run_sync

        async def counting(fn, *args, **kwargs):
            self.hops += 1
            return await real(fn, *args, **kwargs)

        return patch.object(async_database, "run_sync", counting)


@pytest.fixture
def hops():
    return _HopCounter()


def _profile(**kw):
    row = {
        "id": "p1",
        "name": "P1",
        "description": None,
        "is_builtin": False,
        "config": {"tools_preset": "claude_code"},
    }
    row.update(kw)
    return row


def _automation(**kw):
    row = {
        "id": "auto-1",
        "name": "Auto One",
        "trigger_type": "cron",
        "trigger_config": {"expression": "0 9 * * *", "timezone": "UTC"},
        "prompt": "go",
        "profile_id": "p1",
        "is_enabled": True,
        "status": "idle",
    }
    row.update(kw)
    return row


def _hook(**kw):
    row = {
        "id": "hook-1",
        "name": "Hook One",
        "event": "PreToolUse",
        "hook_type": "bash",
        "command": "echo hi",
        "enabled": True,
    }
    row.update(kw)
    return row


def _subagent(**kw):
    row = {
        "id": "s1",
        "name": "S1",
        "description": "d",
        "prompt": "p",
        "is_builtin": False,
    }
    row.update(kw)
    return row


# =============================================================================
# Read tools — the DB read must not happen on the loop
# =============================================================================

class TestReadToolsOffload:
    async def test_list_automations_reads_off_the_loop(self, loop_thread, hops):
        spy = _ThreadSpy(return_value=[_automation()])
        with hops.install():
            tools = _create_automation_tools(is_admin=True, project_id=None)
        tool = get_tool(tools, "listAutomations")
        with patch("app.db.database.get_all_automations", spy):
            body = tool_result_json(await tool.handler({}))

        assert body["count"] == 1
        assert spy.threads and loop_thread not in spy.threads
        assert hops.hops == 1

    async def test_get_automation_folds_row_and_runs_into_one_hop(
        self, loop_thread, hops
    ):
        """Two queries, ONE offload — not one hop each."""
        get_spy = _ThreadSpy(return_value=_automation())
        runs_spy = _ThreadSpy(return_value=[{"id": "r1", "status": "success"}])
        with hops.install():
            tools = _create_automation_tools(is_admin=True, project_id=None)
        tool = get_tool(tools, "getAutomation")
        with patch("app.db.database.get_automation", get_spy), \
             patch("app.db.database.get_automation_runs", runs_spy):
            body = tool_result_json(await tool.handler({"automation_id": "auto-1"}))

        assert [r["id"] for r in body["recent_runs"]] == ["r1"]
        assert hops.hops == 1
        assert get_spy.threads == runs_spy.threads  # same worker thread
        assert loop_thread not in get_spy.threads

    async def test_get_automation_skips_the_runs_query_when_missing(self, hops):
        """The second query must still be conditional on the first — folding
        them into one closure must not turn one query into two."""
        runs_spy = _ThreadSpy(return_value=[])
        with hops.install():
            tools = _create_automation_tools(is_admin=True, project_id=None)
        tool = get_tool(tools, "getAutomation")
        with patch("app.db.database.get_automation", _ThreadSpy(return_value=None)), \
             patch("app.db.database.get_automation_runs", runs_spy):
            body = tool_result_json(await tool.handler({"automation_id": "nope"}))

        assert body["success"] is False
        assert "not found" in body["error"].lower()
        assert runs_spy.calls == []

    async def test_list_hooks_scope_branch_runs_off_the_loop(
        self, loop_thread, hops
    ):
        """Exactly one of the four scope reads runs, and it runs off-loop."""
        project_spy = _ThreadSpy(return_value=[_hook()])
        app_spy = _ThreadSpy(return_value=[])
        with hops.install():
            tools = _create_hook_tools(is_admin=True, project_id="proj-1")
        tool = get_tool(tools, "listHooks")
        with patch("app.db.database.get_hooks_for_project", project_spy), \
             patch("app.db.database.get_app_hooks", app_spy):
            body = tool_result_json(await tool.handler({"scope": "project"}))

        assert body["count"] == 1
        assert app_spy.calls == []
        assert project_spy.threads and loop_thread not in project_spy.threads
        assert hops.hops == 1

    async def test_list_profiles_reads_off_the_loop(self, loop_thread, hops):
        spy = _ThreadSpy(return_value=[_profile()])
        with hops.install():
            tools = _create_profile_tools(is_admin=True)
        tool = get_tool(tools, "listProfiles")
        with patch("app.db.database.get_all_profiles", spy):
            body = tool_result_json(await tool.handler({}))

        assert body["count"] == 1
        assert spy.threads and loop_thread not in spy.threads
        assert hops.hops == 1

    async def test_list_subagents_reads_off_the_loop(self, loop_thread, hops):
        spy = _ThreadSpy(return_value=[_subagent()])
        with hops.install():
            tools = _create_subagent_tools(is_admin=True)
        tool = get_tool(tools, "listSubagents")
        with patch("app.db.database.get_all_subagents", spy):
            body = tool_result_json(await tool.handler({}))

        assert body["count"] == 1
        assert spy.threads and loop_thread not in spy.threads
        assert hops.hops == 1

    async def test_list_connectors_reads_off_the_loop(self, loop_thread, hops):
        spy = _ThreadSpy(return_value=[{"id": "c1", "name": "C1", "type": "telegram"}])
        with hops.install():
            tools = _create_connector_tools(is_admin=True)
        tool = get_tool(tools, "listConnectors")
        with patch("app.db.database.get_all_connectors", spy):
            body = tool_result_json(await tool.handler({}))

        assert body["count"] == 1
        assert spy.threads and loop_thread not in spy.threads
        assert hops.hops == 1

    async def test_get_capabilities_folds_three_reads_into_one_hop(
        self, loop_thread, hops
    ):
        """getConfigCapabilities is the tool agents are told to call FIRST in a
        config turn — its three counts must cost one hop, not three."""
        h = _ThreadSpy(return_value=[_hook()])
        a = _ThreadSpy(return_value=[_automation()])
        p = _ThreadSpy(return_value=[_profile()])
        with hops.install():
            tools = _create_discovery_tools()
        tool = get_tool(tools, "getConfigCapabilities")
        with patch("app.db.database.get_app_hooks", h), \
             patch("app.db.database.get_all_automations", a), \
             patch("app.db.database.get_all_profiles", p):
            await tool.handler({})

        assert hops.hops == 1
        assert h.threads == a.threads == p.threads
        assert loop_thread not in h.threads


# =============================================================================
# Read-modify-write — read and write must share ONE hop
# =============================================================================

class TestReadModifyWriteIsAtomic:
    """A split read-modify-write is a lost-update race.

    If the read and the write sit in separate ``run_sync`` hops, the event loop
    regains control between them and can run a *second* tool call's read. Both
    calls then merge onto the config they each read, and whichever writes last
    silently erases the other's change. Keeping the pair in one closure removes
    the suspension point, so no other turn's tool call can be scheduled in the
    gap.

    Each test asserts the structural property that guarantees this: ONE hop,
    both queries on the SAME non-loop thread, in read → write order.
    """

    async def test_update_profile_reads_and_writes_in_one_hop(
        self, loop_thread, hops
    ):
        order: list[str] = []
        read = _ThreadSpy(return_value=_profile(config={"tools_preset": "claude_code"}),
                          log=order, label="read")
        write = _ThreadSpy(return_value=_profile(name="P1"), log=order, label="write")
        with hops.install():
            tools = _create_profile_tools(is_admin=True)
        tool = get_tool(tools, "updateProfile")
        with patch("app.db.database.get_profile", read), \
             patch("app.db.database.update_profile", write):
            body = tool_result_json(await tool.handler(
                {"profile_id": "p1", "thinking_mode": "think"}
            ))

        assert body["success"] is True
        assert hops.hops == 1
        assert order == ["read", "write"]
        assert read.threads == write.threads
        assert loop_thread not in read.threads
        # ...and the merge really is onto the row read in that same hop.
        assert write.calls[0][1]["config"]["thinking_mode"] == "think"
        assert write.calls[0][1]["config"]["tools_preset"] == "claude_code"

    async def test_update_hook_reads_and_writes_in_one_hop(self, loop_thread, hops):
        order: list[str] = []
        read = _ThreadSpy(return_value=_hook(), log=order, label="read")
        write = _ThreadSpy(return_value=_hook(name="Hook One"), log=order, label="write")
        with hops.install():
            tools = _create_hook_tools(is_admin=True, project_id=None)
        tool = get_tool(tools, "updateHook")
        with patch("app.db.database.get_hook", read), \
             patch("app.db.database.update_hook", write):
            body = tool_result_json(await tool.handler({"hook_id": "hook-1", "name": "New"}))

        assert body["success"] is True
        assert hops.hops == 1
        assert order == ["read", "write"]
        assert read.threads == write.threads
        assert loop_thread not in read.threads

    async def test_toggle_hook_reads_and_writes_in_one_hop(self, loop_thread, hops):
        order: list[str] = []
        read = _ThreadSpy(return_value=_hook(), log=order, label="read")
        write = _ThreadSpy(return_value=_hook(), log=order, label="write")
        with hops.install():
            tools = _create_hook_tools(is_admin=True, project_id=None)
        tool = get_tool(tools, "toggleHook")
        with patch("app.db.database.get_hook", read), \
             patch("app.db.database.update_hook", write):
            body = tool_result_json(await tool.handler({"hook_id": "hook-1", "enabled": False}))

        assert body["success"] is True
        assert hops.hops == 1
        assert order == ["read", "write"]
        assert read.threads == write.threads
        assert loop_thread not in read.threads

    async def test_update_automation_reads_validates_and_writes_in_one_hop(
        self, loop_thread, hops
    ):
        """Existence read, profile validation, UPDATE — one hop. The result's
        model report then reads the agent again in that same hop."""
        order: list[str] = []
        read = _ThreadSpy(return_value=_automation(), log=order, label="read")
        prof = _ThreadSpy(return_value=_profile(), log=order, label="profile")
        write = _ThreadSpy(return_value=_automation(), log=order, label="write")
        with hops.install():
            tools = _create_automation_tools(is_admin=True, project_id=None)
        tool = get_tool(tools, "updateAutomation")
        with patch("app.db.database.get_automation", read), \
             patch("app.db.database.get_profile", prof), \
             patch("app.db.database.update_automation", write):
            body = tool_result_json(await tool.handler(
                {"automation_id": "auto-1", "profile_id": "p1", "name": "New"}
            ))

        assert body["success"] is True
        assert hops.hops == 1
        assert order == ["read", "profile", "write", "profile"]
        assert read.threads == write.threads
        assert set(prof.threads) == set(read.threads)
        assert loop_thread not in read.threads

    @pytest.mark.parametrize("profile_id", [None, ""])
    async def test_toggle_retired_automation_requires_agent(self, loop_thread, hops, profile_id):
        from unittest.mock import AsyncMock

        read = _ThreadSpy(return_value=_automation(profile_id=profile_id, is_enabled=False))
        with hops.install():
            tools = _create_automation_tools(is_admin=True, project_id=None)
        tool = get_tool(tools, "toggleAutomation")
        with patch("app.db.database.get_automation", read), \
             patch("app.db.database.update_automation") as write, \
             patch("app.core.automation_manager.automation_manager.schedule_automation", new_callable=AsyncMock) as schedule:
            body = tool_result_json(await tool.handler({"automation_id": "auto-1", "enabled": True}))
        assert "Choose an agent" in body["error"]
        write.assert_not_called()
        schedule.assert_not_called()
        assert hops.hops == 1
        assert loop_thread not in read.threads

    async def test_toggle_automation_reads_and_writes_in_one_hop(
        self, loop_thread, hops
    ):
        order: list[str] = []
        read = _ThreadSpy(return_value=_automation(), log=order, label="read")
        write = _ThreadSpy(return_value=_automation(), log=order, label="write")
        with hops.install():
            tools = _create_automation_tools(is_admin=True, project_id=None)
        tool = get_tool(tools, "toggleAutomation")
        # The scheduler update after the write is a coroutine on the loop; it is
        # allowed to fail (the tool logs and continues), which is unchanged.
        with patch("app.db.database.get_automation", read), \
             patch("app.db.database.update_automation", write):
            body = tool_result_json(await tool.handler(
                {"automation_id": "auto-1", "enabled": False}
            ))

        assert body["success"] is True
        assert hops.hops == 1
        assert order == ["read", "write"]
        assert read.threads == write.threads
        assert loop_thread not in read.threads

    async def test_update_subagent_reads_and_writes_in_one_hop(
        self, loop_thread, hops
    ):
        order: list[str] = []
        read = _ThreadSpy(return_value=_subagent(), log=order, label="read")
        write = _ThreadSpy(return_value=_subagent(name="S1"), log=order, label="write")
        with hops.install():
            tools = _create_subagent_tools(is_admin=True)
        tool = get_tool(tools, "updateSubagent")
        with patch("app.db.database.get_subagent", read), \
             patch("app.db.database.update_subagent", write):
            body = tool_result_json(await tool.handler({"subagent_id": "s1", "name": "S2"}))

        assert body["success"] is True
        assert hops.hops == 1
        assert order == ["read", "write"]
        assert read.threads == write.threads
        assert loop_thread not in read.threads

    async def test_create_profile_checks_and_inserts_in_one_hop(
        self, loop_thread, hops
    ):
        """The duplicate-ID check only means anything if the INSERT that
        depends on it cannot be scheduled a turn later."""
        order: list[str] = []
        read = _ThreadSpy(return_value=None, log=order, label="read")
        write = _ThreadSpy(return_value=_profile(id="new-one"), log=order, label="write")
        with hops.install():
            tools = _create_profile_tools(is_admin=True)
        tool = get_tool(tools, "createProfile")
        with patch("app.db.database.get_profile", read), \
             patch("app.db.database.create_profile", write):
            body = tool_result_json(await tool.handler({"name": "New One"}))

        assert body["success"] is True
        assert hops.hops == 1
        assert order == ["read", "write"]
        assert read.threads == write.threads
        assert loop_thread not in read.threads

    async def test_delete_profile_guards_and_deletes_in_one_hop(
        self, loop_thread, hops
    ):
        """"No automation still uses this profile" has to be true at the moment
        of deletion, not a few thread hops earlier."""
        order: list[str] = []
        read = _ThreadSpy(return_value=_profile(), log=order, label="read")
        blocking = _ThreadSpy(return_value=[], log=order, label="blocking")
        write = _ThreadSpy(return_value=True, log=order, label="delete")
        with hops.install():
            tools = _create_profile_tools(is_admin=True)
        tool = get_tool(tools, "deleteProfile")
        with patch("app.db.database.get_profile", read), \
             patch("app.db.database.get_automations_for_profile", blocking), \
             patch("app.db.database.delete_profile", write):
            body = tool_result_json(await tool.handler({"profile_id": "p1", "confirm": True}))

        assert body["success"] is True
        assert hops.hops == 1
        assert order == ["read", "blocking", "delete"]
        assert read.threads == blocking.threads == write.threads
        assert loop_thread not in read.threads

    async def test_no_suspension_point_between_the_read_and_the_write(
        self, loop_thread
    ):
        """The guarantee spelled out as a boundary check.

        Instrumenting the offload itself shows the read and the write sit
        INSIDE one dispatch: ``hop-start, read, write, hop-end``. Were they two
        hops the trace would read ``hop-start, read, hop-end, hop-start, write,
        hop-end`` — and each ``hop-end`` is a point where the event loop
        regains control and can resume another turn's config tool between this
        call's read and its write.
        """
        order: list[str] = []
        real = async_database.run_sync

        async def tracing(fn, *args, **kwargs):
            order.append("hop-start")
            try:
                return await real(fn, *args, **kwargs)
            finally:
                order.append("hop-end")

        with patch.object(async_database, "run_sync", tracing):
            tools = _create_profile_tools(is_admin=True)
        tool = get_tool(tools, "updateProfile")
        with patch("app.db.database.get_profile",
                   _ThreadSpy(return_value=_profile(), log=order, label="read")), \
             patch("app.db.database.update_profile",
                   _ThreadSpy(return_value=_profile(), log=order, label="write")):
            body = tool_result_json(await tool.handler(
                {"profile_id": "p1", "thinking_mode": "think"}
            ))

        assert body["success"] is True
        assert order == ["hop-start", "read", "write", "hop-end"]
