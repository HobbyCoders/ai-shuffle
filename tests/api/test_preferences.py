"""
Comprehensive tests for the preferences API endpoints.

Tests cover:
- Preference CRUD operations (get, set, delete)
- User identity resolution (API user vs admin)
- Authentication requirements
- Error handling and edge cases
- Different value types (strings, numbers, objects, arrays)
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock
from typing import Any


class TestPreferencesModuleImports:
    """Verify preferences module can be imported correctly."""

    def test_preferences_module_imports(self):
        """Preferences module should import without errors."""
        from app.api import preferences
        assert preferences is not None

    def test_preferences_router_exists(self):
        """Preferences router should exist."""
        from app.api.preferences import router
        assert router is not None

    def test_preferences_router_prefix(self):
        """Preferences router should have correct prefix."""
        from app.api.preferences import router
        assert router.prefix == "/api/v1/preferences"

    def test_preferences_router_tags(self):
        """Preferences router should have correct tags."""
        from app.api.preferences import router
        assert "Preferences" in router.tags


class TestPreferenceModels:
    """Test Pydantic models for preference validation."""

    def test_preference_value_valid(self):
        """PreferenceValue should accept valid data."""
        from app.api.preferences import PreferenceValue

        pref = PreferenceValue(key="test_key", value="test_value")
        assert pref.key == "test_key"
        assert pref.value == "test_value"

    def test_preference_value_with_dict(self):
        """PreferenceValue should accept dict values."""
        from app.api.preferences import PreferenceValue

        pref = PreferenceValue(key="settings", value={"theme": "dark", "fontSize": 14})
        assert pref.key == "settings"
        assert pref.value == {"theme": "dark", "fontSize": 14}

    def test_preference_value_with_list(self):
        """PreferenceValue should accept list values."""
        from app.api.preferences import PreferenceValue

        pref = PreferenceValue(key="openTabs", value=["tab1", "tab2", "tab3"])
        assert pref.key == "openTabs"
        assert pref.value == ["tab1", "tab2", "tab3"]

    def test_preference_value_with_number(self):
        """PreferenceValue should accept numeric values."""
        from app.api.preferences import PreferenceValue

        pref = PreferenceValue(key="scrollPosition", value=150)
        assert pref.key == "scrollPosition"
        assert pref.value == 150

    def test_preference_value_with_boolean(self):
        """PreferenceValue should accept boolean values."""
        from app.api.preferences import PreferenceValue

        pref = PreferenceValue(key="showSidebar", value=True)
        assert pref.key == "showSidebar"
        assert pref.value is True

    def test_preference_value_with_null(self):
        """PreferenceValue should accept null values."""
        from app.api.preferences import PreferenceValue

        pref = PreferenceValue(key="activeSelection", value=None)
        assert pref.key == "activeSelection"
        assert pref.value is None

    def test_preference_response_valid(self):
        """PreferenceResponse should accept valid data."""
        from app.api.preferences import PreferenceResponse

        resp = PreferenceResponse(
            key="test_key",
            value="test_value",
            updated_at="2024-01-01T00:00:00"
        )
        assert resp.key == "test_key"
        assert resp.value == "test_value"
        assert resp.updated_at == "2024-01-01T00:00:00"

    def test_preference_response_optional_updated_at(self):
        """PreferenceResponse should allow None updated_at."""
        from app.api.preferences import PreferenceResponse

        resp = PreferenceResponse(key="test_key", value="test_value")
        assert resp.key == "test_key"
        assert resp.value == "test_value"
        assert resp.updated_at is None


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def sample_preference():
    """Sample preference data."""
    return {
        "id": 1,
        "user_type": "api_user",
        "user_id": "test-api-user-id",
        "key": "openTabs",
        "value": ["session-1", "session-2"],
        "updated_at": "2024-01-01T00:00:00"
    }


@pytest.fixture
def sample_api_user():
    """Sample API user data."""
    return {
        "id": "test-api-user-id",
        "username": "test-user",
        "role": "user"
    }


@pytest.fixture
def mock_request():
    """Mock FastAPI Request object."""
    request = MagicMock()
    request.state = MagicMock()
    return request


# =============================================================================
# User Identity Tests
# =============================================================================

class TestGetUserIdentity:
    """Test the get_user_identity helper function."""

    def test_get_user_identity_api_user(self, mock_request):
        """Should return api_user type when request has API user."""
        from app.api.preferences import get_user_identity

        with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
            mock_get_api_user.return_value = {
                "id": "api-user-123",
                "username": "testuser"
            }

            user_type, user_id = get_user_identity(mock_request)

        assert user_type == "api_user"
        assert user_id == "api-user-123"

    def test_get_user_identity_admin_user(self, mock_request):
        """Should return admin type when request is from admin."""
        from app.api.preferences import get_user_identity

        with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
            mock_get_api_user.return_value = None

            with patch("app.api.preferences.auth_service") as mock_auth:
                mock_auth.get_admin_username.return_value = "admin_user"

                user_type, user_id = get_user_identity(mock_request)

        assert user_type == "admin"
        assert user_id == "admin_user"

    def test_get_user_identity_admin_fallback(self, mock_request):
        """Should use 'admin' as fallback when no username."""
        from app.api.preferences import get_user_identity

        with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
            mock_get_api_user.return_value = None

            with patch("app.api.preferences.auth_service") as mock_auth:
                mock_auth.get_admin_username.return_value = None

                user_type, user_id = get_user_identity(mock_request)

        assert user_type == "admin"
        assert user_id == "admin"


# =============================================================================
# GET Preference Tests
# =============================================================================

class TestGetPreference:
    """Test get_preference endpoint function."""

    @pytest.mark.asyncio
    async def test_get_preference_success(self, mock_request, sample_preference):
        """Should return preference when it exists."""
        from app.api.preferences import get_preference

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.get_user_preference.return_value = {
                    "key": "openTabs",
                    "value": ["session-1", "session-2"],
                    "updated_at": "2024-01-01T00:00:00"
                }

                result = await get_preference(
                    request=mock_request,
                    key="openTabs",
                    token="test-token"
                )

        assert result is not None
        assert result.key == "openTabs"
        assert result.value == ["session-1", "session-2"]
        assert result.updated_at == "2024-01-01T00:00:00"
        mock_database.get_user_preference.assert_called_once_with(
            "api_user", "test-api-user-id", "openTabs"
        )

    @pytest.mark.asyncio
    async def test_get_preference_not_found(self, mock_request):
        """Should return None when preference does not exist."""
        from app.api.preferences import get_preference

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.get_user_preference.return_value = None

                result = await get_preference(
                    request=mock_request,
                    key="nonexistent",
                    token="test-token"
                )

        assert result is None

    @pytest.mark.asyncio
    async def test_get_preference_admin_user(self, mock_request):
        """Should use admin identity for admin users."""
        from app.api.preferences import get_preference

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                with patch("app.api.preferences.auth_service") as mock_auth_service:
                    mock_get_api_user.return_value = None
                    mock_auth_service.get_admin_username.return_value = "myadmin"
                    mock_database.get_user_preference.return_value = {
                        "key": "theme",
                        "value": "dark",
                        "updated_at": "2024-01-01T00:00:00"
                    }

                    result = await get_preference(
                        request=mock_request,
                        key="theme",
                        token="test-token"
                    )

        assert result is not None
        assert result.key == "theme"
        assert result.value == "dark"
        mock_database.get_user_preference.assert_called_once_with(
            "admin", "myadmin", "theme"
        )

    @pytest.mark.asyncio
    async def test_get_preference_complex_value(self, mock_request):
        """Should return complex nested values correctly."""
        from app.api.preferences import get_preference

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.get_user_preference.return_value = {
                    "key": "uiState",
                    "value": {
                        "sidebar": {"collapsed": False, "width": 250},
                        "tabs": ["tab1", "tab2"],
                        "zoom": 1.5
                    },
                    "updated_at": "2024-01-01T00:00:00"
                }

                result = await get_preference(
                    request=mock_request,
                    key="uiState",
                    token="test-token"
                )

        assert result is not None
        assert result.value["sidebar"]["width"] == 250
        assert result.value["tabs"] == ["tab1", "tab2"]
        assert result.value["zoom"] == 1.5

    @pytest.mark.asyncio
    async def test_get_preference_without_updated_at(self, mock_request):
        """Should handle preference without updated_at field."""
        from app.api.preferences import get_preference

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.get_user_preference.return_value = {
                    "key": "simple",
                    "value": "test"
                }

                result = await get_preference(
                    request=mock_request,
                    key="simple",
                    token="test-token"
                )

        assert result is not None
        assert result.key == "simple"
        assert result.value == "test"
        assert result.updated_at is None

    @pytest.mark.asyncio
    async def test_get_preference_admin_fallback(self, mock_request):
        """Should use 'admin' as fallback when no admin username."""
        from app.api.preferences import get_preference

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                with patch("app.api.preferences.auth_service") as mock_auth_service:
                    mock_get_api_user.return_value = None
                    mock_auth_service.get_admin_username.return_value = None
                    mock_database.get_user_preference.return_value = None

                    result = await get_preference(
                        request=mock_request,
                        key="somePref",
                        token="test-token"
                    )

        mock_database.get_user_preference.assert_called_once_with(
            "admin", "admin", "somePref"
        )


# =============================================================================
# PUT Preference Tests
# =============================================================================

class TestSetPreference:
    """Test set_preference endpoint function."""

    @pytest.mark.asyncio
    async def test_set_preference_success(self, mock_request):
        """Should set preference successfully."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.set_user_preference.return_value = {
                    "key": "openTabs",
                    "value": ["session-1", "session-2", "session-3"],
                    "updated_at": "2024-01-02T00:00:00"
                }

                body = PreferenceValue(
                    key="openTabs",
                    value=["session-1", "session-2", "session-3"]
                )
                result = await set_preference(
                    request=mock_request,
                    key="openTabs",
                    body=body,
                    token="test-token"
                )

        assert result.key == "openTabs"
        assert result.value == ["session-1", "session-2", "session-3"]
        assert result.updated_at == "2024-01-02T00:00:00"
        mock_database.set_user_preference.assert_called_once_with(
            "api_user", "test-api-user-id", "openTabs",
            ["session-1", "session-2", "session-3"]
        )

    @pytest.mark.asyncio
    async def test_set_preference_string_value(self, mock_request):
        """Should set string preference value."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.set_user_preference.return_value = {
                    "key": "theme",
                    "value": "dark",
                    "updated_at": "2024-01-02T00:00:00"
                }

                body = PreferenceValue(key="theme", value="dark")
                result = await set_preference(
                    request=mock_request,
                    key="theme",
                    body=body,
                    token="test-token"
                )

        assert result.key == "theme"
        assert result.value == "dark"

    @pytest.mark.asyncio
    async def test_set_preference_number_value(self, mock_request):
        """Should set numeric preference value."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.set_user_preference.return_value = {
                    "key": "fontSize",
                    "value": 16,
                    "updated_at": "2024-01-02T00:00:00"
                }

                body = PreferenceValue(key="fontSize", value=16)
                result = await set_preference(
                    request=mock_request,
                    key="fontSize",
                    body=body,
                    token="test-token"
                )

        assert result.key == "fontSize"
        assert result.value == 16

    @pytest.mark.asyncio
    async def test_set_preference_boolean_value(self, mock_request):
        """Should set boolean preference value."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.set_user_preference.return_value = {
                    "key": "showSidebar",
                    "value": False,
                    "updated_at": "2024-01-02T00:00:00"
                }

                body = PreferenceValue(key="showSidebar", value=False)
                result = await set_preference(
                    request=mock_request,
                    key="showSidebar",
                    body=body,
                    token="test-token"
                )

        assert result.key == "showSidebar"
        assert result.value is False

    @pytest.mark.asyncio
    async def test_set_preference_null_value(self, mock_request):
        """Should set null preference value."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.set_user_preference.return_value = {
                    "key": "selectedItem",
                    "value": None,
                    "updated_at": "2024-01-02T00:00:00"
                }

                body = PreferenceValue(key="selectedItem", value=None)
                result = await set_preference(
                    request=mock_request,
                    key="selectedItem",
                    body=body,
                    token="test-token"
                )

        assert result.key == "selectedItem"
        assert result.value is None

    @pytest.mark.asyncio
    async def test_set_preference_complex_object(self, mock_request):
        """Should set complex nested object value."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                complex_value = {
                    "layout": {
                        "sidebar": {"width": 300, "collapsed": False},
                        "panels": [
                            {"id": "main", "size": 70},
                            {"id": "details", "size": 30}
                        ]
                    },
                    "settings": {"autoSave": True, "syncInterval": 5000}
                }
                mock_database.set_user_preference.return_value = {
                    "key": "workspaceState",
                    "value": complex_value,
                    "updated_at": "2024-01-02T00:00:00"
                }

                body = PreferenceValue(key="workspaceState", value=complex_value)
                result = await set_preference(
                    request=mock_request,
                    key="workspaceState",
                    body=body,
                    token="test-token"
                )

        assert result.key == "workspaceState"
        assert result.value["layout"]["sidebar"]["width"] == 300
        assert result.value["settings"]["autoSave"] is True

    @pytest.mark.asyncio
    async def test_set_preference_admin_user(self, mock_request):
        """Should use admin identity for admin users."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                with patch("app.api.preferences.auth_service") as mock_auth_service:
                    mock_get_api_user.return_value = None
                    mock_auth_service.get_admin_username.return_value = "superadmin"
                    mock_database.set_user_preference.return_value = {
                        "key": "adminSetting",
                        "value": "secret",
                        "updated_at": "2024-01-02T00:00:00"
                    }

                    body = PreferenceValue(key="adminSetting", value="secret")
                    result = await set_preference(
                        request=mock_request,
                        key="adminSetting",
                        body=body,
                        token="test-token"
                    )

        mock_database.set_user_preference.assert_called_once_with(
            "admin", "superadmin", "adminSetting", "secret"
        )

    @pytest.mark.asyncio
    async def test_set_preference_without_updated_at(self, mock_request):
        """Should handle response without updated_at field."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.set_user_preference.return_value = {
                    "key": "simple",
                    "value": "test"
                }

                body = PreferenceValue(key="simple", value="test")
                result = await set_preference(
                    request=mock_request,
                    key="simple",
                    body=body,
                    token="test-token"
                )

        assert result.key == "simple"
        assert result.value == "test"
        assert result.updated_at is None

    @pytest.mark.asyncio
    async def test_set_preference_update_existing(self, mock_request):
        """Should update existing preference (upsert behavior)."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                # Simulate an update (set_user_preference handles upsert)
                mock_database.set_user_preference.return_value = {
                    "key": "existingKey",
                    "value": "newValue",
                    "updated_at": "2024-01-03T00:00:00"
                }

                body = PreferenceValue(key="existingKey", value="newValue")
                result = await set_preference(
                    request=mock_request,
                    key="existingKey",
                    body=body,
                    token="test-token"
                )

        assert result.value == "newValue"

    @pytest.mark.asyncio
    async def test_set_preference_empty_string_value(self, mock_request):
        """Should handle empty string values."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.set_user_preference.return_value = {
                    "key": "emptyValue",
                    "value": "",
                    "updated_at": "2024-01-02T00:00:00"
                }

                body = PreferenceValue(key="emptyValue", value="")
                result = await set_preference(
                    request=mock_request,
                    key="emptyValue",
                    body=body,
                    token="test-token"
                )

        assert result.value == ""

    @pytest.mark.asyncio
    async def test_set_preference_empty_array_value(self, mock_request):
        """Should handle empty array values."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.set_user_preference.return_value = {
                    "key": "emptyList",
                    "value": [],
                    "updated_at": "2024-01-02T00:00:00"
                }

                body = PreferenceValue(key="emptyList", value=[])
                result = await set_preference(
                    request=mock_request,
                    key="emptyList",
                    body=body,
                    token="test-token"
                )

        assert result.value == []

    @pytest.mark.asyncio
    async def test_set_preference_empty_object_value(self, mock_request):
        """Should handle empty object values."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.set_user_preference.return_value = {
                    "key": "emptyObj",
                    "value": {},
                    "updated_at": "2024-01-02T00:00:00"
                }

                body = PreferenceValue(key="emptyObj", value={})
                result = await set_preference(
                    request=mock_request,
                    key="emptyObj",
                    body=body,
                    token="test-token"
                )

        assert result.value == {}

    @pytest.mark.asyncio
    async def test_set_preference_large_value(self, mock_request):
        """Should handle large values."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                large_value = {"items": [f"item-{i}" for i in range(1000)]}
                mock_database.set_user_preference.return_value = {
                    "key": "largeData",
                    "value": large_value,
                    "updated_at": "2024-01-02T00:00:00"
                }

                body = PreferenceValue(key="largeData", value=large_value)
                result = await set_preference(
                    request=mock_request,
                    key="largeData",
                    body=body,
                    token="test-token"
                )

        assert len(result.value["items"]) == 1000


