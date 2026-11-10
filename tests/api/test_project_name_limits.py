"""A project name the API cannot return must never break the project list (#1359).

``PUT /projects/{id}`` used to accept any name, store it, and then fail
building its own response because the response model limited the name to
1-100 characters. Every later ``GET /projects`` failed the same way, so the
project list stopped loading until the row was corrected by hand.

These tests run the real routes against a real (per-test) database, so they
cover both halves: the update refuses a name outside the limits before
anything is written, and a row that already holds such a name is still listed
and can be renamed.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.auth import require_admin, require_auth
from app.api.projects import router as projects_router
from app.db import database


PROJECT_ID = "named"
GOOD_NAME = "Named project"


@pytest.fixture
def client(mock_db, tmp_path):
    app = FastAPI()
    app.include_router(projects_router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    app.dependency_overrides[require_admin] = lambda: "test-admin-token"
    path = tmp_path / PROJECT_ID
    path.mkdir()
    database.create_project(
        project_id=PROJECT_ID, name=GOOD_NAME, description=None, path=str(path)
    )
    # A failed response must come back as a 500, not as a raised exception,
    # so the tests see what a browser would.
    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


def _listed_names(client):
    response = client.get("/api/v1/projects")
    assert response.status_code == 200, response.text
    return [project["name"] for project in response.json()]


@pytest.mark.parametrize(
    "bad_name", ["", "x" * 101, "x" * 500], ids=["empty", "101-chars", "500-chars"]
)
def test_update_refuses_a_name_outside_the_limits_and_writes_nothing(client, bad_name):
    response = client.put(f"/api/v1/projects/{PROJECT_ID}", json={"name": bad_name})

    assert response.status_code == 422
    assert database.get_project(PROJECT_ID)["name"] == GOOD_NAME
    assert _listed_names(client) == [GOOD_NAME]


def test_update_accepts_a_name_at_the_limit(client):
    longest = "x" * 100

    response = client.put(f"/api/v1/projects/{PROJECT_ID}", json={"name": longest})

    assert response.status_code == 200
    assert response.json()["name"] == longest
    assert _listed_names(client) == [longest]


def test_update_without_a_name_keeps_the_name(client):
    response = client.put(
        f"/api/v1/projects/{PROJECT_ID}", json={"description": "still named"}
    )

    assert response.status_code == 200
    assert response.json()["name"] == GOOD_NAME
    assert response.json()["description"] == "still named"


@pytest.mark.parametrize("stored_name", ["x" * 101, "x" * 500], ids=["101-chars", "500-chars"])
def test_an_already_saved_over_long_name_is_listed_and_can_be_renamed(client, stored_name):
    # A row written before the limit was enforced (or by another worker's
    # older build through sync) still holds the name.
    database.update_project(project_id=PROJECT_ID, name=stored_name)

    assert _listed_names(client) == [stored_name]
    detail = client.get(f"/api/v1/projects/{PROJECT_ID}")
    assert detail.status_code == 200
    assert detail.json()["name"] == stored_name

    # Editing something else must not be blocked by the old name...
    described = client.put(
        f"/api/v1/projects/{PROJECT_ID}", json={"description": "kept"}
    )
    assert described.status_code == 200
    assert described.json()["name"] == stored_name

    # ...and the name can be brought back inside the limit.
    renamed = client.put(f"/api/v1/projects/{PROJECT_ID}", json={"name": GOOD_NAME})
    assert renamed.status_code == 200
    assert _listed_names(client) == [GOOD_NAME]
