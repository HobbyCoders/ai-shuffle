"""Offline catch-up: overlapping restarts and signed membership replay."""
import multiprocessing
from concurrent.futures import ProcessPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path

import pytest

from app.db import network as store
from app.network import catalog, engine, membership, transport
from tests.core.network.harness import Worker, converge, join
from tests.core.network.test_owner_sync_speed import _append, _session
from tests.core.network.test_ownership_revocation import _network


def _restore_and_flush(name, directory, image, identity, peer_id, inbox, barrier):
    """A separate process is essential: engine/identity/outbox state is global.

    Both workers publish their pre-flush frontiers before either releases its
    hold, then both flush before either consumes the other's delta. This is
    the adverse interleaving of two exchanges inside the same sync window.
    """
    # Spawn does not inherit pytest's monkeypatches. Reuse the root isolation
    # fixtures rather than letting a full scan see the real skill/plugin home.
    from tests.conftest import (
        isolate_fluid_plugin_home,
        isolate_fluid_projects_dir,
        isolate_fluid_skills_dir,
    )
    with ExitStack() as stack:
        patch = stack.enter_context(pytest.MonkeyPatch.context())
        for fixture in (isolate_fluid_skills_dir, isolate_fluid_plugin_home, isolate_fluid_projects_dir):
            stack.enter_context(contextmanager(fixture.__wrapped__)(patch, Path(directory) / name))
        return _flush_worker(name, directory, image, identity, peer_id, inbox, barrier)


def _flush_worker(name, directory, image, identity, peer_id, inbox, barrier):
    worker = Worker(name, Path(directory))
    worker.raw.deserialize(image)
    worker.identity = identity
    try:
        with worker.active():
            engine.load_context()
            engine.hold_outbox(True)
            _append(name, 130)
            assert catalog.publish_owned() == 0
            inbox[name + ":marks"] = store.local_watermarks()
            barrier.wait(timeout=20)
            other = "right" if name == "left" else "left"
            peer_marks = inbox[other + ":marks"]
            assert transport.release_hold_for_peer_view(peer_id, peer_marks[identity.device_id])
            engine.scan_all()
            ops = store.ops_since(peer_marks, limit=1000, exclude_origin=peer_id)
            inbox[name + ":ops"] = ops
            barrier.wait(timeout=20)
            incoming = inbox[other + ":ops"]
            result = engine.apply_batch(incoming)
            assert result.applied == len(incoming), (result, [(o["collection"], o["record_id"]) for o in incoming if o["collection"] != "session_events"])
            assert result.retry == result.rejected == result.pending == 0
            assert engine.apply_batch(incoming).duplicate == len(incoming)
            assert not engine.outbox_held()
            for sid in (name, other):
                assert [r["seq"] for r in worker.sql(
                    "SELECT seq FROM session_events WHERE session_id = ? ORDER BY seq", (sid,)
                )] == list(range(1, 136))
            assert len([op for op in ops if op["collection"] == "session_events"]) == 130
            own = [op for op in ops if op["origin_device_id"] == identity.device_id]
            floor = peer_marks[identity.device_id]["1"]
            assert [op["origin_seq"] for op in own] == list(range(floor + 1, floor + len(own) + 1))
            return store.local_watermarks()
    finally:
        worker.close()


def test_two_workers_restore_and_flush_in_the_same_sync_window(workers, tmp_path):
    a, b = _network(workers, "left", "right")
    for worker in (a, b):
        with worker.active():
            _session(worker.name)
            _append(worker.name, 5)
            catalog.publish_owned()
    converge([a, b])
    directory = tmp_path / "restarted"
    directory.mkdir()
    ctx = multiprocessing.get_context("spawn")
    with ctx.Manager() as manager:
        inbox, barrier = manager.dict(), manager.Barrier(2)
        with ProcessPoolExecutor(max_workers=2, mp_context=ctx) as pool:
            futures = [pool.submit(_restore_and_flush, w.name, str(directory), w.raw.serialize(),
                                   w.identity, peer.device_id, inbox, barrier)
                       for w, peer in ((a, b), (b, a))]
            heads = [f.result(timeout=60) for f in futures]
    assert heads[0] == heads[1]


