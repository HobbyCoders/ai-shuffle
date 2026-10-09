"""Direct sync between two workers on the same LAN, sealed end to end (#2169).

Two workers of one account that share a LAN still reached each other through
their Cloudflare tunnels: ``endpoints.dial_endpoints`` dials a peer's https
tunnel whenever it advertises one, because over plain http the device token
(a bearer good for five minutes) would cross the LAN in the clear. This
module adds a second way to reach such a peer that needs no TLS and carries
no bearer token, so the plain-http LAN address can be dialed first.

What the tunnel path guarantees, and what this path matches
-----------------------------------------------------------
Over the tunnel the client proves its identity with an Ed25519 signature over
a fresh challenge (``tokens``). The server's identity rests on Cloudflare TLS
and on the hostname the account assigned to that device: the client checks
only the ``device_id`` the server names in its JSON reply. TLS keeps the
bearer token, watermarks, addresses and capabilities confidential. The data
itself is protected end to end regardless of the transport: ops are sealed
with the network key and signed by their origin, snapshot pages and file
parts are encrypted to the recipient's X25519 key and signed by the sender.

The LAN path gives at least the same, without trusting the network at all:

1. ``POST /peer/lan/hello`` with the client's device id and a fresh X25519
   key. The server answers with a challenge, its own fresh X25519 key, and an
   Ed25519 signature by its *member signing key* over the transcript (both
   device ids, the network id, the challenge and both fresh keys). The client
   verifies it against the key in its own membership records, so an address
   on the LAN that answers as another device, or as no member at all, is
   refused before the client signs or sends anything else.
2. ``POST /peer/lan/session`` with the client's Ed25519 signature over the
   same transcript (another domain tag). The server verifies it against its
   membership records, exactly like a token request, and derives the session
   keys. Both sides compute X25519(client, server) and HKDF-SHA256 over it,
   salted with the transcript hash: one AES-256-GCM key per direction. The
   fresh keys are discarded, so a later key compromise does not open
   recorded traffic (forward secrecy, which the token path gets from TLS).
3. ``POST /peer/lan/call`` carries every later request and response sealed
   with those keys. The nonce is the call's sequence number, bound into the
   associated data with the session id and the direction; the server refuses
   a sequence number it has already seen, so a recorded call cannot be
   replayed. The method, path, query and body all travel inside the seal.

Nothing usable crosses the LAN in the clear: there is no bearer token, the
handshake signatures are bound to fresh keys and are useless elsewhere, and
watermarks, addresses and capabilities are sealed like the data. The two
handshake requests carry only device ids, the challenge, fresh public keys
and signatures; the server stores nothing from them. A member's
description (endpoints, capabilities, incarnation, version, platform)
reaches the server only inside the sealed exchange, so nobody on the LAN
can read it or plant an address in the member's row. The server verifies a
session signature before it spends the challenge, and ``/peer/auth/token``
refuses a challenge a hello answered without spending it, so a junk
signature from whoever saw the challenge on the wire cannot burn it. A session
is bound to the network, the membership generation and an active member;
it expires after ``SESSION_TTL_SECONDS`` and is refused (the client simply
opens a new one) after a restart, a membership change or the kill switch.

Only peer *sync* travels this way (exchange, ops, rollback proof, the state
snapshot, file parts, ping); the server dispatches an explicit list of those
routes. ``/peer/control`` (owner authority) and the other bearer users stay
on the tunnel.

Which addresses
---------------
Only plain-http literal IP addresses in private ranges are dialed directly:
RFC 1918 IPv4 and IPv6 unique-local (fc00::/7). A name could resolve to a
public address, and link-local stays excluded everywhere in the peer plane
(``endpoints.is_valid_endpoint``: cloud metadata, IPv6 zone ids). A worker
with a tunnel advertises nothing else besides it, so a public address the
tunnel hides is never published (``engine._own_endpoints``); the addresses
the account hub relays at admission are sealed to the admitted device
(``seal_admission_addresses``). In Docker the bridge address (172.17.0.0/16)
another host cannot reach is not advertised unless the owner configured it;
the host's LAN address is, once the owner reached the worker on it or set
it (``network_lan_addresses``, or the cluster advertise address).
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import secrets
import struct
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

import httpx
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import x25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.network import crypto, membership
from app.network.endpoints import embedded_ipv4, validate_endpoints

logger = logging.getLogger(__name__)

PROTOCOL = 1
# A LAN address that does not answer must not hold up the tunnel: connect
# within this, and finish the handshake within LAN_HANDSHAKE_TIMEOUT.
LAN_CONNECT_TIMEOUT = 1.5
LAN_HANDSHAKE_TIMEOUT = 4.0
# After the LAN path failed, the tunnel is used and the LAN tried again this
# much later, or as soon as the peer's addresses change.
LAN_REPROBE_SECONDS = 600.0
# At most this many LAN addresses are tried per probe.
LAN_PROBE_ADDRESSES = 3
SESSION_TTL_SECONDS = 600.0
# Clients renew a session this long before the server would refuse it.
SESSION_RENEW_MARGIN_SECONDS = 30.0
SESSIONS_PER_DEVICE = 4
SESSIONS_MAX = 256
# Pending handshakes (hello answered, session not yet opened). Past it new
# hellos are refused rather than evicting another requester's handshake.
PENDING_MAX = 2048
# Sequence numbers kept per session to refuse replays; older ones are
# refused below a moving floor.
REPLAY_WINDOW = 512
CALL_MAX_BYTES = 64 * 1024 * 1024
REPLY_MAX_BYTES = 64 * 1024 * 1024

SETTING_ENABLED = "network_lan_direct"
SETTING_ADDRESSES = "network_lan_addresses"
MAX_OWNER_ADDRESSES = 4

SESSION_HEADER = "X-Shuffle-Lan-Session"
SEQ_HEADER = "X-Shuffle-Lan-Seq"

HELLO_PATH = "/api/v1/network/peer/lan/hello"
SESSION_PATH = "/api/v1/network/peer/lan/session"
CALL_PATH = "/api/v1/network/peer/lan/call"
PATHS = (HELLO_PATH, SESSION_PATH, CALL_PATH)

_HELLO_DOMAIN = b"shuffle-lan-hello-v1|"
_SESSION_DOMAIN = b"shuffle-lan-session-v1|"
_KEYS_INFO = b"shuffle-lan-keys-v1"
_CALL_AAD = "shuffle-lan-call-v1"
_REPLY_AAD = "shuffle-lan-reply-v1"
_ADMISSION_INFO = b"shuffle-lan-admission-v1|"

_PRIVATE_V4 = (ipaddress.IPv4Network("10.0.0.0/8"), ipaddress.IPv4Network("172.16.0.0/12"),
               ipaddress.IPv4Network("192.168.0.0/16"))
_UNIQUE_LOCAL = ipaddress.IPv6Network("fc00::/7")
DOCKER_BRIDGE = ipaddress.IPv4Network("172.17.0.0/16")


class LanError(RuntimeError):
    """The LAN path is not usable right now; the tunnel takes over."""


class LanUnavailable(LanError):
    """The address did not answer, or answers without this protocol (an older worker)."""


class LanIdentityMismatch(LanError):
    """An address on the LAN answered, but not as the expected member."""


class LanSessionRejected(LanError):
    """The peer no longer knows the session (restart, expiry, membership change)."""


class LanRejected(Exception):
    """Server side: a call or handshake step that must be refused."""


class LanBusy(LanRejected):
    """Server side: the pending-handshake table is full (the client uses the tunnel meanwhile)."""


# ---------------------------------------------------------------------------
# Addresses
# ---------------------------------------------------------------------------

def lan_address(url: object) -> Optional[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """The private IP a plain-http LAN address names, else None.

    Only literal RFC 1918 IPv4 or IPv6 unique-local addresses qualify (IPv4
    carried in an IPv6 form counts as that IPv4 address). Names, https,
    loopback, link-local and public addresses never do.
    """
    if not isinstance(url, str) or validate_endpoints([url]) == []:
        return None
    try:
        parts = urlsplit(url)
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None
    if parts.scheme != "http" or not host or (port is not None and not 1 <= port <= 65535):
        return None
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        return None
    try:
        address = embedded_ipv4(ipaddress.ip_address(host))
    except ValueError:
        return None
    if address.version == 4:
        return address if any(address in net for net in _PRIVATE_V4) else None
    return address if address in _UNIQUE_LOCAL else None


def is_lan_candidate(url: object) -> bool:
    return lan_address(url) is not None


def is_docker_bridge(url: object) -> bool:
    address = lan_address(url)
    return address is not None and address.version == 4 and address in DOCKER_BRIDGE


def lan_candidates(endpoints: Iterable[object] | None) -> List[str]:
    """The addresses among ``endpoints`` that may be dialed directly, in order."""
    return [url for url in validate_endpoints(endpoints) if is_lan_candidate(url)]


def normalize_owner_address(text: str, *, default_port: int) -> str:
    """``10.0.0.200:8001`` or ``http://10.0.0.200:8001`` -> ``http://10.0.0.200:8001``.

    Raises ``ValueError`` for anything that is not a private literal IP.
    """
    raw = (text or "").strip().rstrip("/")
    if not raw:
        raise ValueError("Enter an address such as 10.0.0.200:8001")
    url = raw if "://" in raw else "http://" + raw
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError as exc:
        raise ValueError(f"{raw} is not an address and port") from exc
    if parts.scheme != "http":
        raise ValueError(f"{raw}: LAN addresses use plain http; the session is sealed by the workers")
    if not host:
        raise ValueError(f"{raw} has no address")
    netloc = f"[{host}]" if ":" in host else host
    url = f"http://{netloc}:{port or default_port}"
    if not is_lan_candidate(url):
        raise ValueError(f"{raw} is not a private LAN address (10.x, 172.16-31.x, 192.168.x or fc00::/7)")
    return url


def lan_direct_enabled() -> bool:
    """The owner's kill switch for this worker; on unless turned off."""
    from app.db import database
    value = database.get_system_setting(SETTING_ENABLED)
    return (value or "").strip().lower() not in ("0", "false", "off", "no")


