"""Remove credentials from text an ACP agent or its CLI produced (errors,
stderr, logs) before it reaches a chat event, an API response or the app log.

Two strengths:

* :meth:`Redactor.clean` for text that must pass in some form (an RPC error,
  a stderr line, a log record). Removes every configured secret, every URL
  query string, fragment and ``user:pass@``, the whole value of cookie and
  authorization-type headers, the value of any credential-named field, and
  the standard token shapes (``AIza…``, ``ya29.…``, ``Bearer …``, JWTs,
  random-looking runs).
* :meth:`Redactor.quote` for a reason quoted in a chat: a valid JSON error is
  reduced to its message, status and code first; then :meth:`clean`; and only
  plain prose passes. Anything else gives None, and the caller shows a
  generic reason instead.

Bounded work: input is cut to ``MAX_INPUT`` characters before anything runs
(nothing after the cut is ever emitted), and every pattern is linear (bounded
repeats, field names classified in Python, no nested or lazy repeats), since
this runs on the app's event loop for each agent line.

Scope: secrets as a non-hostile CLI writes them, verbatim, JSON-escaped
(``\\u0041Iza…``) or URL-encoded (``%41Iza…``). Hex, HTML-entity or
whitespace-chunked copies of a key are deliberately not matched.
"""

from __future__ import annotations

import json
import re
import traceback
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import unquote

REDACTED = "[redacted]"
MAX_INPUT = 16 * 1024  # longer text is cut before it is cleaned
_MAX_QUOTED = 4 * 1024  # longer reasons are never quoted
_MIN_SECRET = 8  # shorter configured values are ignored (they would blank ordinary words)
_WINDOW = 12  # a run this long of a configured secret counts as the secret

_JSON_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})")
_PERCENT = re.compile(r"%[0-9a-fA-F]{2}")
# A URL: scheme, optional user:pass@, host and path kept; query and fragment dropped.
_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15}://)(?:[^\s/@\"'<>]{1,256}@)?([^\s?#\"'<>]{0,2048})"
                  r"(?P<query>[?#][^\s\"'<>]{0,4096})?")
# A query string with no scheme in front (a path, an OAuth callback "?code=").
_QUERY = re.compile(r"[?&][A-Za-z0-9_.\-\[\]]{1,64}=[^\s\"'<>]{0,4096}")
# "name: value" or "name=value", JSON keys included. The name is classified in
# Python (_is_secret_name), so the pattern itself stays linear.
_FIELD = re.compile(r"(?<![A-Za-z0-9_.\-\"'])([\"']?)([A-Za-z_][A-Za-z0-9_.\-]{0,63})\1([ \t]{0,3}[:=][ \t]{0,3})"
                    r"(\"(?:[^\"\\\n]|\\.){0,1024}\"|'[^'\n]{0,1024}'|[^\s\"',;&}\]]{1,1024})")
_HEADERS = {"cookie", "set-cookie", "authorization", "proxy-authorization", "x-goog-api-key", "x-api-key",
            "api-key", "x-goog-iam-authorization-token", "x-auth-token"}
_STATUS_FIELDS = {"code", "status", "status_code", "statuscode", "error_code", "http_code", "grpc_code"}
_STATUS_VALUE = re.compile(r"\d{1,6}|[A-Z][A-Za-z_]{2,40}")
_SECRET_WORDS = ("token", "secret", "passw", "credential", "cookie", "session", "signature", "apikey",
                 "private", "assertion", "auth")
