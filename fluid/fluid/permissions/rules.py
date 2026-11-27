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
  through ``src/**``. The real path of the value exactly as the tool opens
  it is checked too: on Linux ``settings `` is not ``settings``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from pathlib import Path
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


def _opened_path(value: str, cwd: str | None) -> str | None:
    """The real path the file tools open for ``value``, or None.

    Mirrors ``fluid.tools.builtins._util.resolve``: the value exactly as given
    (no stripping), joined to the session cwd when relative, symlinks
    followed. The cwd is joined as given, not collapsed first: in
    ``/work/link/..`` the ``..`` applies after ``link`` is followed. On Linux
    ``settings `` (trailing space) is a different file from ``settings``;
    Windows drops the trailing space itself, so there this equals the
    normalized spelling. A missing path or a dangling link resolves to where
    Write would create the file. A symlink loop resolves as far as it can
    (best effort, for ask and deny rules); :func:`_openable` keeps an allow
    rule from matching it.
    """
    if not value:
        return None
    try:
        path = Path(value)
        if not path.is_absolute():
            path = Path(cwd or os.getcwd()) / path
        return os.path.realpath(path)
    except (OSError, ValueError):
        return None


def _openable(path: str) -> bool:
    """False when ``path`` cannot be reached (a loop, EACCES, ENOTDIR).

    A missing file is openable: Write creates it.
    """
    try:
        os.stat(path)
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        return False
    return True


def _resolved_forms(resolved: str, root: str, relative: bool) -> set[str]:
    """A resolved path's absolute spelling and, under ``root``, its relative one."""
    forms = {resolved.replace("\\", "/")}
    if relative:
        try:
            if os.path.commonpath([root, resolved]) == root:
                forms.add(os.path.relpath(resolved, root).replace("\\", "/"))
        except ValueError:  # Different drives have no relative form.
            pass
    return forms


def _opened_forms(value: str, cwd: str | None, *, for_allow: bool) -> set[str] | None:
    """:func:`_opened_path` as rule candidates; None when it cannot resolve.

    Ask and deny rules get every form, best effort. An allow rule gets the
    absolute form, plus the cwd-relative one only when the cwd as spelled is
    the real cwd, and nothing for a path that cannot be reached.
    """
    opened = _opened_path(value, cwd)
    if opened is None or (for_allow and not _openable(opened)):
        return None
    relative = bool(cwd) or not os.path.isabs(value)
    base = cwd or os.getcwd()
    spelled = os.path.abspath(base)
    if for_allow and relative:
        # ``work/link/..`` collapses to ``work`` but is really the link's
        # parent; a form relative to ``work`` would name the wrong file.
        try:
            relative = os.path.normcase(spelled) == os.path.normcase(os.path.realpath(base))
        except (OSError, ValueError):
            relative = False
    forms = _resolved_forms(opened, spelled, relative)
    if not for_allow:
        # The opened path has no links in it, so under a symlinked cwd only
        # the real directory gives the relative form ``secrets/*`` is written
        # for. Not for allow: a relative form that lost its link can name a
        # different file than the cwd as spelled does.
        try:
            forms |= _resolved_forms(opened, os.path.realpath(base), relative)
        except (OSError, ValueError):
            pass
    return forms


def path_candidates(value: str, cwd: str | None = None) -> tuple[set[str], set[str]]:
    """``(raw, normalized)`` spellings of a path-bearing input value.

    Normalized spellings: trailing whitespace and separators dropped,
    separators unified to ``/``, ``.`` and ``..`` segments collapsed, the
    absolute form (against ``cwd`` when given), the real path with symlinks
    followed, and the ``cwd``-relative form when the path sits under ``cwd``.
    The real path of the value exactly as the tool opens it (unstripped) is
    included too, so a trailing-space symlink cannot hide its target.
    """
    raw = {value}
    exact = (
        _opened_forms(value, cwd, for_allow=False) if cwd or os.path.isabs(value) else None
    ) or set()
    text = value.strip().replace("\\", "/")
    while len(text) > 1 and text.endswith("/"):
        text = text[:-1]
    if not text:
        return raw, exact
    normalized = {os.path.normpath(text).replace("\\", "/"), *exact}
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
    if not any(fnmatchcase(c, p) for c in candidates for p in patterns):
        return False
    if decision != "allow":
        return True

    # A lexical match must not override an outside resolved target. Match the
    # resolved spelling independently (absolute and cwd-relative), never mixed
    # with the lexical candidates above. This is a permission preflight, NOT a
    # race-safe filesystem broker: the tool still opens the file separately.
    text = value.strip().replace("\\", "/")
    try:
        root = os.path.abspath(cwd or os.getcwd())
        resolved = os.path.realpath(os.path.join(root, text))
    except (OSError, ValueError):
        return False
    resolved_candidates = _resolved_forms(resolved, root, bool(cwd) or not os.path.isabs(text))
    if not any(fnmatchcase(c, p) for c in resolved_candidates for p in patterns):
        return False
    # The file the tool actually opens must be inside too: ``granted/notes ``
    # (trailing space) can link outside while ``granted/notes`` resolves in.
    opened = _opened_forms(value, cwd, for_allow=True)
    return opened is not None and any(fnmatchcase(c, p) for c in opened for p in patterns)


def _command_matches(value: str, pattern: str, decision: str) -> bool:
    from fluid.tools.builtins.bash_risk import simple_commands

    if decision == "allow":
        # Stricter reading: the program each part names must be the one the
        # shell runs, in a shell this module can read (not cmd.exe).
        parts = simple_commands(value, for_allow=True)
        return bool(parts) and all(pattern_matches(part, pattern) for part in parts)
    parts = simple_commands(value)
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
