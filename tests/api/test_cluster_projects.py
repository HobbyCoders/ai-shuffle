"""``GET /api/v1/cluster/projects`` — origin-keyed catalog of projects_v2
with at least one active base checkout (PR 8).

Does **not** call ``require_origin_access`` (XOR 400 when both ids are
absent). Scoped users filter by ``assigned_origin``. A scoped user whose
assigned project is gone gets an empty list, not every origin.
"""

from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.auth import require_auth
from app.api.cluster import router
from app.core.config import settings
from datetime import datetime, timezone

from app.db import checkouts as checkouts_db
from app.db import database
from app.db import projects as projects_db
from app.db import projects_v2
from app.db.base import get_db


ME = "local-me"
PEER = "peer-install-1"
ORIGIN = "git:example.com/shared.git"
OTHER_ORIGIN = "git:example.com/other.git"


@pytest.fixture(autouse=True)
def pin_runner(monkeypatch):
    monkeypatch.setattr(
        type(settings), "effective_instance_id", property(lambda self: ME)
    )


@pytest.fixture
def app(mock_db):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: "test-session-token"
    return app


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def anon_client(mock_db):
    unauthed = FastAPI()
    unauthed.include_router(router)
    with TestClient(unauthed) as test_client:
        yield test_client


def _seed_installs():
    database.register_install(ME, "This Mac", "http://me:8000", "darwin", "1", {})
    database.register_install(PEER, "Studio", "http://peer:8000", "win32", "1", {})


def test_auth_is_required(anon_client):
    assert anon_client.get("/api/v1/cluster/projects").status_code == 401


def test_omits_pv2_with_no_base_checkout(client, mock_db):
    _seed_installs()
    projects_v2.upsert_project(ORIGIN, name="Shared")
    resp = client.get("/api/v1/cluster/projects")
    assert resp.status_code == 200
    assert resp.json()["projects"] == []


def test_includes_remote_only_and_does_not_expose_path(client, mock_db, tmp_path):
    _seed_installs()
    pv2 = projects_v2.upsert_project(ORIGIN, name="Shared")
    checkouts_db.upsert_checkout(pv2["id"], PEER, str(tmp_path / "peer-repo"))
    resp = client.get("/api/v1/cluster/projects")
    assert resp.status_code == 200
    rows = resp.json()["projects"]
    assert len(rows) == 1
    row = rows[0]
    assert row["project_v2_id"] == pv2["id"]
    assert row["origin"] == ORIGIN
    assert row["name"] == "Shared"
    assert row["local_project_id"] is None
    assert row["remote_only"] is True
    assert "path" not in row
    assert row["eligible"] == [
        {
            "install_id": PEER,
            "device_name": "Studio",
            "online": True,
            "is_self": False,
        }
    ]


def test_peer_projects_row_same_origin_is_not_local(client, mock_db, tmp_path):
    """A peer ``projects`` row for the same origin must not become
    ``local_project_id`` and must not leak that machine's ``path``."""
    _seed_installs()
    peer_path = tmp_path / "peer-clone"
    peer_path.mkdir()
    now = datetime.now(timezone.utc).isoformat()
    with get_db() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO projects "
            "(id, name, path, settings, instance_id, created_at, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            ("peer-proj", "Peer clone", str(peer_path), "{}", PEER, now, now),
        )
    projects_db.set_project_link_group("peer-proj", ORIGIN)
    pv2 = projects_v2.upsert_project(ORIGIN, name="Shared")
    checkouts_db.upsert_checkout(pv2["id"], PEER, str(peer_path))

    resp = client.get("/api/v1/cluster/projects")
    assert resp.status_code == 200
    rows = resp.json()["projects"]
    assert len(rows) == 1
    row = rows[0]
    assert row["local_project_id"] is None
    assert row["remote_only"] is True
    assert "path" not in row
    assert str(peer_path) not in str(row)


