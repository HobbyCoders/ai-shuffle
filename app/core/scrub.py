"""Credential scrubbing for text on its way into durable storage.

A model can paste a full ``DATABASE_URL`` — password included — into a
code-mode cell. The cell text rides the normal transcript path into
``fluid_session_nodes`` (and, via the tool-call events that carry the same
text, into ``fluid_session_events``), where it is permanently persisted.
Unredacted, the secret is readable by anything that can read the session
store: recall search, session export, a DB dump, a backup.

This module is the redaction boundary. It is deliberately tiny and dependency
free so the persistence layers can call it from inside their existing jsonb
adaptation without taking on a new import graph — it composes with
``app.core.pg_session_store._scrub_pg_illegal`` the same way, as one more
pure recursive pass over the payload just before ``Json(...)`` wraps it.

Scope: WRITE PATH ONLY, and only for newly persisted rows. Rows already in the
database keep whatever they were written with; retro-scrubbing them is a data
migration that needs an explicit human decision, not a side effect of a code
change.
"""

from __future__ import annotations

import re
from typing import Any

# What a URL-embedded credential looks like, and — more importantly — what it
# must NOT be confused with.
#
#   group 1  ``scheme://[user]:``   the part that is kept verbatim
#   the rest ``password``           the part replaced by ``***``
#   lookahead ``@``                 the delimiter that proves it was userinfo
#
# Everything about the shape below exists to avoid mangling text that merely
# resembles a credential:
#
# * A ``scheme://`` prefix is REQUIRED, so ssh-style git remotes
#   (``git@github.com:HobbyCoders/ai-shuffle.git``) never match — they have no
#   ``://`` at all.
# * A trailing ``@`` is REQUIRED (lookahead, so it stays in the output), so
#   ``http://user@host`` — a userinfo with no password — is left alone: there
#   is no ``:`` between the user and the ``@``.
# * ``/`` and whitespace are excluded from BOTH halves, which is what keeps
#   host:port URLs intact. In ``http://localhost:8080/api`` the candidate
#   "password" ``8080`` is terminated by the ``/`` before any ``@`` can appear,
#   so the match fails and the URL is untouched.
# * Quotes and bracket/brace characters are excluded too. Minified JSON such as
#   ``{"a":"http://x.com:80","b":"u@h"}`` would otherwise let a greedy password
#   run across the string boundary and reach an unrelated ``@``.
# * The user half may be EMPTY (``*`` not ``+``) so password-only forms like
#   ``redis://:pw@host:6379/0`` are still caught.
#
# Only the password is replaced, so the scheme, the user, the host, the port
# and the path all survive and the text stays readable and diagnosable.
_URL_CREDENTIALS_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9+.\-]*://[^\s:/?#@\"'<>\[\]{}\\^|]*:)"
    r"[^\s/@\"'<>\[\]{}\\^|]+"
    r"(?=@)"
)

REDACTED = "***"

_REPLACEMENT = r"\g<1>" + REDACTED


def scrub_url_credentials_text(text: str) -> str:
    """Replace the password of every URL-embedded credential in *text*.

    ``postgres://appuser:s3cr3t@db:5432/appdb`` becomes
    ``postgres://appuser:***@db:5432/appdb``. Text with nothing to redact is
    returned as the SAME object — the hot path (virtually every string that
    reaches this function) does one failed regex scan and no allocation.
    """
    if _URL_CREDENTIALS_RE.search(text) is None:
        return text
    return _URL_CREDENTIALS_RE.sub(_REPLACEMENT, text)


def scrub_url_credentials(value: Any) -> Any:
    """Recursively scrub URL-embedded credentials in every string of *value*.

    Mirrors the shape of ``_scrub_pg_illegal``: strings are rewritten, dicts
    and lists are walked, other scalars pass through, and an untouched subtree
    is returned without copying.

    Dict KEYS are deliberately left alone. A key is structure, not content —
    ``Message.from_dict`` and the event codec both look keys up by name, and a
    rewritten key would silently break decoding. Secrets pasted by a model land
    in values (cell text, tool arguments, tool output), never in a key.
    """
    if isinstance(value, str):
        return scrub_url_credentials_text(value)
    if isinstance(value, dict):
        return {k: scrub_url_credentials(v) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_url_credentials(v) for v in value]
    return value


# The whole userinfo of a URL, user included. Unlike ``_URL_CREDENTIALS_RE``
# this also matches a user with no password: a clone URL routinely carries a
# token in that position (``https://ghp_xxx@github.com/o/r.git``). Scoped to
# git remotes and their error text, where no ``scheme://name@`` is benign
# enough to keep. ssh-style ``git@host:path`` has no ``://`` and is untouched.
_URL_USERINFO_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9+.\-]*://)[^\s/?#@\"'<>\[\]{}\\^|]+(?=@)"
)


def redact_url_credentials(text: str) -> str:
    """Replace the userinfo of every URL in *text* (a git remote, argv or stderr).

    ``https://user:pw@host/r.git`` and ``https://token@host/r.git`` both become
    ``https://***@host/r.git``; the host and path stay readable.
    """
    if not isinstance(text, str) or _URL_USERINFO_RE.search(text) is None:
        return text
    return _URL_USERINFO_RE.sub(r"\g<1>" + REDACTED, text)
