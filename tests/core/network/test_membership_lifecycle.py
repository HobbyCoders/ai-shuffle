"""Membership generations, snapshots, recovery, rejoin and the transition lock.

Several in-process workers run the real engine; each test asserts what every
worker holds afterwards. ``*_guard_bites`` tests switch one guard off with a
monkeypatch (never by editing production code) and require the scenario to
fail without it.
"""
import threading
import time

import httpx
import pytest

from app.core import account_hub, turn_manager
from app.db import database
from app.db import mobile_hub as binding_store
from app.db import network as store
from app.db.base import get_db
from app.network import crypto, engine, envelope as envelope_mod, hub, identity, membership, recovery, transport
from tests.core.network.fake_hub import FakeHub
from tests.core.network.harness import converge, exchange, join, owner_pref, set_owner_pref
from tests.core.network.test_ownership_revocation import Network, _network


def _generation(worker):
    with worker.active():
        return engine.context().generation, store.get_state()["generation"]


def _active(worker):
    with worker.active():
        return {m.device_id for m in engine.context().active_members()}


def _setting(worker, key):
    with worker.active():
        return database.get_system_setting(key)


# ---------------------------------------------------------------------------
# Generations
# ---------------------------------------------------------------------------

def test_generations_rise_with_membership_and_never_go_back(workers):
    a, b, c = _network(workers, "a", "b", "c")
    assert {_generation(w) for w in (a, b, c)} == {(2, 2)}  # founder 0, B at 1, C at 2
    # Two members admit a device each while partitioned: both at generation 3.
    d, e = workers("d"), workers("e")
    join(a, d)
    join(b, e)
    assert _generation(a) == _generation(b) == (3, 3)
    converge([a, b, c])
    for worker in (a, b, c):
        assert _generation(worker) == (3, 3)
        assert _active(worker) == {a.device_id, b.device_id, c.device_id, d.device_id, e.device_id}
    with a.active():
        assert engine.revoke_device(e.device_id)["generation"] == 4
    converge([a, b, c])
    for worker in (a, b, c):
        assert _generation(worker) == (4, 4)
        assert e.device_id not in _active(worker)
    # Replaying the whole membership history (older generations) changes nothing.
    with a.active():
        history = [o for o in store.ops_since({}) if o["collection"] == "membership"]
    for worker in (b, c):
        with worker.active():
            store_marks = store.local_watermarks()
            replay = engine.apply_batch(history)
            assert replay.applied == 0 and replay.rejected == 0
            assert store.local_watermarks() == store_marks
        assert _generation(worker) == (4, 4)


async def test_generation_bump_invalidates_peer_tokens_and_callers_reauthenticate(workers, monkeypatch):
    a, b = _network(workers, "a", "b")
    net = Network(monkeypatch, a=a)
    interrupted = []

    async def fake_interrupt(session_id, mode, stop_background=False, turn_id=None):
        interrupted.append((session_id, mode))
        return {"id": "t"}

    monkeypatch.setattr(turn_manager, "interrupt_async", fake_interrupt)
    with a.active():
        database.create_session(session_id="s", profile_id="default", project_id=None, title="s")
        engine.scan_all()
    converge([a, b])
    from app.network import control
    with b.active():
        store.touch_member(a.device_id, endpoints=[net.url("a")])
        await control.interrupt(a.device_id, "s", "stop")
        token = transport._links[a.device_id].token
    # A admits a new device: its membership generation moves on.
    join(a, workers("c"))
    net.requests("a").clear()
    with b.active():
        await control.interrupt(a.device_id, "s", "stop")
        assert transport._links[a.device_id].token != token
    assert net.requests("a") == [
        ("POST", "/api/v1/network/peer/control/interrupt"),
        ("POST", "/api/v1/network/peer/auth/challenge"),
        ("POST", "/api/v1/network/peer/auth/token"),
        ("POST", "/api/v1/network/peer/control/interrupt"),
    ]
    assert interrupted == [("s", "stop"), ("s", "stop")]


# ---------------------------------------------------------------------------
# Snapshots
# ---------------------------------------------------------------------------

