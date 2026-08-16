"""Claude Code ``settings.json`` reader — permissions, env, plugin enablement.

Ruling CE4. A user who already configured Claude Code keeps that configuration
when they run fluid: the same three scopes are read from disk, merged with the
same precedence, and mapped onto fluid's own primitives.

Scopes, lowest → highest precedence:

  1. user     ``~/.claude/settings.json``
  2. project  ``<dir>/.claude/settings.json`` for every dir from the git root
              down to the cwd (outer → inner, inner wins)
  3. local    ``<dir>/.claude/settings.local.json`` (same walk, gitignored)

Merge rules: lists accumulate in scope order, dicts merge shallowly per key
with the higher scope winning a scalar conflict, scalars override outright.

What v1 consumes: ``permissions.{allow,ask,deny}``, ``env``, ``enabledPlugins``,
``enabledMcpjsonServers`` / ``disabledMcpServers``. ``permissions.defaultMode``
is *mapped* here (:func:`default_permission_mode`) but not yet applied to a
session — see the note on that function's caller-side precedence question.
Not consumed: hooks, model/outputStyle/UI keys.

Nothing here raises (CE11): a malformed or unreadable settings file is skipped
with a ``logging`` warning, because session assembly must never fail on it.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any

from fluid.context.instructions import _walk_up_dirs
from fluid.permissions.rules import Rule

log = logging.getLogger(__name__)

# Claude Code's permission entries: ``Tool(pattern)`` or a bare ``Tool``.
_RULE_RE = re.compile(r"^\s*(?P<tool>[^()\s]+)\s*(?:\((?P<pattern>.*)\)\s*)?$", re.DOTALL)

# Which input field a tool's pattern constrains. Tools outside this table get a
# tool-wide rule: fluid cannot guess which argument the pattern is about, and a
# too-narrow guess would silently fail open.
_PATTERN_FIELD: dict[str, str] = {
    "Bash": "command",
    "Read": "file_path",
    "Write": "file_path",
    "Edit": "file_path",
}

# allow → ask → deny, so fluid's last-match-wins evaluation reproduces Claude's
# deny > ask > allow precedence.
_DECISION_ORDER = ("allow", "ask", "deny")

# ``permissions.defaultMode`` values fluid's SetPermissionMode op understands
# verbatim (see NativeLoop.submit). Anything outside this set is refused rather
# than approximated — see :func:`default_permission_mode`.
_PERMISSION_MODES = ("default", "plan", "acceptEdits", "bypassPermissions")


# ─── loading ─────────────────────────────────────────────────────────────────


def _load_json(path: Path) -> dict[str, Any] | None:
    """Read one settings file. Returns None when absent, unreadable, or not a
    JSON object — never raises (CE11)."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("skipping malformed Claude settings file %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("skipping Claude settings file %s: expected a JSON object", path)
        return None
    return data


def settings_paths(cwd: Path | str) -> list[Path]:
    """Every candidate settings file, lowest precedence first.

    User scope, then the project walk (git root → cwd), then the local walk, so
    ``.claude/settings.local.json`` anywhere outranks every ``settings.json``.
    """
    dirs = _walk_up_dirs(Path(cwd))
    paths = [Path.home() / ".claude" / "settings.json"]
    paths += [d / ".claude" / "settings.json" for d in dirs]
    paths += [d / ".claude" / "settings.local.json" for d in dirs]
    return paths


def merge_settings(layers: list[dict[str, Any]]) -> dict[str, Any]:
    """Merge settings layers, lowest precedence first.

    Lists accumulate in order (duplicates dropped, first occurrence wins the
    position), dicts merge shallowly per key, scalars override.
    """
    merged: dict[str, Any] = {}
    for layer in layers:
        for key, value in layer.items():
            prior = merged.get(key)
            if isinstance(value, list) and isinstance(prior, list):
                merged[key] = prior + [v for v in value if v not in prior]
            elif isinstance(value, dict) and isinstance(prior, dict):
                merged[key] = merge_settings([prior, value])
            else:
                merged[key] = value
    return merged


def load_claude_settings(cwd: Path | str) -> dict[str, Any]:
    """The merged Claude Code settings visible from ``cwd``.

    Returns ``{}`` when the user has no Claude Code setup at all — every
    discovery here soft-fails (CE8).
    """
    layers = [d for d in (_load_json(p) for p in settings_paths(cwd)) if d is not None]
    return merge_settings(layers)


# ─── accessors ───────────────────────────────────────────────────────────────


