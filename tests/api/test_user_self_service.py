"""
Tests for user self-service API endpoints.

Tests cover:
- Profile endpoints (GET/PUT /api/v1/me, PUT /api/v1/me/password)
- Credentials endpoints (GET/POST/DELETE /api/v1/me/credentials)
- GitHub endpoints (GET/POST/DELETE /api/v1/me/github)
- Helper functions (mask_api_key, get_decrypted_user_credential, get_admin_api_key)
- Authentication, validation, and error handling
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi.testclient import TestClient
import httpx

# Import the module under test
from app.api.user_self_service import (
    router,
    mask_api_key,
    get_decrypted_user_credential,
    get_admin_api_key,
    CREDENTIAL_INFO,
)
from app.core.credential_service import ValidationResult, CredentialValidationResponse


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_api_user():
    """Return a mock API user dict for testing."""
    return {
        "id": "test-user-id",
        "name": "Test User",
        "username": "testuser",
        "description": "A test user",
        "project_id": "project-1",
        "profile_id": "profile-1",
        "is_active": True,
        "web_login_allowed": True,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "last_used_at": "2024-01-03T00:00:00Z",
        "password_hash": "$2b$12$abcdefghijklmnopqrstuv",  # bcrypt hash
    }


@pytest.fixture
def mock_api_user_no_password():
    """Return a mock API user without password hash."""
    return {
        "id": "test-user-id",
        "name": "Test User",
        "username": "testuser",
        "description": "A test user",
        "project_id": None,
        "profile_id": None,
        "is_active": True,
        "web_login_allowed": True,
        "created_at": "2024-01-01T00:00:00Z",
        "updated_at": "2024-01-02T00:00:00Z",
        "last_used_at": None,
        "password_hash": None,
    }


@pytest.fixture
def mock_db_functions():
    """Set up common database mocks."""
    with patch("app.api.user_self_service.database") as mock_db:
        yield mock_db


@pytest.fixture
def mock_encryption_functions():
    """Set up encryption mocks."""
    with patch("app.api.user_self_service.encryption") as mock_enc:
        mock_enc.is_encryption_ready.return_value = True
        mock_enc.is_encrypted.return_value = True
        mock_enc.encrypt_value.side_effect = lambda x: f"encrypted:{x}"
        mock_enc.decrypt_value.side_effect = lambda x: x.replace("encrypted:", "")
        yield mock_enc


# =============================================================================
# Test Helper Functions
# =============================================================================

class TestMaskApiKey:
    """Test mask_api_key helper function."""

    def test_mask_normal_key(self):
        """Should mask a normal API key correctly."""
        key = "sk-1234567890abcdefghij"
        masked = mask_api_key(key)
        assert masked == "sk-1234...ghij"
        assert "1234567890" not in masked

    def test_mask_short_key(self):
        """Should return **** for keys shorter than 10 characters."""
        short_key = "sk-short"
        masked = mask_api_key(short_key)
        assert masked == "****"

    def test_mask_empty_key(self):
        """Should return **** for empty key."""
        masked = mask_api_key("")
        assert masked == "****"

    def test_mask_none_key(self):
        """Should return **** for None key."""
        masked = mask_api_key(None)
        assert masked == "****"

    def test_mask_exactly_10_chars(self):
        """Should mask key with exactly 10 characters."""
        key = "1234567890"
        masked = mask_api_key(key)
        assert masked == "1234567...7890"

    def test_mask_long_key(self):
        """Should mask long API key correctly."""
        key = "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz1234567890"
        masked = mask_api_key(key)
        assert masked.startswith("ghp_AbC")
        assert masked.endswith("7890")
        assert len(masked) == 14  # 7 + "..." + 4


class TestGetDecryptedUserCredential:
    """Test get_decrypted_user_credential helper function."""

    def test_returns_none_when_no_credential(self):
        """Should return None when credential doesn't exist."""
        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.get_user_credential.return_value = None

            result = get_decrypted_user_credential("user-id", "openai_api_key")

            assert result is None
            mock_db.get_user_credential.assert_called_once_with("user-id", "openai_api_key")

    def test_returns_none_when_no_encrypted_value(self):
        """Should return None when encrypted_value is missing."""
        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.get_user_credential.return_value = {"id": "cred-1"}

            result = get_decrypted_user_credential("user-id", "openai_api_key")

            assert result is None

    def test_decrypts_encrypted_value(self):
        """Should decrypt an encrypted value."""
        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                mock_db.get_user_credential.return_value = {
                    "encrypted_value": "encrypted:sk-test-key"
                }
                mock_enc.is_encrypted.return_value = True
                mock_enc.is_encryption_ready.return_value = True
                mock_enc.decrypt_value.return_value = "sk-test-key"

                result = get_decrypted_user_credential("user-id", "openai_api_key")

                assert result == "sk-test-key"
                mock_enc.decrypt_value.assert_called_once_with("encrypted:sk-test-key")

    def test_returns_plaintext_value_when_not_encrypted(self):
        """Should return plaintext value if not encrypted."""
        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                mock_db.get_user_credential.return_value = {
                    "encrypted_value": "plaintext-key"
                }
                mock_enc.is_encrypted.return_value = False

                result = get_decrypted_user_credential("user-id", "openai_api_key")

                assert result == "plaintext-key"

    def test_returns_none_when_encryption_not_ready(self):
        """Should return None when encryption is not ready."""
        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                mock_db.get_user_credential.return_value = {
                    "encrypted_value": "encrypted:key"
                }
                mock_enc.is_encrypted.return_value = True
                mock_enc.is_encryption_ready.return_value = False

                result = get_decrypted_user_credential("user-id", "openai_api_key")

                assert result is None

    def test_returns_none_on_decryption_error(self):
        """Should return None and log error on decryption failure."""
        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                mock_db.get_user_credential.return_value = {
                    "encrypted_value": "encrypted:bad-data"
                }
                mock_enc.is_encrypted.return_value = True
                mock_enc.is_encryption_ready.return_value = True
                mock_enc.decrypt_value.side_effect = Exception("Decryption failed")

                result = get_decrypted_user_credential("user-id", "openai_api_key")

                assert result is None


