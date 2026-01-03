"""
Tests for Canvas API endpoints.

This module provides comprehensive tests for the Canvas API including:
- Provider endpoints (image, video, TTS, STT)
- File upload and serving
- Image generation and editing
- Video generation
- TTS (text-to-speech) generation
- STT (speech-to-text) transcription
- Canvas item management (CRUD operations)
"""

import json
import os
import pytest
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from fastapi import HTTPException


# =============================================================================
# Test Fixtures
# =============================================================================

@pytest.fixture
def canvas_temp_dir():
    """Create a temporary directory for canvas tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_canvas_settings(canvas_temp_dir):
    """Mock canvas settings to use temp directory."""
    mock_settings = MagicMock()
    mock_settings.effective_workspace_dir = canvas_temp_dir
    with patch("app.api.canvas.settings", mock_settings):
        yield mock_settings


@pytest.fixture
def canvas_test_client(mock_canvas_settings, canvas_temp_dir):
    """Create a test client with all canvas dependencies mocked."""
    from fastapi.testclient import TestClient
    from fastapi import FastAPI
    from app.api.canvas import router

    # Create a minimal test app with just the canvas router
    test_app = FastAPI()
    test_app.include_router(router)

    # Override auth dependency
    def mock_require_auth():
        return "test-token"

    # Find and override the auth dependency in the router
    from app.api.auth import require_auth
    test_app.dependency_overrides[require_auth] = mock_require_auth

    with TestClient(test_app) as client:
        yield client


# =============================================================================
# Module Import Tests
# =============================================================================

class TestCanvasModuleImports:
    """Verify canvas module can be imported correctly."""

    def test_canvas_module_imports(self):
        """Canvas module should import without errors."""
        from app.api import canvas
        assert canvas is not None

    def test_canvas_router_exists(self):
        """Canvas router should exist."""
        from app.api.canvas import router
        assert router is not None
        assert router.prefix == "/api/v1/canvas"

    def test_provider_definitions_exist(self):
        """Provider definitions should exist."""
        from app.api.canvas import IMAGE_PROVIDERS, VIDEO_PROVIDERS, TTS_PROVIDERS, STT_PROVIDERS
        assert IMAGE_PROVIDERS is not None
        assert VIDEO_PROVIDERS is not None
        assert TTS_PROVIDERS is not None
        assert STT_PROVIDERS is not None

    def test_pydantic_models_exist(self):
        """Pydantic models should be importable."""
        from app.api.canvas import (
            CanvasItem, ImageGenerateRequest, VideoGenerateRequest,
            ImageEditRequest, CanvasListResponse, ProviderInfo,
            ProvidersResponse, TTSGenerateRequest, TTSGenerateResponse,
            STTTranscribeRequest, STTTranscribeResponse
        )
        assert CanvasItem is not None
        assert ImageGenerateRequest is not None


# =============================================================================
# Provider Data Tests
# =============================================================================

class TestProviderDefinitions:
    """Test provider data structures."""

    def test_image_providers_structure(self):
        """Image providers should have correct structure."""
        from app.api.canvas import IMAGE_PROVIDERS

        for provider_id, provider in IMAGE_PROVIDERS.items():
            assert "id" in provider
            assert "name" in provider
            assert "models" in provider
            assert "aspect_ratios" in provider
            assert "resolutions" in provider
            assert provider["id"] == provider_id

    def test_video_providers_structure(self):
        """Video providers should have correct structure."""
        from app.api.canvas import VIDEO_PROVIDERS

        for provider_id, provider in VIDEO_PROVIDERS.items():
            assert "id" in provider
            assert "name" in provider
            assert "models" in provider
            assert "aspect_ratios" in provider
            assert "max_duration" in provider
            assert provider["id"] == provider_id

    def test_tts_providers_structure(self):
        """TTS providers should have correct structure."""
        from app.api.canvas import TTS_PROVIDERS

        for provider_id, provider in TTS_PROVIDERS.items():
            assert "id" in provider
            assert "name" in provider
            assert "models" in provider
            assert "voices" in provider
            assert "output_formats" in provider

    def test_stt_providers_structure(self):
        """STT providers should have correct structure."""
        from app.api.canvas import STT_PROVIDERS

        for provider_id, provider in STT_PROVIDERS.items():
            assert "id" in provider
            assert "name" in provider
            assert "models" in provider
            assert "supported_formats" in provider

    def test_default_model_exists(self):
        """Each provider should have exactly one default model."""
        from app.api.canvas import IMAGE_PROVIDERS, VIDEO_PROVIDERS, TTS_PROVIDERS

        for provider_id, provider in IMAGE_PROVIDERS.items():
            defaults = [m for m in provider["models"] if m.get("default")]
            assert len(defaults) == 1, f"{provider_id} should have one default model"

        for provider_id, provider in VIDEO_PROVIDERS.items():
            defaults = [m for m in provider["models"] if m.get("default")]
            assert len(defaults) == 1, f"{provider_id} should have one default model"


# =============================================================================
# Helper Function Tests
# =============================================================================

class TestHelperFunctions:
    """Test canvas helper functions."""

    def test_get_file_url_image(self):
        """get_file_url should return correct URL for images."""
        from app.api.canvas import get_file_url

        url = get_file_url("/path/to/image.png", "image")
        assert url == "/api/v1/canvas/files/images/image.png"

    def test_get_file_url_video(self):
        """get_file_url should return correct URL for videos."""
        from app.api.canvas import get_file_url

        url = get_file_url("/path/to/video.mp4", "video")
        assert url == "/api/v1/canvas/files/videos/video.mp4"

    def test_get_file_url_audio(self):
        """get_file_url should return correct URL for audio."""
        from app.api.canvas import get_file_url

        url = get_file_url("/path/to/audio.mp3", "audio")
        assert url == "/api/v1/canvas/files/audio/audio.mp3"

    def test_get_file_url_unknown_type(self):
        """get_file_url should default to video for unknown types."""
        from app.api.canvas import get_file_url

        url = get_file_url("/path/to/file.xyz", "unknown")
        assert url == "/api/v1/canvas/files/videos/file.xyz"


class TestDecryptedApiKey:
    """Test _get_decrypted_api_key function."""

    def test_returns_none_when_not_found(self):
        """Should return None when setting doesn't exist."""
        from app.api.canvas import _get_decrypted_api_key

        with patch("app.api.canvas.database.get_system_setting", return_value=None):
            result = _get_decrypted_api_key("nonexistent_key")
            assert result is None

    def test_returns_plaintext_value(self):
        """Should return plaintext value when not encrypted."""
        from app.api.canvas import _get_decrypted_api_key

        with patch("app.api.canvas.database.get_system_setting", return_value="plain_api_key"):
            with patch("app.api.canvas.encryption.is_encrypted", return_value=False):
                result = _get_decrypted_api_key("test_key")
                assert result == "plain_api_key"

    def test_decrypts_encrypted_value(self):
        """Should decrypt encrypted values."""
        from app.api.canvas import _get_decrypted_api_key

        with patch("app.api.canvas.database.get_system_setting", return_value="encrypted:key"):
            with patch("app.api.canvas.encryption.is_encrypted", return_value=True):
                with patch("app.api.canvas.encryption.is_encryption_ready", return_value=True):
                    with patch("app.api.canvas.encryption.decrypt_value", return_value="decrypted_key"):
                        result = _get_decrypted_api_key("test_key")
                        assert result == "decrypted_key"

    def test_returns_none_when_encryption_not_ready(self):
        """Should return None when encryption is not ready."""
        from app.api.canvas import _get_decrypted_api_key

        with patch("app.api.canvas.database.get_system_setting", return_value="encrypted:key"):
            with patch("app.api.canvas.encryption.is_encrypted", return_value=True):
                with patch("app.api.canvas.encryption.is_encryption_ready", return_value=False):
                    result = _get_decrypted_api_key("test_key")
                    assert result is None

    def test_returns_none_on_decryption_failure(self):
        """Should return None when decryption fails."""
        from app.api.canvas import _get_decrypted_api_key

        with patch("app.api.canvas.database.get_system_setting", return_value="encrypted:key"):
            with patch("app.api.canvas.encryption.is_encrypted", return_value=True):
                with patch("app.api.canvas.encryption.is_encryption_ready", return_value=True):
                    with patch("app.api.canvas.encryption.decrypt_value", side_effect=Exception("Decryption failed")):
                        result = _get_decrypted_api_key("test_key")
                        assert result is None