def _resign(chunk, signing_private, **changes):
    forged = {k: v for k, v in chunk.items() if k != "signature"}
    forged.update(changes)
    forged["signature"] = crypto.b64u_encode(crypto.sign(signing_private, crypto.canonical_json(forged)))
    return forged


def test_forged_or_stale_sender_snapshot_chunks_are_refused(workers):
    a, b, c = _network(workers, "a", "b", "c")
    with a.active():
        database.set_system_setting("tts_model", "from-a")
        engine.scan_all()
        chunk = engine.snapshot_chunk("system_settings")
        a_key = a.identity.signing_private
    with b.active():
        database.set_system_setting("tts_model", "from-b")
        engine.scan_all()
        b_chunk = engine.snapshot_chunk("system_settings")
    cases = {
        "snapshot signature is invalid": dict(chunk, count=chunk["count"] + 1),
        "snapshot belongs to another network": _resign(chunk, a_key, network_id="net_elsewhere"),
        "snapshot hash mismatch": _resign(chunk, a_key, content_hash="0" * 64),
        "malformed snapshot chunk": {k: v for k, v in chunk.items() if k != "signature"},
        "snapshot sender is not an active member": dict(chunk, device_id="dev_000000000000000000000000"),
    }
    with c.active():
        for reason, forged in cases.items():
            with pytest.raises(engine.ApplyError, match=reason):
                engine.apply_snapshot_chunk(forged)
        assert database.get_system_setting("tts_model") is None
        with get_db() as conn:
            assert store.get_record(conn.cursor(), "system_settings", "tts_model") is None
    # B is removed; a page it signed before is no longer a source of truth,
    # even though B's record is newer than A's.
    with a.active():
        engine.revoke_device(b.device_id)
    converge([a, c])
    assert _setting(c, "tts_model") == "from-a"
    with c.active():
        with pytest.raises(engine.ApplyError, match="not an active member"):
            engine.apply_snapshot_chunk(b_chunk)
        assert engine.apply_snapshot_chunk(chunk) == 0  # A's page holds nothing newer
    assert _setting(c, "tts_model") == _setting(a, "tts_model") == "from-a"


def test_stale_snapshot_keeps_newer_records_and_lists_the_loser(workers):
    a, b, c = _network(workers, "a", "b", "c")
    with a.active():
        database.set_system_setting("tts_model", "old")
        database.create_tag(tag_id="t1", name="doomed", color="#000")
        engine.scan_all()
        stale = engine.snapshot_chunk("system_settings")
    converge([a, b, c])
    with b.active():
        database.set_system_setting("tts_model", "new")
        engine.scan_all()
    exchange(b, c)
    with a.active():
        database.delete_tag("t1")
        engine.scan_all()
        tags = engine.snapshot_chunk("tags")
    with c.active():
        # The stale page is older than what C holds: nothing is replaced.
        assert engine.apply_snapshot_chunk(stale) == 0
        assert database.get_system_setting("tts_model") == "new"
        losers = [x for x in store.list_conflicts() if x["record_id"] == "tts_model"]
        assert losers and losers[0]["losing_device_id"] == a.device_id
        assert losers[0]["losing_record"]["value"] in ("old", '"old"')
        # A tombstone in a page deletes the row the page is newer than.
        assert database.get_tag("t1") is not None
        assert engine.apply_snapshot_chunk(tags) >= 1
        assert database.get_tag("t1") is None
        with get_db() as conn:
            assert store.get_record(conn.cursor(), "tags", "t1")["deleted"]
    # The tombstone op itself arriving later is a no-op on C.
    exchange(a, c)
    with c.active():
        assert database.get_tag("t1") is None and database.get_system_setting("tts_model") == "new"
    for worker in (a, b):
        exchange(c, worker)
    assert {_setting(w, "tts_model") for w in (a, b, c)} == {"new"}


# ---------------------------------------------------------------------------
# Recovery: wrap upload to the hub, unlock with every worker off, rotation
# ---------------------------------------------------------------------------

@pytest.fixture
def fake_hub(monkeypatch):
    fake = FakeHub()
    servers = {}
    monkeypatch.setattr(account_hub, "call", fake.call)
    monkeypatch.setattr(binding_store, "get_binding", lambda: None if servers.get("current") is None else {
        "hub_url": "https://hub.example", "server_id": servers["current"], "linked_by": "managed:x",
        "active": True, "encrypted_secret": "enc:x"})
    fake.servers = servers
    yield fake
    hub.reset_for_tests()


