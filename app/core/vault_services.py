"""Typed consumer for a privately granted HTTPS API credential.

The user enters a service origin and one credential in the Vault card.
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
import bisect
import datetime
import hashlib
import hmac
import ipaddress
import re
import ssl
from dataclasses import dataclass
from functools import partial
from html.entities import html5
from urllib.parse import unquote, urlsplit

import httpx
from cryptography import x509

from app.core.vault_files import LIMIT, AccessDenied

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
REQUEST_TIMEOUT = 30  # Longest silence between reads (connect, write, read, pool).
# Overall bound on one request, headers and body included: the per-read timeout
# alone lets a slow-dripping service hold a request open for the grant's lifetime.
REQUEST_DEADLINE = 60
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+")
_HOST_LABEL = re.compile(r"(?!-)[A-Za-z0-9-]{1,63}(?<!-)")
CONTENT_TYPE = re.compile(
    r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+(; ?charset=[A-Za-z0-9_-]+)?"
)
_REDACTED = b"[redacted]"


def certificate_pin(value: str) -> str:
    """An owner-entered SHA-256 certificate fingerprint, never a model input."""
    value = value.strip().lower()
    if value and not re.fullmatch(r"[0-9a-f]{64}|(?:[0-9a-f]{2}:){31}[0-9a-f]{2}", value):
        raise AccessDenied("Enter the certificate's SHA-256 fingerprint: 64 hex digits, optionally colon-separated.")
    return value.replace(":", "")


def tls_context() -> ssl.SSLContext:
    """Certificate and host-name verification against the system trust store."""
    return ssl.create_default_context()


def pinned_tls_context(pin: str) -> ssl.SSLContext:
    """Replace CA trust with an exact owner-approved certificate for this grant only.

    asyncio and HTTPX/AnyIO use SSLContext.wrap_bio. Check the certificate at
    handshake completion AND before application writes, so authentication cannot
    be sent to a different certificate, even if a consumer skips do_handshake.
    No automatic discovery, TOFU, or fallback to an unverified connection.
    """
    pin = certificate_pin(pin)
    if not pin:
        raise AccessDenied("A pinned connection requires an owner-approved fingerprint.")

    class PinnedCertificate(ssl.SSLObject):
        def _verify_pin(self):
            der = self.getpeercert(binary_form=True)
            if not der or not hmac.compare_digest(hashlib.sha256(der).hexdigest(), pin):
                raise ssl.SSLCertVerificationError("The service certificate does not match the approved fingerprint.")
            cert = x509.load_der_x509_certificate(der)
            now = datetime.datetime.now(datetime.timezone.utc)
            if not cert.not_valid_before_utc <= now <= cert.not_valid_after_utc:
                raise ssl.SSLCertVerificationError("The pinned service certificate is not currently valid.")

        def do_handshake(self):
            super().do_handshake()
            self._verify_pin()

        def write(self, data):
            self._verify_pin()
            return super().write(data)

    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    # The exact certificate is the trust anchor, not a CA or a hostname claim.
    # This context is never used without mandatory pin verification above.
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.sslobject_class = PinnedCertificate
    return context


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
        port = parts.port
    except ValueError:
        port = 0
    if port is None:
        port = 443
    elif not 1 <= port <= 65535:
        # An explicit ":0" is not "the default port"; refuse it like any other bad port.
        raise AccessDenied("Enter a valid service port.")
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


def credential_header(name: str, scheme: str, secret: str, username: str = "") -> tuple[str, str]:
    """The header that carries the credential, validated.

    With a ``username`` the secret is that account's password and the header
    is HTTP Basic (RFC 7617, UTF-8): the broker encodes the pair, so neither
    the password nor the encoded pair is ever typed, stored or shown in a
    form the model could read. Messages never quote a submitted value.
    """
    name = (name or "").strip()
    scheme = (scheme or "").strip()
    username = (username or "").strip()
    if username:
        if name.lower() != "authorization" or scheme.lower() != "basic":
            raise AccessDenied(
                "A username and password sign in with the Authorization header and the Basic scheme."
            )
        if len(username) > 128 or ":" in username or not username.isprintable():
            raise AccessDenied("Enter the service username without a colon or line breaks.")
        if not isinstance(secret, str) or not 8 <= len(secret) <= 8192 or not secret.isprintable():
            raise AccessDenied("Enter a password of 8 to 8,192 characters without line breaks.")
        pair = base64.b64encode(f"{username}:{secret}".encode()).decode("ascii")
        return name, f"Basic {pair}"
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
    """The credential in every form an echo can carry; ``redact`` also reads through escapes."""
    # The credential without its scheme: for a username and password this is
    # the encoded pair, which is not the secret itself.
    credential = header_value.rpartition(" ")[2]
    values = {
        secret,
        header_value,
        credential,
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
    found = {v.encode() for v in values if len(v) >= 6}
    # A password may hold non-ASCII characters; a service can echo them as
    # Latin-1 instead of UTF-8.
    for value in values:
        if len(value) >= 6 and not value.isascii():
            try:
                found.add(value.encode("latin-1"))
            except UnicodeEncodeError:
                pass
    return sorted(found, key=len, reverse=True)


def _redact_header(value: str, needles: list[bytes]) -> str:
    """Redact a response header however the service encoded what it echoes.

    httpx reads header values as UTF-8 when all of them are valid UTF-8 and as
    Latin-1 otherwise, so a non-ASCII password arrives either as its own
    characters or as the Latin-1 reading of its bytes. Scan both.
    """
    try:
        value = redact(value.encode("latin-1"), needles).decode("latin-1")
    except UnicodeEncodeError:
        pass  # Characters outside Latin-1: the value was read as UTF-8.
    return redact(value.encode("utf-8"), needles).decode("utf-8", "replace")


def _character(code: int) -> bytes | None:
    return chr(code).encode() if code < 0xD800 or 0xDFFF < code <= 0x10FFFF else None


def _backslash_escape(match: re.Match[bytes]) -> bytes | None:
    high, low, unit, byte, short = match.groups()
    if short:
        return short
    if byte:
        return bytes([int(byte, 16)])
    if unit:
        return _character(int(unit, 16))
    return _character(0x10000 + ((int(high, 16) - 0xD800) << 10) + int(low, 16) - 0xDC00)


def _entity(match: re.Match[bytes]) -> bytes | None:
    decimal, hexadecimal, name = match.groups()
    if name:
        return _ENTITIES.get(name)
    return _character(int(decimal) if decimal else int(hexadecimal, 16))


# Every named character of HTML (PHP's htmlentities() writes "é" as "&eacute;").
_ENTITIES = {name[:-1].encode(): text.encode() for name, text in html5.items() if name.endswith(";")}
# The ways a service writes the characters of what it echoes, each as (what an
# escape looks like, what it stands for). JSON serializers disagree on which
# characters they escape and on the case of the digits, and JavaScript and
# Python also write "\xHH" and "\'"; URLs and forms percent-encode; pages use
# entities.
_BACKSLASH = (
    re.compile(
        rb"\\(?:u([dD][89abAB][0-9a-fA-F]{2})\\u([dD][c-fC-F][0-9a-fA-F]{2})|u([0-9a-fA-F]{4})"
        rb"|x([0-9a-fA-F]{2})|([\"\\/']))"
    ),
    _backslash_escape,
)
_PERCENT = (re.compile(rb"%([0-9a-fA-F]{2})"), lambda match: bytes([int(match[1], 16)]))
_FORM = (re.compile(rb"%([0-9a-fA-F]{2})|\+"), lambda match: bytes([int(match[1], 16)]) if match[1] else b" ")
_HTML = (
    re.compile(
        rb"&(?:#([0-9]{1,7})|#[xX]([0-9a-fA-F]{1,6})|([A-Za-z][A-Za-z0-9]{0,%d}));"
        % (max(map(len, _ENTITIES)) - 1)
    ),
    _entity,
)
_ESCAPES = (_BACKSLASH, _PERCENT, _FORM, _HTML)
# The bytes every escape starts with, and the length of the longest one.
_STARTS = b"\\%&"
_LONGEST = max(map(len, _ENTITIES)) + 2


class _Decoded:
    """Text with one kind of escape written out, and where each stretch of it came from."""

    def __init__(self, data: bytes, escapes, source: _Decoded | None = None):
        token, decode = escapes
        out, position = bytearray(), 0
        # Stretches of the output that stand for an escape: output start, output
        # end, input start, input end. Everything between them is copied as is.
        self._at: list[int] = []
        self._stretches: list[tuple[int, int, int]] = []
        for match in token.finditer(data):
            written = decode(match)
            if written is None:
                continue
            out += data[position : match.start()]
            self._at.append(len(out))
            out += written
            self._stretches.append((len(out), match.start(), match.end()))
            position = match.end()
        self.changed = bool(self._at)
        self._tail = (len(out), position)
        self.data = bytes(out + data[position:])
        self._source = source

    def _origin(self, index: int, end: bool) -> int:
        """Where output position ``index`` is in the input; an escape counts as a whole."""
        found = bisect.bisect_right(self._at, index - 1 if end else index) - 1
        if found < 0:
            return index
        stop, before, after = self._stretches[found]
        if (index <= stop) if end else (index < stop):
            return after if end else before
        return after + index - stop

    def span(self, start: int, end: int) -> tuple[int, int]:
        """The stretch of the original text that reads as output ``start``..``end``."""
        start, end = self._origin(start, False), self._origin(end, True)
        return self._source.span(start, end) if self._source else (start, end)


def _readings(data: bytes):
    """``data`` with one kind of escape written out, then a second kind on top of it.

    Two layers cover text held inside other text: JSON in a JSON string, a URL
    or a page in JSON, a page that shows a URL.
    """
    for outer in _ESCAPES:
        once = _Decoded(data, outer)
        if not once.changed:
            continue
        yield once
        for inner in _ESCAPES:
            twice = _Decoded(once.data, inner, once)
            if twice.changed:
                yield twice


def _occurrences(data: bytes, needles: list[bytes]):
    """Where each needle is in ``data``. ``bytes.find`` stays linear on any input, which
    an alternation of long needles does not."""
    for needle in needles:
        at = data.find(needle)
        while at != -1:
            yield at, at + len(needle)
            at = data.find(needle, at + len(needle))


def _borders(needle: bytes) -> list[int]:
    """For each prefix of ``needle``, how much of its end is also its start."""
    table, matched = [0] * len(needle), 0
    for index in range(1, len(needle)):
        while matched and needle[index] != needle[matched]:
            matched = table[matched - 1]
        if needle[index] == needle[matched]:
            matched += 1
        table[index] = matched
    return table


def _running(text: bytes, needle: bytes, borders: list[int]) -> int | None:
    """Where ``needle`` starts in ``text`` without ending before ``text`` does.

    The start of the needle may run to the end of ``text`` or to an escape the
    end cut off, which reads as plain text from the byte every escape starts
    with. Two layers can leave two such pieces. Such a byte near the end counts
    by itself too: the escape it starts may be the needle's first character.
    One pass, like ``redact``.
    """
    size = len(text)
    edge = max(0, size - 2 * _LONGEST)
    earliest, matched = None, 0
    for index in range(max(0, edge - len(needle) + 1), size + 1):
        if matched == len(needle):
            matched = borders[matched - 1]  # A whole occurrence: ``redact`` has it.
        if index == size:
            return earliest if not matched else min(index - matched, size if earliest is None else earliest)
        if index >= edge and text[index] in _STARTS:
            earliest = min(index - matched, size if earliest is None else earliest)
        while matched and text[index] != needle[matched]:
            matched = borders[matched - 1]
        if text[index] == needle[matched]:
            matched += 1
    return earliest


def redact(data: bytes, needles: list[bytes], limit: int | None = None, *, cut: bool = False) -> bytes:
    """Replace every whole occurrence that starts before ``limit``; keep at most ``limit`` input bytes.

    An occurrence is the credential as written or behind up to two layers of
    JSON, URL, form or HTML escapes. Every pass is one linear scan: the service
    chooses the response, so nothing here may backtrack. This catches a service
    that repeats what it was sent. One that transforms a credential some other
    way cannot be caught by a scan; the owner chose to send it that credential.

    With ``cut``, ``data`` is the start of a longer response: the result also
    stops where an occurrence was still running when ``data`` ended, or where
    an escape near the end may have been cut off, so the start of a credential
    is never returned and nothing waits for the rest of the response.

    Positions are counted in the input, so a shortened replacement can never pull
    a partially read occurrence at the end of ``data`` back inside the limit.
    """
    limit = len(data) if limit is None else min(limit, len(data))
    if not needles:
        return data[:limit]
    readings = list(_readings(data))
    spans = list(_occurrences(data, needles))
    for reading in readings:
        spans += [reading.span(start, end) for start, end in _occurrences(reading.data, needles)]
    # Where the result stops when an occurrence was still running. A whole
    # occurrence that starts exactly there is still replaced.
    stop = limit
    if cut:
        for needle in needles:
            borders = _borders(needle)
            at = _running(data, needle, borders)
            if at is not None:
                stop = min(stop, at)
            for reading in readings:
                at = _running(reading.data, needle, borders)
                if at is not None:
                    stop = min(stop, reading.span(at, at + 1)[0])
    out, position = bytearray(), 0
    for start, end in sorted(spans):
        if start >= limit or start > stop:
            break
        if start >= position:
            out += data[position:start] + _REDACTED
        position = max(position, end)
    if position < stop:
        out += data[position:stop]
    return bytes(out)


async def verify_origin(origin: Origin, pin: str = "") -> None:
    """Complete a verified TLS handshake before granting. No HTTP or credential is sent."""
    try:
        async with asyncio.timeout(10):
            _, writer = await asyncio.open_connection(
                origin.host, origin.port, ssl=pinned_tls_context(pin) if pin else tls_context(), server_hostname=origin.host
            )
    except ssl.SSLCertVerificationError:
        raise AccessDenied(
            "The service's TLS certificate is not trusted for this address or does not match a valid approved fingerprint. "
            "For a self-signed service, verify its SHA-256 fingerprint independently and approve it in the private card."
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
    pin: str = "",
    also_redact: tuple[str, ...] = (),
) -> dict:
    """Send one request with the credential header. ``also_redact`` names
    earlier credentials (a token replaced by a refresh) kept out of the result
    as well."""
    url = request_url(origin, path)
    headers = {
        "Accept": "*/*",
        "Accept-Encoding": "identity",
        # Unchanged since this was "private access": services may filter on it.
        "User-Agent": "ai-shuffle-private-access",
        header[0]: header[1],
    }
    if body:
        headers["Content-Type"] = content_type or "application/json"
    needles = sorted(
        set(_needles(secret, header[1])).union(*(_needles(old, old) for old in also_redact if old)),
        key=len, reverse=True,
    )
    # Far enough past the limit to read a credential that starts inside it in full.
    keep = LIMIT + max((len(n) for n in needles), default=0)
    try:
        async with (
            asyncio.timeout(REQUEST_DEADLINE),
            httpx.AsyncClient(
                verify=pinned_tls_context(pin) if pin else tls_context(),
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
            f"The service stopped responding for {REQUEST_TIMEOUT} seconds. "
            "A change may already have been applied."
        ) from None
    except TimeoutError:
        # The deadline cancelled the stream; leaving the context managers closed it.
        raise AccessDenied(
            f"The service did not finish responding within {REQUEST_DEADLINE} seconds. "
            "A change may already have been applied."
        ) from None
    except httpx.HTTPError:
        raise AccessDenied(
            "Could not complete a verified HTTPS request to the granted service."
        ) from None
    # Only occurrences starting inside the returned window matter. One written
    # as sent was read in full (``keep`` adds the longest); one behind escapes
    # may still be running where reading stopped, and the body is cut there.
    # Off the event loop: the service chooses how much of the response is escapes.
    data = await asyncio.to_thread(partial(redact, bytes(kept), needles, LIMIT, cut=truncated))
    truncated = truncated or len(kept) > LIMIT
    return {
        "status": status,
        "headers": {name: _redact_header(value, needles) for name, value in returned.items()},
        "body": data.decode("utf-8", errors="replace"),
        "truncated": truncated,
    }
