# ruff: noqa: F811
"""A joining worker's first copy: progress in status, and resuming after a restart."""
import asyncio
import json
import threading

import httpx
import pytest

from app.api import network as network_api
from app.network import engine, hub, peer_snapshot, startup_sync, transport
from tests.core.network.harness import owner_pref, set_owner_pref
from tests.core.network.test_transport import (  # noqa: F401
    ServedBy,
    workers,
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(startup_sync, "_task", None)
    monkeypatch.setattr(startup_sync, "_last_result", None)
    monkeypatch.setattr(hub, "_binding", lambda: None)
    monkeypatch.setattr(peer_snapshot, "_jobs", {})
    hub.reset_for_tests()
    yield
    hub.reset_for_tests()


def joining(workers):
    """A founder named ServerWorker and a newly enrolled worker that still needs its first copy."""
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="ServerWorker")
        admission = engine.admit_device(device_id=b.device_id, signing_public=b.identity.signing_public,
                                        encryption_public=b.identity.encryption_public, name="Pop")
    with b.active():
        engine.enroll_from_response(admission, device_name="Pop")
        assert engine.context().bootstrap_state == engine.BOOTSTRAP_SYNCING
    set_owner_pref(a, "copied", "yes")
    return a, b


async def test_progress_is_reported_through_network_status(workers, monkeypatch):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    seen = []
    apply = peer_snapshot.apply

    def watched(*args, **kwargs):
        seen.append(transport.state_copy_status())
        return apply(*args, **kwargs)

    monkeypatch.setattr(peer_snapshot, "apply", watched)
    with b.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            await transport.PeerLink(a.device_id, [served.base_url]).receive_state(client, engine.context())
        during = seen[0]
        assert during["phase"] == "applying"
        assert during["peer"] == a.device_id and during["peer_name"] == "ServerWorker"
        assert during["pages_total"] > 0 and during["pages_done"] == during["pages_total"]
        assert during["bytes_total"] > 0 and during["bytes_done"] == during["bytes_total"]
        status = network_api._status()
        assert status["hub"]["first_copy"]["phase"] == "done"
        assert status["hub"]["first_copy"]["error"] is None
        assert owner_pref(b, "copied") == "yes"


async def test_failed_copy_reports_why_in_status_and_launch_reason(workers, monkeypatch):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    original = served.handler

    def interrupted(request):
        if "/peer/state/" in request.url.path:
            return httpx.Response(409, json={"detail": "Peer transfer expired; start a new transfer"})
        return original(request)

    with b.active():
        async with httpx.AsyncClient(transport=httpx.MockTransport(interrupted)) as client:
            with pytest.raises(transport.PeerError):
                await transport.PeerLink(a.device_id, [served.base_url]).receive_state(client, engine.context())
        copy = hub.status()["first_copy"]
        assert copy["phase"] == "failed" and "interrupted" in copy["error"]
        result = await startup_sync.synchronize()
        assert result["status"] == "partial"
        assert result["reason"].startswith("Copying your data from ServerWorker stopped and will retry")


async def test_launch_reports_the_copy_and_status_follows_it(workers):
    _, b = joining(workers)
    with b.active():
        result = await startup_sync.synchronize()
        assert result == {"status": "partial", "first_copy": True,
                          "reason": "Waiting for another worker to copy your data to this one."}
        transport._copy_begin("peer-a", type("Ctx", (), {"member": lambda self, _id: None})())
        transport._copy_progress(phase="downloading", pages_done=12458, pages_total=22362)
        assert startup_sync.status()["reason"] == (
            "Copying your data from another worker: 12,458 of 22,362 pages. "
            "The copy continues in the background.")
        engine.finish_bootstrap()
        assert startup_sync.status() == result


def page_requests(served):
    """Page indexes the receiver fetched, and how many manifests it asked for."""
    pages = [int(path.rsplit("/", 1)[1]) for method, path in served.requests
             if method == "GET" and path.startswith("/api/v1/network/peer/state/")]
    manifests = sum(1 for method, path in served.requests if path == "/api/v1/network/peer/state")
    return pages, manifests


def failing_after(served, last_good):
    """A peer that stops serving pages after ``last_good`` (a dropped connection, say)."""
    def handler(request):
        path = request.url.path
        if path.startswith("/api/v1/network/peer/state/") and int(path.rsplit("/", 1)[1]) > last_good:
            return httpx.Response(503, json={"detail": "unavailable"})
        return served.handler(request)
    return httpx.MockTransport(handler)


def saved_dirs(worker):
    root = worker.data_dir / "peer-receive"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


def restart():
    """What a new process forgets: in-memory progress and page checks."""
    transport.reset_for_tests()
    peer_snapshot._verified.clear()


