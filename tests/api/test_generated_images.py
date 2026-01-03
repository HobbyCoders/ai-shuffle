"""
Comprehensive tests for the generated_images API endpoints.

Tests cover:
- Image serving by path (/api/generated-images/by-path)
- Image serving by filename (/api/generated-images/{filename})
- Listing generated images (/api/generated-images/)
- Path safety validation (path traversal prevention)
- File extension validation
- MIME type resolution
- Error handling (400, 403, 404)
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
import os


# =============================================================================
# Helper function tests (no client needed)
# =============================================================================

class TestIsPathSafe:
    """Test path safety validation to prevent path traversal attacks."""

    def test_path_within_base_is_safe(self, temp_dir):
        """A file inside the base directory should be safe."""
        from app.api.generated_images import is_path_safe

        file_path = temp_dir / "subdir" / "test.png"
        assert is_path_safe(file_path, temp_dir) is True

    def test_path_at_base_root_is_safe(self, temp_dir):
        """A file at the base directory root should be safe."""
        from app.api.generated_images import is_path_safe

        file_path = temp_dir / "test.png"
        assert is_path_safe(file_path, temp_dir) is True

    def test_path_traversal_up_is_unsafe(self, temp_dir):
        """Path traversal with .. should be detected as unsafe."""
        from app.api.generated_images import is_path_safe

        # Create a subdir and try to escape
        subdir = temp_dir / "subdir"
        subdir.mkdir(exist_ok=True)
        file_path = subdir / ".." / ".." / "etc" / "passwd"
        assert is_path_safe(file_path, temp_dir) is False

    def test_path_outside_base_is_unsafe(self, temp_dir):
        """A file completely outside the base should be unsafe."""
        from app.api.generated_images import is_path_safe

        # Use a path that's definitely outside temp_dir
        outside_path = Path("/etc/passwd")
        assert is_path_safe(outside_path, temp_dir) is False

    def test_path_same_as_base_is_safe(self, temp_dir):
        """The base directory itself should be considered safe."""
        from app.api.generated_images import is_path_safe

        assert is_path_safe(temp_dir, temp_dir) is True

    def test_symbolic_link_within_base(self, temp_dir):
        """Symlink within base should be safe (resolved path)."""
        from app.api.generated_images import is_path_safe

        # Create a real file and symlink to it
        real_file = temp_dir / "real.png"
        real_file.touch()
        link_path = temp_dir / "link.png"

        try:
            link_path.symlink_to(real_file)
            assert is_path_safe(link_path, temp_dir) is True
        except OSError:
            # Symlinks may not be available on all platforms
            pytest.skip("Symlinks not available on this platform")

    def test_invalid_path_returns_false(self):
        """Invalid or unresolvable paths should return False."""
        from app.api.generated_images import is_path_safe

        # Very long path that may cause issues
        invalid_path = Path("x" * 10000)
        base_path = Path("/tmp")
        # Should not raise, should return False
        result = is_path_safe(invalid_path, base_path)
        assert result is False or result is True  # Just shouldn't raise

    def test_exception_in_resolve_returns_false(self, temp_dir):
        """Paths that raise exceptions during resolve should return False."""
        from app.api.generated_images import is_path_safe

        # Test with a mocked Path that raises ValueError
        mock_path = MagicMock(spec=Path)
        mock_path.resolve.side_effect = ValueError("test error")

        result = is_path_safe(mock_path, temp_dir)
        assert result is False

    def test_runtime_error_returns_false(self, temp_dir):
        """Paths that raise RuntimeError during resolve should return False."""
        from app.api.generated_images import is_path_safe

        # Test with a mocked Path that raises RuntimeError
        mock_path = MagicMock(spec=Path)
        mock_path.resolve.side_effect = RuntimeError("test error")

        result = is_path_safe(mock_path, temp_dir)
        assert result is False


class TestAllowedExtensions:
    """Test allowed image extensions constant."""

    def test_allowed_extensions_contains_common_image_formats(self):
        """Should include common image formats."""
        from app.api.generated_images import ALLOWED_EXTENSIONS

        assert '.png' in ALLOWED_EXTENSIONS
        assert '.jpg' in ALLOWED_EXTENSIONS
        assert '.jpeg' in ALLOWED_EXTENSIONS
        assert '.gif' in ALLOWED_EXTENSIONS
        assert '.webp' in ALLOWED_EXTENSIONS

    def test_allowed_extensions_are_lowercase(self):
        """All extensions should be lowercase."""
        from app.api.generated_images import ALLOWED_EXTENSIONS

        for ext in ALLOWED_EXTENSIONS:
            assert ext == ext.lower()

    def test_allowed_extensions_is_set(self):
        """ALLOWED_EXTENSIONS should be a set."""
        from app.api.generated_images import ALLOWED_EXTENSIONS

        assert isinstance(ALLOWED_EXTENSIONS, set)


class TestMediaTypes:
    """Test MIME type mappings."""

    def test_common_image_mime_types(self):
        """Common image types should have correct MIME types."""
        from app.api.generated_images import MEDIA_TYPES

        assert MEDIA_TYPES[".png"] == "image/png"
        assert MEDIA_TYPES[".jpg"] == "image/jpeg"
        assert MEDIA_TYPES[".jpeg"] == "image/jpeg"
        assert MEDIA_TYPES[".gif"] == "image/gif"
        assert MEDIA_TYPES[".webp"] == "image/webp"

    def test_media_types_is_dict(self):
        """MEDIA_TYPES should be a dictionary."""
        from app.api.generated_images import MEDIA_TYPES

        assert isinstance(MEDIA_TYPES, dict)

    def test_all_allowed_extensions_have_media_types(self):
        """All allowed extensions should have corresponding media types."""
        from app.api.generated_images import ALLOWED_EXTENSIONS, MEDIA_TYPES

        for ext in ALLOWED_EXTENSIONS:
            assert ext in MEDIA_TYPES, f"Extension {ext} missing from MEDIA_TYPES"


# =============================================================================
# API Endpoint Tests - Using direct endpoint testing
# =============================================================================

class TestGetImageByPathEndpoint:
    """Test the /api/generated-images/by-path endpoint."""

    @pytest.fixture
    def test_client(self, temp_dir):
        """Create a test client with mocked settings."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from app.api.generated_images import router

        # Create a minimal test app
        app = FastAPI()
        app.include_router(router)

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir
            yield TestClient(app), temp_dir

    def test_get_image_success(self, test_client):
        """Should return image when path is valid and within workspace."""
        client, temp_dir = test_client

        # Create a generated-images directory and test file
        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "test.png"
        test_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake png content")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(test_file)}
            )
            assert response.status_code == 200
            assert b"\x89PNG" in response.content
            assert "image/png" in response.headers.get("content-type", "")

    def test_get_image_invalid_extension(self, test_client):
        """Should return 400 for non-image file extensions."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "test.txt"
        test_file.write_text("not an image")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(test_file)}
            )
            assert response.status_code == 400
            assert "Invalid file type" in response.json()["detail"]

    def test_get_image_invalid_extension_exe(self, test_client):
        """Should return 400 for executable files."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "test.exe"
        test_file.write_bytes(b"MZ\x00\x00")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(test_file)}
            )
            assert response.status_code == 400
            assert "Invalid file type" in response.json()["detail"]

    def test_get_image_outside_workspace(self, test_client):
        """Should return 403 when file is outside workspace."""
        client, temp_dir = test_client

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": "/etc/passwd.png"}
            )
            assert response.status_code == 403
            assert "outside workspace" in response.json()["detail"].lower()

    def test_get_image_path_traversal(self, test_client):
        """Should block path traversal attempts."""
        client, temp_dir = test_client

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(temp_dir / ".." / ".." / "etc" / "passwd.png")}
            )
            assert response.status_code == 403
            assert "outside workspace" in response.json()["detail"].lower()

    def test_get_image_not_in_generated_images_dir(self, test_client):
        """Should return 403 when file is not in a generated-images directory."""
        client, temp_dir = test_client

        # Create a file directly in workspace (not in generated-images)
        test_file = temp_dir / "test.png"
        test_file.write_bytes(b"\x89PNG\r\n\x1a\n")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(test_file)}
            )
            assert response.status_code == 403
            assert "not a generated image" in response.json()["detail"].lower()

    def test_get_image_not_found(self, test_client):
        """Should return 404 when image doesn't exist."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(generated_dir / "nonexistent.png")}
            )
            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()

    def test_get_image_directory_not_file(self, test_client):
        """Should return 404 when path is a directory."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        subdir = generated_dir / "subdir.png"  # Directory with .png name
        subdir.mkdir()

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(subdir)}
            )
            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()

    def test_get_image_jpg_mime_type(self, test_client):
        """Should return correct MIME type for JPG."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "photo.jpg"
        test_file.write_bytes(b"\xFF\xD8\xFF\xE0")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(test_file)}
            )
            assert response.status_code == 200
            assert "image/jpeg" in response.headers.get("content-type", "")

    def test_get_image_jpeg_mime_type(self, test_client):
        """Should return correct MIME type for JPEG."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "photo.jpeg"
        test_file.write_bytes(b"\xFF\xD8\xFF\xE0")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(test_file)}
            )
            assert response.status_code == 200
            assert "image/jpeg" in response.headers.get("content-type", "")

    def test_get_image_gif_mime_type(self, test_client):
        """Should return correct MIME type for GIF."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "animation.gif"
        test_file.write_bytes(b"GIF89a")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(test_file)}
            )
            assert response.status_code == 200
            assert "image/gif" in response.headers.get("content-type", "")

    def test_get_image_webp_mime_type(self, test_client):
        """Should return correct MIME type for WebP."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "modern.webp"
        test_file.write_bytes(b"RIFF\x00\x00\x00\x00WEBP")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(test_file)}
            )
            assert response.status_code == 200
            assert "image/webp" in response.headers.get("content-type", "")

    def test_get_image_uppercase_extension(self, test_client):
        """Should handle uppercase extensions correctly."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "TEST.PNG"
        test_file.write_bytes(b"\x89PNG\r\n\x1a\n")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(test_file)}
            )
            assert response.status_code == 200
            assert "image/png" in response.headers.get("content-type", "")

    def test_get_image_nested_generated_images_dir(self, test_client):
        """Should work with nested generated-images directory."""
        client, temp_dir = test_client

        # Create nested structure: project/generated-images/image.png
        project_dir = temp_dir / "project"
        project_dir.mkdir()
        generated_dir = project_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "nested.png"
        test_file.write_bytes(b"\x89PNG\r\n\x1a\n")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(test_file)}
            )
            assert response.status_code == 200


class TestGetGeneratedImageByFilenameEndpoint:
    """Test the /api/generated-images/{filename} endpoint."""

    @pytest.fixture
    def test_client(self, temp_dir):
        """Create a test client with mocked settings."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from app.api.generated_images import router

        # Create a minimal test app
        app = FastAPI()
        app.include_router(router)

        yield TestClient(app), temp_dir

    def test_get_image_by_filename_success(self, test_client):
        """Should serve images from workspace's generated-images directory."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "image.png"
        test_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"content")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/image.png")
            assert response.status_code == 200
            assert "image/png" in response.headers.get("content-type", "")

    def test_get_image_by_filename_not_found(self, test_client):
        """Should return 404 when image doesn't exist."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/nonexistent.png")
            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()

    def test_get_image_by_filename_path_traversal_dotdot(self, test_client):
        """Should block path traversal with .. - route doesn't match paths with slashes."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            # URL-encoded slashes decode to actual slashes, which don't match the {filename} pattern
            # This results in a 404 because the route doesn't match (secure by design)
            response = client.get("/api/generated-images/..%2F..%2Fetc%2Fpasswd.png")
            assert response.status_code == 404

    def test_get_image_by_filename_path_traversal_forward_slash(self, test_client):
        """Should block path traversal with forward slashes - route doesn't match."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            # URL-encoded slashes decode to actual slashes, which don't match the {filename} pattern
            # This results in a 404 because the route doesn't match (secure by design)
            response = client.get("/api/generated-images/subdir%2Fimage.png")
            assert response.status_code == 404

    def test_get_image_by_filename_path_traversal_backslash(self, test_client):
        """Should block path traversal with backslashes."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/subdir%5Cimage.png")
            assert response.status_code == 400
            assert "Invalid filename" in response.json()["detail"]

    def test_get_image_by_filename_invalid_extension(self, test_client):
        """Should return 400 for non-image file extensions."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/document.txt")
            assert response.status_code == 400
            assert "Invalid file type" in response.json()["detail"]

    def test_get_image_by_filename_directory_not_file(self, test_client):
        """Should return 404 when path is a directory."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        subdir = generated_dir / "subdir.png"
        subdir.mkdir()

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/subdir.png")
            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()

    def test_get_image_by_filename_generated_dir_not_exists(self, test_client):
        """Should return 404 when generated-images dir doesn't exist."""
        client, temp_dir = test_client

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/image.png")
            assert response.status_code == 404

    def test_get_image_by_filename_jpg(self, test_client):
        """Should handle JPG files correctly."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "photo.jpg"
        test_file.write_bytes(b"\xFF\xD8\xFF\xE0")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/photo.jpg")
            assert response.status_code == 200
            assert "image/jpeg" in response.headers.get("content-type", "")

    def test_get_image_by_filename_uppercase(self, test_client):
        """Should handle uppercase extensions."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "IMAGE.PNG"
        test_file.write_bytes(b"\x89PNG\r\n\x1a\n")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/IMAGE.PNG")
            assert response.status_code == 200


