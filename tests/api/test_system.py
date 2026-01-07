"""
Comprehensive tests for the system API endpoints.

Tests cover:
- Health check endpoints (/health and /api/v1/health)
- Version endpoint (/api/v1/version)
- Stats endpoint (/api/v1/stats)
- Deployment info endpoint (/api/v1/deployment)
- Workspace configuration endpoints (get, set, validate)
- Tools endpoints (list tools, list builtin tools)
- Error handling
- Edge cases
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from pathlib import Path
import tempfile
import shutil


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory for testing."""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    # Cleanup
    shutil.rmtree(temp_dir, ignore_errors=True)


# =============================================================================
# Module Import Tests
# =============================================================================

class TestSystemModuleImports:
    """Verify system module can be imported correctly."""

    def test_system_module_imports(self):
        """System module should import without errors."""
        from app.api import system
        assert system is not None

    def test_system_router_exists(self):
        """System router should exist."""
        from app.api.system import router
        assert router is not None

    def test_tool_categories_defined(self):
        """Tool categories should be defined."""
        from app.api.system import TOOL_CATEGORIES
        assert TOOL_CATEGORIES is not None
        assert "read_only" in TOOL_CATEGORIES
        assert "edit" in TOOL_CATEGORIES
        assert "execution" in TOOL_CATEGORIES
        assert "mcp" in TOOL_CATEGORIES
        assert "other" in TOOL_CATEGORIES

    def test_builtin_tools_defined(self):
        """Builtin tools should be defined."""
        from app.api.system import BUILTIN_TOOLS
        assert BUILTIN_TOOLS is not None
        assert len(BUILTIN_TOOLS) > 0
        # Verify structure of first tool
        first_tool = BUILTIN_TOOLS[0]
        assert "name" in first_tool
        assert "category" in first_tool
        assert "description" in first_tool


# =============================================================================
# Pydantic Model Tests
# =============================================================================

class TestSystemModels:
    """Test Pydantic models for system endpoints."""

    def test_workspace_config_response_model(self):
        """WorkspaceConfigResponse model should be valid."""
        from app.api.system import WorkspaceConfigResponse
        response = WorkspaceConfigResponse(
            workspace_path="/workspace",
            is_configured=True,
            is_local_mode=True,
            default_path="/default",
            exists=True
        )
        assert response.workspace_path == "/workspace"
        assert response.is_configured is True
        assert response.is_local_mode is True
        assert response.default_path == "/default"
        assert response.exists is True

    def test_workspace_config_request_model(self):
        """WorkspaceConfigRequest model should be valid."""
        from app.api.system import WorkspaceConfigRequest
        request = WorkspaceConfigRequest(workspace_path="/new/path")
        assert request.workspace_path == "/new/path"

    def test_tool_info_model(self):
        """ToolInfo model should be valid."""
        from app.api.system import ToolInfo
        tool = ToolInfo(
            name="TestTool",
            category="read_only",
            description="A test tool"
        )
        assert tool.name == "TestTool"
        assert tool.category == "read_only"
        assert tool.description == "A test tool"
        assert tool.mcp_server is None

    def test_tool_info_with_mcp_server(self):
        """ToolInfo model should accept mcp_server."""
        from app.api.system import ToolInfo
        tool = ToolInfo(
            name="McpTool",
            category="mcp",
            description="An MCP tool",
            mcp_server="test-server"
        )
        assert tool.mcp_server == "test-server"

    def test_tool_category_model(self):
        """ToolCategory model should be valid."""
        from app.api.system import ToolCategory, ToolInfo
        category = ToolCategory(
            id="read_only",
            name="Read-only tools",
            description="Tools that only read data",
            tools=[
                ToolInfo(name="Read", category="read_only", description="Read files")
            ]
        )
        assert category.id == "read_only"
        assert len(category.tools) == 1

    def test_tools_response_model(self):
        """ToolsResponse model should be valid."""
        from app.api.system import ToolsResponse, ToolCategory, ToolInfo
        tool = ToolInfo(name="Read", category="read_only", description="Read files")
        category = ToolCategory(
            id="read_only",
            name="Read-only",
            description="Read tools",
            tools=[tool]
        )
        response = ToolsResponse(categories=[category], all_tools=[tool])
        assert len(response.categories) == 1
        assert len(response.all_tools) == 1


