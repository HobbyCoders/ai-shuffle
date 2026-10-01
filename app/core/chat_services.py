"""Typed consumer for a privately granted HTTPS API credential.

The user enters a service origin and one credential in the private access card.
The model only names a granted handle, a method and a path under that origin;
the broker attaches the credential as a request header. The secret never enters
tool inputs, tool results, the transcript or logs.

Boundaries enforced here:

* HTTPS only, verified against the trust store for the granted host name. A
  model cannot choose the host, port, scheme or credential header.
* Requests stay under the granted path prefix. Dot segments, encoded slashes,
  backslashes and empty segments are refused rather than normalised.
* No redirects are followed, no worker proxy/environment settings, cookies or
  netrc are inherited, and only a small set of response headers is returned.
* Known encodings of the secret are redacted from returned bodies and headers.
  This is defence in depth: a service that transforms and echoes the
  credential in another form could still disclose it, so only grant a
  credential to a service you trust with it.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import ssl
from dataclasses import dataclass
from urllib.parse import quote, unquote, urlsplit

import httpx

from app.core.chat_files import LIMIT, AccessDenied

READ_METHODS = frozenset({"GET", "HEAD"})
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
RESPONSE_HEADERS = (
    "content-type",
    "content-length",
    "location",
    "retry-after",
    "etag",
    "last-modified",
)
# Header names the credential may not replace: framing, routing and hop-by-hop.
RESERVED_HEADERS = frozenset(
    {
        "host",
        "content-length",
        "content-type",
        "transfer-encoding",
        "connection",
        "keep-alive",
        "upgrade",
        "te",
        "trailer",
        "proxy-authorization",
        "proxy-connection",
        "expect",
        "accept",
        "accept-encoding",
        "user-agent",
    }
)
REQUEST_TIMEOUT = 30
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HOST_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)")
CONTENT_TYPE = re.compile(
    r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+(; ?charset=[A-Za-z0-9_-]+)?"
)
_REDACTED = b"[redacted]"


def tls_context() -> ssl.SSLContext:
    """Certificate and host-name verification against the system trust store."""
    return ssl.create_default_context()


@dataclass(frozen=True)
class Origin:
    url: str  # https://host[:port][/prefix], no trailing slash
    host: str
    port: int
    prefix: str  # "" or "/a/b"


def _segments(path: str) -> list[str]:
    segments = path.split("/")
    for segment in segments:
        decoded = unquote(segment)
        # Servlet containers (Tomcat, Jetty, Spring) drop ";params" before
        # normalising, so "..;x" climbs out of the prefix like "..".
        bare = decoded.split(";", 1)[0]
        if (
            not segment
            or bare in ("", ".", "..")
            or "/" in decoded
            or "\\" in decoded
            or "\x00" in decoded
        ):
            raise AccessDenied(
                "Use a path without empty, dot or encoded-slash segments."
            )
    return segments


def _printable(value: str) -> bool:
    return all(0x21 <= ord(ch) <= 0x7E for ch in value)


def parse_origin(url: str) -> Origin:
    if not isinstance(url, str) or not url or len(url) > 2048 or not _printable(url):
        raise AccessDenied("Enter the service as an https:// address.")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise AccessDenied("Only HTTPS services can receive a private credential.")
    if "@" in parts.netloc or parts.query or parts.fragment or "\\" in url:
        raise AccessDenied(
            "Enter the service address without credentials, query or fragment."
        )
    try:
        port = parts.port or 443
    except ValueError:
        raise AccessDenied("Enter a valid service port.") from None
    host = (parts.hostname or "").lower()
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
        labels = host.rstrip(".").split(".")
        if (
            not host
            or len(host) > 253
            or not all(_HOST_LABEL.fullmatch(label) for label in labels)
        ):
            raise AccessDenied(
                "Enter a valid service host name or IP address."
            ) from None
    if address is not None and (
        address.is_unspecified or address.is_multicast or address.is_link_local
    ):
        raise AccessDenied("Use a unicast IP address for this service.")
    prefix = parts.path.rstrip("/")
    if prefix:
        _segments(prefix[1:])
    netloc = f"[{host}]" if address is not None and address.version == 6 else host
    if port != 443:
        netloc = f"{netloc}:{port}"
    return Origin(f"https://{netloc}{prefix}", host, port, prefix)


def credential_header(name: str, scheme: str, secret: str) -> tuple[str, str]:
    name = (name or "").strip()
    scheme = (scheme or "").strip()
    if not _TOKEN.fullmatch(name) or name.lower() in RESERVED_HEADERS:
        raise AccessDenied(
            "Choose a credential header such as Authorization or X-API-Key."
        )
    if scheme and not _TOKEN.fullmatch(scheme):
        raise AccessDenied(
            "The credential scheme must be a single word such as Bearer."
        )
    if (
        not isinstance(secret, str)
        or len(secret) < 8
        or len(secret) > 8192
        or not _printable(secret)
    ):
        raise AccessDenied(
            "Enter a credential of 8 to 8,192 visible characters without spaces."
        )
    return name, f"{scheme} {secret}" if scheme else secret


def request_url(origin: Origin, path: str) -> str:
    """The absolute URL for a model-supplied ``/path?query`` under ``origin``."""
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > 4096
        or not _printable(path)
        or "#" in path
        or "\\" in path
    ):
        raise AccessDenied(
            "Provide a path starting with / and without spaces, fragments or backslashes."
        )
    resource, _, query = path.partition("?")
    if "?" in query:
        raise AccessDenied("Use a single query string.")
    trailing = resource != "/" and resource.endswith("/")
    segments = (
        _segments(resource[1:-1] if trailing else resource[1:])
        if resource != "/"
        else []
    )
    url = (
        origin.url
        + "/"
        + "/".join(segments)
        + ("/" if trailing else "")
        + (f"?{query}" if query else "")
    )
    try:
        parsed = httpx.URL(url)
    except httpx.InvalidURL:
        raise AccessDenied(
            "Provide a valid path under the granted service address."
        ) from None
    raw = parsed.raw_path.decode("ascii").partition("?")[0]
    # Defence in depth: whatever the parser makes of it, the target must not move.
    if (
        parsed.scheme != "https"
        or parsed.host != origin.host
        or (parsed.port or 443) != origin.port
        or not raw.startswith(origin.prefix + "/")
    ):
        raise AccessDenied("The request must stay under the granted service address.")
    return url


def _needles(secret: str, header_value: str) -> list[bytes]:
    values = {
        secret,
        header_value,
        quote(secret, safe=""),
        json.dumps(secret)[1:-1],
        json.dumps(secret)[1:-1].replace("/", "\\/"),
        base64.b64encode(secret.encode()).decode(),
        base64.b64encode(header_value.encode()).decode(),
    }
    try:
        decoded = base64.b64decode(secret, validate=True).decode("utf-8")
        if len(decoded) >= 6 and decoded.isprintable():
            values.add(decoded)
            if ":" in decoded and len(decoded.split(":", 1)[1]) >= 6:
                values.add(decoded.split(":", 1)[1])  # HTTP Basic password
    except (ValueError, UnicodeDecodeError):
        pass
    return sorted({v.encode() for v in values if len(v) >= 6}, key=len, reverse=True)


def redact(data: bytes, needles: list[bytes], limit: int | None = None) -> bytes:
    """Replace every whole occurrence that starts before ``limit``; keep at most ``limit`` input bytes.

    Positions are counted in the input, so a shortened replacement can never pull
    a partially read occurrence at the end of ``data`` back inside the limit.
    """
    limit = len(data) if limit is None else min(limit, len(data))
    if not needles:
        return data[:limit]
    pattern = re.compile(b"|".join(re.escape(needle) for needle in needles))
    out, position = bytearray(), 0
    for match in pattern.finditer(data):
        if match.start() >= limit:
            break
        out += data[position : match.start()] + _REDACTED
        position = match.end()
    if position < limit:
        out += data[position:limit]
    return bytes(out)


async def verify_origin(origin: Origin) -> None:
    """Complete a verified TLS handshake before granting. No HTTP or credential is sent."""
    try:
        async with asyncio.timeout(10):
            _, writer = await asyncio.open_connection(
                origin.host, origin.port, ssl=tls_context(), server_hostname=origin.host
            )
    except ssl.SSLCertVerificationError:
        raise AccessDenied(
            "The service's TLS certificate is not trusted for this address. "
            "Self-signed certificates are not supported."
        ) from None
    except TimeoutError:
        raise AccessDenied(
            "The service did not answer within 10 seconds. Check the address and port."
        ) from None
    except (OSError, ssl.SSLError):
        raise AccessDenied(
            "Could not open a verified HTTPS connection to this service."
        ) from None
    writer.close()
    try:
        await writer.wait_closed()
    except (OSError, ssl.SSLError):
        pass


async def perform(
    origin: Origin,
    header: tuple[str, str],
    secret: str,
    method: str,
    path: str,
    body: str,
    content_type: str,
) -> dict:
    url = request_url(origin, path)
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        "User-Agent": "ai-shuffle-private-access",
        header[0]: header[1],
    }
    if body:
        headers["Content-Type"] = content_type or "application/json"
    needles = _needles(secret, header[1])
    keep = LIMIT + max((len(n) for n in needles), default=0)
    try:
        async with (
            httpx.AsyncClient(
                verify=tls_context(),
                trust_env=False,
                follow_redirects=False,
                timeout=REQUEST_TIMEOUT,
            ) as client,
            client.stream(
                method,
                url,
                headers=headers,
                content=body.encode("utf-8") if body else None,
            ) as response,
        ):
            kept = bytearray()
            truncated = False
            async for chunk in response.aiter_bytes():
                room = max(0, keep - len(kept))
                kept.extend(chunk[:room])
                if len(chunk) > room:
                    truncated = True
                    break
            status = response.status_code
            returned = {
                name: response.headers[name]
                for name in RESPONSE_HEADERS
                if name in response.headers
            }
    except httpx.TimeoutException:
        raise AccessDenied(
            f"The service did not respond within {REQUEST_TIMEOUT} seconds. "
            "A change may already have been applied."
        ) from None
    except httpx.HTTPError:
        raise AccessDenied(
            "Could not complete a verified HTTPS request to the granted service."
        ) from None
    # Only occurrences starting inside the returned window matter, and every
    # such occurrence was read in full (``keep`` adds the longest needle).
    truncated = truncated or len(kept) > LIMIT
    data = redact(bytes(kept), needles, LIMIT)
    return {
        "status": status,
        "headers": {
            name: redact(value.encode("latin-1", "replace"), needles).decode("latin-1")
            for name, value in returned.items()
        },
        "body": data.decode("utf-8", errors="replace"),
        "truncated": truncated,
    }
