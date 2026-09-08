"""Short-lived device tokens obtained by a signed challenge.

A peer proves it holds the private key of an active member:

    1. ``POST /api/v1/network/peer/auth/challenge`` -> ``{challenge}``
    2. the peer signs ``"shuffle-device-token-v1" || challenge || audience``
       with its Ed25519 key and posts the signature
    3. this worker checks the member is active, mints an HMAC token bound to
       the device id and an expiry (five minutes) and returns it

Tokens are stateless: a local per-process secret keys the HMAC, so a restart
invalidates every outstanding token and peers simply re-authenticate. The
membership check runs again on every authenticated request, so a revoked
device loses access within one token lifetime at most.
"""
from __future__ import annotations

import hmac
import secrets
import threading
import time
from typing import Dict, Optional

from fastapi import HTTPException, Request, status

from app.network import crypto, engine, membership

TOKEN_TTL_SECONDS = 300
CHALLENGE_TTL_SECONDS = 120
_CHALLENGE_CAP = 4096
_DOMAIN = b"shuffle-device-token-v1"

_secret = secrets.token_bytes(32)
_lock = threading.Lock()
_challenges: Dict[str, float] = {}


def new_challenge() -> str:
    challenge = crypto.b64u_encode(secrets.token_bytes(32))
    with _lock:
        now = time.monotonic()
        if len(_challenges) >= _CHALLENGE_CAP:
            for stale in [c for c, exp in _challenges.items() if exp <= now]:
                _challenges.pop(stale, None)
            if len(_challenges) >= _CHALLENGE_CAP:
                oldest = min(_challenges, key=_challenges.get)
                _challenges.pop(oldest, None)
        _challenges[challenge] = now + CHALLENGE_TTL_SECONDS
    return challenge


def _take_challenge(challenge: str) -> bool:
    with _lock:
        expires = _challenges.pop(challenge, None)
    return expires is not None and expires > time.monotonic()


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
    if not _take_challenge(challenge):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Challenge expired or unknown")
    try:
        ok = crypto.verify(member.signing_public, challenge_message(challenge, ctx.device_id),
                           crypto.b64u_decode(signature))
    except crypto.CryptoError:
        ok = False
    if not ok:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Challenge signature is invalid")
    expires = int(time.time()) + TOKEN_TTL_SECONDS
    return {"token": _mint(device_id, expires), "expires_at": expires, "device_id": ctx.device_id,
            "network_id": ctx.network_id, "generation": ctx.generation}


def _mint(device_id: str, expires: int) -> str:
    body = f"{device_id}.{expires}"
    mac = hmac.new(_secret, body.encode("utf-8"), "sha256").hexdigest()
    return f"dvt_{body}.{mac}"


def verify_token(token: Optional[str]) -> Optional[str]:
    """Device id for a valid token, else None."""
    if not token or not token.startswith("dvt_"):
        return None
    try:
        device_id, expires, mac = token[4:].rsplit(".", 2)
        expires_at = int(expires)
    except ValueError:
        return None
    expected = hmac.new(_secret, f"{device_id}.{expires_at}".encode("utf-8"), "sha256").hexdigest()
    if not hmac.compare_digest(mac, expected) or expires_at < time.time():
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
