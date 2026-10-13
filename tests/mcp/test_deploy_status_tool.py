"""DeployStatus builtin: registered, allow-listed, read-only and compact."""
import asyncio
import json
from unittest.mock import AsyncMock, patch

import app.mcp.inproc as sdk


def _handlers(is_admin=True):
    from app.mcp import builtin_tools

    captured = {}
    real_create = sdk.create_tool_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_tool_server", side_effect=_spy):
        builtin_tools.create_builtin_tools_server(session_id="deploy-test", is_admin=is_admin)
    return {tool.name: tool for tool in captured["tools"]}


def test_registered_allowlisted_and_safe():
    from app.core.provider.fluid_tool_bridge import BUILTINS_RISK_MAP, NATIVE_SHADOWED_BUILTINS
    from app.mcp.builtin_tools import get_builtin_tool_names

    assert "DeployStatus" in _handlers()
    assert "mcp__builtins__DeployStatus" in get_builtin_tool_names()
    # Read-only: never prompts in Ask mode on either leg.
    assert BUILTINS_RISK_MAP["DeployStatus"] == "safe"
    assert "DeployStatus" not in NATIVE_SHADOWED_BUILTINS


def test_description_marks_it_on_demand():
    description = _handlers()["DeployStatus"].description
    assert "when the owner asks" in description
    assert "not as a routine step after a merge" in description


def _body(result):
    return json.loads(result["content"][0]["text"])


def test_passes_pr_and_drops_empty_fields():
    report = {"origin_main": "abc", "reason": None, "change": {"pr": 5}, "android": None,
              "summary": "Live on: ServerWorker ✓",
              "workers": [{"label": "ServerWorker", "device_id": "d", "behind": 0, "reason": None, "action": "up to date"}]}
    with patch("app.core.deploy_status.deploy_status", new=AsyncMock(return_value=report)) as call:
        body = _body(asyncio.run(_handlers()["DeployStatus"].handler({"pr": 5})))
    call.assert_awaited_once_with(pr=5, commit=None)
    assert body["summary"] == "Live on: ServerWorker ✓"
    assert body["workers"] == [{"label": "ServerWorker", "behind": 0, "action": "up to date"}]
    assert "reason" not in body and "android" not in body


def test_rejects_bad_input_and_api_key_sessions():
    handlers = _handlers()
    assert _body(asyncio.run(handlers["DeployStatus"].handler({"pr": 1, "commit": "abc1234"})))["success"] is False
    assert _body(asyncio.run(handlers["DeployStatus"].handler({"pr": "1"})))["success"] is False
    denied = _body(asyncio.run(_handlers(is_admin=False)["DeployStatus"].handler({})))
    assert denied["success"] is False
