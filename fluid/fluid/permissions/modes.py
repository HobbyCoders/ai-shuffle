"""Claude-Code-style permission modes as fluid policy plus rules.

Clients (the CLI, embedding apps, ``permissions.defaultMode`` in a
settings.json) speak Claude Code's mode names; fluid's :class:`PermissionEngine`
speaks an approval policy (``never_ask`` / ``on_write`` / ``always_ask``) plus
rules. This module is the one translation table, so the settings loader, the
``SetPermissionMode`` op and any host deciding a prompt on the engine's behalf
agree on what each mode means.

``acceptEdits`` is the mode that needs more than a policy: it keeps the
``on_write`` policy (asks for every write-class call) and adds allow rules for
the edit-class tools, so a file edit runs without a prompt while a shell
command, a Python cell, a web fetch or any other write-class tool still asks
exactly as ``default`` does.
"""

from __future__ import annotations

from fluid.permissions.rules import Rule

# The edit-class tools ``acceptEdits`` approves on its own. This is the whole
# set, matching what the mode means in Claude Code: the file-editing tools
# (edit, create or rewrite a file, edit a notebook). ``Edit`` and ``Write`` are
# fluid's own builtins; ``MultiEdit`` and ``NotebookEdit`` are the Claude Code
# spellings a delegated CLI or a settings file can name. Nothing else belongs
# here: ``Bash``, ``run_python`` (an unsandboxed kernel is a shell, not an
# edit), ``WebFetch``, memory writes, skill saves and screen control are all
# write-class calls the mode still asks about. Hosts extend this set only
# through :func:`accept_edits_rules` / their own tables built from it — never by
# spelling the names again.
ACCEPT_EDITS_TOOLS: frozenset[str] = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})

# mode → approval policy. ``acceptEdits`` differs from ``default`` only through
# :func:`accept_edits_rules`.
MODE_POLICY: dict[str, str] = {
    "default": "on_write",
    "acceptEdits": "on_write",
    "bypassPermissions": "never_ask",
}

# The mode names fluid understands verbatim, in the order clients list them.
PERMISSION_MODES: tuple[str, ...] = tuple(MODE_POLICY)


def accept_edits_rules() -> list[Rule]:
    """Fresh tool-wide allow rules for :data:`ACCEPT_EDITS_TOOLS`.

    A new list of new ``Rule`` objects each call, so an engine can track the
    rules it installed for a mode by identity and lift exactly those again.
    """
    return [Rule(tool=tool, decision="allow") for tool in sorted(ACCEPT_EDITS_TOOLS)]


def mode_rules(mode: str) -> list[Rule]:
    """The rules ``mode`` implies on top of its policy: the edit allows for
    ``acceptEdits``, nothing for every other mode or policy name."""
    return accept_edits_rules() if mode == "acceptEdits" else []


__all__ = [
    "ACCEPT_EDITS_TOOLS",
    "MODE_POLICY",
    "PERMISSION_MODES",
    "accept_edits_rules",
    "mode_rules",
]
