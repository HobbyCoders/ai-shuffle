"""What the network's trust primitives refuse.

Pure unit tests for the verification side of ``app.network.membership``,
``app.network.crypto``, ``app.network.recovery`` and ``app.network.tokens``:
malformed, forged, cross-network, self-issued and revoked inputs. Each one is
a record or credential another device (or the hub) hands this worker, so each
refusal is a trust boundary.
"""
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.network import crypto, engine, membership, recovery, tokens

NET = "net_test"


def _device():
    signing = crypto.generate_signing_key()
    enc = crypto.generate_encryption_key()
    public = crypto.signing_public_key(signing)
    return SimpleNamespace(signing=signing, enc=enc, public=public,
                           enc_public=crypto.encryption_public_key(enc),
                           id=membership.device_id_for(public))


def _member(device, status=membership.STATUS_ACTIVE, generation=1):
    return membership.Member(device_id=device.id, signing_public=device.public,
                             encryption_public=device.enc_public, name="d", kind=membership.KIND_WORKER,
                             generation=generation, status=status)


@pytest.fixture
def trio():
    founder, second, third = _device(), _device(), _device()
    members = {founder.id: _member(founder), second.id: _member(second)}
    return founder, second, third, members


def _cert(issuer, subject, **overrides):
    record = membership.issue_certificate(
        network_id=NET, issuer_device_id=issuer.id, issuer_signing_private=issuer.signing,
        device_id=subject.id, signing_public=subject.public, encryption_public=subject.enc_public,
        name="worker", kind=membership.KIND_WORKER, generation=2)
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# Device descriptions
# ---------------------------------------------------------------------------

def test_device_description_proof_binds_keys_to_the_device():
    device, relay = _device(), _device()
    description = {"device_id": device.id, "signing_public": crypto.b64u_encode(device.public),
                   "encryption_public": crypto.b64u_encode(device.enc_public), "kind": "worker"}
    signed = membership.sign_device_description(description, device.signing)
    assert membership.verify_device_description(signed)
    # A relay substituting its own encryption key breaks the proof.
    swapped = dict(signed, encryption_public=crypto.b64u_encode(relay.enc_public))
    assert not membership.verify_device_description(swapped)
    # A proof made by another key, a missing proof, a wrong id, junk: all refused.
    assert not membership.verify_device_description(membership.sign_device_description(description, relay.signing))
    assert not membership.verify_device_description(dict(signed, proof=""))
    assert not membership.verify_device_description(dict(signed, proof=None))
    assert not membership.verify_device_description(dict(signed, device_id=relay.id))
    assert not membership.verify_device_description(dict(signed, signing_public="!!!"))
    assert not membership.verify_device_description({})


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("mutate", [
    lambda r: r.update(v=2),
    lambda r: r.update(type="revocation"),
    lambda r: r.update(signing_public=crypto.b64u_encode(b"x" * 31)),
    lambda r: r.update(encryption_public="not base64!"),
    lambda r: r.update(device_id="dev_000000000000000000000000"),
    lambda r: r.update(kind="admin"),
    lambda r: r.update(generation=-1),
    lambda r: r.update(generation=True),
    lambda r: r.update(generation="2"),
    lambda r: r.update(name="   "),
    lambda r: r.update(name="x" * 201),
    lambda r: r.update(name=None),
    lambda r: r.pop("kind"),
])
def test_certificate_shape_errors(trio, mutate):
    founder, _, third, _ = trio
    record = _cert(founder, third)
    mutate(record)
    with pytest.raises(membership.MembershipError):
        membership.certificate_fields(record)


