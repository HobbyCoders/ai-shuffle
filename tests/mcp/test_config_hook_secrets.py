"""App-config hook tools share the write-only signing-secret contract."""
import json

import pytest

from app.db import database
from app.mcp.config_helpers import _sanitize_hook
from app.mcp.config_hooks import _create_hook_tools


def tools():
    return {tool.name: tool for tool in _create_hook_tools(is_admin=True, project_id=None)}


async def call(name, args):
    result = await tools()[name].handler(args)
    return json.loads(result["content"][0]["text"])


@pytest.mark.parametrize("secret", [None, "", "dummy-hook-secret"])
def test_sanitizer_exposes_presence_without_mutating_row(secret):
    row = {"id": "hook-test", "secret": secret}
    safe = _sanitize_hook(row)
    assert safe == {"id": "hook-test", "has_secret": bool(secret)}
    assert row["secret"] == secret


@pytest.mark.asyncio
async def test_tool_create_list_get_update_preserve_and_clear(mock_db):
    created = await call("createHook", {"name": "Test", "event": "Stop", "hook_type": "webhook", "url": "https://example.test"})
    assert created["has_secret"] is False
    hid = created["hook_id"]
    # Replacement uses the actual update tool; list/get read its persisted value.
    for args in [{"secret": "dummy-hook-secret"}, {"secret": ""}, {"secret": " \t"}, {"secret": None, "name": "Renamed"}]:
        updated = await call("updateHook", {"hook_id": hid, **args})
        assert updated["success"] is True
        assert updated["has_secret"] is True
        assert "secret" not in updated
        assert database.get_hook(hid)["secret"] == "dummy-hook-secret"
    listed = await call("listHooks", {})
    got = await call("getHook", {"hook_id": hid})
    for payload in [listed["hooks"][0], got]:
        assert payload["has_secret"] is True
        assert "secret" not in payload
    conflict = await call("updateHook", {"hook_id": hid, "clear_secret": True, "secret": "dummy-replacement"})
    assert conflict["success"] is False
    assert database.get_hook(hid)["secret"] == "dummy-hook-secret"
    cleared = await call("updateHook", {"hook_id": hid, "clear_secret": True})
    assert cleared["has_secret"] is False
    assert database.get_hook(hid)["secret"] is None
    assert (await call("getHook", {"hook_id": hid}))["has_secret"] is False


def test_shared_update_refuses_conflicting_secret_operations(mock_db):
    database.create_hook("hook-test", "Test", "Stop", "webhook", secret="dummy-hook-secret")
    with pytest.raises(ValueError, match="Cannot replace and clear"):
        database.update_hook("hook-test", secret="dummy-replacement", clear_secret=True)
    assert database.get_hook("hook-test")["secret"] == "dummy-hook-secret"
