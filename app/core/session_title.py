r"""Derive a session title from its first user message.

The composer serialises file references as literal ``@<path>`` tokens in the
prompt text — tiptap file chips (``@${attrs.fullPath}``), @-autocomplete
insertions, and legacy upload refs appended after the message — so a first
message that opens with an attachment produces titles like::

    @/workspace/ai-shuffle/uploads/Screenshot 2026-08-30 233548.png Remove
    computer use section from...

The path — not the request — is what the sidebar shows.

Stripping that token is not a plain ``lstrip('@')`` or ``@\S+`` cut: upload
filenames contain spaces and the token is unquoted, so the end of the path is
not knowable from the text alone. Two signals, in order:

* a caller-supplied list of the paths actually attached to the message (the
  chat WS handler holds the staged ``upload_paths``) — an exact prefix match
  ends the token precisely, spaces and all;
* otherwise a shape heuristic on the whitespace-delimited segments after
  ``@``:
  - a first segment ending in a short extension (``src/lib/api.ts``) is a
    complete single-segment token;
  - a first segment that looks absolute (leading ``/`` or a drive prefix)
    starts a path whose later segments are consumed until one carries an
    extension tail — the filename — or a small cap is hit. This is what
    unwraps space-bearing upload names when the staged paths are not at
    hand; it can over-consume an extension-less reference's neighbours,
    which beats leaking the whole path into every title;
  - anything else (``@user ...``) is a mention: only the first segment goes.

Tokens are consumed one at a time until the text no longer opens with ``@``,
so multi-attachment messages are fully unwrapped. The helper never empties a
title: if the message is nothing but attachments, the original text comes
back through, truncated.
"""

import re
from typing import Iterable, List, Optional

# The first whitespace-delimited segment after '@', plus the remainder.
_AT_TOKEN = re.compile(r"^@(?P<first>\S+)(?P<rest>(?:\s+\S+)*)")

# A segment that can only be part of a file path: one or more non-space
# characters ending in a short extension. Ordinary prose (``etc.``, ``St.``,
# ``wait...``) has no word characters after its final dot, so it never
# matches.
_FILE_TAIL = re.compile(r"\S+\.\w{1,5}\Z")

# A Windows drive prefix (``D:\`` or ``D:/``) makes a segment absolute too.
_DRIVE = re.compile(r"^[A-Za-z]:[\\/]")

# How many neighbours an absolute path token may swallow while hunting for
# its extension-bearing filename segment.
_PATH_SEGMENT_CAP = 4


def _is_absolute(segment: str) -> bool:
    return segment.startswith("/") or bool(_DRIVE.match(segment))


def _consume_one_token(text: str) -> str:
    """Strip a single leading ``@`` token (the text opens with ``@``)."""
    match = _AT_TOKEN.match(text)
    if not match:
        return text[1:].lstrip()

    first = match.group("first")
    rest = (match.group("rest") or "").split()
    consumed = first

    if not _FILE_TAIL.match(first) and _is_absolute(first):
        for segment in rest[:_PATH_SEGMENT_CAP]:
            consumed = f"{consumed} {segment}"
            if _FILE_TAIL.match(segment):
                break

    return text[1 + len(consumed):].lstrip()


def strip_leading_attachment_tokens(
    text: str, known_paths: Optional[Iterable[str]] = None
) -> str:
    """Remove leading ``@<path>`` tokens and the whitespace after each.

    Messages that do not open with ``@`` come back unchanged; an ``@`` later
    in the message (``email me @user``) is never touched. When stripping
    would leave nothing (the message *is* the attachment), the original text
    comes back — a title is never emptied here.
    """
    body = text.lstrip()
    if not body.startswith("@"):
        return text

    known: List[str] = sorted(known_paths or [], key=len, reverse=True)
    stripped = body
    while stripped.startswith("@"):
        rest = stripped[1:]
        exact = next((p for p in known if rest.startswith(p)), None)
        if exact is not None:
            stripped = rest[len(exact):].lstrip()
        else:
            stripped = _consume_one_token(stripped)

    return stripped or text


def derive_session_title(
    text: str,
    known_paths: Optional[Iterable[str]] = None,
    max_chars: int = 100,
    word_boundary: bool = True,
) -> str:
    """Turn a first user message into a session title.

    Leading attachment tokens are stripped first, then what remains is
    truncated to ``max_chars``. ``word_boundary`` keeps the two historical
    shapes: the 100-char sites cut at the last space before the ellipsis, the
    older 50-char sites append the ellipsis to whatever the cut left.
    """
    body = strip_leading_attachment_tokens(text, known_paths)
    title = body[:max_chars].strip()
    if len(body) > max_chars:
        if word_boundary:
            title = title.rsplit(" ", 1)[0] + "..."
        else:
            title += "..."
    return title