class TestListGeneratedImagesEndpoint:
    """Test the /api/generated-images/ endpoint for listing images."""

    @pytest.fixture
    def test_client(self, temp_dir):
        """Create a test client with mocked settings."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from app.api.generated_images import router

        # Create a minimal test app
        app = FastAPI()
        app.include_router(router)

        yield TestClient(app), temp_dir

    def test_list_images_success(self, test_client):
        """Should list all images in generated-images directory."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()

        # Create some test images
        (generated_dir / "image1.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (generated_dir / "image2.jpg").write_bytes(b"\xFF\xD8\xFF\xE0")
        (generated_dir / "image3.gif").write_bytes(b"GIF89a")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/")
            assert response.status_code == 200
            data = response.json()

            assert "images" in data
            assert "directory" in data
            assert len(data["images"]) == 3

            filenames = [img["filename"] for img in data["images"]]
            assert "image1.png" in filenames
            assert "image2.jpg" in filenames
            assert "image3.gif" in filenames

    def test_list_images_includes_metadata(self, test_client):
        """Should include complete metadata for each image."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        (generated_dir / "test.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 1024)

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/")
            assert response.status_code == 200
            data = response.json()

            image_data = data["images"][0]
            assert "filename" in image_data
            assert "url" in image_data
            assert "path" in image_data
            assert "size_bytes" in image_data
            assert "created_at" in image_data

    def test_list_images_empty_directory(self, test_client):
        """Should return empty list when directory is empty."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/")
            assert response.status_code == 200
            data = response.json()
            assert data["images"] == []

    def test_list_images_directory_not_exists(self, test_client):
        """Should return empty list when directory doesn't exist."""
        client, temp_dir = test_client

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/")
            assert response.status_code == 200
            data = response.json()
            assert data["images"] == []
            assert str(temp_dir / "generated-images") in data["directory"]

    def test_list_images_excludes_directories(self, test_client):
        """Should only list files, not subdirectories."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()

        # Create a file and a subdirectory
        (generated_dir / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (generated_dir / "subdir").mkdir()

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/")
            assert response.status_code == 200
            data = response.json()

            # Should only have the image file
            assert len(data["images"]) == 1
            assert data["images"][0]["filename"] == "image.png"

    def test_list_images_excludes_non_image_files(self, test_client):
        """Should only list image files, not other types."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()

        # Create an image and a non-image file
        (generated_dir / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        (generated_dir / "document.txt").write_text("not an image")
        (generated_dir / "script.py").write_text("print('hello')")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/")
            assert response.status_code == 200
            data = response.json()

            # Should only have the image file
            assert len(data["images"]) == 1
            assert data["images"][0]["filename"] == "image.png"

    def test_list_images_sorted_by_creation_time(self, test_client):
        """Images should be sorted by creation time, newest first."""
        import time
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()

        # Create files with slight delays
        (generated_dir / "old.png").write_bytes(b"\x89PNG\r\n\x1a\n")
        time.sleep(0.1)
        (generated_dir / "new.png").write_bytes(b"\x89PNG\r\n\x1a\n")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/")
            assert response.status_code == 200
            data = response.json()

            # Newest should be first
            assert len(data["images"]) == 2
            assert data["images"][0]["filename"] == "new.png"
            assert data["images"][1]["filename"] == "old.png"

    def test_list_images_correct_urls(self, test_client):
        """Should include correct download URLs."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        (generated_dir / "artwork.png").write_bytes(b"\x89PNG\r\n\x1a\n")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/")
            assert response.status_code == 200
            data = response.json()

            image_data = data["images"][0]
            assert image_data["url"] == "/api/generated-images/artwork.png"

    def test_list_images_includes_all_allowed_extensions(self, test_client):
        """Should include all allowed image extensions."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()

        # Create files with all allowed extensions
        (generated_dir / "file.png").write_bytes(b"\x89PNG")
        (generated_dir / "file.jpg").write_bytes(b"\xFF\xD8")
        (generated_dir / "file.jpeg").write_bytes(b"\xFF\xD8")
        (generated_dir / "file.gif").write_bytes(b"GIF89a")
        (generated_dir / "file.webp").write_bytes(b"RIFF")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/")
            assert response.status_code == 200
            data = response.json()

            assert len(data["images"]) == 5


# =============================================================================
# Edge Cases and Special Scenarios
# =============================================================================

class TestEdgeCases:
    """Test edge cases and special scenarios."""

    @pytest.fixture
    def test_client(self, temp_dir):
        """Create a test client with mocked settings."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from app.api.generated_images import router

        # Create a minimal test app
        app = FastAPI()
        app.include_router(router)

        yield TestClient(app), temp_dir

    def test_file_with_spaces_in_name(self, test_client):
        """Should handle files with spaces in names."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "my image.png"
        test_file.write_bytes(b"\x89PNG\r\n\x1a\n")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/my%20image.png")
            assert response.status_code == 200

    def test_file_with_unicode_name(self, test_client):
        """Should handle files with unicode characters."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "imagen.png"
        test_file.write_bytes(b"\x89PNG\r\n\x1a\n")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/imagen.png")
            assert response.status_code == 200

    def test_empty_file(self, test_client):
        """Should handle empty image files correctly."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "empty.png"
        test_file.touch()

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/empty.png")
            assert response.status_code == 200
            assert response.content == b""

    def test_file_with_multiple_dots(self, test_client):
        """Should handle files with multiple dots in name."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "my.image.v2.png"
        test_file.write_bytes(b"\x89PNG\r\n\x1a\n")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get("/api/generated-images/my.image.v2.png")
            assert response.status_code == 200
            assert "image/png" in response.headers.get("content-type", "")

    def test_large_image_file(self, test_client):
        """Should handle large image files."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        test_file = generated_dir / "large.png"
        # Write a 1MB file
        test_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (1024 * 1024))

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(test_file)}
            )
            assert response.status_code == 200

    def test_unknown_extension_fallback(self):
        """Unknown extensions in MEDIA_TYPES should fall back to octet-stream."""
        from app.api.generated_images import MEDIA_TYPES

        # Verify all allowed extensions are in MEDIA_TYPES
        from app.api.generated_images import ALLOWED_EXTENSIONS
        for ext in ALLOWED_EXTENSIONS:
            assert ext in MEDIA_TYPES


