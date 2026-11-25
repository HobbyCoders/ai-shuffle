"""Conservative prompt-injection screening for prompt-bound context files.

Instruction files (AGENTS.md, the identity file) enter the system prompt
verbatim, where the model has no chance to treat them as data — so unlike tool
results, which the base prompt already tells the model to distrust, a poisoned
context file has to be caught *before* injection. This screen is deliberately
narrow: it matches only text whose primary reading is an attempt to hijack the
prompt (override directives, role-tag smuggling, concealment orders), never
ordinary prose about security. A match blocks the whole file with a placeholder
naming the finding — recoverable by the user editing the file — and logs a
warning. False negatives are acceptable; false positives on real project docs
are not, so keep every pattern here strict enough to defend in a diff review.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

logger = logging.getLogger(__name__)

# (finding label, pattern). Case-insensitive. Each pattern must require both an
# imperative and a prompt-machinery object — "ignore the linter" or a doc that
# merely mentions "system prompt" must not match.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override-directive",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b[^.\n]{0,40}\b(previous|prior|"
            r"earlier|above|all|any|your)\b[^.\n]{0,40}\b(instructions?|directives?|"
            r"system prompt|rules)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "role-tag",
        re.compile(
            r"<\s*/?\s*(system|assistant)\s*>|<\|im_(start|end)\|>|\[/?(INST|SYS)\]",
            re.IGNORECASE,
        ),
    ),
    (
        # fluid's own control channels: a genuine system-reminder, steering
        # or peer-message marker is emitted by the harness, never written in a
        # context file —
        # one appearing in an AGENTS.md/identity/memory entry is spoofing.
        "harness-marker",
        re.compile(
            r"<\s*/?\s*system-reminder\s*>|\[/?user interjection"
            r"|\[/?agent message",
            re.IGNORECASE,
        ),
    ),
    (
        "concealment-order",
        re.compile(
            r"\b(do not|don't|never)\b[^.\n]{0,40}\b(tell|reveal|show|mention)\b"
            r"[^.\n]{0,40}\b(the |your )?(user|human|operator)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "new-identity-order",
        re.compile(
            r"\byou are no longer\b[^.\n]{0,60}\b(assistant|agent|ai|model)\b",
            re.IGNORECASE,
        ),
    ),
)


# Invisible / direction-control characters used to hide instructions from the
# human reviewing a file while the model still reads them: zero-width spaces
# and joiners, bidi embedding/override/isolate controls, interior BOMs.
_INVISIBLE_RE = re.compile(
    "[\u200b-\u200f\u202a-\u202e\u2060-\u2064\u2066-\u2069\ufeff]"
)

# How many stripped invisible characters turn from "editor noise" into a
# finding. A stray joiner from a copy-paste is normal; dozens are a payload.
_INVISIBLE_FINDING_THRESHOLD = 8


def strip_invisible(content: str) -> tuple[str, int]:
    """Remove invisible/bidi-control characters; returns (clean, count)."""
    clean, count = _INVISIBLE_RE.subn("", content)
    return clean, count


# Credential shapes that must never persist into prompt-bound stores. Explicit
# vendor prefixes and key blocks only — a generic entropy heuristic would
# false-positive on ordinary prose, and a blocked memory write is friction the
# model retries.
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("Anthropic API key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{10,}")),
    ("OpenAI-style API key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b")),
    ("GitHub token", re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{20,}\b")),
    ("Slack token", re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b")),
    (
        "credential assignment",
        re.compile(
            r"\b(api[_-]?key|access[_-]?token|client[_-]?secret|password)\b"
            r"\s*[=:]\s*[\"']?[A-Za-z0-9+/_-]{16,}",
            re.IGNORECASE,
        ),
    ),
)


def looks_like_secret(content: str) -> str | None:
    """The kind of credential ``content`` appears to contain, or ``None``."""
    for kind, pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            return kind
    return None


REDACTED = "[redacted]"

# Field, flag and variable names whose value is a credential: the name, split
# at ``_``/``-`` and camelCase humps, ENDS in one of these words. A tight list
# on purpose (``key`` alone, ``credential`` or ``cookie`` would catch ordinary
# fields): password, passwd, secret (so client_secret), token (so
# access_token, refresh_token, GITHUB_TOKEN), api_key/apikey, access_key,
# secret_key, private_key, authorization.
_CREDENTIAL_NAME = re.compile(
    r"(?:^|_)(?:password|passwd|secret|token|api_?key|access_?key|secret_?key"
    r"|private_?key|authorization)$"
)
_CAMEL_HUMP = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def is_credential_name(name: str) -> bool:
    """Whether a field/flag/variable called ``name`` holds a credential
    (``password``, ``APP_API_KEY``, ``--api-key``, ``clientSecret``; not
    ``api_key_env``, ``max_tokens`` or ``sort_key``)."""
    norm = _CAMEL_HUMP.sub("_", name.strip().lstrip("-")).lower().replace("-", "_")
    return bool(_CREDENTIAL_NAME.search(norm))


def credential_values(values: Iterable[Any]) -> list[str]:
    """The credentials among ``values``: every non-blank string. A value is
    never exempted by its spelling: a configured key of ``local`` is as
    secret as any other. Only the caller knows that a value is a placeholder
    it chose itself (the local provider's default key) and leaves it out."""
    return [v for v in values if isinstance(v, str) and v.strip()]


# A private-key block, one of two unambiguous shapes. Every quantifier is
# possessive and no two adjacent parts can match the same characters, so
# nothing is ever re-split (a malformed block costs one pass).
#
# Multi-line: line breaks as plain text or as JSON/repr escapes, then any
# indent. Optional ``Name: value`` header lines (Proc-Type, DEK-Info) each
# start after a break, their value ends at the next break, then an optional
# blank line. Then whole base64 lines of at least 16 chars: a line must end
# at a break, a closing quote, a clip mark or the end of the text, so the
# identifier at the start of the line after a clipped block
# (``ABCDEFGHIJKLMNOPQRSTUV = 1``) is never taken for one. A short last line
# goes only before the END line or at the very end of the text.
_PEM_NL = r"(?:\r?\n|\\r\\n|\\n)"
_PEM_LINE_END = rf"(?=[ \t]*+(?:{_PEM_NL}|[\"'…]|\Z))"
_PEM_SHORT_END = r"(?=[ \t]*+(?:[\"'…]|\Z))"
_PEM_B64_LINE = r"[A-Za-z0-9+/]{16,}+={0,2}+"
_PEM_B64_SHORT = r"[A-Za-z0-9+/]{1,15}+={0,2}+"
_PEM_END = r"-----END \1PRIVATE KEY-----"
_PEM_MULTILINE = (
    rf"(?:{_PEM_NL}[ \t]*+[A-Za-z][A-Za-z0-9-]*+:[ \t][^\r\n\\]*+)*+"
    rf"(?:{_PEM_NL}[ \t]*+(?={_PEM_NL}))?"
    rf"(?:{_PEM_NL}[ \t]*+{_PEM_B64_LINE}{_PEM_LINE_END})++"
    rf"(?:(?:{_PEM_NL}[ \t]*+{_PEM_B64_SHORT})?(?:{_PEM_NL}[ \t]*+)?{_PEM_END}"
    rf"|{_PEM_NL}[ \t]*+{_PEM_B64_SHORT}{_PEM_SHORT_END})?"
)
# One line (a key pasted with spaces for breaks): single-space separated
# base64 words, then the END line or the end of the text. No header lines.
_PEM_ONE_LINE = (
    rf" {_PEM_B64_LINE}(?: [A-Za-z0-9+/]++={{0,2}}+)*+"
    rf"(?: {_PEM_END}|{_PEM_SHORT_END})"
)

# Shapes redacted in place, on top of the vendor prefixes above. Each keeps
# its label (the header scheme, the URL's user and host) so a redacted
# diagnostic still says what it was about.
_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # A private key only when key material follows the BEGIN line (see
    # _PEM_MULTILINE): a lone marker, such as a quoted header constant in
    # source, stays. The END line goes with it when present; without one (a
    # clipped block) only the base64 lines go, never the text after them.
    (
        re.compile(
            rf"-----BEGIN ([A-Z ]*)PRIVATE KEY-----(?:{_PEM_MULTILINE}|{_PEM_ONE_LINE})"
        ),
        REDACTED,
    ),
    (re.compile(r"\b(Bearer|Basic)(\s+)[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
     r"\1\2" + REDACTED),
    # ``scheme://user:password@host``: only the password goes. A match starts
    # only where a run of scheme characters starts, so a long token with no
    # ``://`` is scanned once rather than once per character.
    (
        re.compile(
            r"(?<![A-Za-z0-9+.\-])([A-Za-z][A-Za-z0-9+.\-]*://[^\s:/?#@\"'<>]*:)"
            r"[^\s/?#@\"'<>]+(?=@)"
        ),
        r"\1" + REDACTED,
    ),
)

# ``name = value`` / ``name: value`` / ``"name": "value"`` / ``--name=value``
# / ``export NAME=value``: the head (name and separator) is found first and
# its name checked by is_credential_name; only then is a value read after it
# (_ASSIGNMENT_VALUE), so a head for any other name consumes nothing past
# its separator. The name starts only where a run of name characters starts
# and is matched possessively, so a long run is scanned once.
_ASSIGNMENT_HEAD = re.compile(
    r"(?<![A-Za-z0-9_-])(?P<name>-*+[A-Za-z_][A-Za-z0-9_-]*+)"
    r"(?P<sep>(?:\\?[\"'])?[ \t]*[:=](?![=:])[ \t]*)"
)
# A quoted literal, whole: escapes inside it (``"a\"b"``) are part of the
# value, and a JSON-escaped literal (``\"…\"``, as in a tool's serialized
# arguments) is read as the original text's escapes re-escaped once. A
# Python string prefix (r, b, f, u, rb, br, fr, rf, any case) may lead.
# A quote still open at the end of the text (a clipped value) is read to
# the end, so a cut never saves a credential's first half. Otherwise a bare
# run, plus the ``(``/``[`` that makes it a call or subscript. Each token
# kind starts with a different character, so the possessive loops never
# re-split.
_ASSIGNMENT_VALUE = re.compile(
    r"(?P<prefix>[rR][bBfF]|[bBfF][rR]|[rRbBfFuU])?(?:"
    r"\\\"(?P<edq>(?:[^\"\\\r\n]|\\[^\"\\]|\\\\(?:\\\\|\\\"|[^\"\\]))*+)\\\""
    r"|\"(?P<dq>(?:[^\"\\\r\n]|\\.)*+)\""
    r"|\\'(?P<esq>(?:[^'\\\r\n]|\\[^'])*+)\\'"
    r"|'(?P<sq>(?:[^'\\\r\n]|\\.)*+)'"
    r"|\\?[\"'](?P<open>[^\r\n]*+)\Z"
    r")"
    r"|(?P<bare>[^\s\"'`<>,;(){}\[\]&|\\]++)(?P<call>[(\[]?)"
)
_QUOTED_GROUPS = ("edq", "dq", "esq", "sq", "open")
# Tokenizer special tokens (``</s>``, ``<|endoftext|>``, ``[CLS]``): a
# ``token = "</s>"`` in model code names a delimiter, not a credential.
_SPECIAL_TOKEN = re.compile(r"<\|[^\s|<>]*\|>|</?[A-Za-z0-9_.:-]+>|\[[A-Z][A-Z0-9_]*\]")
_SHELL_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_DOTTED_NAME = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+")
_PLAIN_NAME_CHARS = re.compile(r"[A-Za-z0-9_.]*")
# Bare values shorter than this are never guessed to be credentials (a
# ``password: hidden`` in prose). Exact known values have no floor.
_MIN_GUESSED = 8


def _bare_is_literal(value: str, shell: bool) -> bool:
    """Whether a bare (unquoted) right-hand side is a credential literal
    rather than an expression: an identifier, an attribute path
    (``settings.provider_api_key``), a reference (``$TOKEN``) or a path stay.
    ``shell`` (``NAME=value``, upper-case name, no spaces) takes any other
    run as the literal it is; elsewhere only an opaque token of at least
    _MIN_GUESSED chars counts: one with a character no name has, or a long
    one with digits in it."""
    if value == REDACTED or value.startswith(("$", "/", "./", "../", "~/")):
        return False
    if _DOTTED_NAME.fullmatch(value):
        return False
    if shell:
        return True
    if len(value) < _MIN_GUESSED:
        return False
    if not _PLAIN_NAME_CHARS.fullmatch(value):
        return True
    return len(value) >= 16 and sum(c.isdigit() for c in value) >= 2


def _literal_span(head: re.Match[str], value: re.Match[str]) -> tuple[int, int] | None:
    """The span of the credential literal in a credential-named assignment,
    or None when its value is empty, already redacted, a reference or an
    expression."""
    group = next((g for g in _QUOTED_GROUPS if value[g] is not None), None)
    if group is not None:
        quoted = value[group]
        if not quoted or quoted == REDACTED or quoted.startswith("$"):
            return None
        if _SPECIAL_TOKEN.fullmatch(quoted):
            return None
        if "f" in (value["prefix"] or "").lower() and "{" in quoted:
            return None  # an f-string that builds the value: an expression
        return value.span(group)
    if value["call"]:  # ``get_key(...)``, ``os.environ[...]``
        return None
    bare = value["bare"]
    if _query_param(head):
        # ``?api_key=…`` / ``&token=…``: a URL query has no identifiers or
        # attribute paths, so any value but a reference is the credential.
        return None if bare == REDACTED or bare.startswith("$") else value.span("bare")
    shell = head["sep"] == "=" and bool(_SHELL_NAME.fullmatch(head["name"]))
    return value.span("bare") if _bare_is_literal(bare, shell) else None


def _query_param(head: re.Match[str]) -> bool:
    """Whether ``head`` is a URL query parameter: ``?name=`` or ``&name=``
    (after a name character, so not a shell's ``&& NAME=``)."""
    start, text = head.start(), head.string
    if head["sep"] != "=" or start == 0 or text[start - 1] not in "?&":
        return False
    return text[start - 1] == "?" or (start >= 2 and text[start - 2] not in "& \t")


def _redact_assignments(text: str) -> str:
    out: list[str] = []
    copied = 0  # text before this is in ``out``
    scanned = 0  # a head before this sits inside a value already read
    for head in _ASSIGNMENT_HEAD.finditer(text):
        if head.start() < scanned or not is_credential_name(head["name"]):
            continue
        value = _ASSIGNMENT_VALUE.match(text, head.end())
        if value is None:
            continue
        scanned = value.end()
        span = _literal_span(head, value)
        if span is not None:
            out.append(text[copied:span[0]])
            out.append(REDACTED)
            copied = span[1]
    if not out:
        return text
    out.append(text[copied:])
    return "".join(out)


# A known value shorter than this never has its clipped prefix guessed at:
# a short prefix matches ordinary text.
_MIN_KNOWN_PREFIX = 8


def redact_text(text: str, known: Iterable[str] = ()) -> str:
    """``text`` with credentials replaced by ``[redacted]``.

    ``known`` are exact values to remove wherever they appear, at any length
    (the key or token a request actually carried; blanks are skipped, and a
    caller leaves out a placeholder it chose itself). A known value's prefix
    at the very end of
    ``text`` goes too (a clip, with or without its ``…``, that cut the value
    in half). Then the recognised shapes: vendor key prefixes, private-key
    blocks, ``Bearer``/``Basic`` tokens, URL passwords, and literal values
    of credential-named assignments (an expression stays: see
    _bare_is_literal). Recognised shapes only, not arbitrary-secret
    detection. A caller that clips redacts first (see :func:`clip_redacted`).
    """
    for value in sorted(set(credential_values(known)), key=len, reverse=True):
        text = text.replace(value, REDACTED)
        body, mark = (text[:-1], "…") if text.endswith("…") else (text, "")
        for n in range(len(value) - 1, _MIN_KNOWN_PREFIX - 1, -1):
            if body.endswith(value[:n]):
                text = body[:-n] + REDACTED + mark
                break
    for kind, pattern in _SECRET_PATTERNS:
        if kind not in ("private-key block", "credential assignment"):
            text = pattern.sub(REDACTED, text)
    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return _redact_assignments(text)


# The most text clip_redacted redacts before taking its preview. Larger
# input (a kernel's 2 MiB stdout) is cut here first, and the redacted text
# within _CLIP_TAIL of that cut is dropped: a credential the cut split may
# not be recognised there, and earlier redactions can shift it towards the
# preview.
_REDACT_MAX = 256 * 1024
_CLIP_TAIL = 4096


def clip_redacted(text: str, cap: int, known: Iterable[str] = (), mark: str = "") -> str:
    """``text`` redacted, then cut to ``cap`` chars (``mark`` appended when
    cut). The whole text is redacted before the cut, so a cut can never
    leave half a credential that no pass recognises (a URL password without
    its ``@host``, a JSON token without its closing quote). Past
    _REDACT_MAX it fails closed instead (see there)."""
    text = text or ""
    if len(text) <= _REDACT_MAX:
        text = redact_text(text, known)
        return text if len(text) <= cap else text[:cap] + mark
    text = redact_text(text[:_REDACT_MAX], known)
    return text[: min(cap, max(0, len(text) - _CLIP_TAIL))] + mark


def redact_argv(args: Iterable[Any]) -> list[Any]:
    """A command line safe to print: the value of a credential-named flag
    goes, split (``--api-key VALUE``, even a value that starts with ``-``)
    or joined (``--api-key=VALUE``), and every other argument passes
    through :func:`redact_text`."""
    out: list[Any] = []
    mask_next = False
    for arg in args:
        if not isinstance(arg, str):
            out.append(arg)
            mask_next = False
            continue
        if mask_next:
            out.append(REDACTED)
            mask_next = False
            continue
        mask_next = False
        if arg.startswith("-"):
            flag, sep, value = arg.partition("=")
            if is_credential_name(flag):
                if sep:
                    out.append(f"{flag}={REDACTED}" if value else arg)
                else:
                    out.append(arg)
                    mask_next = True
                continue
        out.append(redact_text(arg))
    return out


def redact_secrets(value: Any, known: Iterable[str] = ()) -> Any:
    """A copy of ``value`` with every string passed through :func:`redact_text`.

    Dicts and lists are walked. A string (or list of strings) under a
    credential-named key (see is_credential_name: ``{"password": …}``) is
    masked whole, whatever its shape. Keys and other scalars are kept.
    """
    known = tuple(known)
    if isinstance(value, str):
        return redact_text(value, known)
    if isinstance(value, dict):
        return {k: _redact_field(k, v, known) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_secrets(v, known) for v in value]
    if isinstance(value, tuple):
        return tuple(redact_secrets(v, known) for v in value)
    return value


def _redact_field(key: Any, value: Any, known: tuple[str, ...]) -> Any:
    if isinstance(key, str) and is_credential_name(key):
        if isinstance(value, str):
            return REDACTED if value else value
        if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
            return type(value)(REDACTED if v else v for v in value)
    return redact_secrets(value, known)


def screen_findings(content: str) -> list[str]:
    """Finding labels for suspected injection patterns in ``content``."""
    findings = [label for label, pattern in _PATTERNS if pattern.search(content)]
    _, invisible = strip_invisible(content)
    if invisible >= _INVISIBLE_FINDING_THRESHOLD:
        findings.append(f"invisible-text ({invisible} hidden chars)")
    return findings


def screen(content: str, label: str) -> str:
    """Return ``content``, or a blocking placeholder when it fails the screen.

    ``label`` names the source (a path, usually) in the placeholder and the
    log line. A leading UTF-8 BOM is an editor artifact, not a finding.
    """
    if content.startswith("\ufeff"):
        content = content[1:]
    findings = screen_findings(content)
    if not findings:
        return content
    logger.warning("blocked %s from the prompt: %s", label, ", ".join(findings))
    return (
        f"[blocked: {label} matched suspected prompt-injection pattern(s): "
        f"{', '.join(findings)}. The file was not loaded; edit it to clear the "
        f"match.]"
    )