class TestGetAdminApiKey:
    """Test get_admin_api_key helper function."""

    def test_returns_none_when_setting_not_found(self):
        """Should return None when system setting doesn't exist."""
        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.get_system_setting.return_value = None

            result = get_admin_api_key("openai_api_key")

            assert result is None

    def test_returns_decrypted_value(self):
        """Should return decrypted value for encrypted setting."""
        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                mock_db.get_system_setting.return_value = "encrypted:admin-key"
                mock_enc.is_encrypted.return_value = True
                mock_enc.is_encryption_ready.return_value = True
                mock_enc.decrypt_value.return_value = "admin-key"

                result = get_admin_api_key("openai_api_key")

                assert result == "admin-key"

    def test_returns_plaintext_value(self):
        """Should return plaintext value if not encrypted."""
        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                mock_db.get_system_setting.return_value = "plaintext-key"
                mock_enc.is_encrypted.return_value = False

                result = get_admin_api_key("openai_api_key")

                assert result == "plaintext-key"

    def test_returns_none_when_encryption_not_ready(self):
        """Should return None when encryption is not ready."""
        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                mock_db.get_system_setting.return_value = "encrypted:key"
                mock_enc.is_encrypted.return_value = True
                mock_enc.is_encryption_ready.return_value = False

                result = get_admin_api_key("openai_api_key")

                assert result is None

    def test_returns_none_on_decryption_error(self):
        """Should return None on decryption failure."""
        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                mock_db.get_system_setting.return_value = "encrypted:bad"
                mock_enc.is_encrypted.return_value = True
                mock_enc.is_encryption_ready.return_value = True
                mock_enc.decrypt_value.side_effect = Exception("Decryption failed")

                result = get_admin_api_key("openai_api_key")

                assert result is None


# =============================================================================
# Test Profile Endpoints
# =============================================================================

class TestGetMyProfile:
    """Test GET /api/v1/me endpoint."""

    def test_returns_profile_successfully(self, mock_api_user):
        """Should return user profile data."""
        with patch("app.api.user_self_service.get_current_api_user") as mock_auth:
            mock_auth.return_value = mock_api_user

            from app.main import app
            with TestClient(app) as client:
                # Override the dependency
                app.dependency_overrides[
                    __import__("app.api.auth", fromlist=["get_current_api_user"]).get_current_api_user
                ] = lambda: mock_api_user

                response = client.get("/api/v1/me")

                assert response.status_code == 200
                data = response.json()
                assert data["id"] == "test-user-id"
                assert data["name"] == "Test User"
                assert data["username"] == "testuser"
                assert data["is_active"] is True

                app.dependency_overrides.clear()

    def test_handles_minimal_user_data(self):
        """Should handle user with minimal data (None values)."""
        minimal_user = {
            "id": "user-1",
            "name": "User",
            "is_active": True,
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z",
        }

        from app.api.auth import get_current_api_user
        from app.main import app

        with TestClient(app) as client:
            app.dependency_overrides[get_current_api_user] = lambda: minimal_user

            response = client.get("/api/v1/me")

            assert response.status_code == 200
            data = response.json()
            assert data["username"] is None
            assert data["description"] is None
            assert data["project_id"] is None

            app.dependency_overrides.clear()


class TestUpdateMyProfile:
    """Test PUT /api/v1/me endpoint."""

    def test_updates_name_successfully(self, mock_api_user):
        """Should update user name."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.update_api_user.return_value = {**mock_api_user, "name": "New Name"}

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.put("/api/v1/me", json={"name": "New Name"})

                assert response.status_code == 200
                mock_db.update_api_user.assert_called_once_with(
                    "test-user-id", name="New Name"
                )

                app.dependency_overrides.clear()

    def test_updates_description_successfully(self, mock_api_user):
        """Should update user description."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.update_api_user.return_value = {
                **mock_api_user,
                "description": "Updated description"
            }

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.put("/api/v1/me", json={"description": "Updated description"})

                assert response.status_code == 200
                mock_db.update_api_user.assert_called_once()

                app.dependency_overrides.clear()

    def test_rejects_empty_name(self, mock_api_user):
        """Should reject empty name with 400 error."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with TestClient(app) as client:
            app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

            response = client.put("/api/v1/me", json={"name": "   "})

            assert response.status_code == 400
            assert "cannot be empty" in response.json()["detail"]

            app.dependency_overrides.clear()

    def test_returns_user_when_no_updates(self, mock_api_user):
        """Should return current user when no updates provided."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with TestClient(app) as client:
            app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

            response = client.put("/api/v1/me", json={})

            assert response.status_code == 200

            app.dependency_overrides.clear()

    def test_handles_update_failure(self, mock_api_user):
        """Should return 500 when database update fails."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.update_api_user.return_value = None

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.put("/api/v1/me", json={"name": "New Name"})

                assert response.status_code == 500
                assert "Failed to update profile" in response.json()["detail"]

                app.dependency_overrides.clear()

    def test_clears_empty_description(self, mock_api_user):
        """Should set description to None when empty string provided."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.update_api_user.return_value = {**mock_api_user, "description": None}

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.put("/api/v1/me", json={"description": ""})

                assert response.status_code == 200
                mock_db.update_api_user.assert_called_once_with(
                    "test-user-id", description=None
                )

                app.dependency_overrides.clear()


