"""Fluid-native ``settings.json`` stack — permissions, env, plugin enablement.

Fluid reads only its own ``.fluid`` trees during normal operation; provider
homes (``~/.claude``, ``~/.codex``) stay inert until explicitly imported via
:mod:`fluid.context.settings_importer`. The file FORMAT is Claude Code's
``settings.json`` schema, so an imported file works verbatim — only the paths
are fluid's own.

Scopes, lowest → highest precedence, named with the Agent SDK's
``setting_sources`` vocabulary:

  1. user     ``~/.fluid/settings.json``
  2. project  ``<dir>/.fluid/settings.json`` for every dir from the git root
              down to the cwd (outer → inner, inner wins); the cwd alone
              outside a git checkout (:func:`project_dirs`)
  3. local    ``<dir>/.fluid/settings.local.json`` (same walk, gitignored)

Callers pass the session's enabled sources (``HarnessConfig.setting_sources``)
to load only those scopes.

Merge rules: lists accumulate in scope order, dicts merge shallowly per key
with the higher scope winning a scalar conflict, scalars override outright.

What is consumed (wired by ``fluid.config.loader``):

* ``permissions.{allow,ask,deny}`` become rules evaluated before the
  ``config.toml`` ``[[permission.rules]]``, so a TOML rule outranks them.
* ``permissions.defaultMode`` sets ``permission.approval_policy`` (and, for
  ``acceptEdits``, the edit-tool allow rules) through the mode table in
  ``fluid.permissions.modes`` unless a config.toml/env/CLI layer sets one.
* ``env`` is layered onto the Bash builtin's child environment and each MCP
  stdio server's spawn environment (under the server's own ``env``);
  ``os.environ`` is never mutated.
* ``enabledPlugins``, ``enabledMcpjsonServers`` / ``disabledMcpServers``.

Not consumed: hooks (``hooks.json`` is the file for those), model/outputStyle/UI keys.

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

from fluid.context.instructions import _git_root, _walk_up_dirs
from fluid.permissions.modes import MODE_POLICY, PERMISSION_MODES, mode_rules
from fluid.permissions.rules import Rule

log = logging.getLogger(__name__)

# The valid scope names, in precedence order (mirrors config.model.SETTING_SOURCES).
SETTING_SOURCES = ("user", "project", "local")

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
# verbatim (see NativeLoop.submit): the mode table in
# ``fluid.permissions.modes``. Anything outside it is refused rather than
# approximated — see :func:`default_permission_mode`.
_PERMISSION_MODES = PERMISSION_MODES


# ─── loading ─────────────────────────────────────────────────────────────────


def _load_json(path: Path) -> dict[str, Any] | None:
    """Read one settings file. Returns None when absent, unreadable, or not a
    JSON object — never raises (CE11)."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        log.warning("skipping malformed settings file %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        log.warning("skipping settings file %s: expected a JSON object", path)
        return None
    return data


def project_dirs(cwd: Path | str | os.PathLike[str]) -> list[Path]:
    """The project walk for settings and config files.

    Inside a git checkout: every directory from the root down to ``cwd``,
    outer first. Outside one: ``cwd`` alone — a walk to the filesystem root
    would treat every ancestor (the home directory among them, so ``~/.fluid``
    twice: once as the user scope, once as a "project") as project config.
    The home directory is left out in both cases. Instruction files keep
    their own, wider walk (:func:`_walk_up_dirs`).
    """
    resolved = Path(cwd).resolve()
    dirs = _walk_up_dirs(resolved) if _git_root(resolved) is not None else [resolved]
    home = _resolved_home()
    return [d for d in dirs if d != home]


def _resolved_home() -> Path | None:
    try:
        return Path.home().resolve()
    except (OSError, RuntimeError):
        return None


def settings_paths(
    cwd: Path | str | os.PathLike[str], *, sources: tuple[str, ...] | list[str] = SETTING_SOURCES
) -> list[Path]:
    """Every candidate settings file for the enabled ``sources``, lowest
    precedence first.

    User scope, then the project walk (git root → cwd), then the local walk, so
    ``.fluid/settings.local.json`` anywhere outranks every ``settings.json``.
    """
    enabled = set(sources)
    paths: list[Path] = []
    if "user" in enabled:
        paths.append(Path.home() / ".fluid" / "settings.json")
    if "project" in enabled or "local" in enabled:
        dirs = project_dirs(cwd)
        if "project" in enabled:
            paths += [d / ".fluid" / "settings.json" for d in dirs]
        if "local" in enabled:
            paths += [d / ".fluid" / "settings.local.json" for d in dirs]
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


def load_settings_stack(
    cwd: Path | str | os.PathLike[str], *, sources: tuple[str, ...] | list[str] = SETTING_SOURCES
) -> dict[str, Any]:
    """The merged fluid settings visible from ``cwd`` for the enabled sources.

    Returns ``{}`` when no settings files exist — every discovery here
    soft-fails (CE8).
    """
    layers = [
        d
        for d in (_load_json(p) for p in settings_paths(cwd, sources=tuple(sources)))
        if d is not None
    ]
    return merge_settings(layers)


# ─── accessors ───────────────────────────────────────────────────────────────


def enabled_plugins(settings: dict[str, Any]) -> dict[str, bool]:
    """The merged ``enabledPlugins`` map (``"<plugin>@<marketplace>" → bool``).

    Consumed by plugin discovery; exposed here so the settings reader owns the
    whole on-disk shape in one place.
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
    ``SetPermissionMode`` handler accepts ``default`` / ``acceptEdits`` /
    ``bypassPermissions`` verbatim — so a recognized value
    passes straight through.

    Anything else (``dontAsk``, a future mode, a typo) returns ``None`` with a
    warning rather than a guess: silently mapping an unknown mode onto the
    nearest known one could widen a user's permissions, which is exactly the
    failure this table must not have.

    The loader applies :func:`default_mode_policy` as the lowest config layer,
    so a config.toml, ``FLUID_APPROVAL_POLICY`` or ``--approval-policy`` value
    still wins.
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


def default_mode_policy(settings: dict[str, Any]) -> tuple[str | None, list[Rule]]:
    """``permissions.defaultMode`` as ``(approval_policy, extra rules)``.

    The policy comes from ``fluid.permissions.modes.MODE_POLICY``; the rules
    are the allow entries ``acceptEdits`` implies for the edit-class tools
    (``ACCEPT_EDITS_TOOLS`` there), empty for every other mode. ``(None, [])``
    when the mode is unset or unsupported.
    """
    mode = default_permission_mode(settings)
    if mode is None:
        return None, []
    return MODE_POLICY[mode], mode_rules(mode)


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
        log.warning("skipping unparseable permission entry: %r", entry)
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
    from fluid.tools.child_env import scrub_claude_auth_env

    return scrub_claude_auth_env(env)
