"""The app builtins the delegated (Claude subscription) leg serves over the
bundled MCP bridge.

On the delegated leg the ``claude`` CLI reaches the app's own tools as
``mcp__fluid_bundled__mcp__builtins__<name>``, dispatched by fluid's
ToolRegistry on the parent side. These tests cover what used to differ from
the native leg: images, the sync Task bound, the CLI's tool_use id, agent
messaging, the allow-list the CLI grants from, and SessionSearch's
self-exclusion.
"""

from __future__ import annotations

import asyncio
import base64
import io
from types import SimpleNamespace

import pytest
from fluid.tools.registry import ToolCtx, ToolRegistry, current_tool_use_id

try:  # the real computer-use tools on fake displays; needs Pillow
    from tests.mcp.test_computer_use_tools import desktop  # noqa: F401
except (ImportError, pytest.skip.Exception):  # pragma: no cover - no Pillow
    desktop = None


def _builtins_server(session_id: str = "sess-1", cwd: str | None = None):
    from app.mcp.builtin_tools import create_builtin_tools_server

    return create_builtin_tools_server(
        session_id=session_id, profile_id="prof-1", is_admin=True, cwd=cwd,
    )


def _tools(server) -> dict:
    from app.core.provider.sdk_tool_records import recorded_sdk_server_tools

    return {t.name: t for t in recorded_sdk_server_tools(server)}


def _registry(server, *, bound: float | None = None, native: bool = False) -> ToolRegistry:
    from app.core.provider.fluid_tool_bridge import (
        NATIVE_SHADOWED_BUILTINS,
        register_recorded_sdk_servers,
    )

    reg = ToolRegistry(external_call_timeout=bound) if bound is not None else ToolRegistry()
    register_recorded_sdk_servers(
        reg, {"builtins": server},
        skip_tools={"builtins": set(NATIVE_SHADOWED_BUILTINS)} if native else None,
    )
    return reg


# ─── images ──────────────────────────────────────────────────────────────────


async def test_computer_use_screenshot_keeps_its_image_through_the_bridge(request):
    Image = pytest.importorskip("PIL.Image")
    from app.core.provider.fluid_tool_bridge import COMPUTER_USE_RISK_MAP, register_sdk_tools
    from fluid.tools.mcp.bundled_server import _answer

    handlers = request.getfixturevalue("desktop").tools
    reg = ToolRegistry()
    register_sdk_tools(
        reg, "computer-use",
        [SimpleNamespace(name="screenshot", description="Take a screenshot",
                         input_schema={"type": "object", "properties": {}},
                         handler=handlers["screenshot"])],
        risk_map=COMPUTER_USE_RISK_MAP,
    )

    reply = await _answer(
        reg, ToolCtx(), {"op": "call", "name": "mcp__computer-use__screenshot", "arguments": {}},
    )

    assert reply["is_error"] is False
    assert len(reply["images"]) == 1
    assert reply["images"][0]["media_type"] == "image/png"
    image = Image.open(io.BytesIO(base64.b64decode(reply["images"][0]["data"])))
    assert image.format == "PNG"
    assert image.width == 150  # the virtual screenshot width the server was built with


# ─── the synchronous Task bound ──────────────────────────────────────────────


async def test_sync_task_outlives_the_general_call_bound(mock_db, monkeypatch):
    from app.core import task_dispatch

    async def slow_worker(args, **kwargs):
        await asyncio.sleep(0.5)
        return {"ok": True, "report": "worker finished"}

    monkeypatch.setattr(task_dispatch, "dispatch_task_tool", slow_worker)
    reg = _registry(_builtins_server(), bound=0.1)

    out = await reg.dispatch(
        "mcp__builtins__Task", {"subagent_type": "x", "prompt": "p", "description": "d"},
        ToolCtx(),
    )

    assert not out.is_error, out.text
    assert out.error_code is None
    assert "worker finished" in out.text


def test_only_task_gets_a_longer_bound():
    from app.core.config import settings
    from app.core.provider.fluid_tool_bridge import _builtins_call_timeouts

    cap = float(settings.task_worker_sync_max_seconds)
    assert _builtins_call_timeouts(ToolRegistry(external_call_timeout=330)) == {
        "Task": max(cap + 60.0, 330.0),
    }
    # A general bound already above the sync cap is never lowered.
    assert _builtins_call_timeouts(ToolRegistry(external_call_timeout=cap + 600)) == {
        "Task": cap + 600,
    }


def test_task_is_unbounded_when_the_sync_cap_is_off(monkeypatch):
    from app.core.config import settings
    from app.core.provider.fluid_tool_bridge import _builtins_call_timeouts

    monkeypatch.setattr(settings, "task_worker_sync_max_seconds", 0)
    assert _builtins_call_timeouts(ToolRegistry(external_call_timeout=330)) == {"Task": None}


