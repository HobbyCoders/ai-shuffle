"""
Unit tests for MCP server config tools (_create_mcp_tools) and the McpService
path handling they depend on.

Regression coverage for the listMcpServers crash:
"unsupported operand type(s) for /: 'str' and 'str'". The app-config MCP server
was wiring a *project identifier* string into McpService(project_dir=...), which
then did `str / ".claude"`. The fixes are (1) pass the real project *directory*
to the MCP tools, and (2) defensively coerce a str directory to Path in
McpService so a stray string can never produce a cryptic TypeError.
"""

from pathlib import Path

import pytest
from unittest.mock import patch

from app.mcp.config_mcp_servers import _create_mcp_tools


def _get_tool(tools: list, name: str):
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(f"Tool '{name}' not found in tool list")


# =============================================================================
# McpService path coercion
# =============================================================================

class TestMcpServicePathCoercion:
    def test_str_project_dir_does_not_crash_and_yields_paths(self):
        """A string project_dir must be coerced to Path, not divided as a str."""
        from app.core.mcp_service import McpService

        svc = McpService(project_dir="/tmp/some/project")

        assert isinstance(svc.project_dir, Path)
        assert svc.LOCAL_MCP_FILE == Path("/tmp/some/project/.claude/.mcp.json")
        assert svc.PROJECT_MCP_FILE == Path("/tmp/some/project/.mcp.json")

    def test_none_project_dir_leaves_local_scopes_unset(self):
        from app.core.mcp_service import McpService

        svc = McpService(project_dir=None)

        assert svc.project_dir is None
        assert svc.LOCAL_MCP_FILE is None
        assert svc.PROJECT_MCP_FILE is None


# =============================================================================
# listMcpServers wiring
# =============================================================================

class _FakeService:
    def get_all_servers(self):
        return {"user": [], "local": [], "project": []}


class TestListMcpServers:
    @pytest.mark.asyncio
    async def test_list_passes_project_dir_through(self):
        """listMcpServers must hand the project *directory* to get_mcp_service
        and return a valid envelope without crashing."""
        captured = {}

        def fake_get(project_dir=None):
            captured["project_dir"] = project_dir
            return _FakeService()

        # _create_mcp_tools imports get_mcp_service from this module at call
        # time, so patch the source before building the tools.
        with patch("app.core.mcp_service.get_mcp_service", fake_get):
            tools = _create_mcp_tools(is_admin=True, project_dir="/tmp/proj")
            tool = _get_tool(tools, "listMcpServers")
            result = await tool.handler({})

        import json
        body = json.loads(result["content"][0]["text"])
        assert captured["project_dir"] == "/tmp/proj"
        assert body["total"] == 0
        assert set(body["servers"].keys()) == {"user", "local", "project"}
