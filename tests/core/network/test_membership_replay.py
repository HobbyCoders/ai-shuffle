"""Signed membership replay must not roll a device back after re-admission."""
import itertools

import pytest

from app.db import network as store
from app.db.base import get_db
from app.network import crypto, engine, membership


def _admit(worker, *, name=None, encryption_public=None):
    return engine.admit_device(
        device_id=worker.device_id, signing_public=worker.identity.signing_public,
        encryption_public=encryption_public or worker.identity.encryption_public,
        name=name or worker.name,
    )


@pytest.fixture(params=[False, True], ids=["same-key", "replacement-key"])
def history(workers, request):
    founder, member, receiver = (workers(name) for name in ("founder", "member", "receiver"))
    with founder.active():
        engine.create_network("Replay test")
        original = _admit(member, name="Original")["certificate"]
        engine.revoke_device(member.device_id)
        revocation = next(r["record"] for r in store.list_membership_records()
                          if r["record"]["type"] == membership.TYPE_REVOCATION)
        replacement_key = (crypto.encryption_public_key(crypto.generate_encryption_key())
                           if request.param else member.identity.encryption_public)
        latest = _admit(member, name="Readmitted", encryption_public=replacement_key)["certificate"]
        response = _admit(receiver)
    return founder, member, receiver, original, revocation, latest, response


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
def test_signed_enrollment_reordered_history_preserves_readmitted_member(history, order):
    founder, member, receiver, original, revocation, latest, response = history
    # Reorder the target's signed history, then append a late valid g1 replay.
    records = [r for r in response["membership_records"] if r.get("device_id") != member.device_id]
    target_history = (original, revocation, latest)
    response["membership_records"] = records + [target_history[i] for i in order] + [original]
    with receiver.active():
        installed = engine.enroll_from_response(response, device_name=receiver.name)
        assert installed.member(member.device_id) == membership.certificate_fields(latest)
        assert store.get_member(member.device_id)["generation"] == latest["generation"]
        assert store.get_member(member.device_id)["encryption_public"] == latest["encryption_public"]


@pytest.mark.parametrize("record_index", [0, 1], ids=["certificate", "revocation"])
@pytest.mark.parametrize("delivery", ["batch", "snapshot"])
def test_signed_stale_membership_replay_cannot_roll_back_synced_member(history, record_index, delivery):
    founder, member, receiver, original, revocation, latest, response = history
    with receiver.active():
        engine.enroll_from_response(response, device_name=receiver.name)
        engine.finish_bootstrap()
        before = store.get_member(member.device_id)
    with founder.active():
        # A fresh, authentically signed envelope prevents stream deduplication
        # from hiding a stale inner certificate/revocation.
        with get_db() as conn:
            op = engine.record_membership(conn.cursor(), (original, revocation)[record_index])
        chunk = engine.snapshot_chunk("membership")
    with receiver.active():
        if delivery == "batch":
            # Restore the preceding stream first, as an offline delta does.
            with founder.active():
                ops = store.ops_since({})
            engine.apply_batch(ops)
            engine.apply_batch([op])
        else:
            engine.apply_snapshot_chunk(chunk)
        assert engine.context().member(member.device_id) == membership.certificate_fields(latest)
        # Replaying the legitimate latest certificate can fill enrollment's
        # added_by and update timestamps; every trust field must stay identical.
        after = store.get_member(member.device_id)
        for field in ("generation", "status", "name", "kind", "signing_public", "encryption_public",
                      "last_seq_accepted", "last_incarnation_accepted"):
            assert after[field] == before[field]
        assert engine.context(refresh=True).member(member.device_id) == membership.certificate_fields(latest)


def _apply(ctx, record, members):
    return membership.apply_record(record, network_id=ctx.network_id, members=members,
                                   founder_device_id=ctx.founder_device_id, network_key=ctx.network_key)


@pytest.mark.parametrize("field", ["name", "encryption_public", "kind"])
def test_tied_signed_certificate_cannot_replace_member_fields(history, field):
    founder, member, _, _, _, latest, _ = history
    replacement = dict(latest)
    replacement[field] = {"name": "Conflicting", "kind": membership.KIND_CONSUMER,
                          "encryption_public": crypto.b64u_encode(
                              crypto.encryption_public_key(crypto.generate_encryption_key()))}[field]
    replacement = membership._sign(replacement, founder.identity.signing_private)
    with founder.active():
        ctx = engine.context()
        members = dict(ctx.members)
        with pytest.raises(membership.MembershipError, match="generation"):
            _apply(ctx, replacement, members)
        assert members == ctx.members
        assert _apply(ctx, latest, members) == ctx.members


@pytest.mark.parametrize("field", ["generation", "last_seq_accepted", "last_incarnation_accepted"])
def test_signed_revocation_cannot_regress_or_change_tied_cutoff(history, field):
    founder, member, _, original, revocation, _, _ = history
    with founder.active():
        ctx = engine.context()
        members = dict(ctx.members)
        members[member.device_id] = membership.certificate_fields(original)
        _apply(ctx, revocation, members)
        before = dict(members)
        assert _apply(ctx, revocation, members) == before
        conflicting = dict(revocation)
        conflicting[field] += -1 if field == "generation" else 1
        conflicting = membership._sign(conflicting, founder.identity.signing_private)
        with pytest.raises(membership.MembershipError, match="generation"):
            _apply(ctx, conflicting, members)
        assert members == before


@pytest.mark.parametrize("generation", [1, 3])
def test_recovery_certificate_cannot_roll_back_readmitted_member(history, generation):
    founder, member, _, _, _, _, _ = history
    with founder.active():
        ctx = engine.context()
        certificate = membership.issue_recovery_certificate(
            network_id=ctx.network_id, network_key=ctx.network_key,
            signing_private=member.identity.signing_private,
            encryption_public=member.identity.encryption_public,
            name="Recovered with stale state", kind=membership.KIND_WORKER, generation=generation,
        )
        members = dict(ctx.members)
        with pytest.raises(membership.MembershipError, match="generation"):
            _apply(ctx, certificate, members)
        assert members == ctx.members


def test_idempotent_certificate_still_requires_valid_signature(history):
    founder, _, _, _, _, latest, _ = history
    with founder.active():
        ctx = engine.context()
        members = dict(ctx.members)
        with pytest.raises(membership.MembershipError, match="signature"):
            _apply(ctx, dict(latest, signature=crypto.b64u_encode(bytes(64))), members)
        assert members == ctx.members


def test_tied_revocation_cannot_remove_readmitted_member(history):
    founder, _, _, _, revocation, latest, _ = history
    tied = membership._sign(dict(revocation, generation=latest["generation"]), founder.identity.signing_private)
    with founder.active():
        ctx = engine.context()
        members = dict(ctx.members)
        with pytest.raises(membership.MembershipError, match="generation"):
            _apply(ctx, tied, members)
        assert members == ctx.members
