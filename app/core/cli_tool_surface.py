"""Last-seen Claude CLI tool surface, cached per install.

Why this exists
---------------
The claude CLI reads ``--allowed-tools`` as a PERMISSION grant, not a
restriction, so the delegated leg puts only the tools that should run
unprompted there (``harness_adapter._delegated_permission_grants``); every tool
not walled stays on the model's surface. To make "Allow only selected" mean the
same thing on both legs, the adapter passes ``--disallowed-tools`` = every name
in the CLI's OWN surface that the agent did not select. That requires knowing
the CLI's surface at argv-build time.

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
  translation branch. The CLI sends an init frame at the start of every turn,
  so this runs once per turn; it adds the announced names to the cache.
* ``cached_cli_tools()`` — what the adapter reads at argv-build time: every name
  seen so far plus ``KNOWN_CLI_TOOLS``.

The cache only grows. The CLI leaves ``--disallowed-tools`` entries out of its
init list, so a chat session or a restricted session announces a narrow surface.
Replacing the cache with that list made the next allow-mode session stop walling
Bash, Write and PowerShell. A stale name in ``--disallowed-tools`` is harmless,
so keeping every name ever seen costs nothing.

``KNOWN_CLI_TOOLS`` seeds the cache with the CLI built-ins this app knows about,
so the first session of a process is walled too. Before the seed, a cold cache
sent only the grant for that connect. The delegated PreToolUse hook
(``harness_adapter._delegated_tool_policy``) refuses tools outside the surface
either way; the seed keeps the model from seeing tools it may not call.

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

# Claude Code built-ins known to this app (CLI 2.1.285). A name missing from
# a given CLI build is harmless on --disallowed-tools; a name missing HERE is
# only walled once some session announces it.
KNOWN_CLI_TOOLS: tuple = (
    "Bash", "PowerShell", "Read", "Edit", "Write", "NotebookEdit", "Glob",
    "Grep", "LSP", "WebSearch", "WebFetch", "Skill", "ToolSearch",
    "AskUserQuestion", "RemoteTrigger", "PushNotification", "SendMessage",
    "ListAgents", "EnterWorktree", "ExitWorktree", "DesignSync", "ReportFindings",
)

_LOCK = threading.Lock()
_TOOLS: Optional[List[str]] = None


def record_cli_tools(names: Optional[Iterable[str]]) -> None:
    """Add the tool list a claude CLI ``init`` frame announced to the cache.

    The cache is a union: names are added, never removed, because the CLI omits
    disallowed tools from its init list (see the module docstring). Empty or
    None input is ignored.
    """
    global _TOOLS
    cleaned = [str(n).strip() for n in (names or ()) if str(n).strip()]
    if not cleaned:
        return
    with _LOCK:
        before = list(_TOOLS or ())
        merged = list(dict.fromkeys([*before, *cleaned]))
        _TOOLS = merged
    if len(merged) != len(before):
        logger.debug(
            "recorded claude CLI tool surface: %d tool(s) known", len(merged)
        )


def cached_cli_tools() -> List[str]:
    """The observed CLI surface plus ``KNOWN_CLI_TOOLS``, observed names first."""
    with _LOCK:
        observed = list(_TOOLS or ())
    return list(dict.fromkeys([*observed, *KNOWN_CLI_TOOLS]))


def reset_cli_tools() -> None:
    """Forget the observed surface (tests). The static seed stays."""
    global _TOOLS
    with _LOCK:
        _TOOLS = None
