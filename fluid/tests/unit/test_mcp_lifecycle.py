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


# ─── Actor error handling against fake sessions (no server process) ─────────


class _SdkTimeout(Exception):
    """Shaped like the SDK's McpError for its own read timeout (code 408)."""

    def __init__(self, code: int) -> None:
        super().__init__(f"mcp error {code}")
        self.error = type("ErrorData", (), {"code": code})()


class _FakeSession:
    def __init__(self, raise_exc: Exception | None = None, legacy: bool = False) -> None:
        self.raise_exc = raise_exc
        self.legacy = legacy
        self.calls: list[tuple] = []

    async def call_tool(self, tool, input, **kwargs):
        self.calls.append((tool, input, kwargs))
        if self.legacy and kwargs:
            raise TypeError("unexpected keyword argument 'allow_input_required'")
        if self.raise_exc is not None:
            raise self.raise_exc
        return {"content": [{"type": "text", "text": f"ran {tool}"}], "isError": False}


async def test_sdk_read_timeout_is_reported_as_a_tool_timeout_result() -> None:
    from fluid.tools.mcp.client import McpClient

    client = McpClient("srv", {"call_timeout": 7})
    out = await client._do_call(_FakeSession(_SdkTimeout(408)), "slow", {})
    assert out["is_error"] and out["error_code"] == "tool_timeout"
    assert "did not respond to slow within 7 s" in out["text"]


async def test_other_session_errors_propagate_rather_than_masquerading_as_timeouts() -> None:
    from fluid.tools.mcp.client import McpClient

    client = McpClient("srv", {})
    for exc in (_SdkTimeout(500), ValueError("bad params")):
        try:
            await client._do_call(_FakeSession(exc), "t", {})
        except Exception as raised:  # noqa: BLE001
            assert raised is exc
        else:
            raise AssertionError(f"{exc!r} was swallowed")


async def test_legacy_sdk_without_mrtr_kwargs_is_called_positionally() -> None:
    from fluid.tools.mcp.client import McpClient

    session = _FakeSession(legacy=True)
    out = await McpClient("srv", {})._do_call(session, "echo", None)
    assert out == {
        "kind": "result", "text": "ran echo", "images": [], "is_error": False,
        "structured": None,
    }
    assert session.calls[0][2] == {"allow_input_required": True}
    assert session.calls[1] == ("echo", {}, {})


async def test_actor_death_fails_every_queued_command() -> None:
    """``_fail_pending`` resolves each queued future with an error (skipping
    the stop sentinel and futures a caller already abandoned), so no caller
    waits on a dead actor."""
    from fluid.tools.mcp.client import McpClient

    client = McpClient("srv", {})
    client._fail_pending("never started")  # no queue yet: a no-op
    client._cmd_q = asyncio.Queue()
    loop = asyncio.get_running_loop()
    waiting = [loop.create_future() for _ in range(2)]
    abandoned = loop.create_future()
    abandoned.cancel()
    await client._cmd_q.put((waiting[0], "list", (), {}))
    await client._cmd_q.put(None)
    await client._cmd_q.put((abandoned, "call", ("t", {}), {}))
    await client._cmd_q.put((waiting[1], "call", ("t", {}), {}))
    client._fail_pending("connection closed")
    assert client._cmd_q.empty()
    for fut in waiting:
        exc = fut.exception()
        assert isinstance(exc, RuntimeError) and "'srv': connection closed" in str(exc)
    assert abandoned.cancelled()


async def test_command_loop_survives_a_failing_command_and_skips_cancelled_ones() -> None:
    from fluid.tools.mcp.client import McpClient

    client = McpClient("srv", {})
    client._cmd_q = asyncio.Queue()
    loop = asyncio.get_running_loop()
    unknown, cancelled, ok = (loop.create_future() for _ in range(3))
    cancelled.cancel()
    session = _FakeSession()
    for cmd in (
        (unknown, "bogus", (), {}),
        (cancelled, "call", ("skipped", {}), {}),
        (ok, "call", ("echo", {"a": 1}), {}),
        None,
    ):
        await client._cmd_q.put(cmd)
    await asyncio.wait_for(client._command_loop(session), 5)
    assert isinstance(unknown.exception(), ValueError)
    assert "unknown mcp command: bogus" in str(unknown.exception())
    assert ok.result()["text"] == "ran echo"
    assert [c[0] for c in session.calls] == ["echo"]
