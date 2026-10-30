"""A project linked to its remote after its first chat is listed once (#802).

A project's first chat seeds its origin-keyed identity under the synthesized
``local:<id>`` origin while the project has no remote yet. When the Git panel's
first poll then records the repository and links the project to its remote,
the project's origin changes, but the ``local:<id>`` identity and this
worker's base checkout of it stay. The catalog then offered that identity as a
second, remote-only copy of the project, and the composer's project picker
listed the project twice.
"""

import subprocess

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api import git_helpers
from app.api.auth import require_admin, require_auth
from app.api.cluster import router as cluster_router
from app.api.projects import router as projects_router
from app.core import project_resolver
from app.db import database
from tests.shared_fixtures import LOCAL_ME as ME, pin_runner  # noqa: F401  (autouse fixture)


PROJECT_ID = "sandbox"


@pytest.fixture
def client(mock_db):
    app = FastAPI()
    app.include_router(cluster_router)
    app.include_router(projects_router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    app.dependency_overrides[require_admin] = lambda: "test-admin-token"
    database.register_install(ME, "This Mac", "http://me:8000", "darwin", "1", {}, approve_new=True)
    git_helpers._repo_record_cache.clear()
    with TestClient(app) as test_client:
        yield test_client
    git_helpers._repo_record_cache.clear()


def _repository(path):
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "remote", "add", "origin", "https://github.com/acme/sandbox.git"],
        check=True,
    )


def test_project_linked_to_its_remote_after_its_first_chat_is_listed_once(client, tmp_path):
    path = tmp_path / PROJECT_ID
    _repository(path)
    database.create_project(project_id=PROJECT_ID, name="Sandbox", description=None, path=str(path))
    # The first chat, before any Git status poll: no remote is known yet.
    first = project_resolver.resolve_project_v2({"project_id": PROJECT_ID})
    assert first["origin"] == f"local:{PROJECT_ID}"

    # The Git panel's first poll records the repository and links the remote.
    assert git_helpers.ensure_git_repository_record(PROJECT_ID, str(path)) is not None
    assert database.get_project(PROJECT_ID)["link_group_id"]
    # A later chat resolves the project under its remote origin.
    project_resolver.resolve_project_v2({"project_id": PROJECT_ID})

    local = client.get("/api/v1/projects").json()
    assert [p["id"] for p in local] == [PROJECT_ID]
    catalog = client.get("/api/v1/cluster/projects").json()["projects"]
    # Every catalog row the picker shows beside the local list is this project;
    # none is a remote-only copy of it.
    assert [row for row in catalog if row["local_project_id"] != PROJECT_ID] == []
    assert len(catalog) <= 1