class TestChangeMyPassword:
    """Test PUT /api/v1/me/password endpoint."""

    def test_changes_password_successfully(self, mock_api_user):
        """Should change password when current password is correct."""
        from app.api.auth import get_current_api_user
        from app.main import app
        import bcrypt

        # Create a real bcrypt hash for testing
        password_hash = bcrypt.hashpw(b"oldpassword", bcrypt.gensalt()).decode()
        user_with_hash = {**mock_api_user, "password_hash": password_hash}

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.update_api_user_password.return_value = True

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: user_with_hash

                response = client.put("/api/v1/me/password", json={
                    "current_password": "oldpassword",
                    "new_password": "newpassword123"
                })

                assert response.status_code == 200
                assert response.json()["success"] is True
                mock_db.update_api_user_password.assert_called_once()

                app.dependency_overrides.clear()

    def test_rejects_when_no_password_set(self, mock_api_user_no_password):
        """Should reject password change when no password is set."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with TestClient(app) as client:
            app.dependency_overrides[get_current_api_user] = lambda: mock_api_user_no_password

            response = client.put("/api/v1/me/password", json={
                "current_password": "anypassword",
                "new_password": "newpassword123"
            })

            assert response.status_code == 400
            assert "does not have a password set" in response.json()["detail"]

            app.dependency_overrides.clear()

    def test_rejects_wrong_current_password(self, mock_api_user):
        """Should reject when current password is incorrect."""
        from app.api.auth import get_current_api_user
        from app.main import app
        import bcrypt

        password_hash = bcrypt.hashpw(b"correctpassword", bcrypt.gensalt()).decode()
        user_with_hash = {**mock_api_user, "password_hash": password_hash}

        with TestClient(app) as client:
            app.dependency_overrides[get_current_api_user] = lambda: user_with_hash

            response = client.put("/api/v1/me/password", json={
                "current_password": "wrongpassword",
                "new_password": "newpassword123"
            })

            assert response.status_code == 401
            assert "incorrect" in response.json()["detail"]

            app.dependency_overrides.clear()

    def test_rejects_short_new_password(self, mock_api_user):
        """Should reject new password shorter than 8 characters."""
        from app.api.auth import get_current_api_user
        from app.main import app
        import bcrypt

        password_hash = bcrypt.hashpw(b"oldpassword", bcrypt.gensalt()).decode()
        user_with_hash = {**mock_api_user, "password_hash": password_hash}

        with TestClient(app) as client:
            app.dependency_overrides[get_current_api_user] = lambda: user_with_hash

            response = client.put("/api/v1/me/password", json={
                "current_password": "oldpassword",
                "new_password": "short"
            })

            assert response.status_code == 400
            assert "at least 8 characters" in response.json()["detail"]

            app.dependency_overrides.clear()

    def test_rejects_same_password(self, mock_api_user):
        """Should reject when new password is same as current."""
        from app.api.auth import get_current_api_user
        from app.main import app
        import bcrypt

        password_hash = bcrypt.hashpw(b"samepassword", bcrypt.gensalt()).decode()
        user_with_hash = {**mock_api_user, "password_hash": password_hash}

        with TestClient(app) as client:
            app.dependency_overrides[get_current_api_user] = lambda: user_with_hash

            response = client.put("/api/v1/me/password", json={
                "current_password": "samepassword",
                "new_password": "samepassword"
            })

            assert response.status_code == 400
            assert "different" in response.json()["detail"]

            app.dependency_overrides.clear()

    def test_handles_database_failure(self, mock_api_user):
        """Should return 500 when password update fails."""
        from app.api.auth import get_current_api_user
        from app.main import app
        import bcrypt

        password_hash = bcrypt.hashpw(b"oldpassword", bcrypt.gensalt()).decode()
        user_with_hash = {**mock_api_user, "password_hash": password_hash}

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.update_api_user_password.return_value = False

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: user_with_hash

                response = client.put("/api/v1/me/password", json={
                    "current_password": "oldpassword",
                    "new_password": "newpassword123"
                })

                assert response.status_code == 500
                assert "Failed to update password" in response.json()["detail"]

                app.dependency_overrides.clear()


# =============================================================================
# Test Credentials Endpoints
# =============================================================================

class TestGetMyCredentials:
    """Test GET /api/v1/me/credentials endpoint."""

    def test_returns_all_credentials(self, mock_api_user):
        """Should return status of all credentials."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
                with patch("app.api.user_self_service.get_admin_api_key") as mock_admin:
                    mock_db.get_effective_credential_policy.return_value = {"policy": "optional"}
                    mock_decrypt.return_value = None
                    mock_admin.return_value = None

                    with TestClient(app) as client:
                        app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                        response = client.get("/api/v1/me/credentials")

                        assert response.status_code == 200
                        data = response.json()
                        assert "credentials" in data
                        assert "has_missing_required" in data
                        assert len(data["credentials"]) == len(CREDENTIAL_INFO)

                        app.dependency_overrides.clear()

    def test_detects_missing_required_credentials(self, mock_api_user):
        """Should set has_missing_required when user_provided credential is missing."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
                with patch("app.api.user_self_service.get_admin_api_key") as mock_admin:
                    mock_db.get_effective_credential_policy.return_value = {"policy": "user_provided"}
                    mock_decrypt.return_value = None  # No user credential set
                    mock_admin.return_value = None

                    with TestClient(app) as client:
                        app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                        response = client.get("/api/v1/me/credentials")

                        assert response.status_code == 200
                        data = response.json()
                        assert data["has_missing_required"] is True

                        app.dependency_overrides.clear()

    def test_includes_masked_value_when_set(self, mock_api_user):
        """Should include masked value when credential is set."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
                with patch("app.api.user_self_service.get_admin_api_key") as mock_admin:
                    mock_db.get_effective_credential_policy.return_value = {"policy": "optional"}
                    mock_decrypt.return_value = "sk-1234567890abcdefghij"
                    mock_admin.return_value = None

                    with TestClient(app) as client:
                        app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                        response = client.get("/api/v1/me/credentials")

                        assert response.status_code == 200
                        data = response.json()
                        # All credentials should show as set with masked value
                        for cred in data["credentials"]:
                            assert cred["is_set"] is True
                            assert "..." in cred["masked_value"]

                        app.dependency_overrides.clear()

    def test_shows_admin_available(self, mock_api_user):
        """Should show admin_available when admin has set fallback."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
                with patch("app.api.user_self_service.get_admin_api_key") as mock_admin:
                    mock_db.get_effective_credential_policy.return_value = {"policy": "optional"}
                    mock_decrypt.return_value = None
                    mock_admin.return_value = "admin-key-value"

                    with TestClient(app) as client:
                        app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                        response = client.get("/api/v1/me/credentials")

                        assert response.status_code == 200
                        data = response.json()
                        # Credentials with admin_setting should show admin_available
                        for cred in data["credentials"]:
                            if CREDENTIAL_INFO[cred["credential_type"]].get("admin_setting"):
                                assert cred["admin_available"] is True

                        app.dependency_overrides.clear()


class TestGetCredentialRequirements:
    """Test GET /api/v1/me/credentials/requirements endpoint."""

    def test_returns_requirements_by_category(self, mock_api_user):
        """Should return credentials grouped by requirement category."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.get_admin_api_key") as mock_admin:
                mock_db.get_all_user_credential_policies.return_value = [
                    {"credential_type": "openai_api_key", "policy": "user_provided"},
                    {"credential_type": "gemini_api_key", "policy": "optional"},
                    {"credential_type": "meshy_api_key", "policy": "admin_provided"},
                ]
                mock_db.user_has_credential.return_value = False
                mock_admin.return_value = None

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.get("/api/v1/me/credentials/requirements")

                    assert response.status_code == 200
                    data = response.json()
                    assert "required" in data
                    assert "optional" in data
                    assert "admin_provided" in data

                    app.dependency_overrides.clear()


