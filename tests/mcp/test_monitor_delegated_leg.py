"""The delegated (Claude Code CLI) leg's Monitor — round-6 audit.

The native leg gets fluid's Monitor: the park doctrine is spliced into its
description and its `cwd` defaults to the session's own directory. The bridged
MCP copy had neither. Two audited consequences:

* an Opus session on this lane never called Monitor once and spent 26.7 of its
  45 minutes in blocking Bash sleep-polls, one loop of ten minutes returning
  nothing — the description read as a tool for dev servers;
* Monitor shells inherited the SERVER's working directory, so `gh`/`git`
  commands launched from a worktree session died with "fatal: not a git
  repository", costing a park, a wake and a turn each.
"""

import asyncio
from unittest.mock import patch

import pytest

from tests.mcp.tool_helpers import tool_result_json


def _builtin_tools(session_id="session-1", cwd=None):
    import app.mcp.inproc as sdk

    from app.mcp import builtin_tools

    captured = {}
    real_create = sdk.create_tool_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_tool_server", side_effect=_spy):
        builtin_tools.create_builtin_tools_server(session_id=session_id, cwd=cwd)

    return {tool.name: tool for tool in captured["tools"]}


class _FakeTask:
    def __init__(self, task_id="t-del"):
        self.task_id = task_id
        self.command = "cmd"
        self.cwd = None
        self.lines = []
        self.exit_code = None
        self.started_at = "2026-09-04T00:00:00Z"
        self.process = None
        self._running = True

    def is_running(self):
        return self._running


@pytest.fixture
def fake_shell(monkeypatch):
    """Replace the real subprocess spawn with a task the test drives."""
    from app.mcp import builtin_tools as bt

    task = _FakeTask()

    async def _fake_spawn_background(command, cwd, session_id, tool_use_id=None):
        task.command = command
        task.cwd = cwd
        return task

    monkeypatch.setattr(bt, "_spawn_background", _fake_spawn_background)
    return task


# ─── the description carries the park doctrine ───────────────────────────────


def test_the_description_says_park_not_poll(mock_db):
    description = _builtin_tools()["Monitor"].description.lower()

    assert "end your turn" in description
    assert "do not sleep, poll" in description
    # And it names the trigger, so it is not read as a tool for servers only.
    assert "test suite" in description
    assert "outlast a bash call" in description


def test_the_description_still_explains_the_surviving_shell(mock_db):
    description = _builtin_tools()["Monitor"].description

    assert "User Stop ends" in description
    assert "including serving tasks" in description


def test_the_park_doctrine_is_fluids_own_wording(mock_db):
    from fluid.tools.fragments import END_TURN_ON_BACKGROUND

    assert END_TURN_ON_BACKGROUND in _builtin_tools()["Monitor"].description


# ─── the session's working directory ─────────────────────────────────────────


def test_monitor_falls_back_to_the_session_cwd(mock_db, fake_shell):
    monitor = _builtin_tools(cwd="/work/tree")["Monitor"]

    asyncio.run(monitor.handler({"command": "gh pr checks 7 --watch"}))

    assert fake_shell.cwd == "/work/tree"


def test_an_explicit_cwd_still_wins(mock_db, fake_shell):
    monitor = _builtin_tools(cwd="/work/tree")["Monitor"]

    asyncio.run(monitor.handler({"command": "make ci", "cwd": "/elsewhere"}))

    assert fake_shell.cwd == "/elsewhere"


# ─── wait_seconds ────────────────────────────────────────────────────────────


def test_the_schema_advertises_the_wait(mock_db):
    from app.core.monitor_spawn import MAX_WAIT_SECONDS

    schema = _builtin_tools()["Monitor"].input_schema

    assert schema["properties"]["wait_seconds"]["maximum"] == MAX_WAIT_SECONDS
    assert "wait_seconds" not in schema["required"]


def test_a_command_that_finishes_in_the_wait_settles_inline(mock_db, fake_shell):
    fake_shell._running = False
    fake_shell.exit_code = 0
    fake_shell.lines = ["4 passed in 5.91s"]
    monitor = _builtin_tools()["Monitor"]

    body = tool_result_json(asyncio.run(monitor.handler(
        {"command": "pytest -q", "wait_seconds": 30}
    )))

    assert body["success"] is True
    assert body["exit_code"] == 0
    assert body["status"] == "completed"
    assert "4 passed in 5.91s" in body["output"]
    # No park instruction: there is nothing left to be woken for.
    assert "End the turn now" not in body["message"]


def test_an_inline_failure_is_reported_as_one(mock_db, fake_shell):
    fake_shell._running = False
    fake_shell.exit_code = 1
    fake_shell.lines = ["FAILED tests/test_a.py::test_b"]
    monitor = _builtin_tools()["Monitor"]

    body = tool_result_json(asyncio.run(monitor.handler(
        {"command": "pytest -q", "wait_seconds": 30}
    )))

    # A SUCCESSFUL tool result carrying a failed command: `success: false`
    # sets isError, which the code-execution runner raises on, and a red test
    # suite then took the whole cell (and its queued calls) with it.
    assert body["success"] is True
    assert body["failed"] is True
    assert body["exit_code"] == 1
    assert body["status"] == "error"
    assert "FAILED tests/test_a.py::test_b" in body["output"]


def test_a_command_still_running_after_the_wait_parks_as_before(
    mock_db, fake_shell
):
    from app.core.monitor_spawn import monitor_receipt_text

    monitor = _builtin_tools()["Monitor"]

    body = tool_result_json(asyncio.run(monitor.handler(
        {"command": "npm run build", "wait_seconds": 1}
    )))

    assert body["success"] is True
    assert body["task_id"] == fake_shell.task_id
    # The existing receipt wording is untouched; the wait only adds a clause.
    assert monitor_receipt_text(fake_shell.task_id) in body["message"]
    assert "still running after the 1s" in body["message"]


def test_no_wait_means_the_old_behaviour_exactly(mock_db, fake_shell):
    from app.core.monitor_spawn import monitor_receipt_text

    monitor = _builtin_tools()["Monitor"]

    body = tool_result_json(asyncio.run(monitor.handler({"command": "npm run build"})))

    assert body["message"].startswith(monitor_receipt_text(fake_shell.task_id))
    assert "time budget" in body["message"]
