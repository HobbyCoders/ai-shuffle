"""Projects are Git repository roots, never folders inside a repository (#1604).

Create and update refuse a folder whose enclosing repository root is another
directory and name that root. A repository root and a folder in no repository
stay accepted. Covered in local (absolute path) and Docker (workspace) modes
against real ``git init`` repositories.
"""
import logging
import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_admin, require_auth
from app.api.projects import router
from app.core import project_utils, startup
from app.core.config import settings as app_settings
from app.db import checkouts, database, projects_v2
from tests.shared_fixtures import LOCAL_ME, pin_runner  # noqa: F401


def _git_init(path):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def _same(a, b):
    return os.path.normcase(os.path.realpath(a)) == os.path.normcase(os.path.realpath(b))


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    repo = _git_init(ws / "repo")
    (repo / "frontend").mkdir()
    (ws / "plain").mkdir()
    return ws


@pytest.fixture
def db():
    with patch("app.api.projects.database") as mock_db:
        mock_db.get_project.return_value = None
        mock_db.create_project.side_effect = lambda **kw: {
            "id": kw["project_id"], "name": kw["name"], "description": None,
            "path": kw["path"], "settings": {},
            "created_at": "2026-10-01T00:00:00", "updated_at": "2026-10-01T00:00:00",
        }
        yield mock_db


@pytest.fixture(params=["local", "docker"])
def mode(request):
    return request.param


@pytest.fixture
def client(workspace, db, mode):
    settings = MagicMock()
    settings.workspace_dir = workspace
    settings.effective_workspace_dir = workspace
    with patch("app.api.projects.settings", settings), \
         patch("app.core.project_utils.settings", settings), \
         patch("app.api.projects.is_local_mode", return_value=mode == "local"):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_auth] = lambda: "t"
        app.dependency_overrides[require_admin] = lambda: "t"
        with TestClient(app) as test_client:
            yield test_client


def _path(workspace, mode, rel):
    """The spelling each mode's picker sends: absolute in local mode, and
    workspace-relative in Docker (absolute workspace paths normalize to it)."""
    return str(workspace / rel) if mode == "local" else rel


def _create(client, path, project_id="p1"):
    return client.post("/api/v1/projects", json={"id": project_id, "name": "P", "path": path})


def test_create_refuses_folder_inside_repository_and_names_the_root(client, workspace, mode, db):
    response = _create(client, _path(workspace, mode, "repo/frontend"))
    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "inside the Git repository" in detail
    assert os.path.realpath(workspace / "repo") in detail
    db.create_project.assert_not_called()


def test_create_refuses_new_folder_inside_repository_without_creating_it(client, workspace, mode, db):
    response = _create(client, _path(workspace, mode, "repo/new-sub"))
    assert response.status_code == 400
    assert not (workspace / "repo" / "new-sub").exists()
    db.create_project.assert_not_called()


@pytest.mark.parametrize("rel", ["repo", "plain", "brand-new"])
def test_create_accepts_repository_root_and_non_git_folders(client, workspace, mode, rel):
    response = _create(client, _path(workspace, mode, rel))
    assert response.status_code == 201, response.text
    assert (workspace / rel).is_dir()


def test_create_refuses_absolute_workspace_path_inside_repository_in_docker(workspace, db):
    """Docker mode also receives absolute workspace paths from the picker."""
    settings = MagicMock()
    settings.workspace_dir = workspace
    settings.effective_workspace_dir = workspace
    with patch("app.api.projects.settings", settings), \
         patch("app.core.project_utils.settings", settings), \
         patch("app.api.projects.is_local_mode", return_value=False):
        app = FastAPI()
        app.include_router(router)
        app.dependency_overrides[require_admin] = lambda: "t"
        with TestClient(app) as test_client:
            response = _create(test_client, str(workspace / "repo" / "frontend"))
    assert response.status_code == 400
    assert "inside the Git repository" in response.json()["detail"]