async def test_other_builtins_keep_the_general_bound(mock_db, monkeypatch):
    """A hung Monitor spawn is still cut off by the registry's bound."""
    from app.mcp import builtin_tools as bt

    async def hung_spawn(command, cwd, session_id, tool_use_id=None):
        await asyncio.sleep(5)

    monkeypatch.setattr(bt, "_spawn_background", hung_spawn)
    reg = _registry(_builtins_server(), bound=0.1)

    out = await reg.dispatch("mcp__builtins__Monitor", {"command": "sleep 99"}, ToolCtx())

    assert out.error_code == "tool_timeout"


# ─── the CLI's tool_use id ───────────────────────────────────────────────────


@pytest.fixture
def captured_task(monkeypatch):
    from app.core import task_dispatch

    calls: list[dict] = []

    async def record(args, **kwargs):
        calls.append(kwargs)
        return {"ok": True}

    monkeypatch.setattr(task_dispatch, "dispatch_task_tool", record)
    return calls


async def test_bridged_task_passes_the_cli_tool_use_id(mock_db, captured_task):
    reg = _registry(_builtins_server())
    args = {"subagent_type": "x", "prompt": "p", "description": "d"}

    await reg.dispatch("mcp__builtins__Task", args, ToolCtx(extra={"tool_use_id": "toolu_X"}))
    await reg.dispatch("mcp__builtins__Task", args, ToolCtx())

    assert [c["launch_tool_use_id"] for c in captured_task] == ["toolu_X", None]


async def test_bridged_task_reads_the_context_var(mock_db, captured_task):
    task = _tools(_builtins_server())["Task"]
    token = current_tool_use_id.set("toolu_V")
    try:
        await task.handler({"subagent_type": "x", "prompt": "p", "description": "d"})
    finally:
        current_tool_use_id.reset(token)

    assert captured_task[0]["launch_tool_use_id"] == "toolu_V"


class _FakeShell:
    def __init__(self):
        self.task_id = "t-bridge"
        self.command = "cmd"
        self.cwd = None
        self.lines: list[str] = []
        self.exit_code = None
        self.started_at = "2026-09-30T00:00:00Z"
        self.process = None

    def is_running(self):
        return True


async def test_bridged_monitor_registers_its_shell_with_the_tool_use_id(mock_db, monkeypatch):
    from app.mcp import builtin_tools as bt

    spawned: list = []

    async def fake_spawn(command, cwd, session_id, tool_use_id=None):
        spawned.append(tool_use_id)
        return _FakeShell()

    monkeypatch.setattr(bt, "_spawn_background", fake_spawn)
    reg = _registry(_builtins_server())

    await reg.dispatch(
        "mcp__builtins__Monitor", {"command": "make ci"},
        ToolCtx(extra={"tool_use_id": "toolu_M"}),
    )
    await reg.dispatch("mcp__builtins__Monitor", {"command": "make ci"}, ToolCtx())

    assert spawned == ["toolu_M", None]


# ─── agent messaging ─────────────────────────────────────────────────────────


@pytest.fixture
def messaging_runtime(monkeypatch):
    from app.core import session_runtime, swarm
    from app.core import task_registry as registry_module
    from app.core.session_pool import get_session_pool

    registry = registry_module.TaskRegistry()
    monkeypatch.setattr(registry_module, "task_registry", registry)
    monkeypatch.setattr(swarm, "workers", {})
    monkeypatch.setattr(swarm, "coordinators", {})
    engines: dict = {}
    monkeypatch.setattr(get_session_pool(), "get", engines.get)
    monkeypatch.setattr(session_runtime, "get_live_runtime", engines.get)
    return registry, engines


def _worker(runtime, owner: str, worker_id: str, name: str = "Helper"):
    from unittest.mock import AsyncMock

    registry, engines = runtime
    registry.start_task(owner, worker_id, "agent", "Investigate the flake", agent=name)
    engine = SimpleNamespace(submit=AsyncMock())
    engines[worker_id] = SimpleNamespace(
        adapter=SimpleNamespace(engine=engine, provider_kind="openai_responses"),
    )
    return engine


def test_builtins_server_exposes_agent_messaging(mock_db):
    tools = _tools(_builtins_server())
    assert {"ListAgents", "SendMessage"} <= set(tools)


def test_native_legs_skip_the_bridged_messaging_copies(mock_db):
    from app.core.provider.fluid_tool_bridge import BUILTINS_RISK_MAP, NATIVE_SHADOWED_BUILTINS

    assert {"ListAgents", "SendMessage"} <= NATIVE_SHADOWED_BUILTINS
    assert BUILTINS_RISK_MAP["ListAgents"] == "safe"
    assert BUILTINS_RISK_MAP["SendMessage"] == "write"

    server = _builtins_server()
    native = {s.name for s in _registry(server, native=True).list_tools(loaded_only=False)}
    delegated = {s.name for s in _registry(server).list_tools(loaded_only=False)}
    assert "mcp__builtins__ListAgents" not in native
    assert "mcp__builtins__SendMessage" not in native
    assert {"mcp__builtins__ListAgents", "mcp__builtins__SendMessage"} <= delegated
    assert delegated_risk(server, "SendMessage") == "write"


