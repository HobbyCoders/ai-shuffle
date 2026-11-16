"""Remove credentials from text an ACP agent produced (errors, stderr, logs)
before it reaches a chat event or the app log.

Two strengths:

* :meth:`Redactor.clean` for text that must pass in some form (an RPC error,
  a stderr line, a log record). It decodes first (JSON string escapes, URL
  encoding, repeated), so an escaped or encoded secret is seen in its plain
  form, then removes: every known secret (the spec's own key and tokens),
  including any long run of one; every URL query string and fragment and any
  ``user:pass@``; the whole value of cookie and authorization-type headers;
  the value of any credential-named field (``token``, ``key``, ``code``,
  ``session`` ...); and token shapes (bearer tokens, JWTs, Google keys and
  OAuth tokens, opaque runs of random characters).
* :meth:`Redactor.quote` for a reason quoted in a chat: :meth:`clean`, and
  then only when what is left is plain prose. A line that still holds a
  structured payload (JSON it cannot read, brackets) or anything opaque gives
  None, and the caller shows a generic reason instead of the payload.

A deny-list alone cannot know every token shape, so :meth:`quote` is an
allow rule: prose passes, anything else does not.
"""

from __future__ import annotations

import json
import re
import traceback
from collections.abc import Iterable, Mapping
from typing import Any
from urllib.parse import unquote

REDACTED = "[redacted]"
_MAX_INPUT = 16 * 1024  # longer text is cut before it is cleaned (bounded work)
_MAX_QUOTED_INPUT = 4 * 1024  # longer lines are never quoted: no confident reading
_WINDOW = 12  # a run this long of a known secret counts as the secret

_ESCAPE = re.compile(r"\\(u[0-9a-fA-F]{4}|x[0-9a-fA-F]{2}|[\"\\/bfnrt])")
_SIMPLE_ESCAPES = {'"': '"', "\\": "\\", "/": "/", "b": " ", "f": " ", "n": "\n", "r": " ", "t": " "}
_PERCENT = re.compile(r"%[0-9a-fA-F]{2}")

# A URL: scheme, optional user:pass@, host and path kept; query and fragment dropped.
_URL = re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15}://)(?:[^\s/@\"'<>]*@)?([^\s?#\"'<>]*)(?P<query>[?#][^\s\"'<>]*)?")
# A query string with no scheme in front (a path, an OAuth callback "?code=").
_QUERY = re.compile(r"[?&][A-Za-z0-9_.\-\[\]]+=[^\s\"'<>]*")
# Headers whose whole value is a credential: dropped to the end of the value.
_HEADER = re.compile(
    r"(?i)(\b(?:set-cookie|cookie|(?:proxy-)?authorization|x-goog-api-key|x-api-key|api-key|"
    r"x-goog-iam-authorization-token|x-auth-token)[\"']?\s*[:=]\s*)"
    r"(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\r\n]*)")
# A field whose value is a credential, in JSON, a query or a "name: value".
_FIELD = re.compile(
    r"(?i)([\"']?\b[\w.-]*?(?:token|secret|passw(?:or)?d|api[_-]?key|apikey|credentials?|cookie|"
    r"session(?:id)?|sid|signature|sig|code|key|assertion|private[_-]?key)\b[\"']?\s*[:=]\s*)"
    r"(\"(?:[^\"\\]|\\.)*\"|'[^']*'|[^\s\"',;&}\]]+)")
_KEEP_FIELD_VALUE = re.compile(r"\d{1,6}|[A-Z][A-Za-z_]{2,40}")  # code: 429, code = ResourceExhausted
# Token shapes worth naming even without a field name.
_TOKENS = (
    re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]{5,}(?:\.[A-Za-z0-9_-]*)?"),  # JWT
    re.compile(r"\bAIza[0-9A-Za-z_-]{10,}"),  # Google API key
    re.compile(r"\bya29\.[0-9A-Za-z._-]+"),  # Google OAuth access token
    re.compile(r"\b1//[0-9A-Za-z._-]{8,}"),  # Google OAuth refresh token
    re.compile(r"\b4/[0-9A-Za-z._-]{10,}"),  # Google OAuth authorization code
    re.compile(r"\b(?:sk|pk|rk|gh[pousr]|xox[abpr]|glpat)[-_][A-Za-z0-9_-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)", re.S),
)
# A run that may be random: tested piece by piece (see _opaque).
_RUN = re.compile(r"[A-Za-z0-9_\-+/=.~%]{16,}")
_PIECE_SPLIT = re.compile(r"[/._\-:]")
_STRUCTURE = re.compile(r"[{}\[\]<>]")


def _decode(text: str) -> str:
    """JSON string escapes and URL encoding undone, repeatedly (a JSON string
    inside a JSON string, an encoded URL inside JSON)."""
    for _ in range(4):
        before = text
        text = _ESCAPE.sub(lambda m: chr(int(m.group(1)[1:], 16)) if m.group(1)[0] in "ux"
                           else _SIMPLE_ESCAPES[m.group(1)], text)
        if _PERCENT.search(text):
            text = unquote(text)
        if text == before:
            break
    return text


