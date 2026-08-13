"""PermissionEngine — approval policy × explicit rules, plus a doom-loop guard.

Two independent inputs decide whether a tool call proceeds:
- the **approval policy** axis (``never_ask`` / ``on_write`` / ``always_ask``),
  which keys off the tool's declared ``risk``;
- explicit **rules** (last-match-wins) that override the policy for specific
  tool/input patterns.

``note_execution`` is the doom-loop detector: three identical ``(name, input)``
calls in a row force one ``ask`` checkpoint (then reset), breaking the
"model repeats a failing call forever" pattern.

``plan_mode_tools`` implements schema-level plan mode: a read-only session
simply never sees write/destructive tools in its request schema.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Sequence

from omni.permissions.rules import Rule, evaluate_rules
from omni.tools.registry import ToolSpec

_WRITE_RISKS = {"write", "destructive"}


def _input_hash(input: dict[str, Any]) -> str:
    blob = json.dumps(input, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


class PermissionEngine:
    def __init__(
        self,
        approval_policy: str = "on_write",
        rules: list[Rule] | None = None,
        *,
        doom_loop_threshold: int = 3,
    ) -> None:
        assert approval_policy in ("never_ask", "on_write", "always_ask")
        self.approval_policy = approval_policy
        self.rules = rules or []
        self._threshold = doom_loop_threshold
        self._last_key: tuple[str, str] | None = None
        self._streak = 0
        # Session-scoped "allow always" responses (see allow_always()).
        self._always_allowed: set[str] = set()

    def evaluate(self, spec: ToolSpec, input: dict[str, Any]) -> str:
        """Return ``allow`` | ``deny`` | ``ask`` for one tool call."""
        override = evaluate_rules(self.rules, spec.name, input)
        if override is not None:
            return override
        if spec.name in self._always_allowed:
            return "allow"
        return self._policy_decision(spec)

    def _policy_decision(self, spec: ToolSpec) -> str:
        if self.approval_policy == "never_ask":
            return "allow"
        if self.approval_policy == "always_ask":
            return "ask"
        # on_write
        return "ask" if spec.risk in _WRITE_RISKS else "allow"

    def allow_always(self, name: str) -> None:
        """Record a session-scoped allow for ``name`` (the ``allow_always``
        permission response). Beats the policy's ask for future calls, but
        never an explicit configured rule — those keep last-match-wins."""
        self._always_allowed.add(name)

    def note_execution(self, name: str, input: dict[str, Any]) -> bool:
        """Record a call; return True when it should be force-asked (doom loop).

        Three identical calls in a row trip the guard once, then the streak
        resets so the very next distinct call runs normally.
        """
        key = (name, _input_hash(input))
        if key == self._last_key:
            self._streak += 1
        else:
            self._last_key = key
            self._streak = 1
        if self._streak >= self._threshold:
            self._streak = 0
            return True
        return False


def plan_mode_tools(tools: Sequence[ToolSpec]) -> list[ToolSpec]:
    """Filter write/destructive tools out of a read-only session's schema."""
    return [t for t in tools if t.risk not in _WRITE_RISKS]
