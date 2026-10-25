"""Owner coordination from ordinary no-project sessions (issue #371, step 1)."""
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fluid.tools.registry import ToolCtx
from app.core import session_orchestration as orch, turn_scheduler
from app.core.session_modes import narrow_tool_access_for_mode
from app.core.tool_access import ToolAccess, tool_permitted
from app.db import database, turns, projects_v2, checkouts
from app.db.base import get_db
from app.core.config import settings
from tests.core.test_session_orchestration import chat
from tests.core.test_assistant_started_work import run, wakes, quiet_scheduler  # noqa: F401


@pytest.fixture
def owner(mock_db, monkeypatch):
    database.create_profile("agent", "Agent", None, {"model": "sonnet"})
    monkeypatch.setattr(orch, "_principal", lambda sid: None)
    return chat("coordinator", mode="chat")[0]


#: The principal the agent browser's preview cookie acts as. A no-project chat
#: it sends is still an owner coordinator, but keeps the chat ceiling (#641).
PREVIEW = {"owner": "install-admin", "kind": "admin", "principal_id": "preview"}


def as_preview(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(orch, "_principal", lambda sid: dict(PREVIEW))


async def call(sender, action, **args):
    return await orch.make_handler(lambda: sender)(action, args, ToolCtx())


async def test_owner_without_project_can_start_registered_project(owner, tmp_path):
    project = projects_v2.upsert_project("folder:example", name="Example")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(tmp_path))
    result = await call(owner["id"], "start", title="Work", prompt="Do work", request_id="start", project="Example")
    assert not result.is_error, result.text
    receipt = json.loads(result.text)
    child = database.get_session(receipt["session_id"])
    assert child["project_v2_id"] == project["id"]
    assert child["mode"] is None
    assert child["profile_id"] == owner["profile_id"]
    assert orch.started_by_owner(child["id"]) == owner["id"]
    run(child["id"], receipt["turn_id"], "Work finished.")
    await turn_scheduler.on_turn_settled(child["id"], receipt["turn_id"], "completed")
    assert "Work finished." in wakes(owner["id"])[0]["prompt"]


@pytest.mark.parametrize("fields", [{"api_user_id": "api"}, {"is_worker": True, "parent_session_id": "coordinator"},
                                    {"automation_id": "automation"}])
def test_owner_gate_fails_closed(owner, fields):
    row, _ = chat("restricted", **fields)
    assert not orch.is_owner_session(row)


async def test_settled_starter_is_not_woken_and_gets_push(owner, monkeypatch):
    from app.services import push_notification
    push = []
    monkeypatch.setattr(push_notification, "fire_and_forget_notify", lambda **kw: push.append(kw))
    result = await call(owner["id"], "start", title="Work", prompt="Do work", request_id="start")
    assert not result.is_error, result.text
    receipt = json.loads(result.text)
    database.set_session_active(owner["id"], False)
    run(receipt["session_id"], receipt["turn_id"], "Finished while starter was settled.")
    await turn_scheduler.on_turn_settled(receipt["session_id"], receipt["turn_id"], "completed")
    assert wakes(owner["id"]) == []
    assert len(push) == 1
    assert push[0]["data"]["session_id"] == receipt["session_id"]
    from app.db import notifications
    assert any(row["session_id"] == receipt["session_id"] for row in notifications.list_notifications("admin"))
    await turn_scheduler.on_turn_settled(receipt["session_id"], receipt["turn_id"], "completed")
    assert len(push) == 1 and wakes(owner["id"]) == []


def test_chat_surface_only_widens_for_owner(owner):
    broad = narrow_tool_access_for_mode(ToolAccess(None, []), "chat", owner_session=True)
    limited = narrow_tool_access_for_mode(ToolAccess(None, []), "chat")
    for tool in ("StartSession", "Task", "Glob", "Grep", "GetAppLogs", "ScheduleWakeup"):
        assert tool_permitted(tool, broad.allowed, broad.disallowed), tool
        assert not tool_permitted(tool, limited.allowed, limited.disallowed), tool
    for tool in ("Bash", "Monitor", "Edit", "Write", "run_python", "save_plugin", "save_skill"):
        assert not tool_permitted(tool, broad.allowed, broad.disallowed), tool

