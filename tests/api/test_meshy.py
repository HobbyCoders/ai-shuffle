"""
Comprehensive tests for the Meshy 3D Model Generation API endpoints.

Tests cover:
- Webhook endpoint for receiving Meshy API callbacks
- Task management (list, get, delete, register)
- Meshy API key configuration
- 3D model settings management
- Internal API key access endpoint
- Signature verification
- Error handling
- Status mapping
"""

import pytest
import hmac
import hashlib
import json
from datetime import datetime
from unittest.mock import patch, MagicMock, AsyncMock


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_meshy_auth(client):
    """Override auth dependencies for meshy endpoints."""
    from app.main import app
    from app.api.auth import require_auth, require_admin

    # Override the dependencies
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    app.dependency_overrides[require_admin] = lambda: None

    yield

    # Clean up
    app.dependency_overrides.pop(require_auth, None)
    app.dependency_overrides.pop(require_admin, None)


@pytest.fixture
def mock_meshy_encryption():
    """Mock encryption functions for meshy module."""
    with patch("app.api.meshy.get_decrypted_api_key") as mock_get_key:
        with patch("app.api.meshy.set_encrypted_api_key") as mock_set_key:
            with patch("app.api.meshy.mask_api_key") as mock_mask:
                mock_get_key.return_value = None
                mock_set_key.return_value = None
                mock_mask.side_effect = lambda x: f"{x[:7]}...{x[-4:]}" if x and len(x) >= 10 else ""
                yield {
                    "get_key": mock_get_key,
                    "set_key": mock_set_key,
                    "mask_key": mock_mask
                }


@pytest.fixture
def mock_meshy_database():
    """Mock database module for meshy."""
    with patch("app.api.meshy.database") as mock_db:
        mock_db.get_system_setting.return_value = None
        mock_db.set_system_setting.return_value = None
        mock_db.delete_system_setting.return_value = None
        yield mock_db


@pytest.fixture
def mock_meshy_tasks():
    """Reset meshy tasks database before each test."""
    from app.api import meshy
    original_tasks = meshy.meshy_tasks_db.copy()
    meshy.meshy_tasks_db.clear()
    yield meshy.meshy_tasks_db
    meshy.meshy_tasks_db.clear()
    meshy.meshy_tasks_db.update(original_tasks)


@pytest.fixture
def sample_webhook_payload():
    """Return sample Meshy webhook payload."""
    return {
        "id": "task-12345",
        "status": "SUCCEEDED",
        "model_urls": {
            "glb": "https://meshy.ai/models/task-12345.glb",
            "fbx": "https://meshy.ai/models/task-12345.fbx"
        },
        "thumbnail_url": "https://meshy.ai/thumbnails/task-12345.png",
        "video_url": "https://meshy.ai/videos/task-12345.mp4",
        "texture_urls": [
            {"base_color": "https://meshy.ai/textures/task-12345_base.png"}
        ],
        "prompt": "A cute robot",
        "art_style": "realistic",
        "mode": "preview",
        "created_at": 1704067200000,
        "started_at": 1704067260000,
        "finished_at": 1704067320000
    }


@pytest.fixture
def sample_failed_webhook_payload():
    """Return sample failed Meshy webhook payload."""
    return {
        "id": "task-failed-123",
        "status": "FAILED",
        "task_error": {
            "message": "Generation failed due to content moderation",
            "code": "CONTENT_MODERATION"
        },
        "prompt": "Invalid prompt",
        "created_at": 1704067200000
    }


# =============================================================================
# Pydantic Model Tests (no database needed)
# =============================================================================

class TestMeshyWebhookPayloadModel:
    """Test the MeshyWebhookPayload Pydantic model."""

    def test_minimal_payload(self):
        """Should accept minimal required fields."""
        from app.api.meshy import MeshyWebhookPayload
        payload = MeshyWebhookPayload(id="task-1", status="PENDING")
        assert payload.id == "task-1"
        assert payload.status == "PENDING"

    def test_full_payload(self, sample_webhook_payload):
        """Should accept full payload with all fields."""
        from app.api.meshy import MeshyWebhookPayload
        payload = MeshyWebhookPayload(**sample_webhook_payload)
        assert payload.id == "task-12345"
        assert payload.status == "SUCCEEDED"
        assert payload.model_urls["glb"] == "https://meshy.ai/models/task-12345.glb"
        assert payload.thumbnail_url == "https://meshy.ai/thumbnails/task-12345.png"
        assert payload.prompt == "A cute robot"


