"""Builtin tools, registered with bare names (Read/Edit/Bash/Grep/Glob/Write)."""

from __future__ import annotations

from omni.tools.builtins import (
    bash, edit, glob, grep, note, read, skill, task, tool_search, write,
)
from omni.tools.registry import ToolRegistry

_MODULES = (read, grep, glob, write, edit, bash, skill, note, task, tool_search)


def register_builtins(registry: ToolRegistry) -> None:
    """Register all builtin tools on ``registry``."""
    for mod in _MODULES:
        registry.register_builtin(mod.SPEC, mod.handler)


__all__ = ["register_builtins"]
