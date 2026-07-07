"""
Tests for ConnectorManager — lifecycle, public accessors, encryption helpers.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.connectors.manager import ConnectorManager
from app.core.connectors.base import BaseConnector, ConnectorStatus


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def manager():
    """Fresh ConnectorManager instance for each test."""
    return ConnectorManager()


class FakeConnector(BaseConnector):
    """Minimal concrete connector for testing."""

    def __init__(self, connector_id="fake-1", connector_type="webhook", **kwargs):
        config = kwargs.pop("config", {})
        on_incoming = kwargs.pop("on_incoming_message", AsyncMock())
        on_perm = kwargs.pop("on_permission_response", AsyncMock())
        super().__init__(connector_id, connector_type, config, on_incoming, on_perm)
        self._connected = True

    async def connect(self) -> bool:
        self._status = ConnectorStatus.CONNECTED
        return self._connected

    async def disconnect(self) -> None:
        self._status = ConnectorStatus.DISCONNECTED

    async def send_message(self, message) -> bool:
        return True

    async def send_permission_prompt(self, platform_chat_id, prompt) -> bool:
        return True

    async def send_notification(self, platform_chat_id, event_type, data) -> bool:
        return True

    async def test_connection(self):
        return {"success": True, "message": "OK"}


# =============================================================================
# Public Accessors
# =============================================================================


class TestPublicAccessors:
    @pytest.mark.asyncio
    async def test_get_running_connector_found(self, manager):
        fake = FakeConnector(connector_id="test-1")
        async with manager._lock:
            manager._connectors["test-1"] = fake

        result = await manager.get_running_connector("test-1")
        assert result is fake

    @pytest.mark.asyncio
    async def test_get_running_connector_not_found(self, manager):
        result = await manager.get_running_connector("nonexistent")
        assert result is None

    def test_get_running_connectors_all(self, manager):
        fake1 = FakeConnector(connector_id="c1", connector_type="webhook")
        fake2 = FakeConnector(connector_id="c2", connector_type="telegram")
        manager._connectors = {"c1": fake1, "c2": fake2}

        result = list(manager.get_running_connectors())
        assert len(result) == 2

    def test_get_running_connectors_by_type(self, manager):
        fake1 = FakeConnector(connector_id="c1", connector_type="webhook")
        fake2 = FakeConnector(connector_id="c2", connector_type="telegram")
        manager._connectors = {"c1": fake1, "c2": fake2}

        result = list(manager.get_running_connectors("webhook"))
        assert len(result) == 1
        assert result[0][0] == "c1"

    def test_get_running_connectors_empty(self, manager):
        result = list(manager.get_running_connectors())
        assert result == []


# =============================================================================
# Status Helpers
# =============================================================================


class TestStatusHelpers:
    def test_get_connector_status(self, manager):
        fake = FakeConnector(connector_id="test-1")
        fake._status = ConnectorStatus.CONNECTED
        manager._connectors["test-1"] = fake

        status = manager.get_connector_status("test-1")
        assert status is not None
        assert status["connector_id"] == "test-1"
        assert status["status"] == "connected"

    def test_get_connector_status_not_running(self, manager):
        status = manager.get_connector_status("nonexistent")
        assert status is None


# =============================================================================
# Start / Stop Connector
# =============================================================================


class TestStartStopConnector:
    @pytest.mark.asyncio
    async def test_start_connector_not_found(self, manager, mock_db):
        result = await manager.start_connector("nonexistent")
        assert result is False

    @pytest.mark.asyncio
    async def test_stop_connector_updates_db(self, manager, mock_db):
        fake = FakeConnector(connector_id="test-1")
        manager._connectors["test-1"] = fake

        await manager.stop_connector("test-1")
        assert "test-1" not in manager._connectors

    @pytest.mark.asyncio
    async def test_stop_connector_not_running(self, manager, mock_db):
        # Should not raise
        await manager.stop_connector("nonexistent")

    @pytest.mark.asyncio
    async def test_restart_connector(self, manager, mock_db, mock_encryption):
        from app.db import database

        # Create connector in DB
        database.create_connector(
            connector_id="test-1",
            connector_type="webhook",
            name="Test Webhook",
            config={"mode": "inbound", "inbound_path": "test"},
            events=[],
            api_user_id=None,
        )

        fake = FakeConnector(connector_id="test-1")
        manager._connectors["test-1"] = fake

        with patch.object(manager, "start_connector", new_callable=AsyncMock, return_value=True):
            result = await manager.restart_connector("test-1")
            assert result is True


# =============================================================================
# Decrypt Config
# =============================================================================


class TestDecryptConfig:
    def test_decrypt_encrypted_field(self, mock_encryption):
        with patch("app.core.encryption.is_encrypted", return_value=True):
            config = {"bot_token": "encrypted:secret123"}
            result = ConnectorManager._decrypt_config(config, "telegram")
            assert result["bot_token"] == "secret123"

    def test_decrypt_plaintext_unchanged(self, mock_encryption):
        with patch("app.core.encryption.is_encrypted", return_value=False):
            config = {"bot_token": "plaintext"}
            result = ConnectorManager._decrypt_config(config, "telegram")
            assert result["bot_token"] == "plaintext"

    def test_decrypt_failure_sets_none(self, mock_encryption):
        with patch("app.core.encryption.is_encrypted", return_value=True):
            with patch("app.core.encryption.decrypt_value", side_effect=Exception("decrypt failed")):
                config = {"bot_token": "encrypted:bad"}
                result = ConnectorManager._decrypt_config(config, "telegram")
                assert result["bot_token"] is None

    def test_decrypt_no_sensitive_fields(self, mock_encryption):
        config = {"some_key": "value"}
        result = ConnectorManager._decrypt_config(config, "nonexistent")
        assert result == config

    def test_decrypt_encryption_not_ready(self, mock_encryption):
        with patch("app.core.encryption.is_encrypted", return_value=True):
            with patch("app.core.encryption.is_encryption_ready", return_value=False):
                config = {"bot_token": "encrypted:secret"}
                result = ConnectorManager._decrypt_config(config, "telegram")
                assert result["bot_token"] is None


# =============================================================================
# Encrypt Config
# =============================================================================


class TestEncryptConfig:
    def test_encrypt_plaintext_field(self, mock_encryption):
        config = {"bot_token": "my-token"}
        result = ConnectorManager._encrypt_config(config, "telegram")
        assert result["bot_token"] == "encrypted:my-token"

    def test_encrypt_already_encrypted_skipped(self, mock_encryption):
        with patch("app.core.encryption.is_encrypted", return_value=True):
            config = {"bot_token": "encrypted:already"}
            result = ConnectorManager._encrypt_config(config, "telegram")
            assert result["bot_token"] == "encrypted:already"


# =============================================================================
# Shutdown
# =============================================================================


class TestShutdown:
    @pytest.mark.asyncio
    async def test_shutdown_all(self, manager, mock_db):
        fake = FakeConnector(connector_id="test-1")
        manager._connectors["test-1"] = fake

        await manager.shutdown_all()
        assert len(manager._connectors) == 0


# =============================================================================
# Permission Response Forwarding
# =============================================================================


class TestPermissionResponse:
    @pytest.mark.asyncio
    async def test_approved_forwards_allow(self, manager):
        """A connector approval calls permission_handler.respond with decision=allow."""
        with patch(
            "app.core.permission_handler.permission_handler.respond",
            new=AsyncMock(return_value={"resolved": True}),
        ) as mock_respond:
            await manager.handle_permission_response("req-1", "sess-1", True)

        mock_respond.assert_awaited_once()
        kwargs = mock_respond.await_args.kwargs
        assert kwargs["request_id"] == "req-1"
        assert kwargs["session_id"] == "sess-1"
        assert kwargs["decision"] == "allow"
        assert kwargs["broadcast_func"] is None

    @pytest.mark.asyncio
    async def test_denied_forwards_deny(self, manager):
        with patch(
            "app.core.permission_handler.permission_handler.respond",
            new=AsyncMock(return_value={"resolved": True}),
        ) as mock_respond:
            await manager.handle_permission_response("req-2", "sess-2", False)

        assert mock_respond.await_args.kwargs["decision"] == "deny"

    @pytest.mark.asyncio
    async def test_missing_ids_are_ignored(self, manager):
        """No respond() call when request_id or session_id is missing."""
        with patch(
            "app.core.permission_handler.permission_handler.respond",
            new=AsyncMock(),
        ) as mock_respond:
            await manager.handle_permission_response("", "sess-3", True)
            await manager.handle_permission_response("req-3", "", True)

        mock_respond.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_resolves_real_pending_request(self, manager):
        """End-to-end: a queued request unblocks when the connector approves."""
        from app.core.permission_handler import permission_handler, PermissionResultAllow

        permission_handler.reset_all()

        async def _broadcast(_msg):
            return None

        request_task = asyncio.create_task(
            permission_handler.request_permission(
                request_id="req-e2e",
                session_id="sess-e2e",
                profile_id="prof-e2e",
                tool_name="Bash",
                tool_input={"command": "ls"},
                broadcast_func=_broadcast,
            )
        )
        # Let the request register in the pending queue
        await asyncio.sleep(0.05)

        await manager.handle_permission_response("req-e2e", "sess-e2e", True)

        result = await asyncio.wait_for(request_task, timeout=2.0)
        assert isinstance(result, PermissionResultAllow)
        permission_handler.reset_all()
