"""Worktree lifecycle tools are no longer part of the app's agent tool surface.

Branch checkout creation remains available through the normal session intake
paths. Agents cannot create or clean up worktrees through dedicated tools.
"""

from unittest.mock import patch

import pytest


def _handlers(session_id, project_id):
    import app.mcp.inproc as sdk
    from app.mcp import builtin_tools

    captured = {}
    real_create = sdk.create_tool_server

    def _spy(name, version, tools):
        captured["tools"] = tools
        return real_create(name=name, version=version, tools=tools)

    with patch.object(sdk, "create_tool_server", side_effect=_spy):
        builtin_tools.create_builtin_tools_server(
            session_id=session_id, project_id=project_id
        )

    return {tool.name: tool for tool in captured["tools"]}


@pytest.mark.parametrize("project_id", ["proj-1", None])
@pytest.mark.parametrize("session_id", ["s-enter", None])
def test_worktree_tools_are_not_registered(mock_db, session_id, project_id):
    handlers = _handlers(session_id, project_id)
    assert "EnterWorktree" not in handlers
    assert "ExitWorktree" not in handlers


def test_cli_cannot_restore_worktree_tools():
    from app.core.provider.harness_adapter import DELEGATED_BANNED_TOOLS

    assert "EnterWorktree" in DELEGATED_BANNED_TOOLS
    assert "ExitWorktree" in DELEGATED_BANNED_TOOLS


def test_worktree_tools_are_not_allowlisted():
    from app.mcp.builtin_tools import get_builtin_tool_names

    names = get_builtin_tool_names()
    assert "mcp__builtins__EnterWorktree" not in names
    assert "mcp__builtins__ExitWorktree" not in names
