"""Revocation must fence external side effects, including already-running connectors."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core import automation_service
from app.core.connectors.manager import ConnectorManager
from app.network import ownership


def test_automation_ownership_error_fails_closed(monkeypatch):
    def broken(_row):
        raise RuntimeError("database unavailable")
    monkeypatch.setattr(ownership, "decide_automation", broken)
    assert automation_service.should_run_scheduled_here({"id": "job"}) == (False, "network ownership unavailable")


@pytest.mark.asyncio
async def test_manual_connector_start_respects_network_election(monkeypatch):
    from app.db import database
    monkeypatch.setattr(database, "get_connector", lambda _id: {"id": "c", "type": "telegram", "enabled": True})
    monkeypatch.setattr(ownership, "decide_connector", lambda _row: "skip")
    monkeypatch.setattr(ownership, "enrolled", lambda: True)
    from app.core.connectors import manager as manager_module
    implementation = MagicMock(side_effect=AssertionError("must not instantiate a revoked connector"))
    monkeypatch.setattr(manager_module, "get_connector_class", implementation)
    manager = ConnectorManager()
    assert await manager.start_connector("c") is False
    implementation.assert_not_called()


@pytest.mark.asyncio
async def test_running_connector_does_not_ack_or_route_after_revocation(monkeypatch):
    from app.core.connectors.base import IncomingMessage
    from app.db import database
    monkeypatch.setattr(database, "get_connector", lambda _id: {"id": "c"})
    monkeypatch.setattr(ownership, "enrolled", lambda: True)
    monkeypatch.setattr(ownership, "owns", lambda _row: False)
    manager = ConnectorManager()
    connector = MagicMock()
    connector.send_message = AsyncMock()
    connector.send_typing = AsyncMock()
    connector.send_ack_reaction = AsyncMock()
    connector.mark_as_read = AsyncMock()
    routing = MagicMock(side_effect=AssertionError("must not route a revoked connector"))
    monkeypatch.setattr(database, "get_connector_session", routing)
    manager._connectors["c"] = connector
    manager._route_message = AsyncMock()
    await manager.handle_incoming_message(IncomingMessage(
        connector_id="c", platform_chat_id="chat", platform_user_id="user", platform_user_name="User", text="do work", platform_message_id="message", metadata={},
    ))
    connector.send_ack_reaction.assert_not_awaited()
    connector.mark_as_read.assert_not_awaited()
    routing.assert_not_called()
    connector.send_message.assert_not_awaited()
    connector.send_typing.assert_not_awaited()
    manager._route_message.assert_not_awaited()
