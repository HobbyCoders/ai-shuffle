"""TaskList holds its payload near 8k chars instead of growing with history.

A long session's finished-task list reached 38k chars in one TaskList result.
Running tasks always come back; finished ones are added newest-first while
they fit, and the rest are named by id so TaskGet can still read any of them.
"""

import json
from datetime import datetime, timezone

from app.mcp.builtin_tools import TASK_LIST_CHAR_BUDGET, _bound_task_list


def _task(i: int, status: str, size: int = 600) -> dict:
    return {
        "id": f"bg-{i}",
        "status": status,
        "description": f"task {i}",
        "result": "r" * size,
        "finished_at": datetime(2026, 11, 12, tzinfo=timezone.utc),
    }


def test_budget_is_8k():
    assert TASK_LIST_CHAR_BUDGET == 8_000


def test_a_short_list_is_returned_whole():
    tasks = [_task(i, "completed", 100) for i in range(3)]
    assert _bound_task_list(tasks) == {"tasks": tasks}


def test_older_finished_tasks_are_named_not_inlined():
    tasks = [_task(i, "completed") for i in range(60)]  # newest first
    out = _bound_task_list(tasks)
    kept = [t["id"] for t in out["tasks"]]
    assert kept == [f"bg-{i}" for i in range(len(kept))]  # the newest survive
    assert out["omitted_task_ids"] == [f"bg-{i}" for i in range(len(kept), 60)]
    assert len(json.dumps(out["tasks"], default=str)) <= TASK_LIST_CHAR_BUDGET
    assert "TaskGet" in out["omitted_note"]


def test_running_tasks_are_always_kept():
    tasks = [_task(i, "completed") for i in range(30)]
    tasks += [_task(100 + i, "running", 3_000) for i in range(4)]  # 12k alone
    out = _bound_task_list(tasks)
    ids = {t["id"] for t in out["tasks"]}
    assert {f"bg-{100 + i}" for i in range(4)} <= ids
    assert not any(t["status"] == "running" for t in tasks if t["id"] in out["omitted_task_ids"])
