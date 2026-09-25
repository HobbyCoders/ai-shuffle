"""Validation of the addresses members advertise for each other.

A member tells its peers where it can be reached (``endpoints`` on the
token, exchange and presence paths) and every peer later POSTs to those
addresses with a device token. The same rule applies where an address is
stored and where it is dialed: plain ``http``/``https`` URLs with a host, no
credentials, and no link-local address. Private LAN addresses are the normal
case and stay allowed.
"""
from __future__ import annotations

import ipaddress
from typing import Iterable, List
from urllib.parse import urlsplit

MAX_ENDPOINTS = 8
MAX_ENDPOINT_LENGTH = 512


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
