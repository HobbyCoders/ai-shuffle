"""Webhook signing secrets never reach the agent through app-config tools.

The stored trigger shape is ``{"type", "config": {"secret", ...}}``; the
sanitizer used to mask only ``trigger_config.secret`` and a flat
``triggers[i].secret``, so ``getAutomation`` returned the nested one as-is.
"""

import copy
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.core.automation_manager import automation_manager
from app.mcp.config_automations import _create_automation_tools
from app.mcp.config_helpers import _sanitize_automation

MASK = "••••••••"


def _get_tool(name):
    for t in _create_automation_tools(is_admin=True, project_id=None):
        if getattr(t, "name", None) == name:
            return t
    raise AssertionError(f"tool {name!r} not found")


def _payload(result: dict) -> dict:
    return json.loads(result["content"][0]["text"])


class TestSanitizeAutomation:
    def _auto(self):
        return {
            "id": "auto-1",
            "name": "hook",
            "trigger_type": "webhook",
            "trigger_config": {"secret": "legacy-secret", "note": "keep"},
            "triggers": [
                {"type": "webhook", "config": {"secret": "nested-secret", "path": "/x"}},
                {"type": "cron", "config": {"expression": "0 9 * * *", "timezone": "UTC"}},
                {"type": "webhook", "secret": "flat-secret"},
            ],
        }

    def test_masks_nested_trigger_config_secret(self):
        safe = _sanitize_automation(self._auto())
        assert safe["triggers"][0]["config"]["secret"] == MASK
        assert "nested-secret" not in json.dumps(safe)

    def test_still_masks_trigger_config_and_flat_trigger_secret(self):
        safe = _sanitize_automation(self._auto())
        assert safe["trigger_config"]["secret"] == MASK
        assert safe["triggers"][2]["secret"] == MASK

    def test_non_secret_fields_untouched(self):
        safe = _sanitize_automation(self._auto())
        assert safe["trigger_config"]["note"] == "keep"
        assert safe["triggers"][0]["config"]["path"] == "/x"
        assert safe["triggers"][0]["type"] == "webhook"
        assert safe["triggers"][1] == {
            "type": "cron",
            "config": {"expression": "0 9 * * *", "timezone": "UTC"},
        }
        assert safe["name"] == "hook"

    def test_input_not_mutated(self):
        auto = self._auto()
        before = copy.deepcopy(auto)
        _sanitize_automation(auto)
        assert auto == before

    def test_tolerates_odd_trigger_shapes(self):
        auto = {"triggers": [None, "cron", {"type": "webhook", "config": None}]}
        assert _sanitize_automation(auto)["triggers"] == auto["triggers"]


class TestGetAutomationTool:
    @pytest.mark.asyncio
    async def test_webhook_secret_not_returned(self, mock_db):
        from app.db import database

        database.create_automation(
            automation_id="auto-wh",
            name="Webhook",
            trigger_type="webhook",
            trigger_config={"secret": "live-signing-secret"},
            profile_id="default",
            prompt="hi",
        )
        payload = _payload(await _get_tool("getAutomation").handler({"automation_id": "auto-wh"}))
        assert payload["triggers"][0]["config"]["secret"] == MASK
        assert payload["trigger_config"]["secret"] == MASK
        assert "live-signing-secret" not in json.dumps(payload)


class TestCreateAutomationTool:
    @pytest.mark.asyncio
    async def test_webhook_secret_stored_encrypted(self, mock_db, _api_unit_credentials):
        from app.core import encryption
        from app.db import database

        with patch.object(automation_manager, "schedule_automation", new=AsyncMock()):
            result = await _get_tool("createAutomation").handler({
                "name": "mcp-webhook",
                "trigger_type": "webhook",
                "prompt": "hi",
                "profile_id": "default",
            })
        payload = _payload(result)
        assert payload["success"] is True
        auto = database.get_automation(payload["automation_id"])
        assert encryption.is_encrypted(auto["trigger_config"]["secret"])
        assert encryption.is_encrypted(auto["triggers"][0]["config"]["secret"])

    @pytest.mark.asyncio
    async def test_webhook_refused_without_credential_storage(self, mock_db):
        from app.db import database

        result = await _get_tool("createAutomation").handler({
            "name": "mcp-webhook-locked",
            "trigger_type": "webhook",
            "prompt": "hi",
            "profile_id": "default",
        })
        payload = _payload(result)
        assert payload["success"] is False
        assert "credential storage" in payload["error"]
        assert not [a for a in database.get_all_automations() if a.get("name") == "mcp-webhook-locked"]