class TestSetMyCredential:
    """Test POST /api/v1/me/credentials/{credential_type} endpoint."""

    @pytest.mark.asyncio
    async def test_sets_credential_successfully(self, mock_api_user):
        """Should set credential when validation passes."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                with patch("app.api.user_self_service.validate_credential", new_callable=AsyncMock) as mock_validate:
                    mock_db.get_user_credential_policy.return_value = {"policy": "optional"}
                    mock_validate.return_value = CredentialValidationResponse(
                        result=ValidationResult.VALID,
                        message="Key is valid"
                    )
                    mock_enc.is_encryption_ready.return_value = True
                    mock_enc.encrypt_value.return_value = "encrypted:sk-test"

                    with TestClient(app) as client:
                        app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                        response = client.post(
                            "/api/v1/me/credentials/openai_api_key",
                            json={"value": "sk-test-api-key"}
                        )

                        assert response.status_code == 200
                        data = response.json()
                        assert data["success"] is True
                        assert data["credential_type"] == "openai_api_key"
                        mock_db.set_user_credential.assert_called_once()

                        app.dependency_overrides.clear()

    def test_rejects_invalid_credential_type(self, mock_api_user):
        """Should reject unknown credential type with 400."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with TestClient(app) as client:
            app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

            response = client.post(
                "/api/v1/me/credentials/invalid_type",
                json={"value": "some-value"}
            )

            assert response.status_code == 400
            assert "Invalid credential type" in response.json()["detail"]

            app.dependency_overrides.clear()

    def test_rejects_admin_provided_credential(self, mock_api_user):
        """Should reject setting admin_provided credential with 403."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.get_user_credential_policy.return_value = {"policy": "admin_provided"}

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.post(
                    "/api/v1/me/credentials/openai_api_key",
                    json={"value": "sk-test-key"}
                )

                assert response.status_code == 403
                assert "administrator" in response.json()["detail"]

                app.dependency_overrides.clear()

    def test_rejects_empty_value(self, mock_api_user):
        """Should reject empty credential value."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.get_user_credential_policy.return_value = {"policy": "optional"}

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.post(
                    "/api/v1/me/credentials/openai_api_key",
                    json={"value": "   "}
                )

                assert response.status_code == 400
                assert "cannot be empty" in response.json()["detail"]

                app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_rejects_invalid_credential(self, mock_api_user):
        """Should reject credential that fails validation."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.validate_credential", new_callable=AsyncMock) as mock_validate:
                mock_db.get_user_credential_policy.return_value = {"policy": "optional"}
                mock_validate.return_value = CredentialValidationResponse(
                    result=ValidationResult.INVALID,
                    message="Invalid API key"
                )

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.post(
                        "/api/v1/me/credentials/openai_api_key",
                        json={"value": "invalid-key"}
                    )

                    assert response.status_code == 400
                    assert "Invalid API key" in response.json()["detail"]

                    app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_returns_422_on_validation_error(self, mock_api_user):
        """Should return 422 when validation encounters an error."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.validate_credential", new_callable=AsyncMock) as mock_validate:
                mock_db.get_user_credential_policy.return_value = {"policy": "optional"}
                mock_validate.return_value = CredentialValidationResponse(
                    result=ValidationResult.ERROR,
                    message="Connection failed"
                )

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.post(
                        "/api/v1/me/credentials/openai_api_key",
                        json={"value": "sk-test-key"}
                    )

                    assert response.status_code == 422
                    assert "Could not validate" in response.json()["detail"]

                    app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_allows_timeout_validation(self, mock_api_user):
        """Should allow credential when validation times out."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                with patch("app.api.user_self_service.validate_credential", new_callable=AsyncMock) as mock_validate:
                    mock_db.get_user_credential_policy.return_value = {"policy": "optional"}
                    mock_validate.return_value = CredentialValidationResponse(
                        result=ValidationResult.TIMEOUT,
                        message="Validation timed out"
                    )
                    mock_enc.is_encryption_ready.return_value = True
                    mock_enc.encrypt_value.return_value = "encrypted:sk-test"

                    with TestClient(app) as client:
                        app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                        response = client.post(
                            "/api/v1/me/credentials/openai_api_key",
                            json={"value": "sk-test-api-key"}
                        )

                        # Timeout is allowed - credential should be set
                        assert response.status_code == 200

                        app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_stores_github_user_info(self, mock_api_user):
        """Should store GitHub user info when setting github_pat."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                with patch("app.api.user_self_service.validate_credential", new_callable=AsyncMock) as mock_validate:
                    mock_db.get_user_credential_policy.return_value = {"policy": "optional"}
                    mock_validate.return_value = CredentialValidationResponse(
                        result=ValidationResult.VALID,
                        message="Token is valid",
                        metadata={
                            "github_username": "octocat",
                            "github_avatar_url": "https://avatars.github.com/u/12345"
                        }
                    )
                    mock_enc.is_encryption_ready.return_value = True
                    mock_enc.encrypt_value.return_value = "encrypted:ghp_test"

                    with TestClient(app) as client:
                        app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                        response = client.post(
                            "/api/v1/me/credentials/github_pat",
                            json={"value": "ghp_test-token"}
                        )

                        assert response.status_code == 200
                        mock_db.set_user_github_config.assert_called_once_with(
                            "test-user-id",
                            github_username="octocat",
                            github_avatar_url="https://avatars.github.com/u/12345"
                        )

                        app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_stores_plaintext_when_encryption_not_ready(self, mock_api_user):
        """Should store plaintext when encryption is not ready."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                with patch("app.api.user_self_service.validate_credential", new_callable=AsyncMock) as mock_validate:
                    mock_db.get_user_credential_policy.return_value = {"policy": "optional"}
                    mock_validate.return_value = CredentialValidationResponse(
                        result=ValidationResult.VALID,
                        message="Key is valid"
                    )
                    mock_enc.is_encryption_ready.return_value = False

                    with TestClient(app) as client:
                        app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                        response = client.post(
                            "/api/v1/me/credentials/openai_api_key",
                            json={"value": "sk-test-key"}
                        )

                        assert response.status_code == 200
                        mock_db.set_user_credential.assert_called_once_with(
                            "test-user-id", "openai_api_key", "sk-test-key"
                        )

                        app.dependency_overrides.clear()


class TestDeleteMyCredential:
    """Test DELETE /api/v1/me/credentials/{credential_type} endpoint."""

    def test_deletes_credential_successfully(self, mock_api_user):
        """Should delete credential and return success."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.get_user_credential_policy.return_value = {"policy": "optional"}
            mock_db.delete_user_credential.return_value = True

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.delete("/api/v1/me/credentials/openai_api_key")

                assert response.status_code == 200
                data = response.json()
                assert data["success"] is True
                assert data["credential_type"] == "openai_api_key"
                assert data["warning"] is None

                app.dependency_overrides.clear()

    def test_warns_when_deleting_required_credential(self, mock_api_user):
        """Should return warning when deleting required credential."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.get_user_credential_policy.return_value = {"policy": "user_provided"}
            mock_db.delete_user_credential.return_value = True

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.delete("/api/v1/me/credentials/openai_api_key")

                assert response.status_code == 200
                data = response.json()
                assert "required" in data["warning"]

                app.dependency_overrides.clear()

    def test_deletes_github_config_when_removing_pat(self, mock_api_user):
        """Should delete GitHub config when removing github_pat."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.get_user_credential_policy.return_value = {"policy": "optional"}
            mock_db.delete_user_credential.return_value = True

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.delete("/api/v1/me/credentials/github_pat")

                assert response.status_code == 200
                mock_db.delete_user_github_config.assert_called_once_with("test-user-id")

                app.dependency_overrides.clear()

    def test_rejects_invalid_credential_type(self, mock_api_user):
        """Should reject unknown credential type with 400."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with TestClient(app) as client:
            app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

            response = client.delete("/api/v1/me/credentials/invalid_type")

            assert response.status_code == 400
            assert "Invalid credential type" in response.json()["detail"]

            app.dependency_overrides.clear()


# =============================================================================
# Test GitHub Endpoints
# =============================================================================

class TestGetMyGitHubConfig:
    """Test GET /api/v1/me/github endpoint."""

    def test_returns_not_connected_when_no_config(self, mock_api_user):
        """Should return connected=False when no GitHub config or PAT."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.get_user_github_config.return_value = None
            mock_db.user_has_credential.return_value = False

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.get("/api/v1/me/github")

                assert response.status_code == 200
                data = response.json()
                assert data["connected"] is False
                assert data["github_username"] is None

                app.dependency_overrides.clear()

    def test_returns_connected_with_config(self, mock_api_user):
        """Should return full config when connected."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.get_user_github_config.return_value = {
                "github_username": "octocat",
                "github_avatar_url": "https://avatars.github.com/u/12345",
                "default_repo": "octocat/hello-world",
                "default_branch": "main"
            }
            mock_db.user_has_credential.return_value = True

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.get("/api/v1/me/github")

                assert response.status_code == 200
                data = response.json()
                assert data["connected"] is True
                assert data["github_username"] == "octocat"
                assert data["default_repo"] == "octocat/hello-world"

                app.dependency_overrides.clear()

    def test_handles_pat_without_config(self, mock_api_user):
        """Should show connected when PAT exists but no config."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.get_user_github_config.return_value = None
            mock_db.user_has_credential.return_value = True

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.get("/api/v1/me/github")

                assert response.status_code == 200
                data = response.json()
                assert data["connected"] is True
                assert data["github_username"] is None

                app.dependency_overrides.clear()


