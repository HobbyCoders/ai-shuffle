"""
Tests for subagents API endpoints.

This module provides comprehensive test coverage for the subagent management API,
including:
- CRUD operations (Create, Read, Update, Delete)
- Authentication and authorization checks
- Validation of request/response models
- Error handling
"""

import pytest
import json
from datetime import datetime
from unittest.mock import patch, MagicMock
from fastapi import HTTPException, FastAPI
from fastapi.testclient import TestClient


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def subagents_test_client():
    """Create a test client with all subagents dependencies mocked."""
    from app.api.subagents import router
    from app.api.auth import require_auth, require_admin

    # Create a minimal test app with just the subagents router
    test_app = FastAPI()
    test_app.include_router(router)

    # Override auth dependencies
    def mock_require_auth():
        return "test-token"

    def mock_require_admin():
        return None

    test_app.dependency_overrides[require_auth] = mock_require_auth
    test_app.dependency_overrides[require_admin] = mock_require_admin

    with TestClient(test_app) as client:
        yield client


@pytest.fixture
def subagents_unauthenticated_client():
    """Create a test client that fails auth checks."""
    from app.api.subagents import router
    from app.api.auth import require_auth, require_admin

    test_app = FastAPI()
    test_app.include_router(router)

    def mock_require_auth_fail():
        raise HTTPException(status_code=401, detail="Unauthorized")

    def mock_require_admin_fail():
        raise HTTPException(status_code=403, detail="Admin required")

    test_app.dependency_overrides[require_auth] = mock_require_auth_fail
    test_app.dependency_overrides[require_admin] = mock_require_admin_fail

    with TestClient(test_app) as client:
        yield client


# =============================================================================
# Module Import Tests
# =============================================================================

class TestSubagentsModuleImports:
    """Verify subagents module can be imported correctly."""

    def test_subagents_module_imports(self):
        """Subagents module should import without errors."""
        from app.api import subagents
        assert subagents is not None

    def test_subagents_router_exists(self):
        """Subagents router should exist."""
        from app.api.subagents import router
        assert router is not None

    def test_subagents_router_prefix(self):
        """Subagents router should have correct prefix."""
        from app.api.subagents import router
        assert router.prefix == "/api/v1/subagents"

    def test_subagents_router_tags(self):
        """Subagents router should have correct tags."""
        from app.api.subagents import router
        assert "Subagents" in router.tags


# =============================================================================
# Pydantic Model Tests
# =============================================================================