# =============================================================================
# Health Check Function Tests
# =============================================================================

class TestHealthCheckFunction:
    """Test health_check function directly."""

    @pytest.mark.asyncio
    async def test_health_check_returns_healthy(self):
        """Should return healthy status."""
        from app.api.system import health_check

        with patch("app.api.system.auth_service") as mock_auth, \
             patch("app.api.system.settings") as mock_settings:
            mock_auth.is_setup_required.return_value = False
            mock_auth.is_claude_authenticated.return_value = True
            mock_settings.service_name = "ai-shuffle"
            mock_settings.version = "4.0.0"

            # Create a mock request
            mock_request = MagicMock()
            mock_request.state.is_admin = False

            result = await health_check()

            assert result["status"] == "healthy"
            assert result["service"] == "ai-shuffle"
            assert result["version"] == "4.0.0"

    @pytest.mark.asyncio
    async def test_health_check_shows_setup_required(self):
        """Should show setup_required when admin not configured."""
        from app.api.system import health_check

        with patch("app.api.system.auth_service") as mock_auth, \
             patch("app.api.system.settings") as mock_settings:
            mock_auth.is_setup_required.return_value = True
            mock_auth.is_claude_authenticated.return_value = False
            mock_settings.service_name = "ai-shuffle"
            mock_settings.version = "4.0.0"

            mock_request = MagicMock()
            mock_request.state.is_admin = False

            result = await health_check()

            assert result["setup_required"] is True
            assert result["claude_authenticated"] is False


# =============================================================================
# Version Function Tests
# =============================================================================

class TestVersionFunction:
    """Test get_version function directly."""

    @pytest.mark.asyncio
    async def test_get_version_with_claude(self):
        """Should return API and Claude versions."""
        from app.api.system import get_version

        with patch("app.api.system.settings") as mock_settings, \
             patch("app.api.system.subprocess.run") as mock_run:
            mock_settings.version = "4.0.0"
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Claude 1.2.3\n"
            )

            result = await get_version()

            assert result["api_version"] == "4.0.0"
            assert result["claude_version"] == "Claude 1.2.3"

    @pytest.mark.asyncio
    async def test_get_version_claude_not_installed(self):
        """Should handle Claude not installed."""
        from app.api.system import get_version

        with patch("app.api.system.settings") as mock_settings, \
             patch("app.api.system.subprocess.run") as mock_run:
            mock_settings.version = "4.0.0"
            mock_run.return_value = MagicMock(returncode=1, stdout="")

            result = await get_version()

            assert result["api_version"] == "4.0.0"
            assert result["claude_version"] is None

    @pytest.mark.asyncio
    async def test_get_version_subprocess_exception(self):
        """Should handle subprocess exception gracefully."""
        from app.api.system import get_version

        with patch("app.api.system.settings") as mock_settings, \
             patch("app.api.system.subprocess.run") as mock_run:
            mock_settings.version = "4.0.0"
            mock_run.side_effect = Exception("Command not found")

            result = await get_version()

            assert result["api_version"] == "4.0.0"
            assert result["claude_version"] is None


# =============================================================================
# Stats Function Tests
# =============================================================================

