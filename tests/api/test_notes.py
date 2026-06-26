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

    def test_upload_stores_bytes_in_db(self, client, mock_database, sample_note):
        # The bytes must be handed to the DB layer so installs sharing the
        # database can serve the attachment without the local file.
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
        assert mock_database.create_note_attachment.call_args.kwargs["data"] == b"hello"


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

    def test_download_materializes_from_db_when_file_missing(
        self, client, mock_database, sample_note, temp_data_dir
    ):
        # Cross-machine case: the attachment row is in the shared DB but its
        # file was never written on this machine. The bytes stored in the DB
        # are served and the local cache is repopulated.
        rel = f"notes/attachments/{sample_note['id']}/remote.txt"
        disk_path = temp_data_dir / rel
        assert not disk_path.exists()  # no local file to begin with

        mock_database.get_note_attachment.return_value = {
            "id": "natt-1",
            "note_id": sample_note["id"],
            "filename": "remote.txt",
            "file_path": rel,
            "content_type": "text/plain",
            "size_bytes": 13,
            "created_at": "2024-01-15T10:30:00",
        }
        mock_database.get_note_attachment_data.return_value = b"from other PC"

        response = client.get(
            f"/api/v1/notes/{sample_note['id']}/attachments/natt-1/download"
        )
        assert response.status_code == 200
        assert response.content == b"from other PC"
        # Local cache was repopulated from the DB bytes.
        assert disk_path.exists()
        assert disk_path.read_bytes() == b"from other PC"

    def test_download_404_when_file_and_db_bytes_missing(
        self, client, mock_database, sample_note
    ):
        # No local file and no bytes in the DB (e.g. a legacy attachment whose
        # file only ever lived on another machine and was never backfilled).
        mock_database.get_note_attachment.return_value = {
            "id": "natt-1",
            "note_id": sample_note["id"],
            "filename": "gone.txt",
            "file_path": f"notes/attachments/{sample_note['id']}/gone.txt",
            "content_type": "text/plain",
            "size_bytes": 0,
            "created_at": "2024-01-15T10:30:00",
        }
        mock_database.get_note_attachment_data.return_value = None

        response = client.get(
            f"/api/v1/notes/{sample_note['id']}/attachments/natt-1/download"
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


# =============================================================================
# External-edit reconciliation (GET re-reads the on-disk markdown file when it
# has been edited outside the app, and pushes the new content into the DB).
# =============================================================================

import os
import time as _time


class TestExternalEditReconciliation:
    def _write_note_file(self, temp_data_dir, filename, body):
        notes_dir = temp_data_dir / "notes"
        notes_dir.mkdir(parents=True, exist_ok=True)
        (notes_dir / filename).write_text(body, encoding="utf-8")
        return notes_dir / filename

    def test_get_note_reflects_external_file_edit(
        self, client, mock_database, sample_note, temp_data_dir
    ):
        # DB row is "old"; the file on disk was edited more recently.
        old = dict(sample_note, updated_at="2000-01-01T00:00:00+00:00")
        mock_database.get_note.return_value = old

        path = self._write_note_file(
            temp_data_dir, "my-note.md", "# My Note\n\nEdited externally"
        )
        # Force a recent mtime so it is strictly newer than the DB row.
        os.utime(path, (_time.time(), _time.time()))

        refreshed = dict(old, content="Edited externally")
        mock_database.update_note.return_value = refreshed

        response = client.get(f"/api/v1/notes/{sample_note['id']}")
        assert response.status_code == 200
        assert response.json()["content"] == "Edited externally"

        # The DB was updated with the content read from disk.
        kwargs = mock_database.update_note.call_args.kwargs
        assert kwargs["note_id"] == sample_note["id"]
        assert kwargs["content"] == "Edited externally"
        assert kwargs["title"] == "My Note"

    def test_get_note_no_rewrite_when_file_unchanged(
        self, client, mock_database, sample_note, temp_data_dir
    ):
        # File mtime is OLDER than the DB row → no reconciliation should happen.
        recent = dict(sample_note, updated_at="2999-01-01T00:00:00+00:00")
        mock_database.get_note.return_value = recent

        path = self._write_note_file(
            temp_data_dir, "my-note.md", "# My Note\n\nSome content"
        )
        old_ts = _time.mktime((2001, 1, 1, 0, 0, 0, 0, 0, 0))
        os.utime(path, (old_ts, old_ts))

        response = client.get(f"/api/v1/notes/{sample_note['id']}")
        assert response.status_code == 200
        mock_database.update_note.assert_not_called()

    def test_get_note_missing_file_left_untouched(
        self, client, mock_database, sample_note
    ):
        # No file on disk → return the DB row as-is, no update.
        mock_database.get_note.return_value = sample_note
        response = client.get(f"/api/v1/notes/{sample_note['id']}")
        assert response.status_code == 200
        assert response.json()["content"] == "Some content"
        mock_database.update_note.assert_not_called()

    def test_list_notes_does_not_reconcile(
        self, client, mock_database, sample_note, temp_data_dir
    ):
        # Reconciliation is intentionally limited to the single-note GET, so
        # even when the file on disk is newer, the list returns DB content as-is
        # and performs no write — keeping list reads free of a per-note scan.
        old = dict(sample_note, updated_at="2000-01-01T00:00:00+00:00")
        mock_database.get_all_notes.return_value = [old]

        path = self._write_note_file(
            temp_data_dir, "my-note.md", "# My Note\n\nList edit"
        )
        os.utime(path, (_time.time(), _time.time()))

        response = client.get("/api/v1/notes")
        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["content"] == "Some content"  # original DB content, not reconciled
        mock_database.update_note.assert_not_called()
