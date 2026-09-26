# ruff: noqa: F811
"""Account hub client: refusals, deferred notices, adoption and the enrollment poll."""
import asyncio

import pytest
from fastapi import HTTPException

from app.core import account_hub
from app.db import mobile_hub as binding_store
from app.db import network as store
from app.network import crypto, engine, hub, membership, recovery
from tests.core.network.harness import join
from tests.core.network.test_hub import _signed_in, fake, workers  # noqa: F401


def _description(worker, **overrides):
    with worker.active():
        described = hub._device_description(worker.identity, worker.name)
    described.update(overrides)
    return described


async def test_nothing_is_called_without_an_account_binding(workers, fake, monkeypatch):
    a = workers("a")
    with a.active():
        assert hub.status()["linked"] is False and hub.status()["registered"] is False
        with pytest.raises(hub.HubError):
            hub.register()
        assert hub.notify_revoked("dev_x") is False
        hub.refresh_peer_endpoints()
        with pytest.raises(hub.HubError):
            await hub.adopt_account_network()
        assert await hub._loop_body() is False and hub._state["linked"] is False

        def broken():
            raise RuntimeError("database unavailable")

        monkeypatch.setattr(binding_store, "get_binding", broken)
        assert hub._binding() is None
    assert fake.calls == []


def test_admission_refuses_unproven_or_substituted_keys(workers, fake):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Home", device_name="A")
    good = _description(b)
    bad = [
        dict(good, signing_public="not base64!"),
        dict(good, device_id="dev_000000000000000000000000"),
        {k: v for k, v in good.items() if k != "proof"},
        {"name": "no id at all"},
    ]
    with a.active():
        _signed_in(fake, a)
        for device in bad[:3]:
            with pytest.raises(hub.HubError):
                hub._admit(device)
        assert hub.admit_pending_devices(bad) == 0
        with pytest.raises(hub.HubError, match="No such pending device"):
            hub.approve_device("dev_nobody")
    join(a, b)
    # A properly signed description that swaps B's encryption key for C's.
    with c.active():
        other_encryption = crypto.b64u_encode(c.identity.encryption_public)
    with b.active():
        swapped = membership.sign_device_description(dict(good, encryption_public=other_encryption),
                                                     b.identity.signing_private)
    with a.active():
        _signed_in(fake, a)
        generation = engine.context().generation
        with pytest.raises(hub.HubError, match="different keys"):
            hub._admit(swapped)
        assert engine.context().generation == generation


