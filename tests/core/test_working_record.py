"""Working-record producing paths and coordinator replacement, issue #371 step 2."""
import json

import pytest
from fluid.tools.registry import ToolCtx

from app.core import coordinator_handoff, turn_scheduler, working_record
from app.core import session_orchestration as orch
from app.db import database, sessions, turns
from app.db import working_record as store
from app.db.base import get_db
from tests.core.test_assistant_started_work import quiet_scheduler, run, wakes  # noqa: F401
from tests.core.test_session_orchestration import chat


@pytest.fixture
def owner(mock_db, monkeypatch):
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    return chat("coordinator", mode="chat")[0]


def tool_for(sid, admin=True):
    # Use the real registration decorator, not a hand-built alternate handler.
    from app.mcp.inproc import tool
    from app.mcp.working_record_tools import build_tools
    return build_tools(tool, is_admin=admin, session_id=sid)[0]


async def invoke(sid, **args):
    result = await tool_for(sid).handler(args)
    assert not result.get("isError"), result
    return json.loads(result["content"][0]["text"])


async def test_new_coordinator_after_settle_sees_real_child_report_and_live_state(owner, monkeypatch, tmp_path):
    decision = await invoke(owner["id"], action="add", kind="decision", content="Ship only after review.")
    await invoke(owner["id"], action="add", kind="pending", content="Collect performance numbers.")
    result = await orch.make_handler(lambda: owner["id"])("start", {
        "title": "Investigate the regression", "prompt": "Find the cause", "request_id": "work"}, ToolCtx())
    assert not result.is_error, result.text
    child = json.loads(result.text)
    sessions.set_session_active(owner["id"], False)
    run(child["session_id"], child["turn_id"], "Found the cache invalidation bug; patch ready.")
    await turn_scheduler.on_turn_settled(child["session_id"], child["turn_id"], "completed")
    assert wakes(owner["id"]) == []
    chat("running")
    running = turns.enqueue_turn("running", "user", prompt="Benchmark", profile_id="agent")
    assert turns.start_turn(running["id"])
    chat("needs-owner")
    from app.db.interactions import create_interaction
    create_interaction("needs-owner", "question", {"question": "Publish now?"}, None)
    fresh, _ = chat("fresh", mode="chat")
    text = working_record.summary(fresh["id"])
    for expected in ("Ship only after review", "Collect performance", "cache invalidation bug", "running", "needs_owner"):
        assert expected in text
    assert len(text.encode()) <= working_record.SUMMARY_BYTES
    # Drive the production options builder too: the new agent actually receives
    # this context, not merely a consumer that our fixture called directly.
    from app.core import config, query_options
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    monkeypatch.setattr(query_options, "get_security_instructions", lambda *a, **kw: "")
    options, _ = query_options.build_options_from_profile(database.get_profile("agent"), session_id="fresh")
    assert text in str(options.system_prompt)
    assert "mcp__builtins__WorkingRecord" in options.allowed_tools
    # Reading is not acknowledgement. Delivery retries don't duplicate or reopen.
    reports = [item for item in store.items() if item["kind"] == "report" and item["source_session_id"] == child["session_id"]]
    assert len(reports) == 1
    await invoke("fresh", action="resolve", item_id=reports[0]["id"])
    await turn_scheduler.on_turn_settled(child["session_id"], child["turn_id"], "completed")
    assert not [i for i in store.items() if i["id"] == reports[0]["id"]]
    await invoke("fresh", action="resolve", item_id=decision["item_id"])
    assert "Ship only after review" not in working_record.summary("fresh")


@pytest.mark.parametrize("fields", [{"api_user_id": "api"}, {"is_worker": True}, {"hidden": True},
                                    {"parent_session_id": "coordinator"}])
async def test_tool_rejects_other_principals(owner, fields):
    row, _ = chat("restricted", **fields)
    result = await tool_for(row["id"]).handler({"action": "list"})
    assert result["isError"]
    result = await tool_for("coordinator", admin=False).handler({"action": "add", "kind": "decision", "content": "no"})
    assert result["isError"]
    assert store.items() == []


async def test_validation_and_list_pagination(owner):
    for args in ({"action": "add", "kind": "report", "content": "forged"},
                 {"action": "add", "kind": "pending", "content": " "},
                 {"action": "add", "kind": "pending", "content": "x" * 2001},
                 {"action": "resolve"}, {"action": "list", "offset": -1}):
        assert (await tool_for(owner["id"]).handler(args))["isError"]
    for index in range(101):
        store.add("pending", str(index), owner["id"])
    first = await invoke(owner["id"], action="list")
    second = await invoke(owner["id"], action="list", offset=100)
    assert len(first["items"]) == 100 and len(second["items"]) == 1


def test_summary_budget_unicode_flood_and_fair_categories(owner):
    for kind in store.KINDS:
        for index in range(20):
            store.add(kind, ("測試😀" * 100), owner["id"])
    text = working_record.summary(owner["id"])
    assert len(text.encode("utf-8")) <= working_record.SUMMARY_BYTES
    assert "More items omitted" in text
    # Worst-case entries still must not crowd every other category out.
    assert '"kind": "decision"' in text
    assert '"kind": "pending"' in text


