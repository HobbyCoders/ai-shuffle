"""
Unit tests for MCP (Model Context Protocol) API routes.

Tests cover:
- Global MCP server CRUD endpoints
- Project-scoped MCP server endpoints
- MCP tools discovery
- Error handling
"""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from unittest.mock import patch, MagicMock

from app.api.mcp import router
from app.api.auth import require_auth, require_admin
from app.core.mcp_service import McpScope, McpServerType, McpServerConfig

@pytest.mark.parametrize("server_type", ["stdio", "sse", "http"])
def test_description_round_trip(tmp_path, monkeypatch, server_type):
    from app.core import mcp_service

    monkeypatch.setattr(mcp_service, "_get_claude_dir", lambda: tmp_path / ".claude")
    monkeypatch.setattr(mcp_service, "_get_user_claude_json", lambda: tmp_path / ".claude.json")
    monkeypatch.setattr("app.api.mcp.get_mcp_service", mcp_service.McpService)
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[require_admin] = lambda: "test-admin"
    application.dependency_overrides[require_auth] = lambda: "test-admin"
    payload = {"name": "round-trip", "type": server_type, "description": "Searchable description"}
    payload.update({"command": "not-started"} if server_type == "stdio" else {"url": "http://127.0.0.1:9/mcp"})
    with TestClient(application) as client:
        created = client.post("/api/v1/mcp/servers", json=payload)
        assert created.status_code == 201
        for description in ["Searchable description", "Updated description", ""]:
            if description != payload["description"]:
                updated = client.put("/api/v1/mcp/servers/round-trip", json={"description": description})
                assert updated.status_code == 200
            servers = client.get("/api/v1/mcp/servers").json()
            assert next(s for s in servers if s["name"] == "round-trip")["description"] == description
            reloaded = mcp_service.McpService().get_server("round-trip", McpScope.USER)
            assert reloaded.description == description
        assert client.put("/api/v1/mcp/servers/round-trip", json={"env": {"TEST": "value"}}).status_code == 200
        assert mcp_service.McpService().get_server("round-trip", McpScope.USER).description == ""



# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_mcp_service():
    """Create a mock MCP service."""
    mock_service = MagicMock()
    # Default to no validation errors
    mock_service.validate_server_config.return_value = []
    return mock_service


@pytest.fixture
def app(mock_mcp_service):
    """Create a FastAPI app with the MCP router and mocked dependencies."""
    app = FastAPI()
    app.include_router(router)

    # Override auth dependencies
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    app.dependency_overrides[require_admin] = lambda: "test-admin-token"

    return app


@pytest.fixture
def client(app, mock_mcp_service):
    """Create a test client with mocked dependencies."""
    with patch("app.api.mcp.get_mcp_service") as mock_get_service:
        mock_get_service.return_value = mock_mcp_service
        with TestClient(app) as test_client:
            yield test_client


# =============================================================================
# Global MCP Server Tests
# =============================================================================

