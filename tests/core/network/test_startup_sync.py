# ruff: noqa: F811
"""Launch sync uses the real producer, signed peer API and database apply path."""
import asyncio

import httpx
import pytest

from app.db import network as store
from app.network import engine, hub, startup_sync, transport
from tests.core.network.harness import join
from tests.core.network.test_transport import (  # noqa: F401
    ServedBy,
    _pref,
    _set_pref,
    workers,
)


@pytest.fixture(autouse=True)
def isolated_launch(monkeypatch):
    monkeypatch.setattr(startup_sync, "_task", None)
    monkeypatch.setattr(startup_sync, "_last_result", None)
    monkeypatch.setattr(hub, "_binding", lambda: None)
    hub.reset_for_tests()
    yield
    hub.reset_for_tests()


def connect(monkeypatch, source, destination):
    served = ServedBy(source, "https://source.example.com")
    with destination.active():
        store.touch_member(source.device_id, endpoints=[served.base_url])
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=served.transport(), **kwargs))
    return served


async def test_launch_applies_newer_peer_data_across_all_pages_before_returning(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _set_pref(b, "theme", "old")
    # Forward B's value first so A's update is causally newer.
    from tests.core.network.harness import exchange
    exchange(b, a)
    for i in range(8):
        _set_pref(a, f"new-{i}", i)
    _set_pref(a, "theme", "new")
    served = connect(monkeypatch, a, b)
    monkeypatch.setattr(transport, "BATCH_LIMIT", 2)
    with b.active():
        result = await startup_sync.synchronize()
        assert result == {"status": "synced", "peers": 1}
        assert _pref(b, "theme") == "new"
        assert all(_pref(b, f"new-{i}") == i for i in range(8))
    assert sum(path.endswith('/exchange') for _, path in served.requests) > 1
    # A later desktop launch attached to the same worker must check again.
    _set_pref(a, "theme", "newer")
    with b.active():
        assert (await startup_sync.synchronize())["status"] == "synced"
        assert _pref(b, "theme") == "newer"


async def test_snapshot_catchup_restores_peer_materialized_data(workers, monkeypatch):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    join(a, c)
    _set_pref(a, "saved", "from snapshot")
    # B acquires materialized state through the real producing snapshot path,
    # without A's full operation log, then serves C while A is offline.
    source = ServedBy(a, "https://source.example.com")
    with b.active():
        async with httpx.AsyncClient(transport=source.transport()) as client:
            await transport.PeerLink(a.device_id, [source.base_url]).receive_state(client, engine.context())
    connect(monkeypatch, b, c)
    with c.active():
        store.touch_member(a.device_id, endpoints=[])
        result = await startup_sync.synchronize()
        assert result["peers"] == 1
        assert _pref(c, "saved") == "from snapshot"


async def test_offline_peer_keeps_local_data_and_reports_fallback(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _set_pref(b, "saved", "local")
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(lambda r: httpx.Response(503)), **kwargs))
    with b.active():
        store.touch_member(a.device_id, endpoints=["https://offline.example.com"])
        result = await startup_sync.synchronize()
        assert result["status"] == "offline"
        assert _pref(b, "saved") == "local"
        assert startup_sync.status() == result


async def test_new_install_does_not_wait_for_nonexistent_peers(workers):
    with workers("fresh").active():
        assert (await startup_sync.synchronize())["status"] == "not_enrolled"


async def test_timeout_is_bounded_and_concurrent_requests_share_the_barrier(monkeypatch):
    calls = 0
    async def blocked():
        nonlocal calls
        calls += 1
        await asyncio.Event().wait()
    monkeypatch.setattr(startup_sync, "_sync", blocked)
    monkeypatch.setattr(startup_sync, "STARTUP_TIMEOUT", 0.02)
    a, b = await asyncio.gather(startup_sync.synchronize(), startup_sync.synchronize())
    assert a == b and a["status"] == "partial"
    assert calls == 1


