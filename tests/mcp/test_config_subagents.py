"""
Unit tests for MCP subagent configuration tools.

Tests cover all 5 tools exposed by _create_subagent_tools:
  - listSubagents
  - getSubagent
  - createSubagent
  - updateSubagent
  - deleteSubagent

Tool handlers are invoked directly via tool.handler(args) to avoid
requiring a running MCP server. The database layer is mocked at the
function level (app.db.database.*) so no real DB connection is needed.
"""

import json
import pytest
from unittest.mock import patch

from app.mcp.config_subagents import _create_subagent_tools


# =============================================================================
# Helpers
# =============================================================================

def _get_tool(tools: list, name: str):
    """Return the SdkMcpTool with the given name."""
    for t in tools:
        if t.name == name:
            return t
    raise KeyError(f"Tool '{name}' not found in tool list")


def _parse_result(result: dict) -> dict:
    """Unwrap the MCP tool response envelope to the inner dict."""
    return json.loads(result["content"][0]["text"])


def _make_subagent(**kwargs) -> dict:
    """Return a minimal subagent row dict, overridable via kwargs."""
    defaults = {
        "id": "test-agent",
        "name": "Test Agent",
        "description": "A test agent",
        "prompt": "You are a test agent.",
        "tools": None,
        "model": None,
        "skills": None,
        "memory": None,
        "mcp_servers": None,
        "is_builtin": False,
        "default_name": None,
        "default_description": None,
        "default_prompt": None,
        "default_tools": None,
        "default_model": None,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }
    defaults.update(kwargs)
    return defaults


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def admin_tools():
    """Tool list created with admin=True."""
    return _create_subagent_tools(is_admin=True)


@pytest.fixture
def non_admin_tools():
    """Tool list created with admin=False."""
    return _create_subagent_tools(is_admin=False)


# =============================================================================
# listSubagents
# =============================================================================

class TestListSubagents:
    """Tests for the listSubagents tool."""

    @pytest.mark.asyncio
    async def test_list_subagents_empty(self, admin_tools):
        """Fresh DB → empty list, count=0."""
        tool = _get_tool(admin_tools, "listSubagents")
        with patch("app.db.database.get_all_subagents", return_value=[]):
            result = await tool.handler({})
        data = _parse_result(result)
        assert data["subagents"] == []
        assert data["count"] == 0

    @pytest.mark.asyncio
    async def test_list_subagents_returns_summary_fields(self, admin_tools):
        """Seeded subagent row → all summary fields present in response."""
        subagent = _make_subagent(
            id="my-agent",
            name="My Agent",
            description="Does things",
            is_builtin=True,
            model="claude-opus-4-5",
            memory="project",
        )
        tool = _get_tool(admin_tools, "listSubagents")
        with patch("app.db.database.get_all_subagents", return_value=[subagent]):
            result = await tool.handler({})
        data = _parse_result(result)
        assert data["count"] == 1
        row = data["subagents"][0]
        assert row["id"] == "my-agent"
        assert row["name"] == "My Agent"
        assert row["description"] == "Does things"
        assert row["is_builtin"] is True
        assert row["model"] == "claude-opus-4-5"
        assert row["memory"] == "project"


# =============================================================================
# getSubagent
# =============================================================================

class TestGetSubagent:
    """Tests for the getSubagent tool."""

    @pytest.mark.asyncio
    async def test_get_subagent_not_found(self, admin_tools):
        """Missing ID → error response."""
        tool = _get_tool(admin_tools, "getSubagent")
        with patch("app.db.database.get_subagent", return_value=None):
            result = await tool.handler({"subagent_id": "nonexistent"})
        data = _parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_get_subagent_returns_full_record(self, admin_tools):
        """Seeded row with all optional fields → all fields returned."""
        subagent = _make_subagent(
            id="full-agent",
            tools=["Read", "Grep"],
            skills=["python"],
            mcp_servers=[{"name": "foo"}],
            memory="user",
            model="claude-sonnet-4-5",
        )
        tool = _get_tool(admin_tools, "getSubagent")
        with patch("app.db.database.get_subagent", return_value=subagent):
            result = await tool.handler({"subagent_id": "full-agent"})
        data = _parse_result(result)
        assert data["id"] == "full-agent"
        assert data["tools"] == ["Read", "Grep"]
        assert data["skills"] == ["python"]
        assert data["mcp_servers"] == [{"name": "foo"}]
        assert data["memory"] == "user"


