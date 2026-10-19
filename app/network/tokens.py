"""Short-lived device tokens obtained by a signed challenge.

A peer proves it holds the private key of an active member:

    1. ``POST /api/v1/network/peer/auth/challenge`` -> ``{challenge}``
       (the challenge is issued to one member device id and only that
       device's signature can spend it)
    2. the peer signs a versioned transcript of challenge, audience and dialed
       base endpoint, plus the legacy proof during the compatibility window
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
import ipaddress
import re
import threading
import time
from collections import OrderedDict
from typing import Dict, Iterable, Optional, Tuple
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, status

from app.network import crypto, engine, membership
from app.core.config import settings
from app.db import network as store

ENDPOINT_NOT_ADVERTISED = "endpoint_not_advertised"
TOKEN_TTL_SECONDS = 300
CHALLENGE_TTL_SECONDS = 120
# Outstanding challenges per (requester address, member device). A member
# re-authenticates from a handful of endpoints at most; anything beyond this is
# a flood and evicts that requester's own oldest challenge for the device,
# never one another requester (the member itself) is about to spend.
CHALLENGES_PER_DEVICE = 8
# Ceiling on outstanding challenges from unrecognised addresses (memory bound
# under a flood from many addresses). Past it new ones are refused with 429;
# a pending challenge is never evicted to make room, so a flood cannot take a
# member's challenge away during its round trip.
CHALLENGES_MAX = 4096
# Member addresses keep headroom beyond the ceiling: a (device, address) pair
# that has spent a challenge with the member's key, or an address the member
# advertises as its own endpoint, can always hold its CHALLENGES_PER_DEVICE.
# The remembered pairs are bounded (least recently used dropped), which also
# bounds the reserved headroom to MEMBER_ADDRESSES_MAX * CHALLENGES_PER_DEVICE.
MEMBER_ADDRESSES_MAX = 1024
CHALLENGE_RETRY_AFTER_SECONDS = 30
_DOMAIN = b"shuffle-device-token-v1"

_secret = secrets.token_bytes(32)
_lock = threading.Lock()
# challenge -> (expires_at_monotonic, device_id, requester, reserved)
_challenges: Dict[str, Tuple[float, str, str, bool]] = {}
# (device_id, requester) pairs proven to belong to the member (a spent challenge).
_member_addresses: "OrderedDict[Tuple[str, str], None]" = OrderedDict()


def at_capacity() -> bool:
    """True when an unrecognised address would be refused a new challenge right now."""
    with _lock:
        _purge_expired(time.monotonic())
        return _unreserved_count() >= CHALLENGES_MAX


def is_member_endpoint(requester: str, endpoints: Iterable[object]) -> bool:
    """Whether ``requester`` is the literal IP host of one of a member's advertised endpoints."""
    try:
        address = ipaddress.ip_address(requester)
    except ValueError:
        return False
    for url in endpoints or ():
        if not isinstance(url, str):
            continue
        try:
            host = urlsplit(url).hostname
            if host and ipaddress.ip_address(host) == address:
                return True
        except ValueError:
            continue
    return False


def _purge_expired(now: float) -> None:
    for stale in [c for c, entry in _challenges.items() if entry[0] <= now]:
        _challenges.pop(stale, None)


def _unreserved_count() -> int:
    return sum(1 for entry in _challenges.values() if not entry[3])


def is_known_member_address(device_id: str, requester: str) -> bool:
    """Whether this address has proved possession of this member's signing key."""
    with _lock:
        return (device_id, requester) in _member_addresses


def _remember_member_address(device_id: str, requester: str) -> None:
    key = (device_id, requester)
    with _lock:
        _member_addresses[key] = None
        _member_addresses.move_to_end(key)
        while len(_member_addresses) > MEMBER_ADDRESSES_MAX:
            _member_addresses.popitem(last=False)


