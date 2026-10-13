"""The projectless upload endpoint, as the chat lane's attach button uses it.

The chat composer has no project to upload into, so it posts to
``/api/v1/workspace/upload`` with ``path=chat-uploads``. Two things about that
response are load-bearing and neither was before this lane existed:

* ``full_path`` — the composer turns an attachment into an ``@<fullPath>`` text
  reference for the agent to open. A workspace-relative path would resolve
  against whatever cwd the reader happens to have.
* the file has to land where ``chat_mode_read_permitted`` will let a chat
  session read it. That fence and this endpoint are the two halves of one
  feature, so the last test here checks them against each other rather than
  each against its own idea of where uploads go.
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.core.session_modes import CHAT_UPLOADS_DIRNAME, chat_mode_read_permitted


@pytest.fixture(params=["true", "false"], ids=["local", "docker"])
def workspace_client(tmp_path, monkeypatch, request):
    """The workspace router alone, rooted at a temp dir.

    Mounted without the full app so no lifespan, migration or DB machinery runs;
    auth is overridden because every route here sits behind ``require_auth``.
    The workspace root moves via the runtime override — the same seam local-mode
    setup writes, and the one both the endpoint and the chat fence read, so the
    last test below compares them on equal footing.
    """
    from app.api.auth import require_auth
    from app.api.workspace_files import router

    import app.core.config as config

    # Exercise both deployment modes independently of the test runner's host.
    # The project producer still reads the settings object directly, while the
    # workspace producer reads the runtime override: both must use this root.
    monkeypatch.setenv("LOCAL_MODE", request.param)
    monkeypatch.setattr(config.settings, "workspace_dir", tmp_path)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", tmp_path)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"

    with TestClient(app) as client:
        yield client, tmp_path.resolve()


def _upload(client, name="shot.png", body=b"bytes", path=CHAT_UPLOADS_DIRNAME):
    return client.post(
        "/api/v1/workspace/upload",
        files={"file": (name, body, "image/png")},
        data={"path": path},
    )


def test_upload_returns_an_absolute_full_path(workspace_client):
    client, workspace = workspace_client
    response = _upload(client)
    assert response.status_code == 200

    body = response.json()
    assert body["path"] == f"{CHAT_UPLOADS_DIRNAME}/shot.png"
    full = Path(body["full_path"])
    assert full.is_absolute()
    assert full.read_bytes() == b"bytes"
    assert full.resolve() == (workspace / CHAT_UPLOADS_DIRNAME / "shot.png").resolve()
    # Forward slashes, so the reference survives the round trip through prompt
    # text on Windows (where str(Path) would emit backslash escapes).
    assert "\\" not in body["full_path"]


def test_upload_deduplicates_names_in_both_paths(workspace_client):
    """A second attachment with the same name gets a suffix — and ``full_path``
    has to name the file that was actually written, not the one that was asked
    for, or the ``@ref`` points at the earlier upload."""
    client, _ = workspace_client
    first = _upload(client, body=b"one").json()
    second = _upload(client, body=b"two").json()

    assert first["full_path"] != second["full_path"]
    assert Path(second["full_path"]).name == second["filename"] == "shot_1.png"
    assert Path(second["full_path"]).read_bytes() == b"two"


def test_uploaded_file_is_readable_by_a_chat_session(workspace_client):
    """The join between this endpoint and the chat ceiling: a file the composer
    just uploaded is one the permission callback will let ``Read`` open, in both
    the absolute and the workspace-relative spelling."""
    client, _ = workspace_client
    body = _upload(client).json()

    assert chat_mode_read_permitted("Read", {"file_path": body["full_path"]}) is True
    assert chat_mode_read_permitted("Read", {"file_path": body["path"]}) is True


def test_upload_outside_the_workspace_is_refused(workspace_client):
    """The endpoint's own boundary, unchanged by this slice but worth pinning:
    a traversal in the ``path`` field cannot plant a file the chat fence would
    then happily read."""
    client, _ = workspace_client
    response = _upload(client, path=f"../{CHAT_UPLOADS_DIRNAME}")
    assert response.status_code == 400


def test_an_oversized_body_is_refused_while_it_streams(workspace_client, monkeypatch):
    """The cap that actually matters, with the Content-Length shortcut disabled.

    Only the projects-side limit is lowered here, so this endpoint's header
    pre-check (still 50 MB) waves the request through and the chunked read has to
    catch it. That is the path a lying or absent Content-Length takes, and it is
    the one that used to be a bare ``await file.read()`` — the composer now
    points every chat attachment at this route, so an uncapped read here buffers
    whatever the user dragged onto it into the worker's memory.
    """
    import app.api.projects as projects

    monkeypatch.setattr(projects, "MAX_UPLOAD_SIZE", 1024)
    client, workspace = workspace_client

    response = _upload(client, name="big.png", body=b"x" * 4096)

    assert response.status_code == 413
    assert "too large" in response.json()["detail"].lower()
    # Nothing half-written left behind for a chat session to find.
    assert not (workspace / CHAT_UPLOADS_DIRNAME / "big.png").exists()


def test_an_oversized_declared_length_is_refused_before_the_body_is_read(
    workspace_client, monkeypatch
):
    """The cheap half of the same guard, matching the project upload's shape."""
    import app.api.workspace_files as workspace_files

    monkeypatch.setattr(workspace_files, "MAX_UPLOAD_SIZE", 1024)
    client, _ = workspace_client

    response = _upload(client, name="big.png", body=b"x" * 4096)
    assert response.status_code == 413


