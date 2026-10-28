"""Working-record producing paths, issue #371 step 2.

A long owner chat is never replaced by a new one: the automatic coordinator
hand-off is retired, and compaction owns the context limit."""
import json

import pytest
from fluid.tools.registry import ToolCtx

from app.core import turn_scheduler, working_record
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
    # Per-turn context, not the cached system prompt (#680): the adapter
    # appends turn_note to the user message for flagged owner sessions.
    assert options.aih_working_record is True
    assert text not in str(options.system_prompt)
    assert text in working_record.turn_note("fresh")
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
    for args in ({"action": "list", "limit": 0}, {"action": "list", "limit": 101},
                 {"action": "list", "limit": "5"}, {"action": "list", "limit": True},
                 {"action": "get"}, {"action": "get", "item_id": "missing"}):
        assert (await tool_for(owner["id"]).handler(args))["isError"], args
    for index in range(101):
        store.add("pending", str(index), owner["id"])
    first = await invoke(owner["id"], action="list")
    assert len(first["items"]) == working_record.LIST_DEFAULT
    assert first["next_offset"] == working_record.LIST_DEFAULT
    page = await invoke(owner["id"], action="list", offset=100)
    assert len(page["items"]) == 1 and page["next_offset"] is None and "sessions" not in page
    # The byte budget, not the requested limit, ends a large page.
    full = await invoke(owner["id"], action="list", limit=100)
    assert 50 < len(full["items"]) < 100 and full["next_offset"] == len(full["items"])


def test_read_actions_are_safe_and_writes_are_not():
    from app.core.provider.fluid_tool_bridge import BUILTINS_RISK_CLASSIFIERS
    classify = BUILTINS_RISK_CLASSIFIERS["WorkingRecord"]
    assert [classify({"action": a}) for a in ("list", "get", "add", "resolve", None)] == [
        "safe", "safe", "write", "write", "write"]


async def test_default_list_is_bounded_and_every_item_stays_retrievable(owner):
    """#681: long reports once made the default page 194k characters, which a
    delegated CLI spilled to a file a restricted chat cannot read."""
    from app.core.working_record import LIST_BYTES, PREVIEW_BYTES
    for index in range(150):
        working_record.record_report(owner["id"], {
            "task_id": f"child-{index}", "agent_message_turn_id": "t", "status": "completed",
            "title": "測試😀" * 40, "summary": f"report {index} " + "detail \n\"<>" * 200})
    for index in range(60):
        chat(f"live-{index}" + "長い題名" * 100)
    added = {(await invoke(owner["id"], action="add", kind="pending", content="x" * 2000))["item_id"]}
    expected = {row["id"] for row in store.items(limit=1000)}
    assert len(expected) == 151 and added <= expected
    seen, offset, pages = [], 0, 0
    while offset is not None:
        result = await tool_for(owner["id"]).handler({"action": "list", "offset": offset, "limit": 100})
        text = result["content"][0]["text"]
        assert not result.get("isError") and len(text.encode("utf-8")) <= LIST_BYTES
        page = json.loads(text)
        if pages == 0:
            assert page["sessions"] and page["sessions_omitted"] > 0
        for item in page["items"]:
            assert len(item["text"].encode("utf-8")) <= PREVIEW_BYTES
        seen += [item["id"] for item in page["items"]]
        assert page["items"], "every page makes progress"
        offset, pages = page["next_offset"], pages + 1
    assert len(seen) == len(set(seen)) and set(seen) == expected
    assert pages > 1
    default = (await tool_for(owner["id"]).handler({"action": "list"}))["content"][0]["text"]
    assert len(default.encode("utf-8")) <= LIST_BYTES
    # One item in full, open or resolved.
    item = await invoke(owner["id"], action="get", item_id=next(iter(added)))
    assert item["content"] == "x" * 2000 and item["status"] == "open"
    report = next(row for row in store.items(kind="report", limit=1))
    await invoke(owner["id"], action="resolve", item_id=report["id"])
    detail = await invoke(owner["id"], action="get", item_id=report["id"])
    assert detail["status"] == "resolved" and detail["content"] == report["content"]
    assert not [i for i in (await invoke(owner["id"], action="list", limit=100))["items"] if i["id"] == report["id"]]


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


def _sessions_count():
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) AS n FROM sessions")
        sessions_n = cur.fetchone()["n"]
        cur.execute("SELECT COUNT(*) AS n FROM coordinator_handoffs")
        return sessions_n, cur.fetchone()["n"]


@pytest.mark.parametrize("stop_reason", ["end_turn", "tool_use"])
async def test_full_coordinator_is_not_handed_off_but_still_records(owner, monkeypatch, stop_reason):
    """A no-project owner chat past 80% after a completed turn stays the same
    chat (compaction owns its limit); its result still enters the working
    record, except for a turn cut at the compaction boundary."""
    from app.db import session_events
    kicked = []
    monkeypatch.setattr(turn_scheduler, "kick", kicked.append)
    sessions.set_session_context_usage(owner["id"], {"totalTokens": 95000, "maxTokens": 100000})
    before = _sessions_count()
    turn = turns.enqueue_turn(owner["id"], "user", prompt="Coordinate the release", profile_id="agent")
    run(owner["id"], turn["id"], "Build passed; waiting for review.")
    session_events.append_events(owner["id"], [{"type": "turn.completed", "turn_id": turn["id"],
        "payload": {"stop_reason": stop_reason}}])
    await turn_scheduler.on_turn_settled(owner["id"], turn["id"], "completed")
    assert _sessions_count() == before == (before[0], 0)
    assert sessions.get_session(owner["id"])["is_active"]
    reports = [i for i in store.items() if i["kind"] == "report" and i["source_session_id"] == owner["id"]]
    if stop_reason == "end_turn":
        assert len(reports) == 1 and "Build passed" in reports[0]["content"]
    else:
        assert reports == []


async def test_full_started_coordinator_reports_to_its_starter(owner):
    """A StartSession chat past 80% reports its finished work to the chat
    that started it (report_settled_start), and is not replaced."""
    result = await orch.make_handler(lambda: owner["id"])("start", {
        "title": "Coordinate review", "prompt": "Coordinate a review", "request_id": "nested"}, ToolCtx())
    assert not result.is_error, result.text
    child = json.loads(result.text)
    sessions.set_session_context_usage(child["session_id"], {"totalTokens": 85000, "maxTokens": 100000})
    before = _sessions_count()
    run(child["session_id"], child["turn_id"], "Review completed without blockers.")
    await turn_scheduler.on_turn_settled(child["session_id"], child["turn_id"], "completed")
    assert _sessions_count() == before
    assert "Review completed without blockers" in wakes(owner["id"])[0]["prompt"]


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


def test_legacy_handoff_receipt_still_carries_job_budget(owner):
    """Receipts the retired hand-off wrote stay readable: a successor keeps
    the job time its predecessors used."""
    from datetime import UTC, datetime

    from app.core.spending_limits import _job_over_time
    sid = owner["id"]
    with get_db() as conn:
        cur = conn.cursor()
        cur.execute("UPDATE sessions SET work_ms=%s, work_started_at=NULL WHERE id=%s", (60000, sid))
        cur.execute("INSERT INTO coordinator_handoffs "
                    "(source_session_id,successor_session_id,turn_id,created_at,prior_work_ms) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    ("previous", sid, "turn", datetime.now(UTC).isoformat(), 58 * 60000))
    assert not _job_over_time(sid, 60, datetime.now(UTC), coordination=True)
    assert _job_over_time(sid, 59, datetime.now(UTC), coordination=True)
