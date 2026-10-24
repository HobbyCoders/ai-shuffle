"""Concurrent platform ingress through real routing, turn rows and recorder output."""

import asyncio
import sqlite3
from collections import defaultdict
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from app.core import turn_manager
from app.core.connectors.manager import ConnectorManager
from app.core.turn_recorder import TurnRecorder
from app.db import database, turns


@pytest.mark.parametrize("platform", ["telegram", "discord", "slack"])
@pytest.mark.parametrize("fresh", [False, True], ids=["linked-chat", "first-messages"])
async def test_overlapping_messages_keep_one_session_and_fifo_replies(
    mock_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, platform: str, fresh: bool,
) -> None:
    database.create_api_user("connector-user", "Connector user", "test-hash",
                             profile_ids=["default"])
    database.create_connector("connector", platform, "Test connector", {}, [],
                              api_user_id="connector-user")
    if not fresh:
        database.create_session(session_id="existing", profile_id="default")
        database.create_connector_session("mapping", "connector", "123", ai_session_id="existing")

    manager = ConnectorManager()
    if platform == "telegram":
        from app.core.connectors.telegram import TelegramConnector
        cls = TelegramConnector
    elif platform == "discord":
        from app.core.connectors.discord_connector import DiscordConnector
        cls = DiscordConnector
    else:
        from app.core.connectors.slack import SlackConnector
        cls = SlackConnector
    connector = cls("connector", platform, {}, manager.handle_incoming_message,
                    manager.handle_permission_response, on_trigger=manager._handle_trigger)
    manager._connectors["connector"] = connector
    sent = []
    first_ack = asyncio.Event()
    release_ack = asyncio.Event()
    running = asyncio.Event()
    release_turn = asyncio.Event()
    workers: list[asyncio.Task[Any]] = []
    per_session: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def ack(chat: str, message: str) -> bool:
        if message == "1":
            first_ack.set()
            await release_ack.wait()
        return True

    async def send(message: Any) -> bool:
        sent.append((message.reply_to_message_id, message.text))
        return True

    async def drain(sid: str) -> None:
        async with per_session[sid]:
            for row in await asyncio.to_thread(turns.get_queued_turns, sid):
                recorder = await TurnRecorder.maybe_create(sid)
                assert recorder is not None
                recorder.turn_id = row["id"]
                try:
                    started = await asyncio.to_thread(turns.start_turn, row["id"], recorder.run_id)
                    assert started is not None
                    await recorder.start(row["prompt"])
                    if row["prompt"] == "A":
                        running.set()
                        await release_turn.wait()
                    await recorder.on_event({"type": "stream_delta", "delta_type": "text",
                                             "content": "reply " + row["prompt"]})
                    await recorder.on_event({"type": "done", "metadata": {}})
                    await asyncio.to_thread(turns.settle_turn, row["id"], "completed")
                finally:
                    await recorder.close()

    def kick(sid: str) -> None:
        workers.append(asyncio.create_task(drain(sid)))

    monkeypatch.setattr(connector, "send_ack_reaction", ack)
    monkeypatch.setattr(connector, "mark_as_read", AsyncMock(return_value=True))
    monkeypatch.setattr(connector, "send_typing_indicator", AsyncMock(return_value=True))
    monkeypatch.setattr(connector, "send_message", send)
    monkeypatch.setattr(turn_manager, "_ensure_gateway_listener", lambda: None)
    turn_manager.register_kick(kick)

    async def dispatch(text: str, mid: str) -> None:
        if platform == "telegram":
            await connector._handle_message(SimpleNamespace(
                chat=SimpleNamespace(id=123, type="private"), text=text, message_id=int(mid),
                from_user=SimpleNamespace(id=456, full_name="Sender"),
            ))
        elif platform == "discord":
            await connector._on_message_handler(SimpleNamespace(
                channel=SimpleNamespace(id=123, name="test"), content=text, id=int(mid),
                author=SimpleNamespace(id=456, bot=False), guild=None,
            ))
        else:
            await connector._handle_message_event({"channel": "123", "user": "456",
                                                   "text": text, "ts": mid})

    tasks = [asyncio.create_task(dispatch("A", "1"))]
    try:
        await asyncio.wait_for(first_ack.wait(), 5)
        if not fresh:
            release_ack.set()
            await asyncio.wait_for(running.wait(), 5)
        tasks.append(asyncio.create_task(dispatch("B", "2")))
        # Fresh: hold A after routing but before minting, exposing a stale
        # unlinked mapping to B. Linked: hold A's live turn while B queues.
        await asyncio.sleep(0.1)
        release_ack.set()
        await asyncio.wait_for(running.wait(), 5)
        if platform == "telegram":
            approval = asyncio.get_running_loop().create_future()
            connector._pending_action_approvals["request"] = approval
            await asyncio.wait_for(connector._handle_callback_query(SimpleNamespace(
                data="actreq:request:approve",
                message=SimpleNamespace(chat=SimpleNamespace(id=123)),
                answer=AsyncMock(),
            )), 1)
            assert approval.result() is True
        release_turn.set()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        if platform == "telegram":
            await asyncio.wait_for(asyncio.gather(*connector._chat_tasks), 10)
        await asyncio.gather(*workers)
        with database.get_db() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) AS n FROM sessions")
            assert cursor.fetchone()["n"] == 1
        linked = database.get_connector_session("connector", "123")
        assert linked and linked["session_id"]
        assert sent == [("1", "reply A"), ("2", "reply B")]
        assert not manager._message_locks
    finally:
        release_ack.set()
        release_turn.set()
        turn_manager.register_kick(None)
        pending = tasks + workers + list(getattr(connector, "_chat_tasks", ()))
        for task in pending:
            if not task.done():
                task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
