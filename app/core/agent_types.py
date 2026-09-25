"""App-owned turn configuration, permission decisions and hook payloads.

Only host/Fluid boundary data lives here; no client, subprocess or credentials.
The legacy option names remain accepted while callers use connect_with_sdk_options.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from app.core.provider.types import SessionOptions


@dataclass
class AgentOptions(SessionOptions):
    model: str | None = None
    extra_args: dict[str, str | None] = field(default_factory=dict)
    settings: str | None = None
    resume: str | None = None
    can_use_tool: Callable | None = None


@dataclass
class PermissionResultAllow:
    updated_input: dict[str, Any] | None = None
    behavior: str = field(default="allow", init=False)


@dataclass
class PermissionResultDeny:
    message: str = ""
    interrupt: bool = False
    behavior: str = field(default="deny", init=False)


@dataclass
class HookMatcher:
    matcher: str | None = None
    hooks: list[Callable] = field(default_factory=list)
    timeout: float | None = None


# CLI hook payloads are open JSON objects; unknown fields must survive round trips.
HookInput = dict[str, Any]
HookContext = dict[str, Any]
SyncHookJSONOutput = dict[str, Any]
PreToolUseHookSpecificOutput = dict[str, Any]
PostToolUseHookSpecificOutput = dict[str, Any]
