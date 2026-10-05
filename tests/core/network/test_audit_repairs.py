"""Regressions for the September 12 worker-network audit (docs/reviews/WORKER_NETWORK_AUDIT_2026-09-12.md).

Each test names the finding it pins down. They run several workers in one
process through the real engine, like the rest of this package.
"""
import sqlite3

import pytest

from app.db import database
from app.db import network as store
from app.network import crypto, engine, envelope as envelope_mod, identity as identity_mod, membership, tokens, transport
from tests.core.network.harness import Worker, converge, exchange, join, owner_pref, set_owner_pref


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
    engine.hold_outbox(False)


def _pref_ops(worker, *, exclude=None):
    with worker.active():
        ops = store.ops_since({}, exclude_origin=exclude)
        return [o for o in ops if o["origin_device_id"] == worker.device_id and o["collection"] == "user_preferences"]


# ---------------------------------------------------------------------------
# NET-01: revocation is incarnation-qualified; a revoked founder stays out
# ---------------------------------------------------------------------------

def test_revoked_device_cannot_write_from_a_new_incarnation(workers):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    join(a, c)
    converge([a, b, c])
    set_owner_pref(b, "before_revocation", "kept")
    converge([a, b, c])
    with a.active():
        engine.revoke_device(b.device_id, reason="lost")
        revoked = engine.context().member(b.device_id)
        assert revoked.last_incarnation_accepted == 1 and revoked.last_seq_accepted >= 1
    converge([a, c])
    # B restarts its stream as if restored from a backup and keeps writing.
    with b.active():
        identity_mod.bump_incarnation()
        engine.load_context()
    set_owner_pref(b, "after_revocation", "smuggled")
    ops = [o for o in _pref_ops(b) if o["origin_incarnation"] == 2]
    assert ops
    for member in (a, c):
        with member.active():
            result = engine.apply_batch(ops)
            assert result.applied == 0 and result.rejected == len(ops)
            assert all(r == "origin revoked" for r in result.reasons)
        assert owner_pref(member, "after_revocation") is None