async def test_automatic_handoff_uses_real_usage_producer_and_new_session(owner, monkeypatch):
    kicked = []
    monkeypatch.setattr(turn_scheduler, "kick", kicked.append)
    store.add("decision", "Use the staged rollout.", owner["id"])
    sessions.set_session_run_binding(owner["id"], effort="high", permission_mode="default")
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.core import session_pool
    usage = AsyncMock(return_value={"totalTokens": 80000, "maxTokens": 100000})
    pool = SimpleNamespace(peek=lambda sid: SimpleNamespace(is_connected=True, adapter=SimpleNamespace(get_context_usage=usage)))
    monkeypatch.setattr(session_pool, "get_session_pool", lambda: pool)
    turn = turns.enqueue_turn(owner["id"], "user", prompt="Coordinate the release", profile_id="agent")
    run(owner["id"], turn["id"], "Build passed; waiting for review.")
    await turn_scheduler.on_turn_settled(owner["id"], turn["id"], "completed")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM coordinator_handoffs WHERE source_session_id=%s", (owner["id"],))
        receipt = dict(cur.fetchone())
    successor = sessions.get_session(receipt["successor_session_id"])
    assert not sessions.get_session(owner["id"])["is_active"]
    assert successor["is_active"] and successor["mode"] == "chat"
    assert successor["effort"] == "high" and successor["permission_mode"] == "default"
    assert not successor.get("sdk_session_id") and not successor.get("context_usage_json")
    assert successor["id"] in kicked
    usage.assert_awaited_once()
    wake = turns.get_turn(receipt["turn_id"])
    assert wake["state"] == "queued" and "Coordinate the release" in wake["prompt"]
    text = working_record.summary(successor["id"])
    assert "staged rollout" in text and "Build passed" in text
    await turn_scheduler.on_turn_settled(owner["id"], turn["id"], "completed")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM coordinator_handoffs")
        assert cur.fetchone()["n"] == 1


@pytest.mark.parametrize("guard", ["under", "unknown", "queued", "parked", "stopped", "project", "settled", "question"])
async def test_handoff_guards(owner, guard):
    sessions.set_session_context_usage(owner["id"], {"totalTokens": 79999 if guard == "under" else 90000,
                                                              "maxTokens": 100000})
    if guard == "unknown":
        sessions.set_session_context_usage(owner["id"], {})
    if guard in ("queued", "parked"):
        turn = turns.enqueue_turn(owner["id"], "user", prompt="New work", profile_id="agent")
        if guard == "parked":
            turns.start_turn(turn["id"])
            turns.park_turn(turn["id"])
    if guard == "stopped":
        from app.core.task_registry import task_registry
        task_registry.mark_user_stopped(owner["id"])
    if guard == "settled":
        sessions.set_session_active(owner["id"], False)
    if guard == "project":
        database.create_project("project", "Project", None, "/tmp/example")
        sessions.set_session_project(owner["id"], "project")
    if guard == "question":
        from app.db.interactions import create_interaction
        create_interaction(owner["id"], "question", {}, None)
    assert coordinator_handoff._admit(owner["id"], {}, {}) is None


def test_migration_idempotent_and_sync_registry(owner):
    from app.network import policy
    with get_db() as conn:
        store.create_schema(conn.cursor())
        store.create_schema(conn.cursor())
    item_id = store.add("decision", "Keep this after deletion.", owner["id"])
    sessions.delete_session(owner["id"])
    assert store.items()[0]["id"] == item_id
    coll = policy.COLLECTIONS["working_record"]
    assert coll.table == "owner_working_items" and coll.mode == policy.MODE_SHARED
    assert "content" in coll.fields and coll.shared_row(store.items()[0])
    assert "status" not in coll.fields
    acknowledgements = policy.COLLECTIONS["working_record_acknowledgements"]
    assert acknowledgements.table == "owner_working_acknowledgements"
    assert "report_frame" not in acknowledgements.fields


async def test_task_report_survives_stopped_coordinator_and_is_deduplicated(owner):
    from app.core.task_registry import task_registry
    sid = owner["id"]
    task_registry.start_task(sid, "review-task", "agent", "Review", tool_use_id="review-tool")
    task_registry.mark_background_launch(sid, "review-tool")
    task_registry.complete_task(sid, "review-task", "completed", summary="Review found no blockers.", notify=False)
    task_registry.mark_user_stopped(sid)
    produced = task_registry.get_task(sid, "review-task")
    frame = {"task_id": produced["id"], "status": produced["status"], "summary": produced["summary"]}
    await turn_scheduler.on_task_terminal(sid, frame)
    await turn_scheduler.on_task_terminal(sid, frame)
    assert wakes(sid) == []
    reports = [item for item in store.items() if item["kind"] == "report"]
    assert len(reports) == 1 and "no blockers" in reports[0]["content"]
    assert await invoke(sid, action="resolve", item_id=reports[0]["id"]) == {"resolved": True}
    await turn_scheduler.on_task_terminal(sid, frame)
    assert not store.items()




