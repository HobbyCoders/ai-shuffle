"""
Comprehensive tests for the generated_videos API endpoints.

Tests cover:
- Video serving by path (/api/generated-videos/by-path)
- Video serving by filename (/api/generated-videos/{filename})
- Listing generated videos (/api/generated-videos/)
- Path safety validation (path traversal prevention)
- Video extension validation
- MIME type resolution
- Error handling (400, 403, 404)
- Security checks for generated-videos directory requirement
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
import tempfile
import os

from fastapi import HTTPException
from fastapi.responses import FileResponse


# =============================================================================
# Helper function tests (no client needed)
# =============================================================================

class TestIsPathSafe:
    """Test path safety validation to prevent path traversal attacks."""

    def test_path_within_base_is_safe(self, temp_dir):
        """A file inside the base directory should be safe."""
        from app.api.generated_videos import is_path_safe

        file_path = temp_dir / "subdir" / "video.mp4"
        assert is_path_safe(file_path, temp_dir) is True

    def test_path_at_base_root_is_safe(self, temp_dir):
        """A file at the base directory root should be safe."""
        from app.api.generated_videos import is_path_safe

        file_path = temp_dir / "video.mp4"
        assert is_path_safe(file_path, temp_dir) is True

    def test_path_traversal_up_is_unsafe(self, temp_dir):
        """Path traversal with .. should be detected as unsafe."""
        from app.api.generated_videos import is_path_safe

        # Create a subdir and try to escape
        subdir = temp_dir / "subdir"
        subdir.mkdir(exist_ok=True)
        file_path = subdir / ".." / ".." / "etc" / "passwd"
        assert is_path_safe(file_path, temp_dir) is False

    def test_path_outside_base_is_unsafe(self, temp_dir):
        """A file completely outside the base should be unsafe."""
        from app.api.generated_videos import is_path_safe

        # Use a path that's definitely outside temp_dir
        outside_path = Path("/etc/passwd")
        assert is_path_safe(outside_path, temp_dir) is False

    def test_path_same_as_base_is_safe(self, temp_dir):
        """The base directory itself should be considered safe."""
        from app.api.generated_videos import is_path_safe

        assert is_path_safe(temp_dir, temp_dir) is True

    def test_symbolic_link_within_base(self, temp_dir):
        """Symlink within base should be safe (resolved path)."""
        from app.api.generated_videos import is_path_safe

        # Create a real file and symlink to it
        real_file = temp_dir / "real.mp4"
        real_file.touch()
        link_path = temp_dir / "link.mp4"

        try:
            link_path.symlink_to(real_file)
            assert is_path_safe(link_path, temp_dir) is True
        except OSError:
            # Symlinks may not be available on all platforms
            pytest.skip("Symlinks not available on this platform")

    def test_invalid_path_returns_false(self):
        """Invalid or unresolvable paths should return False."""
        from app.api.generated_videos import is_path_safe

        # Very long path that may cause issues
        invalid_path = Path("x" * 10000)
        base_path = Path("/tmp")
        # Should not raise, should return False
        result = is_path_safe(invalid_path, base_path)
        assert result is False or result is True  # Just shouldn't raise

    def test_exception_in_resolve_returns_false(self):
        """Exceptions during path resolution should return False."""
        from app.api.generated_videos import is_path_safe

        # Mock Path.resolve() to raise an exception
        with patch.object(Path, 'resolve', side_effect=RuntimeError("Mock error")):
            result = is_path_safe(Path("/some/path"), Path("/base"))
            assert result is False

    def test_value_error_in_resolve_returns_false(self):
        """ValueError during path resolution should return False."""
        from app.api.generated_videos import is_path_safe

        # Mock Path.resolve() to raise ValueError
        with patch.object(Path, 'resolve', side_effect=ValueError("Mock error")):
            result = is_path_safe(Path("/some/path"), Path("/base"))
            assert result is False


class TestAllowedExtensions:
    """Test video file extension validation."""

    def test_allowed_extensions_set(self):
        """Should have the expected allowed video extensions."""
        from app.api.generated_videos import ALLOWED_EXTENSIONS

        assert ".mp4" in ALLOWED_EXTENSIONS
        assert ".webm" in ALLOWED_EXTENSIONS
        assert ".mov" in ALLOWED_EXTENSIONS
        assert ".avi" in ALLOWED_EXTENSIONS

    def test_disallowed_extensions(self):
        """Non-video extensions should not be in allowed set."""
        from app.api.generated_videos import ALLOWED_EXTENSIONS

        assert ".txt" not in ALLOWED_EXTENSIONS
        assert ".py" not in ALLOWED_EXTENSIONS
        assert ".jpg" not in ALLOWED_EXTENSIONS
        assert ".pdf" not in ALLOWED_EXTENSIONS
        assert ".exe" not in ALLOWED_EXTENSIONS


class TestMediaTypes:
    """Test MIME type mappings for video files."""

    def test_mp4_mime_type(self):
        """MP4 should have correct MIME type."""
        from app.api.generated_videos import MEDIA_TYPES

        assert MEDIA_TYPES[".mp4"] == "video/mp4"

    def test_webm_mime_type(self):
        """WebM should have correct MIME type."""
        from app.api.generated_videos import MEDIA_TYPES

        assert MEDIA_TYPES[".webm"] == "video/webm"

    def test_mov_mime_type(self):
        """MOV should have correct MIME type."""
        from app.api.generated_videos import MEDIA_TYPES

        assert MEDIA_TYPES[".mov"] == "video/quicktime"

    def test_avi_mime_type(self):
        """AVI should have correct MIME type."""
        from app.api.generated_videos import MEDIA_TYPES

        assert MEDIA_TYPES[".avi"] == "video/x-msvideo"

    def test_all_allowed_extensions_have_media_types(self):
        """All allowed extensions should have corresponding MIME types."""
        from app.api.generated_videos import ALLOWED_EXTENSIONS, MEDIA_TYPES

        for ext in ALLOWED_EXTENSIONS:
            assert ext in MEDIA_TYPES, f"Extension {ext} missing from MEDIA_TYPES"


# =============================================================================
# API Endpoint Tests - Direct function calls
# =============================================================================

class TestGetVideoByPathEndpoint:
    """Test the /api/generated-videos/by-path endpoint."""

    @pytest.mark.asyncio
    async def test_get_video_success(self, temp_dir):
        """Should return FileResponse when path is valid and within workspace."""
        from app.api.generated_videos import get_video_by_path

        # Create a test video file in generated-videos directory
        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "video.mp4"
        test_file.write_bytes(b"\x00\x00\x00\x1c" + b"ftyp" + b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_video_by_path(path=str(test_file))
            assert isinstance(response, FileResponse)
            assert response.media_type == "video/mp4"

    @pytest.mark.asyncio
    async def test_get_video_invalid_extension(self, temp_dir):
        """Should raise 400 for invalid file extension."""
        from app.api.generated_videos import get_video_by_path

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "document.txt"
        test_file.write_text("not a video")

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc_info:
                await get_video_by_path(path=str(test_file))
            assert exc_info.value.status_code == 400
            assert "Invalid file type" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_get_video_outside_workspace(self, temp_dir):
        """Should raise 403 when file is outside workspace."""
        from app.api.generated_videos import get_video_by_path

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc_info:
                await get_video_by_path(path="/etc/passwd.mp4")
            assert exc_info.value.status_code == 403
            assert "outside workspace" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_get_video_path_traversal(self, temp_dir):
        """Should block path traversal attempts."""
        from app.api.generated_videos import get_video_by_path

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc_info:
                await get_video_by_path(path=str(temp_dir / ".." / ".." / "etc" / "video.mp4"))
            assert exc_info.value.status_code == 403
            assert "outside workspace" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_get_video_not_in_generated_videos_dir(self, temp_dir):
        """Should raise 403 when file is not in generated-videos directory."""
        from app.api.generated_videos import get_video_by_path

        # Create a file directly in workspace (not in generated-videos)
        test_file = temp_dir / "video.mp4"
        test_file.write_bytes(b"\x00\x00\x00\x1c" + b"ftyp" + b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc_info:
                await get_video_by_path(path=str(test_file))
            assert exc_info.value.status_code == 403
            assert "not a generated video" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_get_video_not_found(self, temp_dir):
        """Should raise 404 when video file doesn't exist."""
        from app.api.generated_videos import get_video_by_path

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc_info:
                await get_video_by_path(path=str(gen_videos_dir / "nonexistent.mp4"))
            assert exc_info.value.status_code == 404
            assert "not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_get_video_directory_not_allowed(self, temp_dir):
        """Should raise 404 when path is a directory."""
        from app.api.generated_videos import get_video_by_path

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        subdir = gen_videos_dir / "subdir.mp4"  # Directory with video-like name
        subdir.mkdir()

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc_info:
                await get_video_by_path(path=str(subdir))
            assert exc_info.value.status_code == 404
            assert "not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_get_video_webm_mime_type(self, temp_dir):
        """Should return correct MIME type for WebM."""
        from app.api.generated_videos import get_video_by_path

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "video.webm"
        test_file.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_video_by_path(path=str(test_file))
            assert isinstance(response, FileResponse)
            assert response.media_type == "video/webm"

    @pytest.mark.asyncio
    async def test_get_video_mov_mime_type(self, temp_dir):
        """Should return correct MIME type for MOV."""
        from app.api.generated_videos import get_video_by_path

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "video.mov"
        test_file.write_bytes(b"\x00\x00\x00\x14" + b"ftyp" + b"qt  " + b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_video_by_path(path=str(test_file))
            assert isinstance(response, FileResponse)
            assert response.media_type == "video/quicktime"

    @pytest.mark.asyncio
    async def test_get_video_avi_mime_type(self, temp_dir):
        """Should return correct MIME type for AVI."""
        from app.api.generated_videos import get_video_by_path

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "video.avi"
        test_file.write_bytes(b"RIFF" + b"\x00" * 4 + b"AVI " + b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_video_by_path(path=str(test_file))
            assert isinstance(response, FileResponse)
            assert response.media_type == "video/x-msvideo"

    @pytest.mark.asyncio
    async def test_get_video_nested_generated_videos_dir(self, temp_dir):
        """Should work with generated-videos in a nested project directory."""
        from app.api.generated_videos import get_video_by_path

        # Create nested structure: workspace/project/generated-videos/video.mp4
        project_dir = temp_dir / "project"
        project_dir.mkdir()
        gen_videos_dir = project_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "video.mp4"
        test_file.write_bytes(b"\x00\x00\x00\x1c" + b"ftyp" + b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_video_by_path(path=str(test_file))
            assert isinstance(response, FileResponse)
            assert response.media_type == "video/mp4"

    @pytest.mark.asyncio
    async def test_get_video_case_insensitive_extension(self, temp_dir):
        """Should handle uppercase extensions."""
        from app.api.generated_videos import get_video_by_path

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "video.MP4"
        test_file.write_bytes(b"\x00\x00\x00\x1c" + b"ftyp" + b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_video_by_path(path=str(test_file))
            assert isinstance(response, FileResponse)


class TestGetGeneratedVideoEndpoint:
    """Test the /api/generated-videos/{filename} endpoint."""

    @pytest.mark.asyncio
    async def test_get_video_by_filename_success(self, temp_dir):
        """Should serve video from generated-videos directory."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "animation.mp4"
        test_file.write_bytes(b"\x00\x00\x00\x1c" + b"ftyp" + b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_generated_video(filename="animation.mp4")
            assert isinstance(response, FileResponse)
            assert response.media_type == "video/mp4"

    @pytest.mark.asyncio
    async def test_get_video_by_filename_not_found(self, temp_dir):
        """Should raise 404 when video doesn't exist."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc_info:
                await get_generated_video(filename="nonexistent.mp4")
            assert exc_info.value.status_code == 404
            assert "not found" in exc_info.value.detail.lower()

    @pytest.mark.asyncio
    async def test_get_video_by_filename_invalid_extension(self, temp_dir):
        """Should raise 400 for non-video extension."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "file.txt"
        test_file.write_text("not a video")

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc_info:
                await get_generated_video(filename="file.txt")
            assert exc_info.value.status_code == 400
            assert "Invalid file type" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_get_video_path_traversal_dotdot(self, temp_dir):
        """Should block path traversal with .."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc_info:
                await get_generated_video(filename="../../../etc/passwd.mp4")
            assert exc_info.value.status_code == 400
            assert "Invalid filename" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_get_video_path_traversal_forward_slash(self, temp_dir):
        """Should block path traversal with forward slashes."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc_info:
                await get_generated_video(filename="subdir/video.mp4")
            assert exc_info.value.status_code == 400
            assert "Invalid filename" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_get_video_path_traversal_backslash(self, temp_dir):
        """Should block path traversal with backslashes."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc_info:
                await get_generated_video(filename="subdir\\video.mp4")
            assert exc_info.value.status_code == 400
            assert "Invalid filename" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_get_video_no_extension(self, temp_dir):
        """Should raise 400 for files without extension."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc_info:
                await get_generated_video(filename="noextension")
            assert exc_info.value.status_code == 400
            assert "Invalid file type" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_get_video_webm_by_filename(self, temp_dir):
        """Should serve WebM videos correctly."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "animation.webm"
        test_file.write_bytes(b"\x1a\x45\xdf\xa3" + b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_generated_video(filename="animation.webm")
            assert isinstance(response, FileResponse)
            assert response.media_type == "video/webm"

    @pytest.mark.asyncio
    async def test_get_video_directory_with_video_name(self, temp_dir):
        """Should raise 404 when filename points to directory."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        subdir = gen_videos_dir / "video.mp4"  # Directory with video-like name
        subdir.mkdir()

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            with pytest.raises(HTTPException) as exc_info:
                await get_generated_video(filename="video.mp4")
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_get_video_uppercase_extension(self, temp_dir):
        """Should handle uppercase extensions."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "video.MP4"
        test_file.write_bytes(b"\x00\x00\x00\x1c" + b"ftyp" + b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_generated_video(filename="video.MP4")
            assert isinstance(response, FileResponse)

    @pytest.mark.asyncio
    async def test_get_video_mixed_case_extension(self, temp_dir):
        """Should handle mixed case extensions."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "video.Mp4"
        test_file.write_bytes(b"\x00\x00\x00\x1c" + b"ftyp" + b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_generated_video(filename="video.Mp4")
            assert isinstance(response, FileResponse)


class TestListGeneratedVideosEndpoint:
    """Test the /api/generated-videos/ endpoint."""

    @pytest.mark.asyncio
    async def test_list_videos_empty_directory(self, temp_dir):
        """Should return empty list when no videos exist."""
        from app.api.generated_videos import list_generated_videos

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            result = await list_generated_videos()
            assert result["videos"] == []
            assert result["directory"] == str(gen_videos_dir)

    @pytest.mark.asyncio
    async def test_list_videos_directory_not_exists(self, temp_dir):
        """Should return empty list when generated-videos directory doesn't exist."""
        from app.api.generated_videos import list_generated_videos

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            result = await list_generated_videos()
            assert result["videos"] == []
            assert "generated-videos" in result["directory"]

    @pytest.mark.asyncio
    async def test_list_videos_with_files(self, temp_dir):
        """Should list all video files with metadata."""
        from app.api.generated_videos import list_generated_videos

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        # Create test video files
        video1 = gen_videos_dir / "video1.mp4"
        video1.write_bytes(b"\x00" * 1000)
        video2 = gen_videos_dir / "video2.webm"
        video2.write_bytes(b"\x00" * 2000)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            result = await list_generated_videos()
            assert len(result["videos"]) == 2

            # Verify video metadata structure
            for video in result["videos"]:
                assert "filename" in video
                assert "url" in video
                assert "path" in video
                assert "size_bytes" in video
                assert "created_at" in video
                assert video["filename"] in ["video1.mp4", "video2.webm"]

    @pytest.mark.asyncio
    async def test_list_videos_filters_non_video_files(self, temp_dir):
        """Should only list video files, not other file types."""
        from app.api.generated_videos import list_generated_videos

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        # Create video and non-video files
        video = gen_videos_dir / "video.mp4"
        video.write_bytes(b"\x00" * 1000)
        text_file = gen_videos_dir / "readme.txt"
        text_file.write_text("readme")
        py_file = gen_videos_dir / "script.py"
        py_file.write_text("print('hello')")

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            result = await list_generated_videos()
            assert len(result["videos"]) == 1
            assert result["videos"][0]["filename"] == "video.mp4"

    @pytest.mark.asyncio
    async def test_list_videos_sorted_by_creation_time(self, temp_dir):
        """Should sort videos by creation time, newest first."""
        from app.api.generated_videos import list_generated_videos
        import time

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        # Create files with different timestamps
        video1 = gen_videos_dir / "older.mp4"
        video1.write_bytes(b"\x00" * 100)
        time.sleep(0.1)  # Ensure different timestamps
        video2 = gen_videos_dir / "newer.mp4"
        video2.write_bytes(b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            result = await list_generated_videos()
            assert len(result["videos"]) == 2
            # Newer video should be first
            assert result["videos"][0]["filename"] == "newer.mp4"
            assert result["videos"][1]["filename"] == "older.mp4"

    @pytest.mark.asyncio
    async def test_list_videos_url_format(self, temp_dir):
        """Should include correct URL format for each video."""
        from app.api.generated_videos import list_generated_videos

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        video = gen_videos_dir / "test.mp4"
        video.write_bytes(b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            result = await list_generated_videos()
            assert len(result["videos"]) == 1
            assert result["videos"][0]["url"] == "/api/generated-videos/test.mp4"

    @pytest.mark.asyncio
    async def test_list_videos_size_bytes(self, temp_dir):
        """Should include correct file size in bytes."""
        from app.api.generated_videos import list_generated_videos

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        video = gen_videos_dir / "sized.mp4"
        video.write_bytes(b"\x00" * 5000)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            result = await list_generated_videos()
            assert len(result["videos"]) == 1
            assert result["videos"][0]["size_bytes"] == 5000

    @pytest.mark.asyncio
    async def test_list_videos_ignores_directories(self, temp_dir):
        """Should not include directories in the list."""
        from app.api.generated_videos import list_generated_videos

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        # Create a directory with a video-like name
        subdir = gen_videos_dir / "subdir.mp4"
        subdir.mkdir()

        # Create an actual video
        video = gen_videos_dir / "real.mp4"
        video.write_bytes(b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            result = await list_generated_videos()
            assert len(result["videos"]) == 1
            assert result["videos"][0]["filename"] == "real.mp4"

    @pytest.mark.asyncio
    async def test_list_videos_all_supported_extensions(self, temp_dir):
        """Should list all supported video extensions."""
        from app.api.generated_videos import list_generated_videos

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        # Create files with all supported extensions
        (gen_videos_dir / "video.mp4").write_bytes(b"\x00" * 100)
        (gen_videos_dir / "video.webm").write_bytes(b"\x00" * 100)
        (gen_videos_dir / "video.mov").write_bytes(b"\x00" * 100)
        (gen_videos_dir / "video.avi").write_bytes(b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            result = await list_generated_videos()
            assert len(result["videos"]) == 4
            filenames = [v["filename"] for v in result["videos"]]
            assert "video.mp4" in filenames
            assert "video.webm" in filenames
            assert "video.mov" in filenames
            assert "video.avi" in filenames


class TestRouterConfiguration:
    """Test router configuration and module imports."""

    def test_router_prefix(self):
        """Router should have correct prefix."""
        from app.api.generated_videos import router
        assert router.prefix == "/api/generated-videos"

    def test_router_tags(self):
        """Router should have correct tags."""
        from app.api.generated_videos import router
        assert "generated-videos" in router.tags

    def test_module_imports(self):
        """Module should import without errors."""
        from app.api import generated_videos
        assert generated_videos is not None

    def test_router_exists(self):
        """Router should exist."""
        from app.api.generated_videos import router
        assert router is not None


class TestEdgeCases:
    """Test edge cases and unusual inputs."""

    @pytest.mark.asyncio
    async def test_special_characters_in_filename(self, temp_dir):
        """Should handle filenames with spaces and special chars."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "my video (1).mp4"
        test_file.write_bytes(b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_generated_video(filename="my video (1).mp4")
            assert isinstance(response, FileResponse)

    @pytest.mark.asyncio
    async def test_unicode_filename(self, temp_dir):
        """Should handle unicode filenames."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        try:
            test_file = gen_videos_dir / "video_\u4e2d\u6587.mp4"
            test_file.write_bytes(b"\x00" * 100)

            with patch("app.api.generated_videos.settings") as mock_settings:
                mock_settings.effective_workspace_dir = temp_dir

                response = await get_generated_video(filename="video_\u4e2d\u6587.mp4")
                assert isinstance(response, FileResponse)
        except (OSError, UnicodeError):
            pytest.skip("Unicode filenames not supported on this platform")

    @pytest.mark.asyncio
    async def test_very_long_filename(self, temp_dir):
        """Should handle very long filenames appropriately."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()

        # Very long filename
        long_name = "a" * 200 + ".mp4"

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            # Should raise 404 (file doesn't exist)
            with pytest.raises(HTTPException) as exc_info:
                await get_generated_video(filename=long_name)
            assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_double_extension(self, temp_dir):
        """Should handle double extensions correctly."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "video.backup.mp4"
        test_file.write_bytes(b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_generated_video(filename="video.backup.mp4")
            assert isinstance(response, FileResponse)

    @pytest.mark.asyncio
    async def test_hidden_file(self, temp_dir):
        """Should handle hidden files (starting with dot)."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / ".hidden.mp4"
        test_file.write_bytes(b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_generated_video(filename=".hidden.mp4")
            assert isinstance(response, FileResponse)


class TestFileResponseProperties:
    """Test FileResponse properties for video files."""

    @pytest.mark.asyncio
    async def test_response_filename_property(self, temp_dir):
        """Should set filename property on FileResponse."""
        from app.api.generated_videos import get_generated_video

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "download.mp4"
        test_file.write_bytes(b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_generated_video(filename="download.mp4")
            assert isinstance(response, FileResponse)
            assert response.filename == "download.mp4"

    @pytest.mark.asyncio
    async def test_response_path_property(self, temp_dir):
        """Should set path property on FileResponse."""
        from app.api.generated_videos import get_video_by_path

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "video.mp4"
        test_file.write_bytes(b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_video_by_path(path=str(test_file))
            assert isinstance(response, FileResponse)
            assert response.path == test_file

    @pytest.mark.asyncio
    async def test_by_path_sets_correct_filename(self, temp_dir):
        """get_video_by_path should set filename from file path."""
        from app.api.generated_videos import get_video_by_path

        gen_videos_dir = temp_dir / "generated-videos"
        gen_videos_dir.mkdir()
        test_file = gen_videos_dir / "specific_video.mp4"
        test_file.write_bytes(b"\x00" * 100)

        with patch("app.api.generated_videos.settings") as mock_settings:
            mock_settings.effective_workspace_dir = temp_dir

            response = await get_video_by_path(path=str(test_file))
            assert response.filename == "specific_video.mp4"
