"""A packaged webhook connector's host request waits out a long turn (#1388).

``handle_webhook_payload`` / ``handle_webhook_activity`` return only after the
agent's turn for the delivered message settles, so the host must not give up
on them after the ordinary 180-second limit. A worker that dies still ends
the wait with the existing "Connector worker stopped" error.
"""

import asyncio
import logging
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

FAKE_CONNECTOR = '''
import asyncio
from app.core.connectors.base import BaseConnector, IncomingMessage


class FakeConnector(BaseConnector):
    async def disconnect(self):
        pass

    async def send_message(self, message):
        return True

    async def send_permission_prompt(self, platform_chat_id, prompt):
        return True

    async def send_notification(self, platform_chat_id, event_type, data):
        return True

    async def handle_webhook_payload(self, payload):
        return await self._on_incoming_message(IncomingMessage(
            connector_id=self.connector_id, platform_chat_id="chat", platform_user_id="u",
            platform_user_name="u", text=payload["text"]))

    async def handle_webhook_activity(self, activity):
        return await self.handle_webhook_payload(activity)

    async def execute_action(self, *args, **kwargs):
        await asyncio.sleep(1.5)
        return "late"
'''

SHORT = 0.5


def _short_asyncio() -> types.SimpleNamespace:
    """``asyncio`` for package_proxy where the 180-second limit lasts half a second."""
    real_wait_for = asyncio.wait_for

    def wait_for(awaitable, timeout):
        if timeout is not None and timeout <= 180:
            timeout = min(timeout, SHORT)
        return real_wait_for(awaitable, timeout)

    shim = types.SimpleNamespace(**{name: getattr(asyncio, name) for name in dir(asyncio) if not name.startswith("__")})
    shim.wait_for = wait_for
    return shim


async def _start(tmp_path: Path, incoming):
    from app.core.connectors import package_proxy
    connectors = Path(package_proxy.__file__).parent
    package = tmp_path / "package"
    package.mkdir()
    (package / "fake_connector.py").write_text(FAKE_CONNECTOR, encoding="utf-8")
    cls = package_proxy.proxy_class("webhook", package, {"module": "fake_connector", "class": "FakeConnector"})
    proxy = cls("fake-id", "webhook", {}, incoming, AsyncMock())
    proxy._process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(connectors / "package_worker.py"), str(package),
        "fake_connector", "FakeConnector", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=package_proxy.worker_environment(), limit=2_000_000,
    )
    proxy._reader = asyncio.create_task(proxy._read())
    await proxy._call("init", connector_id="fake-id", connector_type="webhook", config={})
    return proxy


@pytest.mark.parametrize("method", ["handle_webhook_payload", "handle_webhook_activity"])
async def test_webhook_request_outlasts_the_short_limit(tmp_path, monkeypatch, caplog, method) -> None:
    from app.core.connectors import package_proxy
    monkeypatch.setattr(package_proxy, "asyncio", _short_asyncio())

    async def incoming(message):
        await asyncio.sleep(SHORT * 3)  # the turn runs past the (shortened) limit
        return f"done {message.text}"

    proxy = await _start(tmp_path, incoming)
    try:
        with caplog.at_level(logging.WARNING):
            result = await asyncio.wait_for(getattr(proxy, method)({"text": "hi"}), 30)
        assert result == "done hi"
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
        # Every other host-to-worker request keeps the ordinary limit.
        with pytest.raises(asyncio.TimeoutError):
            await proxy.execute_action("slow")
    finally:
        await proxy.disconnect()


@pytest.mark.parametrize("ending", ["killed", "stopped"])
async def test_worker_end_fails_a_waiting_webhook_request(tmp_path, ending) -> None:
    started = asyncio.Event()

    async def incoming(message):
        started.set()
        await asyncio.sleep(3600)

    proxy = await _start(tmp_path, incoming)
    try:
        request = asyncio.create_task(proxy.handle_webhook_payload({"text": "hi"}))
        await asyncio.wait_for(started.wait(), 10)
        if ending == "killed":
            proxy._process.kill()
        else:
            asyncio.create_task(proxy.disconnect())
        # Killed: the host's reader fails it. Stopped: the worker fails its own
        # callback as the pipe closes and answers with its ordinary error.
        expected = "Connector worker stopped" if ending == "killed" else "Connector operation failed"
        with pytest.raises(RuntimeError, match=expected):
            await asyncio.wait_for(request, 20)
    finally:
        await proxy.disconnect()
