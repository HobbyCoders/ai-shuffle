"""Short-lived device tokens obtained by a signed challenge.

A peer proves it holds the private key of an active member:

    1. ``POST /api/v1/network/peer/auth/challenge`` -> ``{challenge}``
       (the challenge is issued to one member device id and only that
       device's signature can spend it)
    2. the peer signs ``"shuffle-device-token-v1" || challenge || audience``
       with its Ed25519 key and posts the signature
    3. this worker checks the member is active, mints an HMAC token bound to
       the device id, this network and its membership generation, with an
       expiry (five minutes), and returns it

Tokens are stateless: a local per-process secret keys the HMAC, so a restart
invalidates every outstanding token and peers simply re-authenticate. The
membership check runs again on every authenticated request, so a revoked
device loses access within one token lifetime at most; a membership change
(new generation) invalidates every token at once.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from typing import Dict, Optional, Tuple

from fastapi import HTTPException, Request, status

from app.network import crypto, engine, membership

TOKEN_TTL_SECONDS = 300
CHALLENGE_TTL_SECONDS = 120
# Outstanding challenges per (requester address, member device). A member
# re-authenticates from a handful of endpoints at most; anything beyond this is
# a flood and evicts that requester's own oldest challenge for the device,
# never one another requester (the member itself) is about to spend.
CHALLENGES_PER_DEVICE = 8
# Hard ceiling on all outstanding challenges (memory bound under a flood from
# many addresses); past it the oldest overall is evicted.
CHALLENGES_MAX = 4096
_DOMAIN = b"shuffle-device-token-v1"

_secret = secrets.token_bytes(32)
_lock = threading.Lock()
# challenge -> (expires_at_monotonic, device_id, requester)
_challenges: Dict[str, Tuple[float, str, str]] = {}


def new_challenge(device_id: str, requester: str = "") -> str:
    """Issue a challenge for an active member; unknown devices get none.

    ``requester`` is the caller's address: the per-device bound applies per
    requester, so naming a member's public device id from another host cannot
    evict the challenge that member is about to spend.
    """
    ctx = engine.context()
    if ctx is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This worker has no network")
    member = ctx.member(device_id)
    if member is None or member.status != membership.STATUS_ACTIVE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Device is not an active member of this network")
    challenge = crypto.b64u_encode(secrets.token_bytes(32))
    with _lock:
        now = time.monotonic()
        for stale in [c for c, (exp, _d, _r) in _challenges.items() if exp <= now]:
            _challenges.pop(stale, None)
        # Sort only by expiry: Python's stable sort preserves issuance order
        # when Windows gives several calls the same monotonic timestamp.
        mine = sorted(((exp, c) for c, (exp, d, r) in _challenges.items() if d == device_id and r == requester),
                      key=lambda item: item[0])
        while len(mine) >= CHALLENGES_PER_DEVICE:
            _exp, oldest = mine.pop(0)
            _challenges.pop(oldest, None)
        while len(_challenges) >= CHALLENGES_MAX:
            # Dicts keep insertion order: the first key is the oldest issued.
            _challenges.pop(next(iter(_challenges)), None)
        _challenges[challenge] = (now + CHALLENGE_TTL_SECONDS, device_id, requester)
    return challenge


def _take_challenge(challenge: str, device_id: str) -> bool:
    with _lock:
        entry = _challenges.pop(challenge, None)
    if entry is None:
        return False
    expires, issued_to, _requester = entry
    return expires > time.monotonic() and hmac.compare_digest(issued_to, device_id)


def challenge_message(challenge: str, audience: str) -> bytes:
    return _DOMAIN + b"|" + challenge.encode("ascii") + b"|" + audience.encode("utf-8")


def sign_challenge(signing_private: bytes, challenge: str, audience: str) -> str:
    return crypto.b64u_encode(crypto.sign(signing_private, challenge_message(challenge, audience)))


def issue_token(*, device_id: str, challenge: str, signature: str) -> dict:
    """Verify the challenge signature and mint a token for an active member."""
    ctx = engine.context()
    if ctx is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This worker has no network")
    member = ctx.member(device_id)
    if member is None or member.status != membership.STATUS_ACTIVE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Device is not an active member of this network")
    if not _take_challenge(challenge, device_id):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Challenge expired or unknown")
    try:
        ok = crypto.verify(member.signing_public, challenge_message(challenge, ctx.device_id),
                           crypto.b64u_decode(signature))
    except crypto.CryptoError:
        ok = False
    if not ok:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Challenge signature is invalid")
    expires = int(time.time()) + TOKEN_TTL_SECONDS
    return {"token": _mint(device_id, expires, _scope(ctx)), "expires_at": expires, "device_id": ctx.device_id,
            "network_id": ctx.network_id, "generation": ctx.generation}


def _scope(ctx: engine.NetworkContext) -> str:
    """What a token is bound to besides the device: this network at this membership generation."""
    return hashlib.sha256(f"{ctx.network_id}|{ctx.generation}".encode("utf-8")).hexdigest()[:16]


def _mint(device_id: str, expires: int, scope: str) -> str:
    body = f"{device_id}.{expires}.{scope}"
    mac = hmac.new(_secret, body.encode("utf-8"), "sha256").hexdigest()
    return f"dvt_{body}.{mac}"


def verify_token(token: Optional[str]) -> Optional[str]:
    """Device id for a valid token issued for the current network generation, else None."""
    if not token or not token.startswith("dvt_"):
        return None
    try:
        device_id, expires, scope, mac = token[4:].rsplit(".", 3)
        expires_at = int(expires)
    except ValueError:
        return None
    expected = hmac.new(_secret, f"{device_id}.{expires_at}.{scope}".encode("utf-8"), "sha256").hexdigest()
    if not hmac.compare_digest(mac, expected) or expires_at < time.time():
        return None
    ctx = engine.context()
    if ctx is None or not hmac.compare_digest(scope, _scope(ctx)):
        return None
    return device_id


def require_device(request: Request) -> str:
    """FastAPI dependency: the calling peer's device id, or 401/403."""
    header = request.headers.get("Authorization", "")
    token = header[7:] if header.startswith("Bearer ") else None
    device_id = verify_token(token)
    if device_id is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Device token missing or expired")
    ctx = engine.context()
    if ctx is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This worker has no network")
    member = ctx.member(device_id)
    if member is None or member.status != membership.STATUS_ACTIVE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Device is no longer a member of this network")
    request.state.peer_device_id = device_id
    return device_id


def reset_for_tests() -> None:
    global _secret
    with _lock:
        _challenges.clear()
    _secret = secrets.token_bytes(32)
