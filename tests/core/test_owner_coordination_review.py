"""Permission and accounting regressions from the step-1 security review."""
import json

import pytest
from fluid.tools.registry import ToolCtx
from app.core import session_orchestration as orch
from app.db import database
from app.db.base import get_db
from tests.core.test_session_orchestration import chat


@pytest.fixture
def owner(mock_db, monkeypatch, tmp_path):
    from app.core import query_options, config
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    monkeypatch.setattr(query_options, "get_security_instructions", lambda *a, **kw: "")
    return tmp_path


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("permission", ["default", "auto", "acceptEdits", "bypassPermissions"])
async def test_start_preserves_permission_without_assistant_scope(owner, project, permission):
    from app.core import query_options, assistant_permissions, away_mode
    from app.db import projects_v2, checkouts
    from app.core.config import settings
    registered = projects_v2.upsert_project("folder:review", name="Project")
    checkouts.upsert_checkout(registered["id"], settings.effective_instance_id, str(owner))
    row, _ = chat("starter", project_v2_id=registered["id"] if project else None,
                  mode=None if project else "chat")
    database.set_session_run_binding("starter", permission_mode=permission)
    result = await orch.make_handler(lambda: "starter")(
        "start", {"title": "Child", "prompt": "work", "request_id": "review", "project": "Project"}, ToolCtx())
    assert not result.is_error, result.text
    child = json.loads(result.text)["session_id"]
    assert assistant_permissions.scope_for_session(child) is None
    assert not away_mode.assistant_work(child)
    options, _ = query_options.build_options_from_profile(database.get_profile("agent"), session_id=child)
    assert options.permission_mode == permission
    assert not getattr(options, "aih_assistant_scope", None)
    # Task workers under an ordinary child must not acquire the legacy home's scope.
    worker = database.create_session("task-worker", "agent", is_worker=True, parent_session_id=child,
                                     project_v2_id=registered["id"], permission_mode=permission)
    assert assistant_permissions.scope_for_session(worker["id"]) is None
    options, _ = query_options.build_options_from_profile(database.get_profile("agent"), session_id=worker["id"])
    assert options.permission_mode == permission


def test_ordinary_turn_does_not_scan_other_principals(owner, monkeypatch):
    from app.core import orchestration_limits
    from app.db import mobile_tool_identity
    database.create_project("proj", "Project", None, str(owner))
    for i in range(400):
        database.create_session(f"s{i}", "agent", project_id="proj")
    from app.core.config import settings
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET instance_id=%s", (settings.effective_instance_id,))
    from tests.pg_shim import ShimCursor
    calls, queries = [], []
    monkeypatch.setattr(orch, "_principal", lambda sid: calls.append(sid) or None)
    execute = ShimCursor.execute
    def measured(cursor, sql, params=None):
        queries.append(sql)
        return execute(cursor, sql, params)
    monkeypatch.setattr(ShimCursor, "execute", measured)
    assert orchestration_limits.turn_refusal("s1", "unpark") is None
    assert len(calls) <= 2, calls
    assert len(queries) <= 8, queries
    assert not any("SELECT * FROM sessions" in sql and "WHERE" not in sql for sql in queries)


def test_legacy_code_row_keeps_code_ceiling(owner):
    from app.core import query_options
    from app.core.session_modes import runtime_session_mode
    row, _ = chat("legacy")
    assert runtime_session_mode(row) == "code"
    options, _ = query_options.build_options_from_profile(database.get_profile("agent"), session_id=row["id"])
    assert not options.aih_owner_coordinator
    assert not options.aih_tool_allowlist or "Bash" in options.aih_tool_allowlist


