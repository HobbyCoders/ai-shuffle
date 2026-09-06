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
from unittest.mock import patch, MagicMock
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
            mock_settings.version = "1.2.1"

            # Create a mock request
            mock_request = MagicMock()
            mock_request.state.is_admin = False

            result = await health_check()

            assert result["status"] == "healthy"
            assert result["service"] == "ai-shuffle"
            assert result["version"] == "1.2.1"

    @pytest.mark.asyncio
    async def test_health_check_shows_setup_required(self):
        """Should show setup_required when admin not configured."""
        from app.api.system import health_check

        with patch("app.api.system.auth_service") as mock_auth, \
             patch("app.api.system.settings") as mock_settings:
            mock_auth.is_setup_required.return_value = True
            mock_auth.is_claude_authenticated.return_value = False
            mock_settings.service_name = "ai-shuffle"
            mock_settings.version = "1.2.1"

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
            mock_settings.version = "1.2.1"
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="Claude 1.2.3\n"
            )

            result = await get_version()

            assert result["api_version"] == "1.2.1"
            assert result["claude_version"] == "Claude 1.2.3"

    @pytest.mark.asyncio
    async def test_get_version_claude_not_installed(self):
        """Should handle Claude not installed."""
        from app.api.system import get_version

        with patch("app.api.system.settings") as mock_settings, \
             patch("app.api.system.subprocess.run") as mock_run:
            mock_settings.version = "1.2.1"
            mock_run.return_value = MagicMock(returncode=1, stdout="")

            result = await get_version()

            assert result["api_version"] == "1.2.1"
            assert result["claude_version"] is None

    @pytest.mark.asyncio
    async def test_get_version_subprocess_exception(self):
        """Should handle subprocess exception gracefully."""
        from app.api.system import get_version

        with patch("app.api.system.settings") as mock_settings, \
             patch("app.api.system.subprocess.run") as mock_run:
            mock_settings.version = "1.2.1"
            mock_run.side_effect = Exception("Command not found")

            result = await get_version()

            assert result["api_version"] == "1.2.1"
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
        """Should handle permission denied on existing directory.

        Stat-only handler: writability is probed with os.access, so we mock
        os.access to deny write on the existing directory.
        """
        from app.api.system import validate_workspace_path, WorkspaceConfigRequest

        with patch("app.api.system.os.access", return_value=False):
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

    @pytest.mark.asyncio
    async def test_validate_new_dir_does_not_mutate_filesystem(self, temp_workspace):
        """Stat-only: validating a non-existent path must NOT create it."""
        from app.api.system import validate_workspace_path, WorkspaceConfigRequest

        new_path = temp_workspace / "should_not_be_created"
        assert not new_path.exists()

        request = WorkspaceConfigRequest(workspace_path=str(new_path))
        result = await validate_workspace_path(request)

        # Parent is writable, so it reports creatable, but nothing is created.
        assert result["exists"] is False
        assert not new_path.exists(), "validate must not create the directory"

    @pytest.mark.asyncio
    async def test_validate_existing_dir_creates_no_test_file(self, temp_workspace):
        """Stat-only: validating an existing dir must not leave a test file."""
        from app.api.system import validate_workspace_path, WorkspaceConfigRequest

        before = set(p.name for p in temp_workspace.iterdir())
        request = WorkspaceConfigRequest(workspace_path=str(temp_workspace))
        await validate_workspace_path(request)
        after = set(p.name for p in temp_workspace.iterdir())

        assert before == after, "validate must not touch/remove any files"
        assert ".ai-shuffle-test" not in after


# =============================================================================
# Workspace validate authentication (Phase 1.2)
# =============================================================================

class TestValidateWorkspaceAuth:
    """POST /api/v1/workspace/validate must require admin post-setup."""

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.api.system import router
        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_unauthenticated_post_setup_is_rejected(self, temp_workspace):
        """When setup is complete and no admin cookie/bearer → 401/403."""
        client = self._client()
        with patch("app.api.system.auth_service") as mock_auth:
            mock_auth.is_setup_required.return_value = False  # setup complete
            resp = client.post(
                "/api/v1/workspace/validate",
                json={"workspace_path": str(temp_workspace)},
            )
        assert resp.status_code in (401, 403)

    def test_no_directory_created_on_rejected_request(self, temp_workspace):
        """A rejected (unauthenticated) request must not create any directory."""
        target = temp_workspace / "rejected_should_not_exist"
        client = self._client()
        with patch("app.api.system.auth_service") as mock_auth:
            mock_auth.is_setup_required.return_value = False
            resp = client.post(
                "/api/v1/workspace/validate",
                json={"workspace_path": str(target)},
            )
        assert resp.status_code in (401, 403)
        assert not target.exists()

    def test_remote_pre_setup_request_stays_protected(self, temp_workspace):
        """An empty installation must not grant authority on an untrusted socket."""
        client = self._client()
        with patch("app.api.system.auth_service") as mock_auth:
            mock_auth.is_setup_required.return_value = True
            resp = client.post(
                "/api/v1/workspace/validate",
                json={"workspace_path": str(temp_workspace)},
            )
        assert resp.status_code in (401, 403)


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



# =============================================================================
# Issue #610: PushNotification -> NotifyDevices rename
# =============================================================================

class TestPushNotificationRename:
    """The bare ``PushNotification`` catalog stub collided with the builtin MCP
    tool of the same name; the builtin was renamed to ``NotifyDevices`` and the
    bare stub removed (issue #610)."""

    def test_builtin_exposed_under_new_name(self):
        """The builtin leaf tool is now ``mcp__builtins__NotifyDevices``."""
        from app.mcp.builtin_tools import get_builtin_tool_names

        names = get_builtin_tool_names()
        assert "mcp__builtins__NotifyDevices" in names
        assert "mcp__builtins__PushNotification" not in names
