"""Permission rules — last-match-wins glob patterns (opencode-style).

A rule matches on a glob over the tool name and, optionally, globs over
stringified input fields (so ``Bash`` with ``command: rm *`` can be denied while
other Bash calls are allowed). Rules are evaluated in order; the last match
wins, which lets a broad allow be overridden by a later narrow deny.

Three things make the field match more than a bare ``fnmatch``:

- Globs are case-sensitive on every platform (``fnmatchcase``), so a policy
  file means the same thing on Windows as on Linux.
- A pattern ending in ``:*`` is Claude Code's prefix form: ``curl:*`` matches
  ``curl`` and ``curl <anything>`` but not ``curlx``. It applies to the
  non-path fields (``command``); a path pattern keeps ``:`` literal so
  ``C:*`` still means drive C.
- ``Bash`` ``command`` patterns are matched per simple command. ``npm run *``
  does not cover ``npm run build && rm -rf /``: an allow rule matches only
  when every simple command matches, and never when the line uses command
  substitution or a subshell (the splitter cannot see what those run). A deny
  or ask rule matches when any simple command does.
- Path fields (``file_path``, ``path``, ...) are matched on their normalized
  spelling as well as the raw one, so ``.env/``, ``.env `` (trailing space)
  and ``./x/../.env`` all reach the rule written for ``.env``. An allow rule
  only sees the normalized spellings: a raw ``src/../.env`` must not slip
  through ``src/**``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Any

# Input fields that carry a filesystem path. The tool resolves the path after
# the permission check, so the rule has to see the resolved spelling too.
PATH_FIELDS = frozenset({"file_path", "path", "notebook_path", "directory"})


def _normalize_pattern(pattern: str) -> str:
    text = pattern.replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    return text


def _is_prefix_pattern(pattern: str) -> bool:
    return len(pattern) > 2 and pattern.endswith(":*")


def pattern_matches(value: str, pattern: str) -> bool:
    """One field value against one rule pattern (glob or ``prefix:*``)."""
    if _is_prefix_pattern(pattern):
        prefix = pattern[:-2]
        return value == prefix or value.startswith(prefix + " ")
    return fnmatchcase(value, pattern)


def path_candidates(value: str, cwd: str | None = None) -> tuple[set[str], set[str]]:
    """``(raw, normalized)`` spellings of a path-bearing input value.

    Normalized spellings: trailing whitespace and separators dropped,
    separators unified to ``/``, ``.`` and ``..`` segments collapsed, the
    absolute form (against ``cwd`` when given), the real path with symlinks
    followed, and the ``cwd``-relative form when the path sits under ``cwd``.
    """
    raw = {value}
    text = value.strip().replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    if not text:
        return raw, set()
    normalized = {os.path.normpath(text).replace("\\", "/")}
    absolute = text if os.path.isabs(text) else (os.path.join(cwd, text) if cwd else None)
    if absolute:
        forms = {os.path.normpath(absolute)}
        try:
            forms.add(os.path.realpath(absolute))
        except (OSError, ValueError):
            pass
        for form in list(forms):
            normalized.add(form.replace("\\", "/"))
            if cwd:
                try:
                    root = os.path.normpath(cwd)
                    if os.path.commonpath([root, form]) == root:
                        normalized.add(os.path.relpath(form, root).replace("\\", "/"))
                except ValueError:  # different drives
                    pass
    return raw, normalized


def _path_matches(value: str, pattern: str, decision: str, cwd: str | None) -> bool:
    raw, normalized = path_candidates(value, cwd)
    candidates = normalized if decision == "allow" else raw | normalized
    patterns = {pattern, _normalize_pattern(pattern)}
    return any(fnmatchcase(c, p) for c in candidates for p in patterns)


def _command_matches(value: str, pattern: str, decision: str) -> bool:
    from fluid.tools.builtins.bash_risk import simple_commands

    parts = simple_commands(value)
    if decision == "allow":
        return bool(parts) and all(pattern_matches(part, pattern) for part in parts)
    if pattern_matches(value, pattern):
        return True
    return any(pattern_matches(part, pattern) for part in parts or ())


@dataclass(frozen=True, kw_only=True)
class Rule:
    tool: str  # glob on tool name, e.g. "Bash", "mcp__*", "*"
    decision: str  # "allow" | "deny" | "ask"
    input_globs: dict[str, str] = field(default_factory=dict)  # field → glob on str(value)

    def matches(self, name: str, input: dict[str, Any], *, cwd: str | None = None) -> bool:
        if not fnmatchcase(name, self.tool):
            return False
        for key, pat in self.input_globs.items():
            value = str(input.get(key, ""))
            if key in PATH_FIELDS:
                ok = _path_matches(value, pat, self.decision, cwd)
            elif name == "Bash" and key == "command":
                ok = _command_matches(value, pat, self.decision)
            else:
                ok = pattern_matches(value, pat)
            if not ok:
                return False
        return True


def match_rule(
    rules: list[Rule], name: str, input: dict[str, Any], *, cwd: str | None = None
) -> Rule | None:
    """Return the last matching rule, or None if none match."""
    result: Rule | None = None
    for rule in rules:
        if rule.matches(name, input, cwd=cwd):
            result = rule
    return result


def evaluate_rules(
    rules: list[Rule], name: str, input: dict[str, Any], *, cwd: str | None = None
) -> str | None:
    """Return the last matching rule's decision, or None if none match."""
    rule = match_rule(rules, name, input, cwd=cwd)
    return rule.decision if rule is not None else None