class TestConnectGitHub:
    """Test POST /api/v1/me/github/connect endpoint."""

    @pytest.mark.asyncio
    async def test_connects_github_successfully(self, mock_api_user):
        """Should connect GitHub when PAT is valid."""
        from app.api.auth import get_current_api_user
        from app.main import app

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "login": "octocat",
            "avatar_url": "https://avatars.github.com/u/12345"
        }

        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                with patch("httpx.AsyncClient") as mock_client_class:
                    mock_client = AsyncMock()
                    mock_client.get = AsyncMock(return_value=mock_response)
                    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                    mock_client.__aexit__ = AsyncMock(return_value=None)
                    mock_client_class.return_value = mock_client

                    mock_enc.is_encryption_ready.return_value = True
                    mock_enc.encrypt_value.return_value = "encrypted:ghp_test"

                    with TestClient(app) as client:
                        app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                        response = client.post(
                            "/api/v1/me/github/connect",
                            json={"personal_access_token": "ghp_test_token"}
                        )

                        assert response.status_code == 200
                        data = response.json()
                        assert data["success"] is True
                        assert data["github_username"] == "octocat"
                        mock_db.set_user_credential.assert_called_once()
                        mock_db.set_user_github_config.assert_called_once()

                        app.dependency_overrides.clear()

    def test_rejects_empty_token(self, mock_api_user):
        """Should reject empty PAT with 400."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with TestClient(app) as client:
            app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

            response = client.post(
                "/api/v1/me/github/connect",
                json={"personal_access_token": "   "}
            )

            assert response.status_code == 400
            assert "cannot be empty" in response.json()["detail"]

            app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_rejects_invalid_token(self, mock_api_user):
        """Should reject invalid token with 400."""
        from app.api.auth import get_current_api_user
        from app.main import app

        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.post(
                    "/api/v1/me/github/connect",
                    json={"personal_access_token": "invalid_token"}
                )

                assert response.status_code == 400
                assert "Invalid GitHub" in response.json()["detail"]

                app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_handles_timeout(self, mock_api_user):
        """Should return 504 on timeout."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.post(
                    "/api/v1/me/github/connect",
                    json={"personal_access_token": "ghp_test"}
                )

                assert response.status_code == 504
                assert "timeout" in response.json()["detail"].lower()

                app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_handles_api_error(self, mock_api_user):
        """Should return 400 for non-200 GitHub response."""
        from app.api.auth import get_current_api_user
        from app.main import app

        mock_response = MagicMock()
        mock_response.status_code = 500

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(return_value=mock_response)
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.post(
                    "/api/v1/me/github/connect",
                    json={"personal_access_token": "ghp_test"}
                )

                assert response.status_code == 400
                assert "Failed to validate" in response.json()["detail"]

                app.dependency_overrides.clear()


