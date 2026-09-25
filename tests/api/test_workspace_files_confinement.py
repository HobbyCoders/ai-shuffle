"""Workspace file browser routes stay inside the workspace root.

``app.api.workspace_files`` lists, downloads, creates, renames and deletes
paths taken straight from the query string, so every route must refuse ``..``
traversal and symlinks that point out of the workspace, and the delete route
must never remove the workspace root itself.
"""
import os
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

PREFIX = "/api/v1/workspace"


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """The workspace router alone, rooted at ``tmp_path/workspace``.

    A sibling ``outside`` directory holds a file the routes must never reach.
    """
    from app.api.auth import require_auth
    from app.api.workspace_files import router
    from app.core import config

    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    workspace.mkdir()
    outside.mkdir()
    (outside / "secret.txt").write_text("top secret")
    (workspace / "docs").mkdir()
    (workspace / "docs" / "readme.md").write_text("hello")

    monkeypatch.setattr(config.settings, "workspace_dir", workspace)
    monkeypatch.setitem(config._runtime_overrides, "workspace_dir", workspace)

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    with TestClient(app) as client:
        yield client, workspace.resolve(), outside.resolve()


# ---------------------------------------------------------------------------
# Traversal is refused on every route
# ---------------------------------------------------------------------------

def test_list_refuses_parent_traversal(ws):
    client, _, _ = ws
    response = client.get(f"{PREFIX}/files", params={"path": "../outside"})
    assert response.status_code == 400
    assert "escapes workspace" in response.json()["detail"]


def test_download_refuses_parent_traversal(ws):
    client, _, _ = ws
    response = client.get(f"{PREFIX}/files/download", params={"path": "docs/../../outside/secret.txt"})
    assert response.status_code == 400
    assert "top secret" not in response.text


def test_download_refuses_absolute_path(ws):
    client, _, outside = ws
    response = client.get(f"{PREFIX}/files/download", params={"path": str(outside / "secret.txt")})
    assert response.status_code == 400
    assert "top secret" not in response.text


def test_download_refuses_symlink_out_of_workspace(ws):
    client, workspace, outside = ws
    (workspace / "escape").symlink_to(outside / "secret.txt")
    response = client.get(f"{PREFIX}/files/download", params={"path": "escape"})
    assert response.status_code == 400
    assert "top secret" not in response.text


def test_list_refuses_symlinked_directory_out_of_workspace(ws):
    client, workspace, outside = ws
    (workspace / "linkdir").symlink_to(outside, target_is_directory=True)
    response = client.get(f"{PREFIX}/files", params={"path": "linkdir"})
    assert response.status_code == 400


def test_create_folder_refuses_traversal_in_parent_and_name(ws):
    client, _, outside = ws
    assert client.post(f"{PREFIX}/files/folder", params={"path": "../outside"},
                       json={"name": "planted"}).status_code == 400
    assert client.post(f"{PREFIX}/files/folder", json={"name": ".."}).status_code == 400
    assert not (outside / "planted").exists()


@pytest.mark.parametrize("name", ["", "   ", "a/b", "a\\b", "../x"])
def test_create_folder_rejects_names_with_separators(ws, name):
    client, workspace, _ = ws
    response = client.post(f"{PREFIX}/files/folder", json={"name": name})
    assert response.status_code == 400
    assert sorted(p.name for p in workspace.iterdir()) == ["docs"]


def test_rename_refuses_traversal_source(ws):
    client, _, outside = ws
    response = client.put(f"{PREFIX}/files/rename", params={"path": "../outside/secret.txt"},
                          json={"new_name": "stolen.txt"})
    assert response.status_code == 400
    assert (outside / "secret.txt").exists()


@pytest.mark.parametrize("new_name", ["../moved.md", "sub/moved.md", "..\\moved.md", "  "])
def test_rename_rejects_target_names_that_leave_the_directory(ws, new_name):
    client, workspace, _ = ws
    response = client.put(f"{PREFIX}/files/rename", params={"path": "docs/readme.md"},
                          json={"new_name": new_name})
    assert response.status_code == 400
    assert (workspace / "docs" / "readme.md").exists()


def test_delete_refuses_traversal(ws):
    client, _, outside = ws
    response = client.delete(f"{PREFIX}/files", params={"path": "../outside/secret.txt"})
    assert response.status_code == 400
    assert (outside / "secret.txt").exists()


def test_delete_refuses_symlink_out_of_workspace(ws):
    client, workspace, outside = ws
    (workspace / "escape").symlink_to(outside, target_is_directory=True)
    response = client.delete(f"{PREFIX}/files", params={"path": "escape"})
    assert response.status_code == 400
    assert (outside / "secret.txt").exists()


@pytest.mark.parametrize("path", ["", ".", "docs/..", "./"])
def test_delete_never_removes_the_workspace_root(ws, path):
    client, workspace, _ = ws
    response = client.delete(f"{PREFIX}/files", params={"path": path})
    assert response.status_code == 400
    assert "workspace root" in response.json()["detail"]
    assert (workspace / "docs" / "readme.md").exists()


# ---------------------------------------------------------------------------
# In-bounds behaviour and error mapping
# ---------------------------------------------------------------------------

