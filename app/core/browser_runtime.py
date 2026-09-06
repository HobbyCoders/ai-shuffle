"""App-owned browser MCP actors, shared by panel viewers and agent proxies.

The MCP server owns tabs and control. This module only transports MCP messages;
it never connects to CDP or replays a failed browser command.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

from fluid.tools.mcp.client import McpClient

from app.core import browser_control
from app.db.async_database import run_sync

logger = logging.getLogger(__name__)

IDLE_SECONDS = 900
READY_SECONDS = 45
CALL_SECONDS = 40
MAX_CALLS = 32
TOOL_NAMES = frozenset({
    "browser_status", "browser_navigate", "browser_snapshot", "browser_act",
    "browser_screenshot", "browser_eval", "browser_find", "browser_read_text",
    "browser_console", "browser_network", "browser_resize", "browser_tabs",
    "browser_upload", "browser_batch", "browser_wait",
})


class RuntimeUnavailable(RuntimeError):
    pass


class RawMcpClient(McpClient):
    """Keep the vendored client's actor/transport lifetime, not its outcome mapper."""

    async def _do_call(self, session, tool, input, **kwargs):
        async with asyncio.timeout(self.call_timeout):
            result = await session.call_tool(tool, input)
            return result.model_dump(mode="json", by_alias=True, exclude_none=True)


def resolve_config(session_id):
    """Resolve the same profile env and project MCP scopes as query options."""
    from app.core.config import settings
    from app.core.environment import _build_env_with_ai_tools, apply_provider_env
    from app.core.mcp_config import _build_mcp_servers_dict
    from app.core.profiles import get_profile
    from app.core.project_resolver import resolve_cwd, resolve_project_v2
    from app.db import database

    row = database.get_session(session_id)
    if not row:
        raise RuntimeUnavailable("Session unavailable")
    profile = get_profile(row.get("profile_id"))
    if not profile:
        raise RuntimeUnavailable("Session profile unavailable")
    config = profile.get("config", {})
    cwd = resolve_cwd(row)
    if cwd is None and resolve_project_v2(row):
        raise RuntimeUnavailable("Session project is not checked out on this device")
    cwd = cwd or config.get("cwd") or str(settings.workspace_dir)
    provider = row.get("provider_id")
    env = _build_env_with_ai_tools(
        config.get("env"), config.get("ai_tools"), row.get("api_user_id"), provider
    )
    if provider:
        apply_provider_env(
            env, provider, row.get("model") or config.get("model"), session_id=session_id
        )
    servers = _build_mcp_servers_dict(Path(cwd), env, session_id) or {}
    server = copy.deepcopy(servers.get("browser"))
    if not server or not server.get("command"):
        raise RuntimeUnavailable("Browser runtime unavailable")
    server.setdefault("env", {})["AIS_BROWSER_MODE"] = "runtime"
    server.update(
        cwd=cwd, connect_timeout=15, call_timeout=CALL_SECONDS, shutdown_timeout=3
    )
    return server