async def test_restart_resumes_the_saved_transfer_and_checks_kept_pages(workers):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    with b.active():
        link = transport.PeerLink(a.device_id, [served.base_url])
        async with httpx.AsyncClient(transport=failing_after(served, 2)) as client:
            with pytest.raises(transport.PeerError, match="interrupted"):
                await link.receive_state(client, engine.context())
        assert hub.status()["first_copy"]["pages_done"] == 3
        [transfer] = saved_dirs(b)
        kept = b.data_dir / "peer-receive" / transfer
        assert {p.name for p in kept.glob("*.json")} == {"manifest.json", "0.json", "1.json", "2.json"}
        # A page damaged on disk is fetched again; the others are kept.
        (kept / "1.json").write_bytes(b"{}")
        restart()
        served.requests.clear()
        async with httpx.AsyncClient(transport=served.transport()) as client:
            await transport.PeerLink(a.device_id, [served.base_url]).receive_state(client, engine.context())
        pages, manifests = page_requests(served)
        copy = hub.status()["first_copy"]
        assert manifests == 0, "the saved transfer is reused, not a new capture"
        assert pages == [1] + list(range(3, copy["pages_total"]))
        assert copy["phase"] == "done" and copy["resumed_pages"] == 2
        assert owner_pref(b, "copied") == "yes"
        assert saved_dirs(b) == [], "the copy is deleted once applied"


async def test_transfer_the_peer_no_longer_serves_is_replaced(workers):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    with b.active():
        async with httpx.AsyncClient(transport=failing_after(served, 0)) as client:
            with pytest.raises(transport.PeerError):
                await transport.PeerLink(a.device_id, [served.base_url]).receive_state(client, engine.context())
        [stale] = saved_dirs(b)
    with a.active():
        # The sender dropped that transfer (expired); its next request captures anew.
        peer_snapshot._remove(peer_snapshot._directory(stale))
        peer_snapshot._jobs.clear()
    restart()
    served.requests.clear()
    with b.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            await transport.PeerLink(a.device_id, [served.base_url]).receive_state(client, engine.context())
        pages, manifests = page_requests(served)
        assert pages[0] == 1, "the saved transfer was tried first"
        assert manifests >= 1, "then a new transfer was requested (polled while the peer captured it)"
        assert pages[1:] == list(range(len(pages) - 1)), "the new transfer downloads in full"
        assert saved_dirs(b) == []
        assert owner_pref(b, "copied") == "yes"


async def test_a_different_or_unverifiable_saved_copy_is_deleted(workers):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    with b.active():
        async with httpx.AsyncClient(transport=failing_after(served, 0)) as client:
            with pytest.raises(transport.PeerError):
                await transport.PeerLink(a.device_id, [served.base_url]).receive_state(client, engine.context())
        [first] = saved_dirs(b)
        manifest_path = b.data_dir / "peer-receive" / first / "manifest.json"
        tampered = json.loads(manifest_path.read_text(encoding="utf-8"))
        tampered["expires_at"] += 3600
        manifest_path.write_text(json.dumps(tampered), encoding="utf-8")
        assert peer_snapshot.saved_transfer(a.device_id) is None
        assert saved_dirs(b) == []
        # A new transfer replaces any other partial copy on disk.
        peer_snapshot.begin_received(dict(tampered, transfer="0" * 32))
        with a.active():
            peer_snapshot._jobs.clear()
            fresh = peer_snapshot.capture(b.device_id)
        assert fresh["transfer"] != "0" * 32
        peer_snapshot.begin_received(fresh)
        assert saved_dirs(b) == [fresh["transfer"]]
        peer_snapshot.discard_received()
        assert saved_dirs(b) == []


async def test_an_apply_cut_off_by_cancellation_is_not_repeated(workers, monkeypatch):
    a, b = joining(workers)
    served = ServedBy(a, "https://a.example.com")
    started, release = threading.Event(), threading.Event()
    calls = []
    apply = peer_snapshot.apply

    def slow(*args, **kwargs):
        calls.append(args[0]["transfer"])
        started.set()
        assert release.wait(5)
        return apply(*args, **kwargs)

    monkeypatch.setattr(peer_snapshot, "apply", slow)
    with b.active():
        async with httpx.AsyncClient(transport=served.transport()) as client:
            first = asyncio.create_task(
                transport.PeerLink(a.device_id, [served.base_url]).receive_state(client, engine.context()))
            assert await asyncio.to_thread(started.wait, 5)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            served.requests.clear()
            second = asyncio.create_task(
                transport.PeerLink(a.device_id, [served.base_url]).receive_state(client, engine.context()))
            await asyncio.sleep(0.05)
            release.set()
            assert await second > 0
        assert served.requests == [], "the second attempt took the running apply's result"
        assert len(calls) == 1
        assert owner_pref(b, "copied") == "yes"
        assert hub.status()["first_copy"]["phase"] == "done"
