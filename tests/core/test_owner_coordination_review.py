"""Permission and accounting regressions from the step-1 security review."""
import json
from pathlib import Path

import pytest
from fluid.tools.registry import ToolCtx
from app.core import session_orchestration as orch
from app.db import database
from app.db.base import get_db
from tests.core.test_session_orchestration import chat


@pytest.fixture
def owner(mock_db, monkeypatch, tmp_path):
    from app.core import query_options, config, turn_scheduler
    # These tests inspect durable queue/permission/accounting state, not provider
    # inference. A real kick starts work in a thread and races the SQL counter.
    monkeypatch.setattr(turn_scheduler, "kick", lambda sid: None)
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    monkeypatch.setattr(query_options, "get_security_instructions", lambda *a, **kw: "")
    return tmp_path


@pytest.mark.parametrize("project", [False, True])
@pytest.mark.parametrize("permission", ["default", "auto", "acceptEdits", "bypassPermissions"])
async def test_start_preserves_permission_without_assistant_scope(owner, project, permission):
    from app.core import query_options, away_mode
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
    # #371 step 5: Away-style question answering reaches every owner chat's
    # started chats; it answers questions only and never widens permissions.
    assert away_mode.assistant_work(child)
    # The child starts in the starter's current mode (#486), also from a
    # no-project chat; such a chat runs Accept Edits as Ask, so passes on Ask.
    expected = "default" if not project and permission == "acceptEdits" else permission
    assert database.get_session(child)["permission_mode"] == expected
    options, _ = query_options.build_options_from_profile(database.get_profile("agent"), session_id=child)
    assert options.permission_mode == expected
    assert not getattr(options, "aih_assistant_scope", None)
    # Task workers under an ordinary child keep their own mode.
    worker = database.create_session("task-worker", "agent", is_worker=True, parent_session_id=child,
                                     project_v2_id=registered["id"], permission_mode=permission)
    options, _ = query_options.build_options_from_profile(database.get_profile("agent"), session_id=worker["id"])
    assert options.permission_mode == permission


def _project(owner):
    from app.db import projects_v2, checkouts
    from app.core.config import settings
    registered = projects_v2.upsert_project("folder:review", name="Project")
    checkouts.upsert_checkout(registered["id"], settings.effective_instance_id, str(owner))
    return registered


async def _start(starter, request_id="review", **extra):
    result = await orch.make_handler(lambda: starter)(
        "start", {"title": "Child", "prompt": "work", "request_id": request_id, **extra}, ToolCtx())
    assert not result.is_error, result.text
    return json.loads(result.text)["session_id"]


@pytest.mark.parametrize("case", ["null_binding_agent", "stored"])
@pytest.mark.parametrize("permission", ["default", "auto", "bypassPermissions"])
async def test_chat_coordinator_starts_children_in_its_own_mode(owner, case, permission):
    """A no-project coordinator's started sessions, in a named project or
    without one, start in the coordinator's current mode (#486, owner
    decision replacing the #415/#416 rule that started them on Ask)."""
    from app.core import query_options
    _project(owner)
    chat("coord", mode="chat")
    if case == "null_binding_agent":
        database.update_profile("agent", config={"model": "sonnet", "permission_mode": permission})
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET permission_mode=NULL WHERE id='coord'")
    else:
        database.set_session_run_binding("coord", permission_mode=permission)
    coordinator, _ = query_options.build_options_from_profile(database.get_profile("agent"), session_id="coord")
    assert coordinator.permission_mode == permission
    for child in (await _start("coord", "named", project="Project"), await _start("coord", "chat")):
        assert database.get_session(child)["permission_mode"] == permission
        options, _ = query_options.build_options_from_profile(database.get_profile("agent"), session_id=child)
        assert options.permission_mode == permission


async def test_chat_coordinator_never_hands_on_a_mode_it_cannot_run(owner):
    """An unbound chat whose agent says Accept Edits runs on Ask (a chat has
    no Accept Edits), so a project child it starts runs on Ask too."""
    _project(owner)
    chat("coord", mode="chat")
    database.update_profile("agent", config={"model": "sonnet", "permission_mode": "acceptEdits"})
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET permission_mode=NULL WHERE id='coord'")
    child = await _start("coord", "named", project="Project")
    assert database.get_session(child)["permission_mode"] == "default"


@pytest.mark.parametrize("fate", ["live", "deleted", "archived", "settled"])
async def test_approvals_route_only_to_a_live_starter(owner, fate):
    from app.core.provider_agent_approvals import assistant_route
    from app.db import sessions
    _project(owner)
    chat("starter", mode="chat")
    child = await _start("starter", project="Project")
    if fate == "deleted":
        assert sessions.delete_session("starter")
    elif fate == "archived":
        with get_db() as conn:
            conn.cursor().execute("UPDATE sessions SET status='archived', is_active=FALSE WHERE id='starter'")
    elif fate == "settled":
        database.set_session_active("starter", False)
    route = assistant_route(child, None)
    if fate == "live":
        assert route is not None and route.parent_session_id == "starter"
    else:
        # The ordinary owner approval path; a deleted starter still owes a report.
        assert route is None
        assert orch.started_by_owner(child) == "starter"


