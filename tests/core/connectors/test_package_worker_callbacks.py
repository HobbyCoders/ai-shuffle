"""A packaged connector waits for a long turn's host callback without an error or a thread (#1388)."""

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

FAKE_CONNECTOR = '''
import asyncio, sys, threading
from app.core.connectors.base import BaseConnector, IncomingMessage

worker = sys.modules["__main__"]

class ShortEvent(threading.Event):
    # Stands in for the 180-second limit: any wait over half a second gives up.
    def wait(self, timeout=None):
        return super().wait(0.5 if timeout is None or timeout > 0.5 else timeout)

worker.threading.Event = ShortEvent
worker.HOST_CALL_TIMEOUT = 0.5


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
        results = await asyncio.gather(*(self._on_incoming_message(IncomingMessage(
            connector_id=self.connector_id, platform_chat_id=f"chat-{i}", platform_user_id="u",
            platform_user_name="u", text=str(i))) for i in range(payload["count"])), return_exceptions=True)
        # The default executor is still free while every turn waits.
        probe = await asyncio.wait_for(asyncio.to_thread(lambda: "free"), 5)
        return [repr(r) if isinstance(r, BaseException) else r for r in results] + [probe]
'''


async def test_incoming_callback_outlasts_the_short_limit_without_holding_threads(tmp_path: Path) -> None:
    from app.core.connectors import package_proxy
    connectors = Path(package_proxy.__file__).parent
    package = tmp_path / "package"
    package.mkdir()
    (package / "fake_connector.py").write_text(FAKE_CONNECTOR, encoding="utf-8")
    count = 40  # more than any default executor (at most 32 threads)
    arrived = asyncio.Event()
    seen: list[str] = []

    async def incoming(message):
        seen.append(message.text)
        if len(seen) == count:
            arrived.set()
        await arrived.wait()
        await asyncio.sleep(1.5)  # the turn runs past the (shortened) limit
        return f"done {message.text}"

    cls = package_proxy.proxy_class("webhook", package, {"module": "fake_connector", "class": "FakeConnector"})
    proxy = cls("fake-id", "webhook", {}, incoming, AsyncMock())
    proxy._process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(connectors / "package_worker.py"), str(package),
        "fake_connector", "FakeConnector", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        env=package_proxy.worker_environment(), limit=2_000_000,
    )
    proxy._reader = asyncio.create_task(proxy._read())
    try:
        await proxy._call("init", connector_id="fake-id", connector_type="webhook", config={})
        results = await asyncio.wait_for(proxy._call("handle_webhook_payload", {"count": count}), 30)
        assert results == [f"done {i}" for i in range(count)] + ["free"]
    finally:
        await proxy.disconnect()