class Runtime:
    def __init__(self, session_id):
        self.session_id = session_id
        self.client = None
        self.ready = None
        self.calls = set()
        self.active = 0
        self.touched = time.monotonic()
        self.closed = False
        self.close_lock = asyncio.Lock()
        self.runtime_id = None
        self.idle = asyncio.create_task(self._idle())

    async def _start(self):
        async with asyncio.timeout(READY_SECONDS + 15):
            await self._connect()

    async def _connect(self):
        broker = browser_control.get(self.session_id)
        if broker.runtime_id is not None:
            raise RuntimeUnavailable("An existing browser runtime already owns this session")
        try:
            config = await run_sync(resolve_config, self.session_id)
            from app.core.native_chrome import ensure_started
            await ensure_started()
            if self.closed:
                raise RuntimeUnavailable("Browser runtime closed")
            # Correlate this child before its bridge can register, even if MCP
            # initialization never returns. Never adopt an identity from status.
            self.runtime_id = str(uuid4())
            config.setdefault("env", {})["_AIS_BROWSER_RUNTIME_ID"] = self.runtime_id
            self.client = RawMcpClient("browser", config)
            await self.client.start()
            status = await self.client.call_tool("browser_status", {})
            reported_id = None
            for item in status.get("content", []):
                if item.get("type") == "text":
                    reported_id = browser_control.identifier(
                        json.loads(item["text"])["runtime_id"]
                    )
                    break
            if status.get("isError") or reported_id != self.runtime_id:
                raise RuntimeUnavailable("Browser runtime did not report its expected identity")
            # list ensures one initial page, unlike create (which adds a second).
            result = await self.client.call_tool("browser_tabs", {"op": "list"})
            if result.get("isError"):
                raise RuntimeUnavailable("Browser could not open its initial tab")
            async with asyncio.timeout(READY_SECONDS):
                while not (
                    broker.runtime_id == self.runtime_id
                    and broker.state.get("available")
                    and broker.state.get("target_id")
                ):
                    await asyncio.sleep(0.05)
        except BaseException:
            await self._dispose()
            raise

    async def ensure(self):
        if self.closed:
            raise RuntimeUnavailable("Browser runtime closed")
        self.touched = time.monotonic()
        if self.ready is None:
            self.ready = asyncio.create_task(self._start())
        await asyncio.shield(self.ready)
        broker = browser_control.get(self.session_id)
        if broker.runtime_id != self.runtime_id or not broker.state.get("available"):
            raise RuntimeUnavailable("Browser runtime expired")
        actor = getattr(self.client, "_task", None)
        if actor is not None and actor.done():
            raise RuntimeUnavailable("Browser runtime disconnected")
        return copy.deepcopy(broker.state)

    async def call(self, tool, arguments):
        if self.active >= MAX_CALLS:
            raise RuntimeUnavailable("Browser runtime busy")
        self.active += 1
        try:
            await self.ensure()
            return await self.client.call_tool(tool, arguments)
        except Exception:
            # Unknown outcomes are never replayed. The next request may restart.
            await self.close()
            raise RuntimeUnavailable("Browser runtime disconnected; outcome unknown") from None
        finally:
            self.active -= 1
            self.touched = time.monotonic()

    async def _dispose(self):
        if self.client:
            await self.client.aclose()
            self.client = None
            broker = browser_control.get(self.session_id)
            if self.runtime_id is not None and broker.runtime_id == self.runtime_id:
                await broker.retire()

    async def close(self):
        async with self.close_lock:
            self.closed = True
            if self.idle is not asyncio.current_task():
                self.idle.cancel()
            if (
                self.ready and not self.ready.done()
                and self.ready is not asyncio.current_task()
            ):
                self.ready.cancel()
                with suppress(asyncio.CancelledError, Exception):
                    await self.ready
            await self._dispose()
            if _runtimes.get(self.session_id) is self:
                _runtimes.pop(self.session_id, None)
        # The actor has stopped, so outstanding HTTP jobs cannot mutate again.
        # Retire them too; proxy EOF alone never takes this path.
        pending = [
            job for job in self.calls
            if job is not asyncio.current_task() and not job.done()
        ]
        for job in pending:
            job.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _idle(self):
        try:
            while not self.closed:
                await asyncio.sleep(min(IDLE_SECONDS, 30))
                broker = browser_control.get(self.session_id)
                if (
                    not self.active and not broker.viewers
                    and time.monotonic() - self.touched >= IDLE_SECONDS
                ):
                    await self.close()
        except asyncio.CancelledError:
            pass


_runtimes = {}


def get(session_id):
    runtime = _runtimes.get(session_id)
    if runtime is None:
        runtime = _runtimes[session_id] = Runtime(session_id)
    return runtime


async def start(session_id):
    runtime = get(session_id)
    warm = runtime.ready is not None and runtime.ready.done()
    try:
        async with asyncio.timeout(READY_SECONDS + 20):
            state = await runtime.ensure()
            if warm:
                await runtime.call("browser_status", {})
            return state
    except Exception as exc:
        logger.warning("Browser runtime startup failed (%s)", type(exc).__name__)
        await runtime.close()
        raise RuntimeUnavailable("Browser runtime could not start") from None


async def call(session_id, tool, arguments):
    if tool not in TOOL_NAMES or not isinstance(arguments, dict):
        raise ValueError("Invalid browser tool call")
    browser_control.finite(arguments)
    runtime = get(session_id)
    # Request cancellation must not cancel an admitted mutation. Bound and retain
    # the job until its MCP deadline, even if the proxy exits mid-call.
    task = asyncio.create_task(runtime.call(tool, arguments))
    runtime.calls.add(task)
    def finished(job):
        runtime.calls.discard(job)
        if not job.cancelled():
            job.exception()
    task.add_done_callback(finished)
    return await asyncio.shield(task)


async def close_session(session_id):
    from app.core import browser_panel_registry
    runtime = _runtimes.get(session_id)
    if runtime:
        await runtime.close()
    browser_panel_registry.clear(session_id)


async def shutdown():
    await asyncio.gather(*(close_session(key) for key in list(_runtimes)))
