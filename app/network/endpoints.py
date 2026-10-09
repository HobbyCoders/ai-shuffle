"""Validation and selection of the addresses members advertise for each other.

A member tells its peers where it can be reached (``endpoints`` on the
token, exchange and presence paths) and every peer later dials those
addresses. The same rule applies where an address is stored and where it is
dialed: plain ``http``/``https`` URLs with a host, no credentials, and no
link-local address. Private LAN addresses are the normal case and stay
allowed.

Two ways to reach a peer
------------------------
*Device token* (``tokens``, ``dial_endpoints``): a worker that advertises an
``https`` address, which is its Shuffle-account tunnel, is authenticated and
dialed there and nowhere else with a device token. The token is a bearer
good for five minutes and gives every ``/peer`` route, so it never travels
over plain http to a worker that has a tunnel. Plain http is dialed this way
only for a worker that advertises no ``https`` address (as before).

*Sealed LAN session* (``lan``, #2169): a plain-http private LAN address the
peer advertises (``lan.lan_candidates``) is tried first, with a short
connect timeout, through a session that needs no TLS and carries no bearer:

- Authentication is mutual and at least as strong as the tunnel's. The peer
  signs the handshake transcript with the Ed25519 key in this worker's
  membership records before this worker signs anything; an address that
  answers as another device or network, or cannot sign, is refused and the
  tunnel used. This worker's own signature over the same transcript is
  checked by the peer against its membership records, exactly like a token
  request, and spends a ``tokens`` challenge. The tunnel path, by contrast,
  identifies the server only by Cloudflare TLS and its JSON ``device_id``.
- Confidentiality and integrity come from the session itself: X25519 with
  fresh keys on both sides, HKDF-SHA256 over the signed transcript, one
  AES-256-GCM key per direction, a sequence-number nonce and a replay
  window. Method, path, query, body and reply are all inside the seal, so
  watermarks, addresses and capabilities (which TLS hides on the tunnel)
  are hidden here too; the handshake itself carries none of them. The data itself is protected end to end on either
  path (sealed, signed ops; snapshot pages and file parts encrypted to the
  recipient).
- Only sync routes are reachable through a session (exchange, ops, rollback
  proof, state snapshot, file parts, ping). ``/peer/control`` and the other
  bearer users stay on the tunnel.

Any failure on the LAN (no answer within ``lan.LAN_CONNECT_TIMEOUT``, an
older worker without the protocol, a session the peer refused, a reply that
does not verify, the owner's kill switch ``network_lan_direct``) moves the
link to the tunnel at once without an error; the LAN is tried again after
``lan.LAN_REPROBE_SECONDS`` or as soon as the peer's addresses change. An
address the peer withdrew stops being dialed: a session at it is dropped.
"""
from __future__ import annotations

import ipaddress
from typing import Iterable, List, Mapping, Optional
from urllib.parse import urlsplit

MAX_ENDPOINTS = 8
MAX_ENDPOINT_LENGTH = 512

# Tailscale hands every node an address from the carrier-grade NAT block,
# which ``ipaddress`` classifies as neither private nor global.
_TAILSCALE_IPV4 = ipaddress.IPv4Network("100.64.0.0/10")


def is_valid_endpoint(url: object) -> bool:
    if not isinstance(url, str) or not url or len(url) > MAX_ENDPOINT_LENGTH:
        return False
    if any(ord(c) < 0x21 or ord(c) == 0x7F for c in url):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return False
    try:
        host = parts.hostname
        parts.port  # raises on a malformed port
    except ValueError:
        return False
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # a name; resolution happens at dial time
    return not embedded_ipv4(address).is_link_local


# IPv6 prefixes whose last 32 bits are an IPv4 destination that a gateway or
# SIT route reaches (the same set browser-mcp's ssrf.ts ``unmap`` classifies).
_IPV4_CARRYING_PREFIXES = (
    ipaddress.IPv6Network("::ffff:0:0/96"),      # IPv4-mapped
    ipaddress.IPv6Network("::ffff:0:0:0/96"),    # IPv4-translated (SIIT)
    ipaddress.IPv6Network("64:ff9b::/96"),       # NAT64 well-known
    ipaddress.IPv6Network("64:ff9b:1::/96"),     # NAT64 local-use, /96 layout
)
_IPV4_COMPATIBLE = ipaddress.IPv6Network("::/96")


