"""Background state capture, mixed-version peers, and bootstrap diagnostics."""
import asyncio
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from app.api import network as network_api
from app.db import network as store
from app.network import engine, hub, peer_snapshot, tokens, transport
from tests.core.network.harness import join, owner_pref, set_owner_pref
from tests.core.network.test_transport import ServedBy


@pytest.fixture(autouse=True)
def capture_jobs(monkeypatch):
    # Do not retain Futures/worker contexts after their disposable DBs close.
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(peer_snapshot, "_executor", executor)
    monkeypatch.setattr(peer_snapshot, "_jobs", {})
    hub.reset_for_tests()
    yield
    executor.shutdown(wait=True)
    hub.reset_for_tests()


async def test_slow_capture_survives_request_timeout_and_bootstraps(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
        admission = engine.admit_device(device_id=b.device_id, signing_public=b.identity.signing_public,
                                       encryption_public=b.identity.encryption_public, name="B")
    with b.active():
        engine.enroll_from_response(admission, device_name="B")
        ctx = engine.require_context()
    set_owner_pref(a, "slow_copy", 7)
    capture = peer_snapshot.capture
    verify, apply = peer_snapshot.verify_manifest, peer_snapshot.apply
    calls = []
    def slow(recipient):
        calls.append(recipient)
        time.sleep(0.15)  # Longer than the per-request budget, not the polling budget.
        return capture(recipient)
    def verify_as_joiner(*args):
        with b.active():
            return verify(*args)
    def apply_as_joiner(*args):
        with b.active():
            return apply(*args)
    async def authenticated(*args):
        pass
    monkeypatch.setattr(peer_snapshot, "capture", slow)
    monkeypatch.setattr(peer_snapshot, "verify_manifest", verify_as_joiner)
    monkeypatch.setattr(peer_snapshot, "apply", apply_as_joiner)
    monkeypatch.setattr(transport, "STATE_REQUEST_TIMEOUT", 0.05)
    monkeypatch.setattr(transport, "STATE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(transport, "STATE_POLL_MAX_SECONDS", 0.02)
    monkeypatch.setattr(transport.PeerLink, "authenticate", authenticated)
    app = FastAPI()
    app.include_router(network_api.router)
    app.dependency_overrides[tokens.require_device] = lambda: b.device_id
    asgi = httpx.ASGITransport(app=app)
    # ASGITransport does not enforce httpx timeouts, so enforce the actual
    # client's timeout at the transport boundary, including each API request.
    class TimedASGI(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return await asyncio.wait_for(asgi.handle_async_request(request),
                                          timeout=request.extensions["timeout"]["read"])
    waits = []
    link = transport.PeerLink(a.device_id, ["https://a.example.com"])
    link.base_url, link.token = "https://a.example.com", "test-token"
    # Only A is active during background capture. Switch to B only after the
    # manifest is ready, for verification/application (the harness uses globals).
    with a.active():
        async with httpx.AsyncClient(transport=TimedASGI()) as client:
            assert await link.receive_state(client, ctx, on_wait=waits.append) > 0
    with b.active():
        engine.finish_bootstrap()
        assert engine.context().bootstrap_state == engine.BOOTSTRAP_READY
        assert owner_pref(b, "slow_copy") == 7
    assert len(waits) >= 2
    assert calls == [b.device_id]


def test_concurrent_legacy_and_poll_requests_share_job(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    started, release = threading.Event(), threading.Event()
    calls = []
    capture = peer_snapshot.capture
    def blocked(recipient):
        calls.append(recipient)
        started.set()
        assert release.wait(5)
        return capture(recipient)
    monkeypatch.setattr(peer_snapshot, "capture", blocked)
    with a.active():
        try:
            assert peer_snapshot.state(b.device_id, prepare=True) == {"status": "preparing"}
            assert started.wait(5)
            assert peer_snapshot.state(b.device_id, prepare=True) == {"status": "preparing"}
            # A legacy caller blocks on this same Future, not a new capture.
            with ThreadPoolExecutor(max_workers=1) as callers:
                legacy = callers.submit(peer_snapshot.state, b.device_id)
                release.set()
                manifest = legacy.result(timeout=5)
            assert peer_snapshot.state(b.device_id, prepare=True) == manifest
        finally:
            release.set()
    assert calls == [b.device_id]
    assert manifest["format"] == 1


@pytest.mark.parametrize("known_head", [False, True])
async def test_old_manifest_shape_and_cached_frontier_catch_up(workers, monkeypatch, known_head):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    set_owner_pref(a, "before", 1)
    with a.active():
        old = peer_snapshot.capture(b.device_id)
    set_owner_pref(a, "after", 2)
    with a.active():
        assert peer_snapshot.capture(b.device_id) == old
        live = store.local_watermarks()
        assert live != old["watermarks"]
    if known_head:
        # snapshot_required can arrive with a newer exchange frontier. Do not
        # forget it merely because the reusable capture is older.
        transport.note_watermarks(a.device_id, live)
    served = ServedBy(a, "https://a.example.com")
    original = served.handler
    def older_server(request):
        if request.url.path == "/api/v1/network/peer/state":
            with a.active():
                # Older inviter ignores the opt-in query and returns a manifest.
                return httpx.Response(200, json=peer_snapshot.capture(b.device_id))
        return original(request)
    with b.active():
        async with httpx.AsyncClient(transport=httpx.MockTransport(older_server)) as client:
            link = transport.PeerLink(a.device_id, [served.base_url])
            await link.receive_state(client, engine.context())
            assert owner_pref(b, "before") == 1
            assert owner_pref(b, "after") is None
            assert store.local_watermarks()[a.device_id] == old["watermarks"][a.device_id]
            assert transport._peer_watermarks[a.device_id] == (live if known_head else old["watermarks"])
            incarnation = engine.context().identity.incarnation
            await link.sync_once(client, engine.context())
            assert engine.context().identity.incarnation == incarnation, "no false rollback"
            assert owner_pref(b, "after") == 2
            assert transport._peer_watermarks[a.device_id] != old["watermarks"]


@pytest.mark.parametrize("error", [httpx.ReadTimeout(""), transport.PeerError("HTTP 530"),
                                   transport.PeerError("HTTP 503"),
                                   ValueError("Peer state manifest did not verify: wrong device/network")])
async def test_bootstrap_warning_once_per_streak_and_visible_reason(workers, monkeypatch, caplog, error):
    a = workers("a")
    with a.active():
        engine.create_network("Home")
        async def failing(*args, **kwargs):
            raise error
        monkeypatch.setattr(transport.PeerLink, "receive_state", failing)
        admission = {"issuer": {"device_id": "peer-a", "endpoints": ["https://a.example.com"]}}
        with caplog.at_level(logging.WARNING, logger=hub.__name__):
            for _ in range(2):
                with pytest.raises(hub.HubError):
                    await hub._bootstrap_from_peers(admission)
        warnings = [r.message for r in caplog.records if "Settings bootstrap failed" in r.message]
        assert len(warnings) == 1
        assert "peer-a" in warnings[0] and type(error).__name__ in warnings[0]
        assert "peer-a" in hub.status()["last_error"]
        assert type(error).__name__ in hub.status()["last_error"]
        async def succeeded(*args, **kwargs):
            pass
        monkeypatch.setattr(transport.PeerLink, "receive_state", succeeded)
        await hub._bootstrap_from_peers(admission)
        assert hub.status()["last_error"] is None
        monkeypatch.setattr(transport.PeerLink, "receive_state", failing)
        with pytest.raises(hub.HubError):
            await hub._bootstrap_from_peers(admission)
        assert len([r for r in caplog.records if "Settings bootstrap failed" in r.message]) == 2


@pytest.mark.parametrize("field,value,reason", [
    ("network_id", "wrong-network", "different network"),
    ("recipient", "wrong-device", "different device"),
])
async def test_mismatched_manifest_is_warned_and_shown(workers, monkeypatch, caplog, field, value, reason):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with a.active():
        manifest = peer_snapshot.capture(b.device_id)
    manifest[field] = value
    served = ServedBy(a, "https://a.example.com")
    original = served.handler
    def mismatched(request):
        if request.url.path == "/api/v1/network/peer/state":
            return httpx.Response(200, json=manifest)
        return original(request)
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(
        transport=httpx.MockTransport(mismatched), **kwargs))
    with b.active(), caplog.at_level(logging.WARNING, logger=hub.__name__):
        before = store.local_watermarks()
        with pytest.raises(hub.HubError, match=reason):
            await hub._bootstrap_from_peers({"issuer": {
                "device_id": a.device_id, "endpoints": [served.base_url]}})
        assert store.local_watermarks() == before
        assert reason in hub.status()["last_error"]
    assert any(a.device_id in r.message and reason in r.message for r in caplog.records)


def test_finished_capture_expires_and_revoked_recipient_cannot_reuse(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    with a.active():
        old = peer_snapshot.state(b.device_id)
        assert peer_snapshot.state(b.device_id) == old
        monkeypatch.setattr(peer_snapshot.time, "time", lambda: old["expires_at"] + 1)
        new = peer_snapshot.state(b.device_id)
        assert new["transfer"] != old["transfer"]
        engine.revoke_device(b.device_id)
        with pytest.raises(ValueError, match="no longer a member"):
            peer_snapshot.state(b.device_id, prepare=True)


def test_large_transfer_stays_downloadable_past_ttl_but_is_not_reused(workers, monkeypatch):
    """A copy too large to fetch within TTL must not expire mid-download.

    Seen live: 2.2M transcript events made ~22k pages; the joiner fetched
    ~9k of them, hit the 15-minute expiry (HTTP 409) and restarted forever.
    """
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    set_owner_pref(a, "big_copy", 1)
    clock = [time.time()]
    monkeypatch.setattr(peer_snapshot.time, "time", lambda: clock[0])
    # Pretend each page is huge so the allowance dominates TTL.
    monkeypatch.setattr(peer_snapshot, "BYTES_PER_SECOND", 1)
    with a.active():
        captured_at = clock[0]
        manifest = peer_snapshot.state(b.device_id)
        assert manifest["expires_at"] > captured_at + peer_snapshot.TTL
        assert manifest["expires_at"] <= captured_at + peer_snapshot.MAX_LIFETIME
        # Well past TTL the recipient can still read every page...
        clock[0] = captured_at + peer_snapshot.TTL + 60
        last = len(manifest["pages"]) - 1
        assert peer_snapshot.page(b.device_id, manifest["transfer"], last)
        # ...cleanup keeps it, keyed on the signed expiry, not file age...
        directory = peer_snapshot._directory(manifest["transfer"])
        old = captured_at - 2 * peer_snapshot.TTL
        for path in [directory, *directory.iterdir()]:
            os.utime(path, (old, old))
        peer_snapshot.cleanup()
        assert directory.exists()
        # ...but a NEW download gets a fresh capture, not the aging one.
        fresh = peer_snapshot.state(b.device_id)
        assert fresh["transfer"] != manifest["transfer"]
        assert peer_snapshot.capture(b.device_id)["transfer"] == fresh["transfer"]
        # Once the signed expiry passes, the page endpoint and cleanup refuse it.
        clock[0] = manifest["expires_at"]
        with pytest.raises(ValueError, match="expired"):
            peer_snapshot.page(b.device_id, manifest["transfer"], 0)
        peer_snapshot.cleanup()
        assert not directory.exists()
    # The recipient's apply re-verifies the signed expiry; it holds past TTL.
    with b.active():
        clock[0] = captured_at + peer_snapshot.TTL + 60
        assert peer_snapshot.verify_manifest(manifest, a.device_id) is not None


def test_abandoned_capture_without_manifest_is_cleaned_after_ttl(workers, monkeypatch):
    a = workers("a")
    with a.active():
        engine.create_network("Home")
        directory = peer_snapshot._directory("ab" * 16)
        directory.mkdir()
        (directory / "0.json").write_bytes(b"{}")
        peer_snapshot.cleanup()
        assert directory.exists(), "a recent partial capture is kept"
        old = time.time() - peer_snapshot.TTL - 1
        os.utime(directory, (old, old))
        peer_snapshot.cleanup()
        assert not directory.exists()


def test_download_allowance_is_bounded():
    pages = [{"bytes": 400_000}] * 22_000  # The live case: ~8.8 GB in 22k pages.
    allowance = peer_snapshot._download_allowance(pages)
    assert allowance > 22_000 * 0.25
    assert peer_snapshot.TTL + allowance <= peer_snapshot.MAX_LIFETIME
    assert peer_snapshot._download_allowance([]) == 0


@pytest.mark.parametrize("cache", ["future", "disk"])
async def test_cached_capture_refreshes_when_snapshot_adoption_leaves_tail_gap(workers, tmp_path, cache):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    join(a, c)
    with a.active():
        old = (peer_snapshot.state(b.device_id) if cache == "future"
               else peer_snapshot.capture(b.device_id))
    set_owner_pref(c, "adopted_from_c", 42)
    copied = tmp_path / "from-c"
    copied.mkdir()
    with c.active():
        incoming = peer_snapshot.capture(a.device_id)
        for index in range(len(incoming["pages"])):
            (copied / f"{index}.json").write_bytes(
                peer_snapshot.page(a.device_id, incoming["transfer"], index))
    with a.active():
        peer_snapshot.apply(incoming, copied, c.device_id)
        assert store.requires_snapshot(old["watermarks"], exclude_origin=b.device_id)
        assert owner_pref(a, "adopted_from_c") == 42
        fresh = (peer_snapshot.state(b.device_id) if cache == "future"
                 else peer_snapshot.capture(b.device_id))
        assert fresh["transfer"] != old["transfer"]
        assert not store.requires_snapshot(fresh["watermarks"], exclude_origin=b.device_id)
        assert peer_snapshot.state(b.device_id) == fresh
    served = ServedBy(a, "https://a.example.com")
    with b.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            link = transport.PeerLink(a.device_id, [served.base_url])
            # Startup catch-up must finish, not repeat snapshot_required against
            # a TTL-cached frontier whose tail this forwarding peer cannot serve.
            await link.sync_once(client, engine.context(), catch_up=True)
        assert owner_pref(b, "adopted_from_c") == 42
        assert store.local_watermarks()[c.device_id] == fresh["watermarks"][c.device_id]


def test_waiting_recipient_revocation_rechecks_replaced_context(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    captured, release = threading.Event(), threading.Event()
    capture = peer_snapshot.capture
    def blocked(recipient):
        manifest = capture(recipient)
        captured.set()
        assert release.wait(5)
        return manifest
    monkeypatch.setattr(peer_snapshot, "capture", blocked)
    with a.active(), ThreadPoolExecutor(max_workers=1) as callers:
        old_context = engine.require_context()
        request = callers.submit(peer_snapshot.state, b.device_id)
        try:
            assert captured.wait(5)
            engine.load_context()  # Replace the cached object before revoking.
            engine.revoke_device(b.device_id)
            assert engine.require_context() is not old_context
            assert old_context.member(b.device_id).status == "active"
            release.set()
            with pytest.raises(ValueError, match="no longer a member"):
                request.result(timeout=5)
        finally:
            release.set()


def test_recipient_revoked_during_tail_validation_is_refused(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    validating, release = threading.Event(), threading.Event()
    requires_snapshot = store.requires_snapshot
    def blocked(*args, **kwargs):
        validating.set()
        assert release.wait(5)
        return requires_snapshot(*args, **kwargs)
    with a.active():
        peer_snapshot.state(b.device_id)  # A completed reusable capture.
        monkeypatch.setattr(store, "requires_snapshot", blocked)
        with ThreadPoolExecutor(max_workers=1) as callers:
            request = callers.submit(peer_snapshot.state, b.device_id)
            try:
                assert validating.wait(5)
                engine.revoke_device(b.device_id)
                assert engine.require_context().member(b.device_id).status == "revoked"
                release.set()
                with pytest.raises(ValueError, match="no longer a member"):
                    request.result(timeout=5)
            finally:
                release.set()


@pytest.mark.parametrize("stage", ["authentication", "manifest"])
async def test_preparation_deadline_cancels_authentication_and_late_manifest(monkeypatch, stage):
    link = transport.PeerLink("peer-a", ["https://a.example.com"])
    requests, verified = [], []
    async def authenticate(*args):
        if stage == "authentication":
            await asyncio.sleep(0.1)
    async def manifest(*args, **kwargs):
        requests.append(kwargs["timeout"])
        await asyncio.sleep(0.1)
        return {"pages": []}
    monkeypatch.setattr(link, "authenticate", authenticate)
    monkeypatch.setattr(link, "_request", manifest)
    def verify(*args):
        verified.append(args)
        raise AssertionError("Expired preparation reached manifest verification")
    monkeypatch.setattr(peer_snapshot, "verify_manifest", verify)
    monkeypatch.setattr(transport, "STATE_PREPARE_SECONDS", 0.02)
    with pytest.raises(transport.PeerError, match="Timed out waiting"):
        await asyncio.wait_for(link.receive_state(None, SimpleNamespace()), timeout=0.5)
    assert len(requests) == (1 if stage == "manifest" else 0)
    assert verified == []


async def test_request_budget_is_recomputed_after_authentication(monkeypatch):
    link = transport.PeerLink("peer-a", ["https://a.example.com"])
    now, budgets = [10.0], []
    async def authenticate(*args):
        now[0] += 4
    async def manifest(*args, **kwargs):
        budgets.append(kwargs["timeout"])
        now[0] += 7  # A ready reply still cannot be accepted past the deadline.
        return {"pages": []}
    monkeypatch.setattr(link, "authenticate", authenticate)
    monkeypatch.setattr(link, "_request", manifest)
    monkeypatch.setattr(peer_snapshot, "verify_manifest", lambda *args: pytest.fail(
        "Ready reply was accepted after the preparation deadline"))
    monkeypatch.setattr(transport.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(transport, "STATE_PREPARE_SECONDS", 10)
    with pytest.raises(transport.PeerError, match="Timed out waiting"):
        await link.receive_state(None, SimpleNamespace())
    assert budgets == [6]


async def test_preparing_has_bounded_overall_wait(monkeypatch):
    link = transport.PeerLink("peer-a", ["https://a.example.com"])
    async def authenticated(*args):
        pass
    async def preparing(*args, **kwargs):
        return {"status": "preparing"}
    monkeypatch.setattr(link, "authenticate", authenticated)
    monkeypatch.setattr(link, "_request", preparing)
    monkeypatch.setattr(transport, "STATE_PREPARE_SECONDS", 0.03)
    monkeypatch.setattr(transport, "STATE_POLL_SECONDS", 0.01)
    with pytest.raises(transport.PeerError, match="Timed out waiting"):
        await link.receive_state(None, SimpleNamespace())