def set_lan_direct_enabled(enabled: bool) -> None:
    from app.db import database
    database.set_system_setting(SETTING_ENABLED, "1" if enabled else "0")


def owner_addresses() -> List[str]:
    """Addresses the owner added for this worker (``10.0.0.200:8001`` for a Docker host)."""
    from app.db import database
    raw = database.get_system_setting(SETTING_ADDRESSES)
    if not raw:
        return []
    try:
        values = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(values, list):
        return []
    return [url for url in validate_endpoints(values) if is_lan_candidate(url)][:MAX_OWNER_ADDRESSES]


def set_owner_addresses(values: Iterable[str]) -> List[str]:
    from app.core.config import settings
    from app.db import database
    out: List[str] = []
    for value in values:
        url = normalize_owner_address(value, default_port=int(settings.port or 8000))
        if url not in out:
            out.append(url)
    if len(out) > MAX_OWNER_ADDRESSES:
        raise ValueError(f"At most {MAX_OWNER_ADDRESSES} addresses")
    database.set_system_setting(SETTING_ADDRESSES, json.dumps(out))
    return out


def advertised_addresses(*, tunnel: bool, advertise: Optional[str], source: str,
                         docker: bool, enabled: bool, extra: Iterable[str]) -> List[str]:
    """This worker's addresses besides its tunnel, as peers should learn them.

    Without a tunnel the advertised address is the only way in and is kept
    as before. With one, only private LAN addresses are published, so a
    public address the tunnel hides never leaks, and Docker's guessed bridge
    address (172.17.x.x) is dropped unless the owner configured it: another
    host cannot reach it, and the tunnel already serves. The owner's extra
    addresses (the host's LAN IP, say) are added while LAN-direct is on.
    """
    out: List[str] = []
    if advertise:
        keep = True
        if tunnel and not is_lan_candidate(advertise):
            keep = False
        if tunnel and docker and source != "config" and is_docker_bridge(advertise):
            keep = False
        if keep:
            out.append(advertise.rstrip("/"))
    if enabled:
        for url in extra:
            if url not in out:
                out.append(url)
    return out


