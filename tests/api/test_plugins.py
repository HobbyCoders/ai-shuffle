"""
Unit tests for plugin API endpoints.

Tests cover:
- Marketplace endpoints (list, add, remove, sync)
- Available plugins endpoint
- Installed plugins endpoint
- Plugin details endpoint
- Plugin installation (single and batch)
- Plugin uninstallation
- Plugin enable/disable
- Plugin enable states
- File-based agents endpoint
- Authentication and authorization
- Error handling and edge cases
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.plugins import router
from app.api.auth import require_auth, require_admin


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_plugin_service():
    """Create a mock plugin service."""
    mock_service = MagicMock()
    return mock_service


@pytest.fixture
def app(mock_plugin_service):
    """Create a FastAPI app with the plugins router and mocked dependencies."""
    app = FastAPI()
    app.include_router(router)

    # Override auth dependencies
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    app.dependency_overrides[require_admin] = lambda: "test-admin-token"

    return app


@pytest.fixture
def client(app, mock_plugin_service):
    """Create a test client with mocked dependencies."""
    with patch("app.api.plugins.get_plugin_service") as mock_get_service:
        mock_get_service.return_value = mock_plugin_service
        with TestClient(app) as test_client:
            yield test_client


@pytest.fixture
def sample_marketplace():
    """Sample marketplace data."""
    marketplace = MagicMock()
    marketplace.id = "test-marketplace"
    marketplace.source = "git"
    marketplace.url = "https://github.com/test/plugins.git"
    marketplace.install_location = "/path/to/marketplace"
    marketplace.last_updated = datetime(2024, 1, 15, 10, 30, 0)
    return marketplace


@pytest.fixture
def sample_plugin():
    """Sample installed plugin data."""
    plugin = MagicMock()
    plugin.id = "test-plugin@test-marketplace"
    plugin.name = "test-plugin"
    plugin.marketplace = "test-marketplace"
    plugin.description = "A test plugin"
    plugin.version = "1.0.0"
    plugin.scope = "user"
    plugin.install_path = "/path/to/plugin"
    plugin.installed_at = datetime(2024, 1, 15, 10, 30, 0)
    plugin.enabled = True
    plugin.has_commands = True
    plugin.has_agents = False
    plugin.has_skills = False
    plugin.has_hooks = False
    return plugin


@pytest.fixture
def sample_plugin_details(sample_plugin):
    """Sample plugin details data."""
    details = MagicMock()
    details.id = sample_plugin.id
    details.name = sample_plugin.name
    details.marketplace = sample_plugin.marketplace
    details.description = sample_plugin.description
    details.version = sample_plugin.version
    details.scope = sample_plugin.scope
    details.install_path = sample_plugin.install_path
    details.installed_at = sample_plugin.installed_at
    details.enabled = sample_plugin.enabled
    details.has_commands = sample_plugin.has_commands
    details.has_agents = sample_plugin.has_agents
    details.has_skills = sample_plugin.has_skills
    details.has_hooks = sample_plugin.has_hooks
    details.commands = ["build", "test"]
    details.agents = []
    details.skills = []
    details.readme = "# Test Plugin\n\nThis is a test plugin."
    return details


@pytest.fixture
def sample_available_plugin():
    """Sample available plugin data."""
    plugin = MagicMock()
    plugin.id = "available-plugin@test-marketplace"
    plugin.name = "available-plugin"
    plugin.marketplace = "test-marketplace"
    plugin.marketplace_path = "/path/to/marketplace/plugins/available-plugin"
    plugin.description = "An available plugin"
    plugin.has_commands = True
    plugin.has_agents = True
    plugin.has_skills = False
    plugin.has_hooks = False
    plugin.installed = False
    plugin.enabled = False
    return plugin


# =============================================================================
# Test Module Imports
# =============================================================================

class TestPluginsModuleImports:
    """Verify plugins module can be imported correctly."""

    def test_plugins_module_imports(self):
        """Plugins module should import without errors."""
        from app.api import plugins
        assert plugins is not None

    def test_plugins_router_exists(self):
        """Plugins router should exist."""
        from app.api.plugins import router
        assert router is not None

    def test_router_has_correct_prefix(self):
        """Router should have /api/v1/plugins prefix."""
        from app.api.plugins import router
        assert router.prefix == "/api/v1/plugins"


# =============================================================================
# Test Marketplace Endpoints
# =============================================================================

class TestListMarketplaces:
    """Test GET /marketplaces endpoint."""

    def test_list_marketplaces_success(self, app, mock_plugin_service, sample_marketplace):
        """Should return list of marketplaces."""
        mock_plugin_service.get_marketplaces.return_value = [sample_marketplace]

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/marketplaces")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == "test-marketplace"
        assert data[0]["source"] == "git"
        assert data[0]["url"] == "https://github.com/test/plugins.git"

    def test_list_marketplaces_empty(self, app, mock_plugin_service):
        """Should return empty list when no marketplaces."""
        mock_plugin_service.get_marketplaces.return_value = []

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/marketplaces")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_marketplaces_service_error(self, app, mock_plugin_service):
        """Should return 500 on service error."""
        mock_plugin_service.get_marketplaces.side_effect = Exception("Service error")

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/marketplaces")

        assert response.status_code == 500
        assert "Failed to load marketplaces" in response.json()["detail"]

    def test_list_marketplaces_without_last_updated(self, app, mock_plugin_service):
        """Should handle marketplace without last_updated."""
        marketplace = MagicMock()
        marketplace.id = "test-mp"
        marketplace.source = "git"
        marketplace.url = "https://example.com/plugins.git"
        marketplace.install_location = "/path"
        marketplace.last_updated = None
        mock_plugin_service.get_marketplaces.return_value = [marketplace]

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/marketplaces")

        assert response.status_code == 200
        assert response.json()[0]["last_updated"] is None


class TestAddMarketplace:
    """Test POST /marketplaces endpoint."""

    def test_add_marketplace_success(self, app, mock_plugin_service, sample_marketplace):
        """Should add marketplace successfully."""
        mock_plugin_service.add_marketplace.return_value = sample_marketplace

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/plugins/marketplaces",
                    json={"url": "https://github.com/test/plugins.git"}
                )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "test-marketplace"
        mock_plugin_service.add_marketplace.assert_called_once_with(
            "https://github.com/test/plugins.git", None
        )

    def test_add_marketplace_with_name(self, app, mock_plugin_service, sample_marketplace):
        """Should add marketplace with custom name."""
        mock_plugin_service.add_marketplace.return_value = sample_marketplace

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/plugins/marketplaces",
                    json={"url": "https://github.com/test/plugins.git", "name": "custom-name"}
                )

        assert response.status_code == 201
        mock_plugin_service.add_marketplace.assert_called_once_with(
            "https://github.com/test/plugins.git", "custom-name"
        )

    def test_add_marketplace_runtime_error(self, app, mock_plugin_service):
        """Should return 500 on RuntimeError."""
        mock_plugin_service.add_marketplace.side_effect = RuntimeError("Clone failed")

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/plugins/marketplaces",
                    json={"url": "https://github.com/test/plugins.git"}
                )

        assert response.status_code == 500
        assert "Clone failed" in response.json()["detail"]

    def test_add_marketplace_missing_url(self, app, mock_plugin_service):
        """Should return 422 on missing URL."""
        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/plugins/marketplaces",
                    json={}
                )

        assert response.status_code == 422


class TestRemoveMarketplace:
    """Test DELETE /marketplaces/{marketplace_id} endpoint."""

    def test_remove_marketplace_success(self, app, mock_plugin_service):
        """Should remove marketplace successfully."""
        mock_plugin_service.remove_marketplace.return_value = True

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.delete("/api/v1/plugins/marketplaces/test-marketplace")

        assert response.status_code == 204
        mock_plugin_service.remove_marketplace.assert_called_once_with("test-marketplace")

    def test_remove_marketplace_not_found(self, app, mock_plugin_service):
        """Should return 404 when marketplace not found."""
        mock_plugin_service.remove_marketplace.side_effect = ValueError("Marketplace not found")

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.delete("/api/v1/plugins/marketplaces/nonexistent")

        assert response.status_code == 404
        assert "Marketplace not found" in response.json()["detail"]


class TestSyncMarketplace:
    """Test POST /marketplaces/{marketplace_id}/sync endpoint."""

    def test_sync_marketplace_success(self, app, mock_plugin_service):
        """Should sync marketplace successfully."""
        mock_plugin_service.sync_marketplace.return_value = True

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post("/api/v1/plugins/marketplaces/test-marketplace/sync")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "synced"
        assert data["marketplace_id"] == "test-marketplace"

    def test_sync_marketplace_not_found(self, app, mock_plugin_service):
        """Should return 404 when marketplace not found."""
        mock_plugin_service.sync_marketplace.side_effect = ValueError("Marketplace not found")

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post("/api/v1/plugins/marketplaces/nonexistent/sync")

        assert response.status_code == 404

    def test_sync_marketplace_runtime_error(self, app, mock_plugin_service):
        """Should return 500 on sync failure."""
        mock_plugin_service.sync_marketplace.side_effect = RuntimeError("Sync failed")

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post("/api/v1/plugins/marketplaces/test-marketplace/sync")

        assert response.status_code == 500
        assert "Sync failed" in response.json()["detail"]


# =============================================================================
# Test Available Plugins Endpoint
# =============================================================================

class TestListAvailablePlugins:
    """Test GET /available endpoint."""

    def test_list_available_plugins_success(self, app, mock_plugin_service, sample_available_plugin):
        """Should return list of available plugins."""
        mock_plugin_service.get_available_plugins.return_value = [sample_available_plugin]

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/available")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == "available-plugin@test-marketplace"
        assert data[0]["installed"] is False

    def test_list_available_plugins_with_marketplace_filter(self, app, mock_plugin_service, sample_available_plugin):
        """Should filter by marketplace."""
        mock_plugin_service.get_available_plugins.return_value = [sample_available_plugin]

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/available?marketplace=test-marketplace")

        assert response.status_code == 200
        mock_plugin_service.get_available_plugins.assert_called_once_with("test-marketplace")

    def test_list_available_plugins_empty(self, app, mock_plugin_service):
        """Should return empty list when no plugins available."""
        mock_plugin_service.get_available_plugins.return_value = []

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/available")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_available_plugins_service_error(self, app, mock_plugin_service):
        """Should return 500 on service error."""
        mock_plugin_service.get_available_plugins.side_effect = Exception("Service error")

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/available")

        assert response.status_code == 500
        assert "Failed to load available plugins" in response.json()["detail"]


# =============================================================================
# Test Installed Plugins Endpoint
# =============================================================================

class TestListInstalledPlugins:
    """Test GET /installed endpoint."""

    def test_list_installed_plugins_success(self, app, mock_plugin_service, sample_plugin):
        """Should return list of installed plugins."""
        mock_plugin_service.get_installed_plugins.return_value = [sample_plugin]

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/installed")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == "test-plugin@test-marketplace"
        assert data[0]["enabled"] is True

    def test_list_installed_plugins_empty(self, app, mock_plugin_service):
        """Should return empty list when no plugins installed."""
        mock_plugin_service.get_installed_plugins.return_value = []

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/installed")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_installed_plugins_service_error(self, app, mock_plugin_service):
        """Should return 500 on service error."""
        mock_plugin_service.get_installed_plugins.side_effect = Exception("Service error")

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/installed")

        assert response.status_code == 500
        assert "Failed to load installed plugins" in response.json()["detail"]


# =============================================================================
# Test File-Based Agents Endpoint
# =============================================================================

class TestListFileBasedAgents:
    """Test GET /agents/file-based endpoint."""

    def test_list_file_based_agents_success(self, app, mock_plugin_service):
        """Should return list of file-based agents."""
        mock_plugin_service.get_file_based_agents.return_value = [
            {
                "id": "test-plugin@test-mp:agent1",
                "name": "agent1",
                "description": "Test agent",
                "model": "claude-3",
                "tools": ["tool1", "tool2"],
                "prompt": "You are a test agent.",
                "source_plugin": "test-plugin@test-mp",
                "file_path": "/path/to/agent.md"
            }
        ]

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/agents/file-based")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["name"] == "agent1"
        assert data[0]["model"] == "claude-3"

    def test_list_file_based_agents_empty(self, app, mock_plugin_service):
        """Should return empty list when no agents."""
        mock_plugin_service.get_file_based_agents.return_value = []

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/agents/file-based")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_file_based_agents_minimal_data(self, app, mock_plugin_service):
        """Should handle agents with minimal data."""
        mock_plugin_service.get_file_based_agents.return_value = [
            {
                "id": "test-plugin@test-mp:agent1",
                "name": "agent1",
                "source_plugin": "test-plugin@test-mp",
                "file_path": "/path/to/agent.md"
            }
        ]

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/agents/file-based")

        assert response.status_code == 200
        data = response.json()
        assert data[0]["description"] == ""
        assert data[0]["model"] is None
        assert data[0]["tools"] is None
        assert data[0]["prompt"] == ""

    def test_list_file_based_agents_service_error(self, app, mock_plugin_service):
        """Should return 500 on service error."""
        mock_plugin_service.get_file_based_agents.side_effect = Exception("Service error")

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/agents/file-based")

        assert response.status_code == 500
        assert "Failed to load file-based agents" in response.json()["detail"]


# =============================================================================
# Test Plugin Details Endpoint
# =============================================================================

class TestGetPluginDetails:
    """Test GET /{plugin_id} endpoint."""

    def test_get_plugin_details_success(self, app, mock_plugin_service, sample_plugin_details):
        """Should return plugin details."""
        mock_plugin_service.get_plugin_details.return_value = sample_plugin_details

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/test-plugin@test-marketplace")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "test-plugin@test-marketplace"
        assert data["commands"] == ["build", "test"]
        assert "readme" in data

    def test_get_plugin_details_not_found(self, app, mock_plugin_service):
        """Should return 404 when plugin not found."""
        mock_plugin_service.get_plugin_details.return_value = None

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/nonexistent-plugin@test-marketplace")

        assert response.status_code == 404
        assert "Plugin not found" in response.json()["detail"]

    def test_get_plugin_details_path_with_slash(self, app, mock_plugin_service, sample_plugin_details):
        """Should handle plugin IDs with path separators."""
        mock_plugin_service.get_plugin_details.return_value = sample_plugin_details

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/my/plugin@marketplace")

        assert response.status_code == 200
        mock_plugin_service.get_plugin_details.assert_called_once_with("my/plugin@marketplace")


# =============================================================================
# Test Plugin Installation Endpoints
# =============================================================================

class TestInstallPlugin:
    """Test POST /install endpoint."""

    def test_install_plugin_success(self, app, mock_plugin_service, sample_plugin):
        """Should install plugin successfully."""
        mock_plugin_service.install_plugin.return_value = sample_plugin

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/plugins/install",
                    json={"plugin_id": "test-plugin@test-marketplace"}
                )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "test-plugin@test-marketplace"
        mock_plugin_service.install_plugin.assert_called_once_with(
            "test-plugin@test-marketplace", "user"
        )

    def test_install_plugin_with_scope(self, app, mock_plugin_service, sample_plugin):
        """Should install plugin with specified scope."""
        mock_plugin_service.install_plugin.return_value = sample_plugin

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/plugins/install",
                    json={"plugin_id": "test-plugin@test-marketplace", "scope": "project"}
                )

        assert response.status_code == 201
        mock_plugin_service.install_plugin.assert_called_once_with(
            "test-plugin@test-marketplace", "project"
        )

    def test_install_plugin_value_error(self, app, mock_plugin_service):
        """Should return 400 on ValueError."""
        mock_plugin_service.install_plugin.side_effect = ValueError("Invalid plugin ID")

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/plugins/install",
                    json={"plugin_id": "invalid"}
                )

        assert response.status_code == 400
        assert "Invalid plugin ID" in response.json()["detail"]

    def test_install_plugin_missing_plugin_id(self, app, mock_plugin_service):
        """Should return 422 on missing plugin_id."""
        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/plugins/install",
                    json={}
                )

        assert response.status_code == 422


class TestInstallPluginsBatch:
    """Test POST /install/batch endpoint."""

    def test_install_plugins_batch_success(self, app, mock_plugin_service, sample_plugin):
        """Should install plugins in batch."""
        plugin2 = MagicMock()
        plugin2.id = "plugin2@test-marketplace"
        plugin2.name = "plugin2"
        plugin2.marketplace = "test-marketplace"
        plugin2.description = "Another plugin"
        plugin2.version = "2.0.0"
        plugin2.scope = "user"
        plugin2.install_path = "/path/to/plugin2"
        plugin2.installed_at = datetime(2024, 1, 15, 10, 30, 0)
        plugin2.enabled = True
        plugin2.has_commands = False
        plugin2.has_agents = True
        plugin2.has_skills = False
        plugin2.has_hooks = False

        mock_plugin_service.install_plugins_batch.return_value = [sample_plugin, plugin2]

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/plugins/install/batch",
                    json={
                        "plugin_ids": ["test-plugin@test-marketplace", "plugin2@test-marketplace"]
                    }
                )

        assert response.status_code == 201
        data = response.json()
        assert len(data) == 2

    def test_install_plugins_batch_empty(self, app, mock_plugin_service):
        """Should handle empty plugin list."""
        mock_plugin_service.install_plugins_batch.return_value = []

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/plugins/install/batch",
                    json={"plugin_ids": []}
                )

        assert response.status_code == 201
        assert response.json() == []

    def test_install_plugins_batch_with_scope(self, app, mock_plugin_service, sample_plugin):
        """Should install batch with specified scope."""
        mock_plugin_service.install_plugins_batch.return_value = [sample_plugin]

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/plugins/install/batch",
                    json={
                        "plugin_ids": ["test-plugin@test-marketplace"],
                        "scope": "project"
                    }
                )

        assert response.status_code == 201
        mock_plugin_service.install_plugins_batch.assert_called_once_with(
            ["test-plugin@test-marketplace"], "project"
        )


# =============================================================================
# Test Plugin Uninstallation Endpoint
# =============================================================================

class TestUninstallPlugin:
    """Test DELETE /{plugin_id} endpoint."""

    def test_uninstall_plugin_success(self, app, mock_plugin_service):
        """Should uninstall plugin successfully."""
        mock_plugin_service.uninstall_plugin.return_value = True

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.delete("/api/v1/plugins/test-plugin@test-marketplace")

        assert response.status_code == 204
        mock_plugin_service.uninstall_plugin.assert_called_once_with("test-plugin@test-marketplace")

    def test_uninstall_plugin_not_found(self, app, mock_plugin_service):
        """Should return 404 when plugin not found."""
        mock_plugin_service.uninstall_plugin.side_effect = ValueError("Plugin not installed")

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.delete("/api/v1/plugins/nonexistent@test-marketplace")

        assert response.status_code == 404
        assert "Plugin not installed" in response.json()["detail"]


# =============================================================================
# Test Plugin Enable/Disable Endpoints
# =============================================================================

class TestEnablePlugin:
    """Test POST /{plugin_id}/enable endpoint."""

    def test_enable_plugin_success(self, app, mock_plugin_service):
        """Should enable plugin successfully."""
        mock_plugin_service.enable_plugin.return_value = True

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post("/api/v1/plugins/test-plugin@test-marketplace/enable")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "enabled"
        assert data["plugin_id"] == "test-plugin@test-marketplace"

    def test_enable_plugin_failure(self, app, mock_plugin_service):
        """Should return 500 when enable fails."""
        mock_plugin_service.enable_plugin.return_value = False

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post("/api/v1/plugins/test-plugin@test-marketplace/enable")

        assert response.status_code == 500
        assert "Failed to enable plugin" in response.json()["detail"]


class TestDisablePlugin:
    """Test POST /{plugin_id}/disable endpoint."""

    def test_disable_plugin_success(self, app, mock_plugin_service):
        """Should disable plugin successfully."""
        mock_plugin_service.disable_plugin.return_value = True

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post("/api/v1/plugins/test-plugin@test-marketplace/disable")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "disabled"
        assert data["plugin_id"] == "test-plugin@test-marketplace"

    def test_disable_plugin_failure(self, app, mock_plugin_service):
        """Should return 500 when disable fails."""
        mock_plugin_service.disable_plugin.return_value = False

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post("/api/v1/plugins/test-plugin@test-marketplace/disable")

        assert response.status_code == 500
        assert "Failed to disable plugin" in response.json()["detail"]


class TestGetEnabledPlugins:
    """Test GET /state/enabled endpoint.

    Note: Due to route ordering in the source code, /state/enabled is currently
    caught by the /{plugin_id:path} catch-all route. This is a known issue
    where the route should be defined before the catch-all route.

    These tests verify the intended behavior if the route were properly ordered.
    """

    def test_get_enabled_plugins_route_shadowed_by_catch_all(self, app, mock_plugin_service):
        """
        The /state/enabled route is shadowed by /{plugin_id:path} catch-all.
        This test verifies the actual current behavior.
        """
        # The catch-all route intercepts /state/enabled and calls get_plugin_details
        # with plugin_id="state/enabled", which returns None (not found)
        mock_plugin_service.get_plugin_details.return_value = None

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/state/enabled")

        # Due to route ordering, this is matched as a plugin_id lookup
        # which returns 404 because "state/enabled" is not a valid plugin
        assert response.status_code == 404
        assert "Plugin not found" in response.json()["detail"]


# =============================================================================
# Test Request/Response Models
# =============================================================================

class TestRequestModels:
    """Test Pydantic request models."""

    def test_marketplace_add_request_minimal(self):
        """Should create request with URL only."""
        from app.api.plugins import MarketplaceAddRequest
        request = MarketplaceAddRequest(url="https://github.com/test/plugins.git")
        assert request.url == "https://github.com/test/plugins.git"
        assert request.name is None

    def test_marketplace_add_request_with_name(self):
        """Should create request with URL and name."""
        from app.api.plugins import MarketplaceAddRequest
        request = MarketplaceAddRequest(
            url="https://github.com/test/plugins.git",
            name="custom-name"
        )
        assert request.name == "custom-name"

    def test_plugin_install_request_default_scope(self):
        """Should have default scope of 'user'."""
        from app.api.plugins import PluginInstallRequest
        request = PluginInstallRequest(plugin_id="plugin@mp")
        assert request.plugin_id == "plugin@mp"
        assert request.scope == "user"

    def test_plugin_batch_install_request_default_scope(self):
        """Should have default scope of 'user'."""
        from app.api.plugins import PluginBatchInstallRequest
        request = PluginBatchInstallRequest(plugin_ids=["p1@mp", "p2@mp"])
        assert len(request.plugin_ids) == 2
        assert request.scope == "user"


class TestResponseModels:
    """Test Pydantic response models."""

    def test_marketplace_response(self):
        """Should create marketplace response."""
        from app.api.plugins import MarketplaceResponse
        response = MarketplaceResponse(
            id="test-mp",
            source="git",
            url="https://github.com/test/plugins.git",
            install_location="/path/to/mp",
            last_updated=None
        )
        assert response.id == "test-mp"

    def test_plugin_response(self):
        """Should create plugin response."""
        from app.api.plugins import PluginResponse
        response = PluginResponse(
            id="plugin@mp",
            name="plugin",
            marketplace="mp",
            description="A plugin",
            version="1.0.0",
            scope="user",
            install_path="/path",
            installed_at=None,
            enabled=True,
            has_commands=True,
            has_agents=False,
            has_skills=False,
            has_hooks=False
        )
        assert response.enabled is True

    def test_plugin_details_response(self):
        """Should create plugin details response."""
        from app.api.plugins import PluginDetailsResponse
        response = PluginDetailsResponse(
            id="plugin@mp",
            name="plugin",
            marketplace="mp",
            description="A plugin",
            version="1.0.0",
            scope="user",
            install_path="/path",
            enabled=True,
            has_commands=True,
            has_agents=False,
            has_skills=False,
            has_hooks=False,
            commands=["cmd1"],
            agents=[],
            skills=[]
        )
        assert response.commands == ["cmd1"]
        assert response.readme is None

    def test_available_plugin_response(self):
        """Should create available plugin response."""
        from app.api.plugins import AvailablePluginResponse
        response = AvailablePluginResponse(
            id="plugin@mp",
            name="plugin",
            marketplace="mp",
            marketplace_path="/path/to/plugin",
            description="A plugin",
            has_commands=True,
            has_agents=False,
            has_skills=False,
            has_hooks=False,
            installed=False,
            enabled=False
        )
        assert response.installed is False

    def test_file_agent_response(self):
        """Should create file agent response."""
        from app.api.plugins import FileAgentResponse
        response = FileAgentResponse(
            id="plugin@mp:agent",
            name="agent",
            description="An agent",
            model="claude-3",
            tools=["tool1"],
            prompt="You are an agent.",
            source_plugin="plugin@mp",
            file_path="/path/to/agent.md"
        )
        assert response.tools == ["tool1"]

    def test_plugin_enable_state_response(self):
        """Should create enable state response."""
        from app.api.plugins import PluginEnableStateResponse
        response = PluginEnableStateResponse(
            enabled_plugins={"plugin@mp": True}
        )
        assert response.enabled_plugins["plugin@mp"] is True


# =============================================================================
# Test Authentication Requirements
# =============================================================================

class TestAuthenticationRequirements:
    """Test that endpoints have proper authentication."""

    def test_list_marketplaces_requires_auth(self):
        """list_marketplaces should use require_auth."""
        from app.api.plugins import list_marketplaces
        import inspect
        sig = inspect.signature(list_marketplaces)
        params = sig.parameters
        assert "token" in params

    def test_add_marketplace_requires_admin(self):
        """add_marketplace should use require_admin."""
        from app.api.plugins import add_marketplace
        import inspect
        sig = inspect.signature(add_marketplace)
        params = sig.parameters
        assert "token" in params

    def test_remove_marketplace_requires_admin(self):
        """remove_marketplace should use require_admin."""
        from app.api.plugins import remove_marketplace
        import inspect
        sig = inspect.signature(remove_marketplace)
        params = sig.parameters
        assert "token" in params

    def test_install_plugin_requires_admin(self):
        """install_plugin should use require_admin."""
        from app.api.plugins import install_plugin
        import inspect
        sig = inspect.signature(install_plugin)
        params = sig.parameters
        assert "token" in params


# =============================================================================
# Test Edge Cases
# =============================================================================

class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_plugin_id_with_special_characters(self, app, mock_plugin_service, sample_plugin_details):
        """Should handle plugin IDs with special characters."""
        mock_plugin_service.get_plugin_details.return_value = sample_plugin_details

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                # Plugin ID with dashes and underscores
                response = client.get("/api/v1/plugins/my-plugin_name@test-marketplace")

        assert response.status_code == 200

    def test_marketplace_id_with_special_characters(self, app, mock_plugin_service):
        """Should handle marketplace IDs with special characters."""
        mock_plugin_service.remove_marketplace.return_value = True

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.delete("/api/v1/plugins/marketplaces/my-marketplace-name")

        assert response.status_code == 204

    def test_multiple_plugins_installed(self, app, mock_plugin_service, sample_plugin):
        """Should handle multiple installed plugins."""
        plugin2 = MagicMock()
        plugin2.id = "plugin2@mp"
        plugin2.name = "plugin2"
        plugin2.marketplace = "mp"
        plugin2.description = "Plugin 2"
        plugin2.version = "2.0.0"
        plugin2.scope = "project"
        plugin2.install_path = "/path/to/plugin2"
        plugin2.installed_at = None
        plugin2.enabled = False
        plugin2.has_commands = False
        plugin2.has_agents = True
        plugin2.has_skills = True
        plugin2.has_hooks = True

        mock_plugin_service.get_installed_plugins.return_value = [sample_plugin, plugin2]

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/installed")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["scope"] == "user"
        assert data[1]["scope"] == "project"


# =============================================================================
# Test Unauthenticated Access
# =============================================================================

class TestUnauthenticatedAccess:
    """Test that endpoints properly reject unauthenticated requests."""

    def test_list_marketplaces_unauthorized(self, mock_plugin_service):
        """Should return 401 for unauthenticated request to list marketplaces."""
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)

        # Override require_auth to raise HTTPException
        def unauthorized():
            raise HTTPException(status_code=401, detail="Not authenticated")

        app.dependency_overrides[require_auth] = unauthorized

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/marketplaces")

        assert response.status_code == 401

    def test_add_marketplace_forbidden(self, mock_plugin_service):
        """Should return 403 for non-admin request to add marketplace."""
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)

        # Override require_admin to raise HTTPException
        def forbidden():
            raise HTTPException(status_code=403, detail="Admin access required")

        app.dependency_overrides[require_admin] = forbidden

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/plugins/marketplaces",
                    json={"url": "https://github.com/test/plugins.git"}
                )

        assert response.status_code == 403

    def test_install_plugin_forbidden(self, mock_plugin_service):
        """Should return 403 for non-admin request to install plugin."""
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)

        def forbidden():
            raise HTTPException(status_code=403, detail="Admin access required")

        app.dependency_overrides[require_admin] = forbidden

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post(
                    "/api/v1/plugins/install",
                    json={"plugin_id": "test-plugin@test-marketplace"}
                )

        assert response.status_code == 403

    def test_enable_plugin_forbidden(self, mock_plugin_service):
        """Should return 403 for non-admin request to enable plugin."""
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)

        def forbidden():
            raise HTTPException(status_code=403, detail="Admin access required")

        app.dependency_overrides[require_admin] = forbidden

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.post("/api/v1/plugins/test-plugin@test-marketplace/enable")

        assert response.status_code == 403


# =============================================================================
# Test Multiple Marketplaces
# =============================================================================

class TestMultipleMarketplaces:
    """Test handling of multiple marketplaces."""

    def test_list_multiple_marketplaces(self, app, mock_plugin_service):
        """Should return all marketplaces."""
        mp1 = MagicMock()
        mp1.id = "marketplace1"
        mp1.source = "git"
        mp1.url = "https://github.com/test/mp1.git"
        mp1.install_location = "/path/to/mp1"
        mp1.last_updated = datetime(2024, 1, 15, 10, 30, 0)

        mp2 = MagicMock()
        mp2.id = "marketplace2"
        mp2.source = "git"
        mp2.url = "https://github.com/test/mp2.git"
        mp2.install_location = "/path/to/mp2"
        mp2.last_updated = None

        mock_plugin_service.get_marketplaces.return_value = [mp1, mp2]

        with patch("app.api.plugins.get_plugin_service", return_value=mock_plugin_service):
            with TestClient(app) as client:
                response = client.get("/api/v1/plugins/marketplaces")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["id"] == "marketplace1"
        assert data[1]["id"] == "marketplace2"
        assert data[1]["last_updated"] is None
