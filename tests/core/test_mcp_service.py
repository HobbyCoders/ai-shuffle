"""
Unit tests for MCP (Model Context Protocol) service module.

Tests cover:
- MCP server configuration CRUD operations
- Multi-scope support (USER, LOCAL, PROJECT)
- Configuration file I/O (.mcp.json)
- Server type validation (stdio, sse, http)
- Error handling and edge cases
"""

import json
import pytest
from pathlib import Path
from unittest.mock import patch

from app.core.mcp_service import (
    McpScope,
    McpServerType,
    McpServerConfig,
    McpService,
    get_mcp_service,
    clear_mcp_service_cache,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_claude_dir(tmp_path):
    """Create a temporary Claude directory structure."""
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(parents=True)
    return claude_dir


@pytest.fixture
def temp_project_dir(tmp_path):
    """Create a temporary project directory."""
    project_dir = tmp_path / "test_project"
    project_dir.mkdir()

    # Create .claude subdirectory for local scope
    (project_dir / ".claude").mkdir()

    return project_dir


@pytest.fixture
def mock_platform():
    """Mock platform detection to return local mode."""
    with patch("app.core.mcp_service.is_docker_mode", return_value=False):
        with patch("app.core.mcp_service.get_claude_credentials_dir") as mock_creds:
            yield mock_creds


@pytest.fixture
def mcp_service(temp_claude_dir, mock_platform):
    """Create an MCP service with mocked paths."""
    mock_platform.return_value = temp_claude_dir
    clear_mcp_service_cache()
    service = McpService()
    return service


@pytest.fixture
def mcp_service_with_project(temp_claude_dir, temp_project_dir, mock_platform):
    """Create an MCP service with project directory."""
    mock_platform.return_value = temp_claude_dir
    clear_mcp_service_cache()
    service = McpService(project_dir=temp_project_dir)
    return service


# =============================================================================
# McpServerConfig Tests
# =============================================================================

class TestMcpServerConfig:
    """Tests for McpServerConfig dataclass."""

    def test_create_stdio_server(self):
        """Test creating a stdio server configuration."""
        config = McpServerConfig(
            name="test-server",
            type=McpServerType.STDIO,
            command="python",
            args=["-m", "test_server"],
            env={"TEST_VAR": "value"},
            scope="user",
            enabled=True
        )

        assert config.name == "test-server"
        assert config.type == McpServerType.STDIO
        assert config.command == "python"
        assert config.args == ["-m", "test_server"]
        assert config.env == {"TEST_VAR": "value"}
        assert config.enabled is True

    def test_create_sse_server(self):
        """Test creating an SSE server configuration."""
        config = McpServerConfig(
            name="remote-server",
            type=McpServerType.SSE,
            url="https://example.com/mcp",
            headers={"Authorization": "Bearer token"},
            scope="user",
            enabled=True
        )

        assert config.name == "remote-server"
        assert config.type == McpServerType.SSE
        assert config.url == "https://example.com/mcp"
        assert config.headers == {"Authorization": "Bearer token"}

    def test_create_http_server(self):
        """Test creating an HTTP server configuration."""
        config = McpServerConfig(
            name="http-server",
            type=McpServerType.HTTP,
            url="http://localhost:8080",
            scope="user",
            enabled=True
        )

        assert config.name == "http-server"
        assert config.type == McpServerType.HTTP
        assert config.url == "http://localhost:8080"

    def test_to_dict(self):
        """Test converting config to dictionary."""
        config = McpServerConfig(
            name="test-server",
            type=McpServerType.STDIO,
            command="python",
            args=["-m", "test"],
            scope="user",
            enabled=True
        )

        d = config.to_dict()

        assert d["name"] == "test-server"
        assert d["type"] == "stdio"
        assert d["command"] == "python"
        assert d["args"] == ["-m", "test"]

    def test_to_mcp_json_format_stdio(self):
        """Test converting stdio config to MCP JSON format."""
        config = McpServerConfig(
            name="test-server",
            type=McpServerType.STDIO,
            command="python",
            args=["-m", "server"],
            env={"KEY": "value"},
            scope="user",
            enabled=True
        )

        mcp_format = config.to_mcp_json_format()

        assert mcp_format["command"] == "python"
        assert mcp_format["args"] == ["-m", "server"]
        assert mcp_format["env"] == {"KEY": "value"}
        assert "name" not in mcp_format  # Name is external
        assert "scope" not in mcp_format  # Scope is external

    def test_to_mcp_json_format_sse(self):
        """Test converting SSE config to MCP JSON format."""
        config = McpServerConfig(
            name="sse-server",
            type=McpServerType.SSE,
            url="https://example.com/mcp",
            headers={"Auth": "token"},
            scope="local",
            enabled=True
        )

        mcp_format = config.to_mcp_json_format()

        assert mcp_format["type"] == "sse"
        assert mcp_format["url"] == "https://example.com/mcp"
        assert mcp_format["headers"] == {"Auth": "token"}


# =============================================================================
# McpService Core Tests
# =============================================================================

class TestMcpServiceCore:
    """Tests for core MCP service functionality."""

    def test_service_initialization(self, mcp_service):
        """Test service initializes correctly."""
        assert mcp_service is not None

    def test_service_with_project(self, mcp_service_with_project, temp_project_dir):
        """Test service initializes with project directory."""
        assert mcp_service_with_project.project_dir == temp_project_dir


# =============================================================================
# User Scope MCP Server Tests
# =============================================================================

class TestUserScopeMcpServers:
    """Tests for USER scope MCP servers."""

    def test_list_empty_servers(self, mcp_service):
        """Test listing when no servers are configured."""
        servers = mcp_service.get_servers_for_scope(McpScope.USER)
        assert servers == []

    def test_add_server(self, mcp_service, temp_claude_dir):
        """Test adding a new MCP server."""
        config = McpServerConfig(
            name="test-server",
            type=McpServerType.STDIO,
            command="python",
            args=["-m", "test"],
            scope="user",
            enabled=True
        )

        result = mcp_service.add_server("test-server", config, McpScope.USER)

        assert result.name == "test-server"

        # Verify file was created
        mcp_file = temp_claude_dir / ".mcp.json"
        assert mcp_file.exists()

        # Verify content
        with open(mcp_file) as f:
            data = json.load(f)

        assert "mcpServers" in data
        assert "test-server" in data["mcpServers"]

    def test_list_after_add(self, mcp_service):
        """Test listing servers after adding one."""
        config = McpServerConfig(
            name="test-server",
            type=McpServerType.STDIO,
            command="echo",
            scope="user",
            enabled=True
        )

        mcp_service.add_server("test-server", config, McpScope.USER)
        servers = mcp_service.get_servers_for_scope(McpScope.USER)

        assert len(servers) == 1
        assert servers[0].name == "test-server"

    def test_remove_server(self, mcp_service):
        """Test removing an MCP server."""
        config = McpServerConfig(
            name="to-remove",
            type=McpServerType.STDIO,
            command="test",
            scope="user",
            enabled=True
        )

        mcp_service.add_server("to-remove", config, McpScope.USER)
        mcp_service.remove_server("to-remove", McpScope.USER)

        servers = mcp_service.get_servers_for_scope(McpScope.USER)
        assert len(servers) == 0

    def test_update_server(self, mcp_service):
        """Test updating an existing server."""
        config = McpServerConfig(
            name="update-me",
            type=McpServerType.STDIO,
            command="old-command",
            scope="user",
            enabled=True
        )

        mcp_service.add_server("update-me", config, McpScope.USER)

        # Update the server with a new config
        updated_config = McpServerConfig(
            name="update-me",
            type=McpServerType.STDIO,
            command="new-command",
            description="Updated",
            scope="user",
            enabled=True
        )
        updated = mcp_service.update_server("update-me", updated_config, McpScope.USER)

        assert updated.command == "new-command"
        assert updated.description == "Updated"


# =============================================================================
# Project Scope MCP Server Tests
# =============================================================================

class TestProjectScopeMcpServers:
    """Tests for PROJECT scope MCP servers."""

    def test_list_empty_project_servers(self, mcp_service_with_project):
        """Test listing when no project servers are configured."""
        servers = mcp_service_with_project.get_servers_for_scope(McpScope.PROJECT)
        assert servers == []

    def test_add_project_server(self, mcp_service_with_project, temp_project_dir):
        """Test adding a server to project scope."""
        config = McpServerConfig(
            name="project-server",
            type=McpServerType.SSE,
            url="https://example.com/mcp",
            scope="project",
            enabled=True
        )

        result = mcp_service_with_project.add_server("project-server", config, McpScope.PROJECT)

        assert result.name == "project-server"

        # Verify file was created in project root
        mcp_file = temp_project_dir / ".mcp.json"
        assert mcp_file.exists()


class TestLocalScopeMcpServers:
    """Tests for LOCAL scope MCP servers."""

    def test_add_local_server(self, mcp_service_with_project, temp_project_dir):
        """Test adding a server to local scope."""
        config = McpServerConfig(
            name="local-server",
            type=McpServerType.STDIO,
            command="npm",
            args=["run", "mcp-server"],
            scope="local",
            enabled=True
        )

        result = mcp_service_with_project.add_server("local-server", config, McpScope.LOCAL)

        assert result.name == "local-server"

        # Verify file was created in .claude subdirectory
        mcp_file = temp_project_dir / ".claude" / ".mcp.json"
        assert mcp_file.exists()


# =============================================================================
# Service Cache Tests
# =============================================================================

class TestMcpServiceCache:
    """Tests for MCP service caching."""

    def test_get_service_caches(self, mock_platform, temp_claude_dir):
        """Test that get_mcp_service returns cached instance."""
        mock_platform.return_value = temp_claude_dir
        clear_mcp_service_cache()

        service1 = get_mcp_service()
        service2 = get_mcp_service()

        assert service1 is service2

    def test_clear_cache(self, mock_platform, temp_claude_dir):
        """Test that clearing cache returns new instance."""
        mock_platform.return_value = temp_claude_dir
        clear_mcp_service_cache()

        service1 = get_mcp_service()
        clear_mcp_service_cache()
        service2 = get_mcp_service()

        assert service1 is not service2


# =============================================================================
# Error Handling Tests
# =============================================================================

class TestMcpServiceErrors:
    """Tests for error handling in MCP service."""

    def test_remove_nonexistent_server(self, mcp_service):
        """Test removing a server that doesn't exist."""
        with pytest.raises(ValueError, match="not found"):
            mcp_service.remove_server("nonexistent", McpScope.USER)

    def test_update_nonexistent_server(self, mcp_service):
        """Test updating a server that doesn't exist."""
        config = McpServerConfig(
            name="nonexistent",
            type=McpServerType.STDIO,
            command="test",
            scope="user",
            enabled=True
        )
        with pytest.raises(ValueError, match="not found"):
            mcp_service.update_server("nonexistent", config, McpScope.USER)

    def test_add_duplicate_server(self, mcp_service):
        """Test adding a server with duplicate name."""
        config = McpServerConfig(
            name="duplicate",
            type=McpServerType.STDIO,
            command="test",
            scope="user",
            enabled=True
        )

        mcp_service.add_server("duplicate", config, McpScope.USER)

        with pytest.raises(ValueError, match="already exists"):
            mcp_service.add_server("duplicate", config, McpScope.USER)

    def test_project_scope_without_project(self, mcp_service):
        """Test accessing project scope without project directory."""
        config = McpServerConfig(
            name="test",
            type=McpServerType.STDIO,
            command="test",
            scope="project",
            enabled=True
        )

        with pytest.raises(ValueError, match="not available"):
            mcp_service.add_server("test", config, McpScope.PROJECT)


# =============================================================================
# All Scopes Combined Tests
# =============================================================================

class TestAllScopesMcp:
    """Tests for combined scope operations."""

    def test_list_all_scopes(self, mcp_service_with_project):
        """Test listing servers from all scopes."""
        # Add server to each scope
        user_config = McpServerConfig(
            name="user-server",
            type=McpServerType.STDIO,
            command="user-cmd",
            scope="user",
            enabled=True
        )
        local_config = McpServerConfig(
            name="local-server",
            type=McpServerType.STDIO,
            command="local-cmd",
            scope="local",
            enabled=True
        )
        project_config = McpServerConfig(
            name="project-server",
            type=McpServerType.SSE,
            url="https://example.com",
            scope="project",
            enabled=True
        )

        mcp_service_with_project.add_server("user-server", user_config, McpScope.USER)
        mcp_service_with_project.add_server("local-server", local_config, McpScope.LOCAL)
        mcp_service_with_project.add_server("project-server", project_config, McpScope.PROJECT)

        # Get all servers
        all_servers = mcp_service_with_project.get_all_servers()

        assert len(all_servers[McpScope.USER]) == 1
        assert len(all_servers[McpScope.LOCAL]) == 1
        assert len(all_servers[McpScope.PROJECT]) == 1
        assert all_servers[McpScope.USER][0].name == "user-server"
        assert all_servers[McpScope.LOCAL][0].name == "local-server"
        assert all_servers[McpScope.PROJECT][0].name == "project-server"