# ---------------------------------------------------------------------------
# Handshake primitives
# ---------------------------------------------------------------------------

def _x25519_public(private: x25519.X25519PrivateKey) -> bytes:
    return private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def transcript(*, network_id: str, client: str, server: str, challenge: str,
               client_ephemeral: str, server_ephemeral: str) -> bytes:
    return crypto.canonical_json({"v": PROTOCOL, "network_id": network_id, "client": client, "server": server,
                                  "challenge": challenge, "client_ephemeral": client_ephemeral,
                                  "server_ephemeral": server_ephemeral})


def _derive(private: x25519.X25519PrivateKey, peer_public: bytes, transcript_bytes: bytes) -> Tuple[bytes, bytes]:
    """(client-to-server key, server-to-client key)."""
    shared = private.exchange(x25519.X25519PublicKey.from_public_bytes(peer_public))
    if shared == b"\x00" * 32:
        raise crypto.CryptoError("Degenerate key agreement")
    material = HKDF(algorithm=hashes.SHA256(), length=64, salt=hashlib.sha256(transcript_bytes).digest(),
                    info=_KEYS_INFO).derive(shared)
    return material[:32], material[32:]


def _nonce(seq: int) -> bytes:
    return b"\x00\x00\x00\x00" + struct.pack(">Q", seq)


