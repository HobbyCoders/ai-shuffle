"""One-time device links: the owner proof a receiving worker accepts (#674).

A worker's network key cannot prove the owner: agents run as the same OS user
and can read it. So the receiver trusts only a signature from a key the owner
linked on this worker. The browser or app makes that key non-exportable and
never hands it to the requesting worker, which only relays. Each open signs a
single-use challenge this worker issued, so freshness needs no client clock.

The approval code is derived from the key, and is never shown on this worker:
the owner reads it on the device and types it here. A key an agent submitted
has a different code, so it cannot be approved by clicking the wrong row.

Contract: ``docs/internal/terminals.md``.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature

from app.db import terminal_device_links as store
from app.db.async_database import run_sync
from app.network import crypto

logger = logging.getLogger(__name__)

PURPOSE = "ai-shuffle-peer-terminal-v1"
ACTION_OPEN = "terminal.open"
ACTION_OPEN_AND_PASTE = "terminal.open_and_paste"
ACTIONS = (ACTION_OPEN, ACTION_OPEN_AND_PASTE)
CHALLENGE_SECONDS = 60
REQUEST_SECONDS = 600
MAX_CHALLENGES = 512
MAX_CHALLENGES_PER_PEER = 32
MAX_REQUESTS = 32
MAX_REQUESTS_PER_PEER = 4
MAX_PAYLOAD_CHARS = 8192
_P256_POINT_BYTES = 65
_SIGNATURE_BYTES = 64
_CODE_DOMAIN = b"ai-shuffle-terminal-link-code-v1:"
_TARGET_LIMITS = {"session_id": 200, "cwd": 4096, "shell": 32}

_challenges = {}
_requests = {}


class Refused(Exception):
    """A device proof this worker will not accept. The message is safe to show."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _decode_public_key(encoded):
    """The raw P-256 point, or None. Loading it rejects points off the curve."""
    try:
        raw = crypto.b64u_decode(encoded)
        if len(raw) != _P256_POINT_BYTES:
            return None
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), raw)
    except (TypeError, ValueError):
        return None
    return raw


def key_id_of(raw):
    return crypto.b64u_encode(hashlib.sha256(raw).digest())


def link_code(raw):
    """Sixty bits: a key's public half is visible to the relay, so a shorter code could be ground to match."""
    digest = hashlib.sha256(_CODE_DOMAIN + raw).digest()
    return base64.b32encode(digest[:8]).decode("ascii")[:12]


def _normalized_code(code):
    return re.sub(r"[\s-]", "", code if isinstance(code, str) else "").upper()


def _clean_label(label):
    text = re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", label if isinstance(label, str) else "").strip()
    return text[:80] or "Unnamed device"


def _prune():
    now = time.monotonic()
    for table in (_challenges, _requests):
        for key in [key for key, entry in table.items() if entry["expires"] <= now]:
            table.pop(key, None)


async def issue_challenge(from_device, key_id):
    """A single-use challenge when this key is linked through this worker."""
    _prune()
    link = await run_sync(store.get, key_id) if isinstance(key_id, str) else None
    if not link or link["via_device"] != from_device:
        pending = any(r["key_id"] == key_id and r["from_device"] == from_device for r in _requests.values())
        return {"status": "pending" if pending else "unlinked"}
    mine = [token for token, entry in _challenges.items() if entry["from_device"] == from_device]
    if len(mine) >= MAX_CHALLENGES_PER_PEER:
        _challenges.pop(mine[0], None)  # Dict order is issue order: the oldest goes.
    if len(_challenges) >= MAX_CHALLENGES:
        raise PermissionError("Too many outstanding terminal challenges")
    challenge = secrets.token_urlsafe(32)
    _challenges[challenge] = {"from_device": from_device, "key_id": key_id,
                              "expires": time.monotonic() + CHALLENGE_SECONDS}
    return {"status": "linked", "challenge": challenge, "expires_in": CHALLENGE_SECONDS}


async def request_link(from_device, from_name, public_key, label):
    """Record a key waiting for the owner. Holds no authority until approved."""
    raw = _decode_public_key(public_key)
    if raw is None:
        raise ValueError("Device public key must be an uncompressed P-256 point")
    key_id = key_id_of(raw)
    _prune()
    link = await run_sync(store.get, key_id)
    if link and link["via_device"] == from_device:
        return {"status": "linked", "key_id": key_id}
    existing = next((r for r in _requests.values()
                     if r["key_id"] == key_id and r["from_device"] == from_device), None)
    if existing is None:
        if len(_requests) >= MAX_REQUESTS or sum(r["from_device"] == from_device for r in _requests.values()) >= MAX_REQUESTS_PER_PEER:
            raise PermissionError("Too many pending device link requests")
        existing = {"id": secrets.token_urlsafe(16), "key_id": key_id, "public_key": public_key,
                    "label": _clean_label(label), "from_device": from_device, "from_name": from_name,
                    "created": time.monotonic(), "expires": time.monotonic() + REQUEST_SECONDS}
        _requests[existing["id"]] = existing
        logger.info("terminal device link requested via=%s key=%s", from_device, key_id[:12])
    existing["expires"] = time.monotonic() + REQUEST_SECONDS
    return {"status": "pending", "key_id": key_id, "expires_in": REQUEST_SECONDS}