class TestTaskResponseModel:
    """Test the TaskResponse Pydantic model."""

    def test_task_response_creation(self):
        """Should create valid task response."""
        from app.api.meshy import TaskResponse
        from datetime import datetime

        task = TaskResponse(
            id="task-1",
            type="text-to-3d",
            status="succeeded",
            created_at=datetime.utcnow()
        )
        assert task.id == "task-1"
        assert task.type == "text-to-3d"
        assert task.progress == 0.0  # Default


class TestMeshyKeyRequestModel:
    """Test the MeshyKeyRequest Pydantic model."""

    def test_meshy_key_request_valid(self):
        """Should accept valid API key."""
        from app.api.meshy import MeshyKeyRequest
        request = MeshyKeyRequest(api_key="msy_test_key")
        assert request.api_key == "msy_test_key"


class TestModel3DSettingsRequestModel:
    """Test the Model3DSettingsRequest Pydantic model."""

    def test_settings_request_model_only(self):
        """Should accept model only."""
        from app.api.meshy import Model3DSettingsRequest
        request = Model3DSettingsRequest(model="meshy-5")
        assert request.model == "meshy-5"
        assert request.provider is None

    def test_settings_request_provider_only(self):
        """Should accept provider only."""
        from app.api.meshy import Model3DSettingsRequest
        request = Model3DSettingsRequest(provider="meshy")
        assert request.provider == "meshy"
        assert request.model is None

    def test_settings_request_both(self):
        """Should accept both model and provider."""
        from app.api.meshy import Model3DSettingsRequest
        request = Model3DSettingsRequest(model="latest", provider="meshy")
        assert request.model == "latest"
        assert request.provider == "meshy"


# =============================================================================
# Module Import Tests (no database needed)
# =============================================================================

class TestModuleImports:
    """Verify meshy module can be imported correctly."""

    def test_meshy_module_imports(self):
        """Meshy module should import without errors."""
        from app.api import meshy
        assert meshy is not None

    def test_meshy_router_exists(self):
        """Meshy router should exist."""
        from app.api.meshy import router
        assert router is not None
        assert router.prefix == "/api/v1/meshy"

    def test_meshy_tasks_db_exists(self):
        """In-memory tasks database should exist."""
        from app.api.meshy import meshy_tasks_db
        assert isinstance(meshy_tasks_db, dict)

    def test_pydantic_models_exist(self):
        """All Pydantic models should be importable."""
        from app.api.meshy import (
            MeshyWebhookPayload,
            TaskResponse,
            TaskListResponse,
            MeshyKeyRequest,
            MeshyKeyResponse,
            Model3DSettingsRequest
        )
        assert MeshyWebhookPayload is not None
        assert TaskResponse is not None
        assert TaskListResponse is not None
        assert MeshyKeyRequest is not None
        assert MeshyKeyResponse is not None
        assert Model3DSettingsRequest is not None


# =============================================================================
# Webhook Endpoint Tests (uses test client)
# =============================================================================