def _aad(kind: str, session: str, seq: int) -> bytes:
    return f"{kind}|{session}|{seq}".encode("ascii")


def _decode_ephemeral(text: object) -> bytes:
    if not isinstance(text, str):
        raise crypto.CryptoError("Missing key")
    raw = crypto.b64u_decode(text)
    if len(raw) != 32:
        raise crypto.CryptoError("Bad key length")
    return raw


# ---------------------------------------------------------------------------
# Server side
# ---------------------------------------------------------------------------

@dataclass
class _Pending:
    expires: float
    device_id: str
    challenge: str
    private: x25519.X25519PrivateKey
    transcript: bytes
    client_public: bytes
    requester: str = ""


@dataclass
class ServerSession:
    id: str
    device_id: str
    network_id: str
    generation: int
    k_in: bytes
    k_out: bytes
    expires: float
    floor: int = 0
    seen: set = field(default_factory=set)


_lock = threading.Lock()
_pending: "OrderedDict[str, _Pending]" = OrderedDict()
_sessions: "OrderedDict[str, ServerSession]" = OrderedDict()


def _purge(now: float) -> None:
    for key in [k for k, p in _pending.items() if p.expires <= now]:
        _pending.pop(key, None)
    for key in [k for k, s in _sessions.items() if s.expires <= now]:
        _sessions.pop(key, None)