# =============================================================================
# Canvas Items Storage Tests
# =============================================================================

class TestCanvasItemsStorage:
    """Test canvas items loading and saving."""

    def test_load_canvas_items_empty(self, canvas_temp_dir):
        """load_canvas_items should return empty list when file doesn't exist."""
        from app.api.canvas import load_canvas_items

        with patch("app.api.canvas.get_canvas_items_path", return_value=canvas_temp_dir / "nonexistent.json"):
            items = load_canvas_items()
            assert items == []

    def test_load_canvas_items_valid_json(self, canvas_temp_dir):
        """load_canvas_items should parse valid JSON correctly."""
        from app.api.canvas import load_canvas_items

        items_path = canvas_temp_dir / "canvas_items.json"
        test_items = [{"id": "test-1", "type": "image"}]
        items_path.write_text(json.dumps({"items": test_items}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            items = load_canvas_items()
            assert len(items) == 1
            assert items[0]["id"] == "test-1"

    def test_load_canvas_items_invalid_json(self, canvas_temp_dir):
        """load_canvas_items should return empty list for invalid JSON."""
        from app.api.canvas import load_canvas_items

        items_path = canvas_temp_dir / "canvas_items.json"
        items_path.write_text("not valid json")

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            items = load_canvas_items()
            assert items == []

    def test_save_canvas_items(self, canvas_temp_dir):
        """save_canvas_items should write items to JSON file."""
        from app.api.canvas import save_canvas_items

        items_path = canvas_temp_dir / "canvas_items.json"
        test_items = [{"id": "test-1", "type": "image"}]

        with patch("app.api.canvas.ensure_canvas_directories"):
            with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
                save_canvas_items(test_items)

        saved_data = json.loads(items_path.read_text())
        assert "items" in saved_data
        assert saved_data["items"] == test_items
        assert "updated_at" in saved_data

    def test_save_canvas_items_io_error(self, canvas_temp_dir):
        """save_canvas_items should raise HTTPException on IOError."""
        from app.api.canvas import save_canvas_items

        # Use an invalid path that will cause an IOError
        invalid_path = canvas_temp_dir / "nonexistent_dir" / "canvas_items.json"

        with patch("app.api.canvas.ensure_canvas_directories"):
            with patch("app.api.canvas.get_canvas_items_path", return_value=invalid_path):
                with pytest.raises(HTTPException) as exc_info:
                    save_canvas_items([{"id": "test"}])

        assert exc_info.value.status_code == 500

    def test_get_item_by_id_found(self, canvas_temp_dir):
        """get_item_by_id should return item when found."""
        from app.api.canvas import get_item_by_id

        items_path = canvas_temp_dir / "canvas_items.json"
        test_items = [{"id": "test-1", "type": "image"}, {"id": "test-2", "type": "video"}]
        items_path.write_text(json.dumps({"items": test_items}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            item = get_item_by_id("test-1")
            assert item is not None
            assert item["id"] == "test-1"

    def test_get_item_by_id_not_found(self, canvas_temp_dir):
        """get_item_by_id should return None when not found."""
        from app.api.canvas import get_item_by_id

        items_path = canvas_temp_dir / "canvas_items.json"
        test_items = [{"id": "test-1", "type": "image"}]
        items_path.write_text(json.dumps({"items": test_items}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            item = get_item_by_id("nonexistent")
            assert item is None


# =============================================================================
# API Endpoint Tests with TestClient
# =============================================================================

class TestProvidersEndpoint:
    """Test /providers endpoint."""

    def test_get_providers_success(self, canvas_test_client):
        """GET /providers should return provider information."""
        response = canvas_test_client.get("/api/v1/canvas/providers")
        assert response.status_code == 200

        data = response.json()
        assert "image_providers" in data
        assert "video_providers" in data
        assert len(data["image_providers"]) > 0
        assert len(data["video_providers"]) > 0

    def test_get_providers_has_expected_providers(self, canvas_test_client):
        """GET /providers should include known providers."""
        response = canvas_test_client.get("/api/v1/canvas/providers")
        data = response.json()

        image_provider_ids = [p["id"] for p in data["image_providers"]]
        assert "google-gemini" in image_provider_ids
        assert "google-imagen" in image_provider_ids
        assert "openai-gpt-image" in image_provider_ids

        video_provider_ids = [p["id"] for p in data["video_providers"]]
        assert "google-veo" in video_provider_ids
        assert "openai-sora" in video_provider_ids


class TestListCanvasItems:
    """Test listing canvas items."""

    def test_list_items_empty(self, canvas_test_client, canvas_temp_dir):
        """GET / should return empty list when no items exist."""
        items_path = canvas_temp_dir / "canvas_items.json"

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            response = canvas_test_client.get("/api/v1/canvas")
            assert response.status_code == 200

            data = response.json()
            assert data["items"] == []
            assert data["total"] == 0

    def test_list_items_with_data(self, canvas_test_client, canvas_temp_dir):
        """GET / should return items when they exist."""
        items_path = canvas_temp_dir / "canvas_items.json"
        test_items = [
            {"id": "1", "type": "image", "prompt": "test", "provider": "test",
             "file_path": "/test.png", "file_name": "test.png", "created_at": "2024-01-01T00:00:00Z",
             "updated_at": "2024-01-01T00:00:00Z", "aspect_ratio": "16:9"}
        ]
        items_path.write_text(json.dumps({"items": test_items}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            response = canvas_test_client.get("/api/v1/canvas")
            assert response.status_code == 200

            data = response.json()
            assert len(data["items"]) == 1
            assert data["total"] == 1

    def test_list_items_filter_by_type(self, canvas_test_client, canvas_temp_dir):
        """GET /?type=image should filter by type."""
        items_path = canvas_temp_dir / "canvas_items.json"
        test_items = [
            {"id": "1", "type": "image", "prompt": "img", "provider": "test",
             "file_path": "/img.png", "file_name": "img.png", "created_at": "2024-01-01T00:00:00Z",
             "updated_at": "2024-01-01T00:00:00Z", "aspect_ratio": "16:9"},
            {"id": "2", "type": "video", "prompt": "vid", "provider": "test",
             "file_path": "/vid.mp4", "file_name": "vid.mp4", "created_at": "2024-01-01T00:00:00Z",
             "updated_at": "2024-01-01T00:00:00Z", "aspect_ratio": "16:9"}
        ]
        items_path.write_text(json.dumps({"items": test_items}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            response = canvas_test_client.get("/api/v1/canvas?type=image")
            assert response.status_code == 200

            data = response.json()
            assert len(data["items"]) == 1
            assert data["items"][0]["type"] == "image"

    def test_list_items_filter_by_audio_type(self, canvas_test_client, canvas_temp_dir):
        """GET /?type=audio should filter by audio type."""
        items_path = canvas_temp_dir / "canvas_items.json"
        test_items = [
            {"id": "1", "type": "image", "prompt": "img", "provider": "test",
             "file_path": "/img.png", "file_name": "img.png", "created_at": "2024-01-01T00:00:00Z",
             "updated_at": "2024-01-01T00:00:00Z", "aspect_ratio": "16:9"},
            {"id": "2", "type": "audio", "prompt": "audio", "provider": "test",
             "file_path": "/audio.mp3", "file_name": "audio.mp3", "created_at": "2024-01-01T00:00:00Z",
             "updated_at": "2024-01-01T00:00:00Z", "aspect_ratio": "16:9"}
        ]
        items_path.write_text(json.dumps({"items": test_items}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            response = canvas_test_client.get("/api/v1/canvas?type=audio")
            assert response.status_code == 200

            data = response.json()
            assert len(data["items"]) == 1
            assert data["items"][0]["type"] == "audio"

    def test_list_items_invalid_type_filter(self, canvas_test_client, canvas_temp_dir):
        """GET /?type=invalid should return 400 error."""
        items_path = canvas_temp_dir / "canvas_items.json"
        items_path.write_text(json.dumps({"items": []}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            response = canvas_test_client.get("/api/v1/canvas?type=invalid")
            assert response.status_code == 400

    def test_list_items_pagination(self, canvas_test_client, canvas_temp_dir):
        """GET / should support pagination."""
        items_path = canvas_temp_dir / "canvas_items.json"
        test_items = [
            {"id": str(i), "type": "image", "prompt": f"test{i}", "provider": "test",
             "file_path": f"/test{i}.png", "file_name": f"test{i}.png",
             "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
             "aspect_ratio": "16:9"}
            for i in range(10)
        ]
        items_path.write_text(json.dumps({"items": test_items}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            response = canvas_test_client.get("/api/v1/canvas?limit=3&offset=2")
            assert response.status_code == 200

            data = response.json()
            assert len(data["items"]) == 3
            assert data["total"] == 10


class TestGetCanvasItem:
    """Test getting a single canvas item."""

    def test_get_item_success(self, canvas_test_client, canvas_temp_dir):
        """GET /{item_id} should return item when found."""
        items_path = canvas_temp_dir / "canvas_items.json"
        test_items = [
            {"id": "test-id-123", "type": "image", "prompt": "test", "provider": "test",
             "file_path": "/test.png", "file_name": "test.png", "created_at": "2024-01-01T00:00:00Z",
             "updated_at": "2024-01-01T00:00:00Z", "aspect_ratio": "16:9"}
        ]
        items_path.write_text(json.dumps({"items": test_items}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            response = canvas_test_client.get("/api/v1/canvas/test-id-123")
            assert response.status_code == 200
            assert response.json()["id"] == "test-id-123"

    def test_get_item_not_found(self, canvas_test_client, canvas_temp_dir):
        """GET /{item_id} should return 404 when not found."""
        items_path = canvas_temp_dir / "canvas_items.json"
        items_path.write_text(json.dumps({"items": []}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            response = canvas_test_client.get("/api/v1/canvas/nonexistent")
            assert response.status_code == 404


class TestDeleteCanvasItem:
    """Test deleting canvas items."""

    def test_delete_item_success(self, canvas_test_client, canvas_temp_dir):
        """DELETE /{item_id} should remove item."""
        items_path = canvas_temp_dir / "canvas_items.json"
        test_items = [
            {"id": "test-id-123", "type": "image", "file_path": "/test.png"}
        ]
        items_path.write_text(json.dumps({"items": test_items}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            with patch("app.api.canvas.ensure_canvas_directories"):
                response = canvas_test_client.delete("/api/v1/canvas/test-id-123")
                assert response.status_code == 204

                # Verify item was removed
                saved_data = json.loads(items_path.read_text())
                assert len(saved_data["items"]) == 0

    def test_delete_item_not_found(self, canvas_test_client, canvas_temp_dir):
        """DELETE /{item_id} should return 404 when not found."""
        items_path = canvas_temp_dir / "canvas_items.json"
        items_path.write_text(json.dumps({"items": []}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            response = canvas_test_client.delete("/api/v1/canvas/nonexistent")
            assert response.status_code == 404

    def test_delete_item_with_file(self, canvas_test_client, canvas_temp_dir):
        """DELETE /{item_id}?delete_file=true should delete the file too."""
        items_path = canvas_temp_dir / "canvas_items.json"
        file_path = canvas_temp_dir / "test.png"
        file_path.write_bytes(b"fake image data")

        test_items = [
            {"id": "test-id-123", "type": "image", "file_path": str(file_path)}
        ]
        items_path.write_text(json.dumps({"items": test_items}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            with patch("app.api.canvas.ensure_canvas_directories"):
                response = canvas_test_client.delete("/api/v1/canvas/test-id-123?delete_file=true")
                assert response.status_code == 204
                assert not file_path.exists()


class TestFileUpload:
    """Test file upload endpoint."""

    def test_upload_valid_image(self, canvas_test_client, canvas_temp_dir):
        """POST /upload should accept valid image files."""
        with patch("app.api.canvas.get_uploads_dir", return_value=canvas_temp_dir):
            with patch("app.api.canvas.ensure_canvas_directories"):
                # Create a simple test image
                files = {"file": ("test.png", b"\x89PNG\r\n\x1a\n", "image/png")}
                response = canvas_test_client.post("/api/v1/canvas/upload", files=files)

                assert response.status_code == 200
                data = response.json()
                assert "path" in data
                assert "filename" in data
                assert data["size"] > 0

    def test_upload_jpeg_image(self, canvas_test_client, canvas_temp_dir):
        """POST /upload should accept JPEG files."""
        with patch("app.api.canvas.get_uploads_dir", return_value=canvas_temp_dir):
            with patch("app.api.canvas.ensure_canvas_directories"):
                files = {"file": ("test.jpg", b"\xff\xd8\xff\xe0", "image/jpeg")}
                response = canvas_test_client.post("/api/v1/canvas/upload", files=files)
                assert response.status_code == 200

    def test_upload_gif_image(self, canvas_test_client, canvas_temp_dir):
        """POST /upload should accept GIF files."""
        with patch("app.api.canvas.get_uploads_dir", return_value=canvas_temp_dir):
            with patch("app.api.canvas.ensure_canvas_directories"):
                files = {"file": ("test.gif", b"GIF89a", "image/gif")}
                response = canvas_test_client.post("/api/v1/canvas/upload", files=files)
                assert response.status_code == 200

    def test_upload_webp_image(self, canvas_test_client, canvas_temp_dir):
        """POST /upload should accept WebP files."""
        with patch("app.api.canvas.get_uploads_dir", return_value=canvas_temp_dir):
            with patch("app.api.canvas.ensure_canvas_directories"):
                files = {"file": ("test.webp", b"RIFF", "image/webp")}
                response = canvas_test_client.post("/api/v1/canvas/upload", files=files)
                assert response.status_code == 200

    def test_upload_invalid_file_type(self, canvas_test_client, canvas_temp_dir):
        """POST /upload should reject invalid file types."""
        with patch("app.api.canvas.get_uploads_dir", return_value=canvas_temp_dir):
            files = {"file": ("test.txt", b"text content", "text/plain")}
            response = canvas_test_client.post("/api/v1/canvas/upload", files=files)

            assert response.status_code == 400
            assert "Invalid file type" in response.json()["detail"]


class TestServeFiles:
    """Test file serving endpoints."""

    def test_serve_image_success(self, canvas_test_client, canvas_temp_dir):
        """GET /files/images/{filename} should serve existing images."""
        image_path = canvas_temp_dir / "test.png"
        image_path.write_bytes(b"\x89PNG\r\n\x1a\n fake png data")

        with patch("app.api.canvas.get_images_dir", return_value=canvas_temp_dir):
            response = canvas_test_client.get("/api/v1/canvas/files/images/test.png")
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/png"

    def test_serve_image_not_found(self, canvas_test_client, canvas_temp_dir):
        """GET /files/images/{filename} should return 404 for missing images."""
        with patch("app.api.canvas.get_images_dir", return_value=canvas_temp_dir):
            response = canvas_test_client.get("/api/v1/canvas/files/images/nonexistent.png")
            assert response.status_code == 404

    def test_serve_video_success(self, canvas_test_client, canvas_temp_dir):
        """GET /files/videos/{filename} should serve existing videos."""
        video_path = canvas_temp_dir / "test.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x1cftyp fake mp4 data")

        with patch("app.api.canvas.get_videos_dir", return_value=canvas_temp_dir):
            response = canvas_test_client.get("/api/v1/canvas/files/videos/test.mp4")
            assert response.status_code == 200
            assert response.headers["content-type"] == "video/mp4"

    def test_serve_video_not_found(self, canvas_test_client, canvas_temp_dir):
        """GET /files/videos/{filename} should return 404 for missing videos."""
        with patch("app.api.canvas.get_videos_dir", return_value=canvas_temp_dir):
            response = canvas_test_client.get("/api/v1/canvas/files/videos/nonexistent.mp4")
            assert response.status_code == 404

    def test_serve_audio_success(self, canvas_test_client, canvas_temp_dir):
        """GET /files/audio/{filename} should serve existing audio."""
        audio_path = canvas_temp_dir / "test.mp3"
        audio_path.write_bytes(b"ID3 fake mp3 data")

        with patch("app.api.canvas.get_audio_dir", return_value=canvas_temp_dir):
            response = canvas_test_client.get("/api/v1/canvas/files/audio/test.mp3")
            assert response.status_code == 200
            assert response.headers["content-type"] == "audio/mpeg"

    def test_serve_audio_not_found(self, canvas_test_client, canvas_temp_dir):
        """GET /files/audio/{filename} should return 404 for missing audio."""
        with patch("app.api.canvas.get_audio_dir", return_value=canvas_temp_dir):
            response = canvas_test_client.get("/api/v1/canvas/files/audio/nonexistent.mp3")
            assert response.status_code == 404


# =============================================================================
# Image Generation Tests
# =============================================================================

class TestImageGeneration:
    """Test image generation endpoint."""

    def test_generate_image_invalid_provider(self, canvas_test_client):
        """POST /generate/image should reject invalid provider."""
        response = canvas_test_client.post("/api/v1/canvas/generate/image", json={
            "prompt": "test prompt",
            "provider": "invalid-provider"
        })
        assert response.status_code == 400
        assert "Invalid provider" in response.json()["detail"]

    def test_generate_image_invalid_aspect_ratio(self, canvas_test_client):
        """POST /generate/image should reject invalid aspect ratio."""
        response = canvas_test_client.post("/api/v1/canvas/generate/image", json={
            "prompt": "test prompt",
            "provider": "google-gemini",
            "aspect_ratio": "99:99"
        })
        assert response.status_code == 400
        assert "Aspect ratio" in response.json()["detail"]

    def test_generate_image_invalid_resolution(self, canvas_test_client):
        """POST /generate/image should reject invalid resolution."""
        response = canvas_test_client.post("/api/v1/canvas/generate/image", json={
            "prompt": "test prompt",
            "provider": "google-gemini",
            "resolution": "99K"
        })
        assert response.status_code == 400
        assert "Resolution" in response.json()["detail"]

    def test_generate_image_reference_unsupported(self, canvas_test_client):
        """POST /generate/image should reject reference images for unsupported providers."""
        response = canvas_test_client.post("/api/v1/canvas/generate/image", json={
            "prompt": "test prompt",
            "provider": "google-imagen",  # Does not support reference
            "reference_images": ["/some/image.png"]
        })
        assert response.status_code == 400
        assert "does not support reference" in response.json()["detail"]

    def test_generate_image_success(self, canvas_test_client, canvas_temp_dir):
        """POST /generate/image should create image on success."""
        items_path = canvas_temp_dir / "canvas_items.json"
        images_dir = canvas_temp_dir / "images"
        images_dir.mkdir()

        # Create mock output file
        mock_output = images_dir / "generated.png"
        mock_output.write_bytes(b"fake image data")

        mock_result = {
            "success": True,
            "file_path": str(mock_output),
            "model_used": "gemini-2.5-flash-image"
        }

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            with patch("app.api.canvas.get_images_dir", return_value=images_dir):
                with patch("app.api.canvas.ensure_canvas_directories"):
                    with patch("app.api.canvas.execute_ai_tool", return_value=mock_result):
                        response = canvas_test_client.post("/api/v1/canvas/generate/image", json={
                            "prompt": "A beautiful sunset",
                            "provider": "google-gemini"
                        })

                        assert response.status_code == 201
                        data = response.json()
                        assert data["type"] == "image"
                        assert data["prompt"] == "A beautiful sunset"
                        assert data["provider"] == "google-gemini"

    def test_generate_image_with_reference(self, canvas_test_client, canvas_temp_dir):
        """POST /generate/image should handle reference images."""
        items_path = canvas_temp_dir / "canvas_items.json"
        images_dir = canvas_temp_dir / "images"
        images_dir.mkdir()

        mock_output = images_dir / "generated.png"
        mock_output.write_bytes(b"fake image data")

        mock_result = {
            "success": True,
            "file_path": str(mock_output),
            "model_used": "gemini-2.5-flash-image"
        }

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            with patch("app.api.canvas.get_images_dir", return_value=images_dir):
                with patch("app.api.canvas.ensure_canvas_directories"):
                    with patch("app.api.canvas.execute_ai_tool", return_value=mock_result):
                        response = canvas_test_client.post("/api/v1/canvas/generate/image", json={
                            "prompt": "A cat in this style",
                            "provider": "google-gemini",
                            "reference_images": ["/ref/image.png"]
                        })

                        assert response.status_code == 201

    def test_generate_image_ai_tool_error(self, canvas_test_client, canvas_temp_dir):
        """POST /generate/image should return 500 on AI tool error."""
        items_path = canvas_temp_dir / "canvas_items.json"
        images_dir = canvas_temp_dir / "images"
        images_dir.mkdir()

        mock_result = {
            "success": False,
            "error": "AI generation failed"
        }

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            with patch("app.api.canvas.get_images_dir", return_value=images_dir):
                with patch("app.api.canvas.ensure_canvas_directories"):
                    with patch("app.api.canvas.execute_ai_tool", return_value=mock_result):
                        response = canvas_test_client.post("/api/v1/canvas/generate/image", json={
                            "prompt": "test",
                            "provider": "google-gemini"
                        })

                        assert response.status_code == 500
                        assert "AI generation failed" in response.json()["detail"]

    def test_generate_image_file_not_created(self, canvas_test_client, canvas_temp_dir):
        """POST /generate/image should return 500 if file not created."""
        items_path = canvas_temp_dir / "canvas_items.json"
        images_dir = canvas_temp_dir / "images"
        images_dir.mkdir()

        mock_result = {
            "success": True,
            "file_path": str(images_dir / "nonexistent.png")
        }

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            with patch("app.api.canvas.get_images_dir", return_value=images_dir):
                with patch("app.api.canvas.ensure_canvas_directories"):
                    with patch("app.api.canvas.execute_ai_tool", return_value=mock_result):
                        response = canvas_test_client.post("/api/v1/canvas/generate/image", json={
                            "prompt": "test",
                            "provider": "google-gemini"
                        })

                        assert response.status_code == 500
                        assert "file was not created" in response.json()["detail"]


# =============================================================================
# Image Edit Tests
# =============================================================================

class TestImageEdit:
    """Test image editing endpoint."""

    def test_edit_image_invalid_provider(self, canvas_test_client):
        """POST /edit/image should reject invalid provider."""
        response = canvas_test_client.post("/api/v1/canvas/edit/image", json={
            "item_id": "test-id",
            "prompt": "make it blue",
            "provider": "invalid-provider"
        })
        assert response.status_code == 400
        assert "Invalid provider" in response.json()["detail"]

    def test_edit_image_unsupported_provider(self, canvas_test_client):
        """POST /edit/image should reject providers that don't support editing."""
        response = canvas_test_client.post("/api/v1/canvas/edit/image", json={
            "item_id": "test-id",
            "prompt": "make it blue",
            "provider": "google-imagen"  # Does not support edit
        })
        assert response.status_code == 400
        assert "does not support image editing" in response.json()["detail"]

    def test_edit_image_item_not_found(self, canvas_test_client, canvas_temp_dir):
        """POST /edit/image should return 404 when item not found."""
        items_path = canvas_temp_dir / "canvas_items.json"
        items_path.write_text(json.dumps({"items": []}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            response = canvas_test_client.post("/api/v1/canvas/edit/image", json={
                "item_id": "nonexistent",
                "prompt": "make it blue",
                "provider": "google-gemini"
            })
            assert response.status_code == 404

    def test_edit_image_wrong_type(self, canvas_test_client, canvas_temp_dir):
        """POST /edit/image should reject non-image items."""
        items_path = canvas_temp_dir / "canvas_items.json"
        test_items = [{"id": "video-id", "type": "video", "file_path": "/test.mp4"}]
        items_path.write_text(json.dumps({"items": test_items}))

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            response = canvas_test_client.post("/api/v1/canvas/edit/image", json={
                "item_id": "video-id",
                "prompt": "make it blue",
                "provider": "google-gemini"
            })
            assert response.status_code == 400
            assert "Can only edit image items" in response.json()["detail"]

    def test_edit_image_success(self, canvas_test_client, canvas_temp_dir):
        """POST /edit/image should create edited image on success."""
        items_path = canvas_temp_dir / "canvas_items.json"
        images_dir = canvas_temp_dir / "images"
        images_dir.mkdir()

        original_image = images_dir / "original.png"
        original_image.write_bytes(b"original image data")

        edited_image = images_dir / "edited.png"
        edited_image.write_bytes(b"edited image data")

        test_items = [{
            "id": "original-id",
            "type": "image",
            "prompt": "original prompt",
            "provider": "google-gemini",
            "file_path": str(original_image),
            "file_name": "original.png",
            "aspect_ratio": "16:9",
            "resolution": "1K",
            "created_at": "2024-01-01T00:00:00Z",
            "updated_at": "2024-01-01T00:00:00Z"
        }]
        items_path.write_text(json.dumps({"items": test_items}))

        mock_result = {
            "success": True,
            "file_path": str(edited_image),
            "model_used": "gemini-2.5-flash-image"
        }

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            with patch("app.api.canvas.get_images_dir", return_value=images_dir):
                with patch("app.api.canvas.ensure_canvas_directories"):
                    with patch("app.api.canvas.execute_ai_tool", return_value=mock_result):
                        response = canvas_test_client.post("/api/v1/canvas/edit/image", json={
                            "item_id": "original-id",
                            "prompt": "make it blue",
                            "provider": "google-gemini"
                        })

                        assert response.status_code == 201
                        data = response.json()
                        assert data["type"] == "image"
                        assert data["parent_id"] == "original-id"
                        assert data["prompt"] == "make it blue"


# =============================================================================
# Video Generation Tests
# =============================================================================

class TestVideoGeneration:
    """Test video generation endpoint."""

    def test_generate_video_invalid_provider(self, canvas_test_client):
        """POST /generate/video should reject invalid provider."""
        response = canvas_test_client.post("/api/v1/canvas/generate/video", json={
            "prompt": "test prompt",
            "provider": "invalid-provider"
        })
        assert response.status_code == 400
        assert "Invalid provider" in response.json()["detail"]

    def test_generate_video_invalid_aspect_ratio(self, canvas_test_client):
        """POST /generate/video should reject invalid aspect ratio."""
        response = canvas_test_client.post("/api/v1/canvas/generate/video", json={
            "prompt": "test prompt",
            "provider": "google-veo",
            "aspect_ratio": "99:99"
        })
        assert response.status_code == 400
        assert "Aspect ratio" in response.json()["detail"]

    def test_generate_video_duration_too_long(self, canvas_test_client):
        """POST /generate/video should reject duration exceeding provider max."""
        # Pydantic validates duration <= 60, so test with value that's within Pydantic
        # but exceeds the provider's max_duration (google-veo max is 8)
        response = canvas_test_client.post("/api/v1/canvas/generate/video", json={
            "prompt": "test prompt",
            "provider": "google-veo",
            "duration": 20  # Above google-veo max_duration of 8
        })
        assert response.status_code == 400
        assert "Duration" in response.json()["detail"]

    def test_generate_video_success(self, canvas_test_client, canvas_temp_dir):
        """POST /generate/video should create video on success."""
        items_path = canvas_temp_dir / "canvas_items.json"
        videos_dir = canvas_temp_dir / "videos"
        videos_dir.mkdir()

        mock_output = videos_dir / "generated.mp4"
        mock_output.write_bytes(b"fake video data")

        mock_result = {
            "success": True,
            "file_path": str(mock_output),
            "model_used": "veo-3.1-generate-preview"
        }

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            with patch("app.api.canvas.get_videos_dir", return_value=videos_dir):
                with patch("app.api.canvas.ensure_canvas_directories"):
                    with patch("app.api.canvas.execute_ai_tool", return_value=mock_result):
                        response = canvas_test_client.post("/api/v1/canvas/generate/video", json={
                            "prompt": "A cat playing piano",
                            "provider": "google-veo",
                            "duration": 4
                        })

                        assert response.status_code == 201
                        data = response.json()
                        assert data["type"] == "video"
                        assert data["prompt"] == "A cat playing piano"
                        assert data["provider"] == "google-veo"

    def test_generate_video_with_source_image(self, canvas_test_client, canvas_temp_dir):
        """POST /generate/video should handle source image for image-to-video."""
        items_path = canvas_temp_dir / "canvas_items.json"
        videos_dir = canvas_temp_dir / "videos"
        videos_dir.mkdir()

        mock_output = videos_dir / "generated.mp4"
        mock_output.write_bytes(b"fake video data")

        mock_result = {
            "success": True,
            "file_path": str(mock_output),
            "model_used": "veo-3.1-generate-preview"
        }

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            with patch("app.api.canvas.get_videos_dir", return_value=videos_dir):
                with patch("app.api.canvas.ensure_canvas_directories"):
                    with patch("app.api.canvas.execute_ai_tool", return_value=mock_result):
                        response = canvas_test_client.post("/api/v1/canvas/generate/video", json={
                            "prompt": "Animate this image",
                            "provider": "google-veo",
                            "duration": 4,
                            "source_image": "/path/to/source.png"
                        })

                        assert response.status_code == 201


# =============================================================================
# TTS Generation Tests
# =============================================================================

class TestTTSGeneration:
    """Test text-to-speech generation endpoint."""

    def test_generate_tts_invalid_provider(self, canvas_test_client):
        """POST /generate/tts should reject invalid provider."""
        response = canvas_test_client.post("/api/v1/canvas/generate/tts", json={
            "text": "Hello world",
            "provider": "invalid-provider"
        })
        assert response.status_code == 400
        assert "Invalid TTS provider" in response.json()["detail"]

    def test_generate_tts_invalid_model(self, canvas_test_client):
        """POST /generate/tts should reject invalid model."""
        response = canvas_test_client.post("/api/v1/canvas/generate/tts", json={
            "text": "Hello world",
            "provider": "openai-tts",
            "model": "invalid-model"
        })
        assert response.status_code == 400
        assert "Invalid TTS model" in response.json()["detail"]

    def test_generate_tts_invalid_voice(self, canvas_test_client):
        """POST /generate/tts should reject invalid voice."""
        response = canvas_test_client.post("/api/v1/canvas/generate/tts", json={
            "text": "Hello world",
            "provider": "openai-tts",
            "voice": "invalid-voice"
        })
        assert response.status_code == 400
        assert "Invalid voice" in response.json()["detail"]

    def test_generate_tts_invalid_format(self, canvas_test_client):
        """POST /generate/tts should reject invalid output format."""
        response = canvas_test_client.post("/api/v1/canvas/generate/tts", json={
            "text": "Hello world",
            "provider": "openai-tts",
            "output_format": "invalid-format"
        })
        assert response.status_code == 400
        assert "Invalid output format" in response.json()["detail"]

    def test_generate_tts_success(self, canvas_test_client, canvas_temp_dir):
        """POST /generate/tts should create audio on success."""
        items_path = canvas_temp_dir / "canvas_items.json"
        audio_dir = canvas_temp_dir / "audio"
        audio_dir.mkdir()

        mock_output = audio_dir / "speech.mp3"
        mock_output.write_bytes(b"fake audio data")

        mock_result = {
            "success": True,
            "file_path": str(mock_output),
            "model_used": "gpt-4o-mini-tts"
        }

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            with patch("app.api.canvas.get_audio_dir", return_value=audio_dir):
                with patch("app.api.canvas.ensure_canvas_directories"):
                    with patch("app.api.canvas.execute_ai_tool", return_value=mock_result):
                        response = canvas_test_client.post("/api/v1/canvas/generate/tts", json={
                            "text": "Hello world",
                            "provider": "openai-tts",
                            "voice": "alloy"
                        })

                        assert response.status_code == 201
                        data = response.json()
                        assert data["provider"] == "openai-tts"
                        assert data["voice"] == "alloy"
                        assert "url" in data

    def test_generate_tts_with_voice_instructions(self, canvas_test_client, canvas_temp_dir):
        """POST /generate/tts should handle voice instructions."""
        items_path = canvas_temp_dir / "canvas_items.json"
        audio_dir = canvas_temp_dir / "audio"
        audio_dir.mkdir()

        mock_output = audio_dir / "speech.mp3"
        mock_output.write_bytes(b"fake audio data")

        mock_result = {
            "success": True,
            "file_path": str(mock_output),
            "model_used": "gpt-4o-mini-tts"
        }

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            with patch("app.api.canvas.get_audio_dir", return_value=audio_dir):
                with patch("app.api.canvas.ensure_canvas_directories"):
                    with patch("app.api.canvas.execute_ai_tool", return_value=mock_result):
                        response = canvas_test_client.post("/api/v1/canvas/generate/tts", json={
                            "text": "Hello world",
                            "provider": "openai-tts",
                            "model": "gpt-4o-mini-tts",
                            "voice": "alloy",
                            "voice_instructions": "Speak slowly and clearly"
                        })

                        assert response.status_code == 201


class TestTTSProviders:
    """Test TTS providers endpoint."""

    def test_get_tts_providers_success(self, canvas_test_client):
        """GET /tts/providers should return provider information."""
        with patch("app.api.canvas._get_decrypted_api_key", return_value="test-api-key"):
            response = canvas_test_client.get("/api/v1/canvas/tts/providers")
            assert response.status_code == 200

            data = response.json()
            assert "providers" in data
            assert "openai_configured" in data

    def test_get_tts_providers_shows_availability(self, canvas_test_client):
        """GET /tts/providers should show provider availability based on API keys."""
        with patch("app.api.canvas._get_decrypted_api_key", return_value=None):
            response = canvas_test_client.get("/api/v1/canvas/tts/providers")
            data = response.json()

            assert data["openai_configured"] is False
            for provider in data["providers"]:
                if provider["id"] == "openai-tts":
                    assert provider["available"] is False


# =============================================================================
# STT Transcription Tests
# =============================================================================

class TestSTTTranscription:
    """Test speech-to-text transcription endpoint.

    Note: The transcribe endpoint has a design limitation where JSON body
    requests with file upload parameters result in the Pydantic model
    defaulting. File upload tests verify the current behavior.
    """

    def test_transcribe_default_provider_rejected(self, canvas_test_client):
        """POST /transcribe with default provider should fail validation.

        When file upload is used without explicit provider, the endpoint
        creates a default STTTranscribeRequest() with provider="openai"
        which is not a valid provider ID (should be "openai-stt").
        """
        files = {"file": ("test.mp3", b"fake audio", "audio/mpeg")}
        response = canvas_test_client.post("/api/v1/canvas/transcribe", files=files)

        assert response.status_code == 400
        assert "Invalid STT provider" in response.json()["detail"]
        assert "openai-stt" in response.json()["detail"]

    def test_transcribe_file_upload_uses_default_behavior(self, canvas_test_client, canvas_temp_dir):
        """POST /transcribe file upload demonstrates default provider issue.

        This documents current API behavior where file uploads without
        explicit provider parameter fail because default 'openai' is invalid.
        """
        with patch("app.api.canvas.get_uploads_dir", return_value=canvas_temp_dir):
            with patch("app.api.canvas.ensure_canvas_directories"):
                files = {"file": ("test.mp3", b"fake audio data", "audio/mpeg")}
                response = canvas_test_client.post("/api/v1/canvas/transcribe", files=files)

                assert response.status_code == 400
                assert "Invalid STT provider" in response.json()["detail"]


class TestSTTTranscriptionDirect:
    """Test STT transcription functions and models directly.

    Since the API endpoint has design limitations for full HTTP testing,
    we test the underlying logic and models directly.
    """

    def test_stt_provider_structure(self):
        """STT_PROVIDERS should have valid structure."""
        from app.api.canvas import STT_PROVIDERS

        assert "openai-stt" in STT_PROVIDERS
        provider = STT_PROVIDERS["openai-stt"]
        assert provider["id"] == "openai-stt"
        assert len(provider["models"]) > 0
        assert "supported_formats" in provider

    def test_stt_request_default_provider_mismatch(self):
        """STTTranscribeRequest default provider doesn't match valid providers.

        This documents an API design issue where the default provider
        value in the Pydantic model doesn't match any valid provider ID.
        """
        from app.api.canvas import STTTranscribeRequest, STT_PROVIDERS

        request = STTTranscribeRequest()

        # Default is "openai" but valid provider is "openai-stt"
        assert request.provider == "openai"
        assert request.provider not in STT_PROVIDERS

    def test_stt_request_with_valid_provider(self):
        """STTTranscribeRequest should work with valid provider."""
        from app.api.canvas import STTTranscribeRequest, STT_PROVIDERS

        request = STTTranscribeRequest(provider="openai-stt")
        assert request.provider == "openai-stt"
        assert request.provider in STT_PROVIDERS

    def test_stt_request_with_audio_url(self):
        """STTTranscribeRequest should accept audio_url."""
        from app.api.canvas import STTTranscribeRequest

        request = STTTranscribeRequest(
            provider="openai-stt",
            audio_url="/path/to/audio.mp3"
        )
        assert request.audio_url == "/path/to/audio.mp3"

    def test_stt_request_model_validation(self):
        """STTTranscribeRequest should validate model parameter."""
        from app.api.canvas import STTTranscribeRequest

        request = STTTranscribeRequest(
            provider="openai-stt",
            model="gpt-4o-transcribe"
        )
        assert request.model == "gpt-4o-transcribe"

    def test_stt_request_optional_params(self):
        """STTTranscribeRequest should handle optional parameters."""
        from app.api.canvas import STTTranscribeRequest

        request = STTTranscribeRequest(
            provider="openai-stt",
            language="en",
            translate=True,
            timestamp_granularity="word"
        )
        assert request.language == "en"
        assert request.translate is True
        assert request.timestamp_granularity == "word"


class TestSTTProviders:
    """Test STT providers endpoint."""

    def test_get_stt_providers_success(self, canvas_test_client):
        """GET /stt/providers should return provider information."""
        with patch("app.api.canvas._get_decrypted_api_key", return_value="test-api-key"):
            response = canvas_test_client.get("/api/v1/canvas/stt/providers")
            assert response.status_code == 200

            data = response.json()
            assert "providers" in data
            assert "openai_configured" in data


# =============================================================================
# Execute AI Tool Tests
# =============================================================================

class TestExecuteAITool:
    """Test execute_ai_tool function."""

    def test_execute_ai_tool_success(self, canvas_temp_dir):
        """execute_ai_tool should parse JSON output correctly."""
        from app.api.canvas import execute_ai_tool
        import subprocess

        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stdout = '{"success": true, "message": "done"}'
        mock_process.stderr = ""

        with patch("app.api.canvas.get_canvas_dir", return_value=canvas_temp_dir):
            with patch("app.api.canvas.ensure_canvas_directories"):
                with patch("subprocess.run", return_value=mock_process):
                    with patch("app.api.canvas._get_decrypted_api_key", return_value=None):
                        result = execute_ai_tool("console.log('{}')")

        assert result["success"] is True

    def test_execute_ai_tool_non_json_output(self, canvas_temp_dir):
        """execute_ai_tool should handle non-JSON output."""
        from app.api.canvas import execute_ai_tool

        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stdout = "Plain text output"
        mock_process.stderr = ""

        with patch("app.api.canvas.get_canvas_dir", return_value=canvas_temp_dir):
            with patch("app.api.canvas.ensure_canvas_directories"):
                with patch("subprocess.run", return_value=mock_process):
                    with patch("app.api.canvas._get_decrypted_api_key", return_value=None):
                        result = execute_ai_tool("console.log('test')")

        assert result["message"] == "Plain text output"
        assert result["success"] is True

    def test_execute_ai_tool_failure(self, canvas_temp_dir):
        """execute_ai_tool should raise HTTPException on failure."""
        from app.api.canvas import execute_ai_tool

        mock_process = MagicMock()
        mock_process.returncode = 1
        mock_process.stdout = ""
        mock_process.stderr = "Error: Something went wrong"

        with patch("app.api.canvas.get_canvas_dir", return_value=canvas_temp_dir):
            with patch("app.api.canvas.ensure_canvas_directories"):
                with patch("subprocess.run", return_value=mock_process):
                    with patch("app.api.canvas._get_decrypted_api_key", return_value=None):
                        with pytest.raises(HTTPException) as exc_info:
                            execute_ai_tool("bad script")

        assert exc_info.value.status_code == 500
        assert "Error: Something went wrong" in exc_info.value.detail

    def test_execute_ai_tool_timeout(self, canvas_temp_dir):
        """execute_ai_tool should raise 504 on timeout."""
        from app.api.canvas import execute_ai_tool
        import subprocess

        with patch("app.api.canvas.get_canvas_dir", return_value=canvas_temp_dir):
            with patch("app.api.canvas.ensure_canvas_directories"):
                with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("node", 300)):
                    with patch("app.api.canvas._get_decrypted_api_key", return_value=None):
                        with pytest.raises(HTTPException) as exc_info:
                            execute_ai_tool("slow script")

        assert exc_info.value.status_code == 504
        assert "timed out" in exc_info.value.detail.lower()

    def test_execute_ai_tool_node_not_found(self, canvas_temp_dir):
        """execute_ai_tool should raise 500 when Node.js not found."""
        from app.api.canvas import execute_ai_tool

        with patch("app.api.canvas.get_canvas_dir", return_value=canvas_temp_dir):
            with patch("app.api.canvas.ensure_canvas_directories"):
                with patch("subprocess.run", side_effect=FileNotFoundError()):
                    with patch("app.api.canvas._get_decrypted_api_key", return_value=None):
                        with pytest.raises(HTTPException) as exc_info:
                            execute_ai_tool("script")

        assert exc_info.value.status_code == 500
        assert "Node.js not found" in exc_info.value.detail

    def test_execute_ai_tool_empty_output(self, canvas_temp_dir):
        """execute_ai_tool should raise error on empty output."""
        from app.api.canvas import execute_ai_tool

        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stdout = ""
        mock_process.stderr = ""

        with patch("app.api.canvas.get_canvas_dir", return_value=canvas_temp_dir):
            with patch("app.api.canvas.ensure_canvas_directories"):
                with patch("subprocess.run", return_value=mock_process):
                    with patch("app.api.canvas._get_decrypted_api_key", return_value=None):
                        with pytest.raises(HTTPException) as exc_info:
                            execute_ai_tool("empty output script")

        assert exc_info.value.status_code == 500
        assert "empty output" in exc_info.value.detail.lower()

    def test_execute_ai_tool_with_api_keys(self, canvas_temp_dir):
        """execute_ai_tool should pass API keys from database to environment."""
        from app.api.canvas import execute_ai_tool

        mock_process = MagicMock()
        mock_process.returncode = 0
        mock_process.stdout = '{"success": true}'
        mock_process.stderr = ""

        captured_env = {}

        def capture_run(*args, **kwargs):
            captured_env.update(kwargs.get("env", {}))
            return mock_process

        with patch("app.api.canvas.get_canvas_dir", return_value=canvas_temp_dir):
            with patch("app.api.canvas.ensure_canvas_directories"):
                with patch("subprocess.run", side_effect=capture_run):
                    with patch("app.api.canvas._get_decrypted_api_key", side_effect=lambda k: {
                        "image_api_key": "gemini-key",
                        "openai_api_key": "openai-key"
                    }.get(k)):
                        execute_ai_tool("test script")

        assert captured_env.get("GEMINI_API_KEY") == "gemini-key"
        assert captured_env.get("OPENAI_API_KEY") == "openai-key"


# =============================================================================
# Create Canvas Item Tests
# =============================================================================

class TestCreateCanvasItem:
    """Test create_canvas_item function."""

    def test_create_canvas_item_basic(self, canvas_temp_dir):
        """create_canvas_item should create and save an item."""
        from app.api.canvas import create_canvas_item

        items_path = canvas_temp_dir / "canvas_items.json"
        file_path = canvas_temp_dir / "test.png"
        file_path.write_bytes(b"test data")

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            with patch("app.api.canvas.ensure_canvas_directories"):
                item = create_canvas_item(
                    item_type="image",
                    prompt="test prompt",
                    provider="google-gemini",
                    file_path=str(file_path),
                    model="gemini-2.5-flash-image",
                    aspect_ratio="16:9",
                    resolution="1K"
                )

        assert item["type"] == "image"
        assert item["prompt"] == "test prompt"
        assert item["provider"] == "google-gemini"
        assert item["file_size"] > 0
        assert "id" in item
        assert "created_at" in item

    def test_create_canvas_item_with_parent(self, canvas_temp_dir):
        """create_canvas_item should include parent_id for edits."""
        from app.api.canvas import create_canvas_item

        items_path = canvas_temp_dir / "canvas_items.json"
        file_path = canvas_temp_dir / "edited.png"
        file_path.write_bytes(b"test data")

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            with patch("app.api.canvas.ensure_canvas_directories"):
                item = create_canvas_item(
                    item_type="image",
                    prompt="edit prompt",
                    provider="google-gemini",
                    file_path=str(file_path),
                    parent_id="original-item-id"
                )

        assert item["parent_id"] == "original-item-id"

    def test_create_canvas_item_with_metadata(self, canvas_temp_dir):
        """create_canvas_item should include metadata."""
        from app.api.canvas import create_canvas_item

        items_path = canvas_temp_dir / "canvas_items.json"
        file_path = canvas_temp_dir / "test.png"
        file_path.write_bytes(b"test data")

        with patch("app.api.canvas.get_canvas_items_path", return_value=items_path):
            with patch("app.api.canvas.ensure_canvas_directories"):
                item = create_canvas_item(
                    item_type="image",
                    prompt="test prompt",
                    provider="google-gemini",
                    file_path=str(file_path),
                    metadata={"custom_field": "custom_value"}
                )

        assert item["metadata"]["custom_field"] == "custom_value"


# =============================================================================
# Directory Helper Tests
# =============================================================================

class TestDirectoryHelpers:
    """Test directory helper functions."""

    def test_ensure_canvas_directories(self, canvas_temp_dir):
        """ensure_canvas_directories should create all required directories."""
        from app.api.canvas import ensure_canvas_directories

        with patch("app.api.canvas.settings") as mock_settings:
            mock_settings.effective_workspace_dir = canvas_temp_dir

            ensure_canvas_directories()

            assert (canvas_temp_dir / "canvas").exists()
            assert (canvas_temp_dir / "canvas" / "images").exists()
            assert (canvas_temp_dir / "canvas" / "videos").exists()
            assert (canvas_temp_dir / "canvas" / "uploads").exists()
            assert (canvas_temp_dir / "canvas" / "audio").exists()

    def test_get_canvas_dir(self, canvas_temp_dir):
        """get_canvas_dir should return correct path."""
        from app.api.canvas import get_canvas_dir

        with patch("app.api.canvas.settings") as mock_settings:
            mock_settings.effective_workspace_dir = canvas_temp_dir

            result = get_canvas_dir()
            assert result == canvas_temp_dir / "canvas"

    def test_get_images_dir(self, canvas_temp_dir):
        """get_images_dir should return correct path."""
        from app.api.canvas import get_images_dir

        with patch("app.api.canvas.settings") as mock_settings:
            mock_settings.effective_workspace_dir = canvas_temp_dir

            result = get_images_dir()
            assert result == canvas_temp_dir / "canvas" / "images"

    def test_get_videos_dir(self, canvas_temp_dir):
        """get_videos_dir should return correct path."""
        from app.api.canvas import get_videos_dir

        with patch("app.api.canvas.settings") as mock_settings:
            mock_settings.effective_workspace_dir = canvas_temp_dir

            result = get_videos_dir()
            assert result == canvas_temp_dir / "canvas" / "videos"

    def test_get_audio_dir(self, canvas_temp_dir):
        """get_audio_dir should return correct path."""
        from app.api.canvas import get_audio_dir

        with patch("app.api.canvas.settings") as mock_settings:
            mock_settings.effective_workspace_dir = canvas_temp_dir

            result = get_audio_dir()
            assert result == canvas_temp_dir / "canvas" / "audio"

    def test_get_uploads_dir(self, canvas_temp_dir):
        """get_uploads_dir should return correct path."""
        from app.api.canvas import get_uploads_dir

        with patch("app.api.canvas.settings") as mock_settings:
            mock_settings.effective_workspace_dir = canvas_temp_dir

            result = get_uploads_dir()
            assert result == canvas_temp_dir / "canvas" / "uploads"

    def test_get_canvas_items_path(self, canvas_temp_dir):
        """get_canvas_items_path should return correct path."""
        from app.api.canvas import get_canvas_items_path

        with patch("app.api.canvas.settings") as mock_settings:
            mock_settings.effective_workspace_dir = canvas_temp_dir

            result = get_canvas_items_path()
            assert result == canvas_temp_dir / "canvas" / "canvas_items.json"


# =============================================================================
# Pydantic Model Validation Tests
# =============================================================================

class TestPydanticModels:
    """Test Pydantic model validation."""

    def test_image_generate_request_validation(self):
        """ImageGenerateRequest should validate input."""
        from app.api.canvas import ImageGenerateRequest

        # Valid request
        req = ImageGenerateRequest(prompt="test")
        assert req.prompt == "test"
        assert req.provider == "google-gemini"  # default

        # Empty prompt should fail
        with pytest.raises(Exception):
            ImageGenerateRequest(prompt="")

    def test_video_generate_request_validation(self):
        """VideoGenerateRequest should validate input."""
        from app.api.canvas import VideoGenerateRequest

        # Valid request
        req = VideoGenerateRequest(prompt="test", duration=8)
        assert req.duration == 8

        # Duration out of range should fail
        with pytest.raises(Exception):
            VideoGenerateRequest(prompt="test", duration=100)

    def test_tts_generate_request_validation(self):
        """TTSGenerateRequest should validate input."""
        from app.api.canvas import TTSGenerateRequest

        # Valid request
        req = TTSGenerateRequest(text="Hello")
        assert req.text == "Hello"
        assert req.speed == 1.0

        # Speed out of range should fail
        with pytest.raises(Exception):
            TTSGenerateRequest(text="Hello", speed=10.0)

    def test_canvas_item_model(self):
        """CanvasItem should parse correctly."""
        from app.api.canvas import CanvasItem

        item = CanvasItem(
            id="test-id",
            type="image",
            prompt="test prompt",
            provider="test-provider",
            file_path="/path/to/file.png",
            file_name="file.png",
            created_at="2024-01-01T00:00:00Z",
            updated_at="2024-01-01T00:00:00Z"
        )
        assert item.id == "test-id"
        assert item.type == "image"

    def test_stt_transcribe_request_defaults(self):
        """STTTranscribeRequest should have correct defaults."""
        from app.api.canvas import STTTranscribeRequest

        # Note: The default provider is "openai" in the Pydantic model,
        # but the API requires "openai-stt" as the provider ID
        req = STTTranscribeRequest()
        assert req.provider == "openai"  # Default value in Pydantic model
        assert req.diarization is False
        assert req.translate is False

    def test_stt_transcribe_request_with_valid_provider(self):
        """STTTranscribeRequest should accept valid provider."""
        from app.api.canvas import STTTranscribeRequest

        req = STTTranscribeRequest(provider="openai-stt")
        assert req.provider == "openai-stt"


# =============================================================================
# Audio MIME Type Tests
# =============================================================================

class TestAudioMimeTypes:
    """Test audio file MIME type handling."""

    def test_serve_audio_mp3_mime(self, canvas_test_client, canvas_temp_dir):
        """MP3 files should be served with audio/mpeg MIME type."""
        audio_path = canvas_temp_dir / "test.mp3"
        audio_path.write_bytes(b"fake mp3")

        with patch("app.api.canvas.get_audio_dir", return_value=canvas_temp_dir):
            response = canvas_test_client.get("/api/v1/canvas/files/audio/test.mp3")
            assert response.headers["content-type"] == "audio/mpeg"

    def test_serve_audio_wav_mime(self, canvas_test_client, canvas_temp_dir):
        """WAV files should be served with audio/wav MIME type."""
        audio_path = canvas_temp_dir / "test.wav"
        audio_path.write_bytes(b"RIFF fake wav")

        with patch("app.api.canvas.get_audio_dir", return_value=canvas_temp_dir):
            response = canvas_test_client.get("/api/v1/canvas/files/audio/test.wav")
            assert response.headers["content-type"] == "audio/wav"

    def test_serve_audio_opus_mime(self, canvas_test_client, canvas_temp_dir):
        """Opus files should be served with audio/opus MIME type."""
        audio_path = canvas_temp_dir / "test.opus"
        audio_path.write_bytes(b"OggS fake opus")

        with patch("app.api.canvas.get_audio_dir", return_value=canvas_temp_dir):
            response = canvas_test_client.get("/api/v1/canvas/files/audio/test.opus")
            assert response.headers["content-type"] == "audio/opus"

    def test_serve_audio_flac_mime(self, canvas_test_client, canvas_temp_dir):
        """FLAC files should be served with audio/flac MIME type."""
        audio_path = canvas_temp_dir / "test.flac"
        audio_path.write_bytes(b"fLaC fake flac")

        with patch("app.api.canvas.get_audio_dir", return_value=canvas_temp_dir):
            response = canvas_test_client.get("/api/v1/canvas/files/audio/test.flac")
            assert response.headers["content-type"] == "audio/flac"

    def test_serve_audio_aac_mime(self, canvas_test_client, canvas_temp_dir):
        """AAC files should be served with audio/aac MIME type."""
        audio_path = canvas_temp_dir / "test.aac"
        audio_path.write_bytes(b"fake aac")

        with patch("app.api.canvas.get_audio_dir", return_value=canvas_temp_dir):
            response = canvas_test_client.get("/api/v1/canvas/files/audio/test.aac")
            assert response.headers["content-type"] == "audio/aac"
