"""
Unit tests for import/export API endpoints.

Tests cover:
- Export endpoints (profiles, subagents, single items, all)
- Import endpoints (file upload, JSON body)
- Validation and error handling
- Authentication and authorization
- Edge cases and error scenarios
"""

import pytest
import json
from datetime import datetime
from io import BytesIO
from unittest.mock import patch, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.import_export import (
    router,
    ExportData,
    ExportedProfile,
    ExportedSubagent,
    ImportResult,
    ImportOptions,
    SYSTEM_FIELDS,
    _export_profile,
    _export_subagent,
)
from app.api.auth import require_admin


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_auth():
    """Mock authentication dependencies."""
    with patch("app.api.import_export.require_admin") as mock_require_admin:
        mock_require_admin.return_value = "test-admin-token"
        yield mock_require_admin


@pytest.fixture
def mock_database():
    """Mock database module."""
    with patch("app.api.import_export.database") as mock_db:
        yield mock_db


@pytest.fixture
def app(mock_auth):
    """Create a FastAPI app with the import_export router."""
    app = FastAPI()
    app.include_router(router)

    # Override auth dependency
    app.dependency_overrides[require_admin] = lambda: "test-admin-token"

    return app


@pytest.fixture
def client(app, mock_database):
    """Create a test client with mocked dependencies."""
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_profile():
    """Sample profile data."""
    return {
        "id": "test-profile",
        "name": "Test Profile",
        "description": "A test profile",
        "config": {
            "model": "claude-sonnet-4-20250514",
            "system_prompt": "You are a helpful assistant.",
            "max_tokens": 8096
        },
        "is_builtin": False,
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00"
    }


@pytest.fixture
def sample_profile_builtin():
    """Sample built-in profile data."""
    return {
        "id": "default",
        "name": "Default Profile",
        "description": "The default profile",
        "config": {
            "model": "claude-sonnet-4-20250514",
            "system_prompt": "Default prompt"
        },
        "is_builtin": True,
        "created_at": "2024-01-01T00:00:00",
        "updated_at": "2024-01-01T00:00:00"
    }


@pytest.fixture
def sample_subagent():
    """Sample subagent data."""
    return {
        "id": "test-subagent",
        "name": "Test Subagent",
        "description": "A test subagent",
        "prompt": "You are a specialized agent.",
        "tools": ["tool1", "tool2"],
        "model": "claude-sonnet-4-20250514",
        "is_builtin": False,
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00"
    }


@pytest.fixture
def sample_export_data():
    """Sample export data structure."""
    return {
        "version": "1.0",
        "export_type": "all",
        "exported_at": "2024-01-15T10:30:00Z",
        "profiles": [
            {
                "id": "imported-profile",
                "name": "Imported Profile",
                "description": "An imported profile",
                "config": {"model": "claude-sonnet-4-20250514"}
            }
        ],
        "subagents": [
            {
                "id": "imported-subagent",
                "name": "Imported Subagent",
                "description": "An imported subagent",
                "prompt": "You are an agent.",
                "tools": ["tool1"],
                "model": None
            }
        ]
    }


# =============================================================================
# Test Module Imports
# =============================================================================

class TestImportExportModuleImports:
    """Verify import_export module can be imported correctly."""

    def test_import_export_module_imports(self):
        """Import export module should import without errors."""
        from app.api import import_export
        assert import_export is not None

    def test_import_export_router_exists(self):
        """Import export router should exist."""
        from app.api.import_export import router
        assert router is not None

    def test_router_has_correct_prefix(self):
        """Router should have /api/v1/export-import prefix."""
        from app.api.import_export import router
        assert router.prefix == "/api/v1/export-import"


# =============================================================================
# Test Helper Functions
# =============================================================================

class TestExportHelpers:
    """Test export helper functions."""

    def test_export_profile_basic(self, sample_profile):
        """Should export profile with config preserved."""
        result = _export_profile(sample_profile)

        assert result.id == "test-profile"
        assert result.name == "Test Profile"
        assert result.description == "A test profile"
        assert result.config["model"] == "claude-sonnet-4-20250514"
        assert result.config["system_prompt"] == "You are a helpful assistant."

    def test_export_profile_empty_config(self):
        """Should handle profile with empty config."""
        profile = {
            "id": "empty-config",
            "name": "Empty Config Profile",
            "description": None,
            "config": {}
        }
        result = _export_profile(profile)

        assert result.id == "empty-config"
        assert result.config == {}

    def test_export_profile_none_description(self):
        """Should handle profile with None description."""
        profile = {
            "id": "no-desc",
            "name": "No Description",
            "config": {"model": "test"}
        }
        result = _export_profile(profile)

        assert result.id == "no-desc"
        assert result.description is None

    def test_export_subagent_excludes_system_fields(self, sample_subagent):
        """Should exclude system fields from subagent export."""
        result = _export_subagent(sample_subagent)

        assert "id" in result
        assert "name" in result
        assert "description" in result
        assert "prompt" in result
        assert "tools" in result
        assert "model" in result
        # System fields should be excluded
        assert "is_builtin" not in result
        assert "created_at" not in result
        assert "updated_at" not in result

    def test_export_subagent_preserves_all_non_system_fields(self):
        """Should preserve all non-system fields."""
        subagent = {
            "id": "test",
            "name": "Test",
            "description": "Desc",
            "prompt": "Prompt",
            "tools": ["a", "b"],
            "model": "model",
            "custom_field": "custom_value",  # Extra field
            "is_builtin": True,
            "created_at": "2024-01-01",
            "updated_at": "2024-01-01"
        }
        result = _export_subagent(subagent)

        assert result["custom_field"] == "custom_value"
        assert "is_builtin" not in result

    def test_system_fields_constant(self):
        """System fields should be correctly defined."""
        assert SYSTEM_FIELDS == {"is_builtin", "created_at", "updated_at"}