def server_hello(ctx, *, device_id: str, client_ephemeral: str, challenge: str, requester: str = "") -> dict:
    """Answer a hello for an active member with a challenge issued by ``tokens``.

    Bounded like the ``tokens`` challenges: one requester holds at most
    ``tokens.CHALLENGES_PER_DEVICE`` pending handshakes per device (its own
    oldest makes room), and a full table refuses new hellos instead of
    evicting a handshake another requester (the member) is completing.
    """
    from app.network import tokens
    try:
        client_public = _decode_ephemeral(client_ephemeral)
    except crypto.CryptoError as exc:
        raise LanRejected("Invalid key") from exc
    private = x25519.X25519PrivateKey.generate()
    server_ephemeral = crypto.b64u_encode(_x25519_public(private))
    body = transcript(network_id=ctx.network_id, client=device_id, server=ctx.device_id, challenge=challenge,
                      client_ephemeral=client_ephemeral, server_ephemeral=server_ephemeral)
    with _lock:
        now = time.monotonic()
        _purge(now)
        mine = [k for k, p in _pending.items() if p.device_id == device_id and p.requester == requester]
        while len(mine) >= tokens.CHALLENGES_PER_DEVICE:
            _pending.pop(mine.pop(0), None)
        if len(_pending) >= PENDING_MAX:
            raise LanBusy("Too many pending LAN handshakes; retry shortly")
        _pending[challenge] = _Pending(now + tokens.CHALLENGE_TTL_SECONDS, device_id, challenge, private, body,
                                       client_public, requester)
    signature = crypto.sign(ctx.identity.signing_private, _HELLO_DOMAIN + body)
    return {"v": PROTOCOL, "challenge": challenge, "server_ephemeral": server_ephemeral,
            "device_id": ctx.device_id, "network_id": ctx.network_id,
            "signature": crypto.b64u_encode(signature), "expires_in": tokens.CHALLENGE_TTL_SECONDS}


def is_pending(challenge: str) -> bool:
    """Whether a LAN hello answered ``challenge`` (it then buys only that LAN session)."""
    with _lock:
        return challenge in _pending


def server_session(ctx, *, device_id: str, challenge: str, signature: str) -> dict:
    """Verify the client's transcript signature and open a session (or raise ``LanRejected``)."""
    from app.network import tokens
    member = ctx.member(device_id)
    if member is None or member.status != membership.STATUS_ACTIVE:
        raise LanRejected("Device is not an active member of this network")
    with _lock:
        pending = _pending.get(challenge)
    if (pending is None or pending.device_id != device_id or pending.expires <= time.monotonic()
            or tokens._peek_challenge(challenge, device_id) is None):
        raise LanRejected("Challenge expired or unknown")
    # Verify before spending anything: the challenge crossed the LAN in the
    # clear, and a junk signature from whoever saw it must not burn it.
    try:
        ok = crypto.verify(member.signing_public, _SESSION_DOMAIN + pending.transcript, crypto.b64u_decode(signature))
        k_in, k_out = _derive(pending.private, pending.client_public, pending.transcript) if ok else (b"", b"")
    except crypto.CryptoError:
        ok = False
    if not ok:
        raise LanRejected("Session signature is invalid")
    with _lock:
        taken = _pending.pop(challenge, None) is pending
    # Spend the challenge in ``tokens`` too, so it cannot also buy a device token.
    requester = tokens._take_challenge(challenge, device_id) if taken else None
    if requester is None:
        raise LanRejected("Challenge expired or unknown")
    tokens._remember_member_address(device_id, requester)
    session = ServerSession(id=crypto.b64u_encode(secrets.token_bytes(18)), device_id=device_id,
                            network_id=ctx.network_id, generation=ctx.generation, k_in=k_in, k_out=k_out,
                            expires=time.monotonic() + SESSION_TTL_SECONDS)
    with _lock:
        _purge(time.monotonic())
        mine = [k for k, s in _sessions.items() if s.device_id == device_id]
        while len(mine) >= SESSIONS_PER_DEVICE:
            _sessions.pop(mine.pop(0), None)
        while len(_sessions) >= SESSIONS_MAX:
            _sessions.popitem(last=False)
        _sessions[session.id] = session
    return {"v": PROTOCOL, "session": session.id, "expires_in": SESSION_TTL_SECONDS}


def session_known(session_id: str) -> bool:
    """Whether ``session_id`` names a live session here (checked before a call's body is read)."""
    with _lock:
        session = _sessions.get(session_id or "")
    return session is not None and session.expires > time.monotonic()


