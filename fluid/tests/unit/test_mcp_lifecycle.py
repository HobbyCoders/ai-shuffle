"""MCP deadlines and reconnects against the in-repo fake stdio server.

A server that never answers must not stall the caller: a slow ``tools/call``
returns an error result at ``call_timeout``, a stuck handshake fails at
``connect_timeout`` while the other servers still connect, and ``aclose``
returns even when the server is mid-sleep.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

from fluid.tools.registry import ToolCtx, ToolRegistry

FIXTURE = str(Path(__file__).parent.parent / "fixtures" / "fake_mcp_server.py")

# Process teardown on Windows can take a few seconds on top of any deadline.
SLACK_S = 10.0


def _config(**knobs: str) -> dict:
    env = dict(os.environ)
    env.update(knobs)
    return {"transport": "stdio", "command": sys.executable, "args": [FIXTURE], "env": env}


async def test_slow_call_returns_error_at_call_timeout() -> None:
    reg = ToolRegistry()
    cfg = {**_config(FLUID_FAKE_CALL_SLEEP_S="600"), "call_timeout": 1, "shutdown_timeout": 3}
    try:
        await reg.connect_mcp("srv", cfg)
        t0 = time.monotonic()
        out = await reg.dispatch("mcp__srv__get_weather", {"city": "Paris"}, ToolCtx())
        assert time.monotonic() - t0 < 1 + SLACK_S
        assert out.is_error
        assert "did not respond" in out.text and "1 s" in out.text

        # The actor is free again: a second call gets the same bounded answer
        # instead of queueing forever behind the first.
        t0 = time.monotonic()
        out = await reg.dispatch("mcp__srv__get_weather", {"city": "Rome"}, ToolCtx())
        assert time.monotonic() - t0 < 1 + SLACK_S
        assert out.is_error
    finally:
        t0 = time.monotonic()
        await reg.aclose()
        assert time.monotonic() - t0 < 2 * 3 + SLACK_S


async def test_stuck_handshake_fails_at_connect_timeout() -> None:
    reg = ToolRegistry()
    cfg = {**_config(FLUID_FAKE_HANG_HANDSHAKE="1"), "connect_timeout": 1, "shutdown_timeout": 3}
    t0 = time.monotonic()
    try:
        try:
            await reg.connect_mcp("hang", cfg)
        except TimeoutError as exc:
            assert "handshake" in str(exc) or "connecting" in str(exc)
        else:
            raise AssertionError("connect_mcp returned against a server that never answers")
        assert time.monotonic() - t0 < 1 + SLACK_S
        assert reg.get("mcp__hang__get_weather") is None
    finally:
        await reg.aclose()


async def test_native_loop_connects_servers_side_by_side(tmp_path) -> None:
    """One stuck server costs the first turn its connect timeout, not the
    turn; the healthy server registered alongside it is usable."""
    from fluid.engine.native import NativeLoop
    from fluid.events.model import ErrorEvent
    from fluid.providers.base import ProviderCaps
    from fluid.store.base import MemoryStore

    class _Provider:
        name = "anthropic"
        caps = ProviderCaps()

        async def stream(self, request):  # pragma: no cover - never streamed
            if False:
                yield None

    reg = ToolRegistry()
    loop = NativeLoop(
        provider=_Provider(),
        store=MemoryStore(),
        model="m",
        ctx=ToolCtx(cwd=str(tmp_path)),
        registry=reg,
        mcp_servers={
            "hang": {**_config(FLUID_FAKE_HANG_HANDSHAKE="1"), "connect_timeout": 1, "shutdown_timeout": 3},
            "good": _config(),
        },
    )
    emitted: list = []

    async def capture(event):
        emitted.append(event)
        return event

    loop._emit = capture  # type: ignore[method-assign]
    try:
        t0 = time.monotonic()
        await loop._connect_mcp_once("sid", "turn")
        assert time.monotonic() - t0 < 1 + SLACK_S
        errors = [e for e in emitted if isinstance(e, ErrorEvent)]
        assert len(errors) == 1
        assert "'hang'" in errors[0].message and not errors[0].retriable
        assert reg.get("mcp__good__get_weather") is not None
        assert reg.get("mcp__hang__get_weather") is None
    finally:
        await reg.aclose()


async def test_reconnect_closes_the_previous_client() -> None:
    reg = ToolRegistry()
    try:
        await reg.connect_mcp("srv", _config())
        first = reg._manager.client("srv")
        await reg.connect_mcp("srv", _config())
        second = reg._manager.client("srv")
        assert second is not first
        assert first._task is not None and first._task.done()
        out = await reg.dispatch("mcp__srv__get_weather", {"city": "Paris"}, ToolCtx())
        assert out.text == "Paris: sunny, 21C"
    finally:
        await reg.aclose()


async def test_call_after_actor_death_raises_instead_of_hanging() -> None:
    from fluid.tools.mcp.client import McpClient

    client = McpClient("srv", _config())
    await client.start()
    try:
        client._task.cancel()
        await asyncio.wait({client._task})
        try:
            await asyncio.wait_for(client.list_tools(), 5)
        except RuntimeError as exc:
            assert "connection closed" in str(exc)
        else:
            raise AssertionError("a command against a dead actor returned")
    finally:
        await client.aclose()
