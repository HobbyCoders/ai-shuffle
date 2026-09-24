"""TaskList/TaskGet as parking aids, not poll targets.

Completions wake the session on their own, so a listing where nothing has
finished carries no news. Both descriptions say so, and the handlers say it
again at the top of the result — an audited parent called TaskList three times
and then shadowed its own child for 18 cells.
"""

import asyncio
import json
from unittest.mock import patch


def _builtin_handlers(session_id="session-parent"):
    import app.mcp.inproc as sdk

    from app.mcp import builtin_tools

    captured = {}
    real_create = sdk.create_tool_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_tool_server", side_effect=_spy):
        builtin_tools.create_builtin_tools_server(session_id=session_id)

    return {tool.name: tool for tool in captured["tools"]}


def _body(result):
    return json.loads(result["content"][0]["text"])


# ─── descriptions ────────────────────────────────────────────────────────────


def test_descriptions_do_not_invite_polling(mock_db):
    tools = _builtin_handlers()

    for name in ("TaskList", "TaskGet"):
        description = tools[name].description
        assert "poll" in description.lower()
        assert "wake" in description.lower()
    # The old TaskList text actively recruited the anti-pattern.
    assert "whenever you need the current picture" not in tools["TaskList"].description


# ─── the nudge ───────────────────────────────────────────────────────────────


def _running(task_id="t-1"):
    return {"task_id": task_id, "status": "running", "kind": "agent"}


def test_task_list_nudges_when_nothing_has_finished(mock_db, monkeypatch):
    from app.core.task_registry import task_registry

    monkeypatch.setattr(task_registry, "get_all", lambda sid: [_running()])
    body = _body(asyncio.run(_builtin_handlers()["TaskList"].handler({})))

    assert body["note"] == (
        "all tasks still running — park; you will be woken on completion."
    )
    assert body["running_count"] == 1


def test_task_list_stays_quiet_when_something_finished(mock_db, monkeypatch):
    from app.core.task_registry import task_registry

    monkeypatch.setattr(
        task_registry,
        "get_all",
        lambda sid: [_running(), {"task_id": "t-2", "status": "completed"}],
    )
    body = _body(asyncio.run(_builtin_handlers()["TaskList"].handler({})))

    assert "note" not in body


def test_task_list_stays_quiet_with_no_tasks(mock_db, monkeypatch):
    """Nothing running is not a parking situation — it is an empty session."""
    from app.core.task_registry import task_registry

    monkeypatch.setattr(task_registry, "get_all", lambda sid: [])
    body = _body(asyncio.run(_builtin_handlers()["TaskList"].handler({})))

    assert "note" not in body


def test_task_get_nudges_on_a_still_running_agent_task(mock_db, monkeypatch):
    from app.core.task_registry import task_registry

    monkeypatch.setattr(task_registry, "get_task", lambda sid, tid, **_kw: _running(tid))
    body = _body(
        asyncio.run(_builtin_handlers()["TaskGet"].handler({"task_id": "t-1"}))
    )

    assert body["note"].startswith("all tasks still running")
    assert body["task"]["task_id"] == "t-1"


def test_task_get_stays_quiet_on_a_finished_task(mock_db, monkeypatch):
    from app.core.task_registry import task_registry

    monkeypatch.setattr(
        task_registry,
        "get_task",
        lambda sid, tid, **_kw: {"task_id": tid, "status": "completed"},
    )
    body = _body(
        asyncio.run(_builtin_handlers()["TaskGet"].handler({"task_id": "t-1"}))
    )

    assert "note" not in body
