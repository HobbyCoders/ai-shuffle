"""TaskGet / TaskOutput answer for THIS session's work — round-7 audit.

``_background_tasks`` is a process-global dict and both task tools read it with
no session check. Two audited consequences, opposite in shape:

* a parent named a task id it had read in a WORKER's report, was handed a
  DIFFERENT session's live shell together with "all tasks still running — park;
  you will be woken on completion", parked on it, and was never woken — nothing
  had subscribed it to that task. It re-ran the build on its next unrelated
  wake;
* a parent that legitimately wanted its own worker's Monitor result got
  ``task not found`` (the registry read IS scoped) and re-ran the identical
  test suites itself.

So: scope the lookup, allow the caller's own dispatch lineage through, label it
as the worker's, never promise a wake it will not get, and say "belongs to
another session" instead of "not found".
"""

import asyncio
import json
from unittest.mock import patch

import pytest

from tests.mcp.tool_helpers import tool_result_json


PARENT = "s-parent"
WORKER = "s-worker"
STRANGER = "s-stranger"


def _handlers(session_id=PARENT):
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


def _call(name, args, session_id=PARENT):
    return tool_result_json(asyncio.run(_handlers(session_id)[name].handler(args)))


@pytest.fixture
def shell(monkeypatch):
    """A live Monitor shell in the process-global map, owned by whoever asks."""
    from app.mcp import builtin_tools as bt

    made = []

    def _make(task_id, owner_session_id):
        task = bt._BackgroundTask(
            task_id=task_id, command="npm run check && npm run build",
            cwd=None, session_id=owner_session_id,
        )
        task.lines = ["> check", "Tests 45 passed"]

        task.is_running = lambda: True  # type: ignore[method-assign]
        bt._background_tasks[task_id] = task
        made.append(task_id)
        return task

    yield _make
    for task_id in made:
        bt._background_tasks.pop(task_id, None)


@pytest.fixture
def lineage(mock_db):
    """A real parent → worker chain: session rows plus the worker registration
    ``task_dispatch.caller_chain`` walks."""
    from app.core import provider_agent
    from app.db import database

    database.create_session(session_id=PARENT, profile_id=None, title="parent")
    database.create_session(
        session_id=WORKER, profile_id=None, title="worker",
        parent_session_id=PARENT, is_worker=True,
    )
    database.create_session(session_id=STRANGER, profile_id=None, title="other")
    provider_agent.register_worker_session(WORKER)
    yield
    provider_agent.reset_worker_sessions()


# ─── the leak: another session's shell ───────────────────────────────────────


def test_a_strangers_shell_is_not_served(lineage, shell):
    shell("5513f919", STRANGER)

    body = _call("TaskGet", {"task_id": "5513f919"})

    assert body["success"] is False
    assert "different session" in body["error"]
    assert body["owner_session_id"] == STRANGER
    # And emphatically NOT the promise that made the audited parent park.
    assert "you will be woken" not in json.dumps(body)


def test_a_strangers_buffer_is_not_served_either(lineage, shell):
    shell("5513f919", STRANGER)

    body = _call("TaskOutput", {"task_id": "5513f919"})

    assert body["success"] is False
    assert "different session" in body["error"]


def test_an_unknown_id_is_still_not_found(lineage):
    body = _call("TaskGet", {"task_id": "never-existed"})

    assert body["success"] is False
    assert body["error"].startswith("task not found.")
    assert "owner_session_id" not in body


# ─── the other half: my own worker's task IS readable ────────────────────────


def test_a_workers_shell_is_readable_by_the_parent(lineage, shell):
    shell("bc89bf68", WORKER)

    body = _call("TaskGet", {"task_id": "bc89bf68"})

    assert body["success"] is True
    assert body["owner_session_id"] == WORKER
    assert body["lines"] == ["> check", "Tests 45 passed"]
    # Readable, but no wake is coming from it — that is the whole correction.
    assert "does NOT wake you" in body["note"]
    assert "you will be woken on completion" not in body["note"]


def test_a_workers_buffer_is_readable_by_the_parent(lineage, shell):
    shell("bc89bf68", WORKER)

    body = _call("TaskOutput", {"task_id": "bc89bf68"})

    assert body["success"] is True
    assert body["owner_session_id"] == WORKER
    assert body["lines"] == ["> check", "Tests 45 passed"]
    assert "does NOT wake you" in body["scope_note"]


def test_a_worker_cannot_read_its_parents_task(lineage, shell):
    """Lineage runs one way: a parent owns its workers' work, not the reverse."""
    shell("p-task", PARENT)

    body = _call("TaskGet", {"task_id": "p-task"}, session_id=WORKER)

    assert body["success"] is False
    assert body["owner_session_id"] == PARENT


# ─── unchanged for a session's own task ──────────────────────────────────────


def test_my_own_running_shell_still_gets_the_park_nudge(lineage, shell):
    shell("mine-01", PARENT)

    body = _call("TaskGet", {"task_id": "mine-01"})

    assert body["success"] is True
    assert body["note"].startswith("all tasks still running")
    assert "owner_session_id" not in body


def test_my_own_buffer_reads_exactly_as_before(lineage, shell):
    shell("mine-01", PARENT)

    body = _call("TaskOutput", {"task_id": "mine-01"})

    assert body["success"] is True
    assert body["running"] is True
    assert body["total_lines"] == 2
    assert "scope_note" not in body
