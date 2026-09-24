"""Task tools, app side (round-9 audit, item 7).

(a) "task not found" names the running tasks — models hallucinated 8-hex ids
    five times after compaction and got nothing to correct with.
(b) TaskOutput defaults to a tail; the full buffer is opt-in.
(c) A task the session stopped itself never produces an error/killed steer:
    attribution and wake suppression land BEFORE the kill.
(d) The wake summary budget is ~4,000 chars and an exit-0 truncation says so.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core import monitor_spawn as ms
from app.core.config import settings
from app.core.task_registry import task_registry
from app.mcp import builtin_tools as bt

SESSION = "sess-r9"


@pytest.fixture(autouse=True)
def _clean():
    bt._background_tasks.clear()
    task_registry.reset_all()
    yield
    bt._background_tasks.clear()
    task_registry.reset_all()


def _handlers(session_id=SESSION):
    import app.mcp.inproc as sdk

    captured = {}
    real_create = sdk.create_tool_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_tool_server", side_effect=_spy):
        bt.create_builtin_tools_server(session_id=session_id, profile_id="p", project_id=None)
    return {t.name: t.handler for t in captured["tools"]}


def _body(result):
    return json.loads(result["content"][0]["text"])


def _shell(task_id="t-live", lines=None, running=True):
    task = bt._BackgroundTask(task_id, "npm test", "/work", SESSION)
    task.lines = list(lines or [])
    task.is_running = lambda: running  # type: ignore[method-assign]
    if not running:
        task.exit_code = 0
    bt._background_tasks[task_id] = task
    task_registry.start_task(SESSION, task_id, "shell", "npm test")
    if not running:
        task_registry.complete_task(SESSION, task_id, "completed")
    return task


# ─── (a) not found lists what is running ───────────────────────────────────────


async def test_task_stop_and_get_name_the_running_tasks():
    _shell("a1b2c3d4")
    task_registry.start_task(SESSION, "agent-77", "agent", "Review the diff")
    handlers = _handlers()
    for tool in ("TaskStop", "TaskGet"):
        body = _body(await handlers[tool]({"task_id": "6dc76e90"}))
        assert body["success"] is False
        assert body["error"].startswith("task not found.")
        assert "a1b2c3d4" in body["error"] and "npm test" in body["error"]
        assert "agent-77" in body["error"] and "Review the diff" in body["error"]
        ids = {row["task_id"] for row in body["running_tasks"]}
        assert ids == {"a1b2c3d4", "agent-77"}


async def test_not_found_with_nothing_running_says_so():
    body = _body(await _handlers()["TaskStop"]({"task_id": "ghost"}))
    assert body["error"] == "task not found. No tasks are running in this session."
    assert body["running_tasks"] == []


# ─── (b) TaskOutput defaults to a tail ─────────────────────────────────────────


async def test_task_output_defaults_to_the_last_150_lines():
    _shell(lines=[f"line {i}" for i in range(400)], running=False)
    body = _body(await _handlers()["TaskOutput"]({"task_id": "t-live"}))
    assert body["total_lines"] == 400
    assert body["shown_lines"] == bt._TASK_OUTPUT_DEFAULT_LINES == 150
    assert body["lines"][0] == "line 250" and body["lines"][-1] == "line 399"
    assert "full=true" in body["note"]


async def test_task_output_lines_and_full_opt_in():
    _shell(lines=[f"line {i}" for i in range(400)], running=False)
    handlers = _handlers()
    body = _body(await handlers["TaskOutput"]({"task_id": "t-live", "lines": 10}))
    assert body["lines"] == [f"line {i}" for i in range(390, 400)]
    body = _body(await handlers["TaskOutput"]({"task_id": "t-live", "full": True}))
    assert body["shown_lines"] == 400 and "note" not in body
    # The older spelling still works, and since_line composes with the tail.
    body = _body(await handlers["TaskOutput"]({"task_id": "t-live", "tail": 5}))
    assert len(body["lines"]) == 5
    body = _body(await handlers["TaskOutput"]({"task_id": "t-live", "since_line": 395}))
    assert body["lines"] == [f"line {i}" for i in range(395, 400)] and "note" not in body


# ─── (c) a stop the session asked for never wakes it ───────────────────────────


async def test_stop_attributes_and_suppresses_before_the_process_dies(monkeypatch):
    """The watcher settles the instant the process dies; if the registry is
    written after the kill, a fast settle reads an unattributed exit and the
    model gets an "error" steer for its own TaskStop (F14)."""
    task = bt._BackgroundTask("t-stop", "npm run dev", "/work", SESSION)
    proc = MagicMock()
    proc.returncode = None
    proc.pid = 4321
    task.process = proc
    task.own_process_group = True
    bt._background_tasks["t-stop"] = task
    task_registry.start_task(SESSION, "t-stop", "shell", "npm run dev")

    seen = {}

    async def _kill(process, **kwargs):
        # What the settle path sees at the moment the process goes away.
        record = task_registry.get_task(SESSION, "t-stop") or {}
        seen["status"] = record.get("status")
        seen["reason"] = record.get("reason")
        seen["suppressed"] = task_registry.is_wake_suppressed(SESSION, "t-stop")
        process.returncode = 1
        return []

    monkeypatch.setattr("app.core.process_tree.kill_process_tree", _kill)
    monkeypatch.setattr(bt, "_retire_stopped_shell", AsyncMock())

    result = await bt.stop_background_shell_task("t-stop", session_id=SESSION)
    assert result["task_id"] == "t-stop"
    assert seen["status"] == "killed"
    assert seen["suppressed"] is True
    assert seen["reason"]  # attributed, not a blank kill

    # And the settle that follows reads as a deliberate stop, not an error.
    status, summary = ms.settle_monitor_task(
        exit_code=1, lines=["Traceback…"], command="npm run dev", cwd=None,
        session_id=SESSION, task_id="t-stop", serves=True,
    )
    assert status == "killed" and "Traceback" not in summary


# ─── (d) summary budget and the exit-0 notice ──────────────────────────────────


def test_summary_budget_default_is_about_four_kilobytes():
    assert settings.model_fields["monitor_summary_max_chars"].default == 4000
    assert ms._summary_budget() >= 4000


def test_exit_zero_truncation_says_the_tail_is_optional(monkeypatch):
    monkeypatch.setattr(settings, "monitor_summary_max_chars", 400)
    lines = [f"noise {i}" for i in range(300)] + ["Tests 45 passed"]
    ok = ms._summarize(lines, exit_code=0)
    assert "exit 0; TaskOutput only if you need the tail" in ok
    failed = ms._summarize(lines, exit_code=1)
    assert "exit 0" not in failed and "TaskOutput(lines=N)" in failed
    # The settle passes the exit code through.
    status, summary = ms.settle_monitor_task(
        exit_code=0, lines=lines, command="npm test", cwd=None,
        session_id=None, task_id="t", serves=False,
    )
    assert status == "completed" and "exit 0; TaskOutput only if you need the tail" in summary


def test_monitor_description_names_the_budget_and_the_tail():
    text = bt._monitor_description()
    assert "4,000" in text and "last 150 lines" in text