class TestStatsFunction:
    """Test get_stats function directly."""

    @pytest.mark.asyncio
    async def test_get_stats_success(self):
        """Should return usage statistics."""
        from app.api.system import get_stats

        with patch("app.api.system.database") as mock_db:
            mock_db.get_usage_stats.return_value = {
                "total_sessions": 100,
                "total_queries": 500,
                "total_cost_usd": 15.50,
                "total_tokens_in": 500000,
                "total_tokens_out": 250000
            }

            result = await get_stats(token="test-token")

            assert result["total_sessions"] == 100
            assert result["total_queries"] == 500
            assert result["total_cost_usd"] == 15.50

    @pytest.mark.asyncio
    async def test_get_stats_empty_stats(self):
        """Should handle empty statistics."""
        from app.api.system import get_stats

        with patch("app.api.system.database") as mock_db:
            mock_db.get_usage_stats.return_value = {
                "total_sessions": 0,
                "total_queries": 0,
                "total_cost_usd": 0.0,
                "total_tokens_in": 0,
                "total_tokens_out": 0
            }

            result = await get_stats(token="test-token")

            assert result["total_sessions"] == 0
            assert result["total_queries"] == 0


# =============================================================================
# Deployment Info Function Tests
# =============================================================================

class TestDeploymentInfoFunction:
    """Test get_deployment_info function directly."""

    @pytest.mark.asyncio
    async def test_get_deployment_info_success(self):
        """Should return deployment information."""
        from app.api.system import get_deployment_info

        with patch("app.api.system.settings") as mock_settings:
            mock_settings.get_deployment_info.return_value = {
                "mode": "local",
                "platform": "windows",
                "data_dir": "C:/Users/test/.ai-shuffle",
                "workspace_dir": "C:/Users/test/Documents/ai-shuffle-workspace"
            }

            result = await get_deployment_info(token="test-token")

            assert result["mode"] == "local"
            assert result["platform"] == "windows"


# =============================================================================
# Workspace Config GET Function Tests
# =============================================================================

class TestGetWorkspaceConfigFunction:
    """Test get_workspace_config function directly."""

    @pytest.mark.asyncio
    async def test_get_workspace_config_not_configured(self):
        """Should return unconfigured state when no custom path set."""
        from app.api.system import get_workspace_config

        with patch("app.api.system.database") as mock_db, \
             patch("app.api.system.settings") as mock_settings, \
             patch("app.core.platform.get_default_workspace_dir") as mock_default, \
             patch("app.core.platform.is_local_mode") as mock_local:

            mock_db.get_system_setting.return_value = None
            mock_settings.effective_workspace_dir = Path("/workspace")
            mock_default.return_value = Path("/default/workspace")
            mock_local.return_value = True

            result = await get_workspace_config()

            assert result.is_configured is False
            assert result.is_local_mode is True

    @pytest.mark.asyncio
    async def test_get_workspace_config_configured(self):
        """Should return configured state when custom path is set."""
        from app.api.system import get_workspace_config

        with patch("app.api.system.database") as mock_db, \
             patch("app.api.system.settings") as mock_settings, \
             patch("app.core.platform.get_default_workspace_dir") as mock_default, \
             patch("app.core.platform.is_local_mode") as mock_local:

            mock_db.get_system_setting.return_value = "/custom/workspace"
            mock_settings.effective_workspace_dir = Path("/workspace")
            mock_default.return_value = Path("/default/workspace")
            mock_local.return_value = True

            result = await get_workspace_config()

            assert result.is_configured is True
            assert result.workspace_path == "/custom/workspace"


# =============================================================================
# Workspace Config POST Function Tests
# =============================================================================