# =============================================================================
# createSubagent
# =============================================================================

class TestCreateSubagent:
    """Tests for the createSubagent tool."""

    @pytest.mark.asyncio
    async def test_create_subagent_minimal(self, admin_tools):
        """name+description+prompt only → kebab ID derived, optional fields absent."""
        created = _make_subagent(
            id="my-new-agent",
            name="My New Agent",
            description="Desc",
            prompt="Prompt",
            tools=None,
            skills=None,
            memory=None,
            mcp_servers=None,
        )
        tool = _get_tool(admin_tools, "createSubagent")
        with patch("app.db.database.get_subagent", return_value=None), \
             patch("app.db.database.create_subagent", return_value=created) as mock_create:
            result = await tool.handler({
                "name": "My New Agent",
                "description": "Desc",
                "prompt": "Prompt",
            })
        data = _parse_result(result)
        assert data["success"] is True
        assert data["subagent_id"] == "my-new-agent"
        assert data["name"] == "My New Agent"
        # create_subagent called with tools=None (inherit-all)
        _, kwargs = mock_create.call_args
        assert kwargs.get("tools") is None
        assert kwargs.get("skills") is None

    @pytest.mark.asyncio
    async def test_create_subagent_full(self, admin_tools):
        """All fields supplied → all stored correctly."""
        created = _make_subagent(
            id="full-agent",
            name="Full Agent",
            description="Full desc",
            prompt="Full prompt",
            tools=["Read", "Grep"],
            skills=["x"],
            memory="project",
            mcp_servers=[{"name": "foo"}],
            model="claude-haiku-4-5",
        )
        tool = _get_tool(admin_tools, "createSubagent")
        with patch("app.db.database.get_subagent", return_value=None), \
             patch("app.db.database.create_subagent", return_value=created) as mock_create:
            result = await tool.handler({
                "name": "Full Agent",
                "description": "Full desc",
                "prompt": "Full prompt",
                "tools": ["Read", "Grep"],
                "skills": ["x"],
                "memory": "project",
                "mcp_servers": [{"name": "foo"}],
                "model": "claude-haiku-4-5",
            })
        data = _parse_result(result)
        assert data["success"] is True
        _, kwargs = mock_create.call_args
        assert kwargs.get("tools") == ["Read", "Grep"]
        assert kwargs.get("skills") == ["x"]
        assert kwargs.get("memory") == "project"
        assert kwargs.get("mcp_servers") == [{"name": "foo"}]
        assert kwargs.get("model") == "claude-haiku-4-5"

    @pytest.mark.asyncio
    async def test_create_subagent_tools_none_is_inherit_all(self, admin_tools):
        """Omitting tools → create_subagent called with tools=None (inherit all)."""
        created = _make_subagent(id="agent-x", tools=None)
        tool = _get_tool(admin_tools, "createSubagent")
        with patch("app.db.database.get_subagent", return_value=None), \
             patch("app.db.database.create_subagent", return_value=created) as mock_create:
            await tool.handler({
                "name": "Agent X",
                "description": "Desc",
                "prompt": "Prompt",
            })
        _, kwargs = mock_create.call_args
        assert kwargs.get("tools") is None

    @pytest.mark.asyncio
    async def test_create_subagent_tools_empty_list_is_no_tools(self, admin_tools):
        """Passing tools=[] → create_subagent called with tools=[] (no tools)."""
        created = _make_subagent(id="agent-y", tools=[])
        tool = _get_tool(admin_tools, "createSubagent")
        with patch("app.db.database.get_subagent", return_value=None), \
             patch("app.db.database.create_subagent", return_value=created) as mock_create:
            await tool.handler({
                "name": "Agent Y",
                "description": "Desc",
                "prompt": "Prompt",
                "tools": [],
            })
        _, kwargs = mock_create.call_args
        assert kwargs.get("tools") == []

    @pytest.mark.asyncio
    async def test_create_subagent_duplicate_id_rejected(self, admin_tools):
        """Duplicate name → ID collision → error response."""
        existing = _make_subagent(id="my-agent", name="My Agent")
        tool = _get_tool(admin_tools, "createSubagent")
        with patch("app.db.database.get_subagent", return_value=existing):
            result = await tool.handler({
                "name": "My Agent",
                "description": "Desc",
                "prompt": "Prompt",
            })
        data = _parse_result(result)
        assert data["success"] is False
        assert "already exists" in data["error"]

    @pytest.mark.asyncio
    async def test_create_subagent_requires_admin(self, non_admin_tools):
        """Non-admin session → error response."""
        tool = _get_tool(non_admin_tools, "createSubagent")
        result = await tool.handler({
            "name": "Agent",
            "description": "Desc",
            "prompt": "Prompt",
        })
        data = _parse_result(result)
        assert data["success"] is False
        assert "admin" in data["error"].lower()


