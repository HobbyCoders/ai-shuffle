"""Status codes of /api/v1/network routes on a worker that is not (fully) in a network."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import network as network_api
from app.api.auth import require_admin, require_auth
from app.db import database
from app.network import engine, hub
from tests.core.network.conftest import workers  # noqa: F401


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(network_api.router)
    app.dependency_overrides[require_admin] = lambda: "admin"
    app.dependency_overrides[require_auth] = lambda: "admin"
    yield TestClient(app, raise_server_exceptions=False)
    hub.reset_for_tests()


def test_recovery_key_before_joining_is_a_conflict_not_a_server_error(workers, client):  # noqa: F811
    a = workers("a")
    with a.active():
        assert client.get("/api/v1/network/recovery").json()["configured"] is False
        made = client.post("/api/v1/network/recovery")
        assert made.status_code == 409, made.text
        assert "not joined" in made.json()["detail"]


def test_hydrate_before_joining_is_a_conflict(workers, client):  # noqa: F811
    a = workers("a")
    with a.active():
        database.create_session(session_id="s1", profile_id="default", title="t")
        answer = client.post("/api/v1/network/runners/dev_other/sessions/s1/hydrate")
        assert answer.status_code == 409, answer.text


def test_unlinked_and_unenrolled_worker_route_errors(workers, client):  # noqa: F811
    a = workers("a")
    with a.active():
        assert client.get("/api/v1/network/hub/devices").status_code == 503
        assert client.post("/api/v1/network/hub/devices/dev_x/approve", json={}).status_code == 400
        assert client.post("/api/v1/network/hub/devices/dev_x/reject").status_code == 400
        assert client.post("/api/v1/network/hub/adopt", json={}).status_code == 409
        assert client.post("/api/v1/network/owners", json={"table": "automations", "id": "x"}).status_code == 409
        assert client.delete("/api/v1/network/members/dev_x").status_code == 404
        assert client.delete("/api/v1/network/conflicts/12345").status_code == 404
        assert client.post("/api/v1/network/enroll", json={}).status_code == 400
        assert client.post("/api/v1/network/enroll", json={"link": "not a link"}).status_code == 400
        assert client.get("/api/v1/network/members").json() == {"members": []}
        assert client.get("/api/v1/network/runners").json()["enrolled"] is False
        engine.create_network("Home", device_name="A")
        assert client.post("/api/v1/network/enroll", json={
            "endpoint_url": "http://b.local", "code": "x"}).status_code == 409
        assert client.patch("/api/v1/network/members/dev_x", json={"name": "N"}).status_code == 404
