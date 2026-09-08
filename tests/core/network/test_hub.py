"""Account sync through the hub feed, device approval and recovery."""
import pytest

from app.core import account_hub
from app.db import database
from app.db import mobile_hub as binding_store
from app.network import engine, hub, recovery, tokens, transport
from tests.core.network.fake_hub import FakeHub
from tests.core.network.harness import Worker, exchange


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


async def test_register_push_pull_and_device_approval(workers, fake):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        database.set_system_setting("tts_model", "voice-a")
        engine.scan_all()
        _signed_in(fake, a)
        assert hub.status()["linked"] and not hub.status()["registered"]
        result = hub.register()
        assert result["status"] == "active" and hub.status()["registered"]
        pushed = hub.push_once()
        assert pushed >= 2 and hub.status()["backlog"] == 0
        assert hub.push_once() == 0
        assert hub.pull_once() == 0  # our own ops come back and are ignored
        assert hub.status()["cursor"] == len(fake.feed)
    assert fake.network["network_id"] and fake.devices[a.device_id]["status"] == "active"

    # B is signed in to the same account but has no network yet: it waits for approval.
    with b.active():
        _signed_in(fake, b)
        assert await hub._loop_body() is False
        assert fake.devices[b.device_id]["status"] == "pending"
        assert engine.context() is None
    with a.active():
        _signed_in(fake, a)
        pending = hub.pending_devices()
        assert [d["device_id"] for d in pending] == [b.device_id]
        with pytest.raises(hub.HubError):
            hub.approve_device(b.device_id, fingerprint_confirmed=False)
        hub.approve_device(b.device_id, fingerprint_confirmed=True)
        assert fake.devices[b.device_id]["status"] == "approved"
        hub.push_once()  # B's certificate reaches the feed
    with b.active():
        _signed_in(fake, b)
        assert await hub._loop_body() is True
        ctx = engine.context()
        assert ctx is not None and ctx.bootstrap_state == "ready"
        assert database.get_system_setting("tts_model") == "voice-a"
        assert ctx.member(a.device_id).name == "A" and ctx.member(b.device_id).status == "active"
        assert fake.devices[b.device_id]["status"] == "active"
    # A change on B goes to the hub, A pulls it; the same op over LAN is a no-op.
    _set_pref(b, "from_b", 1)
    with b.active():
        _signed_in(fake, b)
        assert hub.push_once() >= 1
    with a.active():
        _signed_in(fake, a)
        assert hub.pull_once() >= 1
    assert _pref(a, "from_b") == 1
    assert exchange(b, a).applied == 0


async def test_quota_exceeded_keeps_lan_sync_and_shows_backlog(workers, fake):
    fake.quota_ops = 3
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        _signed_in(fake, a)
        hub.register()
        for i in range(6):
            database.set_user_preference("admin", "u", f"k{i}", i)
        engine.scan_all()
        with pytest.raises(hub.HubError):
            hub.push_once()
        status = hub.status()
        assert status["backlog"] > 0 and status["quota"]["limit"] == 3
        assert await hub._loop_body() is False
        assert "quota" in (hub._state.get("last_error") or "")


async def test_recovery_key_restores_a_clean_device_with_every_worker_off(workers, fake):
    a, d = workers("a"), workers("d")
    with a.active():
        engine.create_network("Home", device_name="A")
        database.set_system_setting("tts_model", "voice-9")
        database.create_tag(tag_id="t1", name="urgent", color="#f00")
        engine.scan_all()
        _signed_in(fake, a)
        hub.register()
        key = recovery.create()["recovery_key"]
        hub._upload_recovery()
        hub.push_once()
        assert fake.recovery and fake.recovery["check"]
    # Every worker is off; D signs in with the recovery key.
    with d.active():
        _signed_in(fake, d)
        with pytest.raises(recovery.RecoveryError):
            await recovery.unlock_from_hub("A1-00000-00000-00000-00000-00000-0", device_name="D")
        result = await recovery.unlock_from_hub(key, device_name="D")
        assert result["restored"]
        ctx = engine.context()
        assert ctx.bootstrap_state == "ready" and ctx.member(a.device_id).status == "active"
        assert ctx.member(d.device_id).status == "active"
        assert database.get_system_setting("tts_model") == "voice-9"
        assert database.get_tag("t1")["name"] == "urgent"
        assert fake.devices[d.device_id]["status"] == "active"
        hub.push_once()
    # A comes back later and accepts D purely from the feed.
    with a.active():
        _signed_in(fake, a)
        assert hub.pull_once() >= 1
        assert engine.context().member(d.device_id).status == "active"
    _set_pref(d, "from_d", 5)
    with d.active():
        _signed_in(fake, d)
        hub.push_once()
    with a.active():
        _signed_in(fake, a)
        hub.pull_once()
    assert _pref(a, "from_d") == 5


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
        assert client.post(f"/api/v1/network/hub/devices/{b.device_id}/approve",
                           json={"fingerprint_confirmed": False}).status_code == 400
        approved = client.post(f"/api/v1/network/hub/devices/{b.device_id}/approve",
                               json={"fingerprint_confirmed": True})
        assert approved.status_code == 200 and approved.json()["status"] == "approved"
        assert client.post("/api/v1/network/hub/devices/dev_nobody/reject", json={}).status_code == 200
        status = client.get("/api/v1/network/status").json()
        assert status["hub"]["linked"] and status["hub"]["registered"]
