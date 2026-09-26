"""Last-seen Claude CLI tool surface, cached per install.

Why this exists
---------------
On the delegated leg the app hands the claude CLI ``--allowed-tools``, which the
CLI reads as a PERMISSION grant, not a restriction: an unlisted tool is still on
the model's surface and still callable (it just prompts). To make "Allow only
selected" mean the same thing on both legs, the adapter must also pass
``--disallowed-tools`` = every name in the CLI's OWN surface that the agent did
not select. That requires knowing the CLI's surface at argv-build time.

Where the surface comes from
----------------------------
The CLI announces it in the ``{"type":"system","subtype":"init"}`` frame — but
only AFTER the first user message of a session, so the list is never knowable at
spawn time on a cold process. The vendored ``StreamJsonMapper``
(fluid/engine/delegated_proto.py) reads the tool list out of that frame and
emits it as a ``DelegatedSurface`` event that ``DelegatedLoop`` publishes, so
there IS a live feed; it just arrives too late
to shape the argv of the connect that produced it.

Reconnecting mid-session to apply a subtraction learned from the init frame is
NOT acceptable — it would drop the CLI's session and re-prime its prompt cache.
So this module is a plain module-level cache with an explicit ingest point:

* ``record_cli_tools(names)`` — called from the adapter's ``DelegatedSurface``
  translation branch, i.e. once per CLI process start. Refreshes the cache in
  place.
* ``cached_cli_tools()`` — what the adapter reads at argv-build time.

On a cold cache only ``--allowed-tools`` (a grant, not a wall) goes out for
that connect, and the next observation fills the cache. The delegated PreToolUse
hook (``harness_adapter._delegated_tool_policy``) still refuses tools outside the
surface, so a cold cache weakens only the argv wall.

"none" mode does not need any of this: the Claude Code CLI glob-matches deny
rules, so a single ``--disallowed-tools "*"`` denies every tool. Deny/ask/allow
lookups treat any rule containing ``*`` as a glob matched against the whole
tool name; the single exemption is the CLI's internal end-conversation tool
(verified against the CLI once bundled with claude-agent-sdk 0.2.126; re-check
on CLI upgrades).

The cache is per PROCESS, which is per install — every install runs its own CLI
binary and its own MCP config, so surfaces legitimately differ between them and
must never be shared through the database.
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable, List, Optional

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_TOOLS: Optional[List[str]] = None


def record_cli_tools(names: Optional[Iterable[str]]) -> None:
    """Remember the tool list a claude CLI ``init`` frame announced.

    Ignores empty/None input: a frame without a tool list tells us nothing, and
    blanking a good cache would silently turn the surface wall back off.
    """
    global _TOOLS
    cleaned = [str(n).strip() for n in (names or ()) if str(n).strip()]
    if not cleaned:
        return
    with _LOCK:
        changed = cleaned != _TOOLS
        _TOOLS = cleaned
    if changed:
        logger.debug("recorded claude CLI tool surface: %d tool(s)", len(cleaned))


def cached_cli_tools() -> Optional[List[str]]:
    """The last-seen CLI surface, or None when nothing has been observed yet."""
    with _LOCK:
        return list(_TOOLS) if _TOOLS else None


def reset_cli_tools() -> None:
    """Forget the cached surface (tests)."""
    global _TOOLS
    with _LOCK:
        _TOOLS = None