class TestSetWorkspaceConfigFunction:
    """Test set_workspace_config function directly."""

    @pytest.mark.asyncio
    async def test_set_workspace_config_success(self, temp_workspace):
        """Should set workspace path successfully."""
        from app.api.system import set_workspace_config, WorkspaceConfigRequest

        with patch("app.api.system.database") as mock_db, \
             patch("app.api.system.settings") as mock_settings, \
             patch("app.core.platform.is_local_mode") as mock_local:

            mock_local.return_value = True

            request = WorkspaceConfigRequest(workspace_path=str(temp_workspace))
            result = await set_workspace_config(request, token="test-token")

            assert result.is_configured is True
            assert result.exists is True
            mock_db.set_system_setting.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_workspace_config_empty_path(self):
        """Should reject empty workspace path."""
        from app.api.system import set_workspace_config, WorkspaceConfigRequest
        from fastapi import HTTPException

        with patch("app.core.platform.is_local_mode") as mock_local:
            mock_local.return_value = True

            request = WorkspaceConfigRequest(workspace_path="")

            with pytest.raises(HTTPException) as exc_info:
                await set_workspace_config(request, token="test-token")

            assert exc_info.value.status_code == 400
            assert "empty" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_set_workspace_config_whitespace_path(self):
        """Should reject whitespace-only workspace path."""
        from app.api.system import set_workspace_config, WorkspaceConfigRequest
        from fastapi import HTTPException

        with patch("app.core.platform.is_local_mode") as mock_local:
            mock_local.return_value = True

            request = WorkspaceConfigRequest(workspace_path="   ")

            with pytest.raises(HTTPException) as exc_info:
                await set_workspace_config(request, token="test-token")

            assert exc_info.value.status_code == 400

    @pytest.mark.asyncio
    async def test_set_workspace_config_creates_directory(self, temp_workspace):
        """Should create directory if it doesn't exist."""
        from app.api.system import set_workspace_config, WorkspaceConfigRequest

        new_path = temp_workspace / "new_subdir"

        with patch("app.api.system.database") as mock_db, \
             patch("app.api.system.settings") as mock_settings, \
             patch("app.core.platform.is_local_mode") as mock_local:

            mock_local.return_value = True

            request = WorkspaceConfigRequest(workspace_path=str(new_path))
            result = await set_workspace_config(request, token="test-token")

            assert result.is_configured is True
            assert new_path.exists()

    @pytest.mark.asyncio
    async def test_set_workspace_config_permission_error(self, temp_workspace):
        """Should handle permission errors gracefully."""
        from app.api.system import set_workspace_config, WorkspaceConfigRequest
        from fastapi import HTTPException

        with patch("app.api.system.database") as mock_db, \
             patch("app.core.platform.is_local_mode") as mock_local, \
             patch("pathlib.Path.mkdir") as mock_mkdir:

            mock_local.return_value = True
            mock_mkdir.side_effect = PermissionError("Access denied")

            request = WorkspaceConfigRequest(workspace_path="/restricted/path")

            with pytest.raises(HTTPException) as exc_info:
                await set_workspace_config(request, token="test-token")

            assert exc_info.value.status_code == 400
            assert "permission" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_set_workspace_config_general_error(self, temp_workspace):
        """Should handle general errors gracefully."""
        from app.api.system import set_workspace_config, WorkspaceConfigRequest
        from fastapi import HTTPException

        with patch("app.api.system.database") as mock_db, \
             patch("app.core.platform.is_local_mode") as mock_local, \
             patch("pathlib.Path.mkdir") as mock_mkdir:

            mock_local.return_value = True
            mock_mkdir.side_effect = Exception("Something went wrong")

            request = WorkspaceConfigRequest(workspace_path="/bad/path")

            with pytest.raises(HTTPException) as exc_info:
                await set_workspace_config(request, token="test-token")

            assert exc_info.value.status_code == 400


# =============================================================================
# Workspace Validate Function Tests
# =============================================================================