def _count_queries(monkeypatch):
    from tests.pg_shim import ShimCursor
    queries = []
    execute = ShimCursor.execute

    def measured(cursor, sql, params=None):
        queries.append(sql)
        return execute(cursor, sql, params)
    monkeypatch.setattr(ShimCursor, "execute", measured)
    return queries


async def test_limit_role_scan_is_batched_at_400_sessions(owner, monkeypatch):
    from app.core import orchestration_limits
    from app.core.config import settings
    from app.db import sessions
    from tests.core.test_assistant_limits import book
    _project(owner)
    for i in range(400):
        sessions.create_session(f"c{i}", "agent", title=f"c{i}", is_active=True, mode="chat")
        book(f"c{i}", 0.01)
    chat("coord", mode="chat")
    child = await _start("coord", project="Project")
    from app.db import turns
    [queued] = turns.get_queued_turns(child)
    assert queued["state"] == "queued"  # Real producer, without provider execution.
    worker = database.create_session("task-worker", "agent", is_worker=True, parent_session_id=child)
    for sid in (child, worker["id"]):
        book(sid, 0.01)
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute("UPDATE sessions SET instance_id=%s", (settings.effective_instance_id,))
        # Chats started from the owner's signed-in phone carry an account label.
        cursor.execute("UPDATE sessions SET provenance_owner='account:acct' WHERE id LIKE 'c1%%'")
    from app.core import preference_identity
    accounts = []
    monkeypatch.setattr(preference_identity, "local_owner_account", lambda: accounts.append(1) or "acct")
    principals = []
    monkeypatch.setattr(orch, "_principal", lambda sid: principals.append(sid) or None)
    queries = _count_queries(monkeypatch)
    roles = orchestration_limits.tracked_roles()
    assert principals == [] and len(accounts) == 1 and len(queries) <= 12, (len(queries), queries[:20])
    assert roles == {**{f"c{i}": "coordinator" for i in range(400)}, child: "job", worker["id"]: "job"}
    queries.clear()
    # The batched answer matches the single-session resolver.
    assert all(orchestration_limits.role(sid) == kind for sid, kind in list(roles.items())[:5])


def test_limits_off_skip_the_role_scan(owner, monkeypatch):
    from app.core import orchestration_limits, assistant_limits, assistant_service
    from app.db import assistant_limits as limits_db

    def scan(now=None):
        raise AssertionError("no coordination role scan while both limits are off")
    monkeypatch.setattr(orchestration_limits, "tracked_roles", scan)
    chat("coord", mode="chat")
    limits_db.update(assistant_service.assistant_id(), {"spend_limit_enabled": False, "job_limit_enabled": False})
    assert assistant_limits.status()["limit_reached"] is False
    assert assistant_limits.plan()[1] == []
    limits_db.update(assistant_service.assistant_id(), {"job_limit_enabled": True})
    with pytest.raises(AssertionError, match="no coordination role scan"):
        assistant_limits.status()


async def test_job_time_limit_applies_to_coordination_jobs(owner):
    from app.core import orchestration_limits, assistant_limits, assistant_service
    from app.db import assistant_limits as limits_db, turns
    _project(owner)
    chat("coord", mode="chat")
    child = await _start("coord", project="Project")
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET work_ms=%s WHERE id=%s", (10 * 60000, child))
    assert orchestration_limits.role(child) == "job"
    limits_db.update(assistant_service.assistant_id(), {"job_limit_enabled": False})
    assert orchestration_limits.turn_refusal(child, "unpark") is None
    limits_db.update(assistant_service.assistant_id(), {"job_limit_enabled": True, "max_job_minutes": 5})
    assert "background job passed" in orchestration_limits.turn_refusal(child, "unpark")
    [turn] = turns.get_queued_turns(child)
    assert turns.start_turn(turn["id"])
    from tests.core.test_assistant_limits import book
    book(child, 0.01)
    assert [(sid, reason) for sid, _tid, reason, _ in assistant_limits.plan()[1]] == [(child, "job_time")]
    # The coordinator itself is never stopped for job time.
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET work_ms=%s WHERE id='coord'", (10 * 60000,))
    assert orchestration_limits.turn_refusal("coord", "unpark") is None


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


def test_legacy_projectless_code_row_gets_coordination_ceiling(owner: Path) -> None:
    # #553: preserving the code ceiling here pinned the deferred migration bug.
    from app.core import query_options
    from app.core.session_modes import runtime_session_mode
    from app.core.tool_access import tool_permitted

    row, _ = chat("legacy")
    assert runtime_session_mode(row) == "chat"
    options, _ = query_options.build_options_from_profile(
        database.get_profile("agent"), session_id=row["id"],
    )
    assert options.aih_owner_coordinator
    for tool in ("Bash", "Edit", "Write", "run_python"):
        assert not tool_permitted(tool, options.aih_tool_allowlist, options.disallowed_tools)


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