class TestSubagentModels:
    """Test Pydantic models for subagent API."""

    def test_subagent_response_model(self):
        """SubagentResponse should have all required fields."""
        from app.api.subagents import SubagentResponse

        now = datetime.utcnow()
        response = SubagentResponse(
            id="test-subagent",
            name="Test Subagent",
            description="A test subagent",
            prompt="You are a test assistant",
            tools=["tool1", "tool2"],
            model="claude-sonnet-4-20250514",
            is_builtin=False,
            created_at=now,
            updated_at=now
        )

        assert response.id == "test-subagent"
        assert response.name == "Test Subagent"
        assert response.description == "A test subagent"
        assert response.prompt == "You are a test assistant"
        assert response.tools == ["tool1", "tool2"]
        assert response.model == "claude-sonnet-4-20250514"
        assert response.is_builtin is False

    def test_subagent_response_optional_fields(self):
        """SubagentResponse should handle optional fields."""
        from app.api.subagents import SubagentResponse

        now = datetime.utcnow()
        response = SubagentResponse(
            id="test-subagent",
            name="Test Subagent",
            description="A test subagent",
            prompt="You are a test assistant",
            tools=None,
            model=None,
            is_builtin=False,
            created_at=now,
            updated_at=now
        )

        assert response.tools is None
        assert response.model is None

    def test_subagent_create_request_valid(self):
        """SubagentCreateRequest should accept valid data."""
        from app.api.subagents import SubagentCreateRequest

        request = SubagentCreateRequest(
            id="my-subagent",
            name="My Subagent",
            description="Description here",
            prompt="Prompt here",
            tools=["bash", "read"],
            model="claude-sonnet-4-20250514"
        )

        assert request.id == "my-subagent"
        assert request.name == "My Subagent"

    def test_subagent_create_request_id_validation(self):
        """SubagentCreateRequest should validate ID format."""
        from app.api.subagents import SubagentCreateRequest
        from pydantic import ValidationError

        # Valid IDs
        valid_ids = ["my-subagent", "subagent1", "test-123", "a"]
        for valid_id in valid_ids:
            request = SubagentCreateRequest(
                id=valid_id,
                name="Test",
                description="Test",
                prompt="Test"
            )
            assert request.id == valid_id

        # Invalid IDs (uppercase, special chars, spaces)
        invalid_ids = ["My-Subagent", "test_subagent", "test subagent", "test@agent"]
        for invalid_id in invalid_ids:
            with pytest.raises(ValidationError):
                SubagentCreateRequest(
                    id=invalid_id,
                    name="Test",
                    description="Test",
                    prompt="Test"
                )

    def test_subagent_create_request_id_length(self):
        """SubagentCreateRequest should validate ID length."""
        from app.api.subagents import SubagentCreateRequest
        from pydantic import ValidationError

        # Empty ID should fail
        with pytest.raises(ValidationError):
            SubagentCreateRequest(
                id="",
                name="Test",
                description="Test",
                prompt="Test"
            )

        # ID too long (>50 chars) should fail
        with pytest.raises(ValidationError):
            SubagentCreateRequest(
                id="a" * 51,
                name="Test",
                description="Test",
                prompt="Test"
            )

    def test_subagent_create_request_required_fields(self):
        """SubagentCreateRequest should require all mandatory fields."""
        from app.api.subagents import SubagentCreateRequest
        from pydantic import ValidationError

        # Missing name
        with pytest.raises(ValidationError):
            SubagentCreateRequest(
                id="test",
                description="Test",
                prompt="Test"
            )

        # Missing description
        with pytest.raises(ValidationError):
            SubagentCreateRequest(
                id="test",
                name="Test",
                prompt="Test"
            )

        # Missing prompt
        with pytest.raises(ValidationError):
            SubagentCreateRequest(
                id="test",
                name="Test",
                description="Test"
            )

    def test_subagent_update_request_all_optional(self):
        """SubagentUpdateRequest should allow all fields to be optional."""
        from app.api.subagents import SubagentUpdateRequest

        # All fields can be None
        request = SubagentUpdateRequest()
        assert request.name is None
        assert request.description is None
        assert request.prompt is None
        assert request.tools is None
        assert request.model is None

    def test_subagent_update_request_partial_update(self):
        """SubagentUpdateRequest should allow partial updates."""
        from app.api.subagents import SubagentUpdateRequest

        request = SubagentUpdateRequest(name="New Name")
        assert request.name == "New Name"
        assert request.description is None

        request2 = SubagentUpdateRequest(tools=["new-tool"])
        assert request2.tools == ["new-tool"]


# =============================================================================
# List Subagents Endpoint Tests
# =============================================================================

class TestListSubagentsEndpoint:
    """Test GET /api/v1/subagents endpoint."""

    def test_list_subagents_empty(self, subagents_test_client):
        """GET /subagents should return empty list when no subagents exist."""
        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_all_subagents.return_value = []
            response = subagents_test_client.get("/api/v1/subagents")
            assert response.status_code == 200
            assert response.json() == []

    def test_list_subagents_with_data(self, subagents_test_client):
        """GET /subagents should return all subagents."""
        mock_subagents = [
            {
                "id": "subagent-1",
                "name": "Subagent One",
                "description": "First subagent",
                "prompt": "You are subagent one",
                "tools": ["bash"],
                "model": "claude-sonnet-4-20250514",
                "is_builtin": False,
                "created_at": datetime(2024, 1, 1, 0, 0, 0),
                "updated_at": datetime(2024, 1, 1, 0, 0, 0)
            },
            {
                "id": "subagent-2",
                "name": "Subagent Two",
                "description": "Second subagent",
                "prompt": "You are subagent two",
                "tools": None,
                "model": None,
                "is_builtin": True,
                "created_at": datetime(2024, 1, 2, 0, 0, 0),
                "updated_at": datetime(2024, 1, 2, 0, 0, 0)
            }
        ]

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_all_subagents.return_value = mock_subagents
            response = subagents_test_client.get("/api/v1/subagents")
            assert response.status_code == 200

            data = response.json()
            assert len(data) == 2
            assert data[0]["id"] == "subagent-1"
            assert data[0]["name"] == "Subagent One"
            assert data[0]["tools"] == ["bash"]
            assert data[1]["id"] == "subagent-2"
            assert data[1]["is_builtin"] is True

    def test_list_subagents_requires_auth(self, subagents_unauthenticated_client):
        """GET /subagents should require authentication."""
        response = subagents_unauthenticated_client.get("/api/v1/subagents")
        assert response.status_code == 401


