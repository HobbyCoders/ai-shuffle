"""Peer transport and enrollment over HTTP, with worker A served in-process.

Worker B's HTTP calls are routed by an httpx MockTransport into a
TestClient of worker A's router, entered under A's context. Everything else
is the real code: tokens, exchange, push, snapshot copy, join polling.
"""
import asyncio

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import network as network_api
from app.api import pairing as pairing_api
from app.api.auth import require_admin, require_auth
from app.core import pairing as pairing_core
from app.db import database
from app.db import network as store
from app.network import engine, enrollment, identity, tokens, transport
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
    enrollment._task = None


def _app():
    app = FastAPI()
    app.include_router(network_api.router)
    app.include_router(pairing_api.router)
    from app.middleware.csrf import CSRFProtectionMiddleware
    app.add_middleware(CSRFProtectionMiddleware)
    app.dependency_overrides[require_admin] = lambda: "admin"
    app.dependency_overrides[require_auth] = lambda: "admin"
    return app


class ServedBy:
    """An httpx transport that answers as worker ``server`` for a base URL."""

    def __init__(self, server: Worker, base_url: str):
        self.server = server
        self.base_url = base_url.rstrip("/")
        self.client = TestClient(_app())
        self.requests = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if not url.startswith(self.base_url):
            return httpx.Response(502, json={"detail": "unreachable"})
        path = url[len(self.base_url):]
        self.requests.append((request.method, path.split("?")[0]))
        with self.server.active():
            response = self.client.request(request.method, path, content=request.content,
                                           headers={k: v for k, v in request.headers.items()
                                                    if k.lower() in ("authorization", "content-type", "accept")})
        return httpx.Response(response.status_code, content=response.content,
                              headers={"content-type": response.headers.get("content-type", "application/json")})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


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


async def test_peer_link_exchanges_both_ways_and_detects_rollback(workers, monkeypatch):
    from app.core.config import settings
    monkeypatch.setattr(settings, "cors_origins", "https://app.example.com")
    a, b = workers("a"), workers("b")
    served = ServedBy(a, "https://a.example.com")
    with a.active():
        engine.create_network("Home", device_name="A")
        engine.admit_device(device_id=b.device_id, signing_public=b.identity.signing_public,
                            encryption_public=b.identity.encryption_public, name="B")
        response = engine.join_response(device_id=b.device_id, encryption_public=b.identity.encryption_public,
                                        certificate=[r["record"] for r in store.list_membership_records()
                                                     if r["record"].get("device_id") == b.device_id][0])
        manifest = engine.snapshot_manifest()
        chunks = [engine.snapshot_chunk(c, 0) for c in manifest["collections"]]
        database.set_system_setting("tts_model", "voice-a")
        engine.scan_all()
    with b.active():
        engine.enroll_from_response(response, device_name="B")
        for chunk in chunks:
            engine.apply_snapshot_chunk(chunk)
        engine.adopt_watermarks(manifest["watermarks"])
        engine.finish_bootstrap()
    _set_pref(b, "from_b", 1)

    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            link = transport.PeerLink(a.device_id, ["http://172.17.0.11:8000", "https://a.example.com"])
            summary = await link.sync_once(client, engine.context())
            assert summary["received"] >= 1 and summary["sent"] >= 1
            assert link.base_url == "https://a.example.com" and link.token
            token_before = link.token
            # Second pass reuses the token and moves nothing.
            summary = await link.sync_once(client, engine.context())
            assert summary["received"] == 0 and summary["sent"] == 0 and link.token == token_before
    assert _pref(a, "from_b") == 1
    with b.active():
        assert database.get_system_setting("tts_model") == "voice-a"
        assert store.list_peers()[0]["device_id"] if store.list_peers() else True
    with a.active():
        assert b.device_id in transport.online_devices()
        member = [m for m in store.list_members() if m["device_id"] == b.device_id][0]
        assert isinstance(member["endpoints"], list)

    # Restore-from-backup: wipe B's own ops and watch the incarnation bump.
    with b.active():
        b.sql("DELETE FROM sync_ops WHERE origin_device_id = ?", (b.device_id,))
        assert b.identity.incarnation == 1
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            link = transport.PeerLink(a.device_id, ["https://a.example.com"])
            await link.sync_once(client, engine.context())
            assert identity.load().incarnation == 2
            assert engine.context().identity.incarnation == 2
    _set_pref(b, "after_restore", True)
    async with httpx.AsyncClient(transport=served.transport()) as client:
        with b.active():
            link = transport.PeerLink(a.device_id, ["https://a.example.com"])
            link.token = None
            summary = await link.sync_once(client, engine.context())
            assert summary["sent"] >= 1
    assert _pref(a, "after_restore") is True
    with a.active():
        marks = store.local_watermarks()
        assert "2" in marks[b.device_id]


