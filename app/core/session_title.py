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
title: a message that is nothing but known attachments is titled by their
file names; nothing but unknown tokens, the original text comes back through,
truncated.

A known path is also cut out wherever it sits: the composer puts a chip where
the cursor was, usually after the typed words (``fix it @/ws/a.png``), and the
title is the typed words only (#1654).

The one rule, by what the caller knows about the message's attachments:

* known, with paths: only those ``@<path>`` references go, read left to right
  (the longest path where two start together, each reference consumed whole);
  any other ``@`` text is the user's and stays. A message that is nothing but
  attachments is titled by their file names;
* known and empty (``[]``): the text is the user's as typed, nothing is
  guessed;
* unknown (``None``): only leading tokens are guessed and stripped (above),
  since an ``@`` later in prose may be a mention. Callers that never see the
  attachment list (the API query lanes, the JSONL listing and import) are in
  this state.

Then the result is truncated by code points. The client's optimistic title
(``deriveSessionTitle`` in ``frontend/src/lib/utils/message-presentation.ts``)
is a port of this function for the two known states; both suites assert the
same final strings from ``attachment-title-cases.json``.
"""

import re
from typing import Iterable, List, Optional, Tuple, Union

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


def strip_known_attachment_references(text: str, known_paths: Optional[Iterable[str]] = None) -> str:
    """Remove every ``@<path>`` of a known attachment, wherever it sits.

    Read like :func:`_split_known_references`: left to right, the longest
    path where two start together, each reference consumed whole. The spaces a removal leaves are folded into one. Like the leading strip, a
    message that is nothing but attachments comes back unchanged.
    """
    return _cut_known_references(text, known_paths) or text


def _split_known_references(text: str, known_paths: Optional[Iterable[str]]) -> List[Union[str, Tuple[str]]]:
    """``text`` cut into typed stretches and ``(path,)`` attachment references.

    Read left to right; at each point the earliest ``@<path>`` wins, the
    longest path when two start there, and a reference is consumed whole, so a
    shorter path inside a longer one's reference is never matched again. The
    client's ``splitUploadMentions`` (message-presentation.ts) reads the same.
    """
    known = sorted({p for p in known_paths or [] if p}, key=len, reverse=True)
    parts: List[Union[str, Tuple[str]]] = []
    offset = 0
    while offset < len(text):
        at, path = -1, ""
        for candidate in known:
            index = text.find("@" + candidate, offset)
            if index != -1 and (at == -1 or index < at):
                at, path = index, candidate
        if at == -1:
            parts.append(text[offset:])
            break
        if at > offset:
            parts.append(text[offset:at])
        parts.append((path,))
        offset = at + len(path) + 1
    return parts


def _cut_known_references(text: str, known_paths: Optional[Iterable[str]]) -> str:
    """``text`` with every known ``@<path>`` cut out; may be empty."""
    parts = _split_known_references(text, known_paths)
    if not any(isinstance(part, tuple) for part in parts):
        return text
    body = "".join(part if isinstance(part, str) else " " for part in parts)
    return _LINE_EDGE_SPACES.sub("\n", _SPACE_RUN.sub(" ", body)).strip()


def _attachment_names(text: str, known_paths: Iterable[str]) -> str:
    """The file names of the known attachments ``text`` references, in order, once each."""
    names: List[str] = []
    for part in _split_known_references(text, known_paths):
        if isinstance(part, tuple):
            name = re.split(r"[/\\]", part[0])[-1] or "attachment"
            if name not in names:
                names.append(name)
    return ", ".join(names)


_SPACE_RUN = re.compile(r"[ \t]{2,}")
_LINE_EDGE_SPACES = re.compile(r"[ \t]*\n[ \t]*")


def derive_session_title(
    text: str,
    known_paths: Optional[Iterable[str]] = None,
    max_chars: int = 100,
    word_boundary: bool = True,
) -> str:
    """Turn a first user message into a session title.

    ``known_paths`` is the message's attachment list: ``None`` when the caller
    does not know it (leading tokens are then guessed and stripped), a list
    (possibly empty) when it does (only those references go; see the module
    docstring). What remains is truncated to ``max_chars`` code points.
    ``word_boundary`` keeps the two historical shapes: the 100-char sites
    cut at the last space before the ellipsis (the client copies this one),
    the older 50-char sites append the ellipsis to whatever the cut left.
    """
    if known_paths is None:
        body = strip_leading_attachment_tokens(text)
    else:
        known = list(known_paths)
        # Only attachments: name it by their files, never the raw paths.
        body = _cut_known_references(text, known) or _attachment_names(text, known)
    title = body[:max_chars].strip()
    if len(body) > max_chars:
        if word_boundary:
            title = title.rsplit(" ", 1)[0] + "..."
        else:
            title += "..."
    return title