def test_issue_certificate_refuses_bad_kind_and_mismatched_id(trio):
    founder, _, third, _ = trio
    with pytest.raises(membership.MembershipError):
        membership.issue_certificate(network_id=NET, issuer_device_id=founder.id,
                                     issuer_signing_private=founder.signing, device_id=third.id,
                                     signing_public=third.public, encryption_public=third.enc_public,
                                     name="n", kind="root", generation=1)
    with pytest.raises(membership.MembershipError):
        membership.issue_certificate(network_id=NET, issuer_device_id=founder.id,
                                     issuer_signing_private=founder.signing, device_id=founder.id,
                                     signing_public=third.public, encryption_public=third.enc_public,
                                     name="n", kind="worker", generation=1)


def test_certificate_from_active_member_verifies(trio):
    founder, _, third, members = trio
    member = membership.verify_certificate(_cert(founder, third), network_id=NET, members=members,
                                           founder_device_id=founder.id)
    assert member.device_id == third.id and member.name == "worker"


def test_certificate_for_another_network_is_refused(trio):
    founder, _, third, members = trio
    record = membership.issue_certificate(
        network_id="net_other", issuer_device_id=founder.id, issuer_signing_private=founder.signing,
        device_id=third.id, signing_public=third.public, encryption_public=third.enc_public,
        name="w", kind="worker", generation=2)
    with pytest.raises(membership.MembershipError, match="another network"):
        membership.verify_certificate(record, network_id=NET, members=members, founder_device_id=founder.id)


def test_certificate_from_revoked_or_unknown_issuer_is_refused(trio):
    founder, second, third, members = trio
    members[second.id] = _member(second, status=membership.STATUS_REVOKED)
    with pytest.raises(membership.MembershipError, match="not an active member"):
        membership.verify_certificate(_cert(second, third), network_id=NET, members=members,
                                      founder_device_id=founder.id)
    stranger = _device()
    with pytest.raises(membership.MembershipError, match="not an active member"):
        membership.verify_certificate(_cert(stranger, third), network_id=NET, members=members,
                                      founder_device_id=founder.id)


def test_certificate_claiming_a_member_issuer_but_signed_elsewhere_is_refused(trio):
    founder, second, third, members = trio
    forged = _cert(third, third, issued_by=second.id)
    forged = membership._sign({k: v for k, v in forged.items() if k != "signature"}, third.signing)
    with pytest.raises(membership.MembershipError, match="signature is invalid"):
        membership.verify_certificate(forged, network_id=NET, members=members, founder_device_id=founder.id)


def test_tampered_certificate_signature_fails(trio):
    founder, _, third, members = trio
    record = _cert(founder, third)
    record["name"] = "renamed-after-signing"
    with pytest.raises(membership.MembershipError, match="signature is invalid"):
        membership.verify_certificate(record, network_id=NET, members=members, founder_device_id=founder.id)


def test_only_the_founder_may_self_certify(trio):
    founder, _, third, members = trio
    self_signed = _cert(third, third)
    with pytest.raises(membership.MembershipError, match="Only the founder"):
        membership.verify_certificate(self_signed, network_id=NET, members=members,
                                      founder_device_id=founder.id)


def test_founder_cannot_reissue_a_different_root(trio):
    founder, _, _, members = trio
    members[founder.id] = _member(founder, status=membership.STATUS_REVOKED, generation=1)
    reissued = _cert(founder, founder)  # generation 2 differs from the established root
    with pytest.raises(membership.MembershipError, match="already established"):
        membership.verify_certificate(reissued, network_id=NET, members=members, founder_device_id=founder.id)


def test_forged_self_signed_founder_certificate_fails(trio):
    founder, _, _, _ = trio
    record = _cert(founder, founder)
    record["signature"] = crypto.b64u_encode(bytes(64))
    with pytest.raises(membership.MembershipError, match="Self-signed certificate is invalid"):
        membership.verify_certificate(record, network_id=NET, members={}, founder_device_id=None)