@pytest.mark.parametrize("first", [
    {"status": "killed", "reason": "park_watchdog", "summary": "Watchdog stand-in"},
    {"status": "completed"},
])
async def test_genuine_or_richer_task_report_replaces_provisional_without_reopening(owner, first):
    from app.core.task_registry import task_registry
    sid = owner["id"]
    task_registry.start_task(sid, "late-report", "agent", "Review", tool_use_id="late-tool")
    task_registry.mark_background_launch(sid, "late-tool")
    task_registry.complete_task(sid, "late-report", first["status"], notify=False)
    provisional = {"task_id": "late-report", **first}
    await turn_scheduler.on_task_terminal(sid, provisional)
    report = next(row for row in store.items() if row["kind"] == "report")
    assert store.resolve(report["id"])
    genuine = {"task_id": "late-report", "status": "completed", "summary": "Genuine review: all checks passed."}
    await turn_scheduler.on_task_terminal(sid, genuine)
    rows = [row for row in store.items(include_resolved=True) if row["kind"] == "report"]
    assert len(rows) == 1 and "Genuine review" in rows[0]["content"]
    assert rows[0]["status"] == "resolved"
    # A stale replay, even one carrying a new usage field, cannot restore killed.
    await turn_scheduler.on_task_terminal(sid, {**provisional, "usage": {"tokens": 1}})
    assert store.items(include_resolved=True)[0]["content"] == rows[0]["content"]
    assert not store.items()


def test_handoff_keeps_cumulative_job_budget_but_resets_display_clock(owner):
    from datetime import UTC, datetime

    from app.core.spending_limits import _job_over_time
    sid = owner["id"]
    for used_minutes in (50, 9):
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET work_ms=%s, work_started_at=NULL WHERE id=%s",
                                  (used_minutes * 60000, sid))
        sessions.set_session_context_usage(sid, {"totalTokens": 90000, "maxTokens": 100000})
        turn = turns.enqueue_turn(sid, "user", prompt="Coordinate", profile_id="agent")
        run(sid, turn["id"], "Continuing")
        successor = coordinator_handoff._admit(sid, turns.get_turn(turn["id"]), {"stop_reason": "tool_use"})
        assert successor is not None
        sid = successor["id"]
        assert int(sessions.get_session(sid).get("work_ms") or 0) == 0
        with get_db() as conn:
            cur = conn.cursor()
            cur.execute("SELECT turn_id FROM coordinator_handoffs WHERE successor_session_id=%s", (sid,))
            wake_id = cur.fetchone()["turn_id"]
        run(sid, wake_id, "Fresh context")
    assert not _job_over_time(sid, 60, datetime.now(UTC), coordination=True)
    assert _job_over_time(sid, 59, datetime.now(UTC), coordination=True)
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET work_ms=%s WHERE id=%s", (60000, sid))
    assert _job_over_time(sid, 60, datetime.now(UTC), coordination=True)


async def test_initial_successor_wake_cannot_chain_handoffs(owner):
    sessions.set_session_context_usage(owner["id"], {"totalTokens": 90000, "maxTokens": 100000})
    assert not await coordinator_handoff.on_session_rest(owner["id"],
        {"payload": {"coordinator_predecessor": "previous"}}, {}, "completed")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM coordinator_handoffs")
        assert cur.fetchone() is None


async def test_unfinished_started_coordinator_handoff_preserves_report_routing(owner):
    from app.db import session_events
    result = await orch.make_handler(lambda: owner["id"])("start", {
        "title": "Coordinate review", "prompt": "Coordinate a review", "request_id": "nested"}, ToolCtx())
    assert not result.is_error, result.text
    child = json.loads(result.text)
    sessions.set_session_context_usage(child["session_id"], {"totalTokens": 85000, "maxTokens": 100000})
    run(child["session_id"], child["turn_id"], "Still coordinating the review.")
    session_events.append_events(child["session_id"], [{"type": "turn.completed", "turn_id": child["turn_id"],
        "payload": {"stop_reason": "tool_use"}}])
    await turn_scheduler.on_turn_settled(child["session_id"], child["turn_id"], "completed")
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM coordinator_handoffs WHERE source_session_id=%s", (child["session_id"],))
        receipt = dict(cur.fetchone())
    assert wakes(owner["id"]) == []  # Not a false 'work completed' report.
    assert orch.started_by_owner(receipt["successor_session_id"]) == owner["id"]
    wake = turns.get_turn(receipt["turn_id"])
    original = turns.get_turn(child["turn_id"])
    assert wake["payload"]["orchestration_root"] == original["payload"]["orchestration_root"]
    run(receipt["successor_session_id"], receipt["turn_id"], "Review completed without blockers.")
    await turn_scheduler.on_turn_settled(receipt["successor_session_id"], receipt["turn_id"], "completed")
    assert "Review completed without blockers" in wakes(owner["id"])[0]["prompt"]

