"""Membership records: certificates, revocations, key rotations, recovery.

These records live in the ``membership`` collection and are synced like any
other record (encrypted under the network key). Each one is additionally
signed by its issuer so a device can verify the chain of trust locally:

- ``certificate``  an existing member (or the founder, self-signed) admits a
                    device: its public keys, name, kind and the membership
                    generation at admission.
- ``revocation``   a member removes a device at a new generation and records
                    the last origin sequence it will still accept from it.
- ``key``          a member publishes a new network key version, wrapped to
                    every remaining member's encryption key.
- ``recovery``     a device that unlocked the network with the recovery key
                    self-issues its certificate; the proof is an AES-GCM tag
                    under the network key, which only a key holder can make.

Generation numbers only go up. Every record carries the generation it was
issued at; a certificate issued at generation g is valid while the issuer was
an active member at g.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Mapping, Optional

from app.network import crypto
from app.network.envelope import now_iso

RECORD_VERSION = 1
KIND_WORKER = "worker"
KIND_CONSUMER = "consumer"
KINDS = (KIND_WORKER, KIND_CONSUMER)
TYPE_CERTIFICATE = "certificate"
TYPE_REVOCATION = "revocation"
TYPE_KEY = "key"
TYPE_RECOVERY = "recovery"
STATUS_ACTIVE = "active"
STATUS_REVOKED = "revoked"
STATUS_PENDING = "pending"
_PROOF_INFO = b"shuffle-recovery-certificate-proof-v1"


class MembershipError(ValueError):
    """A record failed verification."""


@dataclass(frozen=True)
class Member:
    device_id: str
    signing_public: bytes
    encryption_public: bytes
    name: str
    kind: str
    generation: int
    status: str = STATUS_ACTIVE
    last_seq_accepted: Optional[int] = None  # set on revocation
    # The incarnation ``last_seq_accepted`` refers to. A revoked device may
    # not open a newer stream; ``None`` (a record from before this field)
    # means "whatever incarnation the receiver already knew at revocation".
    last_incarnation_accepted: Optional[int] = None


def device_id_for(signing_public: bytes) -> str:
    """Self-certifying device id: ``dev_`` plus 24 hex of the key digest."""
    return "dev_" + crypto.sha256_hex(signing_public)[:24]


# ---------------------------------------------------------------------------
# Device descriptions (proof of key possession)
# ---------------------------------------------------------------------------

_DESCRIPTION_PROOF_FIELDS = ("device_id", "signing_public", "encryption_public", "kind")
_DESCRIPTION_PROOF_DOMAIN = b"shuffle-device-description-v1|"


def _description_message(description: Mapping[str, Any]) -> bytes:
    body = {name: description.get(name) for name in _DESCRIPTION_PROOF_FIELDS}
    return _DESCRIPTION_PROOF_DOMAIN + crypto.canonical_json(body)


def sign_device_description(description: Mapping[str, Any], signing_private: bytes) -> dict:
    """Return the description with a ``proof`` only the signing key holder can make.

    The proof binds the encryption key to the device id, so a relay (the
    account hub, a pairing link) cannot substitute its own encryption key and
    receive the network keys in the device's name.
    """
    signed = dict(description)
    signed["proof"] = crypto.b64u_encode(crypto.sign(signing_private, _description_message(signed)))
    return signed


def verify_device_description(description: Mapping[str, Any]) -> bool:
    """True when ``proof`` was made by the signing key the description names."""
    try:
        signing_public = crypto.b64u_decode(description["signing_public"])
        if description.get("device_id") != device_id_for(signing_public):
            return False
        proof = description.get("proof")
        if not isinstance(proof, str) or not proof:
            return False
        return crypto.verify(signing_public, _description_message(description), crypto.b64u_decode(proof))
    except (KeyError, TypeError, AttributeError, crypto.CryptoError):
        return False


def _strip_signature(record: Mapping[str, Any]) -> dict:
    return {k: v for k, v in record.items() if k != "signature"}


def _sign(record: dict, signing_private: bytes) -> dict:
    record["signature"] = crypto.b64u_encode(crypto.sign(signing_private, crypto.canonical_json(_strip_signature(record))))
    return record


def _verify(record: Mapping[str, Any], signing_public: bytes) -> bool:
    try:
        return crypto.verify(signing_public, crypto.canonical_json(_strip_signature(record)),
                             crypto.b64u_decode(record.get("signature", "")))
    except crypto.CryptoError:
        return False


# ---------------------------------------------------------------------------
# Certificates
# ---------------------------------------------------------------------------

def issue_certificate(*, network_id: str, issuer_device_id: str, issuer_signing_private: bytes,
                      device_id: str, signing_public: bytes, encryption_public: bytes, name: str,
                      kind: str, generation: int, legacy_instance_id: Optional[str] = None) -> dict:
    if kind not in KINDS:
        raise MembershipError("Unknown device kind")
    if device_id != device_id_for(signing_public):
        raise MembershipError("Device id does not match its signing key")
    record = {
        "v": RECORD_VERSION,
        "type": TYPE_CERTIFICATE,
        "network_id": network_id,
        "device_id": device_id,
        "signing_public": crypto.b64u_encode(signing_public),
        "encryption_public": crypto.b64u_encode(encryption_public),
        "name": name,
        "kind": kind,
        "generation": generation,
        "issued_by": issuer_device_id,
        "issued_at": now_iso(),
    }
    if legacy_instance_id:
        record["legacy_instance_id"] = legacy_instance_id
    return _sign(record, issuer_signing_private)


def certificate_fields(record: Mapping[str, Any]) -> Member:
    """Shape-check a certificate and lift it to a ``Member`` (unverified)."""
    try:
        if record["v"] != RECORD_VERSION or record["type"] not in (TYPE_CERTIFICATE, TYPE_RECOVERY):
            raise MembershipError("Not a certificate")
        signing_public = crypto.b64u_decode(record["signing_public"])
        encryption_public = crypto.b64u_decode(record["encryption_public"])
        if len(signing_public) != 32 or len(encryption_public) != 32:
            raise MembershipError("Certificate keys have the wrong size")
        if record["device_id"] != device_id_for(signing_public):
            raise MembershipError("Certificate device id does not match its key")
        if record["kind"] not in KINDS:
            raise MembershipError("Unknown device kind")
        generation = record["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise MembershipError("Bad generation")
        name = record.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise MembershipError("Bad device name")
        return Member(device_id=record["device_id"], signing_public=signing_public,
                      encryption_public=encryption_public, name=name.strip(), kind=record["kind"],
                      generation=generation)
    except MembershipError:
        raise
    except (KeyError, TypeError, crypto.CryptoError) as exc:
        raise MembershipError("Malformed certificate") from exc


def verify_certificate(record: Mapping[str, Any], *, network_id: str,
                       members: Mapping[str, Member], founder_device_id: Optional[str],
                       network_key: Optional[bytes] = None) -> Member:
    """Verify a certificate against the members we already trust.

    Accepts, in order: a founder's self-signed certificate; a certificate
    signed by an active member; a recovery certificate whose proof opens
    under the network key. Raises ``MembershipError`` otherwise.
    """
    member = certificate_fields(record)
    if record.get("network_id") != network_id:
        raise MembershipError("Certificate belongs to another network")
    if record["type"] == TYPE_RECOVERY:
        if network_key is None:
            raise MembershipError("Recovery certificate needs the network key to verify")
        if not _verify(record, member.signing_public):
            raise MembershipError("Recovery certificate signature is invalid")
        if not verify_recovery_proof(record, network_key):
            raise MembershipError("Recovery certificate proof is invalid")
        return member
    issuer = record.get("issued_by")
    if issuer == member.device_id:
        if founder_device_id not in (None, member.device_id):
            raise MembershipError("Only the founder may self-certify")
        if not _verify(record, member.signing_public):
            raise MembershipError("Self-signed certificate is invalid")
        known = members.get(member.device_id)
        if known is not None and (known.generation != member.generation
                                  or known.signing_public != member.signing_public
                                  or known.encryption_public != member.encryption_public):
            # The founder's self-signed certificate is a trust root, issued
            # once when the network is created. A founder that was removed
            # cannot re-admit itself by publishing another one; like any
            # other device it needs a certificate from an active member.
            raise MembershipError("The founder certificate is already established")
        return member
    signer = members.get(issuer)
    if signer is None or signer.status != STATUS_ACTIVE:
        raise MembershipError("Certificate issuer is not an active member")
    if not _verify(record, signer.signing_public):
        raise MembershipError("Certificate signature is invalid")
    return member


# ---------------------------------------------------------------------------
# Revocation and key rotation
# ---------------------------------------------------------------------------

def issue_revocation(*, network_id: str, issuer_device_id: str, issuer_signing_private: bytes,
                     device_id: str, generation: int, last_seq_accepted: int,
                     last_incarnation_accepted: int = 0, reason: str = "") -> dict:
    record = {
        "v": RECORD_VERSION,
        "type": TYPE_REVOCATION,
        "network_id": network_id,
        "device_id": device_id,
        "generation": generation,
        "last_seq_accepted": int(last_seq_accepted),
        "last_incarnation_accepted": int(last_incarnation_accepted),
        "reason": reason[:200],
        "issued_by": issuer_device_id,
        "issued_at": now_iso(),
    }
    return _sign(record, issuer_signing_private)


def verify_revocation(record: Mapping[str, Any], *, network_id: str, members: Mapping[str, Member]) -> dict:
    try:
        if record["v"] != RECORD_VERSION or record["type"] != TYPE_REVOCATION:
            raise MembershipError("Not a revocation")
        if record["network_id"] != network_id:
            raise MembershipError("Revocation belongs to another network")
        generation = record["generation"]
        last_seq = record["last_seq_accepted"]
        last_incarnation = record.get("last_incarnation_accepted")
        for value in (generation, last_seq) + ((last_incarnation,) if last_incarnation is not None else ()):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise MembershipError("Bad revocation counters")
        issuer = members.get(record["issued_by"])
    except MembershipError:
        raise
    except (KeyError, TypeError) as exc:
        raise MembershipError("Malformed revocation") from exc
    if issuer is None or issuer.status != STATUS_ACTIVE:
        raise MembershipError("Revocation issuer is not an active member")
    if record["issued_by"] == record["device_id"]:
        raise MembershipError("A device cannot revoke itself")
    if not _verify(record, issuer.signing_public):
        raise MembershipError("Revocation signature is invalid")
    return {"device_id": record["device_id"], "generation": generation, "last_seq_accepted": last_seq,
            "last_incarnation_accepted": last_incarnation}


def issue_key_record(*, network_id: str, issuer_device_id: str, issuer_signing_private: bytes,
                     key_version: int, generation: int, new_key: bytes,
                     recipients: Mapping[str, bytes]) -> dict:
    """Wrap ``new_key`` to every recipient (device id -> X25519 public key)."""
    record = {
        "v": RECORD_VERSION,
        "type": TYPE_KEY,
        "network_id": network_id,
        "key_version": key_version,
        "generation": generation,
        "wraps": {device_id: crypto.wrap_key(public, new_key) for device_id, public in recipients.items()},
        "issued_by": issuer_device_id,
        "issued_at": now_iso(),
    }
    return _sign(record, issuer_signing_private)


def verify_key_record(record: Mapping[str, Any], *, network_id: str, members: Mapping[str, Member]) -> dict:
    try:
        if record["v"] != RECORD_VERSION or record["type"] != TYPE_KEY:
            raise MembershipError("Not a key record")
        if record["network_id"] != network_id:
            raise MembershipError("Key record belongs to another network")
        version = record["key_version"]
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            raise MembershipError("Bad key version")
        generation = record["generation"]
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
            raise MembershipError("Bad key record generation")
        if not isinstance(record["wraps"], dict):
            raise MembershipError("Bad wraps")
        issuer = members.get(record["issued_by"])
    except MembershipError:
        raise
    except (KeyError, TypeError) as exc:
        raise MembershipError("Malformed key record") from exc
    if issuer is None or issuer.status != STATUS_ACTIVE:
        raise MembershipError("Key record issuer is not an active member")
    if not _verify(record, issuer.signing_public):
        raise MembershipError("Key record signature is invalid")
    return {"key_version": version, "generation": record["generation"], "wraps": record["wraps"]}


def unwrap_from_key_record(record: Mapping[str, Any], device_id: str, encryption_private: bytes) -> Optional[bytes]:
    wrapped = (record.get("wraps") or {}).get(device_id)
    if wrapped is None:
        return None
    return crypto.unwrap_key(encryption_private, wrapped)


# ---------------------------------------------------------------------------
# Recovery certificates
# ---------------------------------------------------------------------------

def _proof_aad(record: Mapping[str, Any]) -> bytes:
    return crypto.canonical_json({k: v for k, v in record.items() if k not in ("signature", "proof")})


def issue_recovery_certificate(*, network_id: str, network_key: bytes, signing_private: bytes,
                               encryption_public: bytes, name: str, kind: str, generation: int) -> dict:
    signing_public = crypto.signing_public_key(signing_private)
    record = {
        "v": RECORD_VERSION,
        "type": TYPE_RECOVERY,
        "network_id": network_id,
        "device_id": device_id_for(signing_public),
        "signing_public": crypto.b64u_encode(signing_public),
        "encryption_public": crypto.b64u_encode(encryption_public),
        "name": name,
        "kind": kind,
        "generation": generation,
        "issued_by": device_id_for(signing_public),
        "issued_at": now_iso(),
    }
    proof_key = crypto._hkdf(network_key, _PROOF_INFO)
    record["proof"] = crypto.encrypt_payload(proof_key, b"recovery", _proof_aad(record))
    return _sign(record, signing_private)


def verify_recovery_proof(record: Mapping[str, Any], network_key: bytes) -> bool:
    try:
        proof_key = crypto._hkdf(network_key, _PROOF_INFO)
        return crypto.decrypt_payload(proof_key, record.get("proof", ""), _proof_aad(record)) == b"recovery"
    except (crypto.CryptoError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Applying records to a members map (pure)
# ---------------------------------------------------------------------------

def apply_record(record: Mapping[str, Any], *, network_id: str, members: Dict[str, Member],
                 founder_device_id: Optional[str], network_key: Optional[bytes] = None) -> Dict[str, Member]:
    """Return the members map after ``record``; raises when it must be rejected."""
    kind = record.get("type")
    if kind in (TYPE_CERTIFICATE, TYPE_RECOVERY):
        member = verify_certificate(record, network_id=network_id, members=members,
                                    founder_device_id=founder_device_id, network_key=network_key)
        existing = members.get(member.device_id)
        if existing is not None and existing.status == STATUS_REVOKED and existing.generation >= member.generation:
            raise MembershipError("Device was revoked at a later generation")
        members[member.device_id] = member
        return members
    if kind == TYPE_REVOCATION:
        details = verify_revocation(record, network_id=network_id, members=members)
        existing = members.get(details["device_id"])
        if existing is None:
            raise MembershipError("Revocation names an unknown device")
        members[details["device_id"]] = Member(
            device_id=existing.device_id, signing_public=existing.signing_public,
            encryption_public=existing.encryption_public, name=existing.name, kind=existing.kind,
            generation=details["generation"], status=STATUS_REVOKED,
            last_seq_accepted=details["last_seq_accepted"],
            last_incarnation_accepted=details["last_incarnation_accepted"])
        return members
    if kind == TYPE_KEY:
        verify_key_record(record, network_id=network_id, members=members)
        return members
    raise MembershipError("Unknown membership record type")
