"""Builtin tools, registered with bare names (Read/Edit/Bash/Grep/Write)."""

from __future__ import annotations

from omni.tools.builtins import bash, edit, grep, note, read, skill, task, write
from omni.tools.registry import ToolRegistry

_MODULES = (read, grep, write, edit, bash, skill, note, task)


def register_builtins(registry: ToolRegistry) -> None:
    """Register all builtin tools on ``registry``."""
    for mod in _MODULES:
        registry.register_builtin(mod.SPEC, mod.handler)


__all__ = ["register_builtins"]