def test_recovery_certificate_needs_valid_signature_and_proof(trio):
    founder, _, third, members = trio
    key = crypto.generate_network_key()
    record = membership.issue_recovery_certificate(
        network_id=NET, network_key=key, signing_private=third.signing,
        encryption_public=third.enc_public, name="rec", kind="worker", generation=3)
    assert membership.verify_certificate(record, network_id=NET, members=members, founder_device_id=founder.id,
                                         network_key=key).device_id == third.id
    bad_sig = dict(record, signature=crypto.b64u_encode(bytes(64)))
    with pytest.raises(membership.MembershipError, match="signature is invalid"):
        membership.verify_certificate(bad_sig, network_id=NET, members=members, founder_device_id=founder.id,
                                      network_key=key)
    assert not membership.verify_recovery_proof(record, crypto.generate_network_key())
    assert not membership.verify_recovery_proof(dict(record, proof=None), key)


# ---------------------------------------------------------------------------
# Revocations
# ---------------------------------------------------------------------------

def _revocation(issuer, subject_id, **overrides):
    record = membership.issue_revocation(network_id=NET, issuer_device_id=issuer.id,
                                         issuer_signing_private=issuer.signing, device_id=subject_id,
                                         generation=3, last_seq_accepted=10, last_incarnation_accepted=1)
    record.update(overrides)
    return record


def test_valid_revocation_details(trio):
    founder, second, _, members = trio
    details = membership.verify_revocation(_revocation(founder, second.id), network_id=NET, members=members)
    assert details == {"device_id": second.id, "generation": 3, "last_seq_accepted": 10,
                       "last_incarnation_accepted": 1}


@pytest.mark.parametrize("overrides,match", [
    ({"v": 9}, "Not a revocation"),
    ({"type": "certificate"}, "Not a revocation"),
    ({"network_id": "net_other"}, "another network"),
    ({"generation": -1}, "counters"),
    ({"last_seq_accepted": True}, "counters"),
    ({"last_seq_accepted": "10"}, "counters"),
    ({"last_incarnation_accepted": -2}, "counters"),
    ({"generation": None}, "counters"),
])
def test_revocation_field_errors(trio, overrides, match):
    founder, second, _, members = trio
    with pytest.raises(membership.MembershipError, match=match):
        membership.verify_revocation(_revocation(founder, second.id, **overrides), network_id=NET, members=members)


def test_malformed_revocation(trio):
    founder, second, _, members = trio
    record = _revocation(founder, second.id)
    del record["last_seq_accepted"]
    with pytest.raises(membership.MembershipError, match="Malformed"):
        membership.verify_revocation(record, network_id=NET, members=members)


def test_revocation_by_inactive_issuer_self_or_forged_is_refused(trio):
    founder, second, third, members = trio
    with pytest.raises(membership.MembershipError, match="not an active member"):
        membership.verify_revocation(_revocation(third, second.id), network_id=NET, members=members)
    with pytest.raises(membership.MembershipError, match="cannot revoke itself"):
        membership.verify_revocation(_revocation(second, second.id), network_id=NET, members=members)
    forged = _revocation(founder, second.id, last_seq_accepted=999)  # raised after signing
    with pytest.raises(membership.MembershipError, match="signature is invalid"):
        membership.verify_revocation(forged, network_id=NET, members=members)


def test_apply_revocation_of_unknown_device_is_refused(trio):
    founder, _, third, members = trio
    with pytest.raises(membership.MembershipError, match="unknown device"):
        membership.apply_record(_revocation(founder, third.id), network_id=NET, members=dict(members),
                                founder_device_id=founder.id)
    with pytest.raises(membership.MembershipError, match="Unknown membership record type"):
        membership.apply_record({"type": "grant"}, network_id=NET, members=dict(members),
                                founder_device_id=founder.id)


# ---------------------------------------------------------------------------
# Key records
# ---------------------------------------------------------------------------

def _key_record(issuer, recipients, **overrides):
    record = membership.issue_key_record(network_id=NET, issuer_device_id=issuer.id,
                                         issuer_signing_private=issuer.signing, key_version=2, generation=3,
                                         new_key=crypto.generate_network_key(), recipients=recipients)
    record.update(overrides)
    return record