def new_challenge(device_id: str, requester: str = "", *, member_address: bool = False) -> str:
    """Issue a challenge for an active member; unknown devices get none.

    ``requester`` is the caller's address: the per-device bound applies per
    requester, so naming a member's public device id from another host cannot
    evict the challenge that member is about to spend. ``member_address`` says
    the caller already matched ``requester`` to the member's advertised
    endpoints; such an address (like one that has spent a challenge before)
    is not subject to the global ceiling.
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
        _purge_expired(now)
        reserved = member_address or (device_id, requester) in _member_addresses
        # Sort only by expiry: Python's stable sort preserves issuance order
        # when Windows gives several calls the same monotonic timestamp.
        mine = sorted(((entry[0], c) for c, entry in _challenges.items()
                       if entry[1] == device_id and entry[2] == requester),
                      key=lambda item: item[0])
        # The global ceiling never evicts: only this requester's own oldest
        # challenge for this device makes room, and past the ceiling an
        # unrecognised address that has no slot to recycle is refused.
        if not reserved and len(mine) < CHALLENGES_PER_DEVICE and _unreserved_count() >= CHALLENGES_MAX:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS,
                                "Too many outstanding authentication challenges; retry shortly",
                                headers={"Retry-After": str(CHALLENGE_RETRY_AFTER_SECONDS)})
        while len(mine) >= CHALLENGES_PER_DEVICE:
            _exp, oldest = mine.pop(0)
            _challenges.pop(oldest, None)
        _challenges[challenge] = (now + CHALLENGE_TTL_SECONDS, device_id, requester, reserved)
    return challenge


def _peek_challenge(challenge: str, device_id: str) -> Optional[str]:
    """The address ``challenge`` was issued to when it is live and for ``device_id``; it stays unspent.

    The LAN session verifies the member's signature first and only then
    spends the challenge, so a caller who merely saw it (a LAN hello answers
    in the clear) cannot burn it with a junk signature.
    """
    with _lock:
        entry = _challenges.get(challenge)
    if entry is None:
        return None
    expires, issued_to, requester, _reserved = entry
    if expires > time.monotonic() and hmac.compare_digest(issued_to, device_id):
        return requester
    return None


def _take_challenge(challenge: str, device_id: str) -> Optional[str]:
    """Spend ``challenge`` for ``device_id``; the address it was issued to, or None."""
    with _lock:
        entry = _challenges.pop(challenge, None)
    if entry is None:
        return None
    expires, issued_to, requester, _reserved = entry
    if expires > time.monotonic() and hmac.compare_digest(issued_to, device_id):
        return requester
    return None


def challenge_message(challenge: str, audience: str) -> bytes:
    return _DOMAIN + b"|" + challenge.encode("ascii") + b"|" + audience.encode("utf-8")


def sign_challenge(signing_private: bytes, challenge: str, audience: str) -> str:
    return crypto.b64u_encode(crypto.sign(signing_private, challenge_message(challenge, audience)))


def canonical_endpoint(endpoint: str) -> str:
    """Canonical HTTP(S) base, never obtained from request/forwarded headers.

    Lowercase scheme and ASCII host; compress/bracket IPv6; omit default ports
    and trailing slashes. Preserve path case and interior slashes, decode
    unreserved escapes and uppercase other escapes. Reject ambiguous dot paths,
    credentials, queries, fragments, scoped IPv6, whitespace and backslashes.
    """
    if not isinstance(endpoint, str) or not endpoint or any(
            c.isspace() or ord(c) < 32 or ord(c) == 127 for c in endpoint):
        raise ValueError("Invalid endpoint")
    if "\\" in endpoint or "?" in endpoint or "#" in endpoint:
        raise ValueError("Invalid endpoint")
    url = urlsplit(endpoint)
    if url.scheme not in ("http", "https") or not url.hostname or url.username is not None or url.password is not None:
        raise ValueError("Invalid endpoint")
    # Reject before .hostname lowercases Unicode (e.g. the Kelvin sign).
    # Never use the stdlib IDNA-2003 codec: HTTPX dials IDNA-2008 names, so
    # faß.example and fass.example must not collapse to one signed endpoint.
    # International names must arrive as literal ASCII A-labels on both sides.
    if not url.netloc.isascii():
        raise ValueError("Endpoint hosts must use ASCII A-labels")
    host = url.hostname
    if "%" in host:
        raise ValueError("Scoped or escaped host is not supported")
    if ":" in host:
        host = "[" + str(ipaddress.IPv6Address(host)) + "]"
    else:
        host = host.lower()
        labels = host.removesuffix(".").split(".")
        if len(host.removesuffix(".")) > 253 or any(
                not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels):
            raise ValueError("Invalid ASCII endpoint host")
    port = url.port
    authority = host if port is None or port == {"http": 80, "https": 443}[url.scheme] else f"{host}:{port}"
    path = url.path
    if not path.isascii() or re.search(r"%(?![0-9a-fA-F]{2})", path):
        raise ValueError("Invalid escaped path")
    def escape(match):
        char = chr(int(match.group()[1:], 16))
        return char if char.isascii() and (char.isalnum() or char in "-._~") else match.group().upper()
    path = re.sub(r"%[0-9a-fA-F]{2}", escape, path)
    if any(part in (".", "..") for part in path.split("/")):
        raise ValueError("Dot path segments are not supported")
    return f"{url.scheme}://{authority}{path.rstrip('/')}"


def bound_challenge_message(challenge: str, audience: str, endpoint: str) -> bytes:
    return crypto.canonical_json(["shuffle-device-token-bound-v1", challenge, audience,
                                  canonical_endpoint(endpoint)])


def sign_bound_challenge(signing_private: bytes, challenge: str, audience: str, endpoint: str) -> str:
    return crypto.b64u_encode(crypto.sign(signing_private, bound_challenge_message(challenge, audience, endpoint)))


def _binding_support_message(network_id: str, device_id: str) -> bytes:
    return crypto.canonical_json(["shuffle-device-token-binding-support-v1", network_id, device_id, 1])


def binding_support_proof(ctx: engine.NetworkContext) -> str:
    return crypto.b64u_encode(crypto.sign(ctx.identity.signing_private,
                                       _binding_support_message(ctx.network_id, ctx.device_id)))


def remember_binding_support(device_id: str, proof: Optional[str]) -> None:
    """Only a key-signed capability statement may set the local durable pin."""
    if proof is None:
        return
    ctx = engine.require_context()
    member = ctx.member(device_id)
    try:
        ok = member is not None and member.status == membership.STATUS_ACTIVE and crypto.verify(
            member.signing_public, _binding_support_message(ctx.network_id, device_id), crypto.b64u_decode(proof))
    except (crypto.CryptoError, ValueError, TypeError):
        ok = False
    if not ok:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Invalid token binding capability proof")
    store.pin_token_binding(device_id, crypto.b64u_encode(member.signing_public))


def issue_token(*, device_id: str, challenge: str, signature: str,
                endpoint: Optional[str] = None, bound_signature: Optional[str] = None,
                token_binding_proof: Optional[str] = None, binding_present: bool = False) -> dict:
    """Verify the challenge signature and mint a token for an active member."""
    ctx = engine.context()
    if ctx is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "This worker has no network")
    member = ctx.member(device_id)
    if member is None or member.status != membership.STATUS_ACTIVE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Device is not an active member of this network")
    from app.network import lan
    if lan.is_pending(challenge):
        # A LAN hello answered this challenge, in the clear: it buys only that
        # LAN session, and an attempt here must not spend it either.
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Challenge expired or unknown")
    requester = _take_challenge(challenge, device_id)
    if requester is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Challenge expired or unknown")
    try:
        ok = crypto.verify(member.signing_public, challenge_message(challenge, ctx.device_id),
                           crypto.b64u_decode(signature))
    except crypto.CryptoError:
        ok = False
    if not ok:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Challenge signature is invalid")
    remember_binding_support(device_id, token_binding_proof)
    # Any supplied binding is fail-closed, even for an unpinned legacy peer.
    # The allowlist is current local advertisement, never Host/X-Forwarded-*.
    bound = binding_present or endpoint is not None or bound_signature is not None
    if bound:
        try:
            canonical = canonical_endpoint(endpoint)
            allowed = set()
            for advertised in engine._own_endpoints():
                try:
                    allowed.add(canonical_endpoint(advertised))
                except ValueError:
                    continue
            ok = crypto.verify(
                member.signing_public, bound_challenge_message(challenge, ctx.device_id, canonical),
                crypto.b64u_decode(bound_signature))
        except (crypto.CryptoError, ValueError, TypeError):
            ok = False
        if not ok:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Endpoint binding is invalid")
        if canonical not in allowed:
            # Only distinguish this after verifying the proof. Do not disclose
            # our other addresses, the challenge, signatures or any token.
            raise HTTPException(status.HTTP_403_FORBIDDEN, {
                "code": ENDPOINT_NOT_ADVERTISED,
                "message": "The signed endpoint is not advertised by this worker",
            })
        store.pin_token_binding(device_id, crypto.b64u_encode(member.signing_public))
    elif settings.network_require_token_binding or (store.get_member(device_id) or {}).get("token_binding_required"):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Endpoint binding is required")
    # The member's key signed a challenge issued to this address: it keeps
    # headroom past the global ceiling from now on.
    _remember_member_address(device_id, requester)
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


def require_worker_device(request: Request) -> str:
    """FastAPI dependency: another active *worker* of this network, or 401/403.

    ``require_device`` admits any active member, and consumer devices (a phone,
    a paired browser) can hold a device token too. Routes that act with the
    owner's whole-worker authority admit only the account's other workers, the
    same rule the control plane applies before starting chats or subagents here.
    """
    device_id = require_device(request)
    ctx = engine.context()
    member = ctx.member(device_id) if ctx is not None else None
    if (ctx is None or device_id == ctx.device_id or member is None
            or member.kind != membership.KIND_WORKER or member.status != membership.STATUS_ACTIVE):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Only another worker of this account may do this")
    return device_id


def reset_for_tests() -> None:
    global _secret
    with _lock:
        _challenges.clear()
        _member_addresses.clear()
    _secret = secrets.token_bytes(32)