async def test_account_outage_does_not_skip_paired_peer(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    _set_pref(a, "available", True)
    connect(monkeypatch, a, b)
    async def unavailable():
        await asyncio.Event().wait()
    monkeypatch.setattr(hub, "_loop_body", unavailable)
    monkeypatch.setattr(startup_sync, "ACCOUNT_TIMEOUT", 0.02)
    with b.active():
        result = await startup_sync.synchronize()
        assert result["status"] == "partial" and result["peers"] == 1
        assert _pref(b, "available") is True

async def test_account_admission_finishes_peer_bootstrap_before_launch(workers, monkeypatch):
    a, b = workers("account-source"), workers("account-new")
    with a.active():
        engine.create_network("Home")
        admission = engine.admit_device(device_id=b.device_id, signing_public=b.identity.signing_public,
                                        encryption_public=b.identity.encryption_public, name="New desktop")
    _set_pref(a, "account-setting", "available on first load")
    served = connect(monkeypatch, a, b)
    admission["issuer"]["endpoints"] = [served.base_url]
    monkeypatch.setattr(hub, "_binding", lambda: {"hub_url": "https://account.example.com", "server_id": "new"})
    def account_call(method, route, *args, **kwargs):
        if method == "POST" and route == "":
            return {"status": "approved", "response": admission}
        return {"status": "active", "network_id": admission["network_id"], "devices": []}
    monkeypatch.setattr(hub, "_call", account_call)
    with b.active():
        result = await startup_sync.synchronize()
        assert result["status"] == "synced", (result, dict(hub._state))
        assert engine.context().bootstrap_state == engine.BOOTSTRAP_READY
        assert _pref(b, "account-setting") == "available on first load"


async def test_account_directory_refreshes_known_peer_but_cannot_admit_a_stranger(workers, monkeypatch):
    a, b, stranger = workers("a"), workers("b"), workers("stranger")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    monkeypatch.setattr(hub, "_binding", lambda: {"hub_url": "https://account.example.com", "server_id": "b"})
    with b.active():
        store.touch_member(a.device_id, endpoints=["https://lan.example.com"])
        ctx = engine.load_context()
        monkeypatch.setattr(hub, "_call", lambda *args, **kwargs: {"network_id": ctx.network_id, "devices": [
            {"device_id": a.device_id, "endpoints": ["https://new-address.example.com"]},
            {"device_id": stranger.device_id, "endpoints": ["https://stranger.example.com"]},
        ]})
        hub.refresh_peer_endpoints()
        assert store.get_member(a.device_id)["endpoints"] == ["https://new-address.example.com", "https://lan.example.com"]
        assert store.get_member(stranger.device_id) is None

async def test_startup_downloads_referenced_attachment_bytes_before_returning(workers, monkeypatch):
    from app.db import database
    from app.network import blobs
    a, b = workers("files-source"), workers("files-target")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with a.active():
        database.create_note(note_id="note", title="A file", content="body")
        database.create_note_attachment(attachment_id="file", note_id="note", filename="x.txt",
                                        file_path="/source-only/x.txt", data=b"file bytes", size_bytes=10)
        engine.scan_all()
    connect(monkeypatch, a, b)
    with b.active():
        result = await startup_sync.synchronize()
        assert result["status"] == "synced"
        assert blobs.read_local(blobs.sha256(b"file bytes")) == b"file bytes"


async def test_missing_attachment_stays_pending_without_any_cloud_fallback(workers, monkeypatch):
    from app.network import blobs
    with workers("local").active():
        engine.create_network("Home")
        monkeypatch.setattr(store, "missing_blobs", lambda limit: [{"hash": "0" * 64}])
        monkeypatch.setattr(hub, "_call", lambda *a, **k: pytest.fail("Cloud transport called"))
        assert await blobs.fetch_missing() == 0

async def test_launch_flushes_remote_edits_not_yet_in_the_operation_log(workers, monkeypatch):
    from app.db import database
    a, b = workers("recent-source"), workers("recent-target")
    with a.active():
        engine.create_network("Home")
        database.create_note(note_id="recent", title="Recent", content="old")
        engine.scan_all()
    join(a, b)
    connect(monkeypatch, a, b)
    with b.active():
        await startup_sync.synchronize()
        assert database.get_note("recent")["content"] == "old"
    with a.active():
        # Same producing function as PATCH /notes; deliberately no scan call.
        database.update_note(note_id="recent", content="new edit before launch")
    with b.active():
        assert (await startup_sync.synchronize())["status"] == "synced"
        assert database.get_note("recent")["content"] == "new edit before launch"

async def test_old_worker_cannot_claim_a_fresh_startup_sync(workers, monkeypatch):
    a, b = workers("old"), workers("new")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with b.active():
        link = transport.PeerLink(a.device_id, ["https://old.example.com"])
        async def authenticated(*args):
            return None
        async def exchange(*args, **kwargs):
            return {"ops": [], "watermarks": {}}
        monkeypatch.setattr(link, "authenticate", authenticated)
        monkeypatch.setattr(link, "_request", exchange)
        async with httpx.AsyncClient() as client:
            with pytest.raises(transport.PeerError, match="Update this worker"):
                await link.sync_once(client, engine.context(), catch_up=True)


def test_startup_barrier_requires_owner_access(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.api import network
    from app.core import owner_auth, preview_access
    monkeypatch.setattr(owner_auth, "local_access", lambda request: False)
    monkeypatch.setattr(preview_access, "grant", lambda request: None)
    app = FastAPI()
    app.include_router(network.router)
    with TestClient(app) as client:
        assert client.post("/api/v1/network/startup-sync").status_code == 403
