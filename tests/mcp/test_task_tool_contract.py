"""Provider-scoped Task's delegated MCP result contract."""

import asyncio
import json
from unittest.mock import patch


def _task_handler(*, profile_id: str, provider_id: str | None = None):
    import claude_agent_sdk as sdk

    from app.mcp import builtin_tools

    captured = {}
    real_create = sdk.create_sdk_mcp_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_sdk_mcp_server", side_effect=_spy):
        builtin_tools.create_builtin_tools_server(
            session_id="session-parent",
            profile_id=profile_id,
            provider_id=provider_id,
        )

    tools = {tool.name: tool for tool in captured["tools"]}
    return tools["Task"].handler


def test_task_mcp_preserves_structured_error_and_machine_error_status(
    mock_db, monkeypatch
):
    from app.core import task_dispatch
    from app.db import database

    database.create_profile(
        "caller",
        "Caller",
        None,
        {"model": "sonnet", "provider_models": {"__native__": "sonnet"}},
    )

    async def _reject(*args, **kwargs):
        return {"ok": False, "error": "target rejected"}

    monkeypatch.setattr(task_dispatch, "dispatch_task_tool", _reject)
    result = asyncio.run(
        _task_handler(profile_id="caller")(
            {"agent": "missing", "prompt": "Do the work"}
        )
    )

    assert result["isError"] is True
    assert result["structuredContent"] == {
        "ok": False,
        "error": "target rejected",
    }
    assert json.loads(result["content"][0]["text"]) == result["structuredContent"]
