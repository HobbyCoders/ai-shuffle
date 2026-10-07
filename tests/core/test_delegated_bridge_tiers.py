"""The delegated leg's bundled bridge loads a small tier and defers the rest.

Fluid's bundled bridge serves every app tool to the Claude Code CLI. With all
of them loaded, the CLI's /context showed "MCP tools" at ~29-30.5k tokens per
request (Oct 2026). Tools outside ``DELEGATED_LOADED_TOOLS`` are now listed
with ``_meta`` ``anthropic/alwaysLoad`` false: the CLI announces them by name
and loads a schema through its ToolSearch when the model asks. Measured on CLI
2.1.292 with the default servers: "MCP tools" 30.2k -> 12.1k (18.1k
deferred), whole context 43.9k -> 25.8k.

The bridge is built the way ``HarnessAdapter.connect`` builds it and the tool
list is read through the same ``_answer`` the CLI's child server asks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from fluid.context.trim import estimate_tokens_of_text
from fluid.tools.mcp.bundled_server import ALWAYS_LOAD_META_KEY

import app.core.provider.harness_adapter as ha
from app.core.provider.fluid_tool_bridge import (
    BRIDGE_DEFER_ENV,
    DELEGATED_LOADED_TOOLS,
    delegated_bridge_deferred,
)
from app.core.provider.types import SessionOptions

SID = "11111111-1111-1111-1111-111111111111"
CLI_PREFIX = "mcp__fluid_bundled__"
# The loaded tier, priced with the app's estimator (chars/4) over the CLI's
# view of each tool. The CLI's tokenizer counted the tier at ~1.33x that
# (7.6k for ~5.8k here, an earlier cut of the tier). The first cut's budget
# (8,500, ~11.3k CLI tokens) plus the 1,819 the peer-session, hand-off and
# usage tools cost (ListSessions, MessageSession, StartSession, HandOffSession,
# GetUsage). The tier itself measures ~8.9k here; the CLI counted 12.1k.
LOADED_BUDGET = 10_319
# Named in the private-access guidance but only usable after a grant, which
# the loaded RequestChatAccess and ChatResources lead to; the CLI announces
# them in its deferred list and ToolSearch loads them then.
DEFERRED_AFTER_GRANT = {"ChatHttpRequest", "ChatRunCommand"}


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    from app.core import cli_tool_surface, guidance_surface
    from app.core.config import settings

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv(guidance_surface.GUIDANCE_HOME_ENV, str(home))
    monkeypatch.delenv(BRIDGE_DEFER_ENV, raising=False)
    # Register the bundled Glob/Grep fallback, the larger surface.
    monkeypatch.setattr(ha, "_cli_ships_search_tools", lambda: False)
    # SessionSearch and SessionView register only with the app database.
    monkeypatch.setattr(type(settings), "managed_database_available", lambda self: True)
    cli_tool_surface.reset_cli_tools()
    yield
    cli_tool_surface.reset_cli_tools()


def _servers(cwd: str) -> dict:
    from app.mcp.ai_tools_sdk import create_ai_tools_server
    from app.mcp.app_config_sdk import create_app_config_server
    from app.mcp.builtin_tools import create_builtin_tools_server
    from app.mcp.chat_access_sdk import create_chat_access_server

    servers = {
        "builtins": create_builtin_tools_server(
            session_id=SID, profile_id="p", project_id=None, is_admin=True,
            model="opus", provider_id=None, cwd=cwd,
        ),
        "app-config": create_app_config_server(is_admin=True, project_dir=cwd),
        "chat-access": create_chat_access_server(SID),
        "ai-tools": create_ai_tools_server(
            {key: True for key in (
                "image_generation", "image_editing", "video_generation",
                "image_to_video", "audio_generation", "text_to_3d", "image_to_3d",
                "image_reference", "fal_model", "video_extend", "audio_to_audio",
            )},
            tools_dir=cwd,
        ),
    }
    return {name: cfg for name, cfg in servers.items() if cfg}


async def _bridge(tmp_path, *, lane="code"):
    """(options, registry, the bridge's tool list as the CLI child reads it)."""
    from fluid.engine.delegated import DelegatedLoop
    from fluid.store.base import MemoryStore
    from fluid.tools.mcp.bundled_server import _answer

    options = SessionOptions(
        model="opus", cwd=str(tmp_path), permission_mode="default",
        mcp_servers=_servers(str(tmp_path)),
    )
    options.extra["session_mode"] = lane
    registry = await ha.HarnessAdapter()._build_registry(options, native=False)
    loop = DelegatedLoop(
        store=MemoryStore(), model="opus", registry=registry,
        permission_mode=options.permission_mode,
        allowed_tools=ha._delegated_allowed(options),
        disallowed_tools=ha._delegated_disallowed(options),
        permission_grants=ha._delegated_permission_grants(options),
        claude_cmd=["claude"],
        bridge_deferred=delegated_bridge_deferred(),
    )
    try:
        listed = await _answer(
            registry, loop.ctx, {"op": "list"},
            tool_filter=loop._bridge_tool_permitted, deferred=loop.bridge_deferred,
        )
    finally:
        await loop.aclose()
    return options, registry, listed["tools"]


def _is_deferred(tool: dict) -> bool:
    return (tool.get("meta") or {}).get(ALWAYS_LOAD_META_KEY) is False


def _cli_tokens(tool: dict) -> int:
    return estimate_tokens_of_text(json.dumps({
        "name": CLI_PREFIX + tool["name"],
        "description": tool["description"],
        "input_schema": tool["input_schema"],
    }))


@pytest.mark.asyncio
async def test_loaded_tier_stays_under_budget(tmp_path):
    _options, _registry, tools = await _bridge(tmp_path)
    loaded = [t for t in tools if not _is_deferred(t)]
    loaded_tokens = sum(_cli_tokens(t) for t in loaded)
    everything = sum(_cli_tokens(t) for t in tools)
    assert loaded_tokens <= LOADED_BUDGET, sorted(
        ((_cli_tokens(t), t["name"]) for t in loaded), reverse=True
    )
    # The deferred tier carries most of the bridge: a regression that loads it
    # all again fails the budget above long before this.
    assert everything > 2 * loaded_tokens, (loaded_tokens, everything)


@pytest.mark.asyncio
async def test_rarely_used_tools_are_deferred(tmp_path):
    _options, _registry, tools = await _bridge(tmp_path)
    by_name = {t["name"]: t for t in tools}
    loaded = {name for name, t in by_name.items() if not _is_deferred(t)}
    assert loaded == DELEGATED_LOADED_TOOLS & set(by_name)
    # The default servers carry every loaded tool except the chat lane's reader.
    assert DELEGATED_LOADED_TOOLS - set(by_name) == {"mcp__attachments__Read"}
    for name in (
        "Glob", "Grep",
        "mcp__builtins__AndroidAction", "mcp__builtins__AndroidDevices",
        "mcp__builtins__CronCreate", "mcp__builtins__CronList",
        "mcp__builtins__GetAppErrors", "mcp__builtins__GetAppLogs",
        "mcp__builtins__ArchiveSession",
        "mcp__builtins__SettleSession", "mcp__builtins__UnsettleSession",
        "mcp__builtins__DeployStatus",
        "mcp__builtins__InstallPlugin", "mcp__builtins__WorktreeReport",
        "mcp__app-config__getConfigCapabilities", "mcp__app-config__createAutomation",
        "mcp__ai-tools__generateImage", "mcp__ai-tools__runFalModel",
        "mcp__chat-access__ChatHttpRequest", "mcp__chat-access__ChatRunCommand",
    ):
        assert name in by_name, name
        assert _is_deferred(by_name[name]), name
    # Every app-config and ai-tools tool, including ones added later.
    for name, tool in by_name.items():
        if name.startswith(("mcp__app-config__", "mcp__ai-tools__")):
            assert _is_deferred(tool), name


@pytest.mark.asyncio
async def test_tools_the_prompt_names_are_loaded(tmp_path):
    from app.mcp.chat_access_sdk import GUIDANCE

    _options, registry, tools = await _bridge(tmp_path)
    blocks = "\n".join(ha._delegated_guidance_blocks(_options, registry))
    deferred = {t["name"].rsplit("__", 1)[-1] for t in tools if _is_deferred(t)}
    # The app-tools note calls each bridged tool by its ``__<name>`` suffix.
    named = set(re.findall(r"__([A-Z]\w+)", blocks))
    # Glob/Grep: the note sends the model to the bundled copies only when the
    # CLI's own are missing; the CLI lists the deferred copies by name then.
    assert named - {"Glob", "Grep"}, blocks
    assert not (named - {"Glob", "Grep"}) & deferred, blocks
    # The private-access guidance's first steps.
    for name in set(re.findall(r"\b(RequestChatAccess|Chat[A-Z]\w+)\b", GUIDANCE)):
        assert (name in deferred) == (name in DEFERRED_AFTER_GRANT), name


@pytest.mark.asyncio
async def test_chat_lane_keeps_the_attachment_reader_loaded(tmp_path):
    from app.core.attachment_read import DELEGATED_READ

    _options, _registry, tools = await _bridge(tmp_path, lane="chat")
    reader = next(t for t in tools if t["name"] == DELEGATED_READ)
    assert not _is_deferred(reader)


@pytest.mark.parametrize("flag", ["0", "false", "OFF", "no"])
def test_kill_switch_loads_everything(monkeypatch, flag):
    monkeypatch.setenv(BRIDGE_DEFER_ENV, flag)
    assert delegated_bridge_deferred() is None


def test_deferral_is_on_by_default_and_an_allowlist(monkeypatch):
    monkeypatch.setenv(BRIDGE_DEFER_ENV, "1")
    deferred = delegated_bridge_deferred()
    assert deferred is not None
    assert not deferred("mcp__builtins__Task")
    assert deferred("mcp__builtins__SomeToolAddedLater")
    monkeypatch.delenv(BRIDGE_DEFER_ENV)
    assert delegated_bridge_deferred() is not None


@pytest.mark.asyncio
async def test_kill_switch_lists_every_tool_loaded(tmp_path, monkeypatch):
    monkeypatch.setenv(BRIDGE_DEFER_ENV, "0")
    _options, _registry, tools = await _bridge(tmp_path)
    assert tools and not any("meta" in t for t in tools)
