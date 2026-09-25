"""Edge cases for client attribution behind proxies (app.core.request_identity).

``get_client_ip`` keys the pairing and network-join rate limiters and the
audit log, so a client that can pick or vary its address defeats both.
"""

from __future__ import annotations

import pytest
from starlette.requests import Request

from app.core import request_identity
from app.core.config import settings
from app.core.request_identity import _hop_address, _is_trusted_proxy, get_client_ip


def _request(peer, headers=None):
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
        "client": (peer, 1234) if peer is not None else None,
    }
    return Request(scope)


@pytest.fixture(autouse=True)
def _proxy_settings(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "")
    monkeypatch.setattr(settings, "trusted_proxy_headers", "X-Forwarded-For,X-Real-IP")
    request_identity._trusted_networks.cache_clear()
    yield
    request_identity._trusted_networks.cache_clear()


# ── TRUSTED_PROXIES parsing ──────────────────────────────────────────────────


def test_blank_and_whitespace_entries_are_ignored(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", " , ,\t,10.0.0.1 ,  ")
    assert _is_trusted_proxy("10.0.0.1")
    assert not _is_trusted_proxy("10.0.0.2")


def test_ipv4_and_ipv6_cidrs(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "10.1.0.0/16,2001:db8::/32")
    assert _is_trusted_proxy("10.1.255.3")
    assert not _is_trusted_proxy("10.2.0.1")
    assert _is_trusted_proxy("2001:db8:1::5")
    assert not _is_trusted_proxy("2001:db9::5")


def test_host_bits_set_cidr_is_accepted_non_strict(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "10.1.2.3/24")
    assert _is_trusted_proxy("10.1.2.200")


@pytest.mark.parametrize(
    "entry",
    ["proxy.internal", "10.0.0.0/33", "::1/129", "10.0.0.300", "1.2.3.4/abc", "\x00", "*"],
)
def test_malformed_entry_trusts_nothing_and_never_raises(monkeypatch, entry):
    monkeypatch.setattr(settings, "trusted_proxies", f"{entry},10.9.9.9")
    # The typo trusts nothing; the valid entry beside it still counts.
    assert _is_trusted_proxy("10.9.9.9")
    assert not _is_trusted_proxy("10.0.0.1")
    assert not _is_trusted_proxy("8.8.8.8")
    req = _request("10.0.0.1", {"X-Forwarded-For": "1.1.1.1"})
    assert get_client_ip(req) == "10.0.0.1"


def test_loopback_always_trusted_even_with_empty_config():
    assert _is_trusted_proxy("127.0.0.1")
    assert _is_trusted_proxy("127.8.9.10")
    assert _is_trusted_proxy("::1")
    assert not _is_trusted_proxy("10.0.0.1")


@pytest.mark.parametrize("host", [None, "", "unknown", "testclient", "1.2.3.4:80"])
def test_non_ip_peer_is_never_a_trusted_proxy(host):
    assert _is_trusted_proxy(host) is False


def test_ipv4_mapped_ipv6_proxy_matches_ipv4_entry(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "10.0.0.0/8")
    assert _is_trusted_proxy("::ffff:10.1.2.3")
    assert _is_trusted_proxy("::ffff:127.0.0.1")  # mapped loopback
    assert not _is_trusted_proxy("::ffff:11.1.2.3")


def test_ipv4_mapped_entry_in_config_matches_the_mapped_peer(monkeypatch):
    # Before the fix the peer was folded to IPv4 and compared only against
    # IPv4 networks, so an operator's IPv6-mapped entry never matched.
    monkeypatch.setattr(settings, "trusted_proxies", "::ffff:10.0.0.0/104")
    assert _is_trusted_proxy("::ffff:10.1.2.3")


# ── X-Forwarded-For entries ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "hop, expected",
    [
        ("1.2.3.4", "1.2.3.4"),
        ("1.2.3.4:5678", "1.2.3.4"),
        ("[::1]:80", "::1"),
        ("[2001:db8::1]", "2001:db8::1"),
        ("2001:db8::1", "2001:db8::1"),  # a bare IPv6 address is not host:port
        ("::1", "::1"),
        ("1.2.3.4:", "1.2.3.4:"),
        ("1.2.3.4:http", "1.2.3.4:http"),
        ("1.2.3.4:\u00b2", "1.2.3.4:\u00b2"),  # non-ASCII digit is not a port
        ("[::1", "[::1"),
        ("[]:80", "[]:80"),
        ("[::1]x", "[::1]x"),
        ("unknown", "unknown"),
        ("_hidden", "_hidden"),
    ],
)
def test_hop_address_strips_only_a_port(hop, expected):
    assert _hop_address(hop) == expected