async def test_revocation_notice_is_queued_while_the_hub_is_down(workers, fake, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    fake.devices[b.device_id] = {**_description(b), "server_id": "server-b", "status": "active"}
    real_call = account_hub.call

    def reject_unavailable(binding, method, suffix, body=None, **kwargs):
        if suffix.endswith("/reject"):
            raise HTTPException(503, "Hub unavailable")
        return real_call(binding, method, suffix, body, **kwargs)

    with a.active():
        _signed_in(fake, a)
        hub.register()
        monkeypatch.setattr(account_hub, "call", reject_unavailable)
        engine.revoke_device(b.device_id)
        assert hub.status()["revocations_due"] == [b.device_id]
        assert fake.devices[b.device_id]["status"] == "active"
        # A second failure does not queue it twice.
        assert hub.notify_revoked(b.device_id) is False
        assert hub.status()["revocations_due"] == [b.device_id]
        monkeypatch.setattr(account_hub, "call", real_call)
        await hub._loop_body()
        assert fake.devices[b.device_id]["status"] == "rejected"
        assert hub.status()["revocations_due"] == []


def test_hub_endpoints_are_validated_and_bounded_before_they_are_stored(workers, fake, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
    join(a, b)
    advertised = ["http://user:secret@b.lan:8000", "http://169.254.169.254", "ftp://b.lan",
                  "https://b.lan:8443/"] + [f"http://10.0.0.{i}:8000" for i in range(20)]
    real_info = fake.info

    def info_with_endpoints(server, body, params):
        info = real_info(server, body, params)
        info["devices"] += [{"device_id": b.device_id, "endpoints": advertised},
                            {"device_id": a.device_id, "endpoints": ["http://self.lan"]},
                            {"device_id": "dev_stranger", "endpoints": ["http://stranger.lan"]},
                            {"device_id": b.device_id, "endpoints": "http://not-a-list"}]
        return info

    monkeypatch.setattr(fake, "info", info_with_endpoints)
    with a.active():
        _signed_in(fake, a)
        hub.register()
        hub.refresh_peer_endpoints()
        stored = store.get_member(b.device_id)["endpoints"]
        assert stored[0] == "https://b.lan:8443"
        assert len(stored) <= 8
        assert not any("@" in url or "169.254" in url or url.startswith("ftp") for url in stored)
        assert "http://self.lan" not in (store.get_member(a.device_id).get("endpoints") or [])
        assert store.get_member("dev_stranger") is None
        # Another account network's directory is never used.
        fake.network["network_id"] = "net_other"
        store.touch_member(b.device_id, endpoints=["http://kept.lan"])
        hub.refresh_peer_endpoints()
        assert store.get_member(b.device_id)["endpoints"] == ["http://kept.lan"]


def test_recovery_wrap_upload_and_single_tombstone(workers, fake):
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        _signed_in(fake, a)
        hub.register()
        hub._upload_recovery()  # nothing to upload yet
        assert not any(call[2] == "network/recovery" for call in fake.calls)
        recovery.create()
        assert hub._recovery_upload_due is True
        hub._upload_recovery()
        assert fake.recovery["network_id"] == engine.context().network_id and not hub._recovery_upload_due
        assert recovery.refresh_wrap_after_rotation() is True
        assert recovery.refresh_wrap_after_rotation() is False
        hub._upload_recovery()
        assert fake.recovery is None and store.get_state()["recovery"]["hub_tombstoned"] is True
        posts = fake.calls.count(("server-a", "POST", "network/recovery"))
        hub._upload_recovery()
        assert fake.calls.count(("server-a", "POST", "network/recovery")) == posts
        assert recovery.status()["stale"] is True


def test_founding_refuses_a_second_network_and_survives_presence_failure(workers, fake, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        _signed_in(fake, a)
        with pytest.raises(hub.HubError, match="already belongs"):
            hub.found_account_network()

    def broken_presence(**kwargs):
        raise RuntimeError("presence table locked")

    monkeypatch.setattr(engine, "publish_presence", broken_presence)
    fake.network = None
    with b.active():
        _signed_in(fake, b)
        assert hub.found_account_network(device_name="B")["status"] == "active"


async def test_alone_worker_adopts_the_account_network_and_a_synced_one_waits(workers, fake):
    a, b, c, d = workers("a"), workers("b"), workers("c"), workers("d")
    with a.active():
        _signed_in(fake, a)
        with pytest.raises(hub.HubError, match="no network"):
            await hub.adopt_account_network()
        fake.network = None
        engine.create_network("Account", device_name="A")
        hub.register()
        assert (await hub.adopt_account_network())["adopted"] is False
    # B has its own network and nobody else in it: it switches on its own.
    with b.active():
        engine.create_network("Mine", device_name="B")
        _signed_in(fake, b)
        assert await hub._loop_body() is True
        assert engine.context() is None
        assert hub.status()["status"] == "pending"
        names = [d["name"] for d in hub.status()["account_network"]["devices"]]
        assert "A" in names
    hub.reset_for_tests()
    # C syncs with D: the switch waits for an explicit adoption.
    with c.active():
        engine.create_network("Shared", device_name="C")
    join(c, d)
    with c.active():
        _signed_in(fake, c)
        assert await hub._loop_body() is True
        assert hub.status()["adoption_required"] is True and hub.status()["status"] == "conflict"
        network_id = engine.context().network_id
        assert await hub._loop_body() is True
        assert engine.context().network_id == network_id
        adopted = await hub.adopt_account_network()
        assert adopted["adopted"] is True and engine.context() is None
        assert hub.status()["adoption_required"] is False


async def test_enrollment_poll_backs_off_then_picks_up_an_approval(workers, fake):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home", device_name="A")
        _signed_in(fake, a)
        hub.register()
    with b.active():
        _signed_in(fake, b)
        assert await hub._loop_body() is False
        assert hub.status()["status"] == "pending"
        calls = len(fake.calls)
        assert await hub._loop_body() is False
        assert len(fake.calls) == calls, "the next poll waits for its back-off"
        hub._state["enrollment_poll"]["next"] = 0
        assert await hub._loop_body() is False
        assert hub._state["enrollment_poll"]["delay"] == 30
    with a.active():
        _signed_in(fake, a)
        hub.approve_device(b.device_id)
    with b.active():
        _signed_in(fake, b)
        hub._state.pop("enrollment_poll")
        # Approved: the admission is kept durably; no peer is reachable to copy data from yet.
        assert await hub._loop_body() is False
        assert "another worker" in hub.status()["last_error"]
        assert store.get_join(hub._bootstrap_id()) is not None
        assert engine.context().bootstrap_state == "syncing"


async def test_unexpected_failure_is_reported_without_details(workers, fake, monkeypatch):
    a = workers("a")
    with a.active():
        engine.create_network("Home", device_name="A")
        _signed_in(fake, a)

        def boom():
            raise RuntimeError("secret-bearing internal detail")

        monkeypatch.setattr(hub, "_ensure_hub_scope", boom)
        assert await hub._loop_body() is False
        assert hub.status()["last_error"] == "hub sync failed"


async def test_loop_wakes_on_kick_and_stops(monkeypatch):
    passes = []

    async def body():
        passes.append(1)
        return len(passes) == 1

    monkeypatch.setattr(hub, "_loop_body", body)
    monkeypatch.setattr(hub, "LOOP_ACTIVE_SECONDS", 0.01)
    monkeypatch.setattr(hub, "LOOP_IDLE_SECONDS", 30.0)
    hub.kick()  # no loop yet: nothing to wake
    hub.start()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(passes) >= 2:
            break
    assert len(passes) == 2
    hub.kick()
    for _ in range(50):
        await asyncio.sleep(0.01)
        if len(passes) >= 3:
            break
    assert len(passes) == 3
    await hub.stop()
    assert hub._loop is None and hub._tasks == []
    hub.reset_for_tests()