def _whole_seconds(seconds):
    return max(0, int(round(seconds, 6)))


def pending_requests():
    """What the owner sees on this worker. The code is deliberately absent."""
    _prune()
    now = time.monotonic()
    return [{"request_id": r["id"], "label": r["label"], "worker_id": r["from_device"], "worker_name": r["from_name"],
             "age_seconds": _whole_seconds(now - r["created"]), "expires_in": _whole_seconds(r["expires"] - now)}
            for r in _requests.values()]


async def linked_devices():
    rows = await run_sync(store.list_links)
    return [{"key_id": row["key_id"], "label": row["label"], "worker_id": row["via_device"],
             "worker_name": row["via_name"], "linked_by": row["approved_by"],
             "created_at": str(row["created_at"]), "last_used_at": str(row["last_used_at"]) if row["last_used_at"] else None}
            for row in rows]


async def approve(code, who):
    """The owner's one action on this worker. Callers must be owner-authenticated."""
    _prune()
    wanted = _normalized_code(code).encode()
    # Compare every entry so timing says nothing about which one matched.
    matches = [entry for entry in list(_requests.values())
               if hmac.compare_digest(link_code(crypto.b64u_decode(entry["public_key"])).encode(), wanted)]
    if not matches:
        raise LookupError("No waiting device has that code. Check the code shown on the device.")
    if len({entry["key_id"] for entry in matches}) > 1 or len(matches) > 1:
        # Only a planted request can collide. Link nothing and let the owner look.
        raise ValueError("More than one waiting device has that code. Decline the ones you don't recognize.")
    found = matches[0]
    await run_sync(lambda: store.insert(key_id=found["key_id"], public_key=found["public_key"], label=found["label"],
                                        via_device=found["from_device"], via_name=found["from_name"], approved_by=who))
    _requests.pop(found["id"], None)
    logger.info("terminal device linked via=%s key=%s by=%s", found["from_device"], found["key_id"][:12], who)
    return {"key_id": found["key_id"], "label": found["label"], "worker_name": found["from_name"]}


def decline(request_id):
    if _requests.pop(request_id, None) is None:
        raise LookupError("That device link request expired")
    return {"declined": True}


async def revoke(key_id):
    if not await run_sync(store.delete, key_id):
        raise LookupError("That device is not linked")
    # Nothing issued to this key before the revocation stays spendable.
    for table in (_challenges, _requests):
        for token in [token for token, entry in table.items() if entry["key_id"] == key_id]:
            table.pop(token, None)
    logger.info("terminal device link revoked key=%s", key_id[:12])
    return {"revoked": True}


def _signature_valid(public_key, payload_text, signature):
    try:
        raw_signature = crypto.b64u_decode(signature)
        if len(raw_signature) != _SIGNATURE_BYTES:
            return False
        der = encode_dss_signature(int.from_bytes(raw_signature[:32], "big"), int.from_bytes(raw_signature[32:], "big"))
        key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), crypto.b64u_decode(public_key))
        key.verify(der, payload_text.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
    except (InvalidSignature, TypeError, ValueError):
        return False
    return True


def _signed_target(target):
    if not isinstance(target, dict) or set(target) != set(_TARGET_LIMITS):
        raise Refused("device_proof_invalid", "That terminal request is malformed.")
    for field, limit in _TARGET_LIMITS.items():
        value = target[field]
        if value is not None and (not isinstance(value, str) or not value or len(value) > limit):
            raise Refused("device_proof_invalid", "That terminal request is malformed.")
    return target


async def verify_open(proof, from_device, receiver):
    """The link and signed fields, or Refused. Spends the challenge either way."""
    if not isinstance(proof, dict) or not all(isinstance(proof.get(f), str) for f in ("key_id", "payload", "signature")):
        raise Refused("device_link_required", "Link this device to the worker before opening a terminal there.")
    if len(proof["payload"]) > MAX_PAYLOAD_CHARS:
        raise Refused("device_proof_invalid", "That terminal request is malformed.")
    try:
        payload = json.loads(proof["payload"])
    except ValueError:
        raise Refused("device_proof_invalid", "That terminal request is malformed.") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("challenge"), str):
        raise Refused("device_proof_invalid", "That terminal request is malformed.")
    challenge = _challenges.pop(payload["challenge"], None)
    if challenge is None or challenge["expires"] <= time.monotonic():
        raise Refused("device_proof_expired", "That terminal request expired. Try again.")
    if challenge["from_device"] != from_device or challenge["key_id"] != proof["key_id"]:
        raise Refused("device_proof_invalid", "That terminal request does not match its challenge.")
    link = await run_sync(store.get, proof["key_id"])
    if not link or link["via_device"] != from_device:
        raise Refused("device_link_required", "Link this device to the worker before opening a terminal there.")
    if not _signature_valid(link["public_key"], proof["payload"], proof["signature"]):
        raise Refused("device_proof_invalid", "This device's signature did not verify.")
    # Only the signature above makes these fields trustworthy.
    if (payload.get("purpose") != PURPOSE or payload.get("receiver") != receiver or payload.get("via") != from_device
            or payload.get("action") not in ACTIONS):
        raise Refused("device_proof_invalid", "That terminal request was not made for this worker and action.")
    return {"key_id": link["key_id"], "action": payload["action"], "target": _signed_target(payload.get("target"))}
