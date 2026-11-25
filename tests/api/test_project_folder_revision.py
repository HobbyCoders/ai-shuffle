"""A folder save from an editor opened on an older revision never wins silently.

``PUT /projects/{id}`` takes the ``folder_revision`` the editor opened on as
``expected_revision`` (the Workers folder endpoint's token). A stale one is
refused with 409 and nothing is written; omitting it keeps the old behaviour.
"""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_admin, require_auth
from app.api.projects import router
from app.core.config import settings
from app.core.project_folders import STALE_FOLDER
from app.db import checkouts, database, projects_v2
from tests.shared_fixtures import LOCAL_ME as ME, pin_runner  # noqa: F401  (autouse fixture)


@pytest.fixture
def setup(mock_db, tmp_path, monkeypatch):
    monkeypatch.setattr(type(settings), "effective_workspace_dir", property(lambda _: tmp_path))
    monkeypatch.setattr("app.api.projects.is_local_mode", lambda: False)
    monkeypatch.setattr("app.api.projects._announce_catalog_change", lambda: None)
    for name in ("a", "b", "c"):
        (tmp_path / name).mkdir()
    database.create_project("p1", "P", None, "a")
    identity = projects_v2.get_project_v2_by_origin("local:p1")["id"]
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "t"
    app.dependency_overrides[require_admin] = lambda: "t"
    with TestClient(app) as client:
        yield client, identity, tmp_path


def _revision(client) -> str:
    detail = client.get("/api/v1/projects/p1").json()["folder_revision"]
    listed = next(p for p in client.get("/api/v1/projects").json() if p["id"] == "p1")
    assert listed["folder_revision"] == detail
    assert isinstance(detail, str) and len(detail) == 64
    return detail


def test_a_save_from_an_editor_opened_before_another_save_is_refused(setup):
    client, identity, ws = setup
    opened = _revision(client)  # both windows open the editor on folder a

    # One window saves folder b (an older client: no revision sent).
    moved = client.put("/api/v1/projects/p1", json={"path": "b"})
    assert moved.status_code == 200, moved.text
    assert moved.json()["folder_revision"] == _revision(client) != opened

    # The other window, its catalog not yet refreshed, saves folder c.
    stale = client.put("/api/v1/projects/p1", json={"path": "c", "expected_revision": opened})
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"] == STALE_FOLDER
    assert database.get_project("p1")["path"] == "b"
    assert checkouts.get_checkout(identity, ME)["path"] == str(ws / "b")

    # A metadata change with the stale revision is refused too, and writes nothing.
    renamed = client.put("/api/v1/projects/p1", json={"name": "Renamed", "expected_revision": opened})
    assert renamed.status_code == 409
    assert database.get_project("p1")["name"] == "P"

    # Reopened on the current revision, the save goes through.
    current = _revision(client)
    saved = client.put("/api/v1/projects/p1", json={"path": "c", "expected_revision": current})
    assert saved.status_code == 200, saved.text
    assert database.get_project("p1")["path"] == "c"


def test_an_invalid_revision_is_a_validation_error(setup):
    client, _, _ = setup
    assert client.put("/api/v1/projects/p1", json={"path": "b", "expected_revision": "short"}).status_code == 422
    assert database.get_project("p1")["path"] == "a"
