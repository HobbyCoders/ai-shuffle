"""Session planning reaches chat without granting project, file or Python access."""
from unittest.mock import AsyncMock, patch

import pytest
from app.core.provider import harness_adapter as ha
from app.db import session_plans, session_events
from app.db.base import get_db
from fluid.tools.builtins import plan
from fluid.tools.registry import ToolCtx
from tests.core.test_session_plan import active_plan, initial  # noqa: F401


@pytest.mark.parametrize("native", [True, False], ids=["native", "delegated"])
@pytest.mark.parametrize("config,expected", [
    ({"tool_selection_mode": "all"}, True),
    ({"tool_selection_mode": "disallow", "disallowed_tools": ["Plan"]}, False),
    ({"tool_selection_mode": "allow", "allowed_tools": ["Read"]}, False),
], ids=["session-plan", "explicit-deny", "restricted-agent"])
async def test_no_project_chat_plan_keeps_file_and_kernel_fences(active_plan, monkeypatch, native, config, expected):
    from app.core.session_modes import chat_mode_read_permitted
    from app.core.tool_access import tool_permitted
    from tests.core.test_session_modes import _build_for_mode

    # Exercise the actual no-project chat options builder and SDK compatibility
    # boundary, not a hand-seeded Plan allowlist. No provider process is needed.
    sdk = _build_for_mode("chat", {"model": "fake-ai", **config})
    adapter = ha.HarnessAdapter()
    adapter.connect = AsyncMock()
    await adapter.connect_with_sdk_options(sdk)
    options = adapter.connect.call_args.args[0]
    assert options.extra["session_mode"] == "chat"
    assert ha._expects_code_mode(options) is False
    for name in ("Bash", "Write", "Edit", "run_python", "save_skill", "save_plugin"):
        assert not tool_permitted(name, options.tool_allowlist, options.disallowed_tools)
    assert not chat_mode_read_permitted("Read", {"file_path": "/outside-attachments/private"})
    with patch.object(ha, "_registers_bundled_search", return_value=False), patch.object(ha, "_native_external_mcp_servers", return_value={}):
        registry = await adapter._build_registry(options, native=native)
    permit = ha._tool_predicate(options)
    if native:
        # Chat has no ToolSearch/kernel loader: permitted tools must remain
        # directly callable through its existing JSON-schema path.
        offered = {s.name for s in registry.list_tools(loaded_only=True) if permit is None or permit(s.name)}
        assert ("Plan" in offered) is expected
    else:
        assert ha._delegated_offered(options, registry)("Plan") is expected
        assert ha._delegated_bridge_deferral(options) is None
        assert ("mcp__fluid_bundled__Plan" in (ha._delegated_permission_grants(options, registry) or [])) is expected
        blocked = set(ha._delegated_disallowed(options, registry))
        assert ("TodoWrite" in blocked) is expected
        assert "Bash" in blocked and "Write" in blocked and "Read" in blocked
    if not expected:
        return
    sid, run, _ctx = active_plan
    with get_db() as conn:
        conn.cursor().execute("UPDATE sessions SET mode='chat' WHERE id=%s", (sid,))
    adapter._options = options
    options.extra["app_session_id"] = sid
    adapter._plan_fence = (sid, run["id"], run["fence_token"])
    context = adapter._with_away_hook(ToolCtx(cwd="/not-a-project-or-checkout", session_id="sdk-only"))
    def no_file_fallback(*args):
        raise AssertionError("App chat Plan must not use project/local-file storage")
    monkeypatch.setattr(plan, "_local", no_file_fallback)
    result = await registry.dispatch("Plan", initial(), context)
    assert not result.is_error, result.text
    assert session_plans.read(sid)["plan"]["revision"] == 1
    assert [e for e in session_events.get_events(sid) if e["event_type"] == "plan.updated"]
    assert "Deliver fix" in plan.reminder(context)
