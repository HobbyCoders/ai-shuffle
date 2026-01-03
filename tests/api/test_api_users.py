"""
Unit tests for API User management endpoints.

Tests cover:
- CRUD operations for API users (create, read, update, delete)
- API key generation and regeneration
- Authentication and authorization (admin only)
- Validation of project and profile references
- Error handling and edge cases
"""

import pytest
import hashlib
from datetime import datetime
from unittest.mock import patch, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.api_users import (
    router,
    generate_api_key,
    hash_api_key
)
from app.api.auth import require_admin


# =============================================================================
# Fixtures
# =============================================================================

def mock_require_admin_override():
    """Mock admin authentication - returns a token."""
    return "test-admin-token"


@pytest.fixture
def mock_database():
    """Mock database module."""
    with patch("app.api.api_users.db") as mock_db:
        yield mock_db


@pytest.fixture
def client(mock_database):
    """Create a test client with mocked dependencies."""
    app = FastAPI()
    app.include_router(router)
    # Override the require_admin dependency
    app.dependency_overrides[require_admin] = mock_require_admin_override
    client = TestClient(app)
    yield client
    # Cleanup
    app.dependency_overrides.clear()


@pytest.fixture
def sample_api_user():
    """Sample API user data."""
    return {
        "id": "test-user",
        "name": "Test API User",
        "description": "A test API user",
        "project_id": None,
        "profile_ids": None,
        "is_active": True,
        "username": None,
        "web_login_allowed": True,
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00",
        "last_used_at": None
    }


@pytest.fixture
def sample_api_user_with_project():
    """Sample API user with project assignment."""
    return {
        "id": "user-with-project",
        "name": "Project User",
        "description": "User with project",
        "project_id": "test-project",
        "profile_ids": ["profile-1", "profile-2"],
        "is_active": True,
        "username": "projectuser",
        "web_login_allowed": True,
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00",
        "last_used_at": "2024-01-16T09:00:00"
    }


@pytest.fixture
def sample_project():
    """Sample project data."""
    return {
        "id": "test-project",
        "name": "Test Project",
        "path": "/path/to/project",
        "created_at": "2024-01-01T00:00:00"
    }


@pytest.fixture
def sample_profile():
    """Sample profile data."""
    return {
        "id": "test-profile",
        "name": "Test Profile",
        "model": "claude-sonnet-4-20250514",
        "system_prompt": "You are helpful.",
        "created_at": "2024-01-01T00:00:00"
    }


# =============================================================================
# Test Module Imports
# =============================================================================

class TestApiUsersModuleImports:
    """Verify api_users module can be imported correctly."""

    def test_api_users_module_imports(self):
        """API users module should import without errors."""
        from app.api import api_users
        assert api_users is not None

    def test_api_users_router_exists(self):
        """API users router should exist."""
        from app.api.api_users import router
        assert router is not None

    def test_router_has_correct_prefix(self):
        """Router should have /api/v1/api-users prefix."""
        from app.api.api_users import router
        assert router.prefix == "/api/v1/api-users"


# =============================================================================
# Test Utility Functions
# =============================================================================

class TestGenerateApiKey:
    """Test API key generation utility."""

    def test_generate_api_key_format(self):
        """API key should have correct prefix format."""
        api_key = generate_api_key()
        assert api_key.startswith("aih_")
        # Should have a reasonable length (prefix + urlsafe base64)
        assert len(api_key) > 40

    def test_generate_api_key_uniqueness(self):
        """Multiple API keys should be unique."""
        keys = [generate_api_key() for _ in range(100)]
        assert len(set(keys)) == 100

    def test_generate_api_key_uses_secrets(self):
        """API key should use secure random generation."""
        import secrets
        with patch.object(secrets, 'token_urlsafe', return_value='test_token_123') as mock_token:
            api_key = generate_api_key()
            mock_token.assert_called_once_with(32)
            assert api_key == "aih_test_token_123"