def _unlock_with_recovery(worker, text, wrap, *, name="Restored"):
    """What a fresh worker does with the recovery key and the hub's public wrap."""
    key = recovery.unwrap(text, wrap)
    with worker.active():
        ident = worker.identity
        cert = membership.issue_recovery_certificate(
            network_id=wrap["network_id"], network_key=key, signing_private=ident.signing_private,
            encryption_public=ident.encryption_public, name=name, kind="worker", generation=1)
        engine.enroll_from_response({
            "network_id": wrap["network_id"], "name": None, "founder_device_id": wrap["created_by"],
            "generation": 1, "key_version": wrap["key_version"],
            "keyring": {str(wrap["key_version"]): crypto.wrap_key(ident.encryption_public, key)},
            "certificate": cert, "membership_records": [], "members": [], "legacy_instance_map": {},
        }, device_name=name)
        engine.finish_bootstrap()
    return key


def _recovery_round_trip(workers, fake_hub):
    a = workers("a")
    with a.active():
        engine.create_network("Recoverable")
        database.set_system_setting("tts_model", "voice-before-outage")
        engine.scan_all()
        text = recovery.create()["recovery_key"]
        fake_hub.servers["current"] = "server-a"
        hub.register()
        hub._upload_recovery()
        wrap = store.get_state()["recovery"]
        network_key = engine.context().network_key
    assert fake_hub.recovery == wrap
    # Every worker is off. A fresh device reads the public wrap from the hub.
    d = workers("d")
    fake_hub.servers["current"] = "server-d"
    with d.active():
        summary = hub._summary_or_none()
    assert _unlock_with_recovery(d, text, summary["recovery"]) == network_key
    with d.active():
        ctx = engine.context()
        assert ctx.bootstrap_state == "ready" and ctx.member(d.device_id).status == "active"
        first = store.ops_since({})[0]
        assert (first["collection"], first["origin_seq"]) == ("membership", 1)  # its certificate leads
    # A comes back: each side learns the other from self-certifying records.
    converge([a, d])
    for worker in (a, d):
        assert _active(worker) == {a.device_id, d.device_id}
    assert _setting(d, "tts_model") == "voice-before-outage"
    set_owner_pref(d, "written-after-recovery", True)
    exchange(d, a)
    assert owner_pref(a, "written-after-recovery") is True

    # Removing D rotates the key; the recovery wrap goes stale everywhere.
    fake_hub.servers["current"] = "server-a"
    with a.active():
        engine.revoke_device(d.device_id)
        hub._upload_recovery()
        local = store.get_state()["recovery"]
        assert local.get("stale") and local.get("hub_tombstoned")
        assert recovery.status()["configured"] is False
    fake_hub.servers["current"] = "server-e"
    e = workers("e")
    with e.active():
        assert hub._summary_or_none()["recovery"] is None
    with pytest.raises(recovery.RecoveryError, match="stale"):
        recovery.unwrap(text, local)
    # A copy of the old wrap still opens the old key, but a certificate proven
    # under it no longer admits anyone.
    _unlock_with_recovery(e, text, wrap, name="Too late")
    with e.active():
        late = store.ops_since({})
    with a.active():
        result = engine.apply_batch(late)
        assert result.applied == 0 and engine.context().member(e.device_id) is None


def test_recovery_wrap_round_trips_through_the_hub_and_goes_stale_on_rotation(workers, fake_hub):
    _recovery_round_trip(workers, fake_hub)


def test_recovery_guard_bites_without_stale_marking(workers, fake_hub, monkeypatch):
    """Without marking the wrap stale on rotation, the hub keeps serving it."""
    monkeypatch.setattr(recovery, "refresh_wrap_after_rotation", lambda **kwargs: False)
    with pytest.raises((AssertionError, pytest.fail.Exception)):
        _recovery_round_trip(workers, fake_hub)


