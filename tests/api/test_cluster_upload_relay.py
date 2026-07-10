"""Unit tests for the owner-side cluster chat-attachment upload endpoint.

In cluster mode a session may be OWNED by a different install (shared DB,
separate disks). When a user attaches a file to a session they are only viewing,
the file must land in the OWNING install's project/worktree uploads dir — the
session's agent runs there, so a viewer-local path would be unreadable. The
viewer install relays the bytes to the owner's
``/api/v1/cluster/sessions/{id}/upload``, which saves via the normal project
upload path and returns owner-rooted paths.

These endpoints operate on the LOCAL install (they run on the owner) and never
re-relay. Everything is mocked at the handler/DB boundary — no app, no DB, no
network. Mirrors tests/api/test_cluster_permission_relay.py.
"""

from io import BytesIO

import pytest
from fastapi import HTTPException
from starlette.datastructures import UploadFile

import app.api.cluster as cluster_api


async def _passthrough_run_sync(fn, *args, **kwargs):
    return fn(*args, **kwargs)


def _upload(data: bytes = b"bytes", filename: str = "shot.png") -> UploadFile:
    return UploadFile(file=BytesIO(data), filename=filename)


async def test_cluster_session_upload_saves_and_returns_owner_paths(monkeypatch):
    """Saves via the project upload path and returns owner-rooted paths."""
    fake_session = {"id": "s1", "instance_id": "owner", "project_id": "proj"}
    fake_project = {"id": "proj", "path": "proj"}
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: fake_session)
    monkeypatch.setattr(cluster_api.database, "get_project", lambda pid: fake_project)

    captured: dict = {}

    import app.api.projects as projects_api

    def fake_perform(project, *, content, filename, path, session_id, worktree_id):
        captured.update(
            project=project,
            content=content,
            filename=filename,
            path=path,
            session_id=session_id,
            worktree_id=worktree_id,
        )
        return {
            "filename": filename,
            "path": f"uploads/{filename}",
            "full_path": f"/owner/proj/uploads/{filename}",
            "size": len(content),
            "sizeFormatted": "5 B",
        }

    monkeypatch.setattr(projects_api, "_perform_project_upload", fake_perform)

    result = await cluster_api.cluster_session_upload(
        "s1", file=_upload(b"bytes"), path="", worktree_id="wt1", install_id="peer-9"
    )

    assert result["full_path"] == "/owner/proj/uploads/shot.png"
    assert result["path"] == "uploads/shot.png"
    # The owner resolved the destination from the SESSION it owns.
    assert captured["project"] == fake_project
    assert captured["content"] == b"bytes"
    assert captured["filename"] == "shot.png"
    assert captured["session_id"] == "s1"
    assert captured["worktree_id"] == "wt1"


async def test_cluster_session_upload_404_when_session_missing(monkeypatch):
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: None)

    with pytest.raises(HTTPException) as ei:
        await cluster_api.cluster_session_upload(
            "missing", file=_upload(), install_id="x"
        )
    assert ei.value.status_code == 404


async def test_cluster_session_upload_404_when_project_missing(monkeypatch):
    fake_session = {"id": "s1", "instance_id": "owner", "project_id": "proj"}
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: fake_session)
    monkeypatch.setattr(cluster_api.database, "get_project", lambda pid: None)

    with pytest.raises(HTTPException) as ei:
        await cluster_api.cluster_session_upload(
            "s1", file=_upload(), install_id="x"
        )
    assert ei.value.status_code == 404


async def test_cluster_session_upload_413_when_over_limit(monkeypatch):
    """Defence-in-depth: the receiver re-enforces the size limit."""
    fake_session = {"id": "s1", "instance_id": "owner", "project_id": "proj"}
    fake_project = {"id": "proj", "path": "proj"}
    monkeypatch.setattr(cluster_api, "run_sync", _passthrough_run_sync)
    monkeypatch.setattr(cluster_api.database, "get_session", lambda sid: fake_session)
    monkeypatch.setattr(cluster_api.database, "get_project", lambda pid: fake_project)

    import app.api.projects as projects_api

    monkeypatch.setattr(projects_api, "MAX_UPLOAD_SIZE", 4)

    with pytest.raises(HTTPException) as ei:
        await cluster_api.cluster_session_upload(
            "s1", file=_upload(b"way too big"), install_id="x"
        )
    assert ei.value.status_code == 413
