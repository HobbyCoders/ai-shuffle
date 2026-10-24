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
    from app.core.connectors.watch_history import poll

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


@pytest.mark.asyncio
@pytest.mark.parametrize("messaging", ["telegram"], indirect=True)
async def test_telegram_durable_ingress_retries_before_offset_advance(
    messaging: tuple[dict[str, Any], Any, Any], monkeypatch: MonkeyPatch
) -> None:
    connector, instance, sdk = messaging
    persist = store.record_connector_event
    attempts = 0
    acknowledged = False

    def record(connector_id: str, key: str, payload: dict[str, Any]) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("temporary database failure")
        persist(connector_id, key, payload)
        if attempts == 2:
            raise RuntimeError("commit succeeded but acknowledgement was lost")

    monkeypatch.setattr(store, "record_connector_event", record)

    async def start_polling(bot: Any, *, handle_as_tasks: bool) -> None:
        nonlocal acknowledged
        assert handle_as_tasks is False
        await incoming(instance, sdk, {}, "important")
        # aiogram's next getUpdates offset is only advanced after this await.
        assert attempts == 3
        acknowledged = True

    instance._dp = SimpleNamespace(start_polling=start_polling)
    await instance._run_polling()
    assert acknowledged
    events, cursor = store.connector_page(connector["id"], "123", {"seq": 0})
    assert len(events) == 1
    assert events[0][1]["text"] == "important"
    assert store.connector_page(connector["id"], "123", cursor) == ([], cursor)
    await asyncio.gather(*instance._chat_tasks)
    instance._on_incoming_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("messaging", ["telegram"], indirect=True)
@pytest.mark.parametrize("decision", ["approve", "deny"])
async def test_telegram_approval_and_other_chat_dispatch_while_turn_is_running(
    messaging: tuple[dict[str, Any], Any, Any], monkeypatch: MonkeyPatch, decision: str
) -> None:
    from app.core.connectors import telegram
    from app.core.connectors.base import ActionDef, IncomingMessage

    _, instance, sdk = messaging
    instance.config["allowed_chat_ids"] = ["123", "456"]
    monkeypatch.setattr(telegram, "InlineKeyboardMarkup", SimpleNamespace, raising=False)
    monkeypatch.setattr(telegram, "InlineKeyboardButton", SimpleNamespace, raising=False)
    prompted = asyncio.Event()
    answered = asyncio.Event()
    other_chat = asyncio.Event()
    finish_turn = asyncio.Event()
    outcomes: list[bool] = []
    buttons: list[Any] = []

    async def send_message(**kwargs: Any) -> None:
        buttons.extend(kwargs["reply_markup"].inline_keyboard[0])
        prompted.set()

    async def turn(message: IncomingMessage) -> None:
        if message.platform_chat_id == "456":
            other_chat.set()
            return
        action = ActionDef("remove", "Remove", "Destructive test action", risk="destructive")
        approved, _ = await instance.request_action_approval("123", action, {}, timeout=2)
        outcomes.append(approved)
        answered.set()
        await finish_turn.wait()  # Host still waits for this turn to go idle.

    instance._on_incoming_message = turn
    instance._bot = SimpleNamespace(send_message=send_message, session=SimpleNamespace(
        close=AsyncMock()
    ))

    async def start_polling(bot: Any, *, handle_as_tasks: bool) -> None:
        assert handle_as_tasks is False
        await incoming(instance, sdk, {}, "request a destructive action")
        await asyncio.wait_for(prompted.wait(), timeout=2)
        assert len(instance._chat_tasks) == 1
        button = buttons[0 if decision == "approve" else 1]
        callback = SimpleNamespace(
            data=button.callback_data, message=SimpleNamespace(chat=SimpleNamespace(id=123)),
            answer=AsyncMock(),
        )
        await instance._handle_callback_query(callback)
        callback.answer.assert_awaited_once()
        await asyncio.wait_for(answered.wait(), timeout=2)
        assert outcomes == [decision == "approve"]
        assert len(instance._chat_tasks) == 1  # Approval did not end the requesting turn.
        await instance._handle_message(SimpleNamespace(
            chat=SimpleNamespace(id=456, type="group"), from_user=None,
            text="another chat", message_id=2,
        ))
        await asyncio.wait_for(other_chat.wait(), timeout=2)
        assert not finish_turn.is_set()

    instance._dp = SimpleNamespace(start_polling=start_polling, stop_polling=AsyncMock())
    try:
        await instance._run_polling()
        assert outcomes == [decision == "approve"] and other_chat.is_set()
        finish_turn.set()
        await asyncio.gather(*instance._chat_tasks)
    finally:
        await instance.disconnect()


@pytest.mark.asyncio
@pytest.mark.parametrize("messaging", ["telegram"], indirect=True)
async def test_telegram_chat_dispatch_is_bounded_and_cancelled_on_stop(
    messaging: tuple[dict[str, Any], Any, Any], monkeypatch: MonkeyPatch
) -> None:
    _, instance, sdk = messaging
    monkeypatch.setattr(instance, "MAX_CHAT_TASKS", 1)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def turn(message: Any) -> None:
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    instance._on_incoming_message = turn
    instance.send_message = AsyncMock(return_value=True)
    await incoming(instance, sdk, {}, "first")
    await asyncio.wait_for(started.wait(), timeout=2)
    await incoming(instance, sdk, {}, "capacity reached")
    assert len(instance._chat_tasks) == 1
    instance.send_message.assert_awaited_once()
    assert "Too many chats" in instance.send_message.call_args.args[0].text
    await instance.disconnect()
    assert cancelled.is_set() and not instance._chat_tasks
    await incoming(instance, sdk, {}, "after stop")
    assert not instance._chat_tasks


@pytest.mark.asyncio
async def test_old_telegram_package_fails_explicitly() -> None:
    from app.core.watch_connector_sources import poll

    instance = SimpleNamespace(config={}, PACKAGE_SPEC={"files": {"telegram.py": "old"}})
    with pytest.raises(ValueError, match="Update the Telegram connector package"):
        await poll("telegram", instance, {"channel_id": "123"}, {})
