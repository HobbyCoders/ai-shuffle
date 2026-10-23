"""A deleted project must leave every project selector (#434).

The selectors merge this worker's ``GET /projects`` with the origin-keyed
``GET /cluster/projects`` catalog. Deleting a project removed only its legacy
``projects`` row; its ``projects_v2`` identity and this worker's base
``checkouts`` row stayed active, so the catalog kept offering the deleted
project as one "available on this device" and every picker listed it again.

Deleting must not hard-delete the shared identity or the checkout record: a
chat still bound to the project has to keep opening in its folder.
"""

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.auth import require_admin, require_auth
from app.api.cluster import router as cluster_router
from app.api.projects import router as projects_router
from app.core import project_resolver
from app.db import checkouts as checkouts_db
from app.db import database
from app.db import projects_v2
from tests.shared_fixtures import LOCAL_ME as ME, pin_runner  # noqa: F401  (autouse fixture)


PROJECT_ID = "doomed"


@pytest.fixture
def client(mock_db):
    app = FastAPI()
    app.include_router(cluster_router)
    app.include_router(projects_router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    app.dependency_overrides[require_admin] = lambda: "test-admin-token"
    database.register_install(ME, "This Mac", "http://me:8000", "darwin", "1", {}, approve_new=True)
    with TestClient(app) as test_client:
        yield test_client


def _seed(tmp_path, project_id=PROJECT_ID, name="Doomed"):
    """A project that has had a chat: the chat seeds its origin-keyed identity
    and this worker's base checkout, exactly as a real first send does."""
    path = tmp_path / project_id
    path.mkdir()
    database.create_project(project_id=project_id, name=name, description=None, path=str(path))
    pv2 = project_resolver.resolve_project_v2({"project_id": project_id})
    assert pv2 is not None
    return pv2, path


def _catalog_ids(client):
    response = client.get("/api/v1/cluster/projects")
    assert response.status_code == 200
    return [row["project_v2_id"] for row in response.json()["projects"]]


def test_deleted_project_leaves_the_cluster_catalog(client, tmp_path):
    pv2, _ = _seed(tmp_path)
    assert _catalog_ids(client) == [pv2["id"]]

    assert client.delete(f"/api/v1/projects/{PROJECT_ID}").status_code == 204

    assert [p["id"] for p in client.get("/api/v1/projects").json()] == []
    assert _catalog_ids(client) == []


def test_delete_keeps_the_shared_identity_and_checkout_record(client, tmp_path):
    pv2, path = _seed(tmp_path)

    assert client.delete(f"/api/v1/projects/{PROJECT_ID}").status_code == 204

    # Nothing is hard-deleted: the identity and the checkout row survive, the
    # checkout is only retired from the catalog.
    assert projects_v2.get_project_v2(pv2["id"]) is not None
    rows = checkouts_db.list_checkouts(pv2["id"])
    assert [row["path"] for row in rows] == [str(path)]
    assert rows[0]["status"] == checkouts_db.REMOVED_STATUS
    assert checkouts_db.eligible_base_project_ids(ME) == []


def test_a_chat_still_bound_to_the_deleted_project_opens_without_resurrecting_it(client, tmp_path):
    pv2, path = _seed(tmp_path)
    assert client.delete(f"/api/v1/projects/{PROJECT_ID}").status_code == 204

    # A chat bound by project identity alone (its legacy row is gone) still
    # resolves its folder...
    assert project_resolver.resolve_cwd({"id": "survivor", "project_v2_id": pv2["id"]}) == str(path)
    # ...without putting the deleted project back in the selectors.
    assert _catalog_ids(client) == []


def test_another_clone_of_the_same_origin_keeps_it_listed(client, tmp_path):
    pv2, _ = _seed(tmp_path)
    database.set_project_link_group(PROJECT_ID, pv2["origin"])
    twin = tmp_path / "twin"
    twin.mkdir()
    database.create_project(project_id="twin", name="Twin", description=None, path=str(twin))
    database.set_project_link_group("twin", pv2["origin"])

    assert client.delete(f"/api/v1/projects/{PROJECT_ID}").status_code == 204

    rows = client.get("/api/v1/cluster/projects").json()["projects"]
    assert [(row["project_v2_id"], row["local_project_id"]) for row in rows] == [(pv2["id"], "twin")]


def test_re_adding_the_project_lists_it_again(client, tmp_path):
    pv2, path = _seed(tmp_path)
    assert client.delete(f"/api/v1/projects/{PROJECT_ID}").status_code == 204
    assert _catalog_ids(client) == []

    database.create_project(project_id=PROJECT_ID, name="Doomed", description=None, path=str(path))

    rows = client.get("/api/v1/cluster/projects").json()["projects"]
    assert [(row["project_v2_id"], row["local_project_id"]) for row in rows] == [(pv2["id"], PROJECT_ID)]


def test_re_adding_a_linked_clone_lists_it_again_once_its_remote_is_read(client, tmp_path):
    origin = "git:example.com/doomed.git"
    path = tmp_path / PROJECT_ID
    path.mkdir()
    database.create_project(project_id=PROJECT_ID, name="Doomed", description=None, path=str(path))
    database.set_project_link_group(PROJECT_ID, origin)
    pv2 = project_resolver.resolve_project_v2({"project_id": PROJECT_ID})
    assert pv2["origin"] == origin
    assert client.delete(f"/api/v1/projects/{PROJECT_ID}").status_code == 204
    assert _catalog_ids(client) == []

    # Re-added without a group; the git poll sets it afterwards.
    database.create_project(project_id=PROJECT_ID, name="Doomed", description=None, path=str(path))
    database.set_project_link_group(PROJECT_ID, origin)

    assert _catalog_ids(client) == [pv2["id"]]


def test_refreshing_a_removed_checkout_does_not_reactivate_it(mock_db, tmp_path):
    pv2 = projects_v2.upsert_project("local:x", name="X")
    path = str(tmp_path / "x")
    checkouts_db.upsert_checkout(pv2["id"], ME, path, status=checkouts_db.REMOVED_STATUS)

    row = checkouts_db.upsert_checkout(pv2["id"], ME, path, head_sha="abc", status="active")

    assert row["status"] == checkouts_db.REMOVED_STATUS
    assert row["head_sha"] == "abc"
    # An explicit non-active status still lands (archiving is not blocked).
    assert checkouts_db.upsert_checkout(pv2["id"], ME, path, status="archived")["status"] == "archived"


def test_deleting_one_project_leaves_the_others_listed(client, tmp_path):
    doomed, _ = _seed(tmp_path)
    kept, _ = _seed(tmp_path, project_id="kept", name="Kept")

    assert client.delete(f"/api/v1/projects/{PROJECT_ID}").status_code == 204

    assert _catalog_ids(client) == [kept["id"]]
    assert doomed["id"] != kept["id"]