def delegated_risk(server, name: str) -> str:
    return _registry(server).get(f"mcp__builtins__{name}").risk


async def test_bridged_list_agents_returns_the_roster(mock_db, messaging_runtime):
    _worker(messaging_runtime, "parent", "worker-1")
    tools = _tools(_builtins_server(session_id="parent"))

    result = await tools["ListAgents"].handler({})

    text = result["content"][0]["text"]
    assert not result.get("isError"), text
    assert "Helper [worker-1]" in text
    assert "Investigate the flake" in text


async def test_bridged_send_message_reaches_a_task_worker(mock_db, messaging_runtime):
    from fluid.events.ops import AgentMessage

    engine = _worker(messaging_runtime, "parent", "worker-1")
    tools = _tools(_builtins_server(session_id="parent"))

    result = await tools["SendMessage"].handler(
        {"to": "worker-1", "message": "Check the failing test first"},
    )

    assert not result.get("isError"), result
    assert "Delivered to Helper" in result["content"][0]["text"]
    op = engine.submit.await_args.args[0]
    assert isinstance(op, AgentMessage)
    assert op.sender == "parent"
    assert op.text == "Check the failing test first"


async def test_bridged_send_message_to_an_unknown_agent_is_an_error(mock_db, messaging_runtime):
    tools = _tools(_builtins_server(session_id="parent"))
    result = await tools["SendMessage"].handler({"to": "nobody", "message": "hi"})
    assert result.get("isError") is True


# ─── the allow-list the CLI grants from ──────────────────────────────────────


def test_every_builtin_is_allow_listed_and_every_safe_one_granted(mock_db, monkeypatch, tmp_path):
    import app.core.provider.harness_adapter as ha
    from app.core.config import settings
    from app.core.provider.fluid_tool_bridge import BUILTINS_RISK_MAP
    from app.core.provider.types import SessionOptions
    from app.mcp.builtin_tools import get_builtin_tool_names

    # The session-store pair registers only with a managed database.
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    server = _builtins_server()
    registered = set(_tools(server))
    names = get_builtin_tool_names()

    missing = sorted(n for n in registered if f"mcp__builtins__{n}" not in names)
    assert missing == []

    options = SessionOptions(
        model="opus", cwd=str(tmp_path), permission_mode="default",
        mcp_servers={"builtins": server}, allowed_tools=names,
    )
    grants = set(ha._delegated_permission_grants(options) or ())
    safe = sorted(n for n in registered if BUILTINS_RISK_MAP.get(n) == "safe")
    assert "ListSessions" in safe and "ListAgents" in safe
    ungranted = [n for n in safe if f"mcp__fluid_bundled__mcp__builtins__{n}" not in grants]
    assert ungranted == []


# ─── SessionSearch excludes the calling session ──────────────────────────────


@pytest.fixture
def session_store(monkeypatch):
    from app.core.config import settings
    import app.core.provider.fluid_pg_store as pg

    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    seen: list[tuple[str, object]] = []

    class FakeStore:
        async def search(self, terms, limit=5, exclude_session_id=None, **kw):
            seen.append(("search", exclude_session_id))
            return []

        async def recent_sessions(self, limit=5, exclude_session_id=None, **kw):
            seen.append(("recent", exclude_session_id))
            return []

        async def session_slice(self, *args, **kwargs):
            return None

    monkeypatch.setattr(pg, "PgFluidStore", FakeStore)
    return seen


APP_ID = "42595afe-8a8d-49a2-8a87-c13033d4ec67"


async def test_session_search_excludes_the_fluid_session_id(mock_db, monkeypatch, session_store):
    from app.db import sessions as sessions_db

    lookups: list[str] = []

    def get_session(sid):
        lookups.append(sid)
        return {"id": sid, "sdk_session_id": "ses_4acbe05ac51b"}

    monkeypatch.setattr(sessions_db, "get_session", get_session)
    tools = _tools(_builtins_server(session_id=APP_ID))

    await tools["SessionSearch"].handler({"query": "flaky login"})
    await tools["SessionSearch"].handler({})

    assert session_store == [("search", "ses_4acbe05ac51b"), ("recent", "ses_4acbe05ac51b")]
    # Resolved once per server, not per call.
    assert lookups == [APP_ID]


async def test_session_search_falls_back_to_the_app_id(mock_db, monkeypatch, session_store):
    from app.db import sessions as sessions_db

    monkeypatch.setattr(sessions_db, "get_session", lambda sid: {"id": sid, "sdk_session_id": None})
    tools = _tools(_builtins_server(session_id=APP_ID))

    await tools["SessionSearch"].handler({"query": "flaky login"})

    assert session_store == [("search", APP_ID)]
