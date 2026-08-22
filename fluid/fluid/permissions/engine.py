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
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fluid.permissions.rules import Rule, match_rule
from fluid.tools.registry import ToolSpec

_WRITE_RISKS = {"write", "destructive"}


def _input_hash(input: dict[str, Any]) -> str:
    blob = json.dumps(input, sort_keys=True, default=str)
    return hashlib.sha1(blob.encode()).hexdigest()


@dataclass(frozen=True)
class Verdict:
    """One tool call's permission outcome plus where it came from.

    ``source`` is ``rule`` (a configured rule matched — ``rule`` names it),
    ``session`` (an earlier ``allow_always``), or ``policy``. ``risk`` is the
    effective risk the policy saw, after the tool's per-input classifier.
    """

    decision: str  # "allow" | "deny" | "ask"
    source: str
    risk: str
    rule: Rule | None = None

    def deny_message(self) -> str:
        """Model-facing text for a denial from this verdict."""
        if self.source == "rule" and self.rule is not None:
            return (
                f"permission denied by a configured rule (tool pattern "
                f"{self.rule.tool!r}). This call will not be allowed in this "
                "session; do not retry the same action through another tool."
            )
        return "permission denied"


NON_INTERACTIVE_DENIAL = (
    "permission denied: this non-interactive run cannot prompt. Re-run with "
    "--approval-policy never_ask, or add a permission rule for {name} in "
    ".fluid/config.toml. Do not retry the same action through another tool."
)
PROMPT_DENIAL = (
    "permission denied by the user at the prompt. Do not retry the same "
    "action through another tool; ask the user how to proceed if it matters."
)


def non_interactive_denial(name: str, reason: str | None = None) -> str:
    """Denial text for a responder that has nobody to ask (``fluid run``)."""
    text = NON_INTERACTIVE_DENIAL.format(name=name)
    return f"{text} ({reason})" if reason else text


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
        # tool name → labels allowed (None = every call to the tool).
        self._always_allowed: dict[str, set[str] | None] = {}

    def evaluate(self, spec: ToolSpec, input: dict[str, Any]) -> str:
        """Return ``allow`` | ``deny`` | ``ask`` for one tool call."""
        return self.decide(spec, input).decision

    def decide(self, spec: ToolSpec, input: dict[str, Any]) -> Verdict:
        """Like :meth:`evaluate`, but says where the decision came from.

        The policy axis keys off the call's *effective* risk — the tool's
        ``classify_risk`` hook when it has one (Bash reports read-only
        commands as safe), its static ``risk`` otherwise.
        """
        risk = spec.effective_risk(input)
        rule = match_rule(self.rules, spec.name, input)
        if rule is not None:
            return Verdict(rule.decision, "rule", risk, rule)
        if self._session_allows(spec, input):
            return Verdict("allow", "session", risk)
        return Verdict(self._policy_decision(risk), "policy", risk)

    def _policy_decision(self, risk: str) -> str:
        if self.approval_policy == "never_ask":
            return "allow"
        if self.approval_policy == "always_ask":
            return "ask"
        # on_write
        return "ask" if risk in _WRITE_RISKS else "allow"

    def allow_always(
        self, name: str, scope: frozenset[str] | None = None
    ) -> None:
        """Record a session-scoped allow for ``name`` (the ``allow_always``
        permission response). Beats the policy's ask for future calls, but
        never an explicit configured rule — those keep last-match-wins.

        ``scope`` narrows the allow to labels the tool derives from the call
        (Bash: the programs it runs), so allowing ``git status`` once covers
        later ``git`` commands but not ``rm``. None covers every call."""
        if scope is None:
            self._always_allowed[name] = None
            return
        current = self._always_allowed.get(name, set())
        if current is None:
            return
        current.update(scope)
        self._always_allowed[name] = current

    def _session_allows(self, spec: ToolSpec, input: dict[str, Any]) -> bool:
        if spec.name not in self._always_allowed:
            return False
        allowed = self._always_allowed[spec.name]
        if allowed is None:
            return True
        scope = spec.scope_of(input)
        return scope is not None and scope <= allowed

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