# =============================================================================
# Get Subagent Endpoint Tests
# =============================================================================

class TestGetSubagentEndpoint:
    """Test GET /api/v1/subagents/{subagent_id} endpoint."""

    def test_get_subagent_success(self, subagents_test_client):
        """GET /subagents/{id} should return subagent when found."""
        mock_subagent = {
            "id": "test-subagent",
            "name": "Test Subagent",
            "description": "A test subagent",
            "prompt": "You are a test assistant",
            "tools": ["bash", "read"],
            "model": "claude-sonnet-4-20250514",
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = mock_subagent
            response = subagents_test_client.get("/api/v1/subagents/test-subagent")
            assert response.status_code == 200

            data = response.json()
            assert data["id"] == "test-subagent"
            assert data["name"] == "Test Subagent"
            assert data["description"] == "A test subagent"
            assert data["prompt"] == "You are a test assistant"
            assert data["tools"] == ["bash", "read"]
            assert data["model"] == "claude-sonnet-4-20250514"
            assert data["is_builtin"] is False

    def test_get_subagent_not_found(self, subagents_test_client):
        """GET /subagents/{id} should return 404 when not found."""
        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = None
            response = subagents_test_client.get("/api/v1/subagents/nonexistent")
            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()

    def test_get_subagent_builtin(self, subagents_test_client):
        """GET /subagents/{id} should correctly return is_builtin flag."""
        mock_subagent = {
            "id": "builtin-subagent",
            "name": "Builtin Subagent",
            "description": "A builtin subagent",
            "prompt": "You are a builtin assistant",
            "tools": None,
            "model": None,
            "is_builtin": True,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = mock_subagent
            response = subagents_test_client.get("/api/v1/subagents/builtin-subagent")
            assert response.status_code == 200
            assert response.json()["is_builtin"] is True

    def test_get_subagent_requires_auth(self, subagents_unauthenticated_client):
        """GET /subagents/{id} should require authentication."""
        response = subagents_unauthenticated_client.get("/api/v1/subagents/test-subagent")
        assert response.status_code == 401


# =============================================================================
# Create Subagent Endpoint Tests
# =============================================================================

class TestCreateSubagentEndpoint:
    """Test POST /api/v1/subagents endpoint."""

    def test_create_subagent_success(self, subagents_test_client):
        """POST /subagents should create a new subagent."""
        created_subagent = {
            "id": "new-subagent",
            "name": "New Subagent",
            "description": "A new subagent",
            "prompt": "You are a new assistant",
            "tools": ["bash"],
            "model": "claude-sonnet-4-20250514",
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = None
            mock_db.create_subagent.return_value = created_subagent
            response = subagents_test_client.post(
                "/api/v1/subagents",
                json={
                    "id": "new-subagent",
                    "name": "New Subagent",
                    "description": "A new subagent",
                    "prompt": "You are a new assistant",
                    "tools": ["bash"],
                    "model": "claude-sonnet-4-20250514"
                }
            )

            assert response.status_code == 201
            data = response.json()
            assert data["id"] == "new-subagent"
            assert data["name"] == "New Subagent"
            assert data["is_builtin"] is False

    def test_create_subagent_minimal(self, subagents_test_client):
        """POST /subagents should create subagent with only required fields."""
        created_subagent = {
            "id": "minimal-subagent",
            "name": "Minimal",
            "description": "Minimal description",
            "prompt": "Minimal prompt",
            "tools": None,
            "model": None,
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = None
            mock_db.create_subagent.return_value = created_subagent
            response = subagents_test_client.post(
                "/api/v1/subagents",
                json={
                    "id": "minimal-subagent",
                    "name": "Minimal",
                    "description": "Minimal description",
                    "prompt": "Minimal prompt"
                }
            )

            assert response.status_code == 201
            data = response.json()
            assert data["tools"] is None
            assert data["model"] is None

    def test_create_subagent_conflict(self, subagents_test_client):
        """POST /subagents should return 409 when ID already exists."""
        existing_subagent = {
            "id": "existing-subagent",
            "name": "Existing",
            "description": "Already exists",
            "prompt": "Existing prompt",
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = existing_subagent
            response = subagents_test_client.post(
                "/api/v1/subagents",
                json={
                    "id": "existing-subagent",
                    "name": "New Name",
                    "description": "New description",
                    "prompt": "New prompt"
                }
            )

            assert response.status_code == 409
            assert "already exists" in response.json()["detail"].lower()

    def test_create_subagent_invalid_id_format(self, subagents_test_client):
        """POST /subagents should reject invalid ID format."""
        response = subagents_test_client.post(
            "/api/v1/subagents",
            json={
                "id": "Invalid-ID",  # Uppercase not allowed
                "name": "Test",
                "description": "Test",
                "prompt": "Test"
            }
        )

        assert response.status_code == 422  # Validation error

    def test_create_subagent_missing_required_field(self, subagents_test_client):
        """POST /subagents should reject requests missing required fields."""
        # Missing 'name'
        response = subagents_test_client.post(
            "/api/v1/subagents",
            json={
                "id": "test-subagent",
                "description": "Test",
                "prompt": "Test"
            }
        )

        assert response.status_code == 422

    def test_create_subagent_requires_admin(self, subagents_unauthenticated_client):
        """POST /subagents should require admin privileges."""
        response = subagents_unauthenticated_client.post(
            "/api/v1/subagents",
            json={
                "id": "test-subagent",
                "name": "Test",
                "description": "Test",
                "prompt": "Test"
            }
        )
        assert response.status_code == 403

    def test_create_subagent_with_tools_list(self, subagents_test_client):
        """POST /subagents should accept tools as a list."""
        created_subagent = {
            "id": "tools-subagent",
            "name": "Tools Subagent",
            "description": "Has tools",
            "prompt": "Use tools",
            "tools": ["bash", "read", "write"],
            "model": None,
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = None
            mock_db.create_subagent.return_value = created_subagent
            response = subagents_test_client.post(
                "/api/v1/subagents",
                json={
                    "id": "tools-subagent",
                    "name": "Tools Subagent",
                    "description": "Has tools",
                    "prompt": "Use tools",
                    "tools": ["bash", "read", "write"]
                }
            )

            assert response.status_code == 201
            assert response.json()["tools"] == ["bash", "read", "write"]


# =============================================================================
# Update Subagent Endpoint Tests
# =============================================================================

class TestUpdateSubagentEndpoint:
    """Test PUT /api/v1/subagents/{subagent_id} endpoint."""

    def test_update_subagent_success(self, subagents_test_client):
        """PUT /subagents/{id} should update an existing subagent."""
        existing_subagent = {
            "id": "test-subagent",
            "name": "Original Name",
            "description": "Original description",
            "prompt": "Original prompt",
            "tools": None,
            "model": None,
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        updated_subagent = {
            "id": "test-subagent",
            "name": "Updated Name",
            "description": "Updated description",
            "prompt": "Updated prompt",
            "tools": ["bash"],
            "model": "claude-sonnet-4-20250514",
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 2, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = existing_subagent
            mock_db.update_subagent.return_value = updated_subagent
            response = subagents_test_client.put(
                "/api/v1/subagents/test-subagent",
                json={
                    "name": "Updated Name",
                    "description": "Updated description",
                    "prompt": "Updated prompt",
                    "tools": ["bash"],
                    "model": "claude-sonnet-4-20250514"
                }
            )

            assert response.status_code == 200
            data = response.json()
            assert data["name"] == "Updated Name"
            assert data["description"] == "Updated description"
            assert data["tools"] == ["bash"]

    def test_update_subagent_partial(self, subagents_test_client):
        """PUT /subagents/{id} should allow partial updates."""
        existing_subagent = {
            "id": "test-subagent",
            "name": "Original Name",
            "description": "Original description",
            "prompt": "Original prompt",
            "tools": None,
            "model": None,
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        updated_subagent = {
            "id": "test-subagent",
            "name": "Updated Name Only",
            "description": "Original description",
            "prompt": "Original prompt",
            "tools": None,
            "model": None,
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 2, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = existing_subagent
            mock_db.update_subagent.return_value = updated_subagent
            response = subagents_test_client.put(
                "/api/v1/subagents/test-subagent",
                json={"name": "Updated Name Only"}
            )

            assert response.status_code == 200
            assert response.json()["name"] == "Updated Name Only"

    def test_update_subagent_not_found(self, subagents_test_client):
        """PUT /subagents/{id} should return 404 when subagent not found."""
        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = None
            response = subagents_test_client.put(
                "/api/v1/subagents/nonexistent",
                json={"name": "New Name"}
            )

            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()

    def test_update_subagent_requires_admin(self, subagents_unauthenticated_client):
        """PUT /subagents/{id} should require admin privileges."""
        response = subagents_unauthenticated_client.put(
            "/api/v1/subagents/test-subagent",
            json={"name": "New Name"}
        )
        assert response.status_code == 403

    def test_update_builtin_subagent(self, subagents_test_client):
        """PUT /subagents/{id} should allow updating builtin subagents."""
        existing_subagent = {
            "id": "builtin-subagent",
            "name": "Builtin",
            "description": "Builtin description",
            "prompt": "Builtin prompt",
            "tools": None,
            "model": None,
            "is_builtin": True,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        updated_subagent = {
            "id": "builtin-subagent",
            "name": "Updated Builtin",
            "description": "Builtin description",
            "prompt": "Builtin prompt",
            "tools": None,
            "model": None,
            "is_builtin": True,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 2, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = existing_subagent
            mock_db.update_subagent.return_value = updated_subagent
            response = subagents_test_client.put(
                "/api/v1/subagents/builtin-subagent",
                json={"name": "Updated Builtin"}
            )

            # Per the API, all subagents are editable
            assert response.status_code == 200
            assert response.json()["name"] == "Updated Builtin"

    def test_update_subagent_tools(self, subagents_test_client):
        """PUT /subagents/{id} should update tools list."""
        existing_subagent = {
            "id": "test-subagent",
            "name": "Test",
            "description": "Test",
            "prompt": "Test",
            "tools": ["bash"],
            "model": None,
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        updated_subagent = {
            "id": "test-subagent",
            "name": "Test",
            "description": "Test",
            "prompt": "Test",
            "tools": ["bash", "read", "write"],
            "model": None,
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 2, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = existing_subagent
            mock_db.update_subagent.return_value = updated_subagent
            response = subagents_test_client.put(
                "/api/v1/subagents/test-subagent",
                json={"tools": ["bash", "read", "write"]}
            )

            assert response.status_code == 200
            assert response.json()["tools"] == ["bash", "read", "write"]


# =============================================================================
# Delete Subagent Endpoint Tests
# =============================================================================

class TestDeleteSubagentEndpoint:
    """Test DELETE /api/v1/subagents/{subagent_id} endpoint."""

    def test_delete_subagent_success(self, subagents_test_client):
        """DELETE /subagents/{id} should delete an existing subagent."""
        existing_subagent = {
            "id": "test-subagent",
            "name": "Test",
            "description": "Test",
            "prompt": "Test",
            "tools": None,
            "model": None,
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = existing_subagent
            mock_db.delete_subagent.return_value = True
            response = subagents_test_client.delete("/api/v1/subagents/test-subagent")

            assert response.status_code == 204

    def test_delete_subagent_not_found(self, subagents_test_client):
        """DELETE /subagents/{id} should return 404 when not found."""
        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = None
            response = subagents_test_client.delete("/api/v1/subagents/nonexistent")

            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()

    def test_delete_subagent_requires_admin(self, subagents_unauthenticated_client):
        """DELETE /subagents/{id} should require admin privileges."""
        response = subagents_unauthenticated_client.delete("/api/v1/subagents/test-subagent")
        assert response.status_code == 403

    def test_delete_builtin_subagent(self, subagents_test_client):
        """DELETE /subagents/{id} should allow deleting builtin subagents."""
        existing_subagent = {
            "id": "builtin-subagent",
            "name": "Builtin",
            "description": "Builtin",
            "prompt": "Builtin",
            "tools": None,
            "model": None,
            "is_builtin": True,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = existing_subagent
            mock_db.delete_subagent.return_value = True
            response = subagents_test_client.delete("/api/v1/subagents/builtin-subagent")

            # Per the API, all subagents can be deleted
            assert response.status_code == 204


# =============================================================================
# Response Serialization Tests
# =============================================================================

class TestSubagentResponseSerialization:
    """Test response serialization for various edge cases."""

    def test_subagent_with_empty_tools_list(self, subagents_test_client):
        """Response should handle empty tools list."""
        mock_subagent = {
            "id": "test-subagent",
            "name": "Test",
            "description": "Test",
            "prompt": "Test",
            "tools": [],
            "model": None,
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = mock_subagent
            response = subagents_test_client.get("/api/v1/subagents/test-subagent")
            assert response.status_code == 200
            assert response.json()["tools"] == []

    def test_subagent_datetime_serialization(self, subagents_test_client):
        """Response should properly serialize datetime fields."""
        mock_subagent = {
            "id": "test-subagent",
            "name": "Test",
            "description": "Test",
            "prompt": "Test",
            "tools": None,
            "model": None,
            "is_builtin": False,
            "created_at": datetime(2024, 6, 15, 12, 30, 45),
            "updated_at": datetime(2024, 6, 16, 14, 20, 10)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = mock_subagent
            response = subagents_test_client.get("/api/v1/subagents/test-subagent")
            assert response.status_code == 200

            data = response.json()
            assert "2024-06-15" in data["created_at"]
            assert "2024-06-16" in data["updated_at"]

    def test_list_subagents_order(self, subagents_test_client):
        """GET /subagents should return subagents in order from database."""
        mock_subagents = [
            {
                "id": "alpha-subagent",
                "name": "Alpha",
                "description": "First",
                "prompt": "First",
                "tools": None,
                "model": None,
                "is_builtin": False,
                "created_at": datetime(2024, 1, 1, 0, 0, 0),
                "updated_at": datetime(2024, 1, 1, 0, 0, 0)
            },
            {
                "id": "beta-subagent",
                "name": "Beta",
                "description": "Second",
                "prompt": "Second",
                "tools": None,
                "model": None,
                "is_builtin": False,
                "created_at": datetime(2024, 1, 2, 0, 0, 0),
                "updated_at": datetime(2024, 1, 2, 0, 0, 0)
            }
        ]

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_all_subagents.return_value = mock_subagents
            response = subagents_test_client.get("/api/v1/subagents")
            assert response.status_code == 200

            data = response.json()
            assert len(data) == 2
            assert data[0]["id"] == "alpha-subagent"
            assert data[1]["id"] == "beta-subagent"


# =============================================================================
# Default Value Tests
# =============================================================================

class TestSubagentDefaultValues:
    """Test default values in subagent responses."""

    def test_is_builtin_defaults_to_false(self, subagents_test_client):
        """Subagent response should default is_builtin to False if not set."""
        mock_subagent = {
            "id": "test-subagent",
            "name": "Test",
            "description": "Test",
            "prompt": "Test",
            "tools": None,
            "model": None,
            # is_builtin intentionally omitted to test .get() default
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = mock_subagent
            response = subagents_test_client.get("/api/v1/subagents/test-subagent")
            assert response.status_code == 200
            assert response.json()["is_builtin"] is False

    def test_list_subagents_is_builtin_default(self, subagents_test_client):
        """List subagents should default is_builtin to False."""
        mock_subagents = [
            {
                "id": "test-subagent",
                "name": "Test",
                "description": "Test",
                "prompt": "Test",
                "tools": None,
                "model": None,
                # is_builtin intentionally omitted
                "created_at": datetime(2024, 1, 1, 0, 0, 0),
                "updated_at": datetime(2024, 1, 1, 0, 0, 0)
            }
        ]

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_all_subagents.return_value = mock_subagents
            response = subagents_test_client.get("/api/v1/subagents")
            assert response.status_code == 200
            assert response.json()[0]["is_builtin"] is False


# =============================================================================
# Database Interaction Tests
# =============================================================================

class TestDatabaseInteraction:
    """Test that endpoints properly call database functions."""

    def test_create_passes_correct_params(self, subagents_test_client):
        """Create should pass correct parameters to database."""
        created_subagent = {
            "id": "test-subagent",
            "name": "Test Name",
            "description": "Test Description",
            "prompt": "Test Prompt",
            "tools": ["bash", "read"],
            "model": "claude-sonnet-4-20250514",
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = None
            mock_db.create_subagent.return_value = created_subagent
            subagents_test_client.post(
                "/api/v1/subagents",
                json={
                    "id": "test-subagent",
                    "name": "Test Name",
                    "description": "Test Description",
                    "prompt": "Test Prompt",
                    "tools": ["bash", "read"],
                    "model": "claude-sonnet-4-20250514"
                }
            )

            mock_db.create_subagent.assert_called_once_with(
                subagent_id="test-subagent",
                name="Test Name",
                description="Test Description",
                prompt="Test Prompt",
                tools=["bash", "read"],
                model="claude-sonnet-4-20250514",
                is_builtin=False
            )

    def test_update_passes_correct_params(self, subagents_test_client):
        """Update should pass correct parameters to database."""
        existing_subagent = {
            "id": "test-subagent",
            "name": "Original",
            "description": "Original",
            "prompt": "Original",
            "tools": None,
            "model": None,
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        updated_subagent = {**existing_subagent, "name": "Updated"}

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = existing_subagent
            mock_db.update_subagent.return_value = updated_subagent
            subagents_test_client.put(
                "/api/v1/subagents/test-subagent",
                json={
                    "name": "Updated",
                    "description": "New Desc"
                }
            )

            mock_db.update_subagent.assert_called_once_with(
                subagent_id="test-subagent",
                name="Updated",
                description="New Desc",
                prompt=None,
                tools=None,
                model=None
            )

    def test_delete_calls_database(self, subagents_test_client):
        """Delete should call database delete function."""
        existing_subagent = {
            "id": "test-subagent",
            "name": "Test",
            "description": "Test",
            "prompt": "Test",
            "tools": None,
            "model": None,
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = existing_subagent
            mock_db.delete_subagent.return_value = True
            subagents_test_client.delete("/api/v1/subagents/test-subagent")

            mock_db.delete_subagent.assert_called_once_with("test-subagent")

    def test_get_subagent_calls_database(self, subagents_test_client):
        """Get should call database get_subagent function."""
        mock_subagent = {
            "id": "test-subagent",
            "name": "Test",
            "description": "Test",
            "prompt": "Test",
            "tools": None,
            "model": None,
            "is_builtin": False,
            "created_at": datetime(2024, 1, 1, 0, 0, 0),
            "updated_at": datetime(2024, 1, 1, 0, 0, 0)
        }

        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_subagent.return_value = mock_subagent
            subagents_test_client.get("/api/v1/subagents/test-subagent")

            mock_db.get_subagent.assert_called_once_with("test-subagent")

    def test_list_subagents_calls_database(self, subagents_test_client):
        """List should call database get_all_subagents function."""
        with patch("app.api.subagents.database") as mock_db:
            mock_db.get_all_subagents.return_value = []
            subagents_test_client.get("/api/v1/subagents")

            mock_db.get_all_subagents.assert_called_once()
