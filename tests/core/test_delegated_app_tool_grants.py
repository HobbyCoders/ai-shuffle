"""The app's own MCP tools keep their permission grant on the delegated
(Claude subscription) leg.

``query_options.build_options_from_profile`` puts every app MCP tool name into
the SDK ``allowed_tools`` list; under the Agent SDK that became
``--allowedTools``, so those tools ran without prompting. The delegated leg
serves the in-proc app servers over fluid's bundled bridge, where the CLI calls
them ``mcp__fluid_bundled__<registry name>``, so ``connect_with_sdk_options``
carries the list and ``_delegated_permission_grants`` respells it for the CLI.
Without that, Ask mode prompted for every Task, Remember, Recall,
ScheduleWakeup and browser call. The CLI's own tools stay ungranted.
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


def _cli_rule_allows(tool: str, rules) -> bool:
    """Claude Code allow-rule matching for a bare tool name: an exact name,
    a server-level ``mcp__<server>`` rule, or a ``mcp__<server>__*`` wildcard."""
    for rule in rules or ():
        if rule == tool:
            return True
        if rule.startswith("mcp__") and tool.startswith(rule + "__"):
            return True
        if rule.endswith("*") and tool.startswith(rule[:-1]):
            return True
    return False


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
        mcp_servers={"browser": {"type": "stdio", "command": "node", "args": ["index.js"]}},
        allowed_tools=[
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

    # The CLI's --allowed-tools: the surface grant plus the permission grants.
    granted = [
        *(_KwargsLoop.captured.get("allowed_tools") or ()),
        *(_KwargsLoop.captured.get("permission_grants") or ()),
    ]
    # The names the claude CLI will actually ask about.
    cli_names = [
        "mcp__fluid_bundled__mcp__builtins__Remember",
        "mcp__fluid_bundled__mcp__builtins__Task",
        "mcp__browser__browser_navigate",
    ]
    missing = [name for name in cli_names if not _cli_rule_allows(name, granted)]
    assert not missing, (
        f"--allowed-tools={granted!r} leaves the app's own MCP tools ungranted, "
        f"so the CLI prompts for every call to: {missing}"
    )


def _grant_options(tmp_path, allowed, *, surface=None):
    from app.core.provider.types import SessionOptions

    return SessionOptions(
        model="fable",
        cwd=str(tmp_path),
        mcp_servers={
            "browser": {"type": "stdio", "command": "node", "args": ["index.js"]},
            "builtins": {"type": "sdk", "name": "builtins", "instance": object()},
        },
        allowed_tools=allowed,
        tool_allowlist=surface,
    )


def test_grants_are_spelled_the_way_the_cli_calls_the_tools(tmp_path):
    grants = ha._delegated_permission_grants(_grant_options(tmp_path, [
        "mcp__builtins__Task", "mcp__ai-tools", "mcp__browser", "Bash", "Read",
    ]))
    assert grants == [
        "mcp__fluid_bundled__mcp__builtins__Task",
        # A server-level entry for a bridged server grants its whole prefix.
        "mcp__fluid_bundled__mcp__ai-tools__*",
        "mcp__browser",
    ]
    # The CLI's own tools are never granted here: Ask mode still prompts.
    assert not any(_cli_rule_allows(name, grants) for name in ("Bash", "Read", "Write"))


def test_allow_mode_grants_only_what_is_on_the_surface(tmp_path):
    options = _grant_options(
        tmp_path, ["mcp__builtins__Task", "mcp__builtins__Remember"],
        surface=["mcp__builtins__Remember", "Read"],
    )
    assert ha._delegated_permission_grants(options) == [
        "mcp__fluid_bundled__mcp__builtins__Remember",
    ]
    assert ha._delegated_permission_grants(_grant_options(tmp_path, None)) is None