@pytest.mark.parametrize("reverse_ties", [False, True])
def test_signed_enrollment_replays_tied_generations(workers, monkeypatch, reverse_ties):
    a, b = _network(workers, "issuer-a", "issuer-b")
    c, d = workers("child-a"), workers("child-b")
    # Partitioned issuers independently allocate the same generation.
    join(a, c)
    join(b, d)
    converge([a, b])
    newcomer = workers("newcomer")
    with a.active():
        response = engine.admit_device(device_id=newcomer.device_id,
            signing_public=newcomer.identity.signing_public,
            encryption_public=newcomer.identity.encryption_public, name="Newcomer")
        # Reproduce tied receive timestamps in the *stored, signed* history.
        a.sql("UPDATE network_membership_records SET created_at = ?", ("2026-01-01T00:00:00Z",))
        records = [item["record"] for item in store.list_membership_records()]
    tied = [r for r in records if r.get("generation") == 2]
    assert {r["device_id"] for r in tied} == {c.device_id, d.device_id}
    if reverse_ties:
        records = [r for r in records if r.get("generation") < 2] + list(reversed(tied)) + [
            r for r in records if r.get("generation") > 2]
    response["membership_records"] = records
    with newcomer.active():
        installed = engine.enroll_from_response(response, device_name="Newcomer")
        assert {m.device_id for m in installed.active_members()} == {
            w.device_id for w in (a, b, c, d, newcomer)}
        assert installed.generation == response["generation"]


@pytest.mark.parametrize("old_generation", [1, 2])
def test_signed_enrollment_cannot_replay_certificate_at_or_before_revocation(workers, old_generation):
    a, b = _network(workers, "issuer", "removed")
    newcomer = workers("newcomer")
    with a.active():
        ctx = engine.context()
        old = membership.issue_certificate(network_id=ctx.network_id,
            issuer_device_id=a.device_id, issuer_signing_private=a.identity.signing_private,
            device_id=b.device_id, signing_public=b.identity.signing_public,
            encryption_public=b.identity.encryption_public, name="Removed", kind=membership.KIND_WORKER,
            generation=old_generation)
        assert engine.revoke_device(b.device_id)["generation"] == 2
        response = engine.admit_device(device_id=newcomer.device_id,
            signing_public=newcomer.identity.signing_public,
            encryption_public=newcomer.identity.encryption_public, name="Newcomer")
    # A replay arriving late must not reactivate the removed member, whether
    # its generation ties the revocation or regresses behind it.
    response["membership_records"].append(old)
    with newcomer.active():
        installed = engine.enroll_from_response(response, device_name="Newcomer")
        assert installed.member(b.device_id).status == membership.STATUS_REVOKED
        assert installed.member(b.device_id).generation == 2
        assert store.get_member(b.device_id)["status"] == membership.STATUS_REVOKED


def test_signed_enrollment_regressing_certificate_cannot_downgrade_active_member(workers):
    a, b = _network(workers, "issuer", "readmitted")
    newcomer = workers("newcomer")
    with a.active():
        old = next(item["record"] for item in store.list_membership_records()
                   if item["record"].get("device_id") == b.device_id)
        engine.revoke_device(b.device_id)
        latest = engine.admit_device(device_id=b.device_id,
            signing_public=b.identity.signing_public, encryption_public=b.identity.encryption_public,
            name="Readmitted")["certificate"]
        response = engine.admit_device(device_id=newcomer.device_id,
            signing_public=newcomer.identity.signing_public,
            encryption_public=newcomer.identity.encryption_public, name="Newcomer")
    response["membership_records"].append(old)
    with newcomer.active():
        installed = engine.enroll_from_response(response, device_name="Newcomer")
        assert installed.member(b.device_id).generation == latest["generation"]
        assert installed.member(b.device_id).name == "Readmitted"
        assert store.get_member(b.device_id)["generation"] == latest["generation"]


@pytest.mark.parametrize("revocation_first", [True, False])
def test_revocation_and_offline_delta_in_one_batch(workers, revocation_first):
    a, b, c = _network(workers, "revoker", "offline", "receiver")
    with b.active():
        _session("revoked-chat")
        catalog.publish_owned()
    converge([a, b, c])
    with c.active():
        marks = store.local_watermarks()
    with b.active():
        _append("revoked-chat", 8)
        catalog.publish_owned()
        delta = store.ops_since(marks, exclude_origin=c.device_id)
    with a.active():
        engine.revoke_device(b.device_id)
        revocation = store.ops_since(marks, exclude_origin=c.device_id)
    with c.active():
        result = engine.apply_batch(revocation + delta if revocation_first else delta + revocation)
        assert result.retry == result.pending == 0
        assert engine.context().member(b.device_id).status == membership.STATUS_REVOKED
        # Already-materialized transcript data is retained by policy, but a
        # revocation learned first must prevent the late data from landing.
        rows = c.sql("SELECT seq FROM session_events WHERE session_id = ?", ("revoked-chat",))
        assert len(rows) == (0 if revocation_first else 8)
        again = engine.apply_batch(delta)
        assert again.applied == 0 and again.retry == 0