# =============================================================================
# Test Export Profiles Endpoint
# =============================================================================

class TestExportProfiles:
    """Test GET /api/v1/export-import/profiles endpoint."""

    def test_export_profiles_success(self, client, mock_database, sample_profile):
        """Should export all profiles successfully."""
        mock_database.get_all_profiles.return_value = [sample_profile]

        response = client.get("/api/v1/export-import/profiles")

        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "1.0"
        assert data["export_type"] == "profiles"
        assert "exported_at" in data
        assert len(data["profiles"]) == 1
        assert data["profiles"][0]["id"] == "test-profile"
        assert data["profiles"][0]["name"] == "Test Profile"

    def test_export_profiles_empty(self, client, mock_database):
        """Should return empty profiles list."""
        mock_database.get_all_profiles.return_value = []

        response = client.get("/api/v1/export-import/profiles")

        assert response.status_code == 200
        data = response.json()
        assert data["profiles"] == []

    def test_export_profiles_multiple(self, client, mock_database, sample_profile, sample_profile_builtin):
        """Should export multiple profiles."""
        mock_database.get_all_profiles.return_value = [sample_profile, sample_profile_builtin]

        response = client.get("/api/v1/export-import/profiles")

        assert response.status_code == 200
        data = response.json()
        assert len(data["profiles"]) == 2

    def test_export_profiles_preserves_config(self, client, mock_database, sample_profile):
        """Should preserve all config fields."""
        mock_database.get_all_profiles.return_value = [sample_profile]

        response = client.get("/api/v1/export-import/profiles")

        data = response.json()
        config = data["profiles"][0]["config"]
        assert config["model"] == "claude-sonnet-4-20250514"
        assert config["system_prompt"] == "You are a helpful assistant."
        assert config["max_tokens"] == 8096


class TestExportSingleProfile:
    """Test GET /api/v1/export-import/profiles/{profile_id} endpoint."""

    def test_export_single_profile_success(self, client, mock_database, sample_profile):
        """Should export a single profile successfully."""
        mock_database.get_profile.return_value = sample_profile

        response = client.get("/api/v1/export-import/profiles/test-profile")

        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "1.0"
        assert data["export_type"] == "profiles"
        assert len(data["profiles"]) == 1
        assert data["profiles"][0]["id"] == "test-profile"

    def test_export_single_profile_not_found(self, client, mock_database):
        """Should return 404 for non-existent profile."""
        mock_database.get_profile.return_value = None

        response = client.get("/api/v1/export-import/profiles/nonexistent")

        assert response.status_code == 404
        assert "Profile not found" in response.json()["detail"]

    def test_export_single_profile_calls_correct_db_method(self, client, mock_database, sample_profile):
        """Should call get_profile with correct ID."""
        mock_database.get_profile.return_value = sample_profile

        client.get("/api/v1/export-import/profiles/my-profile-id")

        mock_database.get_profile.assert_called_once_with("my-profile-id")


# =============================================================================
# Test Export Subagents Endpoint
# =============================================================================

