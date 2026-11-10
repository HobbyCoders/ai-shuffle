"""A command in an agent's reply reports its result to the same chat without freezing it (#1400).

Home Assistant runs the agent's reply as a command inside ``send_message``,
while the manager still holds the chat for the turn that produced the reply.
The result comes back as a new message for that chat, so it has to queue
behind the current turn instead of waiting for it.
"""

import asyncio
import sqlite3
from collections import defaultdict
from typing import Any, Callable

import httpx
import pytest

from app.core import turn_manager
from app.core.connectors.base import IncomingMessage, chunk_text
from app.core.connectors.homeassistant import HomeAssistantConnector
from app.core.connectors.manager import ConnectorManager
from app.core.turn_recorder import TurnRecorder
from app.db import database, turns

CHAT = "homeassistant"


async def _until(check: Callable[[], bool]) -> None:
    while not check():
        await asyncio.sleep(0.01)


def _user_message(text: str, mid: str) -> IncomingMessage:
    return IncomingMessage(connector_id="connector", platform_chat_id=CHAT,
                           platform_user_id="owner", platform_user_name="Owner",
                           text=text, platform_message_id=mid)


async def test_command_reply_reaches_the_chat_and_the_chat_keeps_answering(
    mock_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch,
) -> None:
    database.create_api_user("connector-user", "Connector user", "test-hash", profile_ids=["default"])
    database.create_connector("connector", "homeassistant", "Home", {}, [], api_user_id="connector-user")
    manager = ConnectorManager()
    states = [{"entity_id": f"light.lamp_{i}", "state": "on",
               "attributes": {"friendly_name": f"Lamp {i}"}} for i in range(6)]

    def ha_api(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/states"
        return httpx.Response(200, json=states)

    connector = HomeAssistantConnector(
        "connector", "homeassistant", {"url": "http://ha.test", "access_token": "token"},
        manager.handle_incoming_message, manager.handle_permission_response,
        on_trigger=manager._handle_trigger)
    connector._http_client = httpx.AsyncClient(transport=httpx.MockTransport(ha_api))
    connector.MAX_MESSAGE_LENGTH = 80  # a long result arrives as several messages
    manager._connectors["connector"] = connector
    listing = "Entities (6):\n" + "\n".join(connector._format_entity_line(s) for s in states)
    chunks = chunk_text(listing, connector.MAX_MESSAGE_LENGTH)
    assert len(chunks) > 1

    # The agent answers "hello" with the command "status"; everything else is plain text.
    answers = {"hello": "status", "thanks": "you are welcome"}
    prompts: list[str] = []
    sent: list[str] = []
    workers: list[asyncio.Task[Any]] = []
    per_session: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
    real_send = connector.send_message

    async def send(message: Any) -> bool:
        sent.append(message.text)
        return await real_send(message)

    async def drain(sid: str) -> None:
        async with per_session[sid]:
            for row in await asyncio.to_thread(turns.get_queued_turns, sid):
                recorder = await TurnRecorder.maybe_create(sid)
                assert recorder is not None
                recorder.turn_id = row["id"]
                try:
                    assert await asyncio.to_thread(turns.start_turn, row["id"], recorder.run_id)
                    await recorder.start(row["prompt"])
                    prompts.append(row["prompt"])
                    await recorder.on_event({"type": "stream_delta", "delta_type": "text",
                                             "content": answers.get(row["prompt"], "noted")})
                    await recorder.on_event({"type": "done", "metadata": {}})
                    await asyncio.to_thread(turns.settle_turn, row["id"], "completed")
                finally:
                    await recorder.close()

    monkeypatch.setattr(connector, "send_message", send)
    monkeypatch.setattr(turn_manager, "_ensure_gateway_listener", lambda: None)
    turn_manager.register_kick(lambda sid: workers.append(asyncio.create_task(drain(sid))))
    try:
        # The turn whose reply is a command finishes instead of waiting on its own chat.
        await asyncio.wait_for(manager.handle_incoming_message(_user_message("hello", "1")), 5)
        # Every part of the result reaches the chat, in the order it was produced.
        await asyncio.wait_for(_until(lambda: len(prompts) == 1 + len(chunks)), 5)
        assert prompts == ["hello", *chunks]
        # The chat still answers its owner afterwards.
        await asyncio.wait_for(manager.handle_incoming_message(_user_message("thanks", "2")), 5)
        assert prompts[-1] == "thanks"
        assert sent[0] == "status" and sent[-1] == "you are welcome"
        await asyncio.wait_for(asyncio.gather(*workers), 5)
        assert not connector._reply_tasks
        assert not manager._message_locks
    finally:
        turn_manager.register_kick(None)
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await connector.disconnect()


async def test_disconnect_cancels_a_queued_command_result() -> None:
    """A result still waiting for its chat is cancelled, not leaked, by ``disconnect``."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def incoming(message: IncomingMessage) -> None:
        started.set()
        await release.wait()

    async def permission(*args: Any) -> None:
        return None

    connector = HomeAssistantConnector("connector", "homeassistant",
                                       {"url": "http://ha.test", "access_token": "token"},
                                       incoming, permission)
    connector._http_client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=[])))
    from app.core.connectors.base import OutgoingMessage
    assert await asyncio.wait_for(connector.send_message(OutgoingMessage(platform_chat_id=CHAT, text="status")), 1)
    await asyncio.wait_for(started.wait(), 1)
    tasks = tuple(connector._reply_tasks)
    assert tasks
    await asyncio.wait_for(connector.disconnect(), 1)
    assert all(task.cancelled() for task in tasks)
    assert not connector._reply_tasks
