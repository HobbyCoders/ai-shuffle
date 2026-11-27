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
import json
import re
import ssl
from dataclasses import dataclass
from functools import partial
from html.entities import html5
from urllib.parse import quote, unquote, urlsplit

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
_NOT_TEXT = b"[The response is not text, so its body is not shown.]"


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


def _ascii(value: str) -> bool:
    """Visible ASCII characters and spaces."""
    return all(0x20 <= ord(ch) <= 0x7E for ch in value)


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
    is HTTP Basic (RFC 7617): the broker encodes the pair, so neither the
    password nor the encoded pair is ever typed, stored or shown in a form the
    model could read. Messages never quote a submitted value.

    Both are ASCII. Servers disagree on the charset of other characters in
    Basic credentials, and an ASCII character is the same byte in every
    charset and the same number in every escape, which is what lets ``redact``
    find an echoed credential however the service wrote it.
    """
    name = (name or "").strip()
    scheme = (scheme or "").strip()
    username = (username or "").strip()
    if username:
        if name.lower() != "authorization" or scheme.lower() != "basic":
            raise AccessDenied(
                "A username and password sign in with the Authorization header and the Basic scheme."
            )
        if not _basic_username(username):
            raise AccessDenied(
                "Enter the service username without a colon, using unaccented letters, digits and symbols."
            )
        if not _basic_password(secret):
            raise AccessDenied(
                "Enter a password of 8 to 8,192 characters, using unaccented letters, digits, symbols and spaces."
            )
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
    if scheme.lower() == "basic" and not _encoded_pair(secret):
        # A pair the owner encoded themselves is held to the rules above.
        raise AccessDenied(
            "A Basic credential is an encoded username and password: unaccented letters, digits and "
            "symbols, and a password of at least 8 characters. Choose Username and password to enter them."
        )
    return name, f"{scheme} {secret}" if scheme else secret


def _basic_username(value: str) -> bool:
    return len(value) <= 128 and ":" not in value and _ascii(value)


def _basic_password(value: object) -> bool:
    return isinstance(value, str) and 8 <= len(value) <= 8192 and _ascii(value)


def _encoded_pair(credential: str) -> bool:
    """Whether a Basic credential is the base64 form of a username and password that could be typed."""
    try:
        username, colon, password = base64.b64decode(credential, validate=True).decode("ascii").partition(":")
    except ValueError:
        return False
    return bool(colon) and _basic_username(username) and _basic_password(password)


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


def _needles(secret: str, header_value: str) -> list[str]:
    """The credential in every form an echo can carry; ``redact`` also reads through escapes.

    Credentials are ASCII (``credential_header``), and so is what is inside a
    Basic pair the owner encoded themselves. Only a credential of another
    scheme that happens to be base64 may decode to other text: that is matched
    as its UTF-8 bytes.
    """
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
    # Unpadded and URL-safe base64 (JWTs, URL parameters) of the credential.
    for encoded in (base64.b64encode(secret.encode()), base64.urlsafe_b64encode(secret.encode())):
        values.update((encoded.decode(), encoded.decode().rstrip("=")))
    try:
        decoded = base64.b64decode(secret, validate=True).decode("utf-8")
        if len(decoded) >= 6 and decoded.isprintable():
            values.add(decoded)
            if ":" in decoded and len(decoded.split(":", 1)[1]) >= 6:
                values.add(decoded.split(":", 1)[1])  # HTTP Basic password
    except (ValueError, UnicodeDecodeError):
        pass
    # ``redact`` reads one character per byte.
    return sorted((value.encode().decode("latin-1") for value in values if len(value) >= 6), key=len, reverse=True)


# A response is scanned as characters, one per byte. An escape that holds a
# number stands for the character with that number. A credential is ASCII, and
# an ASCII character is one byte in every charset a service answers in and the
# same number whether the writer of "\x2b" meant a character or a byte.


def _character(code: int) -> str | None:
    return chr(code) if code < 0xD800 or 0xDFFF < code <= 0x10FFFF else None


def _numbered(match: re.Match[str]) -> str | None:
    """What a backslash escape stands for: groups are a UTF-16 pair, then hexadecimal
    forms, then an octal one, then a character that the mark only protects."""
    high, low, *hexadecimal, octal, protected = match.groups()
    if protected:
        return protected
    if high:
        return _character(0x10000 + ((int(high, 16) - 0xD800) << 10) + int(low, 16) - 0xDC00)
    if octal:
        return chr(int(octal, 8))
    return _character(int(next(filter(None, hexadecimal)), 16))


def _percent(match: re.Match[str]) -> str | None:
    high, low, unit, byte = match.groups()
    if match[0] == "+":
        return " "
    if high:
        return _character(0x10000 + ((int(high, 16) - 0xD800) << 10) + int(low, 16) - 0xDC00)
    return _character(int(unit or byte, 16))


def _entity(match: re.Match[str]) -> str | None:
    decimal, hexadecimal, name = match.groups()
    if name:
        return _ENTITIES.get(name)
    return _character(int(decimal) if decimal else int(hexadecimal, 16))


# Every named character of HTML ("&quot;", "&plus;", "&sol;").
_ENTITIES = {name[:-1]: text for name, text in html5.items() if name.endswith(";")}


def _pair(mark: str) -> str:
    """A character above U+FFFF written as its two UTF-16 halves, each after ``mark``."""
    return mark + "([dD][89abAB][0-9a-fA-F]{2})" + mark + "([dD][c-fC-F][0-9a-fA-F]{2})"


# The ways a service writes the characters of what it echoes, each as (what an
# escape looks like, what it stands for).
# Backslash escapes of JSON and of the languages services are written in:
# "+" in either case and as a UTF-16 pair, "\U0000002b", "\u{2b}",
# "\x{2b}", "\x2b", "\053", and a symbol or a space the mark only protects
# ("\/" in JSON, "\+" from a pattern escaper, "\ " from a shell).
_BACKSLASH = (
    re.compile(
        _pair(r"\\u") + r"|\\(?:u([0-9a-fA-F]{4})|U([0-9a-fA-F]{8})|[ux]\{([0-9a-fA-F]{1,6})\}"
        r"|x([0-9a-fA-F]{2})|([0-3][0-7]{2})|([ -/:-@\[-`{-~]))"
    ),
    _numbered,
)
# URLs: "%2B", and "%u002B" from JavaScript's escape(). Forms also write a space as "+".
_PERCENT = (re.compile(_pair("%u") + "|%(?:u([0-9a-fA-F]{4})|([0-9a-fA-F]{2}))"), _percent)
_FORM = (re.compile(_PERCENT[0].pattern + r"|\+"), _percent)
# Pages: "&#43;", "&#x2B;" and "&plus;".
_HTML = (re.compile("&(?:#([0-9]{1,7})|#[xX]([0-9a-fA-F]{1,6})|([A-Za-z][A-Za-z0-9]*));"), _entity)
_ESCAPES = (_BACKSLASH, _PERCENT, _FORM, _HTML)
# What an escape starts with, and the length of the longest one.
_STARTS = frozenset("\\%&")
_LONGEST = max(map(len, _ENTITIES)) + 2


class _Decoded:
    """Text with one kind of escape written out, and where each stretch of it came from."""

    def __init__(self, text: str, escapes, source: _Decoded | None = None):
        token, decode = escapes
        out, length, position = [], 0, 0
        # Stretches of the output that stand for an escape: where each starts,
        # and (output end, input start, input end). Everything between them is
        # copied as is.
        self._at: list[int] = []
        self._stretches: list[tuple[int, int, int]] = []
        for match in token.finditer(text):
            written = decode(match)
            if written is None:
                continue
            length += match.start() - position
            self._at.append(length)
            length += len(written)
            out += (text[position : match.start()], written)
            self._stretches.append((length, match.start(), match.end()))
            position = match.end()
        self.changed = bool(self._at)
        out.append(text[position:])
        self.text = "".join(out)
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


def _readings(text: str) -> list[_Decoded]:
    """``text`` with one kind of escape written out, and with a second kind on top of it.

    Two layers cover text held inside other text: JSON in a JSON string, a URL
    or a page in JSON, a page that shows a URL.
    """
    readings: list[_Decoded] = []
    for outer in _ESCAPES:
        once = _Decoded(text, outer)
        if not once.changed:
            continue
        readings.append(once)
        for inner in _ESCAPES:
            twice = _Decoded(once.text, inner, once)
            if twice.changed:
                readings.append(twice)
    return readings


def _occurrences(text: str, needles: list[str]):
    """Where each needle is in ``text``. ``str.find`` stays linear on any input, which
    an alternation of long needles does not."""
    for needle in needles:
        at = text.find(needle)
        while at != -1:
            yield at, at + len(needle)
            at = text.find(needle, at + len(needle))


def _borders(needle: str) -> list[int]:
    """For each prefix of ``needle``, how much of its end is also its start."""
    table, matched = [0] * len(needle), 0
    for index in range(1, len(needle)):
        while matched and needle[index] != needle[matched]:
            matched = table[matched - 1]
        if needle[index] == needle[matched]:
            matched += 1
        table[index] = matched
    return table


def _running(text: str, needle: str, borders: list[int]) -> int | None:
    """Where ``needle`` starts in ``text`` without ending before ``text`` does.

    The start of the needle may run to the end of ``text`` or to an escape the
    end cut off, which reads as plain text from the character every escape
    starts with. Two layers can leave two such pieces. Such a character near
    the end counts by itself too: the escape it starts may be the needle's
    first character. One pass, like ``redact``.
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


