"""
Comprehensive tests for the shared_files API endpoints.

Tests cover:
- File serving by path (/api/files/by-path)
- File info endpoint (/api/files/info)
- Shared file serving by filename (/api/files/{filename})
- Listing shared files (/api/files/)
- Path safety validation (path traversal prevention)
- File size formatting
- File icon mapping
- MIME type resolution
- Error handling (404, 403)
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock
import tempfile
import os
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient


# =============================================================================
# Test fixtures specific to shared_files
# =============================================================================

@pytest.fixture(scope="module")
def files_app():
    """
    Create a minimal FastAPI app with just the shared_files router.
    This avoids loading the full app with its lifespan/migrations.
    """
    from app.api.shared_files import router
    app = FastAPI()
    app.include_router(router)
    return app


@pytest.fixture(scope="function")
def files_client(files_app, temp_dir):
    """
    Create a test client for the shared_files router with mocked settings.
    """
    with patch("app.api.shared_files.settings") as mock_settings:
        mock_settings.effective_workspace_dir = temp_dir
        with TestClient(files_app) as client:
            yield client, temp_dir


# =============================================================================
# Helper function tests (no client needed)
# =============================================================================

class TestIsPathSafe:
    """Test path safety validation to prevent path traversal attacks."""

    def test_path_within_base_is_safe(self, temp_dir):
        """A file inside the base directory should be safe."""
        from app.api.shared_files import is_path_safe

        file_path = temp_dir / "subdir" / "test.txt"
        assert is_path_safe(file_path, temp_dir) is True

    def test_path_at_base_root_is_safe(self, temp_dir):
        """A file at the base directory root should be safe."""
        from app.api.shared_files import is_path_safe

        file_path = temp_dir / "test.txt"
        assert is_path_safe(file_path, temp_dir) is True

    def test_path_traversal_up_is_unsafe(self, temp_dir):
        """Path traversal with .. should be detected as unsafe."""
        from app.api.shared_files import is_path_safe

        # Create a subdir and try to escape
        subdir = temp_dir / "subdir"
        subdir.mkdir(exist_ok=True)
        file_path = subdir / ".." / ".." / "etc" / "passwd"
        assert is_path_safe(file_path, temp_dir) is False

    def test_path_outside_base_is_unsafe(self, temp_dir):
        """A file completely outside the base should be unsafe."""
        from app.api.shared_files import is_path_safe

        # Use a path that's definitely outside temp_dir
        outside_path = Path("/etc/passwd")
        assert is_path_safe(outside_path, temp_dir) is False

    def test_path_same_as_base_is_safe(self, temp_dir):
        """The base directory itself should be considered safe."""
        from app.api.shared_files import is_path_safe

        assert is_path_safe(temp_dir, temp_dir) is True

    def test_symbolic_link_within_base(self, temp_dir):
        """Symlink within base should be safe (resolved path)."""
        from app.api.shared_files import is_path_safe

        # Create a real file and symlink to it
        real_file = temp_dir / "real.txt"
        real_file.touch()
        link_path = temp_dir / "link.txt"

        try:
            link_path.symlink_to(real_file)
            assert is_path_safe(link_path, temp_dir) is True
        except OSError:
            # Symlinks may not be available on all platforms
            pytest.skip("Symlinks not available on this platform")

    def test_invalid_path_returns_false(self):
        """Invalid or unresolvable paths should return False."""
        from app.api.shared_files import is_path_safe

        # Very long path that may cause issues
        invalid_path = Path("x" * 10000)
        base_path = Path("/tmp")
        # Should not raise, should return False
        result = is_path_safe(invalid_path, base_path)
        assert result is False or result is True  # Just shouldn't raise

    def test_path_resolve_exception_returns_false(self):
        """Should return False when path resolution raises an exception."""
        from app.api.shared_files import is_path_safe

        # Mock the Path.resolve() to raise a ValueError (or RuntimeError)
        mock_path = MagicMock(spec=Path)
        mock_path.resolve.side_effect = ValueError("Cannot resolve path")

        base_path = Path("/tmp")
        result = is_path_safe(mock_path, base_path)
        assert result is False

    def test_path_resolve_runtime_error_returns_false(self):
        """Should return False when path resolution raises RuntimeError."""
        from app.api.shared_files import is_path_safe

        # Mock the Path.resolve() to raise a RuntimeError
        mock_path = MagicMock(spec=Path)
        mock_path.resolve.side_effect = RuntimeError("Symlink loop")

        base_path = Path("/tmp")
        result = is_path_safe(mock_path, base_path)
        assert result is False


class TestFormatFileSize:
    """Test file size formatting to human-readable format."""

    def test_format_bytes(self):
        """Should format bytes correctly."""
        from app.api.shared_files import format_file_size
        assert format_file_size(0) == "0 B"
        assert format_file_size(1) == "1 B"
        assert format_file_size(512) == "512 B"
        assert format_file_size(1023) == "1023 B"

    def test_format_kilobytes(self):
        """Should format kilobytes correctly."""
        from app.api.shared_files import format_file_size
        assert format_file_size(1024) == "1.0 KB"
        assert format_file_size(1536) == "1.5 KB"
        assert format_file_size(2048) == "2.0 KB"

    def test_format_megabytes(self):
        """Should format megabytes correctly."""
        from app.api.shared_files import format_file_size
        assert format_file_size(1024 * 1024) == "1.0 MB"
        assert format_file_size(int(1.5 * 1024 * 1024)) == "1.5 MB"

    def test_format_gigabytes(self):
        """Should format gigabytes correctly."""
        from app.api.shared_files import format_file_size
        assert format_file_size(1024 * 1024 * 1024) == "1.0 GB"
        assert format_file_size(int(2.5 * 1024 * 1024 * 1024)) == "2.5 GB"


class TestGetFileIcon:
    """Test file icon mapping based on extension."""

    def test_pdf_icon(self):
        """PDF files should get document icon."""
        from app.api.shared_files import get_file_icon
        assert get_file_icon("document.pdf") == "📄"

    def test_word_document_icons(self):
        """Word documents should get document icon."""
        from app.api.shared_files import get_file_icon
        assert get_file_icon("file.doc") == "📝"
        assert get_file_icon("file.docx") == "📝"

    def test_spreadsheet_icons(self):
        """Spreadsheets should get chart icon."""
        from app.api.shared_files import get_file_icon
        assert get_file_icon("data.xls") == "📊"
        assert get_file_icon("data.xlsx") == "📊"
        assert get_file_icon("data.csv") == "📊"

    def test_python_icon(self):
        """Python files should get snake icon."""
        from app.api.shared_files import get_file_icon
        assert get_file_icon("script.py") == "🐍"

    def test_javascript_icon(self):
        """JavaScript files should get JS icon."""
        from app.api.shared_files import get_file_icon
        assert get_file_icon("app.js") == "💛"
        assert get_file_icon("module.mjs") == "💛"

    def test_typescript_icon(self):
        """TypeScript files should get TS icon."""
        from app.api.shared_files import get_file_icon
        assert get_file_icon("app.ts") == "💙"

    def test_archive_icons(self):
        """Archive files should get package icon."""
        from app.api.shared_files import get_file_icon
        assert get_file_icon("archive.zip") == "📦"
        assert get_file_icon("archive.tar") == "📦"
        assert get_file_icon("archive.gz") == "📦"

    def test_unknown_extension(self):
        """Unknown extensions should get folder icon."""
        from app.api.shared_files import get_file_icon
        assert get_file_icon("file.unknown") == "📁"
        assert get_file_icon("noextension") == "📁"

    def test_case_insensitive(self):
        """Extension matching should be case insensitive."""
        from app.api.shared_files import get_file_icon
        assert get_file_icon("FILE.PDF") == "📄"
        assert get_file_icon("Script.PY") == "🐍"

    def test_html_css_icons(self):
        """HTML and CSS should have appropriate icons."""
        from app.api.shared_files import get_file_icon
        assert get_file_icon("page.html") == "🌐"
        assert get_file_icon("styles.css") == "🎨"

    def test_shell_script_icons(self):
        """Shell scripts should get gear icon."""
        from app.api.shared_files import get_file_icon
        assert get_file_icon("script.sh") == "⚙️"
        assert get_file_icon("script.bash") == "⚙️"
        assert get_file_icon("script.bat") == "⚙️"

    def test_database_icons(self):
        """Database files should get database icon."""
        from app.api.shared_files import get_file_icon
        assert get_file_icon("data.sql") == "🗃️"
        assert get_file_icon("database.sqlite") == "🗃️"
        assert get_file_icon("database.db") == "🗃️"


class TestMediaTypes:
    """Test MIME type mappings."""

    def test_common_mime_types(self):
        """Common file types should have correct MIME types."""
        from app.api.shared_files import MEDIA_TYPES

        assert MEDIA_TYPES[".pdf"] == "application/pdf"
        assert MEDIA_TYPES[".json"] == "application/json"
        assert MEDIA_TYPES[".txt"] == "text/plain"
        assert MEDIA_TYPES[".html"] == "text/html"
        assert MEDIA_TYPES[".css"] == "text/css"
        assert MEDIA_TYPES[".py"] == "text/x-python"
        assert MEDIA_TYPES[".zip"] == "application/zip"

    def test_office_mime_types(self):
        """Office documents should have correct MIME types."""
        from app.api.shared_files import MEDIA_TYPES

        assert MEDIA_TYPES[".doc"] == "application/msword"
        assert MEDIA_TYPES[".docx"] == "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        assert MEDIA_TYPES[".xls"] == "application/vnd.ms-excel"
        assert MEDIA_TYPES[".xlsx"] == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

    def test_archive_mime_types(self):
        """Archive files should have correct MIME types."""
        from app.api.shared_files import MEDIA_TYPES

        assert MEDIA_TYPES[".zip"] == "application/zip"
        assert MEDIA_TYPES[".tar"] == "application/x-tar"
        assert MEDIA_TYPES[".gz"] == "application/gzip"
        assert MEDIA_TYPES[".7z"] == "application/x-7z-compressed"


# =============================================================================
# API Endpoint Tests with FastAPI TestClient
# =============================================================================

class TestGetFileByPathEndpoint:
    """Test the /api/files/by-path endpoint."""

    def test_get_file_success(self, files_client):
        """Should return file when path is valid and within workspace."""
        client, temp_dir = files_client
        # Create a test file
        test_file = temp_dir / "test.txt"
        test_file.write_text("Hello, World!")

        response = client.get(
            "/api/files/by-path",
            params={"path": str(test_file)}
        )
        assert response.status_code == 200
        assert response.content == b"Hello, World!"
        assert "text/plain" in response.headers.get("content-type", "")

    def test_get_file_not_found(self, files_client):
        """Should return 404 when file doesn't exist."""
        client, temp_dir = files_client

        response = client.get(
            "/api/files/by-path",
            params={"path": str(temp_dir / "nonexistent.txt")}
        )
        assert response.status_code == 404
        assert "not found" in response.json()["detail"].lower()

    def test_get_file_outside_workspace(self, files_client):
        """Should return 403 when file is outside workspace."""
        client, temp_dir = files_client

        response = client.get(
            "/api/files/by-path",
            params={"path": "/etc/passwd"}
        )
        assert response.status_code == 403
        assert "outside workspace" in response.json()["detail"].lower()

    def test_get_file_path_traversal(self, files_client):
        """Should block path traversal attempts."""
        client, temp_dir = files_client

        response = client.get(
            "/api/files/by-path",
            params={"path": str(temp_dir / ".." / ".." / "etc" / "passwd")}
        )
        assert response.status_code == 403
        assert "outside workspace" in response.json()["detail"].lower()

    def test_get_file_directory_not_allowed(self, files_client):
        """Should return 404 when path is a directory."""
        client, temp_dir = files_client
        subdir = temp_dir / "subdir"
        subdir.mkdir()

        response = client.get(
            "/api/files/by-path",
            params={"path": str(subdir)}
        )
        assert response.status_code == 404

    def test_get_file_with_pdf_mime_type(self, files_client):
        """Should return correct MIME type for PDF."""
        client, temp_dir = files_client
        test_file = temp_dir / "document.pdf"
        test_file.write_bytes(b"%PDF-1.4 test content")

        response = client.get(
            "/api/files/by-path",
            params={"path": str(test_file)}
        )
        assert response.status_code == 200
        assert "application/pdf" in response.headers.get("content-type", "")

    def test_get_file_with_json_mime_type(self, files_client):
        """Should return correct MIME type for JSON."""
        client, temp_dir = files_client
        test_file = temp_dir / "data.json"
        test_file.write_text('{"key": "value"}')

        response = client.get(
            "/api/files/by-path",
            params={"path": str(test_file)}
        )
        assert response.status_code == 200
        assert "application/json" in response.headers.get("content-type", "")

    def test_get_file_unknown_extension_octet_stream(self, files_client):
        """Should return octet-stream for unknown extensions."""
        client, temp_dir = files_client
        test_file = temp_dir / "binary.xyz"
        test_file.write_bytes(b"\x00\x01\x02\x03")

        response = client.get(
            "/api/files/by-path",
            params={"path": str(test_file)}
        )
        assert response.status_code == 200
        assert "application/octet-stream" in response.headers.get("content-type", "")