@pytest.mark.parametrize("native", [True, False])
@pytest.mark.parametrize("legacy", [True, False])
@pytest.mark.parametrize("mode", [None, "code", "chat"])
async def test_registered_project_reads_through_real_registry(
    owner: dict, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    native: bool, legacy: bool, mode: str | None,
) -> None:
    from app.core.provider.harness_adapter import HarnessAdapter, _delegated_allowed
    from app.core import config, query_options
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path / "workspace")
    as_preview(monkeypatch)
    project_path = tmp_path / "project"
    project_path.mkdir()
    source = project_path / "example.py"
    source.write_text("registered marker\n")
    outside = tmp_path / "private.txt"
    outside.write_text("private marker\n")
    escapes = [outside]
    try:
        (project_path / "escape.txt").symlink_to(outside)
        escapes.append(project_path / "escape.txt")
    except OSError:
        # Windows without Developer Mode or admin cannot create file symlinks
        # (WinError 1314); the direct outside read is still checked.
        assert os.name == "nt"
    if legacy:
        database.create_project("legacy", "Example", None, str(project_path))
    else:
        project = projects_v2.upsert_project("folder:example", name="Example")
        checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(project_path))
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET mode=%s WHERE id=%s", (mode, owner["id"]))
    sdk_options, _ = query_options.build_options_from_profile(
        database.get_profile("agent"), session_id=owner["id"],
    )
    adapter = HarnessAdapter()
    adapter.connect = AsyncMock()
    await adapter.connect_with_sdk_options(sdk_options)
    options = adapter.connect.await_args.args[0]
    assert options.extra["session_mode"] == "chat"
    for tool in ("Bash", "Monitor", "Edit", "Write", "run_python"):
        assert not tool_permitted(tool, options.tool_allowlist, options.disallowed_tools)
    adapter._options = options
    monkeypatch.setattr(adapter, "_ask_owner", AsyncMock(return_value="Unexpected permission prompt"))
    registry = await adapter._build_registry(options, native=native)
    ctx = ToolCtx(cwd=str(tmp_path))
    def name(tool: str) -> str:
        return tool if native else "mcp__attachments__" + tool
    for tool, args in [("Read", {"file_path": str(source)}),
                       ("Glob", {"path": str(project_path), "pattern": "**/*.py"}),
                       ("Grep", {"path": str(project_path), "pattern": "registered marker"})]:
        result = await registry.dispatch(name(tool), args, ctx)
        assert not result.is_error, result.text
        assert "example.py" in result.text if tool == "Glob" else "registered marker" in result.text
    for path in escapes:
        denied = await registry.dispatch(name("Read"), {"file_path": str(path)}, ctx)
        assert denied.is_error and "private marker" not in denied.text
    for tool in ("Glob", "Grep"):
        denied = await registry.dispatch(name(tool), {"path": str(tmp_path), "pattern": "*" if tool == "Glob" else "private"}, ctx)
        assert denied.is_error
    if not native:
        assert {"Read", "Glob", "Grep"}.isdisjoint(_delegated_allowed(options))
        assert {"mcp__attachments__Read", "mcp__attachments__Glob", "mcp__attachments__Grep"} <= set(_delegated_allowed(options))


async def test_app_config_write_waits_for_owner_on_delegated_bridge(owner, monkeypatch, tmp_path):
    from app.core.provider.harness_adapter import HarnessAdapter
    from app.core.provider.types import SessionOptions
    from fluid.tools.registry import ToolSpec, ToolOutcome
    options = SessionOptions(model="test", cwd=str(tmp_path),
        extra={"session_mode": "chat", "owner_coordinator": True, "app_session_id": owner["id"],
               "assistant_scope": {"session_id": owner["id"], "home_session_id": owner["id"], "roots": []}})
    adapter = HarnessAdapter()
    adapter._options = options
    ask = AsyncMock(return_value="Waiting for the owner's yes.")
    monkeypatch.setattr(adapter, "_ask_owner", ask)
    registry = await adapter._build_registry(options, native=False)
    changed = []

    async def mutate(args, ctx):
        changed.append(args)
        return ToolOutcome(text="changed")

    registry.register_builtin(ToolSpec(name="mcp__app-config__createHook", description="Change config",
        input_schema={"type": "object"}, risk="write"), mutate)
    result = await registry.dispatch("mcp__app-config__createHook", {"event": "Stop"}, ToolCtx())
    assert result.is_error and changed == []
    ask.assert_awaited_once()


def test_foreign_principal_never_gains_authority(
    owner: dict, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orch, "_principal", lambda sid: {"owner": "account:someone-else"})
    assert not orch.is_owner_session(owner)
    from app.core.session_modes import runtime_session_mode
    # #553 changes ordinary owner rows only, not a foreign paired principal.
    assert runtime_session_mode({**owner, "mode": None}) == "code"


async def test_existing_daily_setting_counts_coordinator_and_children_after_settle(owner, monkeypatch):
    from app.core import spending_limits, orchestration_limits, owner_services
    from app.db import spending_limits as limits_db
    from tests.core.test_assistant_limits import book
    result = await call(owner["id"], "start", title="Work", prompt="Do work", request_id="budget")
    receipt = json.loads(result.text)
    limits_db.update(owner_services.assistant_id(), {"spend_limit_enabled": True, "daily_spend_limit_usd": 2})
    from app.db.analytics import settle_turn_usage
    from app.core.query_persistence import usage_settlement_key
    owner_turn = turns.enqueue_turn(owner["id"], "user", prompt="Owner input", profile_id="agent")
    assert settle_turn_usage(owner["id"], usage_settlement_key(owner["id"], owner_turn), cost_usd=1.25)
    book(receipt["session_id"], 2.25)  # Job spend only; the owner's chat is exempt.
    database.set_session_active(owner["id"], False)
    assert spending_limits.status()["spent_today_usd"] == pytest.approx(2.25)
    assert orchestration_limits.start_refusal(owner["id"])
    assert orchestration_limits.turn_refusal(owner["id"], "user") is None
    assert orchestration_limits.turn_refusal(receipt["session_id"], "unpark")
    database.create_project("independent-project", "Independent", None, "/example/project")
    independent = database.create_session("independent", "agent", project_id="independent-project")
    assert orchestration_limits.start_refusal(independent["id"]) is None
    assert orchestration_limits.turn_refusal(receipt["session_id"], "user") is None
    # Archiving a completed child must not refund the day's spending.
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET status='archived' WHERE id=%s", (receipt["session_id"],))
    assert spending_limits.status()["spent_today_usd"] == pytest.approx(2.25)