def redact(data: bytes, needles: list[str], limit: int | None = None, *, cut: bool = False) -> bytes:
    """Replace every whole occurrence that starts before ``limit``; keep at most ``limit`` input bytes.

    An occurrence is the credential as written or behind up to two layers of
    the escapes above: those of JSON and the languages services are written
    in, of URLs and forms, and of pages. A credential is ASCII, so the charset
    of the response does not change how it is written; a body in UTF-16 or
    UTF-32 is not returned at all. Every pass is one linear scan: the service
    chooses the response, so nothing here may backtrack. This catches a
    service that repeats what it was sent. One that transforms a credential
    some other way (a hash, a third layer of escapes) cannot be caught by a
    scan; the owner chose to send it that credential.

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
    # One character per byte, so a position in the text is a position in ``data``.
    text = data.decode("latin-1")
    readings = _readings(text)
    spans = list(_occurrences(text, needles))
    for reading in readings:
        spans += [reading.span(start, end) for start, end in _occurrences(reading.text, needles)]
    # Where the result stops when an occurrence was still running. A whole
    # occurrence that starts exactly there is still replaced.
    stop = limit
    if cut:
        for needle in needles:
            borders = _borders(needle)
            at = _running(text, needle, borders)
            if at is not None:
                stop = min(stop, at)
            for reading in readings:
                at = _running(reading.text, needle, borders)
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
    result, _ = await perform_capturing(origin, header, secret, method, path, body, content_type,
                                        pin=pin, also_redact=also_redact)
    return result


# ─── Secret hand-off: capture a value a service creates, deliver it elsewhere ──
#
# A service that creates a credential (a Cloudflare API token, a deploy key)
# answers with its value once. With ``capture`` the broker takes that value out
# of the response before anything is returned: the agent gets the response with
# the value redacted and a short-lived handle, and the value can only be sent on
# to a typed destination (a GitHub Actions secret) after the owner's one-shot
# approval. Nothing is stored in the Vault or on disk.

_CAPTURE_FIELD = re.compile(r"[A-Za-z0-9_$-]{1,64}(?:\.[A-Za-z0-9_$-]{1,64}){0,7}")
CAPTURE_MIN, CAPTURE_MAX = 8, 16384
_WITHHELD = "[withheld]"


def capture_field(field: object) -> str:
    """A dot path into a JSON response, such as ``result.value`` or ``data.0.token``."""
    if not isinstance(field, str) or not _CAPTURE_FIELD.fullmatch(field):
        raise AccessDenied(
            "capture names a field of the JSON response as a dot path, such as result.value or data.0.token."
        )
    return field


def _pick(document: object, field: str) -> object:
    for key in field.split("."):
        if isinstance(document, dict):
            document = document.get(key)
        elif isinstance(document, list) and key.isdigit() and int(key) < len(document):
            document = document[int(key)]
        else:
            return None
    return document


def capturable(value: object) -> bool:
    """One line of visible ASCII: what ``redact`` finds in every form a later
    echo can carry (a JSON ``\\n`` escape of a line break it does not)."""
    return (isinstance(value, str) and CAPTURE_MIN <= len(value) <= CAPTURE_MAX
            and all(0x20 <= ord(ch) <= 0x7E for ch in value))


def _shape(value: object, depth: int = 0) -> object:
    """The response's structure with every value but true, false and null
    withheld, so a wrong field can be corrected without anything being shown."""
    if isinstance(value, str):
        return _WITHHELD
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return "[number]"
    if depth >= 8:
        return "[...]"
    if isinstance(value, dict):
        return {key: _shape(item, depth + 1) for key, item in list(value.items())[:50]}
    if isinstance(value, list):
        return [_shape(item, depth + 1) for item in value[:10]]
    return value


def _mark(shape: object, field: str) -> None:
    """Show where the captured value was in a shape."""
    *parents, last = field.split(".")
    for key in parents:
        if isinstance(shape, dict):
            shape = shape.get(key)
        elif isinstance(shape, list) and key.isdigit() and int(key) < len(shape):
            shape = shape[int(key)]
        else:
            return
    if isinstance(shape, dict) and last in shape:
        shape[last] = "[captured]"
    elif isinstance(shape, list) and last.isdigit() and int(last) < len(shape):
        shape[int(last)] = "[captured]"


def held_needles(values: tuple[str, ...]) -> list[str]:
    """Every form of these captured values ``redact`` should find."""
    return sorted(set().union(*(_needles(value, value) for value in values if value)), key=len, reverse=True)


def is_github_api(origin: Origin | None, pin: str) -> bool:
    """The public GitHub API, verified by the system's certificate store: the
    only place a captured secret may be sent."""
    return (origin is not None and origin.host == GITHUB_API_HOST and origin.port == GITHUB_API_PORT
            and origin.prefix == "" and not pin)


GITHUB_API_HOST, GITHUB_API_PORT = "api.github.com", 443


GITHUB_SECRET_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,254}")
_GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}")


def github_secret_paths(repository: object, environment: object, name: object) -> tuple[str, str, dict]:
    """The public-key and secret paths of a GitHub Actions secret, and the
    target as the owner's approval card shows it."""
    if not isinstance(repository, str) or not _GITHUB_REPOSITORY.fullmatch(repository) \
            or repository.split("/")[1] in (".", ".."):
        raise AccessDenied("repository is owner/name, such as example-org/website.")
    environment = environment if isinstance(environment, str) else ""
    if environment and (len(environment) > 255 or not _ascii(environment)
                        or any(ch in environment for ch in "/\\%?#")
                        or environment.strip() != environment):
        raise AccessDenied("environment is a GitHub environment name without slashes, or blank for a repository secret.")
    if not isinstance(name, str) or not GITHUB_SECRET_NAME.fullmatch(name) or name.upper().startswith("GITHUB_"):
        raise AccessDenied("name is a secret name of letters, digits and underscores that does not start with GITHUB_.")
    base = f"/repos/{repository}" + (f"/environments/{quote(environment, safe='')}/secrets" if environment
                                     else "/actions/secrets")
    return f"{base}/public-key", f"{base}/{name}", {
        "repository": repository, "environment": environment, "name": name}


