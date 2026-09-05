"""Shared client attribution for authentication and security audit records."""
import ipaddress

from fastapi import Request
from app.core.config import settings


# Private/loopback networks whose direct connections we trust as reverse proxies
_TRUSTED_PROXY_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("fc00::/7"),
)


def _is_trusted_proxy(host: str) -> bool:
    """Check if the direct client IP belongs to a trusted proxy (private/loopback)."""
    try:
        addr = ipaddress.ip_address(host)
        return any(addr in net for net in _TRUSTED_PROXY_NETWORKS)
    except (ValueError, TypeError):
        return False


def get_client_ip(request: Request) -> str:
    """Get the real client IP address, respecting reverse proxy headers
    only when the direct connection comes from a trusted proxy."""
    direct_ip = request.client.host if request.client else "unknown"

    # Only trust proxy headers when the immediate connection is from a
    # private/loopback address (i.e., a known reverse proxy).
    if direct_ip != "unknown" and _is_trusted_proxy(direct_ip):
        trusted_headers = settings.trusted_proxy_headers.split(",")
        for header in trusted_headers:
            header = header.strip()
            value = request.headers.get(header)
            if value:
                # X-Forwarded-For can contain multiple IPs, take the first (original client)
                if "," in value:
                    return value.split(",")[0].strip()
                return value.strip()

    return direct_ip


