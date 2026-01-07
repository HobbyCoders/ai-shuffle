"""
Unit tests for Studio API endpoints.

Tests cover:
- Image generation endpoints
- Video generation endpoints
- Generation management (list, get, delete)
- Asset management (create, list, get, delete, update tags)
- Provider information endpoints
- Authentication requirements
- Validation and error handling
- Edge cases and boundary conditions
"""

import pytest
from datetime import datetime
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from fastapi import FastAPI

from app.api.studio import router, generations_db, assets_db
from app.api.auth import require_auth


# =============================================================================
# Fixtures
# =============================================================================

def mock_require_auth_dependency():
    """Mock dependency that returns a test token."""
    return "test-session-token"


@pytest.fixture
def client():
    """Create a test client with mocked dependencies."""
    app = FastAPI()
    app.include_router(router)
    # Override the require_auth dependency
    app.dependency_overrides[require_auth] = mock_require_auth_dependency
    with TestClient(app) as test_client:
        yield test_client
    # Clean up
    app.dependency_overrides.clear()


@pytest.fixture(autouse=True)
def clear_databases():
    """Clear in-memory databases before each test."""
    generations_db.clear()
    assets_db.clear()
    yield
    generations_db.clear()
    assets_db.clear()


@pytest.fixture
def sample_generation():
    """Create a sample generation in the database."""
    gen_id = "test-gen-id-123"
    now = datetime.utcnow()
    generation = {
        "id": gen_id,
        "type": "image",
        "prompt": "A beautiful sunset",
        "status": "pending",
        "progress": 0.0,
        "thumbnail_url": None,
        "result_url": None,
        "provider": "google-gemini",
        "created_at": now,
        "completed_at": None,
        "error": None,
        "metadata": {
            "aspect_ratio": "1:1",
            "style_preset": None,
            "negative_prompt": None
        }
    }
    generations_db[gen_id] = generation
    return generation


@pytest.fixture
def completed_generation():
    """Create a completed generation in the database."""
    gen_id = "completed-gen-id-456"
    now = datetime.utcnow()
    generation = {
        "id": gen_id,
        "type": "image",
        "prompt": "A mountain landscape",
        "status": "completed",
        "progress": 100.0,
        "thumbnail_url": "https://example.com/thumb.jpg",
        "result_url": "https://example.com/result.jpg",
        "provider": "google-imagen",
        "created_at": now,
        "completed_at": now,
        "error": None,
        "metadata": {
            "aspect_ratio": "16:9",
            "style_preset": None,
            "negative_prompt": "blurry"
        }
    }
    generations_db[gen_id] = generation
    return generation


@pytest.fixture
def sample_asset():
    """Create a sample asset in the database."""
    asset_id = "test-asset-id-789"
    now = datetime.utcnow()
    asset = {
        "id": asset_id,
        "type": "image",
        "url": "https://example.com/asset.jpg",
        "thumbnail_url": "https://example.com/asset-thumb.jpg",
        "prompt": "A beautiful sunset",
        "provider": "google-gemini",
        "created_at": now,
        "tags": ["nature", "sunset"],
        "metadata": {
            "generation_id": "test-gen-id-123",
            "aspect_ratio": "1:1"
        }
    }
    assets_db[asset_id] = asset
    return asset


@pytest.fixture
def video_generation():
    """Create a sample video generation."""
    gen_id = "video-gen-id-101"
    now = datetime.utcnow()
    generation = {
        "id": gen_id,
        "type": "video",
        "prompt": "A flying bird",
        "status": "pending",
        "progress": 0.0,
        "thumbnail_url": None,
        "result_url": None,
        "provider": "google-veo",
        "created_at": now,
        "completed_at": None,
        "error": None,
        "metadata": {
            "aspect_ratio": "16:9",
            "duration": 8,
            "image_path": None,
            "image_url": None
        }
    }
    generations_db[gen_id] = generation
    return generation


# =============================================================================
# Test Module Imports
# =============================================================================

class TestStudioModuleImports:
    """Verify studio module can be imported correctly."""

    def test_studio_module_imports(self):
        """Studio module should import without errors."""
        from app.api import studio
        assert studio is not None

    def test_studio_router_exists(self):
        """Studio router should exist."""
        from app.api.studio import router
        assert router is not None

    def test_router_has_correct_prefix(self):
        """Router should have /api/v1/studio prefix."""
        from app.api.studio import router
        assert router.prefix == "/api/v1/studio"


# =============================================================================
# Test Image Generation Endpoint
# =============================================================================