class TestValidateWorkspacePathFunction:
    """Test validate_workspace_path function directly."""

    @pytest.mark.asyncio
    async def test_validate_workspace_path_empty(self):
        """Should return invalid for empty path."""
        from app.api.system import validate_workspace_path, WorkspaceConfigRequest

        request = WorkspaceConfigRequest(workspace_path="")
        result = await validate_workspace_path(request)

        assert result["valid"] is False
        assert "empty" in result["error"].lower()
        assert result["writable"] is False

    @pytest.mark.asyncio
    async def test_validate_workspace_path_whitespace(self):
        """Should return invalid for whitespace path."""
        from app.api.system import validate_workspace_path, WorkspaceConfigRequest

        request = WorkspaceConfigRequest(workspace_path="   ")
        result = await validate_workspace_path(request)

        assert result["valid"] is False

    @pytest.mark.asyncio
    async def test_validate_workspace_path_existing_writable(self, temp_workspace):
        """Should validate existing writable directory."""
        from app.api.system import validate_workspace_path, WorkspaceConfigRequest

        request = WorkspaceConfigRequest(workspace_path=str(temp_workspace))
        result = await validate_workspace_path(request)

        assert result["valid"] is True
        assert result["exists"] is True
        assert result["writable"] is True
        assert result["error"] is None

    @pytest.mark.asyncio
    async def test_validate_workspace_path_new_in_existing_parent(self, temp_workspace):
        """Should validate new directory in existing parent."""
        from app.api.system import validate_workspace_path, WorkspaceConfigRequest

        new_path = temp_workspace / "new_subdir"
        request = WorkspaceConfigRequest(workspace_path=str(new_path))
        result = await validate_workspace_path(request)

        assert result["valid"] is True
        assert result["exists"] is False
        assert result["writable"] is True

    @pytest.mark.asyncio
    async def test_validate_workspace_path_expands_user(self):
        """Should expand user home directory."""
        from app.api.system import validate_workspace_path, WorkspaceConfigRequest

        request = WorkspaceConfigRequest(workspace_path="~/test_workspace")
        result = await validate_workspace_path(request)

        # Path should be expanded (not contain ~)
        assert "~" not in result["path"]

    @pytest.mark.asyncio
    async def test_validate_workspace_path_resolves_absolute(self, temp_workspace):
        """Should resolve to absolute path."""
        from app.api.system import validate_workspace_path, WorkspaceConfigRequest

        request = WorkspaceConfigRequest(workspace_path=str(temp_workspace))
        result = await validate_workspace_path(request)

        # Path should be absolute
        assert Path(result["path"]).is_absolute()

    @pytest.mark.asyncio
    async def test_validate_workspace_path_permission_denied(self, temp_workspace):
        """Should handle permission denied on existing directory."""
        from app.api.system import validate_workspace_path, WorkspaceConfigRequest

        # Create a directory but mock the write test to fail
        with patch("pathlib.Path.touch") as mock_touch:
            mock_touch.side_effect = PermissionError("Access denied")

            request = WorkspaceConfigRequest(workspace_path=str(temp_workspace))
            result = await validate_workspace_path(request)

            assert result["valid"] is False
            assert result["writable"] is False
            assert "not writable" in result["error"].lower() or result["error"] is not None

    @pytest.mark.asyncio
    async def test_validate_workspace_path_exception_handling(self):
        """Should handle exceptions gracefully."""
        from app.api.system import validate_workspace_path, WorkspaceConfigRequest

        with patch("pathlib.Path.expanduser") as mock_expand:
            mock_expand.side_effect = Exception("Unexpected error")

            request = WorkspaceConfigRequest(workspace_path="/some/path")
            result = await validate_workspace_path(request)

            assert result["valid"] is False
            assert result["error"] is not None

    @pytest.mark.asyncio
    async def test_validate_workspace_path_nonexistent_parent(self):
        """Should handle non-existent parent directory."""
        from app.api.system import validate_workspace_path, WorkspaceConfigRequest

        request = WorkspaceConfigRequest(workspace_path="/definitely/does/not/exist/path")
        result = await validate_workspace_path(request)

        # Should report invalid or show parent error
        assert result["writable"] is False or result["error"] is not None


# =============================================================================
# Tools List Function Tests
# =============================================================================