# =============================================================================
# DELETE Preference Tests
# =============================================================================

class TestDeletePreference:
    """Test delete_preference endpoint function."""

    @pytest.mark.asyncio
    async def test_delete_preference_success(self, mock_request):
        """Should delete preference successfully."""
        from app.api.preferences import delete_preference

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.delete_user_preference.return_value = True

                result = await delete_preference(
                    request=mock_request,
                    key="openTabs",
                    token="test-token"
                )

        assert result["deleted"] is True
        mock_database.delete_user_preference.assert_called_once_with(
            "api_user", "test-api-user-id", "openTabs"
        )

    @pytest.mark.asyncio
    async def test_delete_preference_not_found(self, mock_request):
        """Should return deleted=False when preference does not exist."""
        from app.api.preferences import delete_preference

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.delete_user_preference.return_value = False

                result = await delete_preference(
                    request=mock_request,
                    key="nonexistent",
                    token="test-token"
                )

        assert result["deleted"] is False

    @pytest.mark.asyncio
    async def test_delete_preference_admin_user(self, mock_request):
        """Should use admin identity for admin users."""
        from app.api.preferences import delete_preference

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                with patch("app.api.preferences.auth_service") as mock_auth_service:
                    mock_get_api_user.return_value = None
                    mock_auth_service.get_admin_username.return_value = "admin123"
                    mock_database.delete_user_preference.return_value = True

                    result = await delete_preference(
                        request=mock_request,
                        key="adminPref",
                        token="test-token"
                    )

        assert result["deleted"] is True
        mock_database.delete_user_preference.assert_called_once_with(
            "admin", "admin123", "adminPref"
        )

    @pytest.mark.asyncio
    async def test_delete_preference_admin_fallback(self, mock_request):
        """Should use 'admin' as fallback when no username."""
        from app.api.preferences import delete_preference

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                with patch("app.api.preferences.auth_service") as mock_auth_service:
                    mock_get_api_user.return_value = None
                    mock_auth_service.get_admin_username.return_value = None
                    mock_database.delete_user_preference.return_value = True

                    result = await delete_preference(
                        request=mock_request,
                        key="somePref",
                        token="test-token"
                    )

        mock_database.delete_user_preference.assert_called_once_with(
            "admin", "admin", "somePref"
        )