def test_revoked_founder_cannot_recertify_itself(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    with b.active():
        ctx = engine.context()
        # The founder's original certificate replays while it is the known founder (idempotent trust root).
        original = next(r["record"] for r in store.list_membership_records()
                        if r["record"].get("type") == "certificate" and r["record"]["device_id"] == a.device_id)
        membership.apply_record(original, network_id=ctx.network_id, members=dict(ctx.members),
                                founder_device_id=ctx.founder_device_id)
        # A second self-signed certificate at a new generation is not a trust root.
        forged = membership.issue_certificate(
            network_id=ctx.network_id, issuer_device_id=a.device_id, issuer_signing_private=a.identity.signing_private,
            device_id=a.device_id, signing_public=a.identity.signing_public,
            encryption_public=a.identity.encryption_public, name="A again", kind="worker",
            generation=ctx.generation + 1)
        with pytest.raises(membership.MembershipError, match="already established"):
            membership.apply_record(forged, network_id=ctx.network_id, members=dict(ctx.members),
                                    founder_device_id=ctx.founder_device_id)
        engine.revoke_device(a.device_id, reason="compromised")
        ctx = engine.context()
        assert ctx.member(a.device_id).status == "revoked"
        later = membership.issue_certificate(
            network_id=ctx.network_id, issuer_device_id=a.device_id, issuer_signing_private=a.identity.signing_private,
            device_id=a.device_id, signing_public=a.identity.signing_public,
            encryption_public=a.identity.encryption_public, name="A again", kind="worker",
            generation=ctx.generation + 1)
        with pytest.raises(membership.MembershipError, match="already established"):
            membership.apply_record(later, network_id=ctx.network_id, members=dict(ctx.members),
                                    founder_device_id=ctx.founder_device_id)


# ---------------------------------------------------------------------------
# NET-02 / NET-09: admission needs proof of key possession
# ---------------------------------------------------------------------------

def test_device_description_proof_binds_the_encryption_key(workers):
    a = workers("a")
    description = membership.sign_device_description({
        "device_id": a.device_id, "signing_public": crypto.b64u_encode(a.identity.signing_public),
        "encryption_public": crypto.b64u_encode(a.identity.encryption_public), "kind": "worker", "name": "A",
    }, a.identity.signing_private)
    assert membership.verify_device_description(description)
    substituted = dict(description, encryption_public=crypto.b64u_encode(crypto.encryption_public_key(
        crypto.generate_encryption_key())))
    assert not membership.verify_device_description(substituted)
    assert not membership.verify_device_description({k: v for k, v in description.items() if k != "proof"})
    assert not membership.verify_device_description(dict(description, device_id="dev_" + "0" * 24))


@pytest.mark.parametrize("tampering", ["missing-proof", "substituted-key"])
def test_manual_hub_approval_cannot_bypass_device_key_proof(workers, monkeypatch, tampering):
    from app.network import hub

    a, b = workers("a"), workers("b")
    description = membership.sign_device_description({
        "device_id": b.device_id, "signing_public": crypto.b64u_encode(b.identity.signing_public),
        "encryption_public": crypto.b64u_encode(b.identity.encryption_public), "kind": "worker", "name": "B",
    }, b.identity.signing_private)
    untrusted = dict(description)
    if tampering == "missing-proof":
        untrusted.pop("proof")
    else:
        untrusted["encryption_public"] = crypto.b64u_encode(crypto.encryption_public_key(crypto.generate_encryption_key()))
    sent = []
    monkeypatch.setattr(hub, "pending_devices", lambda: [untrusted])
    monkeypatch.setattr(hub, "_call", lambda *args, **kwargs: sent.append(args) or {})
    with a.active():
        engine.create_network("Home")
        with pytest.raises(hub.HubError, match="proof"):
            hub.approve_device(b.device_id, fingerprint_confirmed=True)
        assert engine.context().member(b.device_id) is None
        assert sent == []
        # The producing signer and the same manual approval path succeed when
        # both keys are intact, not just when a test injects a proof-shaped value.
        monkeypatch.setattr(hub, "pending_devices", lambda: [description])
        assert hub.approve_device(b.device_id)["status"] == "approved"
        assert engine.context().member(b.device_id).encryption_public == b.identity.encryption_public
        assert sent


def test_join_code_cannot_rebind_a_members_encryption_key(workers):
    from app.core import pairing as pairing_core
    from app.network import enrollment
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    attacker_encryption = crypto.generate_encryption_key()
    with a.active():
        grant = pairing_core.create_grant(endpoint_url="http://10.0.0.5:8000", purpose="network:join")
        code = grant["code"] if isinstance(grant, dict) else grant[1]
        device = {
            "device_id": b.device_id, "signing_public": crypto.b64u_encode(b.identity.signing_public),
            "encryption_public": crypto.b64u_encode(crypto.encryption_public_key(attacker_encryption)),
            "name": "Not B", "platform": "linux", "kind": "worker",
        }
        with pytest.raises(enrollment.EnrollmentError, match="prove"):
            enrollment.receive_join(code=code, device=device, inviter_fingerprint=None)
        # The code was not consumed by the refused attempt.
        assert engine.context().member(b.device_id).encryption_public == b.identity.encryption_public
        good = membership.sign_device_description(dict(device, encryption_public=crypto.b64u_encode(
            b.identity.encryption_public)), b.identity.signing_private)
        assert enrollment.receive_join(code=code, device=good, inviter_fingerprint=None)["status"] == "approved"


# ---------------------------------------------------------------------------
# NET-03: a parked op that cannot be opened yet is kept, not skipped
# ---------------------------------------------------------------------------

def test_out_of_order_op_under_an_unknown_key_waits_instead_of_being_dropped(workers):
    a, b, c, x = workers("a"), workers("b"), workers("c"), workers("x")
    with a.active():
        engine.create_network("Home")
    for w in (b, c, x):
        join(a, w)
    converge([a, b, c, x])
    set_owner_pref(b, "before", 1)
    converge([a, b, c])
    # A rotates the key (revokes X); B learns it and writes under key v2 twice.
    with a.active():
        engine.revoke_device(x.device_id)
    exchange(a, b)
    set_owner_pref(b, "under_v2_first", 1)
    set_owner_pref(b, "under_v2_second", 2)
    ops = _pref_ops(b)
    first, second = ops[-2], ops[-1]
    assert second["key_version"] == 2 and second["origin_seq"] == first["origin_seq"] + 1
    with c.active():
        # C has not heard about the rotation: neither op can be opened yet, and
        # neither is skipped; the stream simply waits.
        for op in (second, first):
            result = engine.apply_batch([op])
            assert result.retry == 1 and result.rejected == 0 and "unknown key version 2" in result.reasons
        assert store.local_watermarks()[b.device_id]["1"] == first["origin_seq"] - 1
    # Once A's rotation reaches C, both ops apply on the next pass.
    exchange(a, c)
    exchange(b, c)
    assert owner_pref(c, "under_v2_first") == 1 and owner_pref(c, "under_v2_second") == 2
    with c.active():
        assert not store.pending_holes()


def test_buffered_op_that_fails_transiently_on_drain_is_kept(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    set_owner_pref(a, "head", 1)
    set_owner_pref(a, "tail", 2)
    head, tail = _pref_ops(a)[-2:]
    real = engine.materialize

    def locked_for_tail(cursor, coll, record_id, record, **kwargs):
        if record_id == tail["record_id"]:
            raise engine.TransientApplyError("Credential storage is locked; cannot store user_preferences")
        return real(cursor, coll, record_id, record, **kwargs)

    with b.active():
        assert engine.apply_batch([tail]).pending == 1  # a hole: the head has not arrived
        monkeypatch.setattr(engine, "materialize", locked_for_tail)
        result = engine.apply_batch([head])
        assert result.applied == 1 and result.retry == 1 and result.rejected == 0
        # The tail stays buffered and the watermark stops at the head.
        assert store.local_watermarks()[a.device_id]["1"] == head["origin_seq"]
        assert store.pending_holes() and store.pending_holes()[0]["first_pending"] == tail["origin_seq"]
        monkeypatch.setattr(engine, "materialize", real)
        assert engine.apply_batch([tail]).applied == 1
        assert not store.pending_holes()
    assert owner_pref(b, "tail") == 2


# ---------------------------------------------------------------------------
# NET-04: a record storage refuses is a conflict, not a stalled stream
# ---------------------------------------------------------------------------

def test_cross_origin_tag_dependency_retries_without_advancing_stream(workers):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    join(a, c)
    with a.active():
        database.create_session(session_id="tagged-session", profile_id="default", title="Tagged")
        engine.scan_all()
    converge([a, b, c])
    with b.active():
        database.create_tag("late-tag", "Created on B")
        engine.scan_all()
    # A has B's tag, but C has not received B's independent stream yet.
    exchange(b, a)
    with a.active():
        assert database.add_session_tag("tagged-session", "late-tag")
        engine.scan_all()
    with c.active():
        marks = store.local_watermarks()
    with a.active():
        ops = [op for op in store.ops_since(marks) if op["origin_device_id"] == a.device_id]
    assert any(op["collection"] == "session_tags" for op in ops)
    with c.active():
        result = engine.apply_batch(ops)
        assert result.retry == 1 and result.rejected == 0
        assert store.local_watermarks()[a.device_id] == marks[a.device_id]
        assert not store.list_conflicts()
        assert database.get_session_tags("tagged-session") == []
    exchange(b, c)
    with c.active():
        result = engine.apply_batch(ops)
        assert result.applied == 1 and result.retry == 0 and result.rejected == 0
        assert [tag["id"] for tag in database.get_session_tags("tagged-session")] == ["late-tag"]
        assert store.local_watermarks()[a.device_id]["1"] == ops[-1]["origin_seq"]


def test_storage_refusal_keeps_the_stream_moving_and_records_a_conflict(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    set_owner_pref(a, "poison", "x")
    set_owner_pref(a, "healthy", "y")
    poison, healthy = _pref_ops(a)[-2:]
    real = engine.materialize

    def refusing(cursor, coll, record_id, record, **kwargs):
        if record_id == poison["record_id"]:
            raise sqlite3.IntegrityError("NOT NULL constraint failed")
        return real(cursor, coll, record_id, record, **kwargs)

    monkeypatch.setattr(engine, "materialize", refusing)
    with b.active():
        result = engine.apply_batch([poison, healthy])
        assert result.rejected == 1 and result.applied == 1
        assert any("storage refused" in r for r in result.reasons)
        assert store.local_watermarks()[a.device_id]["1"] == healthy["origin_seq"]
        conflicts = store.list_conflicts()
        assert any(c["record_id"] == poison["record_id"] for c in conflicts)
    assert owner_pref(b, "healthy") == "y"


def test_key_record_without_a_generation_is_rejected_not_fatal(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    with a.active():
        ctx = engine.context()
        record = membership.issue_key_record(
            network_id=ctx.network_id, issuer_device_id=ctx.device_id, issuer_signing_private=ctx.identity.signing_private,
            key_version=7, generation=None, new_key=crypto.generate_network_key(),
            recipients={m.device_id: m.encryption_public for m in ctx.active_members()})
        with pytest.raises(membership.MembershipError, match="generation"):
            membership.verify_key_record(record, network_id=ctx.network_id, members=ctx.members)


# ---------------------------------------------------------------------------
# NET-05: restore detection needs proof; own history is re-imported
# ---------------------------------------------------------------------------

def test_own_signed_op_past_the_outbox_moves_to_a_new_incarnation(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    set_owner_pref(a, "before_backup", 1)
    converge([a, b])
    with a.active():
        ctx = engine.context()
        with store.get_db() as conn:
            held = store.own_watermark(conn.cursor(), ctx.device_id, ctx.identity.incarnation)
        # An op A signed that A no longer holds (its database was restored from before it).
        lost = envelope_mod.build(
            network_id=ctx.network_id, collection="user_preferences", record_id="admin|@owner|lost", op="upsert",
            clock=999, origin_device_id=ctx.device_id, origin_incarnation=ctx.identity.incarnation,
            origin_seq=held + 3, schema_version=1, key_version=ctx.key_version, network_key=ctx.network_key,
            signing_private=ctx.identity.signing_private, record={"user_type": "admin", "user_id": "@owner",
                                                                  "key": "lost", "value": 1})
        result = engine.apply_batch([lost.to_dict()])
        assert result.own_ahead == held + 3
        assert engine.context().identity.incarnation == 2


async def test_rollback_claim_without_proof_is_ignored(workers):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    with a.active():
        ctx = engine.context()
        link = transport.PeerLink(b.device_id, ["http://b.local:8000"])
        link.base_url = "http://b.local:8000"
        claims = {a.device_id: {"1": 10 ** 6}}

        async def no_proof(client, method, path, **kwargs):
            raise transport.PeerError("HTTP 404")
        link._request = no_proof
        assert await link._check_rollback(None, ctx, claims) is False
        assert engine.context().identity.incarnation == 1

        forged = envelope_mod.build(
            network_id=ctx.network_id, collection="user_preferences", record_id="admin|@owner|forged", op="upsert",
            clock=5, origin_device_id=a.device_id, origin_incarnation=1, origin_seq=10 ** 6, schema_version=1,
            key_version=ctx.key_version, network_key=ctx.network_key, signing_private=b.identity.signing_private,
            record={"key": "forged"})

        async def forged_proof(client, method, path, **kwargs):
            return {"op": forged.to_dict()}
        link._request = forged_proof
        assert await link._check_rollback(None, ctx, claims) is False
        assert engine.context().identity.incarnation == 1

        with store.get_db() as conn:
            held = store.own_watermark(conn.cursor(), ctx.device_id, 1)
        for changes in ({"origin_seq": held}, {"origin_incarnation": 2},
                        {"network_id": "another-network"}, {"origin_device_id": b.device_id}):
            fields = dict(network_id=ctx.network_id, collection="user_preferences",
                          record_id="admin|@owner|lost", op="upsert", clock=5,
                          origin_device_id=ctx.device_id, origin_incarnation=1, origin_seq=held + 1,
                          schema_version=1, key_version=ctx.key_version, network_key=ctx.network_key,
                          signing_private=ctx.identity.signing_private, record={"key": "lost"})
            fields.update(changes)
            proof = envelope_mod.build(**fields)

            async def mismatched_proof(client, method, path, **kwargs):
                return {"op": proof.to_dict()}
            link._request = mismatched_proof
            assert await link._check_rollback(None, ctx, claims) is False
            assert engine.context().identity.incarnation == 1


def test_outbox_is_held_until_streams_were_compared(workers):
    a = workers("a")
    with a.active():
        engine.create_network("Home")
        engine.hold_outbox(True)
    set_owner_pref(a, "while_held", 1)
    assert not _pref_ops(a)
    with a.active():
        engine.hold_outbox(False)
    set_owner_pref(a, "released", 2)
    keys = {o["record_id"] for o in _pref_ops(a)}
    assert "admin|@owner|while_held" in keys and "admin|@owner|released" in keys


# ---------------------------------------------------------------------------
# NET-13: buffered out-of-order ops are bounded
# ---------------------------------------------------------------------------

def test_pending_buffer_is_bounded(workers, monkeypatch):
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    with a.active():
        ctx = engine.context()
        with store.get_db() as conn:
            held = store.own_watermark(conn.cursor(), ctx.device_id, ctx.identity.incarnation)

        def signed(seq, key):
            return envelope_mod.build(
                network_id=ctx.network_id, collection="user_preferences", record_id=f"admin|@owner|{key}",
                op="upsert", clock=50 + seq, origin_device_id=ctx.device_id, origin_incarnation=1, origin_seq=seq,
                schema_version=1, key_version=ctx.key_version, network_key=ctx.network_key,
                signing_private=ctx.identity.signing_private,
                record={"user_type": "admin", "user_id": "@owner", "key": key, "value": seq}).to_dict()
        far = signed(held + engine.PENDING_WINDOW + 5, "far")
        holes = [signed(held + 2 + i, f"hole{i}") for i in range(3)]
    monkeypatch.setattr(engine, "PENDING_CAP_PER_STREAM", 2)
    with b.active():
        result = engine.apply_batch([far])
        assert result.rejected == 1 and "too far" in result.reasons[0]
        result = engine.apply_batch(holes)
        assert result.pending == 2 and result.rejected == 1 and "too many buffered" in result.reasons[-1]


# ---------------------------------------------------------------------------
# NET-14: a certificate issued past the revocation point is withdrawn
# ---------------------------------------------------------------------------

def test_certificate_issued_after_revocation_point_is_withdrawn_on_revocation(workers):
    a, b, c, x = workers("a"), workers("b"), workers("c"), workers("x")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    join(a, c)
    converge([a, b, c])
    # B admits X; only C hears about it before A removes B.
    join(b, x)
    exchange(b, c)
    with c.active():
        assert engine.context().member(x.device_id).status == "active"
    with a.active():
        engine.revoke_device(b.device_id, reason="gone")
        assert engine.context().member(x.device_id) is None
    exchange(a, c)
    with c.active():
        assert engine.context().member(b.device_id).status == "revoked"
        assert engine.context().member(x.device_id).status == "revoked"


# ---------------------------------------------------------------------------
# NET-17: challenges are per member and bounded per member
# ---------------------------------------------------------------------------

def test_challenges_are_issued_per_member_and_bounded(workers, monkeypatch):
    from fastapi import HTTPException
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    join(a, c)
    with a.active():
        with pytest.raises(HTTPException) as refused:
            tokens.new_challenge("dev_" + "0" * 24)
        assert refused.value.status_code == 403
        # Force a tied Windows clock and descending nonce order. Eviction must
        # follow issuance order, not the random challenge's lexical value.
        monkeypatch.setattr(tokens.time, "monotonic", lambda: 1000.0)
        nonces = iter(bytes([value]) * 32 for value in range(60, 40, -1))
        monkeypatch.setattr(tokens.secrets, "token_bytes", lambda size: next(nonces))
        kept = tokens.new_challenge(c.device_id)
        first = tokens.new_challenge(b.device_id)
        for _ in range(tokens.CHALLENGES_PER_DEVICE):
            tokens.new_challenge(b.device_id)
        assert first not in tokens._challenges and kept in tokens._challenges
        # A challenge issued to C cannot be spent by B even with a valid signature.
        signature = tokens.sign_challenge(b.identity.signing_private, kept, a.device_id)
        with pytest.raises(HTTPException) as wrong:
            tokens.issue_token(device_id=b.device_id, challenge=kept, signature=signature)
        assert wrong.value.status_code == 400


# ---------------------------------------------------------------------------
# NET-19: the one-time code does not travel over public plain http
# ---------------------------------------------------------------------------

async def test_public_http_invites_are_refused_before_the_code_is_sent(workers):
    from app.network import enrollment
    a = workers("a")
    with a.active():
        with pytest.raises(enrollment.EnrollmentError, match="https"):
            await enrollment.start_enrollment(endpoint_url="http://8.8.8.8:8000", code="ABCD-EFGH-JKLM")


# ---------------------------------------------------------------------------
# NET-20: leaving a network with other members is not automatic
# ---------------------------------------------------------------------------

async def test_account_conflict_waits_for_confirmation_when_other_members_exist(workers, monkeypatch):
    from app.network import hub
    a, b = workers("a"), workers("b")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    converge([a, b])
    calls = []

    async def adopt(**kwargs):
        calls.append(kwargs)
        return {"adopted": True}
    monkeypatch.setattr(hub, "adopt_account_network", adopt)
    with a.active():
        await hub._adopt_or_wait()
        assert calls == [] and hub._state.get("adoption_required") is True
        assert engine.context() is not None
        engine.revoke_device(b.device_id)
        await hub._adopt_or_wait()
        assert len(calls) == 1 and not hub._state.get("adoption_required")
    hub.reset_for_tests()


# ---------------------------------------------------------------------------
# NET-21: identical content from another device is not a conflict
# ---------------------------------------------------------------------------

def test_same_content_written_on_two_devices_is_not_a_conflict(workers):
    a, b, c = workers("a"), workers("b"), workers("c")
    with a.active():
        engine.create_network("Home")
    join(a, b)
    join(a, c)
    converge([a, b, c])
    set_owner_pref(a, "theme", "dark")
    exchange(a, c)
    set_owner_pref(b, "theme", "dark")  # B never saw A's write, same value
    exchange(b, c)
    assert owner_pref(c, "theme") == "dark"
    with c.active():
        assert not [x for x in store.list_conflicts() if x["record_id"].endswith("|theme")]


# ---------------------------------------------------------------------------
# NET-23: one unreadable row does not stop or corrupt a scan
# ---------------------------------------------------------------------------

def test_unreadable_row_is_skipped_without_publishing_a_deletion(workers, monkeypatch):
    a = workers("a")
    with a.active():
        engine.create_network("Home")
    set_owner_pref(a, "fine", 1)
    set_owner_pref(a, "locked", 2)
    real = engine.serialize_row

    def locked(coll, row):
        if coll.name == "user_preferences" and row.get("key") == "locked":
            raise engine.TransientApplyError("Credential storage is locked; cannot share user_preferences")
        return real(coll, row)
    monkeypatch.setattr(engine, "serialize_row", locked)
    set_owner_pref(a, "fine", 3)
    ops = _pref_ops(a)
    assert any(o["record_id"] == "admin|@owner|fine" and o["op"] == "upsert" for o in ops[-1:])
    assert not any(o["record_id"] == "admin|@owner|locked" and o["op"] == "delete" for o in ops)


async def test_old_network_proof_is_rejected_at_recovery_boundary(workers, monkeypatch):
    a = workers("a")
    with a.active():
        old = engine.create_network("Old")
        link = transport.PeerLink("peer", ["http://peer.local:8000"])
        proof = envelope_mod.build(
            network_id=old.network_id, collection="user_preferences",
            record_id="admin|@owner|lost", op="upsert", clock=999,
            origin_device_id=old.device_id, origin_incarnation=old.identity.incarnation,
            origin_seq=1000000, schema_version=1, key_version=old.key_version,
            network_key=old.network_key, signing_private=old.identity.signing_private,
            record={"key": "lost"})
        async def answer(*args, **kwargs):
            return {"op": proof.to_dict()}
        monkeypatch.setattr(link, "_request", answer)
        recover = engine.restored_from_backup
        def switch_after_validation(*args, **kwargs):
            engine.leave_network()
            engine.create_network("New")
            return recover(*args, **kwargs)
        monkeypatch.setattr(engine, "restored_from_backup", switch_after_validation)
        assert await link._check_rollback(None, old, {old.device_id: {"1": 1000000}}) is False
        assert engine.context().network_id != old.network_id
        assert engine.context().identity.incarnation == old.identity.incarnation
