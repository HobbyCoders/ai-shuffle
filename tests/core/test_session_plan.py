"""Host Plan registration, owner/run fences, durable events and transcript ACL."""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from app.core.config import settings
from app.core.session_plan import Backend
from app.core.provider import harness_adapter as ha
from app.core.provider.types import SessionOptions
from app.db import database, runs, session_plans, session_events
from app.db.base import get_db
from app.network import identity
from fluid.tools.builtins import plan, register_builtins
from fluid.tools.registry import ToolCtx, ToolRegistry


def initial(**kw):
    return {"action": "update", "expected_revision": 0, "request_id": "initial",
            "goal": "Deliver fix", "done_when": "Check real producer",
            "tasks": [{"id": "one", "title": "Make fix", "status": "in_progress"}], **kw}


@pytest.fixture
def active_plan(mock_db):
    sid = "plan-session"
    database.create_session(sid, "default", title="Plan")
    run = runs.claim_run(sid, settings.effective_instance_id, 120)
    proof = (sid, run["id"], run["fence_token"])
    ctx = ToolCtx(cwd="/unused", session_id="sdk-not-app")
    ctx.extra["session_plan"] = Backend(lambda: sid, lambda: proof)
    return sid, run, ctx


async def test_registered_handler_persists_before_event_and_survives_fresh_engine(active_plan):
    sid, run, ctx = active_plan
    registry = ToolRegistry()
    register_builtins(registry)
    result = await registry.dispatch("Plan", initial(), ctx)
    assert not result.is_error, result.text
    assert json.loads(result.text)["revision"] == 1 and "plan" not in json.loads(result.text)
    assert session_plans.read(sid)["plan"]["goal"] == "Deliver fix"
    rows = session_events.get_events(sid)
    published = [r for r in rows if r["event_type"] == "plan.updated"]
    assert len(published) == 1
    assert published[0]["payload"]["plan"] == session_plans.read(sid)["plan"]
    assert published[0]["payload"]["status_source"] == "model_reported"
    retry = await plan.handler(initial(), ctx)
    assert json.loads(retry.text)["replayed"]
    assert len(session_events.get_events(sid)) == len(rows)
    fresh = ToolCtx(cwd="/different/root", session_id="new-sdk")
    fresh.extra["session_plan"] = Backend(lambda: sid, lambda: (sid, run["id"], run["fence_token"]))
    assert "Deliver fix" in plan.reminder(fresh)
    assert json.loads((await plan.handler({"action": "get"}, fresh)).text)["plan"]["revision"] == 1


async def test_stale_run_and_foreign_owner_cannot_mutate(active_plan):
    sid, run, ctx = active_plan
    assert not (await plan.handler(initial(), ctx)).is_error
    runs.release_run(run["id"], run["fence_token"])
    newer = runs.claim_run(sid, settings.effective_instance_id, 120)
    result = await plan.handler(initial(expected_revision=1, request_id="old"), ctx)
    assert result.is_error and "superseded" in result.text
    runs.release_run(newer["id"], newer["fence_token"])
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET owner_device_id=%s WHERE id=%s", ("foreign", sid))
    refused = await plan.handler(initial(), ctx)
    assert refused.is_error and "another worker" in refused.text
    assert session_plans.read(sid)["plan"]["revision"] == 1


async def test_event_failure_rolls_back_document_and_receipt(active_plan, monkeypatch):
    sid, _, ctx = active_plan
    def fail(*args, **kwargs):
        raise ValueError("event rejected")
    monkeypatch.setattr(session_events, "append_events", fail)
    result = await plan.handler(initial(), ctx)
    assert result.is_error
    assert session_plans.read(sid)["plan"]["revision"] == 0


@pytest.mark.parametrize("native", [True, False])
async def test_both_registries_offer_one_deferred_planner(native):
    adapter = ha.HarnessAdapter()
    options = SessionOptions(model="m", cwd="/fixture", mcp_servers={},
                             extra={"app_session_id": "bound-session"})
    with patch.object(ha, "_registers_bundled_search", return_value=False):
        registry = await adapter._build_registry(options, native=native)
    names = [s.name for s in registry.list_tools(loaded_only=False)]
    assert names.count("Plan") == 1
    if native:
        assert "Plan" in {s.name for s in registry.deferred_tools()}
    else:
        from app.core.provider.fluid_tool_bridge import delegated_bridge_deferred
        assert delegated_bridge_deferred()("Plan")
    blocked = set(ha._delegated_disallowed(options, registry))
    if not native:
        assert {"TodoWrite", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList"} <= blocked
        assert "mcp__builtins__TaskList" not in blocked and "mcp__builtins__TaskGet" not in blocked
        assert "mcp__fluid_bundled__Plan" in ha._delegated_permission_grants(options, registry)


@pytest.mark.parametrize("allowed,denied,registered", [(["Read", "TodoWrite", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList"], [], True), (None, ["Plan"], True), (None, [], False)])
def test_no_effective_plan_leaves_native_cli_planner_alone(allowed, denied, registered):
    registry = ToolRegistry()
    if registered:
        registry.register_builtin(plan.SPEC, plan.handler)
    options = SessionOptions(model="m", cwd="/fixture", tool_allowlist=allowed,
                             disallowed_tools=denied, mcp_servers={})
    blocked = set(ha._delegated_disallowed(options, registry))
    assert not ({"TodoWrite", "TaskCreate", "TaskUpdate", "TaskGet", "TaskList"} & blocked)
    assert "mcp__fluid_bundled__Plan" not in (ha._delegated_permission_grants(options, registry) or [])


@pytest.mark.parametrize("native", [True, False])
async def test_stateless_adapter_does_not_advertise_broken_app_plan(native):
    adapter = ha.HarnessAdapter()
    adapter._options = SessionOptions(model="m", cwd="/fixture", mcp_servers={})
    registry = await adapter._build_registry(adapter._options, native=native)
    ctx = adapter._with_away_hook(ToolCtx())
    assert "session_plan" not in ctx.extra
    if native:
        assert "Plan" not in ha._wired_tool_names(registry, None, ha._wired_capabilities(ctx))
        assert registry.get("Plan").requires_extra == ("session_plan",)
    else:
        assert registry.get("Plan") is None
        assert "TodoWrite" not in ha._delegated_disallowed(adapter._options, registry)
        assert "mcp__fluid_bundled__Plan" not in (ha._delegated_permission_grants(adapter._options, registry) or [])


async def test_plan_endpoint_uses_transcript_access_before_storage(monkeypatch):
    from app.api import sessions
    denied = AsyncMock(side_effect=HTTPException(403, "private"))
    monkeypatch.setattr(sessions, "require_transcript_session", denied)
    with patch.object(session_plans, "projection") as reader:
        with pytest.raises(HTTPException) as exc:
            await sessions.get_session_plan(SimpleNamespace(), "private-session")
        assert exc.value.status_code == 403
        reader.assert_not_called()