_SECRET_PARTS = {"key", "sid", "sig", "code", "pwd", "pin", "otp"}
_NAME_PARTS = re.compile(r"[A-Z]?[a-z0-9]+|[A-Z]+(?![a-z])")
_TOKENS = (
    re.compile(r"(?i)\b(?:bearer|basic)[ \t]{1,4}[A-Za-z0-9._~+/=-]{8,4096}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,4096}\.[A-Za-z0-9_-]{5,4096}(?:\.[A-Za-z0-9_-]{0,4096})?"),  # JWT
    re.compile(r"\bAIza[0-9A-Za-z_-]{10,256}"),  # Google API key
    re.compile(r"\bya29\.[0-9A-Za-z._-]{1,4096}"),  # Google OAuth access token
    re.compile(r"\b1//[0-9A-Za-z._-]{8,4096}"),  # Google OAuth refresh token
    re.compile(r"\b4/[0-9A-Za-z._-]{10,4096}"),  # Google OAuth authorization code
    re.compile(r"\b(?:sk|pk|rk|gh[pousr]|xox[abpr]|glpat)[-_][A-Za-z0-9_-]{10,4096}"),
    re.compile(r"-----BEGIN [A-Z ]{0,40}PRIVATE KEY-----[\s\S]*"),  # the rest of the text
)
_RUN = re.compile(r"[A-Za-z0-9_\-+/=.~%]{16,}")
_PIECE_SPLIT = re.compile(r"[/._\-:=]")
_STRUCTURE = re.compile(r"[{}\[\]<>]")


def _text(value: Any) -> str:
    """``value`` as text, cut to MAX_INPUT before any other work."""
    if isinstance(value, (bytes, bytearray)):
        value = bytes(value[:MAX_INPUT * 4]).decode("utf-8", "replace")
    return str(value)[:MAX_INPUT]


def _decode(text: str) -> str:
    """JSON ``\\uXXXX`` escapes and URL encoding undone once each."""
    text = _JSON_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)
    return unquote(text) if _PERCENT.search(text) else text


def _is_secret_name(name: str) -> bool:
    lower = name.lower()
    if any(word in lower for word in _SECRET_WORDS):
        return True
    parts = {p.lower() for p in _NAME_PARTS.findall(name)}
    return bool(parts & _SECRET_PARTS) or lower.endswith("key")


def _opaque(run: str) -> bool:
    """A run of characters that reads as random (a token), not as words, a
    path or an id: a long piece mixing letters and digits, or a very long
    mixed-case one (base64)."""
    pieces = [run] if ("+" in run or run.endswith("=")) else _PIECE_SPLIT.split(run)  # base64 stays whole
    for piece in pieces:
        letters = sum(c.isalpha() for c in piece)
        digits = sum(c.isdigit() for c in piece)
        if len(piece) >= 16 and letters >= 4 and digits >= 2:
            return True
        if len(piece) >= 32 and any(c.islower() for c in piece) and any(c.isupper() for c in piece):
            return True
    return False