def test_local_project_id_when_this_install_has_a_row(client, mock_db, tmp_path):
    _seed_installs()
    path = tmp_path / "repo"
    path.mkdir()
    database.create_project(
        project_id="proj-1", name="Local", description=None, path=str(path)
    )
    projects_db.set_project_link_group("proj-1", ORIGIN)
    pv2 = projects_v2.upsert_project(ORIGIN, name="Shared")
    checkouts_db.upsert_checkout(pv2["id"], ME, str(path))
    resp = client.get("/api/v1/cluster/projects")
    row = resp.json()["projects"][0]
    assert row["local_project_id"] == "proj-1"
    assert row["remote_only"] is False
    assert "path" not in row


def test_branch_only_checkout_does_not_list_the_project(client, mock_db, tmp_path):
    _seed_installs()
    pv2 = projects_v2.upsert_project(ORIGIN, name="Shared")
    checkouts_db.upsert_checkout(
        pv2["id"], PEER, str(tmp_path / "feat"), branch="feature-x"
    )
    resp = client.get("/api/v1/cluster/projects")
    assert resp.json()["projects"] == []


def test_does_not_call_require_origin_access(client, mock_db):
    _seed_installs()
    with patch("app.api.cluster.require_origin_access") as xor:
        resp = client.get("/api/v1/cluster/projects")
    assert resp.status_code == 200
    xor.assert_not_called()


def test_scoped_user_sees_only_their_origin(client, mock_db, tmp_path):
    _seed_installs()
    path = tmp_path / "assigned"
    path.mkdir()
    database.create_project(
        project_id="proj-assigned", name="Assigned", description=None, path=str(path)
    )
    projects_db.set_project_link_group("proj-assigned", ORIGIN)
    mine = projects_v2.upsert_project(ORIGIN, name="Mine")
    other = projects_v2.upsert_project(OTHER_ORIGIN, name="Other")
    checkouts_db.upsert_checkout(mine["id"], PEER, str(tmp_path / "mine"))
    checkouts_db.upsert_checkout(other["id"], PEER, str(tmp_path / "other"))

    with patch(
        "app.api.cluster.get_api_user_from_request",
        return_value={"id": "user-1", "project_id": "proj-assigned"},
    ):
        resp = client.get("/api/v1/cluster/projects")
    assert resp.status_code == 200
    rows = resp.json()["projects"]
    assert [r["origin"] for r in rows] == [ORIGIN]
    assert rows[0]["project_v2_id"] == mine["id"]


def test_scoped_user_whose_assigned_project_is_gone_gets_empty_list(
    client, mock_db, tmp_path
):
    """Gone assigned project is empty, not unscoped (would list every origin)."""
    _seed_installs()
    pv2 = projects_v2.upsert_project(ORIGIN, name="Shared")
    checkouts_db.upsert_checkout(pv2["id"], PEER, str(tmp_path / "repo"))
    with patch(
        "app.api.cluster.get_api_user_from_request",
        return_value={"id": "user-1", "project_id": "missing-project"},
    ):
        resp = client.get("/api/v1/cluster/projects")
    assert resp.status_code == 200
    assert resp.json()["projects"] == []


def test_unscoped_api_user_sees_every_origin(client, mock_db, tmp_path):
    _seed_installs()
    a = projects_v2.upsert_project(ORIGIN, name="A")
    b = projects_v2.upsert_project(OTHER_ORIGIN, name="B")
    checkouts_db.upsert_checkout(a["id"], PEER, str(tmp_path / "a"))
    checkouts_db.upsert_checkout(b["id"], PEER, str(tmp_path / "b"))
    with patch(
        "app.api.cluster.get_api_user_from_request",
        return_value={"id": "user-1", "project_id": None},
    ):
        resp = client.get("/api/v1/cluster/projects")
    origins = {r["origin"] for r in resp.json()["projects"]}
    assert origins == {ORIGIN, OTHER_ORIGIN}
