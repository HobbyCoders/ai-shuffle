"""Make a plugin from another tool behave as a native one.

Claude Code and Codex plugins carry fields that only mean something in the
tool they were written for. A subagent pinned to one model is the clearest
case: here the model is chosen per chat (with app defaults under Settings >
Default models), so a pin must not travel with the plugin. Tool names differ
too (Codex's ``apply_patch`` is this app's ``Edit``).

Plugins stay on disk exactly as they were published. These rules run every
time a plugin is READ — by the runtime loader, by agent discovery and by the
app's plugin list — so the same adaptation applies to a plugin from any
source (a marketplace install or update, a settings import, a project
folder) and can never be skipped by a path that forgot to call it.

Every adaptation and every skipped part is recorded as a plain sentence. The
plugin list shows them on the component they belong to, and an install
reports them, so nothing is changed or dropped silently.

Nothing here grants anything. A tool list that maps to nothing is NOT turned
into "every tool" (an empty list means unrestricted here): that agent is
skipped instead.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The tool names a plugin subagent may list here: fluid's builtins plus the
# app's own builtins that a subagent can be handed. ``mcp__<server>__<tool>``
# names are accepted as written.
NATIVE_TOOL_NAMES: frozenset[str] = frozenset({
    "Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebFetch", "WebSearch",
    "Task", "Monitor", "Skill", "AskUser", "ToolSearch", "Note", "Ledger",
    "Remember", "Recall", "Forget", "SessionSearch", "SessionView",
    "HistorySearch", "HistoryExpand", "ListAgents", "SendMessage", "GetUsage",
})

# Foreign spelling -> native name, or ``None`` when there is no equivalent.
# Claude Code first, then Codex (open-source tool names, Apache-2.0).
FOREIGN_TOOL_NAMES: Mapping[str, str | None] = {
    # Claude Code
    "MultiEdit": "Edit",
    "NotebookEdit": "Edit",
    "NotebookRead": "Read",
    "LS": "Glob",
    "Agent": "Task",
    "AskUserQuestion": "AskUser",
    "PowerShell": "Bash",
    "BashOutput": None,
    "KillShell": None,
    "KillBash": None,
    "TodoWrite": None,
    "TodoRead": None,
    "ExitPlanMode": None,
    "EnterPlanMode": None,
    "SlashCommand": None,
    "LSP": None,
    # Codex
    "shell": "Bash",
    "local_shell": "Bash",
    "exec_command": "Bash",
    "unified_exec": "Bash",
    "container.exec": "Bash",
    "apply_patch": "Edit",
    "read_file": "Read",
    "list_dir": "Glob",
    "grep_files": "Grep",
    "web_search": "WebSearch",
    "view_image": "Read",
    "update_plan": None,
    "write_stdin": None,
}

# Agent frontmatter this app reads. Everything else is removed with a note.
AGENT_KEPT_FIELDS = frozenset({"name", "description", "tools"})

_PIN_FIELDS = frozenset({
    "model", "effort", "reasoning_effort", "model_reasoning_effort",
    "provider", "model_provider", "modelProvider",
})
_AUTHORITY_FIELDS = frozenset({"permissionMode", "permission_mode", "sandbox_mode", "approval_policy"})
_COMPONENT_FIELDS = frozenset({"hooks", "mcpServers", "mcp_servers"})

_SPECIFIER_RE = re.compile(r"^(?P<name>[A-Za-z_][A-Za-z0-9_.]*)\((?P<spec>.*)\)$")
_USER_CONFIG_RE = re.compile(r"\$\{user_config\.([A-Za-z_][A-Za-z0-9_]*)\}")
_ROOT_TOKEN_RE = re.compile(r"\$\{(CLAUDE_PLUGIN_ROOT|PLUGIN_ROOT|CLAUDE_PLUGIN_DATA|PLUGIN_DATA)\}")


@dataclass(frozen=True)
class AgentImport:
    """What reading one plugin subagent here changed.

    ``fields`` is the frontmatter the app acts on. ``skip_reason`` is set when
    the agent cannot be offered at all.
    """

    fields: dict[str, Any]
    notes: list[str] = field(default_factory=list)
    skip_reason: str = ""


def _items(value: Any) -> list[str]:
    if isinstance(value, (list, tuple)):
        raw = [str(v) for v in value]
    elif isinstance(value, str):
        raw = value.split(",")
    else:
        return []
    return [s.strip().strip("\"'") for s in raw if s.strip().strip("\"'")]


def map_tool(name: str, *, plugin_name: str = "", server_names: Iterable[str] = ()) -> tuple[str | None, str]:
    """``(native name or None, note)`` for one tool a foreign plugin listed.

    The note is empty when the name is already native.
    """
    if name in NATIVE_TOOL_NAMES:
        return name, ""
    # Claude Code names a plugin's MCP tools mcp__plugin_<plugin>_<server>__<tool>;
    # here a plugin server's tools are mcp__<server>__<tool>.
    prefix = f"mcp__plugin_{plugin_name}_" if plugin_name else ""
    if prefix and name.startswith(prefix):
        server, sep, tool = name[len(prefix):].partition("__")
        if sep and tool and server in set(server_names):
            native = f"mcp__{server}__{tool}"
            return native, f"{name} became {native}"
    if name.startswith("mcp__"):
        return name, ""
    spec = _SPECIFIER_RE.match(name)
    if spec:
        base, _ = map_tool(spec.group("name"), plugin_name=plugin_name, server_names=server_names)
        if base is None:
            return None, f"{name} was dropped: there is no {spec.group('name')} tool here"
        return base, f"{name} became {base}: per-command limits in a tool list are not enforced here"
    if name in FOREIGN_TOOL_NAMES:
        native = FOREIGN_TOOL_NAMES[name]
        if native is None:
            return None, f"{name} was dropped: this app has no equivalent tool"
        return native, f"{name} became {native}"
    return None, f"{name} was dropped: no tool by that name exists here"


def map_tools(
    values: Any, *, plugin_name: str = "", server_names: Iterable[str] = ()
) -> tuple[list[str], list[str]]:
    """Map a ``tools`` list. Returns ``(kept names, notes)``, order kept, no duplicates."""
    kept: list[str] = []
    notes: list[str] = []
    servers = list(server_names)
    for raw in _items(values):
        native, note = map_tool(raw, plugin_name=plugin_name, server_names=servers)
        if note:
            notes.append(note)
        if native and native not in kept:
            kept.append(native)
    return kept, notes


def normalize_agent_fields(
    fields: Mapping[str, Any], *, plugin_name: str = "", server_names: Iterable[str] = ()
) -> AgentImport:
    """Adapt one plugin subagent's frontmatter to this app.

    * model, effort and provider pins are removed: the model is chosen per chat;
    * permission, sandbox, hook and MCP fields are removed: an agent file
      never sets its own authority or adds components;
    * ``tools`` names are mapped (or dropped with a note). When the author
      listed tools and none exist here, the agent is skipped: an empty list
      would mean "every tool", more than the author asked for;
    * any other field is removed as unused.
    """
    out: dict[str, Any] = {}
    notes: list[str] = []
    removed_unused: list[str] = []
    for key in fields:
        if key in AGENT_KEPT_FIELDS:
            continue
        if key in _PIN_FIELDS:
            notes.append(f"{key} pin removed: the model is chosen per chat here")
        elif key in _AUTHORITY_FIELDS:
            notes.append(f"{key} removed: a plugin agent runs with the permissions of the chat that starts it")
        elif key in _COMPONENT_FIELDS:
            notes.append(f"{key} removed: an agent file cannot add hooks or MCP servers")
        elif key in ("disallowedTools", "disallowed_tools"):
            notes.append(f"{key} removed: this app does not enforce a deny list for plugin agents")
        else:
            removed_unused.append(str(key))
    if removed_unused:
        notes.append("not used here, removed: " + ", ".join(sorted(removed_unused)))
    for key in ("name", "description"):
        if key in fields:
            out[key] = fields[key]
    skip = ""
    if "tools" in fields:
        listed = _items(fields.get("tools"))
        kept, tool_notes = map_tools(listed, plugin_name=plugin_name, server_names=server_names)
        notes.extend(tool_notes)
        if listed and not kept:
            skip = (
                "none of the tools it lists exist here, and an empty tool list "
                "would give it every tool, so it is not offered"
            )
        elif kept:
            out["tools"] = kept
    return AgentImport(fields=out, notes=notes, skip_reason=skip)


def markdown_notes(fields: Mapping[str, Any], body: str, *, kind: str) -> list[str]:
    """Notes for a plugin skill or command: what in it does not apply here."""
    notes: list[str] = []
    for key in sorted(_PIN_FIELDS & set(fields)):
        notes.append(f"{key} pin ignored: the model is chosen per chat here")
    if "hooks" in fields:
        notes.append(f"hooks in the {kind} frontmatter do not run here")
    if kind == "skill" and ("context" in fields or "agent" in fields):
        notes.append("context/agent fields are ignored: the skill runs in the chat that loads it")
    refs = sorted(set(_USER_CONFIG_RE.findall(body)))
    if refs:
        notes.append(
            "asks for plugin settings this app does not collect ("
            + ", ".join(refs) + "); those references stay as written"
        )
    return notes


def expand_plugin_paths(text: str, *, root: Path | str, data: Path | str | None = None) -> str:
    """Replace ``${CLAUDE_PLUGIN_ROOT}``/``${PLUGIN_ROOT}`` (and the data
    variables) in Markdown content with real paths, as Claude Code does when it
    loads a skill, command or agent. Only these four names are touched; any
    other ``${...}`` stays literal so no environment value leaks into a prompt.
    """
    root_text = str(root).replace("\\", "/")
    data_text = str(data).replace("\\", "/") if data is not None else None

    def repl(match: re.Match[str]) -> str:
        if match.group(1).endswith("ROOT"):
            return root_text
        return data_text if data_text is not None else match.group(0)

    return _ROOT_TOKEN_RE.sub(repl, text)


def summarize(components: Iterable[Any]) -> dict[str, list[str]]:
    """``{"adapted": [...], "skipped": [...]}`` from a plugin's components.

    A loaded component with a reason was adapted; any other support value was
    skipped. Each line names the component.
    """
    adapted: list[str] = []
    skipped: list[str] = []
    for comp in components:
        kind = getattr(comp, "kind", None) or (comp.get("kind") if isinstance(comp, dict) else "")
        name = getattr(comp, "name", None) or (comp.get("name") if isinstance(comp, dict) else "")
        support = getattr(comp, "support", None) or (comp.get("support") if isinstance(comp, dict) else "")
        reason = getattr(comp, "reason", None)
        if reason is None and isinstance(comp, dict):
            reason = comp.get("reason")
        label = f"{str(kind).replace('_', ' ')} {name}"
        if support == "loaded":
            if reason:
                adapted.append(f"{label}: {reason}")
        else:
            skipped.append(f"{label}: {reason or 'not supported here'}")
    return {"adapted": adapted, "skipped": skipped}