def test_recovery_key_for_one_network_does_not_open_another(workers):
    a, z = workers("a"), workers("z")
    with a.active():
        engine.create_network("A")
        text = recovery.create()["recovery_key"]
    with z.active():
        engine.create_network("Z")
        recovery.create()
        other = store.get_state()["recovery"]
    with pytest.raises(recovery.RecoveryError, match="does not belong"):
        recovery.unwrap(text, other)


# ---------------------------------------------------------------------------
# Offline rejoin: replay from the cursor, apply each operation once
# ---------------------------------------------------------------------------

def test_offline_worker_catches_up_in_pages_from_several_peers_without_reapplying(workers):
    a, b, c = _network(workers, "a", "b", "c")
    # C is offline while A and B keep writing (and hear each other).
    for i in range(12):
        set_owner_pref(a, f"a{i}", i)
    for i in range(5):
        set_owner_pref(b, f"b{i}", i)
    set_owner_pref(c, "written-offline", "c")
    converge([a, b])
    with c.active():
        before = store.local_watermarks()
    with a.active():
        missing = len(store.ops_since(before, exclude_origin=c.device_id))
    assert missing >= 17
    # C resumes from its cursor: two small pages from A, then the rest from B.
    first = exchange(a, c, limit=4)
    second = exchange(a, c, limit=4)
    assert first.applied == 4 and second.applied == 4
    rest = exchange(b, c)
    assert rest.applied == missing - 8 and rest.rejected == 0 and rest.pending == 0
    with c.active():
        after = store.local_watermarks()
        assert after[a.device_id]["1"] > before[a.device_id]["1"]
        # A page delivered again, and the same ops over another route, apply nothing.
        with a.active():
            replay = store.ops_since(before, exclude_origin=c.device_id)
        again = engine.apply_batch(replay)
        assert again.applied == 0 and again.duplicate == len(replay)
        assert store.local_watermarks() == after
        assert store.pending_holes() == [] and store.list_conflicts() == []
    assert exchange(a, c).applied == 0 and exchange(b, c).applied == 0
    # C's offline write reaches both, once.
    converge([a, b, c])
    for worker in (a, b, c):
        assert [owner_pref(worker, f"a{i}") for i in range(12)] == list(range(12))
        assert [owner_pref(worker, f"b{i}") for i in range(5)] == list(range(5))
        assert owner_pref(worker, "written-offline") == "c"
        with worker.active():
            ops = [o for o in store.ops_since({}) if o["record_id"].endswith("|written-offline")]
            assert len(ops) == 1
            assert store.pending_holes() == []
    marks = []
    for worker in (a, b, c):
        with worker.active():
            marks.append(store.local_watermarks())
    assert marks[0] == marks[1] == marks[2]


async def test_rejoining_worker_catches_up_over_http_in_bounded_batches(workers, monkeypatch):
    a, c = _network(workers, "a", "c")
    net = Network(monkeypatch, a=a)
    for i in range(12):
        set_owner_pref(a, f"k{i}", i)
    set_owner_pref(c, "from-c", 1)
    monkeypatch.setattr(transport, "BATCH_LIMIT", 5)
    async with httpx.AsyncClient() as client:
        with c.active():
            link = transport.PeerLink(a.device_id, [net.url("a")])
            summary = await link.sync_once(client, engine.context(), catch_up=True)
    exchanges = [p for _, p in net.requests("a") if p.endswith("/peer/exchange")]
    assert len(exchanges) >= 3  # 12 ops in pages of five
    assert summary["received"] >= 12 and summary["sent"] >= 1
    assert [owner_pref(c, f"k{i}") for i in range(12)] == list(range(12))
    assert owner_pref(a, "from-c") == 1
    with a.active():
        marks_a = store.local_watermarks()
    with c.active():
        assert store.local_watermarks() == marks_a


async def test_catch_up_stops_when_a_peer_cannot_fill_the_gap(workers, monkeypatch):
    a, c = _network(workers, "a", "c")
    net = Network(monkeypatch, a=a)
    set_owner_pref(a, "unfillable", 1)
    # C cannot apply anything it is sent (for instance the key is still on its way).
    monkeypatch.setattr(engine, "apply_batch", lambda ops, **kwargs: engine.ApplyResult(retry=len(ops)))
    async with httpx.AsyncClient() as client:
        with c.active():
            link = transport.PeerLink(a.device_id, [net.url("a")])
            with pytest.raises(transport.PeerError, match="could not fill"):
                await link.sync_once(client, engine.context(), catch_up=True)
    assert owner_pref(c, "unfillable") is None
    assert len([p for _, p in net.requests("a") if p.endswith("/peer/exchange")]) == 1