class TestListAvailableToolsFunction:
    """Test list_available_tools function directly."""

    @pytest.mark.asyncio
    async def test_list_tools_success(self):
        """Should return all available tools organized by category."""
        from app.api.system import list_available_tools

        result = await list_available_tools(token="test-token")

        assert hasattr(result, "categories")
        assert hasattr(result, "all_tools")

        # Should have multiple categories
        assert len(result.categories) > 0

        # Should have tools in all_tools
        assert len(result.all_tools) > 0

    @pytest.mark.asyncio
    async def test_list_tools_categories_structure(self):
        """Should have proper category structure."""
        from app.api.system import list_available_tools

        result = await list_available_tools(token="test-token")

        for category in result.categories:
            assert hasattr(category, "id")
            assert hasattr(category, "name")
            assert hasattr(category, "description")
            assert hasattr(category, "tools")

    @pytest.mark.asyncio
    async def test_list_tools_tool_structure(self):
        """Should have proper tool structure."""
        from app.api.system import list_available_tools

        result = await list_available_tools(token="test-token")

        for tool in result.all_tools:
            assert hasattr(tool, "name")
            assert hasattr(tool, "category")
            assert hasattr(tool, "description")

    @pytest.mark.asyncio
    async def test_list_tools_contains_builtin_tools(self):
        """Should contain all builtin tools."""
        from app.api.system import list_available_tools, BUILTIN_TOOLS

        result = await list_available_tools(token="test-token")

        tool_names = [t.name for t in result.all_tools]

        for builtin_tool in BUILTIN_TOOLS:
            assert builtin_tool["name"] in tool_names


# =============================================================================
# Builtin Tools Function Tests
# =============================================================================

class TestListBuiltinToolsFunction:
    """Test list_builtin_tools function directly."""

    @pytest.mark.asyncio
    async def test_list_builtin_tools_success(self):
        """Should return flat list of builtin tools."""
        from app.api.system import list_builtin_tools

        result = await list_builtin_tools(token="test-token")

        assert isinstance(result, list)
        assert len(result) > 0

    @pytest.mark.asyncio
    async def test_list_builtin_tools_structure(self):
        """Should have proper tool structure."""
        from app.api.system import list_builtin_tools

        result = await list_builtin_tools(token="test-token")

        for tool in result:
            assert hasattr(tool, "name")
            assert hasattr(tool, "category")
            assert hasattr(tool, "description")

    @pytest.mark.asyncio
    async def test_list_builtin_tools_matches_constant(self):
        """Should match BUILTIN_TOOLS constant."""
        from app.api.system import list_builtin_tools, BUILTIN_TOOLS

        result = await list_builtin_tools(token="test-token")

        assert len(result) == len(BUILTIN_TOOLS)

        response_names = {t.name for t in result}
        builtin_names = {t["name"] for t in BUILTIN_TOOLS}

        assert response_names == builtin_names

    @pytest.mark.asyncio
    async def test_list_builtin_tools_contains_expected_tools(self):
        """Should contain known builtin tools."""
        from app.api.system import list_builtin_tools

        expected_tools = ["Read", "Write", "Edit", "Bash", "Glob", "Grep"]

        result = await list_builtin_tools(token="test-token")

        tool_names = [t.name for t in result]

        for expected in expected_tools:
            assert expected in tool_names


# =============================================================================
# Tool Categories Verification Tests
# =============================================================================

class TestToolCategories:
    """Test tool category definitions."""

    def test_all_tool_categories_defined(self):
        """All expected categories should be defined."""
        from app.api.system import TOOL_CATEGORIES

        expected = ["read_only", "edit", "execution", "mcp", "other"]

        for cat in expected:
            assert cat in TOOL_CATEGORIES
            assert "name" in TOOL_CATEGORIES[cat]
            assert "description" in TOOL_CATEGORIES[cat]

    def test_tools_have_valid_categories(self):
        """All builtin tools should have valid categories."""
        from app.api.system import BUILTIN_TOOLS, TOOL_CATEGORIES

        valid_categories = set(TOOL_CATEGORIES.keys())

        for tool in BUILTIN_TOOLS:
            assert tool["category"] in valid_categories, \
                f"Tool {tool['name']} has invalid category {tool['category']}"

    def test_read_only_tools(self):
        """Read-only category should contain expected tools."""
        from app.api.system import BUILTIN_TOOLS

        read_only_tools = [t["name"] for t in BUILTIN_TOOLS if t["category"] == "read_only"]

        expected = ["Read", "Glob", "Grep"]
        for tool in expected:
            assert tool in read_only_tools

    def test_edit_tools(self):
        """Edit category should contain expected tools."""
        from app.api.system import BUILTIN_TOOLS

        edit_tools = [t["name"] for t in BUILTIN_TOOLS if t["category"] == "edit"]

        expected = ["Edit", "Write"]
        for tool in expected:
            assert tool in edit_tools

    def test_execution_tools(self):
        """Execution category should contain expected tools."""
        from app.api.system import BUILTIN_TOOLS

        execution_tools = [t["name"] for t in BUILTIN_TOOLS if t["category"] == "execution"]

        expected = ["Bash", "Task"]
        for tool in expected:
            assert tool in execution_tools


