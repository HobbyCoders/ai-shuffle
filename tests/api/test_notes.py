"""
Unit tests for the notes API endpoints, focused on note file attachments.

Tests cover:
- Uploading a file to a note (POST /api/v1/notes/{note_id}/attachments)
- Listing attachments (GET .../attachments)
- Downloading an attachment (GET .../attachments/{id}/download)
- Deleting an attachment (DELETE .../attachments/{id})
- Validation and not-found handling

The database module is mocked (matching the pattern in test_projects.py /
test_tags.py); file storage uses a real temp directory wired through
``settings.effective_data_dir`` so the on-disk write/read path is exercised.
"""

import shutil
import tempfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.notes import router
from app.api.auth import require_auth


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def temp_data_dir():
    tmpdir = tempfile.mkdtemp()
    yield Path(tmpdir)
    shutil.rmtree(tmpdir, ignore_errors=True)


@pytest.fixture
def mock_database():
    """Mock the database module used by the notes API."""
    with patch("app.api.notes.database") as mock_db:
        yield mock_db


@pytest.fixture
def app(mock_database, temp_data_dir):
    """FastAPI app with the notes router; note_files writes under temp_data_dir."""
    # note_files.py reads settings.effective_data_dir lazily inside each helper,
    # so patching the attribute on the shared settings object is sufficient.
    with patch("app.core.note_files.settings") as mock_settings:
        mock_settings.effective_data_dir = temp_data_dir
        application = FastAPI()
        application.include_router(router)
        application.dependency_overrides[require_auth] = lambda: "test-session-token"
        yield application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_note():
    return {
        "id": "note-abc123def456",
        "title": "My Note",
        "content": "Some content",
        "tags": [],
        "is_pinned": False,
        "file_path": "notes/my-note.md",
        "created_at": "2024-01-15T10:30:00",
        "updated_at": "2024-01-15T10:30:00",
    }


# =============================================================================
# Upload
# =============================================================================

class TestUploadNoteAttachment:
    def test_upload_success(self, client, mock_database, sample_note):
        mock_database.get_note.return_value = sample_note

        created = {
            "id": "natt-123456789abc",
            "note_id": sample_note["id"],
            "filename": "diagram.png",
            "content_type": "image/png",
            "size_bytes": 12,
            "created_at": "2024-01-15T10:30:00",
        }
        mock_database.create_note_attachment.return_value = created

        files = {"file": ("diagram.png", BytesIO(b"file content"), "image/png")}
        response = client.post(
            f"/api/v1/notes/{sample_note['id']}/attachments", files=files
        )

        assert response.status_code == 201
        data = response.json()
        assert data["filename"] == "diagram.png"
        assert data["note_id"] == sample_note["id"]

        # The DB layer was asked to persist the attachment with the right note id
        # and a stored relative path under notes/attachments/{note_id}/.
        kwargs = mock_database.create_note_attachment.call_args.kwargs
        assert kwargs["note_id"] == sample_note["id"]
        assert kwargs["filename"] == "diagram.png"
        assert kwargs["file_path"] == f"notes/attachments/{sample_note['id']}/diagram.png"
        assert kwargs["size_bytes"] == 12

    def test_upload_note_not_found(self, client, mock_database):
        mock_database.get_note.return_value = None
        files = {"file": ("x.txt", BytesIO(b"x"), "text/plain")}
        response = client.post("/api/v1/notes/missing/attachments", files=files)
        assert response.status_code == 404

    def test_upload_writes_file_to_disk(self, client, mock_database, sample_note, temp_data_dir):
        mock_database.get_note.return_value = sample_note
        mock_database.create_note_attachment.return_value = {
            "id": "natt-1",
            "note_id": sample_note["id"],
            "filename": "data.bin",
            "content_type": "application/octet-stream",
            "size_bytes": 5,
            "created_at": "2024-01-15T10:30:00",
        }

        files = {"file": ("data.bin", BytesIO(b"hello"), "application/octet-stream")}
        response = client.post(
            f"/api/v1/notes/{sample_note['id']}/attachments", files=files
        )
        assert response.status_code == 201

        written = temp_data_dir / "notes" / "attachments" / sample_note["id"] / "data.bin"
        assert written.exists()
        assert written.read_bytes() == b"hello"


