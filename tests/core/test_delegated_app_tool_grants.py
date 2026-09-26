"""The app's own MCP tools keep their permission grant on the delegated
(Claude subscription) leg.

``query_options.build_options_from_profile`` puts every app MCP tool name into
the SDK ``allowed_tools`` list; under the Agent SDK that became
``--allowedTools``, so those tools ran without prompting. The delegated leg
serves the in-proc app servers over fluid's bundled bridge, where the CLI calls
them ``mcp__fluid_bundled__<registry name>``, so ``connect_with_sdk_options``
carries the list and ``_delegated_permission_grants`` respells it for the CLI.
Only tools the native leg rates ``safe`` in ``fluid_tool_bridge.SERVER_RISK_MAPS``
are granted: write/destructive tools (Task, Remember, createHook, CronCreate,
request_access, ...) keep prompting in Ask mode exactly as on the native leg, and
plan mode grants nothing. External servers such as the browser server and the
CLI's own tools stay ungranted.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

import app.core.provider.harness_adapter as ha

FAKE = Path(__file__).resolve().parents[2] / "fluid" / "tests" / "fixtures" / "fake_claude.py"


@pytest.fixture(autouse=True)
def _isolated_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    from app.core import guidance_surface

    monkeypatch.setenv(guidance_surface.GUIDANCE_HOME_ENV, str(home))


class _KwargsLoop:
    """Stands in for DelegatedLoop and records the kwargs it is built with."""

    captured: ClassVar[dict] = {}

    def __init__(self, **kwargs):
        type(self).captured = dict(kwargs)
        self.agents = []

    async def _ensure_session(self):
        return SimpleNamespace(session_id="ses_x")

    async def aclose(self):
        pass


@pytest.mark.asyncio
async def test_app_mcp_tools_keep_their_grant_on_the_delegated_leg(monkeypatch, tmp_path):
    monkeypatch.setattr(ha, "_resolve_claude_cmd", lambda: [sys.executable, str(FAKE)])
    monkeypatch.setattr(ha, "DelegatedLoop", _KwargsLoop)

    # The shape query_options builds for an ordinary profile in "ask" mode:
    # no tool surface restriction, and a PERMISSION allowlist carrying the
    # app's MCP tools.
    sdk_options = SimpleNamespace(
        model="fable",
        system_prompt={"type": "preset", "preset": "claude_code", "append": "APPEND"},
        cwd=str(tmp_path),
        permission_mode="default",
        mcp_servers={
            "browser": {"type": "stdio", "command": "node", "args": ["index.js"]},
            "builtins": {"type": "sdk", "name": "builtins", "instance": object()},
        },
        allowed_tools=[
            "mcp__builtins__Recall",
            "mcp__builtins__ScheduleWakeup",
            "mcp__builtins__Remember",
            "mcp__builtins__Task",
            "mcp__browser",
        ],
        disallowed_tools=[],
        can_use_tool=None,
        hooks=None,
    )
    adapter = ha.HarnessAdapter()
    await adapter.connect_with_sdk_options(sdk_options)

    # The exact rules the claude CLI receives: the safe bundled app tools in
    # the CLI's spelling. Remember and Task are ``write`` on the native leg and
    # the browser server is external, so all three keep prompting in Ask mode.
    assert _KwargsLoop.captured.get("permission_grants") == [
        "mcp__fluid_bundled__mcp__builtins__Recall",
        "mcp__fluid_bundled__mcp__builtins__ScheduleWakeup",
    ]
    assert _KwargsLoop.captured.get("allowed_tools") is None


def _grant_options(tmp_path, allowed, *, surface=None, mode="default"):
    from app.core.provider.types import SessionOptions

    return SessionOptions(
        model="fable",
        cwd=str(tmp_path),
        permission_mode=mode,
        mcp_servers={
            "browser": {"type": "stdio", "command": "node", "args": ["index.js"]},
            "builtins": {"type": "sdk", "name": "builtins", "instance": object()},
        },
        allowed_tools=allowed,
        tool_allowlist=surface,
    )


def test_grants_are_spelled_the_way_the_cli_calls_the_tools(tmp_path):
    options = _grant_options(tmp_path, [
        "mcp__builtins__Recall", "mcp__builtins__Task", "mcp__ai-tools", "mcp__browser",
        "mcp__browser__browser_click", "mcp__unattached__Tool", "Bash", "Read",
    ])
    options.mcp_servers["ai-tools"] = {"type": "sdk", "name": "ai-tools", "instance": object()}
    # A server-level entry for a server with write tools is expanded to its
    # safe tools only. External servers, servers that are not attached, the
    # CLI's own tools and write tools (Task) are never granted.
    assert ha._delegated_permission_grants(options) == [
        "mcp__fluid_bundled__mcp__builtins__Recall",
        "mcp__fluid_bundled__mcp__ai-tools__searchModels",
    ]


def test_write_and_destructive_app_tools_are_never_granted(tmp_path):
    from app.core.provider.fluid_tool_bridge import SERVER_RISK_MAPS

    servers = {
        name: {"type": "sdk", "name": name, "instance": object()} for name in SERVER_RISK_MAPS
    }
    allowed = [
        f"mcp__{server}__{tool}" for server, tools in SERVER_RISK_MAPS.items() for tool in tools
    ] + ["mcp__builtins__gateway_send_message"]  # unknown -> write, as on native
    options = _grant_options(tmp_path, allowed)
    options.mcp_servers.update(servers)
    grants = ha._delegated_permission_grants(options)
    expected = [
        f"mcp__fluid_bundled__mcp__{server}__{tool}"
        for server, tools in SERVER_RISK_MAPS.items()
        for tool, risk in tools.items()
        if risk == "safe"
    ]
    assert grants == expected
    for dangerous in (
        "app-config__createHook", "app-config__addMcpServer", "computer-use__request_access",
        "builtins__CronCreate", "builtins__CronDelete", "builtins__Remember", "builtins__Task",
        "ai-tools__generateVideo", "builtins__gateway_send_message",
    ):
        assert f"mcp__fluid_bundled__mcp__{dangerous}" not in grants
    assert "mcp__fluid_bundled__mcp__app-config__listHooks" in grants


def test_whole_server_wildcard_only_when_every_tool_is_safe(tmp_path, monkeypatch):
    from app.core.provider import sdk_tool_records

    tools = {
        "chat-access": [SimpleNamespace(name="ChatResources"), SimpleNamespace(name="ChatReadFile")],
        "app-config": [SimpleNamespace(name="listHooks"), SimpleNamespace(name="createHook")],
    }
    monkeypatch.setattr(
        sdk_tool_records, "recorded_sdk_server_tools", lambda cfg: tools.get(cfg.get("name"))
    )
    options = _grant_options(tmp_path, ["mcp__chat-access", "mcp__app-config"])
    for name in tools:
        options.mcp_servers[name] = {"type": "sdk", "name": name, "instance": object()}
    assert ha._delegated_permission_grants(options) == [
        "mcp__fluid_bundled__mcp__chat-access__*",
        "mcp__fluid_bundled__mcp__app-config__listHooks",
    ]


def test_plan_mode_grants_nothing(tmp_path):
    assert ha._delegated_permission_grants(
        _grant_options(tmp_path, ["mcp__builtins__Recall", "mcp__builtins"], mode="plan")
    ) is None


def test_external_servers_are_never_granted(tmp_path):
    assert ha._delegated_permission_grants(
        _grant_options(tmp_path, ["mcp__browser", "mcp__browser__browser_navigate"])
    ) is None


def test_allow_mode_grants_only_what_is_on_the_surface(tmp_path):
    options = _grant_options(
        tmp_path, ["mcp__builtins__Recall", "mcp__builtins__SessionSearch"],
        surface=["mcp__builtins__SessionSearch", "Read"],
    )
    assert ha._delegated_permission_grants(options) == [
        "mcp__fluid_bundled__mcp__builtins__SessionSearch",
    ]
    assert ha._delegated_permission_grants(_grant_options(tmp_path, None)) is None