class TestHashApiKey:
    """Test API key hashing utility."""

    def test_hash_api_key_produces_hash(self):
        """Hashing should produce a valid SHA256 hash."""
        api_key = "aih_test_key_12345"
        hashed = hash_api_key(api_key)
        # SHA256 produces 64 hex characters
        assert len(hashed) == 64
        assert all(c in '0123456789abcdef' for c in hashed)

    def test_hash_api_key_consistent(self):
        """Same key should produce same hash."""
        api_key = "aih_consistent_key"
        hash1 = hash_api_key(api_key)
        hash2 = hash_api_key(api_key)
        assert hash1 == hash2

    def test_hash_api_key_different_keys(self):
        """Different keys should produce different hashes."""
        hash1 = hash_api_key("aih_key_one")
        hash2 = hash_api_key("aih_key_two")
        assert hash1 != hash2

    def test_hash_api_key_uses_sha256(self):
        """Should use SHA256 for hashing."""
        api_key = "aih_test_key"
        expected = hashlib.sha256(api_key.encode()).hexdigest()
        assert hash_api_key(api_key) == expected


# =============================================================================
# Test List API Users Endpoint
# =============================================================================

class TestListApiUsers:
    """Test GET /api/v1/api-users endpoint."""

    def test_list_api_users_success(self, client, mock_database, sample_api_user):
        """Should return list of all API users."""
        mock_database.get_all_api_users.return_value = [sample_api_user]

        response = client.get("/api/v1/api-users")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["id"] == "test-user"
        assert data[0]["name"] == "Test API User"
        mock_database.get_all_api_users.assert_called_once()

    def test_list_api_users_empty(self, client, mock_database):
        """Should return empty list when no API users exist."""
        mock_database.get_all_api_users.return_value = []

        response = client.get("/api/v1/api-users")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_api_users_multiple(self, client, mock_database, sample_api_user, sample_api_user_with_project):
        """Should return all API users."""
        mock_database.get_all_api_users.return_value = [sample_api_user, sample_api_user_with_project]

        response = client.get("/api/v1/api-users")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2


# =============================================================================
# Test Get API User Endpoint
# =============================================================================

