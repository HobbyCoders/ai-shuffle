"""createHook / updateHook refuse message hooks, which are never delivered,
and no longer offer "message" as a hook type."""

from unittest.mock import MagicMock, patch

from app.core.models import MESSAGE_HOOKS_NOT_CREATABLE
from app.mcp.config_hooks import _create_hook_tools
from tests.mcp.tool_helpers import get_tool, tool_result_json


def _tools():
    return _create_hook_tools(is_admin=True, project_id=None)


def test_message_is_not_an_offered_hook_type():
    for name in ("createHook", "updateHook"):
        schema = get_tool(_tools(), name).input_schema
        assert "message" not in schema["properties"]["hook_type"]["enum"]


async def test_create_hook_refuses_a_message_hook():
    create = MagicMock()
    with patch("app.db.database.create_hook", create):
        body = tool_result_json(await get_tool(_tools(), "createHook").handler({
            "name": "m", "event": "Stop", "hook_type": "message", "message_template": "hi",
        }))
    assert body == {"success": False, "error": MESSAGE_HOOKS_NOT_CREATABLE}
    create.assert_not_called()


async def test_update_hook_refuses_turning_a_hook_into_a_message_hook():
    update = MagicMock()
    with patch("app.db.database.get_hook", MagicMock(return_value={"id": "h"})), \
         patch("app.db.database.update_hook", update):
        body = tool_result_json(await get_tool(_tools(), "updateHook").handler({
            "hook_id": "h", "hook_type": "message",
        }))
    assert body == {"success": False, "error": MESSAGE_HOOKS_NOT_CREATABLE}
    update.assert_not_called()