@pytest.mark.parametrize("overrides,match", [
    ({"type": "certificate"}, "Not a key record"),
    ({"network_id": "net_other"}, "another network"),
    ({"key_version": 0}, "key version"),
    ({"key_version": True}, "key version"),
    ({"generation": -1}, "generation"),
    ({"wraps": []}, "Bad wraps"),
])
def test_key_record_field_errors(trio, overrides, match):
    founder, second, _, members = trio
    record = _key_record(founder, {second.id: second.enc_public}, **overrides)
    with pytest.raises(membership.MembershipError, match=match):
        membership.verify_key_record(record, network_id=NET, members=members)


def test_key_record_issuer_and_signature(trio):
    founder, second, third, members = trio
    recipients = {second.id: second.enc_public}
    with pytest.raises(membership.MembershipError, match="not an active member"):
        membership.verify_key_record(_key_record(third, recipients), network_id=NET, members=members)
    record = _key_record(founder, recipients)
    record["wraps"][third.id] = crypto.wrap_key(third.enc_public, crypto.generate_network_key())
    with pytest.raises(membership.MembershipError, match="signature is invalid"):
        membership.verify_key_record(record, network_id=NET, members=members)
    missing = _key_record(founder, recipients)
    del missing["issued_by"]
    with pytest.raises(membership.MembershipError, match="Malformed"):
        membership.verify_key_record(missing, network_id=NET, members=members)


# ---------------------------------------------------------------------------
# Key wraps: every malformed wrap is a CryptoError (regression)
# ---------------------------------------------------------------------------

def _wrap_for(device):
    return crypto.wrap_key(device.enc_public, crypto.generate_network_key())


@pytest.mark.parametrize("mutate", [
    lambda w: w.update(v=99),
    lambda w: w.update(alg="RSA"),
    lambda w: w.pop("ct"),
    lambda w: w.update(epk=crypto.b64u_encode(b"short")),
    lambda w: w.update(ct=crypto.b64u_encode(b"\x00" * 48)),
    # Regression: these two used to escape as a plain ValueError from the
    # cryptography library, which the engine's key-record path (catching
    # CryptoError only) let through as an unexpected failure.
    lambda w: w.update(epk=crypto.b64u_encode(bytes(32))),       # low-order X25519 point
    lambda w: w.update(nonce=crypto.b64u_encode(b"abc")),         # nonce too short for AES-GCM
])
def test_malformed_key_wrap_is_a_crypto_error(mutate):
    device = _device()
    wrapped = _wrap_for(device)
    mutate(wrapped)
    with pytest.raises(crypto.CryptoError):
        crypto.unwrap_key(device.enc, wrapped)


def test_wrap_for_another_device_does_not_open():
    device, other = _device(), _device()
    with pytest.raises(crypto.CryptoError):
        crypto.unwrap_key(other.enc, _wrap_for(device))
    with pytest.raises(crypto.CryptoError):
        crypto.unwrap_key(b"short", _wrap_for(device))


def test_key_record_with_poisoned_wrap_is_a_crypto_error(trio):
    """``engine._apply_membership`` turns CryptoError into ApplyError (a rejected
    op); any other exception would stall the origin's stream."""
    founder, second, _, _ = trio
    record = _key_record(founder, {second.id: second.enc_public})
    record["wraps"][second.id]["epk"] = crypto.b64u_encode(bytes(32))
    with pytest.raises(crypto.CryptoError):
        membership.unwrap_from_key_record(record, second.id, second.enc)
    assert membership.unwrap_from_key_record(record, "dev_absent", second.enc) is None


# ---------------------------------------------------------------------------
# Recovery wraps
# ---------------------------------------------------------------------------

def _recovery_wrap(network_id=NET):
    raw = crypto.generate_recovery_key()
    key = crypto.generate_network_key()
    wrap = {"v": 1, "network_id": network_id,
            "wrapped": crypto.wrap_key_with_recovery(raw, network_id, key),
            "check": crypto.recovery_key_check(raw, network_id)}
    return crypto.format_recovery_key(raw), wrap, key