def embedded_ipv4(address: ipaddress.IPv4Address | ipaddress.IPv6Address):
    """The IPv4 address an IPv6 form carries, else ``address`` itself.

    ``ipaddress`` classifies ``::ffff:169.254.169.254`` as neither link-local
    nor private, so a rule on the IPv6 object alone lets the IPv4 range it
    names through. ``::`` and ``::1`` stay IPv6 (unspecified and loopback).
    """
    if address.version != 6:
        return address
    embedded = ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
    if any(address in net for net in _IPV4_CARRYING_PREFIXES):
        return embedded
    if address in _IPV4_COMPATIBLE and int(address) > 1:
        return embedded
    return address


def validate_endpoints(endpoints: Iterable[object] | None) -> List[str]:
    """The usable addresses, in the order given, without duplicates, at most ``MAX_ENDPOINTS``."""
    out: List[str] = []
    for url in endpoints or []:
        if not is_valid_endpoint(url):
            continue
        url = url.rstrip("/")
        if url and url not in out:
            out.append(url)
        if len(out) >= MAX_ENDPOINTS:
            break
    return out


def is_secure_endpoint(url: object) -> bool:
    """Whether ``url`` is dialed over TLS (the tunnel or an https reverse proxy)."""
    return isinstance(url, str) and url.lower().startswith("https://")


def dial_endpoints(endpoints: Iterable[object] | None) -> List[str]:
    """The addresses a peer is dialed at, in order.

    The ``https`` addresses when the peer advertises any (its account tunnel,
    or an https reverse proxy in front of it); only then its plain-http ones.
    Order within a group follows the order given, so the first source wins.
    """
    usable = validate_endpoints(endpoints)
    secure = [url for url in usable if is_secure_endpoint(url)]
    return secure or usable


def known_endpoints(member: Optional[Mapping[str, object]], presence: Optional[Mapping[str, object]]) -> List[str]:
    """Every usable address a member is known at, from its stored row and its presence record.

    The stored row holds what the member itself last announced on the token
    and exchange paths (and what the account directory reported); the presence
    record is the replicated announcement, which can lag by one presence
    interval. Both are combined; the LAN addresses among them stay listed.
    """
    combined: List[object] = []
    for source in (member, presence):
        if source:
            urls = source.get("endpoints")
            if isinstance(urls, list):
                combined.extend(urls)
    return validate_endpoints(combined)


def peer_endpoints(member: Optional[Mapping[str, object]], presence: Optional[Mapping[str, object]]) -> List[str]:
    """The addresses to dial a member at with a device token (``dial_endpoints`` of ``known_endpoints``)."""
    return dial_endpoints(known_endpoints(member, presence))


def plain_http_is_local(url: str) -> bool:
    """Whether a plain-http address stays on a path the owner controls.

    A one-time invite code travels to this address in the clear, so plain
    http is accepted only for the same machine, a private or link-local LAN
    address, an mDNS ``.local`` name, or a Tailscale address (a ``.ts.net``
    name or the 100.64.0.0/10 block). An https address is always accepted;
    a name or address that cannot be placed on such a path is not.
    """
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower().rstrip(".")
    except ValueError:
        return False
    if not host:
        return False
    if parts.scheme == "https":
        return True
    if parts.scheme != "http":
        return False
    if host in ("localhost", "localhost.localdomain") or host.endswith(".localhost"):
        return True
    if host.endswith(".local") or host.endswith(".ts.net"):
        return True
    try:
        address = embedded_ipv4(ipaddress.ip_address(host))
    except ValueError:
        return False  # a public or unplaceable name
    if address.version == 4 and address in _TAILSCALE_IPV4:
        return True
    return bool(address.is_loopback or address.is_private or address.is_link_local)