async def test_project_starter_work_is_not_coordination_spending(owner):
    from app.core import orchestration_limits, assistant_limits
    from tests.core.test_assistant_limits import book
    database.create_project("proj", "Project", None, str(owner))
    chat("starter", project_id="proj")
    result = await orch.make_handler(lambda: "starter")(
        "start", {"title": "Independent", "prompt": "work", "request_id": "independent"}, ToolCtx())
    assert not result.is_error, result.text
    child = json.loads(result.text)["session_id"]
    book(child, 4)
    assert orchestration_limits.role(child) is None
    assert assistant_limits.status()["spent_today_usd"] == 0


def test_owner_typed_spend_and_live_turns_are_exempt_but_away_is_not(owner):
    from app.core import assistant_limits as limits, orchestration_limits, assistant_service
    from app.db import turns, assistant_limits as limits_db
    from app.core.query_persistence import usage_settlement_key
    row, _ = chat("coordinator", mode="chat")
    for human, cost in ((True, 50), (False, 3)):
        turn = turns.enqueue_turn(row["id"], "user", prompt="answer", profile_id="agent",
                                  payload={"question_answer_human_input": human})
        key = usage_settlement_key(row["id"], turn)
        from app.db.analytics import settle_turn_usage
        assert settle_turn_usage(row["id"], key, cost_usd=cost)
    limits_db.update(assistant_service.assistant_id(), {"spend_limit_enabled": True, "daily_spend_limit_usd": 2})
    assert limits.status()["spent_today_usd"] == 3
    assert orchestration_limits.turn_refusal(row["id"], "user", payload={"question_answer_human_input": True}) is None
    assert orchestration_limits.turn_refusal(row["id"], "user", payload={"question_answer_human_input": False})
    live = turns.enqueue_turn(row["id"], "user", prompt="owner", profile_id="agent")
    assert turns.start_turn(live["id"])
    assert limits.plan()[1] == []
    assert turns.settle_turn(live["id"], "completed")
    away = turns.enqueue_turn(row["id"], "user", prompt="Away answer", profile_id="agent",
                              payload={"question_answer_human_input": False})
    assert turns.start_turn(away["id"])
    assert [(sid, tid, reason) for sid, tid, reason, _ in limits.plan()[1]] == [
        (row["id"], away["id"], "daily_limit")]


async def test_deleted_starter_keeps_report_on_child(owner, monkeypatch):
    from app.core import turn_scheduler
    from app.db import sessions, turns, session_events
    from app.services import push_notification
    from tests.core.test_assistant_started_work import run
    pushed = []
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: None)
    monkeypatch.setattr(turn_scheduler, "note_wake_pending", lambda sid: None)
    monkeypatch.setattr(push_notification, "fire_and_forget_notify", lambda **kw: pushed.append(kw))
    chat("starter", mode="chat")
    outcome = await orch.make_handler(lambda: "starter")(
        "start", {"title": "Child", "prompt": "work", "request_id": "deleted"}, ToolCtx())
    assert not outcome.is_error, outcome.text
    receipt = json.loads(outcome.text)
    assert sessions.delete_session("starter")
    run(receipt["session_id"], receipt["turn_id"], "Report after deletion")
    await turn_scheduler.on_turn_settled(receipt["session_id"], receipt["turn_id"], "completed")
    await turn_scheduler.on_turn_settled(receipt["session_id"], receipt["turn_id"], "completed")
    assert sessions.get_session("starter") is None
    assert len(pushed) == 1
    events = session_events.get_events(receipt["session_id"])
    assert sum(e["event_type"] == "orchestration.report_deferred" for e in events) == 1


def test_coordination_prompt_is_static_across_checkout_registry_changes(owner, monkeypatch):
    from app.core import query_options, coordinator_files
    chat("coordinator", mode="chat")
    texts = []
    for root in (owner / "first-checkout", owner / "later-checkout"):
        monkeypatch.setattr(coordinator_files, "roots", lambda: [root])
        options, _ = query_options.build_options_from_profile(database.get_profile("agent"), session_id="coordinator")
        texts.append(str(options.system_prompt))
    assert texts[0] == texts[1]
    assert str(owner) not in texts[0]