# =============================================================================
# Edge Cases and Error Handling Tests
# =============================================================================

class TestSystemEdgeCases:
    """Test edge cases and error handling."""

    @pytest.mark.asyncio
    async def test_health_check_with_different_service_name(self):
        """Should handle different service names."""
        from app.api.system import health_check

        with patch("app.api.system.settings") as mock_settings, \
             patch("app.api.system.auth_service") as mock_auth:
            mock_settings.service_name = "custom-service"
            mock_settings.version = "1.0.0"
            mock_auth.is_setup_required.return_value = False
            mock_auth.is_claude_authenticated.return_value = True

            mock_request = MagicMock()
            mock_request.state.is_admin = False

            result = await health_check()

            assert result["service"] == "custom-service"

    @pytest.mark.asyncio
    async def test_stats_with_large_numbers(self):
        """Should handle large statistics numbers."""
        from app.api.system import get_stats

        with patch("app.api.system.database") as mock_db:
            mock_db.get_usage_stats.return_value = {
                "total_sessions": 999999999,
                "total_queries": 9999999999,
                "total_cost_usd": 999999.99,
                "total_tokens_in": 99999999999999,
                "total_tokens_out": 99999999999999
            }

            result = await get_stats(token="test-token")

            assert result["total_sessions"] == 999999999

    @pytest.mark.asyncio
    async def test_workspace_validate_with_special_characters(self, temp_workspace):
        """Should handle paths with special characters."""
        from app.api.system import validate_workspace_path, WorkspaceConfigRequest

        special_path = temp_workspace / "path with spaces"
        special_path.mkdir(exist_ok=True)

        request = WorkspaceConfigRequest(workspace_path=str(special_path))
        result = await validate_workspace_path(request)

        assert result["valid"] is True


# =============================================================================
# Integration Tests
# =============================================================================

class TestSystemIntegration:
    """Integration tests for system functions."""

    @pytest.mark.asyncio
    async def test_full_workspace_flow(self, temp_workspace):
        """Test complete workspace configuration flow."""
        from app.api.system import (
            get_workspace_config, set_workspace_config, validate_workspace_path,
            WorkspaceConfigRequest
        )

        with patch("app.api.system.database") as mock_db, \
             patch("app.api.system.settings") as mock_settings, \
             patch("app.core.platform.is_local_mode") as mock_local, \
             patch("app.core.platform.get_default_workspace_dir") as mock_default:

            mock_local.return_value = True
            mock_default.return_value = Path(str(temp_workspace))
            mock_db.get_system_setting.return_value = None
            mock_settings.effective_workspace_dir = Path(str(temp_workspace))

            # 1. Get initial config (unconfigured)
            config = await get_workspace_config()
            assert config.is_configured is False

            # 2. Validate new path
            new_path = temp_workspace / "custom"
            request = WorkspaceConfigRequest(workspace_path=str(new_path))
            validation = await validate_workspace_path(request)
            assert validation["valid"] is True

            # 3. Set the path
            result = await set_workspace_config(request, token="test-token")
            assert result.is_configured is True

    @pytest.mark.asyncio
    async def test_tools_count_matches_builtin(self):
        """Verify tools functions return consistent data."""
        from app.api.system import list_available_tools, list_builtin_tools, BUILTIN_TOOLS

        # Get from both functions
        all_tools_result = await list_available_tools(token="test-token")
        builtin_result = await list_builtin_tools(token="test-token")

        # all_tools should match builtin list
        assert len(all_tools_result.all_tools) == len(builtin_result)
        assert len(all_tools_result.all_tools) == len(BUILTIN_TOOLS)
