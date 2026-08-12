"""Permission rules — last-match-wins glob patterns (opencode-style).

A rule matches on a glob over the tool name and, optionally, globs over
stringified input fields (so ``Bash`` with ``command: rm *`` can be denied while
other Bash calls are allowed). Rules are evaluated in order; the last match
wins, which lets a broad allow be overridden by a later narrow deny.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from fnmatch import fnmatch
from typing import Any


@dataclass(frozen=True, kw_only=True)
class Rule:
    tool: str  # glob on tool name, e.g. "Bash", "mcp__*", "*"
    decision: str  # "allow" | "deny" | "ask"
    input_globs: dict[str, str] = field(default_factory=dict)  # field → glob on str(value)

    def matches(self, name: str, input: dict[str, Any]) -> bool:
        if not fnmatch(name, self.tool):
            return False
        for key, pat in self.input_globs.items():
            if not fnmatch(str(input.get(key, "")), pat):
                return False
        return True


def evaluate_rules(rules: list[Rule], name: str, input: dict[str, Any]) -> str | None:
    """Return the last matching rule's decision, or None if none match."""
    result: str | None = None
    for rule in rules:
        if rule.matches(name, input):
            result = rule.decision
    return result