def server_open_call(ctx, session_id: str, seq: int, body: bytes) -> Tuple[ServerSession, dict]:
    """Decrypt one call; refuses unknown sessions, replays and anything that does not verify."""
    if ctx is None:
        raise LanRejected("This worker has no network")
    with _lock:
        session = _sessions.get(session_id or "")
    if session is None or session.expires <= time.monotonic():
        raise LanRejected("Unknown or expired session")
    member = ctx.member(session.device_id)
    if (session.network_id != ctx.network_id or session.generation != ctx.generation
            or member is None or member.status != membership.STATUS_ACTIVE):
        with _lock:
            _sessions.pop(session.id, None)
        raise LanRejected("Session no longer valid")
    if not isinstance(seq, int) or seq < 1 or seq >= 2 ** 63:
        raise LanRejected("Bad sequence number")
    try:
        clear = AESGCM(session.k_in).decrypt(_nonce(seq), body, _aad(_CALL_AAD, session.id, seq))
    except (InvalidTag, ValueError) as exc:
        raise LanRejected("Call does not verify") from exc
    with _lock:
        if seq <= session.floor or seq in session.seen:
            raise LanRejected("Replayed call")
        session.seen.add(seq)
        if len(session.seen) > REPLAY_WINDOW:
            lowest = min(session.seen)
            session.seen.discard(lowest)
            session.floor = max(session.floor, lowest)
    try:
        call = json.loads(clear)
    except ValueError as exc:
        raise LanRejected("Malformed call") from exc
    if not isinstance(call, dict) or not isinstance(call.get("m"), str) or not isinstance(call.get("p"), str):
        raise LanRejected("Malformed call")
    return session, call


def seal_reply(session: ServerSession, seq: int, status: int, content_type: str, body: bytes) -> bytes:
    header = json.dumps({"status": int(status), "type": content_type}).encode("utf-8")
    clear = struct.pack(">I", len(header)) + header + body
    return AESGCM(session.k_out).encrypt(_nonce(seq), clear, _aad(_REPLY_AAD, session.id, seq))


def drop_sessions(device_id: Optional[str] = None) -> None:
    """Forget server sessions (all, or one device's): the kill switch, a revocation."""
    with _lock:
        if device_id is None:
            _sessions.clear()
            _pending.clear()
        else:
            for key in [k for k, s in _sessions.items() if s.device_id == device_id]:
                _sessions.pop(key, None)


def reset_for_tests() -> None:
    drop_sessions()


# ---------------------------------------------------------------------------
# Client side
# ---------------------------------------------------------------------------

@dataclass
class ClientSession:
    base: str
    id: str
    k_out: bytes
    k_in: bytes
    expires: float
    seq: int = 0

    def usable(self, now: float) -> bool:
        return self.expires - SESSION_RENEW_MARGIN_SECONDS > now


def _timeout(total: float, connect_limit: Optional[float]) -> httpx.Timeout:
    connect = LAN_CONNECT_TIMEOUT if connect_limit is None else min(LAN_CONNECT_TIMEOUT, connect_limit)
    return httpx.Timeout(total, connect=min(total, connect))


