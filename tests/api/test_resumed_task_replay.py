"""Resumed task terminals survive live feed replay and old-log hydration (#2233)."""

import asyncio

import pytest

from app.api.events import event_stream
from app.core import task_registry as registry_module
from app.core.provider_agent_feed import WorkerFeed
from app.core.provider_agent_supervisor import WorkerRecord, _settle_card
from app.core.turn_recorder import record_task_start_frame
from app.db import background_tasks, session_events
from tests.api.sse_helpers import parse_frame


@pytest.fixture
def registry(monkeypatch):
    registry = registry_module.TaskRegistry()
    monkeypatch.setattr(registry_module, "task_registry", registry)
    return registry


async def replay(session_id, from_seq=0):
    stream = event_stream(session_id, from_seq)
    frames = []
    try:
        async with asyncio.timeout(5):
            async for frame in stream:
                parsed = parse_frame(frame)
                if parsed.get("event") == "caught_up":
                    break
                if parsed.get("event") == "task.updated":
                    frames.append(parsed["data"])
    finally:
        await stream.aclose()
    return frames


async def test_resumed_stop_records_terminal_before_replay(mock_db, gw, registry, monkeypatch):
    monkeypatch.setattr(registry_module, "_now_iso", lambda: "2026-10-09T19:00:00Z")
    registry.start_task("parent", "child", "agent", "Worker", tool_use_id="child")
    feed = WorkerFeed(parent_session_id="parent", worker_session_id="child")
    await feed.started()
    registry.complete_task("parent", "child", "completed", notify=False)
    await feed.completed("completed")
    monkeypatch.setattr(registry_module, "_now_iso", lambda: "2026-10-11T00:26:42Z")
    registry.resume_task("parent", "child")
    await record_task_start_frame("parent", "child", tool_use_id="child")
    record = WorkerRecord("parent", "child", "child", "child", continuation_turn_id="resume")
    await _settle_card(record, "Stopped by user", "Stopped by user")
    registry.complete_task("parent", "child", "killed", notify=False)
    # Assert producer output BEFORE replay has any opportunity to repair it.
    updates = session_events.get_latest_task_updates("parent")
    assert updates[0]["state"] == "killed"
    frames = await replay("parent")
    assert [f["state"] for f in frames] == ["running", "completed", "running", "killed"]
    assert frames[0]["started_at"] == "2026-10-09T19:00:00Z"
    assert frames[2]["started_at"] == "2026-10-11T00:26:42Z"
    assert frames[-1]["task_id"] == frames[2]["task_id"]


@pytest.mark.parametrize("status", ["completed", "error", "killed"])
@pytest.mark.parametrize("reconnect", [False, True])
async def test_old_running_log_repaired_from_durable_terminal(mock_db, gw, registry, status, reconnect):
    session_events.append_events("parent", [
        {"type": "task.updated", "payload": {"task_id": "child", "state": state}}
        for state in ("running", "completed", "running")
    ])
    # A restarted process has no in-memory task or original WorkerFeed.
    background_tasks.upsert_background_task({
        "session_id": "parent", "task_id": "child", "kind": "agent",
        "tool_use_id": "child", "status": status, "title": "Old resumed worker",
        "started_at": "2026-10-11T00:26:42Z", "updated_at": "2026-10-11T00:43:21Z",
    })
    assert registry.get_task("parent", "child") is None
    frames = await replay("parent", 3 if reconnect else 0)
    assert frames[-1]["state"] == status
    assert frames[-1]["tool_use_id"] == "child"
    repaired_seq = session_events.get_max_seq("parent")
    await replay("parent")
    assert session_events.get_max_seq("parent") == repaired_seq


async def test_live_resume_wins_over_old_durable_terminal(mock_db, gw, registry):
    background_tasks.upsert_background_task({
        "session_id": "parent", "task_id": "child", "kind": "agent",
        "status": "killed", "started_at": "2026-10-09T19:00:00Z",
        "updated_at": "2026-10-09T20:00:00Z",
    })
    registry.resume_task("parent", "child")
    await record_task_start_frame("parent", "child")
    frames = await replay("parent")
    assert frames[-1]["state"] == "running"
    assert session_events.get_latest_task_updates("parent")[0]["state"] == "running"