class TestDisconnectGitHub:
    """Test DELETE /api/v1/me/github endpoint."""

    def test_disconnects_successfully(self, mock_api_user):
        """Should disconnect GitHub and return success."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.delete("/api/v1/me/github")

                assert response.status_code == 200
                data = response.json()
                assert data["success"] is True
                mock_db.delete_user_credential.assert_called_once_with(
                    "test-user-id", "github_pat"
                )
                mock_db.delete_user_github_config.assert_called_once_with("test-user-id")

                app.dependency_overrides.clear()


class TestListMyGitHubRepos:
    """Test GET /api/v1/me/github/repos endpoint."""

    @pytest.mark.asyncio
    async def test_lists_repos_successfully(self, mock_api_user):
        """Should list repos when connected."""
        from app.api.auth import get_current_api_user
        from app.main import app

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "full_name": "octocat/hello-world",
                "name": "hello-world",
                "owner": {"login": "octocat"},
                "private": False,
                "description": "My first repo",
                "default_branch": "main"
            }
        ]

        with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_decrypt.return_value = "ghp_test_token"

                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client_class.return_value = mock_client

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.get("/api/v1/me/github/repos")

                    assert response.status_code == 200
                    data = response.json()
                    assert len(data["repos"]) == 1
                    assert data["repos"][0]["full_name"] == "octocat/hello-world"
                    assert data["page"] == 1
                    assert data["per_page"] == 30

                    app.dependency_overrides.clear()

    def test_rejects_when_not_connected(self, mock_api_user):
        """Should return 400 when GitHub not connected."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
            mock_decrypt.return_value = None

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.get("/api/v1/me/github/repos")

                assert response.status_code == 400
                assert "not connected" in response.json()["detail"]

                app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_handles_expired_token(self, mock_api_user):
        """Should return 401 when token is expired."""
        from app.api.auth import get_current_api_user
        from app.main import app

        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_decrypt.return_value = "expired_token"

                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client_class.return_value = mock_client

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.get("/api/v1/me/github/repos")

                    assert response.status_code == 401
                    assert "expired or invalid" in response.json()["detail"]

                    app.dependency_overrides.clear()