def test_list_missing_and_non_directory(ws):
    client, _, _ = ws
    assert client.get(f"{PREFIX}/files", params={"path": "nope"}).status_code == 404
    assert client.get(f"{PREFIX}/files", params={"path": "docs/readme.md"}).status_code == 400


def test_list_hides_dotfiles_unless_asked(ws):
    client, workspace, _ = ws
    (workspace / ".hidden").write_text("x")
    names = {f["name"] for f in client.get(f"{PREFIX}/files").json()["files"]}
    assert ".hidden" not in names and "docs" in names
    names = {f["name"] for f in client.get(f"{PREFIX}/files", params={"show_hidden": True}).json()["files"]}
    assert ".hidden" in names


def test_download_missing_and_directory(ws):
    client, _, _ = ws
    assert client.get(f"{PREFIX}/files/download", params={"path": "docs/none.md"}).status_code == 404
    assert client.get(f"{PREFIX}/files/download", params={"path": "docs"}).status_code == 400
    ok = client.get(f"{PREFIX}/files/download", params={"path": "docs/readme.md"})
    assert ok.status_code == 200 and ok.content == b"hello"


def test_create_folder_inside_parent_and_conflict(ws):
    client, workspace, _ = ws
    response = client.post(f"{PREFIX}/files/folder", params={"path": "docs"}, json={"name": "new"})
    assert response.status_code == 200
    assert response.json() == {"name": "new", "path": "docs/new", "type": "directory"}
    assert (workspace / "docs" / "new").is_dir()
    assert client.post(f"{PREFIX}/files/folder", params={"path": "docs"},
                       json={"name": "new"}).status_code == 409


def test_create_folder_mkdir_failure_is_500(ws, monkeypatch):
    client, _, _ = ws

    def fail(self, *args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(Path, "mkdir", fail)
    response = client.post(f"{PREFIX}/files/folder", json={"name": "new"})
    assert response.status_code == 500
    assert "disk full" not in response.text


def test_rename_in_place_missing_and_conflict(ws):
    client, workspace, _ = ws
    (workspace / "docs" / "other.md").write_text("x")
    assert client.put(f"{PREFIX}/files/rename", params={"path": "docs/none.md"},
                      json={"new_name": "x.md"}).status_code == 404
    assert client.put(f"{PREFIX}/files/rename", params={"path": "docs/readme.md"},
                      json={"new_name": "other.md"}).status_code == 409
    response = client.put(f"{PREFIX}/files/rename", params={"path": "docs/readme.md"},
                          json={"new_name": "renamed.md"})
    assert response.status_code == 200
    assert response.json() == {"name": "renamed.md", "path": "docs/renamed.md", "type": "file"}
    assert (workspace / "docs" / "renamed.md").read_text() == "hello"


def test_rename_failure_is_500(ws, monkeypatch):
    client, _, _ = ws

    def fail(self, target):
        raise OSError("busy")

    monkeypatch.setattr(Path, "rename", fail)
    response = client.put(f"{PREFIX}/files/rename", params={"path": "docs/readme.md"},
                          json={"new_name": "renamed.md"})
    assert response.status_code == 500


def test_delete_file_and_folder(ws):
    client, workspace, _ = ws
    (workspace / "docs" / "sub").mkdir()
    (workspace / "docs" / "sub" / "a.txt").write_text("a")
    assert client.delete(f"{PREFIX}/files", params={"path": "docs/readme.md"}).json() == {"deleted": "docs/readme.md"}
    assert not (workspace / "docs" / "readme.md").exists()
    assert client.delete(f"{PREFIX}/files", params={"path": "docs"}).status_code == 200
    assert not (workspace / "docs").exists()
    assert client.delete(f"{PREFIX}/files", params={"path": "docs"}).status_code == 404


def test_delete_failure_is_500(ws, monkeypatch):
    client, workspace, _ = ws
    import app.api.workspace_files as mod

    def fail(path):
        raise OSError("busy")

    monkeypatch.setattr(mod.shutil, "rmtree", fail)
    response = client.delete(f"{PREFIX}/files", params={"path": "docs"})
    assert response.status_code == 500
    assert (workspace / "docs").exists()


def test_upload_rejects_dotfile_and_traversal_target(ws):
    client, workspace, outside = ws
    dot = client.post(f"{PREFIX}/upload", files={"file": (".env", b"x", "text/plain")})
    assert dot.status_code == 400
    assert not (workspace / ".env").exists()
    escape = client.post(f"{PREFIX}/upload", files={"file": ("a.txt", b"x", "text/plain")},
                         data={"path": "../outside"})
    assert escape.status_code == 400
    assert sorted(os.listdir(outside)) == ["secret.txt"]


def test_upload_strips_directory_components_from_filename(ws):
    client, workspace, outside = ws
    response = client.post(f"{PREFIX}/upload",
                           files={"file": ("../../outside/evil.txt", b"x", "text/plain")})
    assert response.status_code == 200
    assert response.json()["path"] == "evil.txt"
    assert (workspace / "evil.txt").exists()
    assert not (outside / "evil.txt").exists()
