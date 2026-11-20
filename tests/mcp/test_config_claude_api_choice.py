"""S15: an agent's config tools cannot newly choose a Claude API model.

Claude API bills the saved Anthropic key. Only the owner chooses it (picker,
Automations or Agents page); a choice the owner already saved may be kept.
"""

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.core.automation_manager import automation_manager
from app.mcp.config_automations import _create_automation_tools
from app.mcp.config_helpers import claude_api_choice_error
from app.mcp.config_profiles import _create_profile_tools
from tests.mcp.tool_helpers import get_tool, tool_result_json

API = "prov:claude-api:claude-sonnet-5"
API_OPUS = "prov:claude-api:claude-opus-5"


def _automation_tool(name):
    return get_tool(_create_automation_tools(is_admin=True, project_id=None), name)


def test_the_rule():
    assert claude_api_choice_error(API, where="x")
    assert claude_api_choice_error(API, API, where="x") is None  # owner's own, kept
    assert claude_api_choice_error(API_OPUS, API, where="x")  # another paid model
    assert claude_api_choice_error("sonnet", API, where="x") is None  # off the key is fine
    assert claude_api_choice_error("prov:oa:gpt-5", where="x") is None
    assert claude_api_choice_error({"__native__": API}, {}, where="x")
    assert claude_api_choice_error({"__native__": API}, {"__native__": API}, where="x") is None


def _accept_any_model(model, provider_id=None, **_):
    return model, (model.split(":")[1] if model and model.startswith("prov:") else None)


@pytest.mark.asyncio
async def test_create_automation_refuses_a_claude_api_model(mock_db):
    from app.db import database

    with patch("app.core.automation_service.normalize_model_choice", side_effect=_accept_any_model), \
            patch.object(automation_manager, "schedule_automation", new=AsyncMock()):
        result = await _automation_tool("createAutomation").handler({
            "name": "review", "trigger_type": "manual", "prompt": "review",
            "profile_id": "default", "model": API, "requires_approval": False,
        })
    payload = json.loads(result["content"][0]["text"])
    assert payload["success"] is False and "Claude API" in payload["error"]
    assert not [a for a in database.get_automations_for_profile("default") if a.get("name") == "review"]


@pytest.mark.asyncio
async def test_update_automation_keeps_the_owners_api_pick_but_cannot_change_to_one(mock_db):
    from app.db import database

    database.create_automation(automation_id="auto-api", name="Owner's", trigger_type="manual",
                               trigger_config={}, profile_id="default", prompt="hi",
                               model=API, provider_id="claude-api")
    database.create_automation(automation_id="auto-sub", name="Sub", trigger_type="manual",
                               trigger_config={}, profile_id="default", prompt="hi", model="sonnet")
    tool = _automation_tool("updateAutomation")
    with patch("app.core.automation_service.normalize_model_choice", side_effect=_accept_any_model), \
            patch.object(automation_manager, "schedule_automation", new=AsyncMock()), \
            patch.object(automation_manager, "unschedule_automation", new=AsyncMock(), create=True):
        kept = json.loads((await tool.handler({"automation_id": "auto-api", "model": API,
                                               "prompt": "new"}))["content"][0]["text"])
        moved = json.loads((await tool.handler({"automation_id": "auto-api", "model": API_OPUS})
                            )["content"][0]["text"])
        switched = json.loads((await tool.handler({"automation_id": "auto-sub", "model": API})
                               )["content"][0]["text"])
    assert kept["success"] is True
    assert moved["success"] is False and "Claude API" in moved["error"]
    assert switched["success"] is False and "Claude API" in switched["error"]
    assert database.get_automation("auto-sub")["model"] == "sonnet"


@pytest.mark.asyncio
async def test_update_profile_cannot_pin_a_claude_api_subagent_model():
    tools = _create_profile_tools(is_admin=True)
    original = {"id": "p1", "name": "P1", "description": None, "is_builtin": False,
                "config": {"provider_models": {"__native__": "sonnet"}}, "mcp_tools": []}
    with patch("app.db.database.get_profile", return_value=original), \
            patch("app.db.database.update_profile") as write:
        result = await get_tool(tools, "updateProfile").handler(
            {"profile_id": "p1", "provider_models": {"__native__": API}})
    payload = tool_result_json(result)
    assert payload["success"] is False and "Claude API" in payload["error"]
    write.assert_not_called()


@pytest.mark.asyncio
async def test_create_profile_cannot_pin_a_claude_api_subagent_model():
    tools = _create_profile_tools(is_admin=True)
    with patch("app.db.database.get_profile", return_value=None), \
            patch("app.db.database.create_profile") as write:
        result = await get_tool(tools, "createProfile").handler(
            {"name": "Paid", "provider_models": {"__native__": API}})
    payload = tool_result_json(result)
    assert payload["success"] is False and "Claude API" in payload["error"]
    write.assert_not_called()


# ─── subagents (round 5) ──────────────────────────────────────────────────

def _subagent_tool(name):
    from app.mcp.config_subagents import _create_subagent_tools

    return get_tool(_create_subagent_tools(is_admin=True), name)


def _subagent_row(provider_models):
    return {"id": "rev", "name": "Rev", "description": "d", "prompt": "p", "tools": None,
            "model": None, "provider_models": provider_models, "skills": None,
            "memory": None, "mcp_servers": None, "is_builtin": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("slot", ["__native__", "__voice__"])
async def test_create_subagent_cannot_pin_a_claude_api_model(slot):
    with patch("app.db.database.get_subagent", return_value=None), \
            patch("app.db.database.create_subagent") as write:
        result = await _subagent_tool("createSubagent").handler(
            {"name": "Rev", "description": "d", "prompt": "p", "provider_models": {slot: API}})
    payload = tool_result_json(result)
    assert payload["success"] is False and "Claude API" in payload["error"]
    write.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("args", [
    {"provider_models": {"__native__": API}},
    {"add": {"provider_models": {"__native__": API}}},
], ids=["replace", "map-add"])
async def test_update_subagent_cannot_newly_pin_a_claude_api_model(args):
    with patch("app.db.database.get_subagent",
               return_value=_subagent_row({"__native__": "sonnet"})), \
            patch("app.db.database.update_subagent") as write:
        result = await _subagent_tool("updateSubagent").handler({"subagent_id": "rev", **args})
    payload = tool_result_json(result)
    assert payload["success"] is False and "Claude API" in payload["error"]
    write.assert_not_called()


@pytest.mark.asyncio
async def test_update_subagent_keeps_the_owners_saved_api_pin():
    row = _subagent_row({"__native__": API})
    with patch("app.db.database.get_subagent", return_value=row), \
            patch("app.db.database.update_subagent", return_value={**row, "prompt": "new"}) as write:
        kept = tool_result_json(await _subagent_tool("updateSubagent").handler(
            {"subagent_id": "rev", "prompt": "new", "provider_models": {"__native__": API}}))
        moved = tool_result_json(await _subagent_tool("updateSubagent").handler(
            {"subagent_id": "rev", "provider_models": {"__native__": API_OPUS}}))
    assert kept["success"] is True
    assert moved["success"] is False and "Claude API" in moved["error"]
    assert write.call_count == 1