# =============================================================================
# List
# =============================================================================

class TestListNoteAttachments:
    def test_list_success(self, client, mock_database, sample_note):
        mock_database.get_note.return_value = sample_note
        mock_database.get_note_attachments.return_value = [
            {
                "id": "natt-1",
                "note_id": sample_note["id"],
                "filename": "a.txt",
                "content_type": "text/plain",
                "size_bytes": 3,
                "created_at": "2024-01-15T10:30:00",
            }
        ]
        response = client.get(f"/api/v1/notes/{sample_note['id']}/attachments")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["filename"] == "a.txt"

    def test_list_note_not_found(self, client, mock_database):
        mock_database.get_note.return_value = None
        response = client.get("/api/v1/notes/missing/attachments")
        assert response.status_code == 404


# =============================================================================
# Download
# =============================================================================

class TestDownloadNoteAttachment:
    def test_download_success(self, client, mock_database, sample_note, temp_data_dir):
        # Place a real file on disk and have the DB return its relative path.
        rel = f"notes/attachments/{sample_note['id']}/report.txt"
        disk_path = temp_data_dir / rel
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(b"report body")

        mock_database.get_note_attachment.return_value = {
            "id": "natt-1",
            "note_id": sample_note["id"],
            "filename": "report.txt",
            "file_path": rel,
            "content_type": "text/plain",
            "size_bytes": 11,
            "created_at": "2024-01-15T10:30:00",
        }

        response = client.get(
            f"/api/v1/notes/{sample_note['id']}/attachments/natt-1/download"
        )
        assert response.status_code == 200
        assert response.content == b"report body"

    def test_download_wrong_note(self, client, mock_database, sample_note):
        # Attachment belongs to a different note than the URL — must 404.
        mock_database.get_note_attachment.return_value = {
            "id": "natt-1",
            "note_id": "some-other-note",
            "filename": "report.txt",
            "file_path": "notes/attachments/some-other-note/report.txt",
            "content_type": "text/plain",
            "size_bytes": 11,
            "created_at": "2024-01-15T10:30:00",
        }
        response = client.get(
            f"/api/v1/notes/{sample_note['id']}/attachments/natt-1/download"
        )
        assert response.status_code == 404

    def test_download_not_found(self, client, mock_database, sample_note):
        mock_database.get_note_attachment.return_value = None
        response = client.get(
            f"/api/v1/notes/{sample_note['id']}/attachments/natt-x/download"
        )
        assert response.status_code == 404


# =============================================================================
# Delete
# =============================================================================

class TestDeleteNoteAttachment:
    def test_delete_success(self, client, mock_database, sample_note, temp_data_dir):
        rel = f"notes/attachments/{sample_note['id']}/old.txt"
        disk_path = temp_data_dir / rel
        disk_path.parent.mkdir(parents=True, exist_ok=True)
        disk_path.write_bytes(b"bye")

        mock_database.get_note_attachment.return_value = {
            "id": "natt-1",
            "note_id": sample_note["id"],
            "filename": "old.txt",
            "file_path": rel,
            "content_type": "text/plain",
            "size_bytes": 3,
            "created_at": "2024-01-15T10:30:00",
        }
        mock_database.delete_note_attachment.return_value = True

        response = client.delete(
            f"/api/v1/notes/{sample_note['id']}/attachments/natt-1"
        )
        assert response.status_code == 204
        assert not disk_path.exists()  # file removed from disk
        mock_database.delete_note_attachment.assert_called_once_with("natt-1")

    def test_delete_not_found(self, client, mock_database, sample_note):
        mock_database.get_note_attachment.return_value = None
        response = client.delete(
            f"/api/v1/notes/{sample_note['id']}/attachments/natt-x"
        )
        assert response.status_code == 404
