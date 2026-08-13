"""Memory builtin tool (P1.5, ruling A11): view/add/replace/clear against the
profile row's config.memory, size cap enforced, builtin profiles writable."""

import asyncio
import json
from unittest.mock import patch

import pytest


def _build_memory_handler(profile_id):
    import claude_agent_sdk as sdk
    import app.mcp.builtin_tools as bt

    captured = {}
    real_create = sdk.create_sdk_mcp_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_sdk_mcp_server", side_effect=_spy):
        bt.create_builtin_tools_server(
            session_id="s1", profile_id=profile_id, project_id=None
        )
    tools = {t.name: t for t in captured["tools"]}
    assert "Memory" in tools, "Memory tool was not registered"
    return tools["Memory"].handler


def _payload(result):
    return json.loads(result["content"][0]["text"])


@pytest.fixture()
def agent_profile(mock_db):
    from app.db import database

    database.create_profile(
        "agent-mem", "Mem Agent", None, {"model": "opus"}, is_builtin=False
    )
    return "agent-mem"


@pytest.fixture()
def builtin_profile(mock_db):
    from app.db import database

    database.create_profile(
        "agent-builtin", "Builtin Agent", None, {"model": "opus"}, is_builtin=True
    )
    return "agent-builtin"


def test_view_empty(agent_profile):
    handler = _build_memory_handler(agent_profile)
    out = _payload(asyncio.run(handler({"action": "view"})))
    assert out["memory"] == "(empty)"


def test_add_then_view_then_replace_then_clear(agent_profile):
    from app.db import database

    handler = _build_memory_handler(agent_profile)

    out = _payload(asyncio.run(handler({"action": "add", "text": "fact one"})))
    assert out["saved"] is True
    assert database.get_profile(agent_profile)["config"]["memory"] == "fact one"

    _payload(asyncio.run(handler({"action": "add", "text": "fact two"})))
    mem = database.get_profile(agent_profile)["config"]["memory"]
    assert "fact one" in mem and "fact two" in mem

    _payload(asyncio.run(handler({"action": "replace", "text": "condensed"})))
    assert database.get_profile(agent_profile)["config"]["memory"] == "condensed"

    _payload(asyncio.run(handler({"action": "clear"})))
    assert database.get_profile(agent_profile)["config"]["memory"] == ""


def test_size_cap(agent_profile):
    handler = _build_memory_handler(agent_profile)
    result = asyncio.run(handler({"action": "replace", "text": "x" * 9000}))
    out = _payload(result)
    assert result.get("is_error") or "cap" in json.dumps(out)
    from app.db import database

    assert not (database.get_profile(agent_profile)["config"].get("memory"))


def test_builtin_profile_memory_writable(builtin_profile):
    from app.db import database

    handler = _build_memory_handler(builtin_profile)
    out = _payload(asyncio.run(handler({"action": "add", "text": "learned"})))
    assert out["saved"] is True
    assert database.get_profile(builtin_profile)["config"]["memory"] == "learned"


def test_requires_profile_context(mock_db):
    handler = _build_memory_handler(None)
    result = asyncio.run(handler({"action": "view"}))
    assert "active agent" in result["content"][0]["text"]