def test_an_upload_under_the_cap_still_lands(workspace_client, monkeypatch):
    """The boundary from the other side — a cap that refuses everything would
    pass both tests above and break the feature."""
    import app.api.projects as projects
    import app.api.workspace_files as workspace_files

    monkeypatch.setattr(projects, "MAX_UPLOAD_SIZE", 1024)
    monkeypatch.setattr(workspace_files, "MAX_UPLOAD_SIZE", 1024)
    client, _ = workspace_client

    body = _upload(client, name="small.png", body=b"x" * 512).json()
    assert Path(body["full_path"]).read_bytes() == b"x" * 512

@pytest.mark.parametrize("producer", ["workspace", "project"])
def test_upload_does_not_overwrite_a_hardlinked_destination(workspace_client, producer):
    import os
    from app.api.projects import _perform_project_upload
    client, workspace = workspace_client
    outside = workspace.parent / (workspace.name + "-outside.txt")
    outside.write_bytes(b"outside marker")
    uploads = workspace / CHAT_UPLOADS_DIRNAME
    uploads.mkdir()
    os.link(outside, uploads / "notes.txt")
    try:
        if producer == "workspace":
            response = _upload(client, name="notes.txt", body=b"new attachment")
            assert response.status_code == 200
            body = response.json()
        else:
            body = _perform_project_upload({"path": str(workspace)}, filename="notes.txt",
                                           path=CHAT_UPLOADS_DIRNAME, content=b"new attachment")
        assert body["filename"] == "notes_1.txt"
        assert Path(body["full_path"]).read_bytes() == b"new attachment"
        assert outside.read_bytes() == b"outside marker"
    finally:
        outside.unlink()


def test_project_upload_rejects_sibling_project_traversal(workspace_client):
    from app.api.projects import _perform_project_upload
    from fastapi import HTTPException
    _, workspace = workspace_client
    project = workspace / "project"
    project.mkdir()
    with pytest.raises((ValueError, HTTPException)):
        _perform_project_upload({"path": str(project)}, path="../sibling",
                                filename="file.txt", content=b"bad")
    assert not (workspace / "sibling" / "file.txt").exists()

@pytest.mark.parametrize("producer", ["workspace", "project"])
def test_upload_dangling_destination_through_actual_producer(workspace_client, producer):
    from app.api.projects import _perform_project_upload
    client, workspace = workspace_client
    uploads = workspace / CHAT_UPLOADS_DIRNAME
    uploads.mkdir()
    outside = workspace.parent / (workspace.name + "-absent.txt")
    try:
        (uploads / "notes.txt").symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"File symlink creation unavailable: {exc}")
    if producer == "workspace":
        response = _upload(client, name="notes.txt", body=b"new attachment")
        assert response.status_code == 200
        body = response.json()
    else:
        body = _perform_project_upload({"path": str(workspace)}, filename="notes.txt",
                                       path=CHAT_UPLOADS_DIRNAME, content=b"new attachment")
    assert body["filename"] == "notes_1.txt"
    assert Path(body["full_path"]).read_bytes() == b"new attachment"
    assert not outside.exists()