# ---------------------------------------------------------------------------
# Leave/create/join transition lock vs. an in-flight rollback claim
# ---------------------------------------------------------------------------

def _leave_during_claim(worker):
    """Run ``restored_from_backup`` (a verified rollback claim) and ``leave_network`` concurrently.

    Returns the order in which the claim's incarnation bump and the leave finished.
    """
    with worker.active():
        ctx = engine.context()
        with get_db() as conn:
            held = store.own_watermark(conn.cursor(), ctx.device_id, ctx.identity.incarnation)
        evidence = dict(evidence_seq=held + 5, evidence_incarnation=ctx.identity.incarnation,
                        evidence_network_id=ctx.network_id, evidence_device_id=ctx.device_id)
    order, errors = [], []
    in_bump, release = threading.Event(), threading.Event()
    real_bump = identity.bump_incarnation

    def slow_bump():
        in_bump.set()
        assert release.wait(5)
        result = real_bump()
        order.append("bump")
        return result

    def claim():
        try:
            engine.restored_from_backup(**evidence)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def leave():
        try:
            engine.leave_network()
            order.append("leave")
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    # The worker's routing is process-wide, so both threads act as ``worker``.
    identity.bump_incarnation = slow_bump
    try:
        worker_context = worker.active()
        worker_context.__enter__()
        claimer = threading.Thread(target=claim)
        claimer.start()
        assert in_bump.wait(5)
        leaver = threading.Thread(target=leave)
        leaver.start()
        time.sleep(0.2)  # give an unguarded leave time to overtake the claim
        release.set()
        claimer.join(5)
        leaver.join(5)
        worker_context.__exit__(None, None, None)
    finally:
        identity.bump_incarnation = real_bump
    assert not errors, errors
    return order


def test_leave_waits_for_an_in_flight_rollback_claim(workers):
    a, b = _network(workers, "a", "b")
    assert _leave_during_claim(a) == ["bump", "leave"]
    with a.active():
        # The claim finished under the network it was proven for; then A left.
        assert identity.load().incarnation == 2
        assert engine.context() is None and store.get_state() is None
        assert store.count_ops() == 0
    # B is unaffected and still lists A (A never told anyone it left).
    assert a.device_id in _active(b)


def test_transition_lock_guard_bites(workers, monkeypatch):
    """An unguarded leave overtakes the claim: the device bumps its stream after leaving."""
    a, _b = _network(workers, "a", "b")
    monkeypatch.setattr(engine, "leave_network", engine.leave_network.__wrapped__)
    assert _leave_during_claim(a) == ["leave", "bump"]


async def test_leave_between_proof_and_claim_does_not_bump_or_rejoin(workers, monkeypatch):
    """The network is left while a peer's rollback proof is in flight."""
    a, b = _network(workers, "a", "b")
    with a.active():
        old = engine.context()
        proof = envelope_mod.build(
            network_id=old.network_id, collection="user_preferences", record_id="admin|@owner|lost", op="upsert",
            clock=999, origin_device_id=old.device_id, origin_incarnation=old.identity.incarnation,
            origin_seq=1000, schema_version=1, key_version=old.key_version, network_key=old.network_key,
            signing_private=old.identity.signing_private, record={"key": "lost"})
        link = transport.PeerLink(b.device_id, ["http://b.local:8000"])
        link.token, link.token_expires = "dvt_test", time.time() + 300  # authenticated, as in a sync pass

        async def answer(*args, **kwargs):
            return {"op": proof.to_dict()}

        monkeypatch.setattr(link, "_request", answer)
        recover = engine.restored_from_backup

        def leave_first(*args, **kwargs):
            engine.leave_network()
            return recover(*args, **kwargs)

        monkeypatch.setattr(engine, "restored_from_backup", leave_first)
        assert await link._check_rollback(None, old, {old.device_id: {"1": 1000}}) is False
        assert engine.context() is None and store.get_state() is None
        assert identity.load().incarnation == old.identity.incarnation == 1
        # A later batch carrying the same proof finds no network to act on.
        with pytest.raises(engine.NotEnrolled):
            engine.apply_batch([proof.to_dict()])
    with a.active():
        assert identity.load().incarnation == 1