class TestGenerateImage:
    """Test POST /image/generate endpoint."""

    def test_generate_image_success(self, client):
        """Should generate image with valid request."""
        response = client.post(
            "/api/v1/studio/image/generate",
            json={
                "prompt": "A beautiful sunset over mountains",
                "provider": "google-gemini",
                "aspect_ratio": "16:9"
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["type"] == "image"
        assert data["prompt"] == "A beautiful sunset over mountains"
        assert data["status"] == "pending"
        assert data["progress"] == 0.0
        assert data["provider"] == "google-gemini"
        assert "id" in data
        assert "created_at" in data

    def test_generate_image_default_provider(self, client):
        """Should use default provider when not specified."""
        response = client.post(
            "/api/v1/studio/image/generate",
            json={"prompt": "A forest scene"}
        )

        assert response.status_code == 201
        data = response.json()
        assert data["provider"] == "google-gemini"

    def test_generate_image_default_aspect_ratio(self, client):
        """Should use default aspect ratio when not specified."""
        response = client.post(
            "/api/v1/studio/image/generate",
            json={"prompt": "A forest scene"}
        )

        assert response.status_code == 201
        data = response.json()
        assert data["metadata"]["aspect_ratio"] == "1:1"

    def test_generate_image_with_style_preset(self, client):
        """Should accept style preset for supported providers."""
        response = client.post(
            "/api/v1/studio/image/generate",
            json={
                "prompt": "A portrait",
                "provider": "openai-gpt-image",
                "style_preset": "natural"
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["metadata"]["style_preset"] == "natural"

    def test_generate_image_with_negative_prompt(self, client):
        """Should accept negative prompt."""
        response = client.post(
            "/api/v1/studio/image/generate",
            json={
                "prompt": "A landscape",
                "provider": "google-imagen",
                "negative_prompt": "blurry, dark"
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["metadata"]["negative_prompt"] == "blurry, dark"

    def test_generate_image_unknown_provider(self, client):
        """Should return 400 for unknown provider."""
        response = client.post(
            "/api/v1/studio/image/generate",
            json={
                "prompt": "A sunset",
                "provider": "unknown-provider"
            }
        )

        assert response.status_code == 400
        assert "Unknown provider" in response.json()["detail"]

    def test_generate_image_video_provider(self, client):
        """Should return 400 when using video-only provider for images."""
        response = client.post(
            "/api/v1/studio/image/generate",
            json={
                "prompt": "A sunset",
                "provider": "google-veo"
            }
        )

        assert response.status_code == 400
        assert "does not support image generation" in response.json()["detail"]

    def test_generate_image_invalid_aspect_ratio(self, client):
        """Should return 400 for invalid aspect ratio."""
        response = client.post(
            "/api/v1/studio/image/generate",
            json={
                "prompt": "A sunset",
                "provider": "google-gemini",
                "aspect_ratio": "21:9"  # Not supported
            }
        )

        assert response.status_code == 400
        assert "Invalid aspect ratio" in response.json()["detail"]

    def test_generate_image_empty_prompt(self, client):
        """Should return 422 for empty prompt."""
        response = client.post(
            "/api/v1/studio/image/generate",
            json={"prompt": ""}
        )

        assert response.status_code == 422

    def test_generate_image_missing_prompt(self, client):
        """Should return 422 for missing prompt."""
        response = client.post(
            "/api/v1/studio/image/generate",
            json={}
        )

        assert response.status_code == 422

    def test_generate_image_stores_in_db(self, client):
        """Should store generation in database."""
        response = client.post(
            "/api/v1/studio/image/generate",
            json={"prompt": "Test prompt"}
        )

        assert response.status_code == 201
        gen_id = response.json()["id"]
        assert gen_id in generations_db
        assert generations_db[gen_id]["prompt"] == "Test prompt"

    def test_generate_image_all_providers(self, client):
        """Should work with all image providers."""
        image_providers = ["google-gemini", "google-imagen", "openai-gpt-image"]

        for provider in image_providers:
            response = client.post(
                "/api/v1/studio/image/generate",
                json={
                    "prompt": f"Test with {provider}",
                    "provider": provider
                }
            )
            assert response.status_code == 201, f"Failed for provider {provider}"
            assert response.json()["provider"] == provider


# =============================================================================
# Test Video Generation Endpoint
# =============================================================================

class TestGenerateVideo:
    """Test POST /video/generate endpoint."""

    def test_generate_video_success(self, client):
        """Should generate video with valid request."""
        response = client.post(
            "/api/v1/studio/video/generate",
            json={
                "prompt": "A flying bird over the ocean",
                "provider": "google-veo",
                "duration": 8,
                "aspect_ratio": "16:9"
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["type"] == "video"
        assert data["prompt"] == "A flying bird over the ocean"
        assert data["status"] == "pending"
        assert data["provider"] == "google-veo"
        assert data["metadata"]["duration"] == 8

    def test_generate_video_default_provider(self, client):
        """Should use default provider when not specified."""
        response = client.post(
            "/api/v1/studio/video/generate",
            json={"prompt": "A car driving"}
        )

        assert response.status_code == 201
        data = response.json()
        assert data["provider"] == "google-veo"

    def test_generate_video_default_duration(self, client):
        """Should use default duration when not specified."""
        response = client.post(
            "/api/v1/studio/video/generate",
            json={"prompt": "A car driving"}
        )

        assert response.status_code == 201
        data = response.json()
        assert data["metadata"]["duration"] == 8

    def test_generate_video_with_image_path(self, client):
        """Should accept image path for image-to-video."""
        response = client.post(
            "/api/v1/studio/video/generate",
            json={
                "prompt": "Animate this image",
                "provider": "google-veo",
                "image_path": "/path/to/image.jpg"
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["metadata"]["image_path"] == "/path/to/image.jpg"

    def test_generate_video_with_image_url(self, client):
        """Should accept image URL for image-to-video."""
        response = client.post(
            "/api/v1/studio/video/generate",
            json={
                "prompt": "Animate this image",
                "provider": "google-veo",
                "image_url": "https://example.com/image.jpg"
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["metadata"]["image_url"] == "https://example.com/image.jpg"

    def test_generate_video_unknown_provider(self, client):
        """Should return 400 for unknown provider."""
        response = client.post(
            "/api/v1/studio/video/generate",
            json={
                "prompt": "A sunset",
                "provider": "unknown-provider"
            }
        )

        assert response.status_code == 400
        assert "Unknown provider" in response.json()["detail"]

    def test_generate_video_image_provider(self, client):
        """Should return 400 when using image-only provider for video."""
        response = client.post(
            "/api/v1/studio/video/generate",
            json={
                "prompt": "A sunset",
                "provider": "google-gemini"
            }
        )

        assert response.status_code == 400
        assert "does not support video generation" in response.json()["detail"]

    def test_generate_video_invalid_aspect_ratio(self, client):
        """Should return 400 for invalid aspect ratio."""
        response = client.post(
            "/api/v1/studio/video/generate",
            json={
                "prompt": "A sunset",
                "provider": "google-veo",
                "aspect_ratio": "4:3"  # Not supported for video
            }
        )

        assert response.status_code == 400
        assert "Invalid aspect ratio" in response.json()["detail"]

    def test_generate_video_invalid_duration(self, client):
        """Should return 400 for invalid duration for provider."""
        # Google Veo supports [5, 6, 8], so 10 should fail business validation
        response = client.post(
            "/api/v1/studio/video/generate",
            json={
                "prompt": "A sunset",
                "provider": "google-veo",
                "duration": 10  # Not in google-veo's supported list [5, 6, 8]
            }
        )

        assert response.status_code == 400
        assert "Invalid duration" in response.json()["detail"]

    def test_generate_video_image_to_video_not_supported(self, client):
        """Should return 400 when provider doesn't support image-to-video."""
        response = client.post(
            "/api/v1/studio/video/generate",
            json={
                "prompt": "Animate this",
                "provider": "openai-sora",
                "duration": 10,  # Valid duration for openai-sora
                "image_url": "https://example.com/image.jpg"
            }
        )

        assert response.status_code == 400
        assert "does not support image-to-video" in response.json()["detail"]

    def test_generate_video_all_providers(self, client):
        """Should work with all video providers."""
        video_configs = [
            {"provider": "google-veo", "duration": 8},
            {"provider": "openai-sora", "duration": 10}
        ]

        for config in video_configs:
            response = client.post(
                "/api/v1/studio/video/generate",
                json={
                    "prompt": f"Test with {config['provider']}",
                    **config
                }
            )
            assert response.status_code == 201, f"Failed for provider {config['provider']}"

    def test_generate_video_duration_validation(self, client):
        """Should validate duration is within range."""
        # Test below minimum
        response = client.post(
            "/api/v1/studio/video/generate",
            json={
                "prompt": "A sunset",
                "duration": 2  # Below min of 4
            }
        )
        assert response.status_code == 422

        # Test above maximum
        response = client.post(
            "/api/v1/studio/video/generate",
            json={
                "prompt": "A sunset",
                "duration": 20  # Above max of 16
            }
        )
        assert response.status_code == 422


# =============================================================================
# Test List Generations Endpoint
# =============================================================================

class TestListGenerations:
    """Test GET /generations endpoint."""

    def test_list_generations_empty(self, client):
        """Should return empty list when no generations."""
        response = client.get("/api/v1/studio/generations")

        assert response.status_code == 200
        data = response.json()
        assert data["generations"] == []
        assert data["total"] == 0

    def test_list_generations_with_data(self, client, sample_generation, video_generation):
        """Should return all generations."""
        response = client.get("/api/v1/studio/generations")

        assert response.status_code == 200
        data = response.json()
        assert len(data["generations"]) == 2
        assert data["total"] == 2

    def test_list_generations_filter_by_type_image(self, client, sample_generation, video_generation):
        """Should filter by image type."""
        response = client.get("/api/v1/studio/generations?type=image")

        assert response.status_code == 200
        data = response.json()
        assert len(data["generations"]) == 1
        assert data["generations"][0]["type"] == "image"
        assert data["total"] == 1

    def test_list_generations_filter_by_type_video(self, client, sample_generation, video_generation):
        """Should filter by video type."""
        response = client.get("/api/v1/studio/generations?type=video")

        assert response.status_code == 200
        data = response.json()
        assert len(data["generations"]) == 1
        assert data["generations"][0]["type"] == "video"
        assert data["total"] == 1

    def test_list_generations_filter_by_status(self, client, sample_generation, completed_generation):
        """Should filter by status."""
        response = client.get("/api/v1/studio/generations?status=completed")

        assert response.status_code == 200
        data = response.json()
        assert len(data["generations"]) == 1
        assert data["generations"][0]["status"] == "completed"
        assert data["total"] == 1

    def test_list_generations_combined_filters(self, client, sample_generation, completed_generation, video_generation):
        """Should apply multiple filters."""
        response = client.get("/api/v1/studio/generations?type=image&status=pending")

        assert response.status_code == 200
        data = response.json()
        assert len(data["generations"]) == 1
        assert data["generations"][0]["type"] == "image"
        assert data["generations"][0]["status"] == "pending"

    def test_list_generations_pagination_limit(self, client):
        """Should respect limit parameter."""
        # Create multiple generations
        for i in range(10):
            generations_db[f"gen-{i}"] = {
                "id": f"gen-{i}",
                "type": "image",
                "prompt": f"Prompt {i}",
                "status": "pending",
                "progress": 0.0,
                "provider": "google-gemini",
                "created_at": datetime.utcnow()
            }

        response = client.get("/api/v1/studio/generations?limit=5")

        assert response.status_code == 200
        data = response.json()
        assert len(data["generations"]) == 5
        assert data["total"] == 10

    def test_list_generations_pagination_offset(self, client):
        """Should respect offset parameter."""
        # Create multiple generations
        for i in range(10):
            generations_db[f"gen-{i}"] = {
                "id": f"gen-{i}",
                "type": "image",
                "prompt": f"Prompt {i}",
                "status": "pending",
                "progress": 0.0,
                "provider": "google-gemini",
                "created_at": datetime.utcnow()
            }

        response = client.get("/api/v1/studio/generations?limit=5&offset=5")

        assert response.status_code == 200
        data = response.json()
        assert len(data["generations"]) == 5
        assert data["total"] == 10

    def test_list_generations_sorted_by_date(self, client, sample_generation, completed_generation):
        """Should return generations sorted by date (newest first)."""
        response = client.get("/api/v1/studio/generations")

        assert response.status_code == 200
        data = response.json()
        # The second one created should be first (newer)
        dates = [g["created_at"] for g in data["generations"]]
        assert dates == sorted(dates, reverse=True)


# =============================================================================
# Test Get Generation Endpoint
# =============================================================================

class TestGetGeneration:
    """Test GET /generations/{gen_id} endpoint."""

    def test_get_generation_success(self, client, sample_generation):
        """Should return generation details."""
        response = client.get(f"/api/v1/studio/generations/{sample_generation['id']}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == sample_generation["id"]
        assert data["prompt"] == sample_generation["prompt"]
        assert data["type"] == "image"

    def test_get_generation_not_found(self, client):
        """Should return 404 for non-existent generation."""
        response = client.get("/api/v1/studio/generations/non-existent-id")

        assert response.status_code == 404
        assert "Generation not found" in response.json()["detail"]

    def test_get_generation_completed(self, client, completed_generation):
        """Should return completed generation with result URL."""
        response = client.get(f"/api/v1/studio/generations/{completed_generation['id']}")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "completed"
        assert data["progress"] == 100.0
        assert data["result_url"] is not None
        assert data["thumbnail_url"] is not None


# =============================================================================
# Test Delete Generation Endpoint
# =============================================================================

class TestDeleteGeneration:
    """Test DELETE /generations/{gen_id} endpoint."""

    def test_delete_generation_success(self, client, sample_generation):
        """Should delete generation successfully."""
        gen_id = sample_generation["id"]
        assert gen_id in generations_db

        response = client.delete(f"/api/v1/studio/generations/{gen_id}")

        assert response.status_code == 204
        assert gen_id not in generations_db

    def test_delete_generation_not_found(self, client):
        """Should return 404 for non-existent generation."""
        response = client.delete("/api/v1/studio/generations/non-existent-id")

        assert response.status_code == 404
        assert "Generation not found" in response.json()["detail"]


# =============================================================================
# Test List Assets Endpoint
# =============================================================================

class TestListAssets:
    """Test GET /assets endpoint."""

    def test_list_assets_empty(self, client):
        """Should return empty list when no assets."""
        response = client.get("/api/v1/studio/assets")

        assert response.status_code == 200
        data = response.json()
        assert data["assets"] == []
        assert data["total"] == 0

    def test_list_assets_with_data(self, client, sample_asset):
        """Should return all assets."""
        response = client.get("/api/v1/studio/assets")

        assert response.status_code == 200
        data = response.json()
        assert len(data["assets"]) == 1
        assert data["total"] == 1

    def test_list_assets_filter_by_type(self, client, sample_asset):
        """Should filter by type."""
        # Add a video asset
        video_asset_id = "video-asset-123"
        assets_db[video_asset_id] = {
            "id": video_asset_id,
            "type": "video",
            "url": "https://example.com/video.mp4",
            "prompt": "A video",
            "provider": "google-veo",
            "created_at": datetime.utcnow(),
            "tags": []
        }

        response = client.get("/api/v1/studio/assets?type=image")

        assert response.status_code == 200
        data = response.json()
        assert len(data["assets"]) == 1
        assert data["assets"][0]["type"] == "image"

    def test_list_assets_filter_by_tag(self, client, sample_asset):
        """Should filter by tag."""
        response = client.get("/api/v1/studio/assets?tag=nature")

        assert response.status_code == 200
        data = response.json()
        assert len(data["assets"]) == 1
        assert "nature" in data["assets"][0]["tags"]

    def test_list_assets_filter_by_nonexistent_tag(self, client, sample_asset):
        """Should return empty for non-existent tag."""
        response = client.get("/api/v1/studio/assets?tag=nonexistent")

        assert response.status_code == 200
        data = response.json()
        assert len(data["assets"]) == 0

    def test_list_assets_pagination(self, client):
        """Should paginate results."""
        # Create multiple assets
        for i in range(10):
            assets_db[f"asset-{i}"] = {
                "id": f"asset-{i}",
                "type": "image",
                "url": f"https://example.com/asset{i}.jpg",
                "prompt": f"Asset {i}",
                "provider": "google-gemini",
                "created_at": datetime.utcnow(),
                "tags": []
            }

        response = client.get("/api/v1/studio/assets?limit=5&offset=3")

        assert response.status_code == 200
        data = response.json()
        assert len(data["assets"]) == 5
        assert data["total"] == 10


# =============================================================================
# Test Save Asset Endpoint
# =============================================================================

class TestSaveAsset:
    """Test POST /assets endpoint."""

    def test_save_asset_success(self, client, completed_generation):
        """Should save completed generation as asset."""
        response = client.post(
            "/api/v1/studio/assets",
            json={
                "generation_id": completed_generation["id"],
                "tags": ["landscape", "mountain"]
            }
        )

        assert response.status_code == 201
        data = response.json()
        assert data["type"] == "image"
        assert data["url"] == completed_generation["result_url"]
        assert data["prompt"] == completed_generation["prompt"]
        assert data["tags"] == ["landscape", "mountain"]
        assert "id" in data

    def test_save_asset_without_tags(self, client, completed_generation):
        """Should save asset with empty tags."""
        response = client.post(
            "/api/v1/studio/assets",
            json={"generation_id": completed_generation["id"]}
        )

        assert response.status_code == 201
        data = response.json()
        assert data["tags"] == []

    def test_save_asset_generation_not_found(self, client):
        """Should return 404 for non-existent generation."""
        response = client.post(
            "/api/v1/studio/assets",
            json={"generation_id": "non-existent-id"}
        )

        assert response.status_code == 404
        assert "Generation not found" in response.json()["detail"]

    def test_save_asset_not_completed(self, client, sample_generation):
        """Should return 400 for non-completed generation."""
        response = client.post(
            "/api/v1/studio/assets",
            json={"generation_id": sample_generation["id"]}
        )

        assert response.status_code == 400
        assert "Must be 'completed'" in response.json()["detail"]

    def test_save_asset_no_result_url(self, client):
        """Should return 400 when generation has no result URL."""
        # Create a completed generation without result_url
        gen_id = "no-result-gen"
        generations_db[gen_id] = {
            "id": gen_id,
            "type": "image",
            "prompt": "Test",
            "status": "completed",
            "progress": 100.0,
            "result_url": None,
            "provider": "google-gemini",
            "created_at": datetime.utcnow()
        }

        response = client.post(
            "/api/v1/studio/assets",
            json={"generation_id": gen_id}
        )

        assert response.status_code == 400
        assert "no result URL" in response.json()["detail"]

    def test_save_asset_stores_metadata(self, client, completed_generation):
        """Should store generation metadata in asset."""
        response = client.post(
            "/api/v1/studio/assets",
            json={"generation_id": completed_generation["id"]}
        )

        assert response.status_code == 201
        data = response.json()
        assert data["metadata"]["generation_id"] == completed_generation["id"]


# =============================================================================
# Test Get Asset Endpoint
# =============================================================================

class TestGetAsset:
    """Test GET /assets/{asset_id} endpoint."""

    def test_get_asset_success(self, client, sample_asset):
        """Should return asset details."""
        response = client.get(f"/api/v1/studio/assets/{sample_asset['id']}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == sample_asset["id"]
        assert data["url"] == sample_asset["url"]
        assert data["tags"] == sample_asset["tags"]

    def test_get_asset_not_found(self, client):
        """Should return 404 for non-existent asset."""
        response = client.get("/api/v1/studio/assets/non-existent-id")

        assert response.status_code == 404
        assert "Asset not found" in response.json()["detail"]


# =============================================================================
# Test Delete Asset Endpoint
# =============================================================================

class TestDeleteAsset:
    """Test DELETE /assets/{asset_id} endpoint."""

    def test_delete_asset_success(self, client, sample_asset):
        """Should delete asset successfully."""
        asset_id = sample_asset["id"]
        assert asset_id in assets_db

        response = client.delete(f"/api/v1/studio/assets/{asset_id}")

        assert response.status_code == 204
        assert asset_id not in assets_db

    def test_delete_asset_not_found(self, client):
        """Should return 404 for non-existent asset."""
        response = client.delete("/api/v1/studio/assets/non-existent-id")

        assert response.status_code == 404
        assert "Asset not found" in response.json()["detail"]


# =============================================================================
# Test Update Asset Tags Endpoint
# =============================================================================

class TestUpdateAssetTags:
    """Test PATCH /assets/{asset_id}/tags endpoint."""

    def test_update_tags_success(self, client, sample_asset):
        """Should update asset tags."""
        response = client.patch(
            f"/api/v1/studio/assets/{sample_asset['id']}/tags",
            json=["new-tag", "another-tag"]
        )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["tags"] == ["new-tag", "another-tag"]
        assert assets_db[sample_asset["id"]]["tags"] == ["new-tag", "another-tag"]

    def test_update_tags_empty(self, client, sample_asset):
        """Should clear tags when empty list provided."""
        response = client.patch(
            f"/api/v1/studio/assets/{sample_asset['id']}/tags",
            json=[]
        )

        assert response.status_code == 200
        assert assets_db[sample_asset["id"]]["tags"] == []

    def test_update_tags_not_found(self, client):
        """Should return 404 for non-existent asset."""
        response = client.patch(
            "/api/v1/studio/assets/non-existent-id/tags",
            json=["tag"]
        )

        assert response.status_code == 404
        assert "Asset not found" in response.json()["detail"]


# =============================================================================
# Test List Providers Endpoint
# =============================================================================

class TestListProviders:
    """Test GET /providers endpoint."""

    def test_list_providers_all(self, client):
        """Should return all providers."""
        response = client.get("/api/v1/studio/providers")

        assert response.status_code == 200
        data = response.json()
        assert len(data) >= 5  # At least 5 providers configured
        provider_ids = [p["id"] for p in data]
        assert "google-gemini" in provider_ids
        assert "google-imagen" in provider_ids
        assert "openai-gpt-image" in provider_ids
        assert "google-veo" in provider_ids
        assert "openai-sora" in provider_ids

    def test_list_providers_filter_image(self, client):
        """Should filter by image type."""
        response = client.get("/api/v1/studio/providers?type=image")

        assert response.status_code == 200
        data = response.json()
        for provider in data:
            assert provider["type"] in ("image", "both")

    def test_list_providers_filter_video(self, client):
        """Should filter by video type."""
        response = client.get("/api/v1/studio/providers?type=video")

        assert response.status_code == 200
        data = response.json()
        for provider in data:
            assert provider["type"] in ("video", "both")

    def test_provider_has_required_fields(self, client):
        """Should return providers with all required fields."""
        response = client.get("/api/v1/studio/providers")

        assert response.status_code == 200
        data = response.json()
        for provider in data:
            assert "id" in provider
            assert "name" in provider
            assert "type" in provider
            assert "models" in provider
            assert "aspect_ratios" in provider
            assert "max_prompt_length" in provider
            assert "supports_negative_prompt" in provider
            assert "supports_style_presets" in provider

    def test_video_provider_has_durations(self, client):
        """Video providers should have duration info."""
        response = client.get("/api/v1/studio/providers?type=video")

        assert response.status_code == 200
        data = response.json()
        for provider in data:
            assert "durations" in provider
            assert "supports_image_to_video" in provider


# =============================================================================
# Test Get Provider Endpoint
# =============================================================================

class TestGetProvider:
    """Test GET /providers/{provider_id} endpoint."""

    def test_get_provider_success(self, client):
        """Should return provider details."""
        response = client.get("/api/v1/studio/providers/google-gemini")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "google-gemini"
        assert data["name"] == "Google Gemini"
        assert data["type"] == "image"

    def test_get_provider_video(self, client):
        """Should return video provider details."""
        response = client.get("/api/v1/studio/providers/google-veo")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "google-veo"
        assert data["type"] == "video"
        assert data["durations"] is not None
        assert data["supports_image_to_video"] is True

    def test_get_provider_not_found(self, client):
        """Should return 404 for non-existent provider."""
        response = client.get("/api/v1/studio/providers/non-existent")

        assert response.status_code == 404
        assert "Provider not found" in response.json()["detail"]


# =============================================================================
# Test Request/Response Models
# =============================================================================

class TestRequestModels:
    """Test Pydantic request models."""

    def test_image_generate_request_validation(self):
        """Should validate ImageGenerateRequest."""
        from app.api.studio import ImageGenerateRequest

        # Valid request
        request = ImageGenerateRequest(
            prompt="A sunset",
            provider="google-gemini",
            aspect_ratio="16:9"
        )
        assert request.prompt == "A sunset"

        # Prompt too long
        with pytest.raises(Exception):
            ImageGenerateRequest(prompt="x" * 4001)

    def test_video_generate_request_validation(self):
        """Should validate VideoGenerateRequest."""
        from app.api.studio import VideoGenerateRequest

        # Valid request
        request = VideoGenerateRequest(
            prompt="A flying bird",
            provider="google-veo",
            duration=8
        )
        assert request.duration == 8

        # Duration out of range
        with pytest.raises(Exception):
            VideoGenerateRequest(prompt="test", duration=3)

        with pytest.raises(Exception):
            VideoGenerateRequest(prompt="test", duration=17)

    def test_asset_create_request_defaults(self):
        """Should have proper defaults for AssetCreateRequest."""
        from app.api.studio import AssetCreateRequest

        request = AssetCreateRequest(generation_id="gen-123")
        assert request.generation_id == "gen-123"
        assert request.tags == []


class TestResponseModels:
    """Test Pydantic response models."""

    def test_generation_response_model(self):
        """Should create GenerationResponse correctly."""
        from app.api.studio import GenerationResponse

        response = GenerationResponse(
            id="gen-123",
            type="image",
            prompt="A sunset",
            status="pending",
            progress=0.0,
            provider="google-gemini",
            created_at=datetime.utcnow()
        )
        assert response.id == "gen-123"
        assert response.thumbnail_url is None
        assert response.error is None

    def test_asset_response_model(self):
        """Should create AssetResponse correctly."""
        from app.api.studio import AssetResponse

        response = AssetResponse(
            id="asset-123",
            type="image",
            url="https://example.com/image.jpg",
            prompt="A sunset",
            provider="google-gemini",
            created_at=datetime.utcnow()
        )
        assert response.tags == []
        assert response.thumbnail_url is None

    def test_provider_capability_model(self):
        """Should create ProviderCapability correctly."""
        from app.api.studio import ProviderCapability

        capability = ProviderCapability(
            id="test-provider",
            name="Test Provider",
            type="image",
            models=["model-1"],
            aspect_ratios=["1:1"],
            max_prompt_length=4000
        )
        assert capability.supports_negative_prompt is False
        assert capability.style_presets == []


# =============================================================================
# Test Authentication Requirements
# =============================================================================

class TestAuthenticationRequirements:
    """Test that endpoints have proper authentication."""

    def test_image_generate_requires_auth(self):
        """generate_image should use require_auth."""
        from app.api.studio import generate_image
        import inspect

        sig = inspect.signature(generate_image)
        params = sig.parameters
        assert "token" in params

    def test_video_generate_requires_auth(self):
        """generate_video should use require_auth."""
        from app.api.studio import generate_video
        import inspect

        sig = inspect.signature(generate_video)
        params = sig.parameters
        assert "token" in params

    def test_list_generations_requires_auth(self):
        """list_generations should use require_auth."""
        from app.api.studio import list_generations
        import inspect

        sig = inspect.signature(list_generations)
        params = sig.parameters
        assert "token" in params

    def test_list_assets_requires_auth(self):
        """list_assets should use require_auth."""
        from app.api.studio import list_assets
        import inspect

        sig = inspect.signature(list_assets)
        params = sig.parameters
        assert "token" in params

    def test_list_providers_requires_auth(self):
        """list_providers should use require_auth."""
        from app.api.studio import list_providers
        import inspect

        sig = inspect.signature(list_providers)
        params = sig.parameters
        assert "token" in params


# =============================================================================
# Test Helper Functions
# =============================================================================

class TestHelperFunctions:
    """Test internal helper functions."""

    def test_create_generation_response(self):
        """Should convert generation dict to response model."""
        from app.api.studio import _create_generation_response

        gen_data = {
            "id": "gen-123",
            "type": "image",
            "prompt": "Test prompt",
            "status": "completed",
            "progress": 100.0,
            "thumbnail_url": "https://example.com/thumb.jpg",
            "result_url": "https://example.com/result.jpg",
            "provider": "google-gemini",
            "created_at": datetime.utcnow(),
            "completed_at": datetime.utcnow(),
            "error": None,
            "metadata": {"aspect_ratio": "1:1"}
        }

        response = _create_generation_response(gen_data)
        assert response.id == "gen-123"
        assert response.status == "completed"
        assert response.result_url == "https://example.com/result.jpg"

    def test_create_generation_response_minimal(self):
        """Should handle minimal generation data."""
        from app.api.studio import _create_generation_response

        gen_data = {
            "id": "gen-123",
            "type": "image",
            "prompt": "Test",
            "status": "pending",
            "provider": "google-gemini",
            "created_at": datetime.utcnow()
        }

        response = _create_generation_response(gen_data)
        assert response.progress == 0.0
        assert response.thumbnail_url is None

    def test_create_asset_response(self):
        """Should convert asset dict to response model."""
        from app.api.studio import _create_asset_response

        asset_data = {
            "id": "asset-123",
            "type": "image",
            "url": "https://example.com/image.jpg",
            "thumbnail_url": "https://example.com/thumb.jpg",
            "prompt": "A sunset",
            "provider": "google-gemini",
            "created_at": datetime.utcnow(),
            "tags": ["nature"],
            "metadata": {"aspect_ratio": "16:9"}
        }

        response = _create_asset_response(asset_data)
        assert response.id == "asset-123"
        assert response.tags == ["nature"]

    def test_create_asset_response_minimal(self):
        """Should handle minimal asset data."""
        from app.api.studio import _create_asset_response

        asset_data = {
            "id": "asset-123",
            "type": "image",
            "url": "https://example.com/image.jpg",
            "prompt": "A sunset",
            "provider": "google-gemini",
            "created_at": datetime.utcnow()
        }

        response = _create_asset_response(asset_data)
        assert response.tags == []
        assert response.thumbnail_url is None


# =============================================================================
# Test Edge Cases
# =============================================================================

class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_prompt_max_length(self, client):
        """Should accept prompts up to max length."""
        # Max length is 4000
        long_prompt = "x" * 4000
        response = client.post(
            "/api/v1/studio/image/generate",
            json={"prompt": long_prompt}
        )
        assert response.status_code == 201

    def test_prompt_exceeds_max_length(self, client):
        """Should reject prompts exceeding max length."""
        too_long_prompt = "x" * 4001
        response = client.post(
            "/api/v1/studio/image/generate",
            json={"prompt": too_long_prompt}
        )
        assert response.status_code == 422

    def test_special_characters_in_prompt(self, client):
        """Should handle special characters in prompt."""
        response = client.post(
            "/api/v1/studio/image/generate",
            json={"prompt": "A sunset with emoji: sunset and 'quotes' and \"double quotes\""}
        )
        assert response.status_code == 201

    def test_unicode_in_prompt(self, client):
        """Should handle unicode in prompt."""
        response = client.post(
            "/api/v1/studio/image/generate",
            json={"prompt": "A beautiful picture"}
        )
        assert response.status_code == 201

    def test_pagination_beyond_data(self, client, sample_generation):
        """Should return empty when offset exceeds data."""
        response = client.get("/api/v1/studio/generations?offset=100")

        assert response.status_code == 200
        data = response.json()
        assert len(data["generations"]) == 0
        assert data["total"] == 1

    def test_limit_validation(self, client):
        """Should validate limit parameter bounds."""
        # Limit below minimum
        response = client.get("/api/v1/studio/generations?limit=0")
        assert response.status_code == 422

        # Limit above maximum
        response = client.get("/api/v1/studio/generations?limit=101")
        assert response.status_code == 422

    def test_offset_validation(self, client):
        """Should validate offset parameter."""
        response = client.get("/api/v1/studio/generations?offset=-1")
        assert response.status_code == 422

    def test_multiple_assets_same_generation(self, client, completed_generation):
        """Should allow saving same generation multiple times."""
        response1 = client.post(
            "/api/v1/studio/assets",
            json={"generation_id": completed_generation["id"], "tags": ["first"]}
        )
        assert response1.status_code == 201

        response2 = client.post(
            "/api/v1/studio/assets",
            json={"generation_id": completed_generation["id"], "tags": ["second"]}
        )
        assert response2.status_code == 201

        # Both assets should exist with different IDs
        assert response1.json()["id"] != response2.json()["id"]

    def test_concurrent_generations(self, client):
        """Should handle multiple generations being created."""
        gen_ids = []
        for i in range(5):
            response = client.post(
                "/api/v1/studio/image/generate",
                json={"prompt": f"Concurrent test {i}"}
            )
            assert response.status_code == 201
            gen_ids.append(response.json()["id"])

        # All IDs should be unique
        assert len(set(gen_ids)) == 5

        # All should be in database
        for gen_id in gen_ids:
            assert gen_id in generations_db


# =============================================================================
# Test Provider Configuration
# =============================================================================

class TestProviderConfiguration:
    """Test provider configuration constants."""

    def test_providers_dict_structure(self):
        """Should have proper structure for all providers."""
        from app.api.studio import PROVIDERS

        required_fields = ["id", "name", "type", "models", "aspect_ratios", "max_prompt_length"]

        for provider_id, config in PROVIDERS.items():
            for field in required_fields:
                assert field in config, f"Provider {provider_id} missing {field}"
            assert config["id"] == provider_id

    def test_image_providers_config(self):
        """Image providers should have correct configuration."""
        from app.api.studio import PROVIDERS

        image_providers = ["google-gemini", "google-imagen", "openai-gpt-image"]
        for provider_id in image_providers:
            provider = PROVIDERS[provider_id]
            assert provider["type"] == "image"
            assert len(provider["models"]) > 0
            assert len(provider["aspect_ratios"]) > 0

    def test_video_providers_config(self):
        """Video providers should have correct configuration."""
        from app.api.studio import PROVIDERS

        video_providers = ["google-veo", "openai-sora"]
        for provider_id in video_providers:
            provider = PROVIDERS[provider_id]
            assert provider["type"] == "video"
            assert "durations" in provider
            assert "supports_image_to_video" in provider

    def test_google_veo_supports_image_to_video(self):
        """Google Veo should support image-to-video."""
        from app.api.studio import PROVIDERS

        assert PROVIDERS["google-veo"]["supports_image_to_video"] is True

    def test_openai_sora_does_not_support_image_to_video(self):
        """OpenAI Sora should not support image-to-video."""
        from app.api.studio import PROVIDERS

        assert PROVIDERS["openai-sora"]["supports_image_to_video"] is False