class TestGetApiUser:
    """Test GET /api/v1/api-users/{user_id} endpoint."""

    def test_get_api_user_success(self, client, mock_database, sample_api_user):
        """Should return API user details."""
        mock_database.get_api_user.return_value = sample_api_user

        response = client.get("/api/v1/api-users/test-user")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "test-user"
        assert data["name"] == "Test API User"
        assert data["description"] == "A test API user"
        mock_database.get_api_user.assert_called_once_with("test-user")

    def test_get_api_user_not_found(self, client, mock_database):
        """Should return 404 for non-existent API user."""
        mock_database.get_api_user.return_value = None

        response = client.get("/api/v1/api-users/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_api_user_with_project_and_profiles(self, client, mock_database, sample_api_user_with_project):
        """Should return API user with project and profile details."""
        mock_database.get_api_user.return_value = sample_api_user_with_project

        response = client.get("/api/v1/api-users/user-with-project")

        assert response.status_code == 200
        data = response.json()
        assert data["project_id"] == "test-project"
        assert data["profile_ids"] == ["profile-1", "profile-2"]


# =============================================================================
# Test Create API User Endpoint
# =============================================================================

class TestCreateApiUser:
    """Test POST /api/v1/api-users endpoint."""

    def test_create_api_user_success(self, client, mock_database):
        """Should create a new API user and return API key."""
        created_user = {
            "id": "new-user",
            "name": "New User",
            "description": None,
            "project_id": None,
            "profile_ids": None,
            "is_active": True,
            "username": None,
            "web_login_allowed": True,
            "created_at": "2024-01-15T10:30:00",
            "updated_at": "2024-01-15T10:30:00",
            "last_used_at": None
        }
        mock_database.create_api_user.return_value = created_user

        response = client.post(
            "/api/v1/api-users",
            json={"name": "New User"}
        )

        assert response.status_code == 201
        data = response.json()
        assert data["name"] == "New User"
        # Should include the API key (only returned once)
        assert "api_key" in data
        assert data["api_key"].startswith("aih_")
        mock_database.create_api_user.assert_called_once()

    def test_create_api_user_with_description(self, client, mock_database):
        """Should create API user with description."""
        created_user = {
            "id": "new-user",
            "name": "New User",
            "description": "A detailed description",
            "project_id": None,
            "profile_ids": None,
            "is_active": True,
            "username": None,
            "web_login_allowed": True,
            "created_at": "2024-01-15T10:30:00",
            "updated_at": "2024-01-15T10:30:00",
            "last_used_at": None
        }
        mock_database.create_api_user.return_value = created_user

        response = client.post(
            "/api/v1/api-users",
            json={
                "name": "New User",
                "description": "A detailed description"
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["description"] == "A detailed description"

    def test_create_api_user_with_project(self, client, mock_database, sample_project):
        """Should create API user with project assignment."""
        mock_database.get_project.return_value = sample_project
        created_user = {
            "id": "new-user",
            "name": "Project User",
            "description": None,
            "project_id": "test-project",
            "profile_ids": None,
            "is_active": True,
            "username": None,
            "web_login_allowed": True,
            "created_at": "2024-01-15T10:30:00",
            "updated_at": "2024-01-15T10:30:00",
            "last_used_at": None
        }
        mock_database.create_api_user.return_value = created_user

        response = client.post(
            "/api/v1/api-users",
            json={
                "name": "Project User",
                "project_id": "test-project"
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["project_id"] == "test-project"
        mock_database.get_project.assert_called_once_with("test-project")

    def test_create_api_user_project_not_found(self, client, mock_database):
        """Should return 400 if project doesn't exist."""
        mock_database.get_project.return_value = None

        response = client.post(
            "/api/v1/api-users",
            json={
                "name": "User",
                "project_id": "nonexistent-project"
            }
        )

        assert response.status_code == 400
        assert "Project not found" in response.json()["detail"]

    def test_create_api_user_with_profiles(self, client, mock_database, sample_profile):
        """Should create API user with profile assignments."""
        mock_database.get_profile.return_value = sample_profile
        created_user = {
            "id": "new-user",
            "name": "Profile User",
            "description": None,
            "project_id": None,
            "profile_ids": ["test-profile"],
            "is_active": True,
            "username": None,
            "web_login_allowed": True,
            "created_at": "2024-01-15T10:30:00",
            "updated_at": "2024-01-15T10:30:00",
            "last_used_at": None
        }
        mock_database.create_api_user.return_value = created_user

        response = client.post(
            "/api/v1/api-users",
            json={
                "name": "Profile User",
                "profile_ids": ["test-profile"]
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["profile_ids"] == ["test-profile"]
        mock_database.get_profile.assert_called_once_with("test-profile")

    def test_create_api_user_profile_not_found(self, client, mock_database):
        """Should return 400 if profile doesn't exist."""
        mock_database.get_profile.return_value = None

        response = client.post(
            "/api/v1/api-users",
            json={
                "name": "User",
                "profile_ids": ["nonexistent-profile"]
            }
        )

        assert response.status_code == 400
        assert "Profile not found" in response.json()["detail"]
        assert "nonexistent-profile" in response.json()["detail"]

    def test_create_api_user_multiple_profiles(self, client, mock_database):
        """Should validate all profiles exist."""
        # First profile exists, second doesn't
        mock_database.get_profile.side_effect = [
            {"id": "profile-1", "name": "Profile 1"},
            None
        ]

        response = client.post(
            "/api/v1/api-users",
            json={
                "name": "User",
                "profile_ids": ["profile-1", "profile-2"]
            }
        )

        assert response.status_code == 400
        assert "Profile not found" in response.json()["detail"]
        assert "profile-2" in response.json()["detail"]

    def test_create_api_user_with_web_login_disabled(self, client, mock_database):
        """Should create API user with web login disabled."""
        created_user = {
            "id": "new-user",
            "name": "API Only User",
            "description": None,
            "project_id": None,
            "profile_ids": None,
            "is_active": True,
            "username": None,
            "web_login_allowed": False,
            "created_at": "2024-01-15T10:30:00",
            "updated_at": "2024-01-15T10:30:00",
            "last_used_at": None
        }
        mock_database.create_api_user.return_value = created_user

        response = client.post(
            "/api/v1/api-users",
            json={
                "name": "API Only User",
                "web_login_allowed": False
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["web_login_allowed"] is False

    def test_create_api_user_validates_name_required(self, client, mock_database):
        """Should require name field."""
        response = client.post(
            "/api/v1/api-users",
            json={}
        )

        assert response.status_code == 422  # Validation error

    def test_create_api_user_validates_name_not_empty(self, client, mock_database):
        """Should reject empty name."""
        response = client.post(
            "/api/v1/api-users",
            json={"name": ""}
        )

        assert response.status_code == 422  # Validation error

    def test_create_api_user_api_key_is_hashed(self, client, mock_database):
        """API key should be stored as hash, not plaintext."""
        created_user = {
            "id": "new-user",
            "name": "User",
            "description": None,
            "project_id": None,
            "profile_ids": None,
            "is_active": True,
            "username": None,
            "web_login_allowed": True,
            "created_at": "2024-01-15T10:30:00",
            "updated_at": "2024-01-15T10:30:00",
            "last_used_at": None
        }
        mock_database.create_api_user.return_value = created_user

        response = client.post(
            "/api/v1/api-users",
            json={"name": "User"}
        )

        assert response.status_code == 201
        # Check that create_api_user was called with a hashed key
        call_kwargs = mock_database.create_api_user.call_args
        api_key_hash = call_kwargs.kwargs.get("api_key_hash") or call_kwargs[1].get("api_key_hash")
        # Hash should be 64 char hex (SHA256)
        assert len(api_key_hash) == 64
        assert all(c in '0123456789abcdef' for c in api_key_hash)


# =============================================================================
# Test Update API User Endpoint
# =============================================================================

class TestUpdateApiUser:
    """Test PUT /api/v1/api-users/{user_id} endpoint."""

    def test_update_api_user_success(self, client, mock_database, sample_api_user):
        """Should update API user details."""
        mock_database.get_api_user.return_value = sample_api_user
        updated_user = {**sample_api_user, "name": "Updated Name"}
        mock_database.update_api_user.return_value = updated_user

        response = client.put(
            "/api/v1/api-users/test-user",
            json={"name": "Updated Name"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "Updated Name"
        mock_database.update_api_user.assert_called_once()

    def test_update_api_user_not_found(self, client, mock_database):
        """Should return 404 for non-existent API user."""
        mock_database.get_api_user.return_value = None

        response = client.put(
            "/api/v1/api-users/nonexistent",
            json={"name": "Updated"}
        )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_update_api_user_description(self, client, mock_database, sample_api_user):
        """Should update description."""
        mock_database.get_api_user.return_value = sample_api_user
        updated_user = {**sample_api_user, "description": "New description"}
        mock_database.update_api_user.return_value = updated_user

        response = client.put(
            "/api/v1/api-users/test-user",
            json={"description": "New description"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["description"] == "New description"

    def test_update_api_user_project(self, client, mock_database, sample_api_user, sample_project):
        """Should update project assignment."""
        mock_database.get_api_user.return_value = sample_api_user
        mock_database.get_project.return_value = sample_project
        updated_user = {**sample_api_user, "project_id": "test-project"}
        mock_database.update_api_user.return_value = updated_user

        response = client.put(
            "/api/v1/api-users/test-user",
            json={"project_id": "test-project"}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["project_id"] == "test-project"
        mock_database.get_project.assert_called_once_with("test-project")

    def test_update_api_user_project_not_found(self, client, mock_database, sample_api_user):
        """Should return 400 if project doesn't exist."""
        mock_database.get_api_user.return_value = sample_api_user
        mock_database.get_project.return_value = None

        response = client.put(
            "/api/v1/api-users/test-user",
            json={"project_id": "nonexistent"}
        )

        assert response.status_code == 400
        assert "Project not found" in response.json()["detail"]

    def test_update_api_user_profiles(self, client, mock_database, sample_api_user, sample_profile):
        """Should update profile assignments."""
        mock_database.get_api_user.return_value = sample_api_user
        mock_database.get_profile.return_value = sample_profile
        updated_user = {**sample_api_user, "profile_ids": ["test-profile"]}
        mock_database.update_api_user.return_value = updated_user

        response = client.put(
            "/api/v1/api-users/test-user",
            json={"profile_ids": ["test-profile"]}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["profile_ids"] == ["test-profile"]

    def test_update_api_user_profile_not_found(self, client, mock_database, sample_api_user):
        """Should return 400 if profile doesn't exist."""
        mock_database.get_api_user.return_value = sample_api_user
        mock_database.get_profile.return_value = None

        response = client.put(
            "/api/v1/api-users/test-user",
            json={"profile_ids": ["nonexistent"]}
        )

        assert response.status_code == 400
        assert "Profile not found" in response.json()["detail"]

    def test_update_api_user_is_active(self, client, mock_database, sample_api_user):
        """Should update is_active status."""
        mock_database.get_api_user.return_value = sample_api_user
        updated_user = {**sample_api_user, "is_active": False}
        mock_database.update_api_user.return_value = updated_user

        response = client.put(
            "/api/v1/api-users/test-user",
            json={"is_active": False}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["is_active"] is False

    def test_update_api_user_web_login_allowed(self, client, mock_database, sample_api_user):
        """Should update web_login_allowed."""
        mock_database.get_api_user.return_value = sample_api_user
        updated_user = {**sample_api_user, "web_login_allowed": False}
        mock_database.update_api_user.return_value = updated_user

        response = client.put(
            "/api/v1/api-users/test-user",
            json={"web_login_allowed": False}
        )

        assert response.status_code == 200
        data = response.json()
        assert data["web_login_allowed"] is False

    def test_update_api_user_multiple_fields(self, client, mock_database, sample_api_user, sample_project):
        """Should update multiple fields at once."""
        mock_database.get_api_user.return_value = sample_api_user
        mock_database.get_project.return_value = sample_project
        updated_user = {
            **sample_api_user,
            "name": "New Name",
            "description": "New desc",
            "project_id": "test-project",
            "is_active": False
        }
        mock_database.update_api_user.return_value = updated_user

        response = client.put(
            "/api/v1/api-users/test-user",
            json={
                "name": "New Name",
                "description": "New desc",
                "project_id": "test-project",
                "is_active": False
            }
        )

        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "New Name"
        assert data["description"] == "New desc"
        assert data["project_id"] == "test-project"
        assert data["is_active"] is False


# =============================================================================
# Test Regenerate API Key Endpoint
# =============================================================================

class TestRegenerateApiKey:
    """Test POST /api/v1/api-users/{user_id}/regenerate-key endpoint."""

    def test_regenerate_api_key_success(self, client, mock_database, sample_api_user):
        """Should regenerate API key and return new key."""
        mock_database.get_api_user.return_value = sample_api_user
        updated_user = {**sample_api_user, "updated_at": "2024-01-16T10:30:00"}
        mock_database.update_api_user_key.return_value = updated_user

        response = client.post("/api/v1/api-users/test-user/regenerate-key")

        assert response.status_code == 200
        data = response.json()
        # Should include the new API key
        assert "api_key" in data
        assert data["api_key"].startswith("aih_")
        mock_database.update_api_user_key.assert_called_once()

    def test_regenerate_api_key_not_found(self, client, mock_database):
        """Should return 404 for non-existent API user."""
        mock_database.get_api_user.return_value = None

        response = client.post("/api/v1/api-users/nonexistent/regenerate-key")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_regenerate_api_key_creates_new_hash(self, client, mock_database, sample_api_user):
        """New API key hash should be stored."""
        mock_database.get_api_user.return_value = sample_api_user
        mock_database.update_api_user_key.return_value = sample_api_user

        response = client.post("/api/v1/api-users/test-user/regenerate-key")

        assert response.status_code == 200
        # Verify update_api_user_key was called with a hash
        call_args = mock_database.update_api_user_key.call_args
        user_id = call_args[0][0]
        api_key_hash = call_args[0][1]
        assert user_id == "test-user"
        # Hash should be 64 char hex (SHA256)
        assert len(api_key_hash) == 64

    def test_regenerate_api_key_returns_new_unique_key(self, client, mock_database, sample_api_user):
        """Each regeneration should return a unique key."""
        mock_database.get_api_user.return_value = sample_api_user
        mock_database.update_api_user_key.return_value = sample_api_user

        response1 = client.post("/api/v1/api-users/test-user/regenerate-key")
        response2 = client.post("/api/v1/api-users/test-user/regenerate-key")

        assert response1.status_code == 200
        assert response2.status_code == 200
        # Keys should be different
        key1 = response1.json()["api_key"]
        key2 = response2.json()["api_key"]
        assert key1 != key2


# =============================================================================
# Test Delete API User Endpoint
# =============================================================================

class TestDeleteApiUser:
    """Test DELETE /api/v1/api-users/{user_id} endpoint."""

    def test_delete_api_user_success(self, client, mock_database):
        """Should delete API user successfully."""
        mock_database.delete_api_user.return_value = True

        response = client.delete("/api/v1/api-users/test-user")

        assert response.status_code == 204
        mock_database.delete_api_user.assert_called_once_with("test-user")

    def test_delete_api_user_not_found(self, client, mock_database):
        """Should return 404 for non-existent API user."""
        mock_database.delete_api_user.return_value = False

        response = client.delete("/api/v1/api-users/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


# =============================================================================
# Test Authentication Requirements
# =============================================================================

def mock_require_admin_raises():
    """Mock admin authentication that raises 403."""
    from fastapi import HTTPException
    raise HTTPException(status_code=403, detail="Admin access required")


class TestAuthenticationRequirements:
    """Test that all endpoints require admin authentication."""

    def test_list_requires_admin(self, mock_database):
        """List endpoint should require admin auth."""
        app = FastAPI()
        app.include_router(router)
        # Override with a function that raises 403
        app.dependency_overrides[require_admin] = mock_require_admin_raises
        client = TestClient(app)

        response = client.get("/api/v1/api-users")
        assert response.status_code == 403
        app.dependency_overrides.clear()

    def test_get_requires_admin(self, mock_database):
        """Get endpoint should require admin auth."""
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_admin] = mock_require_admin_raises
        client = TestClient(app)

        response = client.get("/api/v1/api-users/test-user")
        assert response.status_code == 403
        app.dependency_overrides.clear()

    def test_create_requires_admin(self, mock_database):
        """Create endpoint should require admin auth."""
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_admin] = mock_require_admin_raises
        client = TestClient(app)

        response = client.post("/api/v1/api-users", json={"name": "Test"})
        assert response.status_code == 403
        app.dependency_overrides.clear()

    def test_update_requires_admin(self, mock_database):
        """Update endpoint should require admin auth."""
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_admin] = mock_require_admin_raises
        client = TestClient(app)

        response = client.put("/api/v1/api-users/test-user", json={"name": "Updated"})
        assert response.status_code == 403
        app.dependency_overrides.clear()

    def test_regenerate_key_requires_admin(self, mock_database):
        """Regenerate key endpoint should require admin auth."""
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_admin] = mock_require_admin_raises
        client = TestClient(app)

        response = client.post("/api/v1/api-users/test-user/regenerate-key")
        assert response.status_code == 403
        app.dependency_overrides.clear()

    def test_delete_requires_admin(self, mock_database):
        """Delete endpoint should require admin auth."""
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_admin] = mock_require_admin_raises
        client = TestClient(app)

        response = client.delete("/api/v1/api-users/test-user")
        assert response.status_code == 403
        app.dependency_overrides.clear()


# =============================================================================
# Test Edge Cases
# =============================================================================

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_create_user_with_all_profiles_valid(self, client, mock_database):
        """Should validate all profiles in list."""
        profiles = [
            {"id": "profile-1", "name": "Profile 1"},
            {"id": "profile-2", "name": "Profile 2"},
            {"id": "profile-3", "name": "Profile 3"},
        ]
        mock_database.get_profile.side_effect = profiles
        created_user = {
            "id": "new-user",
            "name": "User",
            "description": None,
            "project_id": None,
            "profile_ids": ["profile-1", "profile-2", "profile-3"],
            "is_active": True,
            "username": None,
            "web_login_allowed": True,
            "created_at": "2024-01-15T10:30:00",
            "updated_at": "2024-01-15T10:30:00",
            "last_used_at": None
        }
        mock_database.create_api_user.return_value = created_user

        response = client.post(
            "/api/v1/api-users",
            json={
                "name": "User",
                "profile_ids": ["profile-1", "profile-2", "profile-3"]
            }
        )

        assert response.status_code == 201
        # All three profiles should have been validated
        assert mock_database.get_profile.call_count == 3

    def test_update_with_empty_profile_ids(self, client, mock_database, sample_api_user):
        """Empty profile_ids should be allowed (means any profile)."""
        mock_database.get_api_user.return_value = sample_api_user
        updated_user = {**sample_api_user, "profile_ids": []}
        mock_database.update_api_user.return_value = updated_user

        response = client.put(
            "/api/v1/api-users/test-user",
            json={"profile_ids": []}
        )

        assert response.status_code == 200
        # get_profile should not be called for empty list
        mock_database.get_profile.assert_not_called()

    def test_create_with_empty_profile_ids(self, client, mock_database):
        """Empty profile_ids on create should be allowed."""
        created_user = {
            "id": "new-user",
            "name": "User",
            "description": None,
            "project_id": None,
            "profile_ids": [],
            "is_active": True,
            "username": None,
            "web_login_allowed": True,
            "created_at": "2024-01-15T10:30:00",
            "updated_at": "2024-01-15T10:30:00",
            "last_used_at": None
        }
        mock_database.create_api_user.return_value = created_user

        response = client.post(
            "/api/v1/api-users",
            json={
                "name": "User",
                "profile_ids": []
            }
        )

        assert response.status_code == 201
        mock_database.get_profile.assert_not_called()

    def test_create_user_with_long_name(self, client, mock_database):
        """Should handle names at maximum length."""
        long_name = "A" * 100  # Maximum allowed
        created_user = {
            "id": "new-user",
            "name": long_name,
            "description": None,
            "project_id": None,
            "profile_ids": None,
            "is_active": True,
            "username": None,
            "web_login_allowed": True,
            "created_at": "2024-01-15T10:30:00",
            "updated_at": "2024-01-15T10:30:00",
            "last_used_at": None
        }
        mock_database.create_api_user.return_value = created_user

        response = client.post(
            "/api/v1/api-users",
            json={"name": long_name}
        )

        assert response.status_code == 201
        assert response.json()["name"] == long_name

    def test_create_user_name_too_long(self, client, mock_database):
        """Should reject names exceeding maximum length."""
        too_long_name = "A" * 101  # Exceeds max 100

        response = client.post(
            "/api/v1/api-users",
            json={"name": too_long_name}
        )

        assert response.status_code == 422  # Validation error

    def test_user_includes_all_response_fields(self, client, mock_database, sample_api_user_with_project):
        """Response should include all expected fields."""
        mock_database.get_api_user.return_value = sample_api_user_with_project

        response = client.get("/api/v1/api-users/user-with-project")

        assert response.status_code == 200
        data = response.json()
        # Check all expected fields are present
        expected_fields = [
            "id", "name", "description", "project_id", "profile_ids",
            "is_active", "username", "web_login_allowed",
            "created_at", "updated_at", "last_used_at"
        ]
        for field in expected_fields:
            assert field in data, f"Missing field: {field}"