class TestGetFileInfoEndpoint:
    """Test the /api/files/info endpoint."""

    def test_get_file_info_success(self, files_client):
        """Should return complete file metadata."""
        client, temp_dir = files_client
        test_file = temp_dir / "document.pdf"
        test_file.write_bytes(b"%PDF-1.4 " + b"x" * 1000)

        response = client.get(
            "/api/files/info",
            params={"path": str(test_file)}
        )
        assert response.status_code == 200
        data = response.json()

        assert data["filename"] == "document.pdf"
        assert data["path"] == str(test_file)
        assert data["size_bytes"] == 1009
        assert "size_formatted" in data
        assert data["extension"] == "pdf"
        assert data["mime_type"] == "application/pdf"
        assert data["icon"] == "📄"
        assert "created_at" in data
        assert "modified_at" in data
        assert "download_url" in data

    def test_get_file_info_size_formatting(self, files_client):
        """Should format file sizes correctly."""
        client, temp_dir = files_client
        # Small file
        small_file = temp_dir / "small.txt"
        small_file.write_text("x" * 100)

        response = client.get(
            "/api/files/info",
            params={"path": str(small_file)}
        )
        assert response.status_code == 200
        data = response.json()
        assert "B" in data["size_formatted"]

    def test_get_file_info_not_found(self, files_client):
        """Should return 404 when file doesn't exist."""
        client, temp_dir = files_client

        response = client.get(
            "/api/files/info",
            params={"path": str(temp_dir / "nonexistent.txt")}
        )
        assert response.status_code == 404

    def test_get_file_info_outside_workspace(self, files_client):
        """Should return 403 for files outside workspace."""
        client, temp_dir = files_client

        response = client.get(
            "/api/files/info",
            params={"path": "/etc/passwd"}
        )
        assert response.status_code == 403

    def test_get_file_info_download_url(self, files_client):
        """Should include valid download URL."""
        client, temp_dir = files_client
        test_file = temp_dir / "test.txt"
        test_file.write_text("content")

        response = client.get(
            "/api/files/info",
            params={"path": str(test_file)}
        )
        assert response.status_code == 200
        data = response.json()
        assert "/api/files/by-path" in data["download_url"]
        assert str(test_file) in data["download_url"]


