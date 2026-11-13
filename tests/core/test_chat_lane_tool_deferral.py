"""The native chat lane defers its large tool servers behind ToolSearch.

Before, chat had no ToolSearch, so nothing could be deferred there and every
app-config and ai-tools schema rode every request (87 tools, ~24k tokens of
schemas on a default chat surface, Nov 2026). ToolSearch is now on the chat
ceiling (``CHAT_MODE_TOOL_SURFACE``); it reads the loop's filtered view, so it
loads only tools that ceiling permits. Chat still never gets code mode.
"""

import pytest

import app.core.provider.harness_adapter as ha
from app.core.provider.types import SessionOptions
from app.core.session_modes import (
    CHAT_MODE_DENIED_TOOLS,
    CHAT_MODE_TOOL_SURFACE,
    PROACTIVE_DENIED_TOOLS,
)
from tests.core.test_delegated_bridge_tiers import _isolated, _servers  # noqa: F401


class _NullProvider:
    name = "null"

    async def stream(self, *args, **kwargs):  # pragma: no cover - never called
        if False:
            yield None


def _chat_options(tmp_path, surface=None):
    options = SessionOptions(model="m", cwd=str(tmp_path), permission_mode="default",
                             mcp_servers=_servers(str(tmp_path)),
                             tool_allowlist=list(CHAT_MODE_TOOL_SURFACE if surface is None else surface))
    options.extra["session_mode"] = "chat"
    return options


async def _chat_loop(tmp_path, options):
    from fluid.engine.native import NativeLoop
    from fluid.store.base import MemoryStore
    from fluid.tools.registry import ToolCtx

    registry = await ha.HarnessAdapter()._build_registry(options, native=True)
    loop = NativeLoop(provider=_NullProvider(), store=MemoryStore(), model="m", registry=registry,
                      ctx=ToolCtx(cwd=str(tmp_path)), allowed_tools=ha._tool_allowlist(options))
    return registry, loop


async def _tool_search(registry, loop, query):
    return await registry.dispatch("ToolSearch", {"query": query}, loop.ctx)


def test_the_chat_ceiling_carries_toolsearch_but_not_code_mode():
    assert "ToolSearch" in CHAT_MODE_TOOL_SURFACE
    assert "ToolSearch" not in CHAT_MODE_DENIED_TOOLS
    assert "run_python" in CHAT_MODE_DENIED_TOOLS and "run_python" not in CHAT_MODE_TOOL_SURFACE
    # A background proactive run still may not search for tools.
    assert "ToolSearch" in PROACTIVE_DENIED_TOOLS


@pytest.mark.asyncio
async def test_chat_defers_app_config_and_ai_tools_and_loads_only_permitted_ones(tmp_path):
    options = _chat_options(tmp_path)
    registry, loop = await _chat_loop(tmp_path, options)
    sent = {tool["name"] for tool in loop._tools_api()}
    assert "ToolSearch" in sent
    assert not [n for n in sent if n.startswith(("mcp__app-config__", "mcp__ai-tools__"))]
    deferred = {spec.name for spec in registry.deferred_tools()}
    assert "mcp__app-config__listProfiles" in deferred
    assert "mcp__ai-tools__generateImage" in deferred
    assert len(sent) < 25

    # A tool on the chat ceiling: found and loaded, then sent.
    out = await _tool_search(registry, loop, "select:mcp__ai-tools__generateImage")
    assert not out.is_error, out.text
    assert '"name": "mcp__ai-tools__generateImage"' in out.text
    assert "mcp__ai-tools__generateImage" in {t["name"] for t in loop._tools_api()}

    # A deferred tool outside the ceiling reads as one that does not exist.
    blocked = "mcp__ai-tools__editImage"
    assert blocked in deferred and blocked not in CHAT_MODE_TOOL_SURFACE
    out = await _tool_search(registry, loop, f"select:{blocked}")
    assert out.is_error and "unknown tool" in out.text
    out = await _tool_search(registry, loop, "edit an image")
    assert blocked not in out.text
    assert blocked not in {t["name"] for t in loop._tools_api()}


@pytest.mark.asyncio
async def test_a_chat_surface_without_toolsearch_defers_nothing(tmp_path):
    surface = [name for name in CHAT_MODE_TOOL_SURFACE if name != "ToolSearch"]
    options = _chat_options(tmp_path, surface)
    registry, loop = await _chat_loop(tmp_path, options)
    sent = {tool["name"] for tool in loop._tools_api()}
    # No tool may become unreachable: the permitted generation tools stay sent.
    assert "mcp__ai-tools__generateImage" in sent
    assert "mcp__app-config__listProfiles" in sent
    assert not [s for s in registry.deferred_tools() if loop._tool_wired(s.name)]