class TestExportSubagents:
    """Test GET /api/v1/export-import/subagents endpoint."""

    def test_export_subagents_success(self, client, mock_database, sample_subagent):
        """Should export all subagents successfully."""
        mock_database.get_all_subagents.return_value = [sample_subagent]

        response = client.get("/api/v1/export-import/subagents")

        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "1.0"
        assert data["export_type"] == "subagents"
        assert "exported_at" in data
        assert len(data["subagents"]) == 1
        assert data["subagents"][0]["id"] == "test-subagent"
        assert data["subagents"][0]["name"] == "Test Subagent"
        # System fields should not be present
        assert "is_builtin" not in data["subagents"][0]
        assert "created_at" not in data["subagents"][0]
        assert "updated_at" not in data["subagents"][0]

    def test_export_subagents_empty(self, client, mock_database):
        """Should return empty subagents list."""
        mock_database.get_all_subagents.return_value = []

        response = client.get("/api/v1/export-import/subagents")

        assert response.status_code == 200
        data = response.json()
        assert data["subagents"] == []

    def test_export_subagents_multiple(self, client, mock_database, sample_subagent):
        """Should export multiple subagents."""
        subagent2 = {
            **sample_subagent,
            "id": "subagent-2",
            "name": "Subagent 2"
        }
        mock_database.get_all_subagents.return_value = [sample_subagent, subagent2]

        response = client.get("/api/v1/export-import/subagents")

        assert response.status_code == 200
        data = response.json()
        assert len(data["subagents"]) == 2

    def test_export_subagents_preserves_tools(self, client, mock_database, sample_subagent):
        """Should preserve tools list."""
        mock_database.get_all_subagents.return_value = [sample_subagent]

        response = client.get("/api/v1/export-import/subagents")

        data = response.json()
        assert data["subagents"][0]["tools"] == ["tool1", "tool2"]


class TestExportSingleSubagent:
    """Test GET /api/v1/export-import/subagents/{subagent_id} endpoint."""

    def test_export_single_subagent_success(self, client, mock_database, sample_subagent):
        """Should export a single subagent successfully."""
        mock_database.get_subagent.return_value = sample_subagent

        response = client.get("/api/v1/export-import/subagents/test-subagent")

        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "1.0"
        assert data["export_type"] == "subagents"
        assert len(data["subagents"]) == 1
        assert data["subagents"][0]["id"] == "test-subagent"

    def test_export_single_subagent_not_found(self, client, mock_database):
        """Should return 404 for non-existent subagent."""
        mock_database.get_subagent.return_value = None

        response = client.get("/api/v1/export-import/subagents/nonexistent")

        assert response.status_code == 404
        assert "Subagent not found" in response.json()["detail"]

    def test_export_single_subagent_excludes_system_fields(self, client, mock_database, sample_subagent):
        """Should exclude system fields from exported subagent."""
        mock_database.get_subagent.return_value = sample_subagent

        response = client.get("/api/v1/export-import/subagents/test-subagent")

        data = response.json()
        subagent = data["subagents"][0]
        assert "is_builtin" not in subagent
        assert "created_at" not in subagent
        assert "updated_at" not in subagent


# =============================================================================
# Test Export All Endpoint
# =============================================================================

class TestExportAll:
    """Test GET /api/v1/export-import/all endpoint."""

    def test_export_all_success(self, client, mock_database, sample_profile, sample_subagent):
        """Should export all profiles and subagents."""
        mock_database.get_all_profiles.return_value = [sample_profile]
        mock_database.get_all_subagents.return_value = [sample_subagent]

        response = client.get("/api/v1/export-import/all")

        assert response.status_code == 200
        data = response.json()
        assert data["version"] == "1.0"
        assert data["export_type"] == "all"
        assert "exported_at" in data
        assert len(data["profiles"]) == 1
        assert len(data["subagents"]) == 1

    def test_export_all_empty(self, client, mock_database):
        """Should handle empty database."""
        mock_database.get_all_profiles.return_value = []
        mock_database.get_all_subagents.return_value = []

        response = client.get("/api/v1/export-import/all")

        assert response.status_code == 200
        data = response.json()
        assert data["profiles"] == []
        assert data["subagents"] == []

    def test_export_all_profiles_only(self, client, mock_database, sample_profile):
        """Should export when only profiles exist."""
        mock_database.get_all_profiles.return_value = [sample_profile]
        mock_database.get_all_subagents.return_value = []

        response = client.get("/api/v1/export-import/all")

        assert response.status_code == 200
        data = response.json()
        assert len(data["profiles"]) == 1
        assert data["subagents"] == []

    def test_export_all_subagents_only(self, client, mock_database, sample_subagent):
        """Should export when only subagents exist."""
        mock_database.get_all_profiles.return_value = []
        mock_database.get_all_subagents.return_value = [sample_subagent]

        response = client.get("/api/v1/export-import/all")

        assert response.status_code == 200
        data = response.json()
        assert data["profiles"] == []
        assert len(data["subagents"]) == 1


# =============================================================================
# Test Import File Upload Endpoint
# =============================================================================

