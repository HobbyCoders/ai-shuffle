"""The picker staging endpoint produces the files the remote-turn lift reads."""
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.api import sessions_uploads
from app.api.auth import require_auth
from app.core import upload_transfer


@pytest.fixture
def upload_client(tmp_path, monkeypatch):
    import app.core.config as config
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)
    row = {"id": "worker-session", "project_v2_id": "canonical-project", "mode": "code"}
    check = AsyncMock(return_value=row)
    monkeypatch.setattr(sessions_uploads, "require_session", check)
    app = FastAPI()
    app.include_router(sessions_uploads.router, prefix="/api/v1/sessions")
    app.dependency_overrides[require_auth] = lambda: "test-token"
    with TestClient(app) as client:
        yield client, row, check, tmp_path


def test_upload_lift_materialize_and_prompt_rewrite(upload_client, mock_db):
    client, row, _, root = upload_client
    response = client.post("/api/v1/sessions/worker-session/upload",
                           files={"file": ("notes.txt", b"remote attachment", "text/plain")})
    assert response.status_code == 200
    body = response.json()
    source = body["full_path"]
    assert Path(source).parent == upload_transfer.session_upload_dir(row["id"])
    assert Path(source).read_bytes() == b"remote attachment"
    ids = upload_transfer.lift_session_paths_to_blobs([source], row)
    owner = root / "owner-checkout"
    owner.mkdir()
    mapping = upload_transfer.materialize_blobs_for_turn(ids, str(owner))
    destination = owner / "uploads" / "notes.txt"
    assert destination.read_bytes() == b"remote attachment"
    assert upload_transfer.rewrite_prompt(f"Read @{source}", mapping) == f"Read @{destination}"


def test_access_refusal_never_stages_bytes(upload_client):
    client, _, check, root = upload_client
    check.side_effect = HTTPException(403, "Not your session")
    response = client.post("/api/v1/sessions/worker-session/upload",
                           files={"file": ("notes.txt", b"secret", "text/plain")})
    assert response.status_code == 403
    assert not (root / "session-uploads").exists()


def test_upload_size_limit_and_no_partial_file(upload_client, monkeypatch):
    from app.api import projects
    client, _, _, root = upload_client
    monkeypatch.setattr(projects, "MAX_UPLOAD_SIZE", 8)
    response = client.post("/api/v1/sessions/worker-session/upload",
                           files={"file": ("notes.txt", b"over the limit", "text/plain")})
    assert response.status_code == 413
    assert not (root / "session-uploads").exists()


def test_other_session_paths_cannot_be_lifted(upload_client, mock_db):
    client, row, _, _ = upload_client
    body = client.post("/api/v1/sessions/worker-session/upload",
                       files={"file": ("notes.txt", b"data", "text/plain")}).json()
    with pytest.raises(upload_transfer.UploadLiftError, match="outside the project"):
        upload_transfer.lift_session_paths_to_blobs([body["full_path"]], {**row, "id": "another"})


def test_uncreated_project_and_chat_lanes_are_not_staged_here(upload_client):
    client, row, _, root = upload_client
    for changes in [{"project_v2_id": None}, {"project_v2_id": "p", "mode": "chat"}]:
        row.update(changes)
        response = client.post("/api/v1/sessions/worker-session/upload",
                               files={"file": ("notes.txt", b"data", "text/plain")})
        assert response.status_code == 400
    assert not (root / "session-uploads").exists()