class TestGlobalMcpServers:
    """Tests for global MCP server endpoints."""

    def test_list_global_servers(self, client, mock_mcp_service):
        """Test listing global MCP servers."""
        mock_mcp_service.get_all_servers_with_builtins.return_value = [
            McpServerConfig(
                name="test-server",
                type=McpServerType.STDIO,
                command="test",
                scope="user",
                enabled=True
            )
        ]

        response = client.get("/api/v1/mcp/servers")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["name"] == "test-server"

    def test_add_global_server(self, client, mock_mcp_service):
        """Test adding a global MCP server."""
        mock_mcp_service.add_server.return_value = McpServerConfig(
            name="new-server",
            type=McpServerType.STDIO,
            command="echo",
            scope="user",
            enabled=True
        )

        response = client.post(
            "/api/v1/mcp/servers",
            json={
                "name": "new-server",
                "type": "stdio",
                "command": "echo"
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "new-server"
        mock_mcp_service.add_server.assert_called_once()

    def test_update_global_server(self, client, mock_mcp_service):
        """Test updating a global MCP server."""
        mock_mcp_service.get_server.return_value = McpServerConfig(
            name="server",
            type=McpServerType.STDIO,
            command="old-cmd",
            scope="user",
            enabled=True
        )
        mock_mcp_service.update_server.return_value = McpServerConfig(
            name="server",
            type=McpServerType.STDIO,
            command="new-cmd",
            scope="user",
            enabled=True
        )

        response = client.put(
            "/api/v1/mcp/servers/server",
            json={
                "command": "new-cmd"
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["command"] == "new-cmd"

    def test_remove_global_server(self, client, mock_mcp_service):
        """Test removing a global MCP server."""
        mock_mcp_service.remove_server.return_value = True

        response = client.delete("/api/v1/mcp/servers/test-server")

        assert response.status_code == 204
        mock_mcp_service.remove_server.assert_called_with("test-server", McpScope.USER)


# =============================================================================
# Project MCP Server Tests
# =============================================================================

class TestProjectMcpServers:
    """Tests for project-scoped MCP server endpoints."""

    @pytest.fixture
    def project_client(self, app, mock_mcp_service):
        """Create a test client with mocked MCP service and database."""
        with patch("app.api.mcp.get_mcp_service") as mock_get_mcp:
            # validate_project_access() lives in app.api.common and reads
            # `database` from that module's namespace, so the project lookup
            # must be patched there (not on app.api.mcp).
            with patch("app.api.common.database") as mock_db:
                mock_get_mcp.return_value = mock_mcp_service
                mock_db.get_project.return_value = {
                    "id": "test-project",
                    "path": "/test/project"
                }
                with TestClient(app) as test_client:
                    yield test_client

    def test_list_project_servers(self, project_client, mock_mcp_service):
        """Test listing project MCP servers."""
        mock_mcp_service.get_all_servers.return_value = {
            "user": [],
            "local": [],
            "project": []
        }

        response = project_client.get("/api/v1/projects/test-project/mcp/servers")

        assert response.status_code == 200
        data = response.json()
        assert "user" in data
        assert "local" in data
        assert "project" in data

    def test_add_project_server(self, project_client, mock_mcp_service):
        """Test adding an MCP server to a project."""
        mock_mcp_service.add_server.return_value = McpServerConfig(
            name="project-server",
            type=McpServerType.SSE,
            url="https://example.com/mcp",
            scope="local",
            enabled=True
        )

        response = project_client.post(
            "/api/v1/projects/test-project/mcp/servers",
            json={
                "name": "project-server",
                "type": "sse",
                "url": "https://example.com/mcp",
                "scope": "local"
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "project-server"


# =============================================================================
# MCP Tools Tests
# =============================================================================

class TestMcpTools:
    """Tests for MCP tools discovery endpoints."""

    def test_list_tools_empty(self, client, mock_mcp_service):
        """No MCP server answers ``tools/list`` -> an empty list, not an error."""
        response = client.get("/api/v1/mcp/tools")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        # The in-process groups (harness/ai/...) carry no ``server``, so they
        # must never leak into the MCP-tool listing.
        assert all(entry["server"] for entry in data)

    def test_list_tools_with_tools(self, client, mock_mcp_service):
        """Tools come from the live surface (ruling T1), split back into
        ``server`` + bare tool name."""
        from unittest.mock import AsyncMock

        import app.core.tool_catalog as catalog

        catalog.clear_mcp_cache()
        with patch.object(
            catalog,
            "_configured_mcp_servers",
            new=lambda errors: (None, {"server": object()}),
        ), patch.object(
            catalog,
            "mcp_server_tools",
            new=AsyncMock(
                return_value=(
                    [{"name": "mcp__server__tool1", "description": "One.", "risk": "read"}],
                    None,
                )
            ),
        ):
            response = client.get("/api/v1/mcp/tools")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "tool1"
        assert data[0]["server"] == "server"
        assert data[0]["description"] == "One."


# =============================================================================
# MCP Stats Tests
# =============================================================================

class TestMcpStats:
    """Tests for MCP statistics endpoint."""

    def test_get_stats(self, client, mock_mcp_service):
        """Test getting MCP stats."""
        mock_mcp_service.get_servers_for_scope.return_value = [
            McpServerConfig(name="s1", type=McpServerType.STDIO, command="cmd", scope="user", enabled=True),
            McpServerConfig(name="s2", type=McpServerType.SSE, url="http://x", scope="user", enabled=True),
        ]

        response = client.get("/api/v1/mcp/stats")

        assert response.status_code == 200
        data = response.json()
        assert "global_server_count" in data
        assert data["global_server_count"] == 2


# =============================================================================
# Error Handling Tests
# =============================================================================

class TestMcpApiErrors:
    """Tests for error handling in MCP API."""

    def test_add_server_invalid_type(self, client, mock_mcp_service):
        """Test adding a server with invalid type."""
        response = client.post(
            "/api/v1/mcp/servers",
            json={
                "name": "bad-server",
                "type": "invalid"
            }
        )

        assert response.status_code == 422  # Validation error

    def test_remove_nonexistent_server(self, client, mock_mcp_service):
        """Test removing a server that doesn't exist."""
        mock_mcp_service.remove_server.side_effect = ValueError("Server not found")

        response = client.delete("/api/v1/mcp/servers/nonexistent")

        assert response.status_code == 404


@pytest.mark.parametrize(
    ("name", "detail"),
    [
        ("a" * 65, "Server name must be at most 64 characters"),
        ("bad name!", "Server name must be alphanumeric with hyphens/underscores only"),
        ("", "Server name is required"),
    ],
)
def test_invalid_server_names_are_400_with_the_name_rule(tmp_path, monkeypatch, name, detail):
    """Every name rule, length included, answers the same 400 (not a schema 422)."""
    from app.core import mcp_service

    monkeypatch.setattr(mcp_service, "_get_claude_dir", lambda: tmp_path / ".claude")
    monkeypatch.setattr(mcp_service, "_get_user_claude_json", lambda: tmp_path / ".claude.json")
    monkeypatch.setattr("app.api.mcp.get_mcp_service", mcp_service.McpService)
    application = FastAPI()
    application.include_router(router)
    application.dependency_overrides[require_admin] = lambda: "test-admin"
    with TestClient(application) as client:
        response = client.post("/api/v1/mcp/servers", json={"name": name, "command": "not-started"})
        assert response.status_code == 400
        assert response.json()["detail"] == detail
        assert mcp_service.McpService().get_servers_for_scope(McpScope.USER) == []

        at_limit = client.post("/api/v1/mcp/servers", json={"name": "b" * 64, "command": "not-started"})
        assert at_limit.status_code == 201