def test_runtime_lane_of_legacy_projectless_rows(
    owner: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    # #641: the owner's own projectless row is a normal session whatever its
    # stored mode. #553 still confines the rows nobody is watching for the owner.
    from app.core.session_modes import runtime_session_mode
    for mode in (None, "code", "chat"):
        assert runtime_session_mode({**owner, "mode": mode}) == "code"
    assert runtime_session_mode({**owner, "mode": None, "project_v2_id": "project"}) == "code"
    assert runtime_session_mode({**owner, "id": "watch-event", "mode": None}) == "chat"
    as_preview(monkeypatch)
    assert runtime_session_mode({**owner, "mode": None}) == "chat"
    assert runtime_session_mode({**owner, "mode": "chat"}) == "chat"


@pytest.mark.parametrize("mode", [None, "code", "chat"])
@pytest.mark.parametrize("permission", ["default", "acceptEdits", "bypassPermissions"])
def test_selected_agent_effort_and_runtime_ceiling_are_built_together(
    owner: dict, monkeypatch: pytest.MonkeyPatch, tmp_path: "Path",
    mode: str | None, permission: str,
) -> None:
    from app.core import query_options, config
    from app.core.session_modes import CHAT_MODE_TOOL_SURFACE
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    monkeypatch.setattr(query_options, "get_security_instructions", lambda *a, **kw: "")
    # A preview-cookie chat: the owner's, but it keeps the ceiling (#641).
    as_preview(monkeypatch)
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET mode=%s WHERE id=%s", (mode, owner["id"]))
    database.set_session_run_binding(owner["id"], model="sonnet", effort="high", permission_mode=permission)
    options, _ = query_options.build_options_from_profile(database.get_profile("agent"), session_id=owner["id"])
    assert options.effort == "high"
    assert options.aih_profile_id == "agent"
    assert options.aih_owner_coordinator
    # Every mode runs as given; Accept Edits has no edit tool to approve here.
    assert options.permission_mode == permission
    from app.core.session_modes import chat_uploads_dir
    assert str(chat_uploads_dir()) in options.aih_assistant_scope["roots"]
    assert "app-config" in options.mcp_servers
    assert all(tool_permitted(name, CHAT_MODE_TOOL_SURFACE, []) for name in options.allowed_tools)
    for tool in ("Bash", "Monitor", "Edit", "Write", "run_python"):
        assert not tool_permitted(tool, options.aih_tool_allowlist, options.disallowed_tools)
    for tool in ("StartSession", "Task", "Glob", "Grep", "GetAppLogs", "ScheduleWakeup"):
        assert tool in options.aih_tool_allowlist
    assert "# No-project coordination" in str(options.system_prompt)
    assert "Use StartSession(project=...) for project work" in str(options.system_prompt)


@pytest.mark.parametrize("v2", [False, True])
def test_project_bound_legacy_owner_keeps_checkout_and_full_surface(
    owner: dict, tmp_path: Path, v2: bool,
) -> None:
    from app.core import query_options

    database.create_project("bound-project", "Project", None, str(tmp_path))
    project = projects_v2.upsert_project("folder:bound-project", name="Project")
    checkouts.upsert_checkout(project["id"], settings.effective_instance_id, str(tmp_path))
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE sessions SET mode=NULL, project_id=%s, project_v2_id=%s WHERE id=%s",
            (None if v2 else "bound-project", project["id"] if v2 else None, owner["id"]),
        )
    options, _ = query_options.build_options_from_profile(
        database.get_profile("agent"), project=database.get_project("bound-project"),
        session_id=owner["id"],
    )
    assert not options.aih_owner_coordinator
    assert options.cwd == str(tmp_path)
    for tool in ("Bash", "Edit", "Write", "run_python"):
        assert tool_permitted(tool, options.aih_tool_allowlist, options.disallowed_tools)


def test_owner_approval_marker_skips_api(owner):
    from app.core import provider_agent_approvals
    route = provider_agent_approvals.WorkerApprovalRoute(worker_session_id="child", parent_session_id=owner["id"], label="Work")
    assert provider_agent_approvals._assistant_confirm(owner["id"], {"tool_name": "Write", "tool_input": {}}, route)
    row, _ = chat("api-coordinator", mode="chat", api_user_id="api")
    assert provider_agent_approvals._assistant_confirm(row["id"], {"tool_name": "Write"}, route) is None