def test_port_suffix_does_not_vary_the_client_key():
    # A proxy that appends ip:port must not give each connection its own
    # rate-limit key.
    a = get_client_ip(_request("127.0.0.1", {"X-Forwarded-For": "203.0.113.9:50001"}))
    b = get_client_ip(_request("127.0.0.1", {"X-Forwarded-For": "203.0.113.9:50002"}))
    assert a == b == "203.0.113.9"


def test_bracketed_ipv6_hop_with_port():
    req = _request("::1", {"X-Forwarded-For": "[2001:db8::7]:443"})
    assert get_client_ip(req) == "2001:db8::7"


def test_trusted_proxy_hop_with_port_is_skipped(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "10.0.0.0/8")
    req = _request("10.0.0.2", {"X-Forwarded-For": "198.51.100.4, 10.0.0.5:8443"})
    assert get_client_ip(req) == "198.51.100.4"


# ── resolution ───────────────────────────────────────────────────────────────


def test_untrusted_peer_ignores_every_header():
    req = _request("8.8.8.8", {"X-Forwarded-For": "1.1.1.1", "X-Real-IP": "2.2.2.2"})
    assert get_client_ip(req) == "8.8.8.8"


def test_no_client_is_unknown():
    assert get_client_ip(_request(None, {"X-Forwarded-For": "1.1.1.1"})) == "unknown"


def test_forged_leftmost_entry_is_ignored_right_to_left():
    req = _request("127.0.0.1", {"X-Forwarded-For": "6.6.6.6, 203.0.113.9"})
    assert get_client_ip(req) == "203.0.113.9"


def test_loopback_peer_with_forged_xff_takes_the_header():
    # Loopback is a trusted proxy (a reverse proxy on the same host), so a
    # process on the host can name any address. That is the documented model.
    req = _request("127.0.0.1", {"X-Forwarded-For": "8.8.4.4"})
    assert get_client_ip(req) == "8.8.4.4"


def test_every_hop_trusted_returns_the_first_hop(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "10.0.0.0/8")
    req = _request("10.0.0.1", {"X-Forwarded-For": "10.0.0.9, 10.0.0.3, 10.0.0.2"})
    assert get_client_ip(req) == "10.0.0.9"


def test_empty_and_comma_only_xff_falls_through_to_real_ip():
    req = _request("127.0.0.1", {"X-Forwarded-For": " , ,", "X-Real-IP": "203.0.113.1"})
    assert get_client_ip(req) == "203.0.113.1"


def test_xff_wins_over_x_real_ip_in_default_order():
    req = _request("127.0.0.1", {"X-Forwarded-For": "203.0.113.1", "X-Real-IP": "203.0.113.2"})
    assert get_client_ip(req) == "203.0.113.1"


def test_header_order_follows_the_setting(monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxy_headers", " X-Real-IP , X-Forwarded-For")
    req = _request("127.0.0.1", {"X-Forwarded-For": "203.0.113.1", "X-Real-IP": "203.0.113.2"})
    assert get_client_ip(req) == "203.0.113.2"


@pytest.mark.parametrize("headers_setting", ["", None, ",,"])
def test_blank_header_setting_uses_the_peer(monkeypatch, headers_setting):
    monkeypatch.setattr(settings, "trusted_proxy_headers", headers_setting)
    req = _request("127.0.0.1", {"X-Forwarded-For": "203.0.113.1"})
    assert get_client_ip(req) == "127.0.0.1"


def test_garbage_hop_is_returned_not_raised():
    req = _request("127.0.0.1", {"X-Forwarded-For": "not-an-ip"})
    assert get_client_ip(req) == "not-an-ip"