class TestImportFileUpload:
    """Test POST /api/v1/export-import/import endpoint."""

    def test_import_file_success(self, client, mock_database, sample_export_data):
        """Should import from JSON file successfully."""
        mock_database.get_subagent.return_value = None  # No existing
        mock_database.get_profile.return_value = None  # No existing
        mock_database.create_subagent.return_value = {"id": "imported-subagent"}
        mock_database.create_profile.return_value = {"id": "imported-profile"}

        file_content = json.dumps(sample_export_data).encode('utf-8')
        files = {"file": ("export.json", BytesIO(file_content), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["subagents_imported"] == 1
        assert data["profiles_imported"] == 1

    def test_import_file_invalid_json(self, client, mock_database):
        """Should return 400 for invalid JSON."""
        file_content = b"not valid json {"
        files = {"file": ("bad.json", BytesIO(file_content), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 400
        assert "Invalid JSON file" in response.json()["detail"]

    def test_import_file_not_object(self, client, mock_database):
        """Should return 400 for non-object JSON."""
        file_content = b"[1, 2, 3]"  # Array, not object
        files = {"file": ("array.json", BytesIO(file_content), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 400
        assert "expected JSON object" in response.json()["detail"]

    def test_import_file_wrong_version(self, client, mock_database):
        """Should return 400 for unsupported version."""
        data = {"version": "2.0", "export_type": "all"}
        file_content = json.dumps(data).encode('utf-8')
        files = {"file": ("v2.json", BytesIO(file_content), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 400
        assert "Unsupported export version" in response.json()["detail"]

    def test_import_file_skip_existing(self, client, mock_database, sample_export_data):
        """Should skip existing items by default."""
        # Subagent exists
        mock_database.get_subagent.return_value = {"id": "imported-subagent"}
        # Profile exists
        mock_database.get_profile.return_value = {"id": "imported-profile"}

        file_content = json.dumps(sample_export_data).encode('utf-8')
        files = {"file": ("export.json", BytesIO(file_content), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["subagents_skipped"] == 1
        assert data["profiles_skipped"] == 1
        assert len(data["warnings"]) == 2

    def test_import_file_overwrite_existing(self, client, mock_database, sample_export_data):
        """Should overwrite existing items when flag is set."""
        # Items exist
        mock_database.get_subagent.return_value = {"id": "imported-subagent"}
        mock_database.get_profile.return_value = {"id": "imported-profile"}
        mock_database.update_subagent.return_value = {"id": "imported-subagent"}
        mock_database.update_profile.return_value = {"id": "imported-profile"}

        file_content = json.dumps(sample_export_data).encode('utf-8')
        files = {"file": ("export.json", BytesIO(file_content), "application/json")}

        response = client.post(
            "/api/v1/export-import/import?overwrite_existing=true",
            files=files
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["subagents_updated"] == 1
        assert data["profiles_updated"] == 1

    def test_import_file_missing_subagent_id(self, client, mock_database):
        """Should report error for subagent without ID."""
        data = {
            "version": "1.0",
            "export_type": "subagents",
            "subagents": [{"name": "No ID", "description": "test", "prompt": "test"}]
        }
        file_content = json.dumps(data).encode('utf-8')
        files = {"file": ("no_id.json", BytesIO(file_content), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert len(data["errors"]) == 1
        assert "missing required 'id' field" in data["errors"][0]

    def test_import_file_missing_profile_id(self, client, mock_database):
        """Should report error for profile without ID."""
        data = {
            "version": "1.0",
            "export_type": "profiles",
            "profiles": [{"name": "No ID", "config": {}}]
        }
        file_content = json.dumps(data).encode('utf-8')
        files = {"file": ("no_id.json", BytesIO(file_content), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert len(data["errors"]) == 1
        assert "Profile missing required 'id' field" in data["errors"][0]

    def test_import_file_subagent_db_error(self, client, mock_database):
        """Should handle database errors for subagent creation."""
        mock_database.get_subagent.return_value = None
        mock_database.create_subagent.side_effect = Exception("DB error")

        data = {
            "version": "1.0",
            "subagents": [{"id": "test", "name": "Test", "description": "d", "prompt": "p"}]
        }
        file_content = json.dumps(data).encode('utf-8')
        files = {"file": ("error.json", BytesIO(file_content), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert len(data["errors"]) == 1
        assert "Failed to import subagent" in data["errors"][0]

    def test_import_file_profile_db_error(self, client, mock_database):
        """Should handle database errors for profile creation."""
        mock_database.get_profile.return_value = None
        mock_database.create_profile.side_effect = Exception("DB error")

        data = {
            "version": "1.0",
            "profiles": [{"id": "test", "name": "Test", "config": {}}]
        }
        file_content = json.dumps(data).encode('utf-8')
        files = {"file": ("error.json", BytesIO(file_content), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is False
        assert len(data["errors"]) == 1
        assert "Failed to import profile" in data["errors"][0]

    def test_import_file_empty_data(self, client, mock_database):
        """Should handle file with no profiles or subagents."""
        data = {"version": "1.0", "export_type": "all"}
        file_content = json.dumps(data).encode('utf-8')
        files = {"file": ("empty.json", BytesIO(file_content), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["profiles_imported"] == 0
        assert data["subagents_imported"] == 0

    def test_import_file_default_values(self, client, mock_database):
        """Should use default values for missing fields."""
        mock_database.get_subagent.return_value = None
        mock_database.create_subagent.return_value = {"id": "minimal"}

        data = {
            "version": "1.0",
            "subagents": [{"id": "minimal"}]  # Only ID, no other fields
        }
        file_content = json.dumps(data).encode('utf-8')
        files = {"file": ("minimal.json", BytesIO(file_content), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 200
        # Should have called create_subagent with default values
        mock_database.create_subagent.assert_called_once()
        call_kwargs = mock_database.create_subagent.call_args
        assert call_kwargs.kwargs["name"] == "minimal"  # Default to ID
        assert call_kwargs.kwargs["description"] == ""
        assert call_kwargs.kwargs["prompt"] == ""


# =============================================================================
# Test Import JSON Body Endpoint
# =============================================================================

class TestImportJsonBody:
    """Test POST /api/v1/export-import/import/json endpoint."""

    def test_import_json_success(self, client, mock_database):
        """Should import from JSON body successfully."""
        mock_database.get_subagent.return_value = None
        mock_database.get_profile.return_value = None
        mock_database.create_subagent.return_value = {"id": "imported-subagent"}
        mock_database.create_profile.return_value = {"id": "imported-profile"}

        data = {
            "version": "1.0",
            "export_type": "all",
            "exported_at": "2024-01-15T10:30:00Z",
            "profiles": [
                {
                    "id": "imported-profile",
                    "name": "Imported Profile",
                    "description": "An imported profile",
                    "config": {"model": "claude-sonnet-4-20250514"}
                }
            ],
            "subagents": [
                {
                    "id": "imported-subagent",
                    "name": "Imported Subagent",
                    "description": "An imported subagent",
                    "prompt": "You are an agent."
                }
            ]
        }

        response = client.post("/api/v1/export-import/import/json", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
        assert result["subagents_imported"] == 1
        assert result["profiles_imported"] == 1

    def test_import_json_skip_existing(self, client, mock_database):
        """Should skip existing items by default."""
        mock_database.get_subagent.return_value = {"id": "existing"}
        mock_database.get_profile.return_value = {"id": "existing"}

        data = {
            "version": "1.0",
            "export_type": "all",
            "exported_at": "2024-01-15T10:30:00Z",
            "profiles": [{"id": "existing", "name": "Existing", "config": {}}],
            "subagents": [{"id": "existing", "name": "Existing", "description": "d", "prompt": "p"}]
        }

        response = client.post("/api/v1/export-import/import/json", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
        assert result["subagents_skipped"] == 1
        assert result["profiles_skipped"] == 1

    def test_import_json_overwrite_existing(self, client, mock_database):
        """Should overwrite existing items when flag is set."""
        mock_database.get_subagent.return_value = {"id": "existing"}
        mock_database.get_profile.return_value = {"id": "existing"}
        mock_database.update_subagent.return_value = {"id": "existing"}
        mock_database.update_profile.return_value = {"id": "existing"}

        data = {
            "version": "1.0",
            "export_type": "all",
            "exported_at": "2024-01-15T10:30:00Z",
            "profiles": [{"id": "existing", "name": "Updated", "config": {}}],
            "subagents": [{"id": "existing", "name": "Updated", "description": "d", "prompt": "p"}]
        }

        response = client.post(
            "/api/v1/export-import/import/json?overwrite_existing=true",
            json=data
        )

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
        assert result["subagents_updated"] == 1
        assert result["profiles_updated"] == 1

    def test_import_json_subagent_db_error(self, client, mock_database):
        """Should handle database errors for subagent creation."""
        mock_database.get_subagent.return_value = None
        mock_database.create_subagent.side_effect = Exception("DB connection failed")

        data = {
            "version": "1.0",
            "export_type": "subagents",
            "exported_at": "2024-01-15T10:30:00Z",
            "subagents": [{"id": "test", "name": "Test", "description": "d", "prompt": "p"}]
        }

        response = client.post("/api/v1/export-import/import/json", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is False
        assert len(result["errors"]) == 1
        assert "Failed to import subagent" in result["errors"][0]

    def test_import_json_profile_db_error(self, client, mock_database):
        """Should handle database errors for profile creation."""
        mock_database.get_profile.return_value = None
        mock_database.create_profile.side_effect = Exception("DB connection failed")

        data = {
            "version": "1.0",
            "export_type": "profiles",
            "exported_at": "2024-01-15T10:30:00Z",
            "profiles": [{"id": "test", "name": "Test", "config": {}}]
        }

        response = client.post("/api/v1/export-import/import/json", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is False
        assert len(result["errors"]) == 1
        assert "Failed to import profile" in result["errors"][0]

    def test_import_json_only_profiles(self, client, mock_database):
        """Should handle import with only profiles."""
        mock_database.get_profile.return_value = None
        mock_database.create_profile.return_value = {"id": "profile-only"}

        data = {
            "version": "1.0",
            "export_type": "profiles",
            "exported_at": "2024-01-15T10:30:00Z",
            "profiles": [{"id": "profile-only", "name": "Only Profile", "config": {}}]
        }

        response = client.post("/api/v1/export-import/import/json", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
        assert result["profiles_imported"] == 1
        assert result["subagents_imported"] == 0

    def test_import_json_only_subagents(self, client, mock_database):
        """Should handle import with only subagents."""
        mock_database.get_subagent.return_value = None
        mock_database.create_subagent.return_value = {"id": "subagent-only"}

        data = {
            "version": "1.0",
            "export_type": "subagents",
            "exported_at": "2024-01-15T10:30:00Z",
            "subagents": [
                {"id": "subagent-only", "name": "Only Subagent", "description": "d", "prompt": "p"}
            ]
        }

        response = client.post("/api/v1/export-import/import/json", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
        assert result["subagents_imported"] == 1
        assert result["profiles_imported"] == 0

    def test_import_json_empty_data(self, client, mock_database):
        """Should handle empty import data."""
        data = {
            "version": "1.0",
            "export_type": "all",
            "exported_at": "2024-01-15T10:30:00Z"
        }

        response = client.post("/api/v1/export-import/import/json", json=data)

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
        assert result["profiles_imported"] == 0
        assert result["subagents_imported"] == 0

    def test_import_json_with_tools_and_model(self, client, mock_database):
        """Should preserve subagent tools and model."""
        mock_database.get_subagent.return_value = None
        mock_database.create_subagent.return_value = {"id": "with-tools"}

        data = {
            "version": "1.0",
            "export_type": "subagents",
            "exported_at": "2024-01-15T10:30:00Z",
            "subagents": [
                {
                    "id": "with-tools",
                    "name": "With Tools",
                    "description": "Has tools",
                    "prompt": "Use tools",
                    "tools": ["tool1", "tool2"],
                    "model": "claude-sonnet-4-20250514"
                }
            ]
        }

        response = client.post("/api/v1/export-import/import/json", json=data)

        assert response.status_code == 200
        mock_database.create_subagent.assert_called_once()
        call_kwargs = mock_database.create_subagent.call_args.kwargs
        assert call_kwargs["tools"] == ["tool1", "tool2"]
        assert call_kwargs["model"] == "claude-sonnet-4-20250514"


# =============================================================================
# Test Authentication Requirements
# =============================================================================

class TestAuthenticationRequirements:
    """Test that endpoints require proper authentication."""

    def test_export_profiles_requires_admin(self):
        """Export profiles should require admin."""
        from app.api.import_export import export_profiles
        import inspect
        sig = inspect.signature(export_profiles)
        params = sig.parameters
        assert "token" in params

    def test_export_single_profile_requires_admin(self):
        """Export single profile should require admin."""
        from app.api.import_export import export_single_profile
        import inspect
        sig = inspect.signature(export_single_profile)
        params = sig.parameters
        assert "token" in params

    def test_export_subagents_requires_admin(self):
        """Export subagents should require admin."""
        from app.api.import_export import export_subagents
        import inspect
        sig = inspect.signature(export_subagents)
        params = sig.parameters
        assert "token" in params

    def test_export_single_subagent_requires_admin(self):
        """Export single subagent should require admin."""
        from app.api.import_export import export_single_subagent
        import inspect
        sig = inspect.signature(export_single_subagent)
        params = sig.parameters
        assert "token" in params

    def test_export_all_requires_admin(self):
        """Export all should require admin."""
        from app.api.import_export import export_all
        import inspect
        sig = inspect.signature(export_all)
        params = sig.parameters
        assert "token" in params

    def test_import_data_requires_admin(self):
        """Import data should require admin."""
        from app.api.import_export import import_data
        import inspect
        sig = inspect.signature(import_data)
        params = sig.parameters
        assert "token" in params

    def test_import_data_json_requires_admin(self):
        """Import data JSON should require admin."""
        from app.api.import_export import import_data_json
        import inspect
        sig = inspect.signature(import_data_json)
        params = sig.parameters
        assert "token" in params


class TestUnauthenticatedAccess:
    """Test that endpoints properly reject unauthenticated requests."""

    def test_export_profiles_unauthorized(self, mock_database):
        """Should return 401/403 for unauthenticated request."""
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)

        # Override require_admin to raise HTTPException
        def unauthorized():
            raise HTTPException(status_code=401, detail="Not authenticated")

        app.dependency_overrides[require_admin] = unauthorized

        with TestClient(app) as client:
            response = client.get("/api/v1/export-import/profiles")

        assert response.status_code == 401

    def test_import_forbidden(self, mock_database):
        """Should return 403 for non-admin request."""
        from fastapi import FastAPI, HTTPException
        from fastapi.testclient import TestClient

        app = FastAPI()
        app.include_router(router)

        def forbidden():
            raise HTTPException(status_code=403, detail="Admin access required")

        app.dependency_overrides[require_admin] = forbidden

        file_content = b'{"version": "1.0"}'
        files = {"file": ("test.json", BytesIO(file_content), "application/json")}

        with TestClient(app) as client:
            response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 403


# =============================================================================
# Test Pydantic Models
# =============================================================================

class TestPydanticModels:
    """Test Pydantic model validation."""

    def test_exported_subagent_model(self):
        """Should create ExportedSubagent correctly."""
        subagent = ExportedSubagent(
            id="test",
            name="Test",
            description="Desc",
            prompt="Prompt",
            tools=["tool1"],
            model="claude"
        )
        assert subagent.id == "test"
        assert subagent.tools == ["tool1"]

    def test_exported_subagent_allows_extra_fields(self):
        """ExportedSubagent should allow extra fields."""
        subagent = ExportedSubagent(
            id="test",
            name="Test",
            description="Desc",
            prompt="Prompt",
            custom_field="custom"
        )
        assert subagent.id == "test"
        # Extra fields are allowed

    def test_exported_profile_model(self):
        """Should create ExportedProfile correctly."""
        profile = ExportedProfile(
            id="test",
            name="Test",
            description="Desc",
            config={"model": "claude"}
        )
        assert profile.id == "test"
        assert profile.config["model"] == "claude"

    def test_exported_profile_default_config(self):
        """ExportedProfile should have default empty config."""
        profile = ExportedProfile(
            id="test",
            name="Test"
        )
        assert profile.config == {}

    def test_export_data_model(self):
        """Should create ExportData correctly."""
        data = ExportData(
            version="1.0",
            export_type="all",
            exported_at="2024-01-15T10:30:00Z",
            profiles=[ExportedProfile(id="p1", name="P1")],
            subagents=[ExportedSubagent(id="s1", name="S1", description="d", prompt="p")]
        )
        assert data.version == "1.0"
        assert data.export_type == "all"
        assert len(data.profiles) == 1
        assert len(data.subagents) == 1

    def test_import_result_model(self):
        """Should create ImportResult correctly."""
        result = ImportResult(
            success=True,
            profiles_imported=2,
            profiles_skipped=1,
            subagents_imported=3,
            errors=["Error 1"],
            warnings=["Warning 1"]
        )
        assert result.success is True
        assert result.profiles_imported == 2
        assert result.subagents_imported == 3
        assert len(result.errors) == 1
        assert len(result.warnings) == 1

    def test_import_result_defaults(self):
        """ImportResult should have sensible defaults."""
        result = ImportResult(success=True)
        assert result.profiles_imported == 0
        assert result.profiles_skipped == 0
        assert result.profiles_updated == 0
        assert result.subagents_imported == 0
        assert result.subagents_skipped == 0
        assert result.subagents_updated == 0
        assert result.errors == []
        assert result.warnings == []

    def test_import_options_defaults(self):
        """ImportOptions should have sensible defaults."""
        options = ImportOptions()
        assert options.overwrite_existing is False
        assert options.skip_existing is True


# =============================================================================
# Test Edge Cases
# =============================================================================

class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_export_profile_with_large_config(self, client, mock_database):
        """Should handle profile with large config."""
        large_config = {f"key_{i}": f"value_{i}" for i in range(1000)}
        profile = {
            "id": "large-config",
            "name": "Large Config",
            "description": None,
            "config": large_config
        }
        mock_database.get_all_profiles.return_value = [profile]

        response = client.get("/api/v1/export-import/profiles")

        assert response.status_code == 200
        data = response.json()
        assert len(data["profiles"][0]["config"]) == 1000

    def test_export_subagent_with_unicode(self, client, mock_database):
        """Should handle subagent with unicode characters."""
        subagent = {
            "id": "unicode-test",
            "name": "Unicode Test",
            "description": "Test unicode characters",
            "prompt": "You are a multilingual assistant.",
            "tools": None,
            "model": None
        }
        mock_database.get_all_subagents.return_value = [subagent]

        response = client.get("/api/v1/export-import/subagents")

        assert response.status_code == 200
        data = response.json()
        assert data["subagents"][0]["name"] == "Unicode Test"

    def test_import_with_partial_success(self, client, mock_database):
        """Should handle partial import success."""
        mock_database.get_subagent.return_value = None
        mock_database.get_profile.return_value = None

        # First subagent succeeds, second fails
        mock_database.create_subagent.side_effect = [
            {"id": "success"},
            Exception("DB error")
        ]
        mock_database.create_profile.return_value = {"id": "profile"}

        data = {
            "version": "1.0",
            "subagents": [
                {"id": "success", "name": "Success", "description": "d", "prompt": "p"},
                {"id": "failure", "name": "Failure", "description": "d", "prompt": "p"}
            ],
            "profiles": [
                {"id": "profile", "name": "Profile", "config": {}}
            ]
        }
        file_content = json.dumps(data).encode('utf-8')
        files = {"file": ("partial.json", BytesIO(file_content), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is False
        assert result["subagents_imported"] == 1
        assert result["profiles_imported"] == 1
        assert len(result["errors"]) == 1

    def test_export_timestamp_format(self, client, mock_database):
        """Should export timestamp in ISO format with Z suffix."""
        mock_database.get_all_profiles.return_value = []

        response = client.get("/api/v1/export-import/profiles")

        assert response.status_code == 200
        data = response.json()
        assert data["exported_at"].endswith("Z")

    def test_import_file_read_error(self, client, mock_database):
        """Should handle file read errors - empty content triggers JSON decode error."""
        files = {"file": ("empty.json", BytesIO(b""), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        # Empty content triggers JSON decode error
        assert response.status_code == 400
        assert "Invalid JSON file" in response.json()["detail"]

    def test_import_file_generic_exception(self, mock_database):
        """Should handle generic exceptions during file read."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from unittest.mock import AsyncMock

        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_admin] = lambda: "test-admin-token"

        # Create a special mock file that raises a non-JSON exception
        with patch("starlette.datastructures.UploadFile.read", new_callable=AsyncMock) as mock_read:
            mock_read.side_effect = UnicodeDecodeError("utf-8", b"", 0, 1, "invalid byte")

            with TestClient(app) as test_client:
                files = {"file": ("bad.json", BytesIO(b"\xff\xfe"), "application/json")}
                response = test_client.post("/api/v1/export-import/import", files=files)

            # Generic exception handler should catch this
            assert response.status_code == 400
            assert "Failed to read file" in response.json()["detail"]

    def test_import_subagent_with_null_tools(self, client, mock_database):
        """Should handle subagent with null tools."""
        mock_database.get_subagent.return_value = None
        mock_database.create_subagent.return_value = {"id": "null-tools"}

        data = {
            "version": "1.0",
            "subagents": [
                {
                    "id": "null-tools",
                    "name": "Null Tools",
                    "description": "Has null tools",
                    "prompt": "Prompt",
                    "tools": None,
                    "model": None
                }
            ]
        }
        file_content = json.dumps(data).encode('utf-8')
        files = {"file": ("null-tools.json", BytesIO(file_content), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
        assert result["subagents_imported"] == 1

    def test_import_profile_with_empty_config(self, client, mock_database):
        """Should handle profile with empty config."""
        mock_database.get_profile.return_value = None
        mock_database.create_profile.return_value = {"id": "empty-config"}

        data = {
            "version": "1.0",
            "profiles": [
                {
                    "id": "empty-config",
                    "name": "Empty Config",
                    "description": None,
                    "config": {}
                }
            ]
        }
        file_content = json.dumps(data).encode('utf-8')
        files = {"file": ("empty-config.json", BytesIO(file_content), "application/json")}

        response = client.post("/api/v1/export-import/import", files=files)

        assert response.status_code == 200
        result = response.json()
        assert result["success"] is True
        assert result["profiles_imported"] == 1

    def test_import_update_calls_correct_methods(self, client, mock_database):
        """Should call correct update methods when overwriting."""
        # Items exist
        mock_database.get_subagent.return_value = {"id": "existing"}
        mock_database.get_profile.return_value = {"id": "existing"}
        mock_database.update_subagent.return_value = {"id": "existing"}
        mock_database.update_profile.return_value = {"id": "existing"}

        data = {
            "version": "1.0",
            "profiles": [{"id": "existing", "name": "Updated", "config": {"key": "value"}}],
            "subagents": [
                {
                    "id": "existing",
                    "name": "Updated",
                    "description": "Updated desc",
                    "prompt": "Updated prompt",
                    "tools": ["new-tool"],
                    "model": "new-model"
                }
            ]
        }
        file_content = json.dumps(data).encode('utf-8')
        files = {"file": ("update.json", BytesIO(file_content), "application/json")}

        response = client.post(
            "/api/v1/export-import/import?overwrite_existing=true",
            files=files
        )

        assert response.status_code == 200

        # Verify update_subagent was called with correct args
        mock_database.update_subagent.assert_called_once_with(
            subagent_id="existing",
            name="Updated",
            description="Updated desc",
            prompt="Updated prompt",
            tools=["new-tool"],
            model="new-model"
        )

        # Verify update_profile was called with correct args
        mock_database.update_profile.assert_called_once_with(
            profile_id="existing",
            name="Updated",
            description=None,  # Not provided in data
            config={"key": "value"},
            allow_builtin=True
        )
