"""Account registration and device approval without cloud data storage."""
import pytest

from app.core import account_hub
from app.db import database
from app.db import mobile_hub as binding_store
from app.db import network as store
from app.network import engine, hub, tokens, transport
from tests.core.network.fake_hub import FakeHub
from tests.core.network.harness import Worker


@pytest.fixture
def workers(tmp_path):
    made = []

    def make(name):
        worker = Worker(name, tmp_path)
        made.append(worker)
        return worker

    yield make
    for worker in made:
        worker.close()
    engine.reset_for_tests()
    tokens.reset_for_tests()
    transport.reset_for_tests()
    hub.reset_for_tests()


@pytest.fixture
def fake(monkeypatch):
    fake_hub = FakeHub()
    monkeypatch.setattr(account_hub, "call", fake_hub.call)
    servers = {}

    def binding():
        name = servers.get("current")
        if name is None:
            return None
        return {"hub_url": "https://hub.example", "server_id": name, "linked_by": "managed:x", "active": True,
                "encrypted_secret": "enc:x"}

    monkeypatch.setattr(binding_store, "get_binding", binding)
    fake_hub.servers = servers
    return fake_hub


def _signed_in(fake, worker):
    fake.servers["current"] = "server-" + worker.name


def _set_pref(worker, key, value):
    with worker.active():
        from app.core import owner_auth
        database.set_user_preference("admin", owner_auth.preference_identity(), key, value)
        engine.scan_all()


def _pref(worker, key):
    with worker.active():
        from app.core import owner_auth
        row = database.get_user_preference("admin", owner_auth.preference_identity(), key)
        return row["value"] if row else None


async def test_worker_rename_retries_registration_and_survives_restart(workers, fake, monkeypatch):
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="Old name")
        _signed_in(fake, a)
        hub.register()
        # A pre-upgrade rename can exist in the DB with a stale cached context.
        store.rename_member(a.device_id, "Unraid")
        original_call = account_hub.call

        def unavailable(*args, **kwargs):
            from fastapi import HTTPException
            raise HTTPException(503, "Unavailable")

        monkeypatch.setattr(account_hub, "call", unavailable)
        await hub._loop_body()
        assert hub._hub_state()["registered_name"] == "Old name"
        assert fake.devices[a.device_id]["name"] == "Old name"

        hub.reset_for_tests()
        engine.load_context()
        monkeypatch.setattr(account_hub, "call", original_call)
        await hub._loop_body()
        assert fake.devices[a.device_id]["name"] == "Unraid"
        assert hub._hub_state()["registered_name"] == "Unraid"
        calls = fake.calls.count(("server-a", "POST", "network"))
        await hub._loop_body()
        assert fake.calls.count(("server-a", "POST", "network")) == calls


async def test_existing_registration_refreshes_name_after_upgrade(workers, fake):
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="Unraid")
        _signed_in(fake, a)
        hub.register()
        state = hub._hub_state()
        state.pop("registered_name")
        store.update_state(hub=state)
        fake.devices[a.device_id]["name"] = "Old name"
        await hub._loop_body()
        assert fake.devices[a.device_id]["name"] == "Unraid"


async def test_first_worker_to_sign_in_founds_the_account_network(workers, fake):
    a = workers("a")
    with a.active():
        _signed_in(fake, a)
        assert engine.context() is None
        assert await hub._loop_body() is True
        ctx = engine.context()
        assert ctx is not None and ctx.founder_device_id == a.device_id and ctx.bootstrap_state == "ready"
        assert fake.network["network_id"] == ctx.network_id
        assert fake.devices[a.device_id]["status"] == "active"
        assert hub.status()["registered"] and hub.status()["status"] == "active"
        # Later passes just sync.
        database.set_system_setting("tts_model", "voice-founder")
        engine.scan_all()
        assert await hub._loop_body() is False
        assert hub.status()["sync_mode"] == "peers" and not fake.feed


def test_hub_device_routes_approve_and_reject_through_the_api(workers, fake):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api import network as network_api
    from app.api.auth import require_admin, require_auth

    a, b = workers("a"), workers("b")
    app = FastAPI()
    app.include_router(network_api.router)
    app.dependency_overrides[require_admin] = lambda: "admin"
    app.dependency_overrides[require_auth] = lambda: "admin"
    client = TestClient(app)
    with a.active():
        engine.create_network("Home", device_name="A")
        _signed_in(fake, a)
        hub.register()
    with b.active():
        _signed_in(fake, b)
        hub.register()
    with a.active():
        _signed_in(fake, a)
        listed = client.get("/api/v1/network/hub/devices").json()["devices"]
        assert [d["device_id"] for d in listed] == [b.device_id]
        approved = client.post(f"/api/v1/network/hub/devices/{b.device_id}/approve", json={})
        assert approved.status_code == 200 and approved.json()["status"] == "approved"
        assert client.post(f"/api/v1/network/hub/devices/{b.device_id}/approve", json={}).status_code == 400
        assert client.post("/api/v1/network/hub/devices/dev_nobody/reject", json={}).status_code == 200
        status = client.get("/api/v1/network/status").json()
        assert status["hub"]["linked"] and status["hub"]["registered"]