def test_create_and_join_refuse_while_enrolled_and_leave_refuses_while_syncing(workers):
    a, b = _network(workers, "a", "b")
    with a.active():
        with pytest.raises(RuntimeError, match="already belongs"):
            engine.create_network("Second")
        response = {"keyring": {}, "network_id": "net_x", "founder_device_id": a.device_id}
        with pytest.raises(RuntimeError, match="already belongs"):
            engine.enroll_from_response(response, device_name="again")
    c = workers("c")
    with c.active():
        with pytest.raises(RuntimeError, match="does not belong"):
            engine.leave_network()
    # A joiner still copying its snapshot cannot switch networks mid-way.
    with c.active():
        public = identity.load().public_summary()
    with a.active():
        admitted = engine.admit_device(device_id=public["device_id"],
                                       signing_public=crypto.b64u_decode(public["signing_public"]),
                                       encryption_public=crypto.b64u_decode(public["encryption_public"]),
                                       name="C")
    with c.active():
        engine.enroll_from_response(admitted, device_name="C")
        assert engine.context().bootstrap_state == "syncing"
        with pytest.raises(RuntimeError, match="Wait for the current settings sync"):
            engine.leave_network()
        # Nothing is published while syncing.
        database.set_system_setting("tts_model", "not-yet")
        assert engine.scan_all() == 0
        engine.finish_bootstrap()
        assert engine.scan_all() >= 1


def test_join_response_that_does_not_admit_this_device_is_refused(workers):
    a, b, stranger = workers("a"), workers("b"), workers("stranger")
    with a.active():
        engine.create_network("Home")
    with b.active():
        public = identity.load().public_summary()
    with a.active():
        response = engine.admit_device(device_id=public["device_id"],
                                       signing_public=crypto.b64u_decode(public["signing_public"]),
                                       encryption_public=crypto.b64u_decode(public["encryption_public"]),
                                       name="B")
    with stranger.active():
        # The keys are wrapped to B: the stranger cannot open them.
        with pytest.raises(RuntimeError, match="do not open"):
            engine.enroll_from_response(response, device_name="stranger")
        # Keys re-wrapped to the stranger, but the certificate still names B.
        rewrapped = dict(response, keyring={"1": crypto.wrap_key(stranger.identity.encryption_public,
                                                                 _network_key(a))})
        with pytest.raises(RuntimeError, match="does not admit"):
            engine.enroll_from_response(rewrapped, device_name="stranger")
        assert engine.context() is None


def _network_key(worker):
    with worker.active():
        return engine.context().network_key


def test_rollback_proof_delivered_twice_bumps_the_incarnation_once(workers):
    """Startup sync and the background link can both carry the same proof."""
    a, b = _network(workers, "a", "b")
    set_owner_pref(a, "before-restore", 1)
    with a.active():
        ctx = engine.context()
        with get_db() as conn:
            held = store.own_watermark(conn.cursor(), ctx.device_id, 1)
        proof = dict(evidence_incarnation=1, evidence_network_id=ctx.network_id, evidence_device_id=ctx.device_id)
        # A claim that does not go past what this device holds proves nothing.
        assert engine.restored_from_backup(held, **proof).identity.incarnation == 1
        # A real proof moves to a fresh stream; the same proof again is spent.
        assert engine.restored_from_backup(held + 3, **proof).identity.incarnation == 2
        assert engine.restored_from_backup(held + 3, **proof).identity.incarnation == 2
        # Proof for another network or device is ignored.
        other = dict(proof, evidence_incarnation=2)
        assert engine.restored_from_backup(held + 9, **dict(other, evidence_network_id="net_x")).identity.incarnation == 2
        assert engine.restored_from_backup(held + 9, **dict(other, evidence_device_id=b.device_id)).identity.incarnation == 2
    set_owner_pref(a, "after-restore", 2)
    exchange(a, b)
    assert owner_pref(b, "after-restore") == 2
    with b.active():
        assert "2" in store.local_watermarks()[a.device_id]