# =============================================================================
# Edge Cases and Value Types Tests
# =============================================================================

class TestPreferencesEdgeCases:
    """Test edge cases and different value types."""

    @pytest.mark.asyncio
    async def test_get_preference_special_characters_in_key(self, mock_request):
        """Should handle keys with special characters."""
        from app.api.preferences import get_preference

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.get_user_preference.return_value = {
                    "key": "ui.settings.theme",
                    "value": "dark",
                    "updated_at": "2024-01-01T00:00:00"
                }

                result = await get_preference(
                    request=mock_request,
                    key="ui.settings.theme",
                    token="test-token"
                )

        assert result is not None
        mock_database.get_user_preference.assert_called_once_with(
            "api_user", "test-api-user-id", "ui.settings.theme"
        )

    @pytest.mark.asyncio
    async def test_set_preference_float_value(self, mock_request):
        """Should handle float values."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.set_user_preference.return_value = {
                    "key": "zoomLevel",
                    "value": 1.75,
                    "updated_at": "2024-01-02T00:00:00"
                }

                body = PreferenceValue(key="zoomLevel", value=1.75)
                result = await set_preference(
                    request=mock_request,
                    key="zoomLevel",
                    body=body,
                    token="test-token"
                )

        assert result.value == 1.75

    @pytest.mark.asyncio
    async def test_set_preference_negative_number(self, mock_request):
        """Should handle negative numbers."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                mock_database.set_user_preference.return_value = {
                    "key": "offset",
                    "value": -100,
                    "updated_at": "2024-01-02T00:00:00"
                }

                body = PreferenceValue(key="offset", value=-100)
                result = await set_preference(
                    request=mock_request,
                    key="offset",
                    body=body,
                    token="test-token"
                )

        assert result.value == -100

    @pytest.mark.asyncio
    async def test_set_preference_nested_arrays(self, mock_request):
        """Should handle deeply nested arrays."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                mock_get_api_user.return_value = {
                    "id": "test-api-user-id",
                    "username": "test-user"
                }
                nested_value = {
                    "matrix": [[1, 2, 3], [4, 5, 6], [7, 8, 9]],
                    "tags": [["a", "b"], ["c", "d"]]
                }
                mock_database.set_user_preference.return_value = {
                    "key": "nestedData",
                    "value": nested_value,
                    "updated_at": "2024-01-02T00:00:00"
                }

                body = PreferenceValue(key="nestedData", value=nested_value)
                result = await set_preference(
                    request=mock_request,
                    key="nestedData",
                    body=body,
                    token="test-token"
                )

        assert result.value["matrix"][0] == [1, 2, 3]
        assert result.value["tags"][1] == ["c", "d"]


# =============================================================================
# User Isolation Tests
# =============================================================================

class TestUserIsolation:
    """Test that preferences are isolated between users."""

    @pytest.mark.asyncio
    async def test_different_api_users_get_different_preferences(self, mock_request):
        """Different API users should have separate preference namespaces."""
        from app.api.preferences import get_preference

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                # First user
                mock_get_api_user.return_value = {"id": "user-1", "username": "user1"}
                mock_database.get_user_preference.return_value = {
                    "key": "theme",
                    "value": "dark"
                }

                await get_preference(
                    request=mock_request,
                    key="theme",
                    token="test-token"
                )

                # Verify called with first user's ID
                mock_database.get_user_preference.assert_called_with(
                    "api_user", "user-1", "theme"
                )

                # Second user
                mock_get_api_user.return_value = {"id": "user-2", "username": "user2"}
                mock_database.get_user_preference.return_value = {
                    "key": "theme",
                    "value": "light"
                }

                await get_preference(
                    request=mock_request,
                    key="theme",
                    token="test-token"
                )

                # Verify called with second user's ID
                mock_database.get_user_preference.assert_called_with(
                    "api_user", "user-2", "theme"
                )

    @pytest.mark.asyncio
    async def test_api_user_and_admin_have_separate_preferences(self, mock_request):
        """API users and admin users should have separate preference namespaces."""
        from app.api.preferences import set_preference, PreferenceValue

        with patch("app.api.preferences.database") as mock_database:
            with patch("app.api.preferences.get_api_user_from_request") as mock_get_api_user:
                with patch("app.api.preferences.auth_service") as mock_auth_service:
                    # API user request
                    mock_get_api_user.return_value = {"id": "api-user-1", "username": "apiuser"}
                    mock_database.set_user_preference.return_value = {
                        "key": "theme",
                        "value": "dark",
                        "updated_at": "2024-01-01T00:00:00"
                    }

                    body = PreferenceValue(key="theme", value="dark")
                    await set_preference(
                        request=mock_request,
                        key="theme",
                        body=body,
                        token="test-token"
                    )

                    first_call = mock_database.set_user_preference.call_args
                    assert first_call[0][0] == "api_user"
                    assert first_call[0][1] == "api-user-1"

                    # Admin user request
                    mock_get_api_user.return_value = None
                    mock_auth_service.get_admin_username.return_value = "admin"

                    body = PreferenceValue(key="theme", value="light")
                    await set_preference(
                        request=mock_request,
                        key="theme",
                        body=body,
                        token="test-token"
                    )

                    second_call = mock_database.set_user_preference.call_args
                    assert second_call[0][0] == "admin"
                    assert second_call[0][1] == "admin"


# =============================================================================
# PreferenceValue Model Validation Tests
# =============================================================================

class TestPreferenceValueValidation:
    """Test PreferenceValue Pydantic model validation."""

    def test_preference_value_requires_key(self):
        """PreferenceValue should require key field."""
        from app.api.preferences import PreferenceValue
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            PreferenceValue(value="test")

    def test_preference_value_empty_key_allowed(self):
        """PreferenceValue should allow empty key (not recommended but valid)."""
        from app.api.preferences import PreferenceValue

        pref = PreferenceValue(key="", value="test")
        assert pref.key == ""

    def test_preference_value_unicode_key(self):
        """PreferenceValue should handle unicode keys."""
        from app.api.preferences import PreferenceValue

        pref = PreferenceValue(key="theme_emoji", value="test")
        assert pref.key == "theme_emoji"

    def test_preference_value_unicode_value(self):
        """PreferenceValue should handle unicode values."""
        from app.api.preferences import PreferenceValue

        pref = PreferenceValue(key="message", value="Hello World")
        assert pref.value == "Hello World"

    def test_preference_value_with_special_json_chars(self):
        """PreferenceValue should handle values with special JSON characters."""
        from app.api.preferences import PreferenceValue

        pref = PreferenceValue(key="code", value='function() { return "test"; }')
        assert pref.value == 'function() { return "test"; }'


# =============================================================================
# PreferenceResponse Model Tests
# =============================================================================

class TestPreferenceResponseModel:
    """Test PreferenceResponse Pydantic model."""

    def test_preference_response_serialization(self):
        """PreferenceResponse should serialize correctly."""
        from app.api.preferences import PreferenceResponse

        resp = PreferenceResponse(
            key="test",
            value={"nested": True},
            updated_at="2024-01-01T00:00:00"
        )
        data = resp.model_dump()

        assert data["key"] == "test"
        assert data["value"] == {"nested": True}
        assert data["updated_at"] == "2024-01-01T00:00:00"

    def test_preference_response_with_list_value(self):
        """PreferenceResponse should handle list values."""
        from app.api.preferences import PreferenceResponse

        resp = PreferenceResponse(
            key="tabs",
            value=["tab1", "tab2", "tab3"]
        )

        assert resp.value == ["tab1", "tab2", "tab3"]

    def test_preference_response_with_none_value(self):
        """PreferenceResponse should handle None values."""
        from app.api.preferences import PreferenceResponse

        resp = PreferenceResponse(
            key="selection",
            value=None
        )

        assert resp.value is None