def seal_for_github(public_key: object, value: str) -> str:
    """Encrypt ``value`` to a repository or environment public key (a libsodium
    sealed box, as GitHub requires). Only GitHub can open it."""
    from nacl import exceptions, public

    try:
        key = base64.b64decode(public_key, validate=True) if isinstance(public_key, str) else b""
        if len(key) != 32:
            raise ValueError
        sealed = public.SealedBox(public.PublicKey(key)).encrypt(value.encode())
    except (ValueError, TypeError, exceptions.CryptoError):
        raise AccessDenied("GitHub did not return a usable public key for this secret. Nothing was written.") from None
    return base64.b64encode(sealed).decode("ascii")


async def perform_capturing(
    origin: Origin,
    header: tuple[str, str],
    secret: str,
    method: str,
    path: str,
    body: str,
    content_type: str,
    pin: str = "",
    also_redact: tuple[str, ...] = (),
    capture: str = "",
) -> tuple[dict, str | None]:
    """``perform``, and with ``capture`` also the text at that field of a
    successful JSON response, which the returned result never contains.

    When the field cannot be taken (missing, not text, a cut or non-JSON
    response) a successful response's body is withheld, because it may hold a
    credential that is shown only once; only its structure is returned. An
    unsuccessful response is returned as usual and captures nothing.
    """
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
    keep = LIMIT + max(map(len, needles), default=0)
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
            # The bytes the service sent: the charset httpx reads them in is a guess.
            charset = response.headers.encoding
            returned = {
                name: b", ".join(value for key, value in response.headers.raw if key.lower() == name.encode())
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
    captured = None
    note = None
    if capture and 200 <= status < 300:
        document = None
        try:
            # The whole response is needed to read a field; a cut one is not parsed.
            if not truncated and b"\0" not in kept:
                document = json.loads(bytes(kept).decode("utf-8"))
        except ValueError:
            document = None
        value = _pick(document, capture) if document is not None else None
        shape = _shape(document) if document is not None else None
        if capturable(value) and not (value in header[1] or any(n in value for n in needles)):
            captured = value
            _mark(shape, capture)
            needles = sorted(set(needles).union(_needles(value, value)), key=len, reverse=True)
            note = {"field": capture, "captured": True}
        else:
            reason = ("the response is not complete JSON" if document is None
                      else f"{capture} holds the credential this grant signs in with" if capturable(value)
                      else f"{capture} is not one line of 8 to {CAPTURE_MAX:,} visible characters in this response")
            note = {"field": capture, "captured": False,
                    "reason": reason + "; nothing was captured. The service may already have created it"}
        # The response to a request that creates a credential may hold it, or
        # another, under any field or header: only its structure is returned,
        # never a value, whether or not the named field was taken.
        shown = json.dumps({"withheld": "Every value is withheld from a capture response.", "shape": shape})
        return {
            "status": status,
            "headers": {},
            "body": redact(shown.encode(), needles).decode("utf-8", "replace"),
            "truncated": False,
            "capture": note,
        }, captured
    elif capture:
        note = {"field": capture, "captured": False,
                "reason": f"the service answered {status}; nothing was captured"}
    # Only occurrences starting inside the returned window matter. One written
    # as sent was read in full (``keep`` adds the longest); one behind escapes
    # may still be running where reading stopped, and the body is cut there.
    # Off the event loop: the service chooses how much of the response is escapes.
    if b"\0" in kept:
        # A file, or text in UTF-16 or UTF-32: nothing the agent can read as
        # text, and an encoding the scan does not read. None of it is returned.
        data = _NOT_TEXT
    else:
        data = await asyncio.to_thread(partial(redact, bytes(kept), needles, LIMIT, cut=truncated))
    truncated = truncated or len(kept) > LIMIT
    result = {
        "status": status,
        "headers": {name: redact(value, needles).decode(charset, "replace") for name, value in returned.items()},
        "body": data.decode("utf-8", errors="replace"),
        "truncated": truncated,
    }
    if note is not None:
        result["capture"] = note
    return result, captured
