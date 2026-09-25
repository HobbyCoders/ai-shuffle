"""Project file upload, rename, folder create and delete refuse ``.git``.

``.git`` controls what the app's own git runs execute (hooks, config), so an
API key limited to a project must not be able to write into it through the
file routes, in any spelling git itself treats as ``.git`` (case, trailing
dots or spaces, an NTFS stream suffix, the 8.3 name ``GIT~1``).
"""

from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_admin, require_auth
from app.api.projects import router

DOTGIT_SPELLINGS = [".git", ".GIT", ".git.", ".git ", ".git::$INDEX_ALLOCATION", "GIT~1", "git~1"]
DETAIL = "Path is inside the repository metadata directory (.git)"


@pytest.fixture
def project_dir(temp_dir):
    project_dir = temp_dir / "test-project"
    (project_dir / ".git" / "hooks").mkdir(parents=True)
    (project_dir / "src").mkdir()
    (project_dir / "src" / "a.txt").write_text("a")
    return project_dir


@pytest.fixture
def client(temp_dir, project_dir):
    settings = MagicMock(workspace_dir=temp_dir, effective_workspace_dir=temp_dir)
    project = {"id": "test-project", "name": "P", "path": "test-project", "settings": {}}
    with patch("app.api.projects.database") as db, \
         patch("app.api.projects.get_api_user_from_request", return_value={"project_id": "test-project"}), \
         patch("app.api.common.get_api_user_from_request", return_value={"project_id": "test-project"}), \
         patch("app.api.projects.settings", settings), \
         patch("app.core.project_utils.settings", settings):
        db.get_project.return_value = project
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_auth] = lambda: "token"
        app.dependency_overrides[require_admin] = lambda: "token"
        with TestClient(app) as c:
            yield c


def _upload(client, path, filename="pre-commit"):
    return client.post(
        "/api/v1/projects/test-project/upload",
        files={"file": (filename, BytesIO(b"#!/bin/sh\necho pwned\n"), "text/plain")},
        data={"path": path},
    )


@pytest.mark.parametrize("dotgit", DOTGIT_SPELLINGS)
@pytest.mark.parametrize("template", ["{}/hooks", "{}", "src/{}/hooks"])
def test_upload_refuses_git_metadata_destination(client, project_dir, dotgit, template):
    response = _upload(client, template.format(dotgit))

    assert response.status_code == 400
    assert response.json()["detail"] == DETAIL
    assert not (project_dir / ".git" / "hooks" / "pre-commit").exists()
    assert not (project_dir / "src" / ".git").exists()


def test_upload_refuses_git_short_name_as_filename(client, project_dir):
    assert _upload(client, "", filename="GIT~1").status_code == 400


def test_upload_elsewhere_still_works(client, project_dir):
    response = _upload(client, "src/hooks")

    assert response.status_code == 200
    assert (project_dir / "src" / "hooks" / "pre-commit").exists()


def _rename(client, path, new_name):
    return client.put(
        "/api/v1/projects/test-project/files/rename", params={"path": path}, json={"new_name": new_name}
    )


@pytest.mark.parametrize("dotgit", DOTGIT_SPELLINGS)
def test_rename_refuses_moving_into_git_metadata(client, project_dir, dotgit):
    (project_dir / "hooks").mkdir()
    (project_dir / "hooks" / "pre-commit").write_text("x")

    response = _rename(client, "hooks", dotgit)

    assert response.status_code == 400
    assert response.json()["detail"] == DETAIL
    assert (project_dir / "hooks").is_dir()


@pytest.mark.parametrize("path", [".git", ".git/hooks", ".GIT/config"])
def test_rename_refuses_git_metadata_source(client, project_dir, path):
    (project_dir / ".git" / "config").write_text("[core]\n")

    response = _rename(client, path, "moved")

    assert response.status_code == 400
    assert (project_dir / ".git" / "hooks").is_dir()
    assert (project_dir / ".git" / "config").exists()


def test_rename_refuses_git_metadata_reached_through_a_link(client, project_dir):
    (project_dir / "meta").symlink_to(project_dir / ".git", target_is_directory=True)

    response = _rename(client, "meta/hooks", "moved")

    assert response.status_code == 400
    assert (project_dir / ".git" / "hooks").is_dir()


def test_rename_elsewhere_still_works(client, project_dir):
    response = _rename(client, "src/a.txt", "b.txt")

    assert response.status_code == 200
    assert (project_dir / "src" / "b.txt").read_text() == "a"


def _mkdir(client, path, name):
    return client.post(
        "/api/v1/projects/test-project/files/folder", params={"path": path}, json={"name": name}
    )


@pytest.mark.parametrize("dotgit", DOTGIT_SPELLINGS)
def test_create_folder_refuses_git_metadata_parent(client, project_dir, dotgit):
    response = _mkdir(client, f"{dotgit}/hooks", "pre-commit")

    assert response.status_code == 400
    assert response.json()["detail"] == DETAIL
    assert not (project_dir / ".git" / "hooks" / "pre-commit").exists()


@pytest.mark.parametrize("dotgit", DOTGIT_SPELLINGS)
def test_create_folder_refuses_git_metadata_name(client, project_dir, dotgit):
    response = _mkdir(client, "src", dotgit)

    assert response.status_code == 400
    assert response.json()["detail"] == DETAIL
    assert not (project_dir / "src" / ".git").exists()


def test_create_folder_refuses_git_metadata_reached_through_a_link(client, project_dir):
    (project_dir / "meta").symlink_to(project_dir / ".git", target_is_directory=True)

    response = _mkdir(client, "meta/hooks", "pre-commit")

    assert response.status_code == 400
    assert response.json()["detail"] == DETAIL
    assert not (project_dir / ".git" / "hooks" / "pre-commit").exists()


def test_create_folder_elsewhere_still_works(client, project_dir):
    response = _mkdir(client, "src", "hooks")

    assert response.status_code == 200
    assert (project_dir / "src" / "hooks").is_dir()


def _delete(client, path):
    return client.delete("/api/v1/projects/test-project/files", params={"path": path})


@pytest.mark.parametrize("path", [".git", ".git/hooks", ".GIT/config", "src/../.git", "GIT~1/config"])
def test_delete_refuses_git_metadata(client, project_dir, path):
    (project_dir / ".git" / "config").write_text("[core]\n")

    response = _delete(client, path)

    assert response.status_code == 400
    assert response.json()["detail"] == DETAIL
    assert (project_dir / ".git" / "hooks").is_dir()
    assert (project_dir / ".git" / "config").exists()


def test_delete_refuses_git_metadata_reached_through_a_link(client, project_dir):
    (project_dir / "meta").symlink_to(project_dir / ".git", target_is_directory=True)

    response = _delete(client, "meta/hooks")

    assert response.status_code == 400
    assert response.json()["detail"] == DETAIL
    assert (project_dir / ".git" / "hooks").is_dir()


def test_delete_of_a_link_to_git_metadata_removes_only_the_link(client, project_dir):
    (project_dir / "meta").symlink_to(project_dir / ".git", target_is_directory=True)

    response = _delete(client, "meta")

    assert response.status_code == 200
    assert not (project_dir / "meta").is_symlink()
    assert (project_dir / ".git" / "hooks").is_dir()


def test_delete_elsewhere_still_works(client, project_dir):
    response = _delete(client, "src/a.txt")

    assert response.status_code == 200
    assert not (project_dir / "src" / "a.txt").exists()