# =============================================================================
# updateSubagent
# =============================================================================

class TestUpdateSubagent:
    """Tests for the updateSubagent tool."""

    @pytest.mark.asyncio
    async def test_update_subagent_partial_only_prompt(self, admin_tools):
        """Update only prompt → name/description unchanged in response."""
        original = _make_subagent(
            id="agent-1",
            name="A",
            description="Desc",
            prompt="Old",
        )
        updated = _make_subagent(
            id="agent-1",
            name="A",
            description="Desc",
            prompt="new",
        )
        tool = _get_tool(admin_tools, "updateSubagent")
        with patch("app.db.database.get_subagent", return_value=original), \
             patch("app.db.database.update_subagent", return_value=updated) as mock_update:
            result = await tool.handler({
                "subagent_id": "agent-1",
                "prompt": "new",
            })
        data = _parse_result(result)
        assert data["success"] is True
        assert data["updated_fields"] == ["prompt"]
        # Only the supplied key should appear in kwargs. Omitted keys fall
        # through to the DB layer's Ellipsis sentinel default.
        _, kwargs = mock_update.call_args
        assert kwargs == {"prompt": "new"}

    @pytest.mark.asyncio
    async def test_update_subagent_clears_tools_with_null(self, admin_tools):
        """Update tools=None (explicit null) → update_subagent called with tools=None."""
        original = _make_subagent(id="agent-2", tools=["Read"])
        updated = _make_subagent(id="agent-2", tools=None)
        tool = _get_tool(admin_tools, "updateSubagent")
        with patch("app.db.database.get_subagent", return_value=original), \
             patch("app.db.database.update_subagent", return_value=updated) as mock_update:
            result = await tool.handler({
                "subagent_id": "agent-2",
                "tools": None,
            })
        data = _parse_result(result)
        assert data["success"] is True
        _, kwargs = mock_update.call_args
        assert kwargs["tools"] is None

    @pytest.mark.asyncio
    async def test_update_subagent_sets_empty_tools(self, admin_tools):
        """Update tools=[] → update_subagent called with tools=[]."""
        original = _make_subagent(id="agent-3", tools=None)
        updated = _make_subagent(id="agent-3", tools=[])
        tool = _get_tool(admin_tools, "updateSubagent")
        with patch("app.db.database.get_subagent", return_value=original), \
             patch("app.db.database.update_subagent", return_value=updated) as mock_update:
            result = await tool.handler({
                "subagent_id": "agent-3",
                "tools": [],
            })
        data = _parse_result(result)
        assert data["success"] is True
        _, kwargs = mock_update.call_args
        assert kwargs["tools"] == []

    @pytest.mark.asyncio
    async def test_update_subagent_not_found(self, admin_tools):
        """Missing subagent ID → error response."""
        tool = _get_tool(admin_tools, "updateSubagent")
        with patch("app.db.database.get_subagent", return_value=None):
            result = await tool.handler({
                "subagent_id": "ghost",
                "prompt": "new",
            })
        data = _parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_update_subagent_no_fields_returns_error(self, admin_tools):
        """Only subagent_id provided, no updatable fields → error."""
        original = _make_subagent(id="agent-4")
        tool = _get_tool(admin_tools, "updateSubagent")
        with patch("app.db.database.get_subagent", return_value=original):
            result = await tool.handler({"subagent_id": "agent-4"})
        data = _parse_result(result)
        assert data["success"] is False
        assert "no fields" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_update_subagent_requires_admin(self, non_admin_tools):
        """Non-admin session → error response."""
        tool = _get_tool(non_admin_tools, "updateSubagent")
        result = await tool.handler({
            "subagent_id": "agent-5",
            "prompt": "new",
        })
        data = _parse_result(result)
        assert data["success"] is False
        assert "admin" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_update_subagent_allows_builtin(self, admin_tools):
        """Built-in subagents remain editable via MCP (mirrors REST behavior)."""
        builtin = _make_subagent(id="builtin-test", name="Built-in Test", is_builtin=True)
        updated_row = _make_subagent(id="builtin-test", name="Built-in Test", prompt="new", is_builtin=True)
        tool = _get_tool(admin_tools, "updateSubagent")
        with patch("app.db.database.get_subagent", return_value=builtin), \
             patch("app.db.database.update_subagent", return_value=updated_row) as mock_update:
            result = await tool.handler({
                "subagent_id": "builtin-test",
                "prompt": "new",
            })
        data = _parse_result(result)
        assert data["success"] is True
        assert data["updated_fields"] == ["prompt"]
        # Confirm the DB layer was reached
        mock_update.assert_called_once()