def test_recovery_unwrap_round_trip():
    text, wrap, key = _recovery_wrap()
    assert recovery.unwrap(text, wrap) == key
    assert recovery.unwrap(text.lower().replace("-", " "), wrap) == key


def test_recovery_unwrap_refuses_stale_and_non_dict_wraps():
    text, wrap, _ = _recovery_wrap()
    with pytest.raises(recovery.RecoveryError, match="stale"):
        recovery.unwrap(text, dict(wrap, stale=True))
    with pytest.raises(recovery.RecoveryError):
        recovery.unwrap(text, None)


def test_recovery_unwrap_refuses_a_key_for_another_network():
    text, _, _ = _recovery_wrap()
    _, other_wrap, _ = _recovery_wrap("net_other")
    with pytest.raises(recovery.RecoveryError, match="does not belong"):
        recovery.unwrap(text, other_wrap)
    # Without the public check value the AES-GCM tag still refuses it.
    with pytest.raises(recovery.RecoveryError, match="does not open"):
        recovery.unwrap(text, dict(other_wrap, check=None))


@pytest.mark.parametrize("mutate", [
    lambda w: w.pop("wrapped"),
    lambda w: w.update(wrapped={"v": 1}),
    lambda w: w.update(wrapped=dict(w["wrapped"], alg="none")),
    lambda w: w.update(wrapped=dict(w["wrapped"], nonce=crypto.b64u_encode(b""))),   # regression
    lambda w: w.update(network_id="net_moved", check=None),
])
def test_recovery_unwrap_malformed_wrap_is_a_recovery_error(mutate):
    text, wrap, _ = _recovery_wrap()
    mutate(wrap)
    with pytest.raises(recovery.RecoveryError):
        recovery.unwrap(text, wrap)


@pytest.mark.parametrize("text", ["", "B1-AAAAA", "A1-" + "0" * 25, "A1-" + "U" * 26, "A1-" + "Z" * 26])
def test_recovery_key_text_errors(text):
    with pytest.raises(crypto.CryptoError):
        crypto.parse_recovery_key(text)


# ---------------------------------------------------------------------------
# Device tokens
# ---------------------------------------------------------------------------

@pytest.fixture
def network(monkeypatch):
    """A fake network context: ``me`` is this worker, ``peer`` an active member."""
    tokens.reset_for_tests()
    me, peer, revoked = _device(), _device(), _device()
    members = {me.id: _member(me), peer.id: _member(peer),
               revoked.id: _member(revoked, status=membership.STATUS_REVOKED)}
    ctx = SimpleNamespace(device_id=me.id, network_id=NET, generation=4, member=members.get)
    monkeypatch.setattr(engine, "context", lambda: ctx)
    yield SimpleNamespace(ctx=ctx, me=me, peer=peer, revoked=revoked, members=members)
    tokens.reset_for_tests()


def _bearer(token):
    headers = [(b"authorization", f"Bearer {token}".encode())] if token else []
    return Request({"type": "http", "method": "GET", "path": "/", "headers": headers, "query_string": b""})


def _token_for(net, device):
    challenge = tokens.new_challenge(device.id)
    signature = tokens.sign_challenge(device.signing, challenge, net.me.id)
    return tokens.issue_token(device_id=device.id, challenge=challenge, signature=signature)


def test_challenge_token_round_trip(network):
    issued = _token_for(network, network.peer)
    assert issued["device_id"] == network.me.id and issued["generation"] == 4
    request = _bearer(issued["token"])
    assert tokens.require_device(request) == network.peer.id
    assert request.state.peer_device_id == network.peer.id