def enabled_plugins(settings: dict[str, Any]) -> dict[str, bool]:
    """The merged ``enabledPlugins`` map (``"<plugin>@<marketplace>" → bool``).

    Consumed by plugin discovery in W3; exposed now so the settings reader owns
    the whole on-disk shape in one place.
    """
    raw = settings.get("enabledPlugins")
    if not isinstance(raw, dict):
        return {}
    return {str(k): bool(v) for k, v in raw.items()}


def settings_env(settings: dict[str, Any]) -> dict[str, str]:
    """The merged ``env`` map, stringified.

    Layered over child-process environments (Bash builtin, MCP stdio spawn);
    ``os.environ`` is never mutated.
    """
    raw = settings.get("env")
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if v is not None}


def default_permission_mode(settings: dict[str, Any]) -> str | None:
    """``permissions.defaultMode`` as a fluid ``SetPermissionMode`` value.

    Claude Code and fluid already share this vocabulary — ``NativeLoop``'s
    ``SetPermissionMode`` handler accepts ``default`` / ``plan`` /
    ``acceptEdits`` / ``bypassPermissions`` verbatim — so a recognized value
    passes straight through.

    Anything else (``dontAsk``, a future mode, a typo) returns ``None`` with a
    warning rather than a guess: silently mapping an unknown mode onto the
    nearest known one could widen a user's permissions, which is exactly the
    failure this table must not have.

    Applying the result to a session is deliberately NOT done here or in
    ``NativeLoop`` yet: the setting must lose to an explicit host choice, and
    the engine currently has no way to tell a host that deliberately passed
    ``PermissionEngine("on_write")`` from one that simply took the default —
    both arrive as an approval policy with a value. Wiring it without that
    signal could silently override a host's configured policy.
    """
    perms = settings.get("permissions")
    if not isinstance(perms, dict):
        return None
    raw = perms.get("defaultMode")
    if raw is None:
        return None
    mode = str(raw).strip()
    if mode in _PERMISSION_MODES:
        return mode
    log.warning("ignoring unsupported permissions.defaultMode %r", raw)
    return None


def mcp_toggles(settings: dict[str, Any]) -> tuple[list[str], list[str]]:
    """``(enabledMcpjsonServers, disabledMcpServers)`` as name lists."""

    def _names(key: str) -> list[str]:
        raw = settings.get(key)
        if not isinstance(raw, list):
            return []
        return [str(v) for v in raw]

    return _names("enabledMcpjsonServers"), _names("disabledMcpServers")


# ─── permissions ─────────────────────────────────────────────────────────────


def parse_permission_entry(entry: str, decision: str) -> Rule | None:
    """Parse one ``Tool(pattern)`` / bare ``Tool`` entry into a fluid ``Rule``.

    The pattern narrows the rule only for tools whose constrained input field is
    known (:data:`_PATTERN_FIELD`); anything else — an unknown tool, an MCP
    server name, a bare tool name — becomes a tool-wide rule so the entry still
    has its intended effect rather than being dropped.
    """
    if not isinstance(entry, str) or not entry.strip():
        return None
    match = _RULE_RE.match(entry)
    if match is None:
        log.warning("skipping unparseable Claude permission entry: %r", entry)
        return None
    tool = match.group("tool")
    pattern = match.group("pattern")
    field = _PATTERN_FIELD.get(tool)
    if pattern and field:
        return Rule(tool=tool, decision=decision, input_globs={field: pattern})
    return Rule(tool=tool, decision=decision)


def permission_rules(settings: dict[str, Any]) -> list[Rule]:
    """``permissions.{allow,ask,deny}`` as fluid rules, in evaluation order.

    Emitted allow → ask → deny: fluid's ``evaluate_rules`` is last-match-wins,
    so a path listed in both ``allow`` and ``deny`` ends up denied, matching
    Claude Code's deny > ask > allow precedence.
    """
    perms = settings.get("permissions")
    if not isinstance(perms, dict):
        return []
    rules: list[Rule] = []
    for decision in _DECISION_ORDER:
        entries = perms.get(decision)
        if not isinstance(entries, list):
            continue
        for entry in entries:
            rule = parse_permission_entry(entry, decision)
            if rule is not None:
                rules.append(rule)
    return rules


# ─── child-process env ───────────────────────────────────────────────────────


def layered_env(extra: dict[str, str] | None, base: dict[str, str] | None = None) -> dict[str, str]:
    """``base`` (default: a copy of ``os.environ``) with ``extra`` layered on top.

    A plain copy — the process environment is never mutated, so one session's
    settings can't leak into another's subprocesses.
    """
    env = dict(os.environ if base is None else base)
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env
