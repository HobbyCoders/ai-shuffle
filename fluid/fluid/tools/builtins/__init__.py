"""Builtin tools, registered with bare names (Read/Edit/Bash/Grep/Glob/Write/Monitor)."""

from __future__ import annotations

from fluid.tools.builtins import (
    ask_user,
    bash,
    edit,
    glob,
    grep,
    history_expand,
    history_search,
    ledger,
    memory,
    monitor,
    note,
    read,
    session_search,
    skill,
    task,
    tool_search,
    write,
)
from fluid.tools.registry import ToolRegistry

_MODULES = (
    read, grep, glob, write, edit, bash, skill, note, task, monitor,
    tool_search, history_expand, history_search, ledger,
    memory, session_search, ask_user,
)


def register_builtins(registry: ToolRegistry) -> None:
    """Register all builtin tools on ``registry``."""
    for mod in _MODULES:
        registry.register_builtin(mod.SPEC, mod.handler)


__all__ = ["register_builtins"]