def test_no_network_means_404_everywhere(network, monkeypatch):
    monkeypatch.setattr(engine, "context", lambda: None)
    for call in (lambda: tokens.new_challenge(network.peer.id),
                 lambda: tokens.issue_token(device_id=network.peer.id, challenge="c", signature="s")):
        with pytest.raises(HTTPException) as info:
            call()
        assert info.value.status_code == 404


def test_revoked_and_unknown_devices_get_no_challenge_or_token(network):
    for device_id in (network.revoked.id, "dev_unknown"):
        with pytest.raises(HTTPException) as info:
            tokens.new_challenge(device_id)
        assert info.value.status_code == 403
        with pytest.raises(HTTPException) as info:
            tokens.issue_token(device_id=device_id, challenge="c", signature="s")
        assert info.value.status_code == 403


def test_challenge_is_bound_to_its_device_and_single_use(network):
    other = _device()
    network.members[other.id] = _member(other)
    challenge = tokens.new_challenge(network.peer.id)
    stolen = tokens.sign_challenge(other.signing, challenge, network.me.id)
    with pytest.raises(HTTPException) as info:
        tokens.issue_token(device_id=other.id, challenge=challenge, signature=stolen)
    assert info.value.status_code == 400
    # The attempt consumed it: the rightful device cannot reuse it either.
    good = tokens.sign_challenge(network.peer.signing, challenge, network.me.id)
    with pytest.raises(HTTPException) as info:
        tokens.issue_token(device_id=network.peer.id, challenge=challenge, signature=good)
    assert info.value.status_code == 400


@pytest.mark.parametrize("signature", ["!!not-base64!!", crypto.b64u_encode(bytes(64)), ""])
def test_bad_challenge_signature_is_403(network, signature):
    challenge = tokens.new_challenge(network.peer.id)
    with pytest.raises(HTTPException) as info:
        tokens.issue_token(device_id=network.peer.id, challenge=challenge, signature=signature)
    assert info.value.status_code == 403


def test_signature_for_another_audience_is_refused(network):
    challenge = tokens.new_challenge(network.peer.id)
    signature = tokens.sign_challenge(network.peer.signing, challenge, "dev_some_other_worker")
    with pytest.raises(HTTPException) as info:
        tokens.issue_token(device_id=network.peer.id, challenge=challenge, signature=signature)
    assert info.value.status_code == 403


def test_expired_challenges_are_pruned_and_refused(network, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(tokens.time, "monotonic", lambda: now[0])
    old = tokens.new_challenge(network.peer.id)
    now[0] += tokens.CHALLENGE_TTL_SECONDS + 1
    tokens.new_challenge(network.peer.id)  # prunes ``old``
    assert old not in tokens._challenges
    signature = tokens.sign_challenge(network.peer.signing, old, network.me.id)
    with pytest.raises(HTTPException) as info:
        tokens.issue_token(device_id=network.peer.id, challenge=old, signature=signature)
    assert info.value.status_code == 400


def test_require_device_refusals(network, monkeypatch):
    issued = _token_for(network, network.peer)["token"]
    for token in (None, "not-a-token", "dvt_nope", issued[:-1] + ("0" if issued[-1] != "0" else "1")):
        with pytest.raises(HTTPException) as info:
            tokens.require_device(_bearer(token))
        assert info.value.status_code == 401
    # Revoked after the token was issued: the membership check runs every time.
    network.members[network.peer.id] = _member(network.peer, status=membership.STATUS_REVOKED)
    with pytest.raises(HTTPException) as info:
        tokens.require_device(_bearer(issued))
    assert info.value.status_code == 403


def test_membership_generation_change_invalidates_tokens(network):
    issued = _token_for(network, network.peer)["token"]
    network.ctx.generation = 5
    assert tokens.verify_token(issued) is None


def test_expired_token_is_refused(network, monkeypatch):
    issued = _token_for(network, network.peer)["token"]
    later = time.time() + tokens.TOKEN_TTL_SECONDS + 5
    monkeypatch.setattr(tokens.time, "time", lambda: later)
    assert tokens.verify_token(issued) is None