class TestMeshyWebhookEndpoint:
    """Test the Meshy webhook endpoint."""

    def test_receive_webhook_succeeded(self, client, mock_meshy_auth, mock_meshy_tasks, sample_webhook_payload):
        """Should process successful webhook and store task data."""
        response = client.post(
            "/api/v1/meshy/webhook",
            json=sample_webhook_payload
        )
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert data["task_id"] == "task-12345"
        assert data["status"] == "succeeded"

        # Verify task was stored
        assert "task-12345" in mock_meshy_tasks
        task = mock_meshy_tasks["task-12345"]
        assert task["status"] == "succeeded"
        assert task["progress"] == 100.0
        assert task["model_urls"]["glb"] == "https://meshy.ai/models/task-12345.glb"
        assert task["thumbnail_url"] == "https://meshy.ai/thumbnails/task-12345.png"
        assert task["prompt"] == "A cute robot"

    def test_receive_webhook_in_progress(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should handle IN_PROGRESS status."""
        payload = {
            "id": "task-progress-1",
            "status": "IN_PROGRESS",
            "prompt": "A spaceship"
        }

        response = client.post("/api/v1/meshy/webhook", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "in_progress"

        task = mock_meshy_tasks["task-progress-1"]
        assert task["status"] == "in_progress"
        assert task["progress"] == 50.0

    def test_receive_webhook_pending(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should handle PENDING status."""
        payload = {
            "id": "task-pending-1",
            "status": "PENDING",
            "prompt": "A castle"
        }

        response = client.post("/api/v1/meshy/webhook", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "pending"

        task = mock_meshy_tasks["task-pending-1"]
        assert task["status"] == "pending"

    def test_receive_webhook_failed(self, client, mock_meshy_auth, mock_meshy_tasks, sample_failed_webhook_payload):
        """Should handle FAILED status with error info."""
        response = client.post(
            "/api/v1/meshy/webhook",
            json=sample_failed_webhook_payload
        )
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"

        task = mock_meshy_tasks["task-failed-123"]
        assert task["status"] == "failed"
        assert task["progress"] == 0.0
        assert "moderation" in task["error"].lower()

    def test_receive_webhook_expired(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should map EXPIRED status to failed."""
        payload = {
            "id": "task-expired-1",
            "status": "EXPIRED"
        }

        response = client.post("/api/v1/meshy/webhook", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "failed"

        task = mock_meshy_tasks["task-expired-1"]
        assert task["status"] == "failed"

    def test_receive_webhook_unknown_status(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should handle unknown status by lowercasing it."""
        payload = {
            "id": "task-unknown-1",
            "status": "PROCESSING"
        }

        response = client.post("/api/v1/meshy/webhook", json=payload)
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "processing"

    def test_receive_webhook_updates_existing_task(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should update existing task with new webhook data."""
        # First register a task
        mock_meshy_tasks["task-update-1"] = {
            "id": "task-update-1",
            "type": "text-to-3d",
            "status": "pending",
            "prompt": "Original prompt",
            "created_at": datetime.utcnow()
        }

        # Then send webhook update
        payload = {
            "id": "task-update-1",
            "status": "SUCCEEDED",
            "model_urls": {"glb": "https://meshy.ai/models/updated.glb"},
            "finished_at": 1704067320000
        }

        response = client.post("/api/v1/meshy/webhook", json=payload)
        assert response.status_code == 200

        task = mock_meshy_tasks["task-update-1"]
        assert task["status"] == "succeeded"
        assert task["type"] == "text-to-3d"  # Preserved from original
        assert task["model_urls"]["glb"] == "https://meshy.ai/models/updated.glb"

    def test_receive_webhook_with_valid_signature(self, client, mock_meshy_auth, mock_meshy_tasks, mock_meshy_encryption, sample_webhook_payload):
        """Should verify valid webhook signature."""
        api_key = "msy_test_api_key"
        mock_meshy_encryption["get_key"].return_value = api_key

        # Create valid signature
        body_bytes = json.dumps(sample_webhook_payload).encode()
        expected_sig = hmac.new(
            api_key.encode(),
            body_bytes,
            hashlib.sha256
        ).hexdigest()

        response = client.post(
            "/api/v1/meshy/webhook",
            json=sample_webhook_payload,
            headers={"X-Meshy-Signature": expected_sig}
        )
        assert response.status_code == 200
        assert response.json()["received"] is True

    def test_receive_webhook_with_invalid_signature(self, client, mock_meshy_auth, mock_meshy_tasks, mock_meshy_encryption, sample_webhook_payload):
        """Should log warning for invalid signature but still process webhook."""
        api_key = "msy_test_api_key"
        mock_meshy_encryption["get_key"].return_value = api_key

        response = client.post(
            "/api/v1/meshy/webhook",
            json=sample_webhook_payload,
            headers={"X-Meshy-Signature": "invalid-signature"}
        )
        # Should still return 200 (MVP behavior - process anyway)
        assert response.status_code == 200
        assert response.json()["received"] is True

    def test_receive_webhook_invalid_json(self, client, mock_meshy_auth):
        """Should handle invalid JSON gracefully."""
        response = client.post(
            "/api/v1/meshy/webhook",
            content=b"not json",
            headers={"Content-Type": "application/json"}
        )
        # Should return 200 with error to prevent retries
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert "error" in data

    def test_receive_webhook_missing_required_fields(self, client, mock_meshy_auth):
        """Should handle missing required fields gracefully."""
        # Missing 'id' and 'status' which are required
        response = client.post(
            "/api/v1/meshy/webhook",
            json={"prompt": "test"}
        )
        # Should return 200 with error to prevent retries
        assert response.status_code == 200
        data = response.json()
        assert data["received"] is True
        assert "error" in data

    def test_receive_webhook_timestamps_converted(self, client, mock_meshy_auth, mock_meshy_tasks, sample_webhook_payload):
        """Should convert Unix timestamps to datetime objects."""
        response = client.post(
            "/api/v1/meshy/webhook",
            json=sample_webhook_payload
        )
        assert response.status_code == 200

        task = mock_meshy_tasks["task-12345"]
        assert isinstance(task["created_at"], datetime)
        assert isinstance(task["completed_at"], datetime)


# =============================================================================
# Task Management Endpoint Tests
# =============================================================================

class TestListTasksEndpoint:
    """Test the list tasks endpoint."""

    def test_list_tasks_empty(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should return empty list when no tasks."""
        response = client.get("/api/v1/meshy/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["tasks"] == []
        assert data["total"] == 0

    def test_list_tasks_with_tasks(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should return list of tasks."""
        now = datetime.utcnow()
        mock_meshy_tasks["task-1"] = {
            "id": "task-1",
            "type": "text-to-3d",
            "status": "succeeded",
            "prompt": "A dragon",
            "progress": 100.0,
            "created_at": now
        }
        mock_meshy_tasks["task-2"] = {
            "id": "task-2",
            "type": "image-to-3d",
            "status": "pending",
            "progress": 0.0,
            "created_at": now
        }

        response = client.get("/api/v1/meshy/tasks")
        assert response.status_code == 200
        data = response.json()
        assert len(data["tasks"]) == 2
        assert data["total"] == 2

    def test_list_tasks_filter_by_status(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should filter tasks by status."""
        now = datetime.utcnow()
        mock_meshy_tasks["task-1"] = {
            "id": "task-1",
            "status": "succeeded",
            "created_at": now
        }
        mock_meshy_tasks["task-2"] = {
            "id": "task-2",
            "status": "pending",
            "created_at": now
        }

        response = client.get("/api/v1/meshy/tasks?status_filter=succeeded")
        assert response.status_code == 200
        data = response.json()
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["id"] == "task-1"

    def test_list_tasks_filter_by_type(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should filter tasks by type."""
        now = datetime.utcnow()
        mock_meshy_tasks["task-1"] = {
            "id": "task-1",
            "type": "text-to-3d",
            "status": "succeeded",
            "created_at": now
        }
        mock_meshy_tasks["task-2"] = {
            "id": "task-2",
            "type": "image-to-3d",
            "status": "succeeded",
            "created_at": now
        }

        response = client.get("/api/v1/meshy/tasks?type_filter=image-to-3d")
        assert response.status_code == 200
        data = response.json()
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["id"] == "task-2"

    def test_list_tasks_with_limit(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should respect limit parameter."""
        now = datetime.utcnow()
        for i in range(10):
            mock_meshy_tasks[f"task-{i}"] = {
                "id": f"task-{i}",
                "status": "succeeded",
                "created_at": now
            }

        response = client.get("/api/v1/meshy/tasks?limit=5")
        assert response.status_code == 200
        data = response.json()
        assert len(data["tasks"]) == 5
        assert data["total"] == 10

    def test_list_tasks_sorted_by_created_at(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should sort tasks by created_at descending."""
        from datetime import timedelta
        now = datetime.utcnow()
        mock_meshy_tasks["old-task"] = {
            "id": "old-task",
            "status": "succeeded",
            "created_at": now - timedelta(hours=2)
        }
        mock_meshy_tasks["new-task"] = {
            "id": "new-task",
            "status": "succeeded",
            "created_at": now
        }

        response = client.get("/api/v1/meshy/tasks")
        assert response.status_code == 200
        data = response.json()
        assert data["tasks"][0]["id"] == "new-task"
        assert data["tasks"][1]["id"] == "old-task"


class TestGetTaskEndpoint:
    """Test the get task endpoint."""

    def test_get_task_success(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should return task details."""
        now = datetime.utcnow()
        mock_meshy_tasks["task-123"] = {
            "id": "task-123",
            "type": "text-to-3d",
            "status": "succeeded",
            "prompt": "A cute cat",
            "model_urls": {"glb": "https://meshy.ai/model.glb"},
            "thumbnail_url": "https://meshy.ai/thumb.png",
            "video_url": "https://meshy.ai/video.mp4",
            "progress": 100.0,
            "created_at": now
        }

        response = client.get("/api/v1/meshy/tasks/task-123")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "task-123"
        assert data["type"] == "text-to-3d"
        assert data["status"] == "succeeded"
        assert data["prompt"] == "A cute cat"
        assert data["progress"] == 100.0

    def test_get_task_not_found(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should return 404 for non-existent task."""
        response = client.get("/api/v1/meshy/tasks/nonexistent")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_task_with_error(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should include error details for failed tasks."""
        now = datetime.utcnow()
        mock_meshy_tasks["failed-task"] = {
            "id": "failed-task",
            "type": "text-to-3d",
            "status": "failed",
            "error": "Content moderation rejected",
            "progress": 0.0,
            "created_at": now
        }

        response = client.get("/api/v1/meshy/tasks/failed-task")
        assert response.status_code == 200
        data = response.json()
        assert data["error"] == "Content moderation rejected"


class TestDeleteTaskEndpoint:
    """Test the delete task endpoint."""

    def test_delete_task_success(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should delete task successfully."""
        mock_meshy_tasks["task-to-delete"] = {
            "id": "task-to-delete",
            "status": "succeeded",
            "created_at": datetime.utcnow()
        }

        response = client.delete("/api/v1/meshy/tasks/task-to-delete")
        assert response.status_code == 204
        assert "task-to-delete" not in mock_meshy_tasks

    def test_delete_task_not_found(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should return 404 for non-existent task."""
        response = client.delete("/api/v1/meshy/tasks/nonexistent")
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()


class TestRegisterTaskEndpoint:
    """Test the register task endpoint."""

    def test_register_task_success(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should register a new task."""
        response = client.post(
            "/api/v1/meshy/tasks/register",
            params={
                "task_id": "new-task-123",
                "task_type": "text-to-3d",
                "prompt": "A magical sword"
            }
        )
        assert response.status_code == 200
        data = response.json()
        assert data["registered"] is True
        assert data["task_id"] == "new-task-123"

        # Verify task was stored
        assert "new-task-123" in mock_meshy_tasks
        task = mock_meshy_tasks["new-task-123"]
        assert task["type"] == "text-to-3d"
        assert task["status"] == "pending"
        assert task["prompt"] == "A magical sword"
        assert task["progress"] == 0.0

    def test_register_task_with_metadata(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should register task with metadata."""
        response = client.post(
            "/api/v1/meshy/tasks/register",
            params={
                "task_id": "meta-task-123",
                "task_type": "image-to-3d"
            },
            json={"source_image": "image.png", "user_id": "user-1"}
        )
        assert response.status_code == 200

        task = mock_meshy_tasks["meta-task-123"]
        assert task["metadata"]["source_image"] == "image.png"

    def test_register_task_without_prompt(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should register task without optional prompt."""
        response = client.post(
            "/api/v1/meshy/tasks/register",
            params={
                "task_id": "no-prompt-task",
                "task_type": "rig"
            }
        )
        assert response.status_code == 200

        task = mock_meshy_tasks["no-prompt-task"]
        assert task["prompt"] is None

    def test_register_task_different_types(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should register tasks of different types."""
        task_types = ["text-to-3d", "image-to-3d", "retexture", "rig", "animate"]

        for i, task_type in enumerate(task_types):
            response = client.post(
                "/api/v1/meshy/tasks/register",
                params={
                    "task_id": f"task-type-{i}",
                    "task_type": task_type
                }
            )
            assert response.status_code == 200
            assert mock_meshy_tasks[f"task-type-{i}"]["type"] == task_type


# =============================================================================
# Meshy Configuration Endpoint Tests
# =============================================================================

class TestGetMeshyConfigEndpoint:
    """Test the get Meshy config endpoint."""

    def test_get_config_no_key(self, client, mock_meshy_auth, mock_meshy_encryption):
        """Should show no API key configured."""
        mock_meshy_encryption["get_key"].return_value = None

        response = client.get("/api/v1/meshy/config")
        assert response.status_code == 200
        data = response.json()
        assert data["api_key_set"] is False
        assert data["api_key_masked"] == ""
        assert data["webhook_url"] == "/api/v1/meshy/webhook"

    def test_get_config_with_key(self, client, mock_meshy_auth, mock_meshy_encryption):
        """Should show masked API key when configured."""
        mock_meshy_encryption["get_key"].return_value = "msy_test_api_key_12345"
        mock_meshy_encryption["mask_key"].return_value = "msy_tes...2345"

        response = client.get("/api/v1/meshy/config")
        assert response.status_code == 200
        data = response.json()
        assert data["api_key_set"] is True
        assert data["api_key_masked"] == "msy_tes...2345"


class TestSetMeshyApiKeyEndpoint:
    """Test the set Meshy API key endpoint."""

    def test_set_api_key_success(self, client, mock_meshy_auth, mock_meshy_encryption):
        """Should successfully set Meshy API key."""
        mock_meshy_encryption["mask_key"].return_value = "msy_val...key1"

        response = client.post(
            "/api/v1/meshy/config/api-key",
            json={"api_key": "msy_valid_api_key1"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["masked_key"] == "msy_val...key1"
        mock_meshy_encryption["set_key"].assert_called_once()

    def test_set_api_key_empty(self, client, mock_meshy_auth):
        """Should reject empty API key."""
        response = client.post(
            "/api/v1/meshy/config/api-key",
            json={"api_key": ""}
        )
        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()

    def test_set_api_key_whitespace_only(self, client, mock_meshy_auth):
        """Should reject whitespace-only API key."""
        response = client.post(
            "/api/v1/meshy/config/api-key",
            json={"api_key": "   "}
        )
        assert response.status_code == 400
        assert "empty" in response.json()["detail"].lower()

    def test_set_api_key_invalid_format(self, client, mock_meshy_auth):
        """Should reject API key not starting with 'msy_'."""
        response = client.post(
            "/api/v1/meshy/config/api-key",
            json={"api_key": "invalid_key_format"}
        )
        assert response.status_code == 400
        assert "msy_" in response.json()["detail"]

    def test_set_api_key_trims_whitespace(self, client, mock_meshy_auth, mock_meshy_encryption):
        """Should trim whitespace from API key."""
        mock_meshy_encryption["mask_key"].return_value = "msy_val...key2"

        response = client.post(
            "/api/v1/meshy/config/api-key",
            json={"api_key": "  msy_valid_api_key2  "}
        )
        assert response.status_code == 200
        # The set_key should be called with trimmed value
        mock_meshy_encryption["set_key"].assert_called_once_with("meshy_api_key", "msy_valid_api_key2")


class TestRemoveMeshyApiKeyEndpoint:
    """Test the remove Meshy API key endpoint."""

    def test_remove_api_key_success(self, client, mock_meshy_auth, mock_meshy_database):
        """Should successfully remove Meshy API key."""
        response = client.delete("/api/v1/meshy/config/api-key")
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        mock_meshy_database.delete_system_setting.assert_called_with("meshy_api_key")


class TestUpdateMeshySettingsEndpoint:
    """Test the update Meshy settings endpoint."""

    def test_update_model_latest(self, client, mock_meshy_auth, mock_meshy_database):
        """Should update 3D model to latest."""
        response = client.put(
            "/api/v1/meshy/config/settings",
            json={"model": "latest"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        mock_meshy_database.set_system_setting.assert_called_with("model3d_model", "latest")

    def test_update_model_meshy_5(self, client, mock_meshy_auth, mock_meshy_database):
        """Should update 3D model to meshy-5."""
        response = client.put(
            "/api/v1/meshy/config/settings",
            json={"model": "meshy-5"}
        )
        assert response.status_code == 200
        mock_meshy_database.set_system_setting.assert_called_with("model3d_model", "meshy-5")

    def test_update_model_meshy_4(self, client, mock_meshy_auth, mock_meshy_database):
        """Should update 3D model to meshy-4."""
        response = client.put(
            "/api/v1/meshy/config/settings",
            json={"model": "meshy-4"}
        )
        assert response.status_code == 200
        mock_meshy_database.set_system_setting.assert_called_with("model3d_model", "meshy-4")

    def test_update_model_invalid(self, client, mock_meshy_auth):
        """Should reject invalid model."""
        response = client.put(
            "/api/v1/meshy/config/settings",
            json={"model": "invalid-model"}
        )
        assert response.status_code == 400
        assert "Invalid model" in response.json()["detail"]

    def test_update_provider_meshy(self, client, mock_meshy_auth, mock_meshy_database):
        """Should update provider to meshy."""
        response = client.put(
            "/api/v1/meshy/config/settings",
            json={"provider": "meshy"}
        )
        assert response.status_code == 200
        mock_meshy_database.set_system_setting.assert_called_with("model3d_provider", "meshy")

    def test_update_provider_invalid(self, client, mock_meshy_auth):
        """Should reject invalid provider."""
        response = client.put(
            "/api/v1/meshy/config/settings",
            json={"provider": "invalid-provider"}
        )
        assert response.status_code == 400
        assert "Invalid provider" in response.json()["detail"]

    def test_update_both_model_and_provider(self, client, mock_meshy_auth, mock_meshy_database):
        """Should update both model and provider."""
        response = client.put(
            "/api/v1/meshy/config/settings",
            json={"model": "meshy-5", "provider": "meshy"}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_update_nothing(self, client, mock_meshy_auth, mock_meshy_database):
        """Should succeed with empty update."""
        mock_meshy_database.get_system_setting.return_value = "latest"

        response = client.put(
            "/api/v1/meshy/config/settings",
            json={}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True


# =============================================================================
# Internal API Key Endpoint Tests
# =============================================================================

class TestGetInternalMeshyKeyEndpoint:
    """Test the internal Meshy API key endpoint."""

    def test_get_internal_key_success(self, client, mock_meshy_auth, mock_meshy_encryption):
        """Should return decrypted API key."""
        mock_meshy_encryption["get_key"].return_value = "msy_secret_key"

        response = client.get("/api/v1/meshy/internal/api-key")
        assert response.status_code == 200
        data = response.json()
        assert data["key"] == "msy_secret_key"

    def test_get_internal_key_not_configured(self, client, mock_meshy_auth, mock_meshy_encryption):
        """Should return 404 when key not configured."""
        mock_meshy_encryption["get_key"].return_value = None

        response = client.get("/api/v1/meshy/internal/api-key")
        assert response.status_code == 404
        assert "not configured" in response.json()["detail"]


# =============================================================================
# Edge Cases and Error Handling Tests
# =============================================================================

class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_task_without_created_at(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should handle task without created_at gracefully."""
        mock_meshy_tasks["no-date-task"] = {
            "id": "no-date-task",
            "status": "pending"
        }

        response = client.get("/api/v1/meshy/tasks/no-date-task")
        assert response.status_code == 200
        # Should provide a default created_at

    def test_task_with_missing_optional_fields(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should handle task with missing optional fields."""
        mock_meshy_tasks["minimal-task"] = {
            "id": "minimal-task",
            "created_at": datetime.utcnow()
        }

        response = client.get("/api/v1/meshy/tasks/minimal-task")
        assert response.status_code == 200
        data = response.json()
        assert data["type"] == "text-to-3d"  # Default
        assert data["status"] == "pending"  # Default
        assert data["progress"] == 0.0  # Default

    def test_webhook_with_null_values(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should handle webhook with null optional values."""
        payload = {
            "id": "null-task",
            "status": "SUCCEEDED",
            "model_urls": None,
            "thumbnail_url": None,
            "video_url": None,
            "prompt": None
        }

        response = client.post("/api/v1/meshy/webhook", json=payload)
        assert response.status_code == 200

        task = mock_meshy_tasks["null-task"]
        assert "model_urls" not in task or task.get("model_urls") is None

    def test_concurrent_task_updates(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should handle multiple updates to same task."""
        # First update - pending
        payload1 = {"id": "concurrent-task", "status": "PENDING"}
        response1 = client.post("/api/v1/meshy/webhook", json=payload1)
        assert response1.status_code == 200
        assert mock_meshy_tasks["concurrent-task"]["status"] == "pending"

        # Second update - in progress
        payload2 = {"id": "concurrent-task", "status": "IN_PROGRESS"}
        response2 = client.post("/api/v1/meshy/webhook", json=payload2)
        assert response2.status_code == 200
        assert mock_meshy_tasks["concurrent-task"]["status"] == "in_progress"

        # Third update - succeeded
        payload3 = {
            "id": "concurrent-task",
            "status": "SUCCEEDED",
            "model_urls": {"glb": "https://meshy.ai/final.glb"}
        }
        response3 = client.post("/api/v1/meshy/webhook", json=payload3)
        assert response3.status_code == 200
        assert mock_meshy_tasks["concurrent-task"]["status"] == "succeeded"
        assert mock_meshy_tasks["concurrent-task"]["progress"] == 100.0

    def test_special_characters_in_prompt(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should handle special characters in prompt."""
        response = client.post(
            "/api/v1/meshy/tasks/register",
            params={
                "task_id": "special-chars-task",
                "task_type": "text-to-3d",
                "prompt": "A robot with <special> \"characters\" & symbols!"
            }
        )
        assert response.status_code == 200

        task = mock_meshy_tasks["special-chars-task"]
        assert task["prompt"] == "A robot with <special> \"characters\" & symbols!"

    def test_unicode_in_prompt(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should handle unicode characters in prompt."""
        response = client.post(
            "/api/v1/meshy/tasks/register",
            params={
                "task_id": "unicode-task",
                "task_type": "text-to-3d",
                "prompt": "A dragon with flames: flame dragon"
            }
        )
        assert response.status_code == 200

        task = mock_meshy_tasks["unicode-task"]
        assert "dragon" in task["prompt"].lower()


# =============================================================================
# Status Mapping Tests
# =============================================================================

class TestStatusMapping:
    """Test the status mapping from Meshy to internal format."""

    def test_all_status_mappings(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should correctly map all Meshy statuses."""
        status_tests = [
            ("PENDING", "pending"),
            ("IN_PROGRESS", "in_progress"),
            ("SUCCEEDED", "succeeded"),
            ("FAILED", "failed"),
            ("EXPIRED", "failed"),
        ]

        for i, (meshy_status, expected_internal) in enumerate(status_tests):
            task_id = f"status-test-{i}"
            response = client.post(
                "/api/v1/meshy/webhook",
                json={"id": task_id, "status": meshy_status}
            )
            assert response.status_code == 200
            assert mock_meshy_tasks[task_id]["status"] == expected_internal
            assert mock_meshy_tasks[task_id]["meshy_status"] == meshy_status


# =============================================================================
# Progress Calculation Tests
# =============================================================================

class TestProgressCalculation:
    """Test progress calculation based on status."""

    def test_progress_for_succeeded(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should set progress to 100 for succeeded tasks."""
        response = client.post(
            "/api/v1/meshy/webhook",
            json={"id": "progress-succeeded", "status": "SUCCEEDED"}
        )
        assert response.status_code == 200
        assert mock_meshy_tasks["progress-succeeded"]["progress"] == 100.0

    def test_progress_for_in_progress(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should set progress to 50 for in_progress tasks."""
        response = client.post(
            "/api/v1/meshy/webhook",
            json={"id": "progress-in-progress", "status": "IN_PROGRESS"}
        )
        assert response.status_code == 200
        assert mock_meshy_tasks["progress-in-progress"]["progress"] == 50.0

    def test_progress_for_failed(self, client, mock_meshy_auth, mock_meshy_tasks):
        """Should set progress to 0 for failed tasks."""
        response = client.post(
            "/api/v1/meshy/webhook",
            json={"id": "progress-failed", "status": "FAILED"}
        )
        assert response.status_code == 200
        assert mock_meshy_tasks["progress-failed"]["progress"] == 0.0
