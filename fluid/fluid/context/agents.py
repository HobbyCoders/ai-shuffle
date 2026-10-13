"""Fluid-native agent definition discovery.

Definitions live in ``~/.fluid/agents/**/*.md`` globally and
``<directory>/.fluid/agents/**/*.md`` (or the codex-native
``.agents/agents`` tree) at every project scope from git root to cwd. The
nearest project definition wins a name collision (``.fluid`` beats ``.agents``
in the same directory); every project definition wins over the global one, and
plugin agents — namespaced ``<plugin>:<name>`` — never shadow either. Files
use the existing :class:`AgentDefinition` parser and precedence semantics.

Unlike skills, definitions are loaded eagerly because a Task host needs the
worker's complete system prompt when it dispatches. Provider homes
(``~/.claude``) are never read; use the explicit settings import to copy
definitions into these native locations.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fluid.context.instructions import (
    _read_opt,
    _unquote,
    _walk_up_dirs,
    parse_frontmatter,
)

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
    # ``disallowedTools``: tools the worker may never use, whatever it
    # inherits. Task dispatch sends it as the worker's deny list.
    disallowed_tools: tuple[str, ...] = ()
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
    path: str | os.PathLike[str],
    *,
    scope: str = "project",
    name: str | None = None,
    plugin: Any | None = None,
) -> AgentDefinition | None:
    """Read one agent file. ``None`` (with a warning) when it cannot be read.

    ``name`` overrides the discovered name — plugin agents arrive already
    namespaced ``<plugin>:<agent>`` and must keep that name.

    A ``scope="plugin"`` definition is adapted by
    :func:`fluid.context.plugin_import.normalize_agent_fields`: no model pin,
    foreign tool names mapped, unused fields removed. ``None`` when nothing in
    its tool list exists here. ``plugin`` (the loaded plugin) expands
    ``${CLAUDE_PLUGIN_ROOT}``/``${PLUGIN_ROOT}`` in the body.
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
    if scope == "plugin":
        from fluid.context.plugin_import import expand_plugin_paths, normalize_agent_fields

        adapted = normalize_agent_fields(
            fields,
            plugin_name=str(getattr(plugin, "name", "") or ""),
            server_names=tuple(getattr(plugin, "mcp_servers", {}) or ()),
        )
        if adapted.skip_reason:
            log.info("skipping plugin agent %s: %s", p, adapted.skip_reason)
            return None
        fields = adapted.fields
        root = getattr(plugin, "root", None)
        if root is not None:
            from fluid.context.plugins import plugin_data_dir

            body = expand_plugin_paths(
                body, root=root,
                data=plugin_data_dir(str(plugin.name), str(getattr(plugin, "marketplace", ""))),
            )
    model = _as_text(fields.get("model")).strip() or None
    return AgentDefinition(
        name=name or _as_text(fields.get("name")).strip() or p.stem,
        description=_as_text(fields.get("description")),
        path=str(p),
        scope=scope,
        model=model,
        tools=split_tools(fields.get("tools")),
        disallowed_tools=split_tools(fields.get("disallowedTools", fields.get("disallowed_tools"))),
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
    include_project: bool = True,
    include_plugins: bool = False,
    plugins: Sequence[Any] | None = None,
) -> list[AgentDefinition]:
    """Every agent definition visible from ``cwd``, sorted by name.

    A project definition beats a global one of the same name, and among project
    directories the one NEAREST the cwd wins — the walk runs outermost → cwd, so
    a later (inner) directory overwrites an outer one's entry. In one directory
    ``.fluid/agents`` beats ``.agents/agents``. ``include_plugins`` appends
    enabled plugins' agents (namespaced ``<plugin>:<name>``, so they never
    shadow a native definition); ``plugins`` reuses an already-discovered list.
    """
    by_name: dict[str, AgentDefinition] = {}
    if include_project:
        for directory in _walk_up_dirs(Path(cwd)):
            # Nearest-wins: the walk is outermost → cwd, so the inner
            # directory's entry replaces the outer one's under the same key —
            # and ``.fluid`` is applied after ``.agents`` so it wins in-place.
            by_name.update(_agents_in(directory / ".agents" / "agents", "project"))
            by_name.update(_agents_in(directory / ".fluid" / "agents", "project"))
    if include_global:
        for name, definition in _agents_in(
            Path.home() / ".fluid" / "agents", "global"
        ).items():
            by_name.setdefault(name, definition)
    if include_plugins:
        if plugins is None:
            from fluid.context.plugins import discover_plugins

            plugins = discover_plugins(cwd)
        for plugin in plugins:
            for stub in getattr(plugin, "agents", ()):
                definition = load_agent_definition(
                    stub.path, scope="plugin", name=stub.name, plugin=plugin
                )
                if definition is not None:
                    by_name.setdefault(definition.name, definition)

    return sorted(by_name.values(), key=lambda d: d.name)