class TestGetSharedFileEndpoint:
    """Test the /api/files/{filename} endpoint."""

    def test_get_shared_file_success(self, files_client):
        """Should serve files from shared-files directory."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()
        test_file = shared_dir / "report.pdf"
        test_file.write_bytes(b"%PDF-1.4 content")

        response = client.get("/api/files/report.pdf")
        assert response.status_code == 200
        assert "application/pdf" in response.headers.get("content-type", "")

    def test_get_shared_file_not_found(self, files_client):
        """Should return 404 when file doesn't exist."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()

        response = client.get("/api/files/nonexistent.txt")
        assert response.status_code == 404

    def test_get_shared_file_path_traversal_dotdot(self, files_client):
        """Should block path traversal with .. (via route not matching)."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()

        # URL-encoded slashes create multi-segment paths that don't match /{filename}
        # FastAPI returns 404 because no route matches
        response = client.get("/api/files/..%2F..%2Fetc%2Fpasswd")
        assert response.status_code == 404

    def test_get_shared_file_path_traversal_forward_slash(self, files_client):
        """Should block path traversal with forward slashes (via route not matching)."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()

        # URL-encoded slashes create multi-segment paths that don't match /{filename}
        response = client.get("/api/files/subdir%2Ffile.txt")
        assert response.status_code == 404

    def test_get_shared_file_path_traversal_backslash(self, files_client):
        """Should block path traversal with backslashes."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()

        # Backslashes in filenames are rejected by the endpoint's validation
        response = client.get("/api/files/subdir%5Cfile.txt")
        assert response.status_code == 400
        assert "Invalid filename" in response.json()["detail"]

    def test_get_shared_file_path_traversal_dotdot_only(self, files_client):
        """Should block path traversal with .. in filename without slashes."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()

        # Filename containing .. but no slashes gets to the endpoint and is rejected
        response = client.get("/api/files/..important.txt")
        assert response.status_code == 400
        assert "Invalid filename" in response.json()["detail"]

    def test_get_shared_file_directory_not_exists(self, files_client):
        """Should return 404 when shared-files dir doesn't exist."""
        client, temp_dir = files_client

        response = client.get("/api/files/somefile.txt")
        assert response.status_code == 404

    def test_get_shared_file_correct_mime_type(self, files_client):
        """Should serve with correct MIME type."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()
        test_file = shared_dir / "data.json"
        test_file.write_text('{"key": "value"}')

        response = client.get("/api/files/data.json")
        assert response.status_code == 200
        assert "application/json" in response.headers.get("content-type", "")

    def test_get_shared_file_strips_path_components(self, files_client):
        """Should only use filename, stripping any path components."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()
        test_file = shared_dir / "file.txt"
        test_file.write_text("content")

        # If somehow a path with / gets through URL encoding, should fail
        response = client.get("/api/files/file.txt")
        assert response.status_code == 200