class TestListGitHubBranches:
    """Test GET /api/v1/me/github/branches/{owner}/{repo} endpoint."""

    @pytest.mark.asyncio
    async def test_lists_branches_successfully(self, mock_api_user):
        """Should list branches when connected."""
        from app.api.auth import get_current_api_user
        from app.main import app

        branches_response = MagicMock()
        branches_response.status_code = 200
        branches_response.json.return_value = [
            {"name": "main"},
            {"name": "develop"},
            {"name": "feature/test"}
        ]

        repo_response = MagicMock()
        repo_response.status_code = 200
        repo_response.json.return_value = {"default_branch": "main"}

        with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_decrypt.return_value = "ghp_test_token"

                mock_client = AsyncMock()
                mock_client.get = AsyncMock(side_effect=[branches_response, repo_response])
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client_class.return_value = mock_client

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.get("/api/v1/me/github/branches/octocat/hello-world")

                    assert response.status_code == 200
                    data = response.json()
                    assert len(data["branches"]) == 3
                    assert data["default_branch"] == "main"

                    # Main should be marked as default
                    main_branch = next(b for b in data["branches"] if b["name"] == "main")
                    assert main_branch["is_default"] is True

                    app.dependency_overrides.clear()

    def test_rejects_when_not_connected(self, mock_api_user):
        """Should return 400 when GitHub not connected."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
            mock_decrypt.return_value = None

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.get("/api/v1/me/github/branches/octocat/hello-world")

                assert response.status_code == 400
                assert "not connected" in response.json()["detail"]

                app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_handles_repo_not_found(self, mock_api_user):
        """Should return 404 when repo not found."""
        from app.api.auth import get_current_api_user
        from app.main import app

        mock_response = MagicMock()
        mock_response.status_code = 404

        with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_decrypt.return_value = "ghp_test"

                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client_class.return_value = mock_client

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.get("/api/v1/me/github/branches/octocat/nonexistent")

                    assert response.status_code == 404
                    assert "not found" in response.json()["detail"]

                    app.dependency_overrides.clear()


class TestSetGitHubDefaults:
    """Test POST /api/v1/me/github/config endpoint."""

    def test_sets_defaults_successfully(self, mock_api_user):
        """Should set default repo and branch."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.user_has_credential.return_value = True
            mock_db.set_user_github_config.return_value = {
                "default_repo": "octocat/hello-world",
                "default_branch": "develop"
            }

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.post(
                    "/api/v1/me/github/config",
                    json={
                        "default_repo": "octocat/hello-world",
                        "default_branch": "develop"
                    }
                )

                assert response.status_code == 200
                data = response.json()
                assert data["success"] is True
                assert data["default_repo"] == "octocat/hello-world"
                assert data["default_branch"] == "develop"

                app.dependency_overrides.clear()

    def test_rejects_when_not_connected(self, mock_api_user):
        """Should return 400 when GitHub not connected."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.user_has_credential.return_value = False

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.post(
                    "/api/v1/me/github/config",
                    json={"default_repo": "octocat/hello-world"}
                )

                assert response.status_code == 400
                assert "not connected" in response.json()["detail"]

                app.dependency_overrides.clear()


# =============================================================================
# Test Module and Router
# =============================================================================

class TestModuleImports:
    """Verify module can be imported correctly."""

    def test_module_imports(self):
        """Module should import without errors."""
        from app.api import user_self_service
        assert user_self_service is not None

    def test_router_exists(self):
        """Router should exist."""
        from app.api.user_self_service import router
        assert router is not None

    def test_router_has_correct_prefix(self):
        """Router should have correct prefix."""
        from app.api.user_self_service import router
        assert router.prefix == "/api/v1/me"

    def test_credential_info_exists(self):
        """CREDENTIAL_INFO should be defined."""
        from app.api.user_self_service import CREDENTIAL_INFO
        assert CREDENTIAL_INFO is not None
        assert "openai_api_key" in CREDENTIAL_INFO
        assert "gemini_api_key" in CREDENTIAL_INFO
        assert "meshy_api_key" in CREDENTIAL_INFO
        assert "github_pat" in CREDENTIAL_INFO

    def test_all_credential_info_has_required_fields(self):
        """Each credential type should have required info fields."""
        from app.api.user_self_service import CREDENTIAL_INFO

        for cred_type, info in CREDENTIAL_INFO.items():
            assert "name" in info, f"{cred_type} missing 'name'"
            assert "description" in info, f"{cred_type} missing 'description'"
            assert "admin_setting" in info, f"{cred_type} missing 'admin_setting'"


# =============================================================================
# Test Edge Cases
# =============================================================================

class TestAdditionalGitHubErrorCases:
    """Additional tests for GitHub error handling edge cases."""

    @pytest.mark.asyncio
    async def test_connect_github_generic_exception(self, mock_api_user):
        """Should return 500 for generic exception during connect."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("httpx.AsyncClient") as mock_client_class:
            mock_client = AsyncMock()
            mock_client.get = AsyncMock(side_effect=Exception("Network error"))
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_class.return_value = mock_client

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.post(
                    "/api/v1/me/github/connect",
                    json={"personal_access_token": "ghp_test"}
                )

                assert response.status_code == 500
                assert "Failed to connect to GitHub" in response.json()["detail"]

                app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_connect_github_stores_plaintext_when_encryption_not_ready(self, mock_api_user):
        """Should store PAT as plaintext when encryption is not ready."""
        from app.api.auth import get_current_api_user
        from app.main import app

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "login": "octocat",
            "avatar_url": "https://avatars.github.com/u/12345"
        }

        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                with patch("httpx.AsyncClient") as mock_client_class:
                    mock_client = AsyncMock()
                    mock_client.get = AsyncMock(return_value=mock_response)
                    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                    mock_client.__aexit__ = AsyncMock(return_value=None)
                    mock_client_class.return_value = mock_client

                    mock_enc.is_encryption_ready.return_value = False

                    with TestClient(app) as client:
                        app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                        response = client.post(
                            "/api/v1/me/github/connect",
                            json={"personal_access_token": "ghp_plaintext_token"}
                        )

                        assert response.status_code == 200
                        # PAT should be stored as plaintext
                        mock_db.set_user_credential.assert_called_once_with(
                            "test-user-id", "github_pat", "ghp_plaintext_token"
                        )

                        app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_repos_non_200_status_code(self, mock_api_user):
        """Should return error for non-200/401 status code."""
        from app.api.auth import get_current_api_user
        from app.main import app

        mock_response = MagicMock()
        mock_response.status_code = 403

        with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_decrypt.return_value = "ghp_test"

                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client_class.return_value = mock_client

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.get("/api/v1/me/github/repos")

                    assert response.status_code == 403
                    assert "Failed to list repositories" in response.json()["detail"]

                    app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_repos_timeout(self, mock_api_user):
        """Should return 504 on timeout when listing repos."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_decrypt.return_value = "ghp_test"

                mock_client = AsyncMock()
                mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client_class.return_value = mock_client

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.get("/api/v1/me/github/repos")

                    assert response.status_code == 504
                    assert "timeout" in response.json()["detail"].lower()

                    app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_repos_generic_exception(self, mock_api_user):
        """Should return 500 for generic exception when listing repos."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_decrypt.return_value = "ghp_test"

                mock_client = AsyncMock()
                mock_client.get = AsyncMock(side_effect=Exception("Connection error"))
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client_class.return_value = mock_client

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.get("/api/v1/me/github/repos")

                    assert response.status_code == 500
                    assert "Failed to list repos" in response.json()["detail"]

                    app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_branches_401_status(self, mock_api_user):
        """Should return 401 when token is expired for branches."""
        from app.api.auth import get_current_api_user
        from app.main import app

        mock_response = MagicMock()
        mock_response.status_code = 401

        with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_decrypt.return_value = "ghp_test"

                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client_class.return_value = mock_client

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.get("/api/v1/me/github/branches/octocat/hello")

                    assert response.status_code == 401
                    assert "expired or invalid" in response.json()["detail"]

                    app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_branches_non_200_status(self, mock_api_user):
        """Should return error for non-standard status codes on branches."""
        from app.api.auth import get_current_api_user
        from app.main import app

        mock_response = MagicMock()
        mock_response.status_code = 403

        with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_decrypt.return_value = "ghp_test"

                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client_class.return_value = mock_client

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.get("/api/v1/me/github/branches/octocat/hello")

                    assert response.status_code == 403
                    assert "Failed to list branches" in response.json()["detail"]

                    app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_branches_timeout(self, mock_api_user):
        """Should return 504 on timeout when listing branches."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_decrypt.return_value = "ghp_test"

                mock_client = AsyncMock()
                mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("Timeout"))
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client_class.return_value = mock_client

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.get("/api/v1/me/github/branches/octocat/hello")

                    assert response.status_code == 504
                    assert "timeout" in response.json()["detail"].lower()

                    app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_branches_generic_exception(self, mock_api_user):
        """Should return 500 for generic exception when listing branches."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_decrypt.return_value = "ghp_test"

                mock_client = AsyncMock()
                mock_client.get = AsyncMock(side_effect=Exception("Network failure"))
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client_class.return_value = mock_client

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.get("/api/v1/me/github/branches/octocat/hello")

                    assert response.status_code == 500
                    assert "Failed to list branches" in response.json()["detail"]

                    app.dependency_overrides.clear()


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_unauthenticated_access(self):
        """Should return 401 for unauthenticated requests."""
        from app.main import app

        with TestClient(app) as client:
            response = client.get("/api/v1/me")
            assert response.status_code == 401

    def test_special_characters_in_name(self, mock_api_user):
        """Should handle special characters in name."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.update_api_user.return_value = {
                **mock_api_user,
                "name": "Test User <script>alert('xss')</script>"
            }

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.put("/api/v1/me", json={
                    "name": "Test User <script>alert('xss')</script>"
                })

                # Should accept the name (sanitization should happen at render time)
                assert response.status_code == 200

                app.dependency_overrides.clear()

    def test_unicode_in_description(self, mock_api_user):
        """Should handle unicode characters in description."""
        from app.api.auth import get_current_api_user
        from app.main import app

        with patch("app.api.user_self_service.database") as mock_db:
            mock_db.update_api_user.return_value = {
                **mock_api_user,
                "description": "Test with emojis"
            }

            with TestClient(app) as client:
                app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                response = client.put("/api/v1/me", json={
                    "description": "Test with emojis"
                })

                assert response.status_code == 200

                app.dependency_overrides.clear()

    def test_very_long_credential_value(self, mock_api_user):
        """Should handle very long credential values."""
        from app.api.auth import get_current_api_user
        from app.main import app

        long_key = "sk-" + "a" * 1000

        with patch("app.api.user_self_service.database") as mock_db:
            with patch("app.api.user_self_service.encryption") as mock_enc:
                with patch("app.api.user_self_service.validate_credential", new_callable=AsyncMock) as mock_validate:
                    mock_db.get_user_credential_policy.return_value = {"policy": "optional"}
                    mock_validate.return_value = CredentialValidationResponse(
                        result=ValidationResult.VALID,
                        message="Valid"
                    )
                    mock_enc.is_encryption_ready.return_value = True
                    mock_enc.encrypt_value.return_value = f"encrypted:{long_key}"

                    with TestClient(app) as client:
                        app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                        response = client.post(
                            "/api/v1/me/credentials/openai_api_key",
                            json={"value": long_key}
                        )

                        assert response.status_code == 200

                        app.dependency_overrides.clear()

    @pytest.mark.asyncio
    async def test_github_repos_pagination(self, mock_api_user):
        """Should pass pagination parameters correctly."""
        from app.api.auth import get_current_api_user
        from app.main import app

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []

        with patch("app.api.user_self_service.get_decrypted_user_credential") as mock_decrypt:
            with patch("httpx.AsyncClient") as mock_client_class:
                mock_decrypt.return_value = "ghp_test"

                mock_client = AsyncMock()
                mock_client.get = AsyncMock(return_value=mock_response)
                mock_client.__aenter__ = AsyncMock(return_value=mock_client)
                mock_client.__aexit__ = AsyncMock(return_value=None)
                mock_client_class.return_value = mock_client

                with TestClient(app) as client:
                    app.dependency_overrides[get_current_api_user] = lambda: mock_api_user

                    response = client.get(
                        "/api/v1/me/github/repos",
                        params={"page": 2, "per_page": 50}
                    )

                    assert response.status_code == 200
                    data = response.json()
                    assert data["page"] == 2
                    assert data["per_page"] == 50

                    app.dependency_overrides.clear()
