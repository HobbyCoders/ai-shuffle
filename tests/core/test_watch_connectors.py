"""Fake connector producing paths: approval, idle polling, replay, revocation and controls."""
from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pytest import MonkeyPatch

from app.core import connector_service, connector_packages, watches, watch_sources
from app.core import watch_manager as manager
from app.core.connectors.manager import connector_manager
from app.db import database, watches as store, turns
from tests.core.test_watches import owner  # noqa: F401


class FakeSlack:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []
        self.calls: list[dict[str, Any]] = []

    async def conversations_history(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        messages = [m for m in self.messages if m["ts"] > kwargs["oldest"]]
        offset = int(kwargs.get("cursor", "0"))
        batch = list(reversed(messages))[offset:offset + 100]
        more = offset + 100 < len(messages)
        return {"messages": batch, "has_more": more, "response_metadata": {
            "next_cursor": str(offset + 100) if more else ""
        }}


class FakeChannel:
    def __init__(self) -> None:
        self.messages: list[Any] = []
        self.guild = SimpleNamespace(id=42)

    async def history(self, **kwargs: Any) -> AsyncIterator[Any]:
        assert kwargs["oldest_first"]
        for message in [m for m in self.messages if m.id > kwargs["after"].id][:kwargs["limit"]]:
            yield message


@pytest.fixture(params=["slack", "discord", "telegram"])
def messaging(
    owner: dict[str, Any], monkeypatch: MonkeyPatch, request: pytest.FixtureRequest
) -> tuple[dict[str, Any], Any, Any]:
    from app.core.connectors.slack import SlackConnector
    from app.core.connectors.discord_connector import DiscordConnector
    from app.core.connectors.telegram import TelegramConnector
    from app.core.automation_manager import automation_manager

    kind = request.param
    monkeypatch.setattr(connector_packages, "require_installed", lambda _: "fake-runtime")
    connector = asyncio.run(connector_service.create_connector("Messages", kind, {}, []))
    database.update_connector(connector["id"], is_active=True)
    classes = {"slack": SlackConnector, "discord": DiscordConnector, "telegram": TelegramConnector}
    cls = classes[kind]
    instance = cls(
        connector["id"], kind, {"allowed_channel_ids": ["123"], "allowed_chat_ids": ["123"]},
        AsyncMock(), AsyncMock(), on_trigger=connector_manager._handle_trigger,
    )
    sdk: Any = FakeSlack() if kind == "slack" else FakeChannel()
    if kind == "slack":
        instance._web_client = sdk
    elif kind == "discord":
        instance._client = SimpleNamespace(get_channel=lambda _: sdk)
        monkeypatch.setitem(sys.modules, "discord", SimpleNamespace(Object=SimpleNamespace))
    monkeypatch.setattr(
        connector_manager, "get_running_connector", AsyncMock(return_value=instance)
    )
    monkeypatch.setattr(automation_manager, "handle_connector_trigger", AsyncMock())
    return connector, instance, sdk


async def incoming(instance: Any, sdk: Any, cursor: dict[str, Any], text: str) -> None:
    kind = instance.connector_type
    if kind == "slack":
        sdk.messages.append({"ts": str(float(cursor["after"]) + 1), "text": text, "user": "u"})
    elif kind == "discord":
        sdk.messages.append(SimpleNamespace(
            id=int(cursor["after"]) + 1, content=text, author=SimpleNamespace(id=7)
        ))
    else:
        await instance._handle_message(SimpleNamespace(
            chat=SimpleNamespace(id=123, type="group"), from_user=SimpleNamespace(
                id=7, full_name="Test member"
            ), text=text, message_id=1,
        ))


@pytest.mark.asyncio
async def test_confirm_poll_restart_and_controls(
    owner: dict[str, Any], messaging: tuple[dict[str, Any], Any, Any], monkeypatch: MonkeyPatch
) -> None:
    from app.core.permission_handler import permission_handler
    from app.core.agent_types import PermissionResultAllow

    connector, instance, sdk = messaging
    approve = AsyncMock(return_value=PermissionResultAllow())
    monkeypatch.setattr(permission_handler, "request_permission", approve)
    summaries = AsyncMock()
    monkeypatch.setattr(manager, "summarize", summaries)
    result = await watches.create_from_conversation(owner["id"], {
        "description": "important messages", "filter": {"contains": "important"},
        "source": {"kind": "connector", "connector_id": connector["id"], "channel_id": "123"},
    })
    watch = result["watch"]
    assert watch["cursor"]
    assert approve.await_count == 1
    await manager.WatchManager().run_once()
    summaries.assert_not_awaited()
    assert store.events(watch["id"]) == []
    await incoming(instance, sdk, watch["cursor"], "important new message")
    await manager.WatchManager().run_once()
    event = store.events(watch["id"])[0]
    assert event["payload"]["text"] == "important new message"
    assert turns.get_turn_by_command(owner["id"], event["id"])
    await manager.WatchManager().run_once()
    assert len(store.events(watch["id"])) == 1
    assert store.get(watch["id"])["cursor"] != watch["cursor"]
    summaries.assert_not_awaited()
    store.set_status(watch["id"], "admin", "paused")
    frozen = store.get(watch["id"])["cursor"]
    await manager.WatchManager().run_once()
    assert store.get(watch["id"])["cursor"] == frozen
    store.set_status(watch["id"], "admin", "stopped")
    store.set_status(watch["id"], "admin", "active")
    assert store.get(watch["id"])["status"] == "stopped"
    from app.db.base import get_db

    assert connector["api_user_id"]
    with get_db() as conn:
        conn.cursor().execute(
            "UPDATE api_users SET is_active=FALSE WHERE id=%s", (connector["api_user_id"],)
        )
    with pytest.raises(PermissionError):
        await watch_sources.poll(store.get(watch["id"]))


@pytest.mark.asyncio
async def test_channel_scope_and_unmatched_cursor(
    owner: dict[str, Any], messaging: tuple[dict[str, Any], Any, Any]
) -> None:
    connector, instance, sdk = messaging
    row = watches.prepare(owner["id"], {
        "description": "messages", "source": {
            "kind": "connector", "connector_id": connector["id"], "channel_id": "123"
        }, "filter": {"contains": "important"},
    })
    _, cursor = await watch_sources.poll(dict(row, cursor={}))
    await incoming(instance, sdk, cursor, "ordinary message")
    wid = store.create(**row, cursor=cursor)
    await manager.WatchManager().run_once()
    assert store.events(wid) == []
    assert store.get(wid)["cursor"] != cursor
    instance.config["allowed_channel_ids"] = ["456"]
    instance.config["allowed_chat_ids"] = ["456"]
    with pytest.raises(PermissionError, match="no longer allowed"):
        await watch_sources.poll(store.get(wid))


@pytest.mark.asyncio
async def test_slack_page_boundary_preserves_high_watermark() -> None:
    from app.core.watch_connector_sources import poll

    sdk = FakeSlack()
    sdk.messages = [{"ts": f"{n:05}", "text": "message"} for n in range(1, 206)]
    instance = SimpleNamespace(config={}, _web_client=sdk)
    cursor = {"after": "00000"}
    keys: list[str] = []
    for _ in range(3):
        events, cursor = await poll("slack", instance, {"channel_id": "123"}, cursor)
        keys.extend(key for key, _ in events)
    assert len(set(keys)) == 205
    assert cursor == {"after": "00205"}
    events, _ = await poll("slack", instance, {"channel_id": "123"}, cursor)
    assert events == []