# =============================================================================
# deleteSubagent
# =============================================================================

class TestDeleteSubagent:
    """Tests for the deleteSubagent tool."""

    @pytest.mark.asyncio
    async def test_delete_subagent_requires_confirm(self, admin_tools):
        """confirm=False or missing → error response."""
        tool = _get_tool(admin_tools, "deleteSubagent")
        # confirm=False
        result = await tool.handler({"subagent_id": "agent-1", "confirm": False})
        data = _parse_result(result)
        assert data["success"] is False
        assert "confirm" in data["error"].lower()

        # confirm not supplied
        result2 = await tool.handler({"subagent_id": "agent-1"})
        data2 = _parse_result(result2)
        assert data2["success"] is False
        assert "confirm" in data2["error"].lower()

    @pytest.mark.asyncio
    async def test_delete_subagent_not_found(self, admin_tools):
        """Missing subagent → error response."""
        tool = _get_tool(admin_tools, "deleteSubagent")
        with patch("app.db.database.get_subagent", return_value=None):
            result = await tool.handler({"subagent_id": "ghost", "confirm": True})
        data = _parse_result(result)
        assert data["success"] is False
        assert "not found" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_delete_subagent_rejects_builtin(self, admin_tools):
        """Built-in subagent → error mentions built-in."""
        builtin = _make_subagent(id="builtin-agent", is_builtin=True)
        tool = _get_tool(admin_tools, "deleteSubagent")
        with patch("app.db.database.get_subagent", return_value=builtin):
            result = await tool.handler({"subagent_id": "builtin-agent", "confirm": True})
        data = _parse_result(result)
        assert data["success"] is False
        assert "built-in" in data["error"].lower()

    @pytest.mark.asyncio
    async def test_delete_subagent_success(self, admin_tools):
        """Successful deletion → success response."""
        subagent = _make_subagent(id="del-agent", name="Delete Me")
        tool = _get_tool(admin_tools, "deleteSubagent")
        with patch("app.db.database.get_subagent", return_value=subagent), \
             patch("app.db.database.delete_subagent", return_value=True):
            result = await tool.handler({"subagent_id": "del-agent", "confirm": True})
        data = _parse_result(result)
        assert data["success"] is True
        assert data["subagent_id"] == "del-agent"

    @pytest.mark.asyncio
    async def test_delete_subagent_requires_admin(self, non_admin_tools):
        """Non-admin session → error response."""
        tool = _get_tool(non_admin_tools, "deleteSubagent")
        result = await tool.handler({"subagent_id": "agent-x", "confirm": True})
        data = _parse_result(result)
        assert data["success"] is False
        assert "admin" in data["error"].lower()


# =============================================================================
# Module structure tests
# =============================================================================

class TestModuleStructure:
    """Verify tool factory output structure."""

    def test_factory_returns_five_tools(self, admin_tools):
        """_create_subagent_tools should return exactly 5 tools."""
        assert len(admin_tools) == 5

    def test_tool_names(self, admin_tools):
        """All expected tool names should be present."""
        names = {t.name for t in admin_tools}
        assert names == {
            "listSubagents",
            "getSubagent",
            "createSubagent",
            "updateSubagent",
            "deleteSubagent",
        }

    def test_all_tools_have_handlers(self, admin_tools):
        """Every tool must expose a callable handler."""
        for t in admin_tools:
            assert callable(t.handler), f"{t.name} has no callable handler"
