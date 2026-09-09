"""Interrupted account/recovery enrollment resumes without replacing local identity."""
import json

import pytest

from app.db import database, network as store
from app.network import engine, hub, recovery
from tests.core.network.test_hub import fake, workers, _signed_in  # noqa: F401


def prepare(workers, fake, mode):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        database.set_system_setting("tts_model", "voice-to-resume")
        engine.scan_all()
        _signed_in(fake, a)
        hub.register()
        key = recovery.create()["recovery_key"]
        hub._upload_recovery()
        hub.push_once()
    if mode == "approval":
        with b.active():
            _signed_in(fake, b)
            hub.register()
        with a.active():
            _signed_in(fake, a)
            hub.approve_device(b.device_id, fingerprint_confirmed=True)
            hub.push_once()
    return a, b, key


@pytest.mark.parametrize("mode", ["approval", "recovery"])
async def test_partial_feed_resumes_after_restart_without_reapproval_or_recovery_key(workers, fake, monkeypatch, mode):
    a, b, key = prepare(workers, fake, mode)
    call = hub._call
    monkeypatch.setattr(hub, "PULL_BATCH", 1)

    def interrupted(method, suffix, *args, **kwargs):
        if suffix.startswith("feed?cursor=") and not suffix.startswith("feed?cursor=0&"):
            raise hub.HubError("Connection interrupted during initial feed")
        return call(method, suffix, *args, **kwargs)

    with b.active():
        _signed_in(fake, b)
        original_identity = engine.identity_mod.load().public_summary()
        monkeypatch.setattr(hub, "_call", interrupted)
        if mode == "recovery":
            with pytest.raises(recovery.RecoveryError, match="interrupted"):
                await recovery.unlock_from_hub(key, device_name="B")
        else:
            assert await hub._loop_body() is False
        assert engine.context().bootstrap_state == "syncing"
        marker = store.get_join(hub._bootstrap_id())
        assert marker["role"] == "hub_bootstrap"
        assert key not in json.dumps(marker), "the resume record must not store the recovery key"
        assert len(b.sql("SELECT * FROM sync_ops")) > 0, "interruption follows an applied feed page"
        # Simulate process-local state being lost while the private DB remains.
        engine.reset_for_tests()
        hub.reset_for_tests()
        monkeypatch.setattr(hub, "_call", call)
        assert await hub._loop_body() is True
        ctx = engine.context()
        assert ctx.bootstrap_state == "ready"
        assert ctx.identity.public_summary() == original_identity
        assert ctx.member(a.device_id).status == "active"
        assert ctx.member(b.device_id).status == "active"
        assert database.get_system_setting("tts_model") == "voice-to-resume"
        assert store.get_join(hub._bootstrap_id()) is None
        # A further pass is ordinary sync and does not install another identity.
        await hub._loop_body()
        assert engine.context().identity.public_summary() == original_identity


async def test_marker_is_durable_before_network_state_is_installed(workers, fake, monkeypatch):
    _, b, _ = prepare(workers, fake, "approval")
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
    _, b, _ = prepare(workers, fake, "approval")
    replay = hub._replay_feed_from_start
    monkeypatch.setattr(hub, "_replay_feed_from_start", lambda: (_ for _ in ()).throw(hub.HubError("Offline")))
    with b.active():
        _signed_in(fake, b)
        assert await hub._loop_body() is False
        marker = store.get_join(hub._bootstrap_id())
        monkeypatch.setattr(hub, "_replay_feed_from_start", replay)
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


async def test_hub_does_not_complete_direct_invite_snapshot(workers, fake):
    _, b, _ = prepare(workers, fake, "approval")
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
