"""Opt-in external transcript redaction; never used for storage or replay.

Recognized credentials only, not arbitrary-secret detection. Owners must review
sharing artifacts before posting. Raw backup exports intentionally bypass this.
"""
from __future__ import annotations

import json
import re
from typing import Any

from app.core.harness_reminders import strip_harness_reminders_from_entry
from app.core.scrub import REDACTED, redact_url_credentials, strip_url_userinfo

# Match the preview-access wire format, including copies outside a URL.
_PREVIEW = re.compile(r"v1\.[A-Za-z0-9_-]+\.[0-9]+\.[a-fA-F0-9]{64}")
_BEARER = re.compile(r"\b(?:Bearer|Basic)\s+([A-Za-z0-9._~+/=-]+)", re.IGNORECASE)
_PROVIDER_KEY = re.compile(
    r"\b(?:sk-(?:proj-|ant-)?[A-Za-z0-9_-]{20,}|gh[pousr]_[A-Za-z0-9]{20,}"
    r"|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{20,})"
)
_PRIVATE_KEY = re.compile(
    r"-----BEGIN ((?:(?:RSA|EC|DSA|OPENSSH) )?PRIVATE KEY)-----"
    r"[\s\S]*?-----END \1-----"
)
_SENSITIVE = {
    "password", "passwd", "pwd", "secret", "clientsecret", "apikey", "apitoken",
    "accesstoken", "refreshtoken", "idtoken", "token", "authorization",
    "proxyauthorization", "cookie", "setcookie", "privatekey", "credentials",
    "aispreview", "xapikey",
}
_ASSIGNMENT = re.compile(
    r"\b(?:password|passwd|pwd|client[_-]?secret|api[_-]?(?:key|token)|"
    r"access[_-]?token|refresh[_-]?token|id[_-]?token|token|secret|ais_preview)"
    r"[\"']?\s*[:=]\s*(?:\"((?:\\.|[^\"\r\n])+)\"|"
    r"'((?:\\.|[^'\r\n])+)'|([^\s,;&\"'<>}]+))",
    re.IGNORECASE,
)
# As in scrub.py, a scheme-run boundary avoids quadratic scans of large blobs.
_URL = re.compile(
    r"(?<![A-Za-z0-9+.\-])[A-Za-z][A-Za-z0-9+.\-]*://[^\s/?#\"'<>\[\]{}\\]+@"
)
# A decoded view lets the same rules see percent / JSON-unicode / HTML copies.
# Span mapping keeps unrelated encoding and text byte-for-byte, even when a
# credential elsewhere in the string is removed. Up to three encoding layers.
_ENCODED = re.compile(
    r"%([0-9a-fA-F]{2})|\\u00([0-9a-fA-F]{2})|"
    r"&#([0-9]{1,3});|&#x([0-9a-fA-F]{1,2});|&(amp|quot|apos|period);",
    re.IGNORECASE,
)
_ENTITIES = {"amp": "&", "quot": '"', "apos": "'", "period": "."}


def _decoded_view(text: str) -> tuple[str, list[tuple[int, int]]]:
    spans = [(i, i + 1) for i in range(len(text))]
    for _ in range(3):
        chars, mapped, end = [], [], 0
        for match in _ENCODED.finditer(text):
            chars.extend(text[end:match.start()])
            mapped.extend(spans[end:match.start()])
            hex_value = match[1] or match[2] or match[4]
            char = (chr(int(hex_value, 16)) if hex_value else
                    chr(int(match[3])) if match[3] else _ENTITIES[match[5].lower()])
            chars.append(char)
            mapped.append((spans[match.start()][0], spans[match.end() - 1][1]))
            end = match.end()
        if not end:
            break
        chars.extend(text[end:])
        mapped.extend(spans[end:])
        text, spans = "".join(chars), mapped
    return text, spans


def redact_export_text(text: str) -> str:
    """Filter recognized credentials in text without decoding unrelated content."""
    decoded, spans = _decoded_view(text)
    replacements = []
    for pattern in (_PREVIEW, _PROVIDER_KEY, _PRIVATE_KEY, _BEARER, _ASSIGNMENT):
        for match in pattern.finditer(decoded):
            if pattern is _BEARER:
                group = 1
            elif pattern is _ASSIGNMENT:
                group = next(i for i in (1, 2, 3) if match[i] is not None)
            else:
                group = 0
            start, end = match.span(group)
            replacements.append((spans[start][0], spans[end - 1][1], REDACTED))
    for match in _URL.finditer(decoded):
        # Reuse the existing SSH-login exception: git is not a credential in
        # ssh://git@host, but passwords and HTTP token-userinfo are private.
        if strip_url_userinfo(match[0]) == match[0]:
            continue
        redacted = redact_url_credentials(match[0])
        if redacted != match[0]:
            replacements.append((spans[match.start()][0], spans[match.end() - 1][1], redacted))
    # Longer outer matches win (e.g. a URL userinfo containing a preview token).
    result, end = [], 0
    for start, stop, replacement in sorted(replacements, key=lambda r: (r[0], -r[1])):
        if start < end:
            continue
        result.extend((text[end:start], replacement))
        end = stop
    result.append(text[end:])
    return "".join(result)


def redact_external_export(value: Any) -> Any:
    """Copy and filter nested export data, including content pasted into keys."""
    if isinstance(value, str):
        return redact_export_text(value)
    if isinstance(value, dict):
        return {
            redact_export_text(k) if isinstance(k, str) else k:
            REDACTED if isinstance(k, str) and re.sub(r"[-_ ]", "", k).lower() in _SENSITIVE
            else redact_external_export(v)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_external_export(v) for v in value]
    return value


def redact_export_jsonl(content: str) -> str:
    """Parse every nonblank record, rejecting malformed / non-object JSONL.

    Never fall back to raw bytes. Reject nonstandard JSON constants as well;
    malformed history can still be downloaded through the private raw path.
    Harness reminder blocks (saved memory, environment facts) are removed
    from user messages: they are private model context, not the conversation.
    """
    def reject_constant(_value: str) -> None:
        raise ValueError("Nonstandard JSON constant")

    entries = []
    for line in content.splitlines():
        if not line.strip():
            continue
        entry = json.loads(line, parse_constant=reject_constant)
        if not isinstance(entry, dict):
            raise ValueError("JSONL record must be an object")
        entries.append(redact_external_export(strip_harness_reminders_from_entry(entry)))
    return "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)