class Redactor:
    """Removes ``secrets`` (and credentials of any standard shape) from text."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self.secrets = sorted({str(s) for s in secrets if s and len(str(s)) >= _MIN_SECRET},
                              key=len, reverse=True)

    def _known(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
            if len(secret) < _WINDOW + 4:
                continue
            # A cut-short or partly echoed secret: any run of _WINDOW of it.
            hit = [False] * len(text)
            for i in range(len(secret) - _WINDOW + 1):
                window = secret[i:i + _WINDOW]
                start = text.find(window)
                while start != -1:
                    hit[start:start + _WINDOW] = [True] * _WINDOW
                    start = text.find(window, start + 1)
            if any(hit):
                out, i = [], 0
                while i < len(text):
                    if hit[i]:
                        while i < len(text) and hit[i]:
                            i += 1
                        out.append(REDACTED)
                    else:
                        out.append(text[i])
                        i += 1
                text = "".join(out)
        return text

    def clean(self, value: Any) -> str:
        """``value`` as text with every credential removed (see module doc)."""
        text = _text(value)
        if not text:
            return text
        text = self._known(_decode(text))
        text = _URL.sub(lambda m: m.group(1) + m.group(2) + ("?…" if m.group("query") else ""), text)
        text = _QUERY.sub(lambda m: m.group(0)[0] + "…", text)

        def field(m: re.Match[str]) -> str:
            name, value = m.group(2), m.group(4)
            lower = name.lower()
            if lower in _HEADERS:
                # The whole header value, to the end of the line.
                return f"{m.group(1)}{name}{m.group(1)}{m.group(3)}{REDACTED}\x00"
            if not _is_secret_name(name):
                return m.group(0)
            bare = value.strip("\"'")
            if lower in _STATUS_FIELDS and _STATUS_VALUE.fullmatch(bare):
                return m.group(0)  # code: 429, code = Unavailable
            if bare.startswith(REDACTED[:-1]) or bare == "…":
                return m.group(0)
            quote = value[0] if value[:1] in "\"'" else ""
            return f"{m.group(1)}{name}{m.group(1)}{m.group(3)}{quote}{REDACTED}{quote}"

        text = _FIELD.sub(field, text)
        # A header's value runs to the end of its line.
        text = re.sub(r"\x00[^\r\n]*", "", text)
        for pattern in _TOKENS:
            text = pattern.sub(REDACTED, text)
        return _RUN.sub(lambda m: REDACTED if _opaque(m.group(0)) else m.group(0), text)

    def quote(self, value: Any, limit: int = 400) -> str | None:
        """A one-line reason safe to quote in a chat, at most ``limit``
        characters; None when it cannot be shown with confidence."""
        text = _text(value)
        if not text or len(text) > _MAX_QUOTED:
            return None
        text = _readable_json(text)  # parsed before any unescaping
        if text is None:
            return None
        text = " ".join(self.clean(text).split())
        if not text or _STRUCTURE.search(text.replace(REDACTED, "")) or text.count(REDACTED) > 2:
            return None
        if any(_opaque(m.group(0)) for m in _RUN.finditer(text)):
            return None
        text = text.rstrip(". ")
        if len(text) > limit:
            cut = text[:limit - 1].rsplit(" ", 1)[0]
            text = (cut or text[:limit - 1]) + "…"
        return text


def _readable_json(text: str) -> str | None:
    """``text`` with each JSON object in it replaced by its error's message
    and status (all else in it dropped); None when it holds an object it
    cannot read as an error."""
    out: list[str] = []
    i = 0
    decoder = json.JSONDecoder()
    while i < len(text):
        start = text.find("{", i)
        if start == -1:
            out.append(text[i:])
            break
        out.append(text[i:start])
        try:
            value, end = decoder.raw_decode(text, start)
        except ValueError:
            return None
        summary = _error_summary(value, depth=0)
        if summary is None:
            return None
        out.append(summary)
        i = end
    return "".join(out)


def _error_summary(value: Any, depth: int) -> str | None:
    """``{"error": {"code": 429, "message": "...", "status": "..."}}`` (Google's
    error shape, or those fields at the top) → ``message (STATUS, code N)``; a
    message that is itself such JSON is read in turn. None when it names no
    message."""
    if not isinstance(value, Mapping) or depth > 3:
        return None
    inner = value.get("error") if isinstance(value.get("error"), Mapping) else value
    message = inner.get("message")
    if not isinstance(message, str) or not message.strip():
        return None
    if message.lstrip().startswith("{"):
        try:
            return _error_summary(json.loads(message), depth + 1)
        except ValueError:
            return None
    tags = [inner["status"]] if isinstance(inner.get("status"), str) else []
    if isinstance(inner.get("code"), int):
        tags.append(f"code {inner['code']}")
    return message.strip() + (f" ({', '.join(tags)})" if tags else "")


_SECRET_ENV = re.compile(r"(?i)(key|token|secret|password|passwd|credential|auth|cookie|session|^(https?|all)_proxy$)")


def env_secrets(env: Mapping[str, str] | None) -> list[str]:
    """The values of ``env`` whose names say they are credentials."""
    return [str(v) for k, v in (env or {}).items() if v and _SECRET_ENV.search(str(k))]


def traceback_text(exc: BaseException) -> str:
    """``exc`` and its traceback as one string, for a log record that is
    cleaned before it is written (``logger.warning(..., exc_info=True)``
    would write the exception's message unfiltered)."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