def _opaque(run: str) -> bool:
    """A run of characters that reads as random (a token), not as words, a
    path or an id: a long piece mixing letters and digits, or a very long
    mixed-case one (base64)."""
    pieces = _PIECE_SPLIT.split(run) if not re.search(r"[+=]", run) else [run]
    for piece in pieces:
        letters = sum(c.isalpha() for c in piece)
        digits = sum(c.isdigit() for c in piece)
        if len(piece) >= 16 and letters >= 4 and digits >= 2:
            return True
        if len(piece) >= 32 and any(c.islower() for c in piece) and any(c.isupper() for c in piece):
            return True
    return False


class Redactor:
    """Removes ``secrets`` (and credentials of any shape) from agent text."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self.secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def _known(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
            if len(secret) < _WINDOW:
                continue
            # A cut-short or partly echoed secret: any long run of it.
            spans: list[tuple[int, int]] = []
            for i in range(len(secret) - _WINDOW + 1):
                window = secret[i:i + _WINDOW]
                start = text.find(window)
                while start != -1:
                    spans.append((start, start + _WINDOW))
                    start = text.find(window, start + 1)
            if spans:
                spans.sort()
                merged = [list(spans[0])]
                for a, b in spans[1:]:
                    if a <= merged[-1][1]:
                        merged[-1][1] = max(merged[-1][1], b)
                    else:
                        merged.append([a, b])
                for a, b in reversed(merged):
                    text = text[:a] + REDACTED + text[b:]
        return text

    def clean(self, text: str) -> str:
        """``text`` decoded, with every credential removed (see module doc)."""
        if not text:
            return text
        text = _decode(str(text)[:_MAX_INPUT])
        text = self._known(text)
        text = _URL.sub(lambda m: m.group(1) + m.group(2) + ("?…" if m.group("query") else ""), text)
        text = _QUERY.sub(lambda m: m.group(0)[0] + "…", text)
        text = _HEADER.sub(lambda m: m.group(1) + REDACTED, text)

        def field(m: re.Match[str]) -> str:
            value = m.group(2)
            bare = value.strip("\"'")
            name = m.group(1).lower()
            if _KEEP_FIELD_VALUE.fullmatch(bare) and ("code" in name or "status" in name):
                return m.group(0)
            if bare.startswith(REDACTED[:-1]) or bare == "…":
                return m.group(0)
            quote = value[0] if value[:1] in "\"'" else ""
            return f"{m.group(1)}{quote}{REDACTED}{quote}"

        text = _FIELD.sub(field, text)
        for pattern in _TOKENS:
            text = pattern.sub(REDACTED, text)
        text = _RUN.sub(lambda m: REDACTED if _opaque(m.group(0)) else m.group(0), text)
        return text

    def data(self, value: Any) -> Any:
        """``value`` (an RPC error's ``data``) with every string cleaned."""
        if isinstance(value, str):
            return self.clean(value)
        if isinstance(value, Mapping):
            return {k: self.data(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.data(v) for v in value]
        return value

    def quote(self, text: str, limit: int = 400) -> str | None:
        """A one-line reason safe to quote in a chat, at most ``limit``
        characters; None when ``text`` cannot be shown with confidence."""
        if not text or len(text) > _MAX_QUOTED_INPUT:
            return None
        text = _readable_json(_decode(text))
        if text is None:
            return None
        text = " ".join(self.clean(text).split())
        if not text or _STRUCTURE.search(text.replace(REDACTED, "")) or text.count(REDACTED) > 2:
            return None
        # Nothing opaque may be left, even what clean() let through.
        if any(_opaque(m.group(0)) for m in _RUN.finditer(text)):
            return None
        text = text.rstrip(". ")
        if len(text) > limit:
            cut = text[:limit - 1].rsplit(" ", 1)[0]
            text = (cut or text[:limit - 1]) + "…"
        return text


def _readable_json(text: str) -> str | None:
    """``text`` with each JSON object in it replaced by its error's message
    and status (all else in it dropped); None when it has an object it cannot
    read."""
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
        summary = _error_summary(value)
        if summary is None:
            return None
        out.append(summary)
        i = end
    return "".join(out)


def _error_summary(value: Any) -> str | None:
    """``{"error": {"code": 429, "message": "...", "status": "..."}}`` (Google's
    error shape, or the same fields at the top) → ``message (STATUS, code N)``.
    None when it names no message."""
    if not isinstance(value, Mapping):
        return None
    inner = value.get("error") if isinstance(value.get("error"), Mapping) else value
    message = inner.get("message")
    if isinstance(message, str) and message.lstrip().startswith("{"):
        try:
            nested = json.loads(message)
        except ValueError:
            return None
        return _error_summary(nested)
    if not isinstance(message, str) or not message.strip():
        return None
    tags = [str(inner["status"])] if isinstance(inner.get("status"), str) else []
    if isinstance(inner.get("code"), int):
        tags.append(f"code {inner['code']}")
    return message.strip() + (f" ({', '.join(tags)})" if tags else "")


_SECRET_NAME = re.compile(r"(?i)(key|token|secret|password|passwd|credential|auth|cookie|session|proxy)")


def env_secrets(env: Mapping[str, str] | None) -> list[str]:
    """The values of ``env`` whose names say they are credentials."""
    return [str(v) for k, v in (env or {}).items() if v and _SECRET_NAME.search(str(k))]


def traceback_text(exc: BaseException) -> str:
    """``exc`` and its traceback as one string, for a log record that is
    cleaned before it is written (``logger.warning(..., exc_info=True)``
    would write the exception's message unfiltered)."""
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)).rstrip()
