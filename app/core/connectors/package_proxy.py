"""Host-side connector SDK adapter for isolated first-party worker processes."""
import asyncio
import dataclasses
import json
import os
import uuid
from pathlib import Path

from app.core.connectors.base import BaseConnector, ConnectorStatus, IncomingMessage


def worker_environment():
    # Do not inherit database URLs, provider keys, or other connectors' secrets.
    return {k: v for k, v in os.environ.items() if k.upper() in {
        "PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "LANG", "LC_ALL"}}


def interpreter(path):
    return path / "env" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


class PackageConnector(BaseConnector):
    PACKAGE_PATH: Path
    PACKAGE_SPEC: dict

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._process = None
        self._reader = None
        self._pending = {}
        self._callbacks = set()
        self._write_lock = asyncio.Lock()
        self._instructions = None

    async def _send(self, payload):
        data = (json.dumps(payload, default=lambda o: dataclasses.asdict(o) if dataclasses.is_dataclass(o) else str(o)) + "\n").encode()
        if len(data) > 2_000_000:
            raise ValueError("Connector request too large")
        async with self._write_lock:
            self._process.stdin.write(data)
            await self._process.stdin.drain()

    async def _call(self, method, *args, **kwargs):
        key = str(uuid.uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending[key] = future
        try:
            await self._send({"id": key, "method": method, "args": args, "kwargs": kwargs})
            return await asyncio.wait_for(future, 180)
        finally:
            self._pending.pop(key, None)

    async def _callback(self, value):
        try:
            method, args = value["method"], value["args"]
            if method == "incoming":
                message = IncomingMessage(**args[0])
                if message.connector_id != self.connector_id:
                    raise ValueError("Wrong connection identity")
                result = await self._on_incoming_message(message) if self._on_incoming_message else None
            elif method == "trigger":
                if args[0] != self.connector_id:
                    raise ValueError("Wrong connection identity")
                result = await self._on_trigger(*args) if self._on_trigger else None
            elif method == "permission":
                result = await self._on_permission_response(*args) if self._on_permission_response else None
            elif method == "status":
                from app.db import database
                from app.db.async_database import run_sync
                positional, kwargs = args
                if positional[0] != self.connector_id:
                    raise ValueError("Wrong connection identity")
                result = await run_sync(database.update_connector_status, *positional, **kwargs)
            else:
                raise ValueError("Unknown callback")
            response = {"host_id": value["host_id"], "result": result}
        except Exception:
            response = {"host_id": value["host_id"], "error": "Host callback failed"}
        await self._send(response)

    async def _read(self):
        try:
            while line := await self._process.stdout.readline():
                value = json.loads(line)
                if "host_id" in value:
                    task = asyncio.create_task(self._callback(value))
                    self._callbacks.add(task)
                    task.add_done_callback(self._callbacks.discard)
                    continue
                future = self._pending.get(value.get("id"))
                if future is None or future.done():
                    continue
                if value.get("error"):
                    future.set_exception(RuntimeError(value["error"]))
                else:
                    self._status = ConnectorStatus(value.get("status", "disconnected"))
                    self._instructions = value.get("instructions")
                    future.set_result(value.get("result"))
        finally:
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Connector worker stopped"))
            if self._status != ConnectorStatus.DISCONNECTED:
                self._status = ConnectorStatus.ERROR
                self._status_message = "Connector worker stopped. Restart this connection."
                from app.db import database
                from app.db.async_database import run_sync
                try:
                    await run_sync(database.update_connector_status, self.connector_id, "error", self._status_message)
                except Exception:
                    pass  # Shutdown may have closed the database already.

    async def _do_connect(self):
        from app.core.connector_packages import _verify
        _verify(self.PACKAGE_PATH, self.connector_type, self.PACKAGE_SPEC)
        spec = self.PACKAGE_SPEC
        self._process = await asyncio.create_subprocess_exec(
            str(interpreter(self.PACKAGE_PATH)), "-I", str(Path(__file__).with_name("package_worker.py")),
            str(self.PACKAGE_PATH), spec["module"], spec["class"],
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=worker_environment(), limit=2_000_000)
        self._reader = asyncio.create_task(self._read())
        try:
            await self._call("init", connector_id=self.connector_id, connector_type=self.connector_type, config=self.config)
            if not await self._call("connect"):
                raise RuntimeError("Connector could not connect. Check its account settings.")
            if self.connector_type != "discord":
                from app.db import database
                from app.db.async_database import run_sync
                await run_sync(database.update_connector_status, self.connector_id, "connected")
        except BaseException:
            await self.disconnect()
            raise

    async def disconnect(self):
        process = self._process
        if process and process.returncode is None:
            try:
                await asyncio.wait_for(self._call("disconnect"), 5)
            except Exception:
                pass
            process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        if self._reader:
            await asyncio.gather(self._reader, return_exceptions=True)
        for task in self._callbacks:
            task.cancel()
        if self._callbacks:
            await asyncio.gather(*self._callbacks, return_exceptions=True)
        self._status = ConnectorStatus.DISCONNECTED

    def get_context_instructions(self, platform_chat_id):
        return self._instructions

    def verify_inbound_request(self, *args, **kwargs):
        # The host verifies HTTP authentication before forwarding any payload.
        from app.core.connectors.webhook_connector import WebhookConnector
        if self.connector_type != "webhook":
            return False
        return WebhookConnector.verify_inbound_request(self, *args, **kwargs)

    async def send_message(self, message):
        return await self._call("send_message", message)

    async def send_permission_prompt(self, platform_chat_id, prompt):
        return await self._call("send_permission_prompt", platform_chat_id, prompt)

    async def send_notification(self, platform_chat_id, event_type, data):
        return await self._call("send_notification", platform_chat_id, event_type, data)


def proxy_class(kind, path, spec):
    from app.core.connectors.registry import _CONNECTOR_CLASSES
    bundled = _CONNECTOR_CLASSES[kind]
    attrs = {key: getattr(bundled, key) for key in ("CAPABILITIES", "MAX_MESSAGE_LENGTH", "TRIGGERS", "ACTIONS", "TOOLS", "SUPPORTS_ACTION_GATEWAY")}
    attrs.update(PACKAGE_PATH=path, PACKAGE_SPEC=spec, __module__=__name__)
    for method in ("send_typing_indicator", "send_ack_reaction", "mark_as_read", "execute_action", "execute_tool",
                   "get_action_availability", "request_action_approval", "handle_webhook_payload", "handle_webhook_activity"):
        def forward(name):
            async def call(self, *args, **kwargs):
                return await self._call(name, *args, **kwargs)
            return call
        attrs[method] = forward(method)
    return type(f"{kind.title()}PluginConnector", (PackageConnector,), attrs)
