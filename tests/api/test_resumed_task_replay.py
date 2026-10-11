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


@pytest.fixture(autouse=True)
def owned_parent(mock_db, monkeypatch):
    from app.db.sessions import create_session

    monkeypatch.setattr("app.network.runners.self_device_id", lambda: "local-worker")
    create_session("parent", None)


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
    # Match the historical event timestamps: registry start predates the resume
    # event slightly, and the terminal snapshot was written after it.
    mock_db.execute("UPDATE session_events SET created_at=? WHERE seq=1", ("2026-10-09T19:00:00Z",))
    mock_db.execute("UPDATE session_events SET created_at=? WHERE seq=2", ("2026-10-09T20:00:00Z",))
    mock_db.execute("UPDATE session_events SET created_at=? WHERE seq=3", ("2026-10-11T00:26:43Z",))
    mock_db.commit()
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


def seed_stale_resume(mock_db, *, explicit_start=True):
    payload = {"task_id": "child", "state": "running"}
    if explicit_start:
        payload["started_at"] = "2026-10-11T00:26:42Z"
    session_events.append_events("parent", [{"type": "task.updated", "payload": payload}])
    mock_db.execute("UPDATE session_events SET created_at=?", ("2026-10-11T00:26:43Z",))
    mock_db.commit()
    background_tasks.upsert_background_task({
        "session_id": "parent", "task_id": "child", "kind": "agent",
        "status": "killed", "started_at": "2026-10-11T00:26:42Z",
        "updated_at": "2026-10-11T00:43:21Z",
    })


@pytest.mark.parametrize("ownership", ["mirror", "other-instance", "unknown", "handoff", "handoff-hold"])
async def test_non_authoritative_hydration_never_repairs(mock_db, gw, registry, ownership):
    seed_stale_resume(mock_db)
    if ownership == "mirror":
        mock_db.execute("UPDATE sessions SET owner_device_id='other-worker'")
    elif ownership == "other-instance":
        mock_db.execute("UPDATE sessions SET instance_id='other-install'")
    elif ownership == "unknown":
        mock_db.execute("UPDATE sessions SET owner_device_id=NULL")
    else:
        from app.db.runs import claim_run
        claim_run("parent", ownership + ":reservation", 120)
    mock_db.commit()
    frames = await replay("parent")
    assert [f["state"] for f in frames] == ["running"]
    assert session_events.get_max_seq("parent") == 1


@pytest.mark.parametrize("explicit_start", [False, True])
async def test_older_terminal_snapshot_cannot_settle_new_assignment(mock_db, gw, registry, explicit_start):
    seed_stale_resume(mock_db, explicit_start=explicit_start)
    background_tasks.upsert_background_task({
        "session_id": "parent", "task_id": "child", "kind": "agent",
        "status": "killed", "started_at": "2026-10-09T19:00:00Z",
        "updated_at": "2026-10-09T20:00:00Z",
    })
    frames = await replay("parent")
    assert [f["state"] for f in frames] == ["running"]


async def test_generation_mismatch_rejected_even_if_old_snapshot_updated_later(mock_db, gw, registry):
    seed_stale_resume(mock_db)
    background_tasks.upsert_background_task({
        "session_id": "parent", "task_id": "child", "kind": "agent",
        "status": "killed", "started_at": "2026-10-09T19:00:00Z",
        "updated_at": "2026-10-11T01:00:00Z",
    })
    assert [f["state"] for f in await replay("parent")] == ["running"]


async def test_legacy_previous_terminal_fences_assignment(mock_db, gw, registry):
    session_events.append_events("parent", [{"type": "task.updated", "payload": {
        "task_id": "child", "state": "completed",
    }}])
    seed_stale_resume(mock_db, explicit_start=False)
    mock_db.execute("UPDATE session_events SET created_at=? WHERE seq=1", ("2026-10-11T00:00:00Z",))
    mock_db.commit()
    background_tasks.upsert_background_task({
        "session_id": "parent", "task_id": "child", "kind": "agent",
        "status": "killed", "started_at": "2026-10-09T19:00:00Z",
        "updated_at": "2026-10-11T01:00:00Z",
    })
    assert [f["state"] for f in await replay("parent")] == ["completed", "running"]


async def test_concurrent_hydrations_append_exactly_one_repair(mock_db, gw, registry, monkeypatch):
    import threading
    from app.core.task_replay import reconcile_task_replay

    seed_stale_resume(mock_db)
    original = session_events.get_latest_task_updates
    both_read = threading.Barrier(2)

    def read_before_either_locks(session_id):
        updates = original(session_id)
        both_read.wait(timeout=5)
        return updates

    monkeypatch.setattr(session_events, "get_latest_task_updates", read_before_either_locks)
    await asyncio.gather(reconcile_task_replay("parent"), reconcile_task_replay("parent"))
    events = session_events.get_events("parent")
    assert [e["payload"]["state"] for e in events] == ["running", "killed"]
    assert session_events.get_max_seq("parent") == 2


async def test_owner_change_after_candidate_read_prevents_repair(mock_db, gw, registry, monkeypatch):
    from app.core.task_replay import reconcile_task_replay

    seed_stale_resume(mock_db)
    original = session_events.get_latest_task_updates

    def transfer_after_read(session_id):
        updates = original(session_id)
        mock_db.execute("UPDATE sessions SET owner_device_id='other-worker'")
        mock_db.commit()
        return updates

    monkeypatch.setattr(session_events, "get_latest_task_updates", transfer_after_read)
    await reconcile_task_replay("parent")
    assert session_events.get_max_seq("parent") == 1
