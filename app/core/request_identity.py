"""Shared client attribution for authentication and security audit records."""
import functools
import ipaddress

from fastapi import Request
from app.core.config import settings


# Loopback is always a trusted proxy (a reverse proxy on the same host). Any
# other proxy must be named in TRUSTED_PROXIES: trusting every private address
# let any LAN host pick its own rate-limit key and audit address.
_LOOPBACK_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


@functools.lru_cache(maxsize=8)
def _trusted_networks(configured: str) -> tuple:
    networks = list(_LOOPBACK_NETWORKS)
    for entry in configured.split(","):
        entry = entry.strip()
        if not entry:
            continue
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue  # A typo must not widen trust; it simply trusts nothing.
    return tuple(networks)


def _is_trusted_proxy(host: str) -> bool:
    """Whether a direct peer is loopback or a configured TRUSTED_PROXIES address."""
    try:
        addr = ipaddress.ip_address(host)
    except (ValueError, TypeError):
        return False
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return any(addr in net for net in _trusted_networks(settings.trusted_proxies or ""))


def get_client_ip(request: Request) -> str:
    """The client address, read from proxy headers only behind a trusted proxy.

    X-Forwarded-For is walked from the right (the entry the nearest proxy
    appended) past every trusted proxy, so a client cannot choose its address
    by sending its own header through the proxy.
    """
    direct_ip = request.client.host if request.client else "unknown"
    if direct_ip == "unknown" or not _is_trusted_proxy(direct_ip):
        return direct_ip
    for header in settings.trusted_proxy_headers.split(","):
        value = request.headers.get(header.strip())
        if not value:
            continue
        hops = [hop.strip() for hop in value.split(",") if hop.strip()]
        if not hops:
            continue
        for hop in reversed(hops):
            if not _is_trusted_proxy(hop):
                return hop
        return hops[0]
    return direct_ip
