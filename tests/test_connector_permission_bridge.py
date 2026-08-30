"""
Tests for the connector permission funnel (app/core/connector_permissions.py)
and the permission_handler hook that drives it.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.connector_permissions import deliver_permission_prompt
from app.core.connectors.base import PermissionPrompt


def _fake_instance(sent=True):
    inst = MagicMock()
    inst.send_permission_prompt = AsyncMock(return_value=sent)
    return inst


# =============================================================================
# deliver_permission_prompt
# =============================================================================


class TestDeliverPermissionPrompt:
    @pytest.mark.asyncio
    async def test_delivers_to_running_connector(self):
        inst = _fake_instance()
        cs = [{"connector_id": "c1", "platform_chat_id": "chan-9"}]

        with patch(
            "app.core.connectors.manager.connector_manager"
        ) as mgr, patch(
            "app.db.database.get_connector_sessions_for_ai_session",
            return_value=cs,
        ):
            mgr.get_running_instance.return_value = inst
            await deliver_permission_prompt(
                session_id="sess-1",
                request_id="req-1",
                tool_name="Bash",
                tool_input={"command": "ls"},
                description="run ls",
            )

        mgr.get_running_instance.assert_called_once_with("c1")
        inst.send_permission_prompt.assert_awaited_once()
        args = inst.send_permission_prompt.await_args.args
        assert args[0] == "chan-9"
        prompt = args[1]
        assert isinstance(prompt, PermissionPrompt)
        assert prompt.request_id == "req-1"
        assert prompt.session_id == "sess-1"
        assert prompt.tool_name == "Bash"
        assert prompt.tool_input == {"command": "ls"}

    @pytest.mark.asyncio
    async def test_skips_connector_not_running(self):
        cs = [{"connector_id": "c1", "platform_chat_id": "chan-9"}]
        with patch(
            "app.core.connectors.manager.connector_manager"
        ) as mgr, patch(
            "app.db.database.get_connector_sessions_for_ai_session",
            return_value=cs,
        ):
            mgr.get_running_instance.return_value = None
            # Must not raise.
            await deliver_permission_prompt(
                "sess-1", "req-1", "Bash", {}, "d"
            )
        mgr.get_running_instance.assert_called_once_with("c1")

    @pytest.mark.asyncio
    async def test_no_connector_sessions_is_noop(self):
        with patch(
            "app.db.database.get_connector_sessions_for_ai_session",
            return_value=[],
        ):
            await deliver_permission_prompt(
                "sess-1", "req-1", "Bash", {}, "d"
            )

    @pytest.mark.asyncio
    async def test_lookup_failure_is_swallowed(self):
        with patch(
            "app.db.database.get_connector_sessions_for_ai_session",
            side_effect=RuntimeError("db down"),
        ):
            await deliver_permission_prompt(
                "sess-1", "req-1", "Bash", {}, "d"
            )

    @pytest.mark.asyncio
    async def test_platform_send_failure_does_not_raise(self):
        inst = _fake_instance(sent=False)
        cs = [{"connector_id": "c1", "platform_chat_id": "chan-9"}]
        with patch(
            "app.core.connectors.manager.connector_manager"
        ) as mgr, patch(
            "app.db.database.get_connector_sessions_for_ai_session",
            return_value=cs,
        ):
            mgr.get_running_instance.return_value = inst
            await deliver_permission_prompt(
                "sess-1", "req-1", "Bash", {}, "d"
            )
        inst.send_permission_prompt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_one_connectors_failure_does_not_stop_the_other(self):
        bad = _fake_instance()
        bad.send_permission_prompt = AsyncMock(side_effect=RuntimeError("boom"))
        good = _fake_instance()
        cs = [
            {"connector_id": "c1", "platform_chat_id": "chan-1"},
            {"connector_id": "c2", "platform_chat_id": "chan-2"},
        ]
        with patch(
            "app.core.connectors.manager.connector_manager"
        ) as mgr, patch(
            "app.db.database.get_connector_sessions_for_ai_session",
            return_value=cs,
        ):
            mgr.get_running_instance.side_effect = lambda cid: (
                bad if cid == "c1" else good
            )
            await deliver_permission_prompt(
                "sess-1", "req-1", "Bash", {}, "d"
            )
        good.send_permission_prompt.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rows_missing_ids_are_skipped(self):
        inst = _fake_instance()
        cs = [
            {"connector_id": None, "platform_chat_id": "chan-1"},
            {"connector_id": "c2", "platform_chat_id": None},
        ]
        with patch(
            "app.core.connectors.manager.connector_manager"
        ) as mgr, patch(
            "app.db.database.get_connector_sessions_for_ai_session",
            return_value=cs,
        ):
            mgr.get_running_instance.return_value = inst
            await deliver_permission_prompt(
                "sess-1", "req-1", "Bash", {}, "d"
            )
        inst.send_permission_prompt.assert_not_awaited()
        mgr.get_running_instance.assert_not_called()