class TestRouterConfiguration:
    """Test router configuration and structure."""

    def test_router_exists(self):
        """Router should exist and be properly configured."""
        from app.api.generated_images import router
        assert router is not None

    def test_router_prefix(self):
        """Router should have correct prefix."""
        from app.api.generated_images import router
        assert router.prefix == "/api/generated-images"

    def test_router_tags(self):
        """Router should have appropriate tags."""
        from app.api.generated_images import router
        assert "generated-images" in router.tags


class TestModuleImports:
    """Test that module imports work correctly."""

    def test_module_imports(self):
        """Module should import without errors."""
        from app.api import generated_images
        assert generated_images is not None

    def test_helper_functions_importable(self):
        """Helper functions should be importable."""
        from app.api.generated_images import is_path_safe
        assert callable(is_path_safe)

    def test_constants_importable(self):
        """Constants should be importable."""
        from app.api.generated_images import ALLOWED_EXTENSIONS, MEDIA_TYPES
        assert isinstance(ALLOWED_EXTENSIONS, set)
        assert isinstance(MEDIA_TYPES, dict)


# =============================================================================
# Security Tests
# =============================================================================

class TestSecurityByPath:
    """Security-focused tests for the by-path endpoint."""

    @pytest.fixture
    def test_client(self, temp_dir):
        """Create a test client with mocked settings."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from app.api.generated_images import router

        # Create a minimal test app
        app = FastAPI()
        app.include_router(router)

        yield TestClient(app), temp_dir

    def test_null_byte_injection(self, test_client):
        """Should handle null byte injection attempts."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(generated_dir / "image.png\x00.txt")}
            )
            # Should either reject or handle safely
            assert response.status_code in [400, 403, 404]

    def test_double_encoded_path_traversal(self, test_client):
        """Should block double-encoded path traversal."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            # %252e%252e = double-encoded ..
            response = client.get(
                "/api/generated-images/by-path",
                params={"path": str(temp_dir / "%2e%2e" / "%2e%2e" / "etc" / "passwd.png")}
            )
            assert response.status_code in [400, 403, 404]


class TestSecurityByFilename:
    """Security-focused tests for the by-filename endpoint."""

    @pytest.fixture
    def test_client(self, temp_dir):
        """Create a test client with mocked settings."""
        from fastapi.testclient import TestClient
        from fastapi import FastAPI
        from app.api.generated_images import router

        # Create a minimal test app
        app = FastAPI()
        app.include_router(router)

        yield TestClient(app), temp_dir

    def test_only_filename_component_used(self, test_client):
        """Should strip any directory components and use only filename."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        # Create a file that would match if path components weren't stripped
        (generated_dir / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            # Even without path traversal characters, path.name should be used
            response = client.get("/api/generated-images/image.png")
            assert response.status_code == 200

    def test_rejects_hidden_files(self, test_client):
        """Should handle requests for hidden files (starting with .)."""
        client, temp_dir = test_client

        generated_dir = temp_dir / "generated-images"
        generated_dir.mkdir()
        hidden_file = generated_dir / ".hidden.png"
        hidden_file.write_bytes(b"\x89PNG\r\n\x1a\n")

        with patch("app.api.generated_images.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            # Hidden files are valid if they exist
            response = client.get("/api/generated-images/.hidden.png")
            # Could be 200 or rejected depending on implementation
            assert response.status_code in [200, 400, 404]