async def _post_json(client: httpx.AsyncClient, url: str, body: dict, connect_limit: Optional[float]) -> dict:
    try:
        response = await client.post(url, json=body, headers={"Accept": "application/json"},
                                     timeout=_timeout(LAN_HANDSHAKE_TIMEOUT, connect_limit))
    except httpx.HTTPError as exc:
        raise LanUnavailable(f"{type(exc).__name__}: {exc}"[:200]) from exc
    if response.status_code != 200:
        # 404/405: an older worker, or LAN-direct turned off there.
        raise LanUnavailable(f"HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise LanUnavailable("Not a worker answer") from exc
    if not isinstance(data, dict):
        raise LanUnavailable("Not a worker answer")
    return data


async def handshake(client: httpx.AsyncClient, ctx, peer_device_id: str, base: str, *,
                    connect_limit: Optional[float] = None) -> ClientSession:
    """Open a sealed session with ``peer_device_id`` at ``base``.

    The peer must prove it holds that member's signing key before this
    worker signs anything; ``LanIdentityMismatch`` otherwise.
    """
    member = ctx.member(peer_device_id)
    if member is None or member.status != membership.STATUS_ACTIVE:
        raise LanIdentityMismatch("Peer is not an active member")
    base = base.rstrip("/")
    private = x25519.X25519PrivateKey.generate()
    client_ephemeral = crypto.b64u_encode(_x25519_public(private))
    try:
        async with asyncio.timeout(LAN_HANDSHAKE_TIMEOUT * 2):
            return await _handshake(client, ctx, member, peer_device_id, base, private, client_ephemeral,
                                    connect_limit)
    except TimeoutError as exc:
        raise LanUnavailable("Handshake timed out") from exc


async def _handshake(client, ctx, member, peer_device_id, base, private, client_ephemeral,
                     connect_limit) -> ClientSession:
    hello = await _post_json(client, base + HELLO_PATH, {"v": PROTOCOL, "device_id": ctx.device_id,
                                                         "ephemeral": client_ephemeral}, connect_limit)
    challenge = hello.get("challenge")
    if (hello.get("device_id") != peer_device_id or hello.get("network_id") != ctx.network_id
            or not isinstance(challenge, str) or not isinstance(hello.get("signature"), str)):
        raise LanIdentityMismatch(f"{base} answered for another device or network")
    body = transcript(network_id=ctx.network_id, client=ctx.device_id, server=peer_device_id,
                      challenge=challenge, client_ephemeral=client_ephemeral,
                      server_ephemeral=str(hello.get("server_ephemeral")))
    try:
        server_public = _decode_ephemeral(hello.get("server_ephemeral"))
        ok = crypto.verify(member.signing_public, _HELLO_DOMAIN + body, crypto.b64u_decode(hello["signature"]))
    except crypto.CryptoError:
        ok = False
    if not ok:
        raise LanIdentityMismatch(f"{base} could not prove it is {peer_device_id}")
    try:
        k_out, k_in = _derive(private, server_public, body)
    except (crypto.CryptoError, ValueError) as exc:
        raise LanUnavailable("Key agreement failed") from exc
    signature = crypto.sign(ctx.identity.signing_private, _SESSION_DOMAIN + body)
    opened = await _post_json(client, base + SESSION_PATH, {
        "v": PROTOCOL, "device_id": ctx.device_id, "challenge": challenge,
        "signature": crypto.b64u_encode(signature)}, connect_limit)
    session_id = opened.get("session")
    if not isinstance(session_id, str) or not session_id:
        raise LanUnavailable("No session")
    try:
        ttl = float(opened.get("expires_in") or SESSION_TTL_SECONDS)
    except (TypeError, ValueError):
        ttl = SESSION_TTL_SECONDS
    return ClientSession(base=base, id=session_id, k_out=k_out, k_in=k_in,
                         expires=time.monotonic() + max(0.0, min(ttl, SESSION_TTL_SECONDS)))


async def sealed_call(client: httpx.AsyncClient, session: ClientSession, method: str, path: str, *,
                      json_body: Any = None, params: Optional[Mapping[str, Any]] = None,
                      timeout: float = 15.0, max_bytes: int = REPLY_MAX_BYTES,
                      connect_limit: Optional[float] = None) -> Tuple[int, str, bytes]:
    """One request through the session: ``(status, content type, body)``.

    Raises ``LanSessionRejected`` when the peer refused the session before
    acting on the call, ``LanUnavailable`` when the LAN failed (no answer,
    or an answer that does not verify). Both mean: use the tunnel.
    """
    session.seq += 1
    seq = session.seq
    clear = json.dumps({"m": method.upper(), "p": path,
                        "q": {k: str(v) for k, v in (params or {}).items()}, "b": json_body},
                       separators=(",", ":")).encode("utf-8")
    sealed = AESGCM(session.k_out).encrypt(_nonce(seq), clear, _aad(_CALL_AAD, session.id, seq))
    raw = bytearray()
    limit = max_bytes + 65536
    try:
        async with client.stream("POST", session.base + CALL_PATH, content=sealed,
                                 headers={SESSION_HEADER: session.id, SEQ_HEADER: str(seq),
                                          "Content-Type": "application/octet-stream"},
                                 timeout=_timeout(timeout, connect_limit)) as response:
            if response.status_code == 401:
                raise LanSessionRejected("Session refused")
            if response.status_code != 200:
                raise LanUnavailable(f"HTTP {response.status_code}")
            async for piece in response.aiter_bytes():
                raw.extend(piece)
                if len(raw) > limit:
                    raise LanUnavailable("Reply exceeds the transfer limit")
    except httpx.HTTPError as exc:
        raise LanUnavailable(f"{type(exc).__name__}: {exc}"[:200]) from exc
    try:
        reply = AESGCM(session.k_in).decrypt(_nonce(seq), bytes(raw), _aad(_REPLY_AAD, session.id, seq))
        (length,) = struct.unpack(">I", reply[:4])
        header = json.loads(reply[4:4 + length])
        body = reply[4 + length:]
        status = int(header["status"])
        content_type = str(header.get("type") or "application/json")
    except (InvalidTag, ValueError, KeyError, TypeError, struct.error) as exc:
        raise LanUnavailable("Reply does not verify") from exc
    return status, content_type, body


# ---------------------------------------------------------------------------
# Admission relayed by the account hub
# ---------------------------------------------------------------------------

def _admission_aad(network_id: str, device_id: str) -> bytes:
    return _ADMISSION_INFO + f"{network_id}|{device_id}".encode("utf-8")


def seal_admission_addresses(response: dict, *, device_id: str, encryption_public: bytes) -> dict:
    """Move LAN addresses out of an admission the account hub relays.

    The hub stores and forwards the admission to the new device. LAN
    addresses of members that also have a tunnel (the only ones a joiner
    dials directly; older joiners never dial them) are taken out of the
    plain fields and sealed to the admitted device's encryption key, so only
    that worker can read them. Members without a tunnel keep their address
    in the clear: it is their only way in, as before.
    """
    out = dict(response)
    sealed: Dict[str, List[str]] = {}

    def strip(entry: Mapping[str, Any]) -> dict:
        entry = dict(entry)
        urls = validate_endpoints(entry.get("endpoints") if isinstance(entry.get("endpoints"), list) else [])
        if any(u.startswith("https://") for u in urls):
            local = [u for u in urls if is_lan_candidate(u)]
            if local and isinstance(entry.get("device_id"), str):
                sealed.setdefault(entry["device_id"], [])
                sealed[entry["device_id"]] += [u for u in local if u not in sealed[entry["device_id"]]]
            entry["endpoints"] = [u for u in urls if not is_lan_candidate(u)]
        return entry

    if isinstance(out.get("issuer"), Mapping):
        out["issuer"] = strip(out["issuer"])
    if isinstance(out.get("members"), list):
        out["members"] = [strip(m) if isinstance(m, Mapping) else m for m in out["members"]]
    if sealed:
        key = secrets.token_bytes(32)
        payload = crypto.encrypt_payload(key, crypto.canonical_json(sealed),
                                         _admission_aad(str(out.get("network_id")), device_id))
        out["lan_endpoints"] = {"v": PROTOCOL, "key": crypto.wrap_key(encryption_public, key), "payload": payload}
    return out


def open_admission_addresses(admission: Mapping[str, Any], ctx) -> Dict[str, List[str]]:
    """The sealed LAN addresses of an admission, readable only by the admitted device."""
    sealed = admission.get("lan_endpoints")
    if not isinstance(sealed, Mapping):
        return {}
    try:
        key = crypto.unwrap_key(ctx.identity.encryption_private, sealed["key"])
        clear = crypto.decrypt_payload(key, sealed["payload"], _admission_aad(ctx.network_id, ctx.device_id))
        data = json.loads(clear)
    except (crypto.CryptoError, KeyError, TypeError, ValueError):
        logger.debug("Admission LAN addresses did not open", exc_info=True)
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(device): lan_candidates(urls) for device, urls in data.items() if isinstance(urls, list)}
