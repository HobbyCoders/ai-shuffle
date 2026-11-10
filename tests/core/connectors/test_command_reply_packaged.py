"""A command reply through an isolated connector worker reaches the chat in order (#1400).

The same flow as ``test_connector_command_reply`` but packaged: the real
Home Assistant connector runs in a real worker subprocess behind the real
``package_proxy``, with only its HTTP calls faked inside the worker.
"""

import asyncio
import shutil
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

import pytest

from app.core import turn_manager
from app.core.connectors import package_proxy
from app.core.connectors.base import IncomingMessage, chunk_text
from app.core.connectors.homeassistant import HomeAssistantConnector
from app.core.connectors.manager import ConnectorManager
from app.core.turn_recorder import TurnRecorder
from app.db import database, turns

CHAT = "homeassistant"
MAX_LENGTH = 80

FAKE_HA = '''
import httpx
from app.core.connectors.homeassistant import HomeAssistantConnector

STATES = [{"entity_id": f"light.lamp_{i}", "state": "on",
           "attributes": {"friendly_name": f"Lamp {i}"}} for i in range(6)]


class FakeHomeAssistant(HomeAssistantConnector):
    MAX_MESSAGE_LENGTH = %d  # a long result arrives as several messages

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._http_client = httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=STATES)))
''' % MAX_LENGTH


async def _until(check: Callable[[], bool]) -> None:
    while not check():
        await asyncio.sleep(0.01)


def _user_message(text: str, mid: str) -> IncomingMessage:
    return IncomingMessage(connector_id="connector", platform_chat_id=CHAT,
                           platform_user_id="owner", platform_user_name="Owner",
                           text=text, platform_message_id=mid)


async def test_packaged_command_reply_reaches_the_chat_and_the_chat_keeps_answering(
    mock_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    database.create_api_user("connector-user", "Connector user", "test-hash", profile_ids=["default"])
    database.create_connector("connector", "homeassistant", "Home", {}, [], api_user_id="connector-user")
    connectors = Path(package_proxy.__file__).parent
    package = tmp_path / "package"
    package.mkdir()
    for name in ("homeassistant.py", "homeassistant_actions.py"):
        shutil.copy(connectors / name, package / name)
    (package / "fake_ha.py").write_text(FAKE_HA, encoding="utf-8")

    manager = ConnectorManager()
    cls = package_proxy.proxy_class("homeassistant", package, {"module": "fake_ha", "class": "FakeHomeAssistant"})
    config = {"url": "http://ha.test", "access_token": "token"}
    proxy = cls("connector", "homeassistant", config, manager.handle_incoming_message,
                manager.handle_permission_response, on_trigger=manager._handle_trigger)
    manager._connectors["connector"] = proxy

    # The listing the worker will produce, built with the host's copy of the same code.
    host = HomeAssistantConnector("x", "homeassistant", config, manager.handle_incoming_message,
                                  manager.handle_permission_response)
    states = [{"entity_id": f"light.lamp_{i}", "state": "on",
               "attributes": {"friendly_name": f"Lamp {i}"}} for i in range(6)]
    listing = "Entities (6):\n" + "\n".join(host._format_entity_line(s) for s in states)
    chunks = chunk_text(listing, MAX_LENGTH)
    assert len(chunks) > 1

    answers = {"hello": "status", "thanks": "you are welcome"}
    prompts: list[str] = []
    workers: list[asyncio.Task[Any]] = []
    per_session: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

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

    monkeypatch.setattr(turn_manager, "_ensure_gateway_listener", lambda: None)
    turn_manager.register_kick(lambda sid: workers.append(asyncio.create_task(drain(sid))))
    proxy._process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(connectors / "package_worker.py"), str(package),
        "fake_ha", "FakeHomeAssistant", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=package_proxy.worker_environment(), limit=2_000_000,
    )
    proxy._reader = asyncio.create_task(proxy._read())
    try:
        await proxy._call("init", connector_id="connector", connector_type="homeassistant", config=config)
        # The turn whose reply is a command finishes instead of waiting on its own chat.
        await asyncio.wait_for(manager.handle_incoming_message(_user_message("hello", "1")), 10)
        # Every part of the result comes back over the pipe and reaches the chat in order.
        await asyncio.wait_for(_until(lambda: len(prompts) == 1 + len(chunks)), 10)
        assert prompts == ["hello", *chunks]
        # The chat still answers its owner afterwards.
        await asyncio.wait_for(manager.handle_incoming_message(_user_message("thanks", "2")), 10)
        assert prompts[-1] == "thanks"
        await asyncio.wait_for(asyncio.gather(*workers), 10)
        assert not manager._message_locks
    finally:
        turn_manager.register_kick(None)
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        await proxy.disconnect()
