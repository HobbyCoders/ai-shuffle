"""Agent definitions — ``.claude/agents`` + plugin agents (ruling CE6).

A user who already wrote subagent definitions for Claude Code keeps them when
they run fluid: the same markdown files are read off disk, from the same three
scopes, with the same ``<plugin>:<name>`` namespacing.

Scopes, in precedence order:

  1. project  ``<dir>/.claude/agents/**/*.md`` for every dir from the git root
              down to the cwd — the directory NEAREST the cwd wins a name
              collision (the most specific definition is the one in effect);
  2. global   ``~/.claude/agents/**/*.md`` — loses to any project definition of
              the same name;
  3. plugin   an enabled plugin's ``agents/**/*.md`` (``fluid.context.plugins``),
              named ``<plugin>:<agent>`` and therefore collision-free.

Unlike skills, definitions are loaded EAGERLY: an agent file is a
description plus a system prompt (a few hundred tokens), so there is nothing to
gain from progressive disclosure and a dispatcher needs the body the moment it
spawns the agent.

What fluid does with them (CE6, and no more than this):

  * :func:`fluid.context.compiler.agents_index` advertises name+description in
    the system prompt, so the model knows what it can delegate to;
  * :class:`~fluid.engine.native.NativeLoop` publishes the full definitions on
    ``ctx.extra["agent_registry"]`` for the host's Task dispatcher.

Dispatch itself stays host-app territory (rulings A6/A8 — ``tools/builtins/
task.py``): fluid discovers, surfaces and exposes; it never spawns.

Nothing here raises: an unreadable or malformed file is skipped with a
``logging`` warning (CE11).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from fluid.context.instructions import _read_opt, _unquote, _walk_up_dirs, parse_frontmatter

log = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class AgentDefinition:
    """One agent definition file, fully read.

    ``raw`` keeps every frontmatter field as parsed, including the ones this
    version does not act on (``permissionMode``, ``maxTurns``, ``memory``,
    ``effort``, ``color``, …) — a host dispatcher that understands them can
    read them straight off the record instead of re-parsing the file.
    """

    name: str
    description: str = ""
    path: str
    scope: str = "project"  # "global" | "project" | "plugin"
    model: str | None = None
    tools: tuple[str, ...] = ()  # empty = inherit the parent's whole toolset
    body: str = ""  # the markdown body IS the agent's system prompt
    raw: dict[str, Any] = field(default_factory=dict)


def _as_text(value: Any) -> str:
    """A frontmatter value as display text (a list joins with commas)."""
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return "" if value is None else str(value)


def split_tools(value: Any) -> tuple[str, ...]:
    """The ``tools:`` field as a tuple, from either frontmatter shape.

    ``tools: [Read, Grep]`` arrives as a list (``parse_frontmatter``'s inline
    array), ``tools: Read, Grep`` as one string — both mean the same thing, so
    both comma-split. An absent or empty field means "inherit everything",
    which is the empty tuple.
    """
    if isinstance(value, (list, tuple)):
        items = [str(v) for v in value]
    elif isinstance(value, str):
        items = value.split(",")
    else:
        return ()
    return tuple(cleaned for item in items if (cleaned := _unquote(item)))


def load_agent_definition(
    path: str | os.PathLike[str], *, scope: str = "project", name: str | None = None
) -> AgentDefinition | None:
    """Read one agent file. ``None`` (with a warning) when it cannot be read.

    ``name`` overrides the discovered name — plugin agents arrive already
    namespaced ``<plugin>:<agent>`` and must keep that name.
    """
    p = Path(path)
    text = _read_opt(p)
    if text is None:
        log.warning("skipping unreadable agent definition %s", p)
        return None
    try:
        fields, body = parse_frontmatter(text)
    except Exception as exc:  # noqa: BLE001 — CE11: discovery never raises
        log.warning("skipping malformed agent definition %s: %s", p, exc)
        return None
    model = _as_text(fields.get("model")).strip() or None
    return AgentDefinition(
        name=name or _as_text(fields.get("name")).strip() or p.stem,
        description=_as_text(fields.get("description")),
        path=str(p),
        scope=scope,
        model=model,
        tools=split_tools(fields.get("tools")),
        body=body,
        raw=dict(fields),
    )


def _agents_in(base: Path, scope: str) -> dict[str, AgentDefinition]:
    """Every definition under one ``agents`` directory, keyed by name.

    The scan is recursive (Claude Code lets agents sit in subdirectories). A
    name claimed twice inside the SAME tree keeps the first in sorted order —
    the shallower/earlier file — so the result does not depend on walk order.
    """
    out: dict[str, AgentDefinition] = {}
    if not base.is_dir():
        return out
    try:
        files = sorted(base.rglob("*.md"))
    except OSError as exc:
        log.warning("cannot list agents dir %s: %s", base, exc)
        return out
    for path in files:
        definition = load_agent_definition(path, scope=scope)
        if definition is not None:
            out.setdefault(definition.name, definition)
    return out


def discover_agents(
    cwd: str | os.PathLike[str],
    *,
    include_global: bool = True,
    include_plugins: bool = True,
    plugins: Sequence[Any] | None = None,
) -> list[AgentDefinition]:
    """Every agent definition visible from ``cwd``, sorted by name.

    Precedence (CE6): a project definition beats a global one of the same name,
    and among project directories the one NEAREST the cwd wins — the walk runs
    outermost → cwd, so a later (inner) directory overwrites an outer one's
    entry. Plugin agents are namespaced and can collide with nothing.

    ``plugins`` passes an already-loaded plugin list (the engine reads it once
    per session); leave it None to let discovery load them itself.
    """
    by_name: dict[str, AgentDefinition] = {}
    for directory in _walk_up_dirs(Path(cwd)):
        # Nearest-wins: the walk is outermost → cwd, so the inner directory's
        # entry replaces the outer one's under the same key.
        by_name.update(_agents_in(directory / ".claude" / "agents", "project"))
    if include_global:
        for name, definition in _agents_in(
            Path.home() / ".claude" / "agents", "global"
        ).items():
            by_name.setdefault(name, definition)

    records = plugins if plugins is not None else (_plugins(cwd) if include_plugins else [])
    for plugin in records:
        for agent in getattr(plugin, "agents", ()):
            # Bodies are unread on the PluginAgent record (progressive
            # disclosure suits skills, not agents) — load it here.
            definition = load_agent_definition(agent.path, scope="plugin", name=agent.name)
            if definition is not None:
                by_name.setdefault(definition.name, definition)
    return sorted(by_name.values(), key=lambda d: d.name)


def _plugins(cwd: str | os.PathLike[str]) -> list[Any]:
    """Enabled plugins, imported lazily (plugins imports instructions)."""
    from fluid.context.plugins import discover_plugins

    return discover_plugins(cwd)
