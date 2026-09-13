# ruff: noqa: F811
"""Interrupted account enrollment resumes without replacing local identity."""
import json

import httpx

from app.db import database
from app.db import network as store
from app.network import engine, hub, recovery
from tests.core.network.test_hub import _signed_in, fake, workers  # noqa: F401


def prepare(workers, fake, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        database.set_system_setting("tts_model", "voice-to-resume")
        engine.scan_all()
        _signed_in(fake, a)
        hub.register()
        key = recovery.create()["recovery_key"]
        hub._upload_recovery()
    with b.active():
        _signed_in(fake, b)
        hub.register()
    with a.active():
        _signed_in(fake, a)
        hub.approve_device(b.device_id, fingerprint_confirmed=True)
    from tests.core.network.test_transport import ServedBy
    served = ServedBy(a, "https://a.example.com")
    fake.approvals[b.device_id]["issuer"]["endpoints"] = [served.base_url]
    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(transport=served.transport(), **kwargs))
    return a, b, key


async def test_peer_snapshot_resumes_after_restart_without_reapproval(workers, fake, monkeypatch):
    _, b, key = prepare(workers, fake, monkeypatch)
    copy = hub._bootstrap_from_peers
    async def interrupted(*args):
        raise hub.HubError("Peer interrupted")
    with b.active():
        _signed_in(fake, b)
        original_identity = engine.identity_mod.load().public_summary()
        monkeypatch.setattr(hub, "_bootstrap_from_peers", interrupted)
        assert await hub._loop_body() is False
        assert engine.context().bootstrap_state == "syncing"
        marker = store.get_join(hub._bootstrap_id())
        assert key not in json.dumps(marker)
        engine.reset_for_tests()
        hub.reset_for_tests()
        monkeypatch.setattr(hub, "_bootstrap_from_peers", copy)
        assert await hub._loop_body() is True
        assert engine.context().identity.public_summary() == original_identity
        assert engine.context().bootstrap_state == "ready"
        assert database.get_system_setting("tts_model") == "voice-to-resume"
        assert store.get_join(hub._bootstrap_id()) is None


async def test_marker_is_durable_before_network_state_is_installed(workers, fake, monkeypatch):
    _, b, _ = prepare(workers, fake, monkeypatch)
    enroll = engine.enroll_from_response

    def interrupted(*args, **kwargs):
        raise hub.HubError("Process stopped before enrollment")

    with b.active():
        _signed_in(fake, b)
        monkeypatch.setattr(engine, "enroll_from_response", interrupted)
        assert await hub._loop_body() is False
        assert engine.context() is None
        assert store.get_join(hub._bootstrap_id()) is not None
        engine.reset_for_tests()
        hub.reset_for_tests()
        monkeypatch.setattr(engine, "enroll_from_response", enroll)
        assert await hub._loop_body() is True
        assert engine.context().bootstrap_state == "ready"
        assert database.get_system_setting("tts_model") == "voice-to-resume"


async def test_resume_waits_for_same_account_connection(workers, fake, monkeypatch):
    _, b, _ = prepare(workers, fake, monkeypatch)
    replay = hub._bootstrap_from_peers
    async def offline(*args):
        raise hub.HubError("Offline")
    monkeypatch.setattr(hub, "_bootstrap_from_peers", offline)
    with b.active():
        _signed_in(fake, b)
        assert await hub._loop_body() is False
        marker = store.get_join(hub._bootstrap_id())
        monkeypatch.setattr(hub, "_bootstrap_from_peers", replay)
        fake.servers["current"] = None
        assert await hub._loop_body() is False
        fake.servers["current"] = "different-server"
        before = len(fake.calls)
        assert await hub._loop_body() is False
        assert "different worker or account" in hub.status()["last_error"]
        assert len(fake.calls) == before, "do not read a different account's feed"
        assert store.get_join(hub._bootstrap_id()) == marker
        assert engine.context().bootstrap_state == "syncing"
        _signed_in(fake, b)
        assert await hub._loop_body() is True
        assert engine.context().bootstrap_state == "ready"


async def test_hub_does_not_complete_direct_invite_snapshot(workers, fake, monkeypatch):
    _, b, _ = prepare(workers, fake, monkeypatch)
    response = fake.approvals[b.device_id]
    with b.active():
        _signed_in(fake, b)
        engine.enroll_from_response(response, device_name="B")
        store.create_join({"id": "direct-invite", "role": "joiner", "status": "syncing",
                           "device_id": b.device_id, "name": "B", "response": response,
                           "endpoint_url": "https://peer.example"})
        before = len(fake.calls)
        assert await hub._loop_body() is False
        assert engine.context().bootstrap_state == "syncing"
        assert store.get_join("direct-invite")["response"] == response
        assert store.get_join(hub._bootstrap_id()) is None
        assert not any("feed?" in call[2] for call in fake.calls[before:])