async def test_enrollment_over_http_then_sync(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    served = ServedBy(a, "http://a.local:8000")
    monkeypatch.setattr(enrollment, "POLL_INTERVAL_SECONDS", 0.01)

    real_async, real_sync = httpx.AsyncClient, httpx.Client

    def async_client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_async(**kwargs)

    def sync_client(*args, **kwargs):
        kwargs["transport"] = served.transport()
        return real_sync(**kwargs)

    # enrollment uses the module, so patch the module attributes for its calls only.
    monkeypatch.setattr(httpx, "AsyncClient", async_client)
    monkeypatch.setattr(httpx, "Client", sync_client)

    with a.active():
        engine.create_network("Home", device_name="A")
        database.set_system_setting("tts_model", "voice-a")
        engine.scan_all()
        grant = pairing_core.create_grant(endpoint_url="http://a.local:8000", purpose="network:join")
        code = grant["code"]
        fingerprint = a.identity.fingerprint
        wrong_fp = fingerprint[:-4] + ("0000" if not fingerprint.endswith("0000") else "1111")

    with b.active():
        with pytest.raises(enrollment.EnrollmentError):
            await enrollment.start_enrollment(endpoint_url="http://a.local:8000", code=code, inviter_fingerprint=wrong_fp)
        started = await enrollment.start_enrollment(endpoint_url="http://a.local:8000", code=code,
                                                    inviter_fingerprint=fingerprint, device_name="B")
        assert started["status"] == "pending" and started["inviter"]["device_id"] == a.device_id
        assert enrollment.enrollment_status()["status"] in ("pending", "syncing", "complete")
        poller = enrollment._task
    # The one-time code admitted B on contact: nothing waits for an administrator on A.
    with a.active():
        assert enrollment.pending_joins() == []
        assert engine.context().member(b.device_id).status == "active"

    with b.active():
        await asyncio.wait_for(poller, timeout=10)
        status = enrollment.enrollment_status()
        assert status["status"] == "complete", status
        ctx = engine.context()
        assert ctx is not None and ctx.bootstrap_state == "ready"
        assert database.get_system_setting("tts_model") == "voice-a"
        assert ctx.member(a.device_id).name == "A"
        assert [m["endpoints"][0] for m in store.list_members() if m["device_id"] == a.device_id] == ["http://a.local:8000"]
    paths = [p for _, p in served.requests]
    assert "/api/v1/network/join" in paths and "/api/v1/network/peer/snapshot" in paths

    # B now syncs to A over the transport loop's link.
    _set_pref(b, "from_b", 42)
    async with real_async(transport=served.transport()) as client:
        with b.active():
            results = await transport.sync_pass(client)
            assert results[a.device_id]["sent"] >= 1
    assert _pref(a, "from_b") == 42


async def test_enrolled_worker_switches_networks_with_an_invite(workers, monkeypatch):
    """A clean-install worker later joins another worker: it leaves its own network
    only once the inviter accepted the code; the inviter's records win, local-only rows merge."""
    a, c = workers("a"), workers("c")
    served = ServedBy(a, "http://a.local:8000")
    monkeypatch.setattr(enrollment, "POLL_INTERVAL_SECONDS", 0.01)
    real_async, real_sync = httpx.AsyncClient, httpx.Client
    monkeypatch.setattr(httpx, "AsyncClient", lambda *args, **kwargs: real_async(**{**kwargs, "transport": served.transport()}))
    monkeypatch.setattr(httpx, "Client", lambda *args, **kwargs: real_sync(**{**kwargs, "transport": served.transport()}))
    with a.active():
        engine.create_network("Home", device_name="A")
        database.set_system_setting("tts_model", "voice-a")
        engine.scan_all()
        grant = pairing_core.create_grant(endpoint_url="http://a.local:8000", purpose="network:join")
    with c.active():
        engine.create_network("Solo", device_name="C")
        database.set_system_setting("tts_model", "voice-c")
        database.set_system_setting("only_on_c", "yes")
        engine.scan_all()
        solo = engine.context().network_id
        with pytest.raises(enrollment.EnrollmentError, match="already belongs"):
            await enrollment.start_enrollment(endpoint_url="http://a.local:8000", code=grant["code"], device_name="C")
        # A wrong code never costs the current membership.
        with pytest.raises(enrollment.EnrollmentError):
            await enrollment.start_enrollment(endpoint_url="http://a.local:8000", code="AAAA-AAAA-AAAA", device_name="C",
                                              leave_current=True)
        assert engine.context() is not None and engine.context().network_id == solo
        await enrollment.start_enrollment(endpoint_url="http://a.local:8000", code=grant["code"], device_name="C",
                                          leave_current=True)
        await asyncio.wait_for(enrollment._task, timeout=10)
        ctx = engine.context()
        assert ctx is not None and ctx.network_id != solo and ctx.bootstrap_state == "ready"
        assert ctx.member(a.device_id).name == "A"
        assert database.get_system_setting("tts_model") == "voice-a"
        assert database.get_system_setting("only_on_c") == "yes"
        engine.scan_all()
    async with real_async(transport=served.transport()) as client:
        with c.active():
            results = await transport.sync_pass(client)
            assert results[a.device_id]["sent"] >= 1
    with a.active():
        assert database.get_system_setting("only_on_c") == "yes"
        assert database.get_system_setting("tts_model") == "voice-a"