@pytest.fixture
def registered_project(mock_db, workspace, mode, monkeypatch):
    """Use the real writer: retarget requires an owned project and its base checkout."""
    monkeypatch.setattr(type(app_settings), "effective_workspace_dir", property(lambda _: workspace))
    monkeypatch.setattr("app.api.projects.is_local_mode", lambda: mode == "local")
    # The edit announces to peers after commit; this client has no peer transport.
    monkeypatch.setattr("app.api.projects._announce_catalog_change", lambda: None)
    (workspace / "elsewhere").mkdir()
    row = database.create_project("p1", "P", None, _path(workspace, mode, "elsewhere"))
    identity = projects_v2.get_project_v2_by_origin("local:p1")["id"]
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: "t"
    with TestClient(app) as test_client:
        yield test_client, row, identity


def test_update_refuses_moving_a_project_inside_a_repository(registered_project, workspace, mode):
    client, row, identity = registered_project
    base = checkouts.get_checkout(identity, LOCAL_ME)
    response = client.put("/api/v1/projects/p1", json={"path": _path(workspace, mode, "repo/frontend")})
    assert response.status_code == 400
    assert os.path.realpath(workspace / "repo") in response.json()["detail"]
    assert database.get_project("p1") == row
    assert checkouts.get_checkout(identity, LOCAL_ME) == base


@pytest.mark.parametrize("rel", ["repo", "plain"])
def test_update_accepts_repository_root_and_non_git_folder(registered_project, workspace, mode, rel):
    client, _, identity = registered_project
    base = checkouts.get_checkout(identity, LOCAL_ME)
    path = _path(workspace, mode, rel)
    response = client.put("/api/v1/projects/p1", json={"path": path})
    assert response.status_code == 200, response.text
    assert response.json()["path"] == database.get_project("p1")["path"] == path
    updated_base = checkouts.get_checkout(identity, LOCAL_ME)
    assert updated_base["id"] == base["id"]
    assert _same(updated_base["path"], workspace / rel)


def test_update_resending_an_existing_nested_path_is_not_refused(client, workspace, mode, db):
    """Existing nested projects are reported, not changed or locked."""
    stored = _path(workspace, mode, "repo/frontend")
    db.get_project.return_value = {"id": "p1", "path": stored}
    db.update_project.side_effect = lambda **kw: {
        "id": "p1", "name": kw["name"], "description": None, "path": stored, "settings": {},
        "created_at": "2026-10-01T00:00:00", "updated_at": "2026-10-01T00:00:00",
    }
    response = client.put("/api/v1/projects/p1", json={"name": "Renamed", "path": stored})
    assert response.status_code == 200, response.text


def test_enclosing_repo_root(workspace):
    repo = workspace / "repo"
    assert _same(project_utils.enclosing_repo_root(repo / "frontend"), repo)
    assert _same(project_utils.enclosing_repo_root(repo / "missing" / "deeper"), repo)
    assert project_utils.enclosing_repo_root(repo) is None
    assert project_utils.enclosing_repo_root(workspace / "plain") is None


def test_enclosing_repo_root_accepts_when_git_cannot_answer(workspace, monkeypatch):
    from app.core.git_service import git_service

    def missing_git(*args, **kwargs):
        raise FileNotFoundError("git")
    monkeypatch.setattr(git_service, "_run_git", missing_git)
    assert project_utils.enclosing_repo_root(workspace / "repo" / "frontend") is None


def test_startup_reports_existing_nested_projects_without_changing_them(workspace, monkeypatch, caplog):
    rows = [
        {"id": "nested", "path": str(workspace / "repo" / "frontend")},
        {"id": "root", "path": str(workspace / "repo")},
        {"id": "plain", "path": "plain"},
        {"id": "gone", "path": str(workspace / "missing")},
    ]
    settings = MagicMock()
    settings.effective_workspace_dir = workspace
    monkeypatch.setattr(project_utils, "settings", settings)
    monkeypatch.setattr(startup.database, "get_all_projects", lambda: rows)
    writes = MagicMock()
    monkeypatch.setattr(startup.database, "update_project", writes)
    with caplog.at_level(logging.WARNING, logger="app.core.startup"):
        assert startup.report_nested_projects() == 1
    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 1 and "Project nested" in messages[0]
    assert os.path.realpath(workspace / "repo") in messages[0]
    writes.assert_not_called()