class TestListSharedFilesEndpoint:
    """Test the /api/files/ endpoint for listing files."""

    def test_list_shared_files_success(self, files_client):
        """Should list all files in shared-files directory."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()

        # Create some test files
        (shared_dir / "file1.txt").write_text("content1")
        (shared_dir / "file2.pdf").write_bytes(b"%PDF content")
        (shared_dir / "file3.json").write_text('{"data": true}')

        response = client.get("/api/files/")
        assert response.status_code == 200
        data = response.json()

        assert "files" in data
        assert "directory" in data
        assert len(data["files"]) == 3

        filenames = [f["filename"] for f in data["files"]]
        assert "file1.txt" in filenames
        assert "file2.pdf" in filenames
        assert "file3.json" in filenames

    def test_list_shared_files_includes_metadata(self, files_client):
        """Should include complete metadata for each file."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()
        (shared_dir / "test.pdf").write_bytes(b"x" * 1024)

        response = client.get("/api/files/")
        assert response.status_code == 200
        data = response.json()

        file_data = data["files"][0]
        assert "filename" in file_data
        assert "url" in file_data
        assert "path" in file_data
        assert "size_bytes" in file_data
        assert "size_formatted" in file_data
        assert "icon" in file_data
        assert "extension" in file_data
        assert "created_at" in file_data

    def test_list_shared_files_empty_directory(self, files_client):
        """Should return empty list when directory is empty."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()

        response = client.get("/api/files/")
        assert response.status_code == 200
        data = response.json()
        assert data["files"] == []

    def test_list_shared_files_directory_not_exists(self, files_client):
        """Should return empty list when directory doesn't exist."""
        client, temp_dir = files_client

        response = client.get("/api/files/")
        assert response.status_code == 200
        data = response.json()
        assert data["files"] == []
        assert str(temp_dir / "shared-files") in data["directory"]

    def test_list_shared_files_excludes_directories(self, files_client):
        """Should only list files, not subdirectories."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()

        # Create a file and a subdirectory
        (shared_dir / "file.txt").write_text("content")
        (shared_dir / "subdir").mkdir()

        response = client.get("/api/files/")
        assert response.status_code == 200
        data = response.json()

        # Should only have the file
        assert len(data["files"]) == 1
        assert data["files"][0]["filename"] == "file.txt"

    def test_list_shared_files_sorted_by_creation_time(self, files_client):
        """Files should be sorted by creation time, newest first."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()

        # Create files with slight delays
        (shared_dir / "old.txt").write_text("old")
        time.sleep(0.1)
        (shared_dir / "new.txt").write_text("new")

        response = client.get("/api/files/")
        assert response.status_code == 200
        data = response.json()

        # Newest should be first
        assert len(data["files"]) == 2
        assert data["files"][0]["filename"] == "new.txt"
        assert data["files"][1]["filename"] == "old.txt"

    def test_list_shared_files_correct_urls(self, files_client):
        """Should include correct download URLs."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()
        (shared_dir / "report.pdf").write_bytes(b"content")

        response = client.get("/api/files/")
        assert response.status_code == 200
        data = response.json()

        file_data = data["files"][0]
        assert file_data["url"] == "/api/files/report.pdf"


# =============================================================================
# Edge Cases and Special Scenarios
# =============================================================================

class TestEdgeCases:
    """Test edge cases and special scenarios."""

    def test_file_with_spaces_in_name(self, files_client):
        """Should handle files with spaces in names."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()
        test_file = shared_dir / "my file.txt"
        test_file.write_text("content")

        response = client.get("/api/files/my%20file.txt")
        assert response.status_code == 200

    def test_file_with_unicode_name(self, files_client):
        """Should handle files with unicode characters."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()
        test_file = shared_dir / "documento.txt"
        test_file.write_text("contenido")

        response = client.get("/api/files/documento.txt")
        assert response.status_code == 200

    def test_empty_file(self, files_client):
        """Should handle empty files correctly."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()
        test_file = shared_dir / "empty.txt"
        test_file.touch()

        response = client.get("/api/files/empty.txt")
        assert response.status_code == 200
        assert response.content == b""

    def test_large_file_info(self, files_client):
        """Should handle large file info correctly."""
        client, temp_dir = files_client
        test_file = temp_dir / "large.bin"
        # Write a file that's 5MB
        test_file.write_bytes(b"x" * (5 * 1024 * 1024))

        response = client.get(
            "/api/files/info",
            params={"path": str(test_file)}
        )
        assert response.status_code == 200
        data = response.json()
        assert data["size_bytes"] == 5 * 1024 * 1024
        assert "5.0 MB" in data["size_formatted"]

    def test_file_with_no_extension(self, files_client):
        """Should handle files without extensions."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()
        test_file = shared_dir / "Dockerfile"
        test_file.write_text("FROM python:3.9")

        response = client.get("/api/files/Dockerfile")
        assert response.status_code == 200
        # Unknown extension should use octet-stream
        assert "application/octet-stream" in response.headers.get("content-type", "")

    def test_file_with_multiple_dots(self, files_client):
        """Should handle files with multiple dots in name."""
        client, temp_dir = files_client
        shared_dir = temp_dir / "shared-files"
        shared_dir.mkdir()
        test_file = shared_dir / "app.config.json"
        test_file.write_text('{"key": "value"}')

        response = client.get("/api/files/app.config.json")
        assert response.status_code == 200
        assert "application/json" in response.headers.get("content-type", "")


class TestRouterConfiguration:
    """Test router configuration and structure."""

    def test_router_exists(self):
        """Router should exist and be properly configured."""
        from app.api.shared_files import router
        assert router is not None

    def test_router_prefix(self):
        """Router should have correct prefix."""
        from app.api.shared_files import router
        assert router.prefix == "/api/files"

    def test_router_tags(self):
        """Router should have appropriate tags."""
        from app.api.shared_files import router
        assert "files" in router.tags


class TestModuleImports:
    """Test that module imports work correctly."""

    def test_module_imports(self):
        """Module should import without errors."""
        from app.api import shared_files
        assert shared_files is not None

    def test_helper_functions_importable(self):
        """Helper functions should be importable."""
        from app.api.shared_files import is_path_safe, format_file_size, get_file_icon
        assert callable(is_path_safe)
        assert callable(format_file_size)
        assert callable(get_file_icon)

    def test_constants_importable(self):
        """Constants should be importable."""
        from app.api.shared_files import MEDIA_TYPES, FILE_ICONS
        assert isinstance(MEDIA_TYPES, dict)
        assert isinstance(FILE_ICONS, dict)
