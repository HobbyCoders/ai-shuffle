"""
Custom Slash Command Discovery and Execution

This module handles discovering and parsing custom slash commands
from .claude/commands/ directories in project working directories.
"""

import logging
import re
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass, field
import yaml

logger = logging.getLogger(__name__)


@dataclass
class SlashCommand:
    """Represents a custom slash command"""
    name: str  # Command name without leading /
    description: str
    content: str  # The prompt content
    file_path: str  # Source file path
    source: str  # "project" or "user"
    namespace: Optional[str] = None  # Subdirectory namespace

    # Frontmatter options
    allowed_tools: List[str] = field(default_factory=list)
    argument_hint: Optional[str] = None
    model: Optional[str] = None
    disable_model_invocation: bool = False
    type: str = "custom"
    user_invocable: bool = True

    def get_display_name(self) -> str:
        """Get display name with leading /"""
        return f"/{self.name}"

    def get_description_with_source(self) -> str:
        """Get description with source indicator"""
        source_indicator = f"({self.source}"
        if self.namespace:
            source_indicator += f":{self.namespace}"
        source_indicator += ")"
        return f"{self.description} {source_indicator}"

    def expand_prompt(self, arguments: str = "") -> str:
        """
        Expand the command prompt with arguments.

        Supports:
        - $ARGUMENTS - All arguments as a single string
        - $1, $2, etc. - Individual positional arguments
        """
        prompt = self.content

        from fluid.context.instructions import split_command_args

        args = split_command_args(arguments)

        def substitute(match):
            if match.group(1) is None:
                return arguments
            index = int(match.group(1)) - 1
            return args[index] if 0 <= index < len(args) else ""

        return re.sub(r"\$ARGUMENTS\b|\$(\d+)", substitute, prompt).strip()


def parse_frontmatter(content: str) -> tuple[Dict[str, Any], str]:
    """
    Parse YAML frontmatter from markdown content.

    Returns (frontmatter_dict, remaining_content)
    """
    if not content.startswith("---"):
        return {}, content

    # Find the closing ---
    lines = content.split("\n")
    end_index = -1
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end_index = i
            break

    if end_index == -1:
        return {}, content

    # Parse YAML frontmatter
    frontmatter_text = "\n".join(lines[1:end_index])
    remaining_content = "\n".join(lines[end_index + 1:]).strip()

    try:
        frontmatter = yaml.safe_load(frontmatter_text) or {}
    except yaml.YAMLError as e:
        logger.warning(f"Failed to parse frontmatter: {e}")
        frontmatter = {}

    if not isinstance(frontmatter, dict):
        logger.warning("Ignoring non-mapping command frontmatter")
        frontmatter = {}
    return frontmatter, remaining_content


def parse_command_file(file_path: Path, source: str, namespace: Optional[str] = None) -> Optional[SlashCommand]:
    """
    Parse a slash command markdown file.

    Args:
        file_path: Path to the .md file
        source: "project" or "user"
        namespace: Optional subdirectory namespace

    Returns:
        SlashCommand object or None if parsing fails
    """
    try:
        content = file_path.read_text(encoding="utf-8")
    except Exception as e:
        logger.error(f"Failed to read command file {file_path}: {e}")
        return None

    # Parse frontmatter
    frontmatter, prompt_content = parse_frontmatter(content)

    # Get command name from filename
    name = file_path.stem  # filename without .md extension

    # Get description from frontmatter or first line
    description = frontmatter.get("description", "")
    if not description and prompt_content:
        # Use first line as description
        first_line = prompt_content.split("\n")[0].strip()
        # Remove markdown headers
        description = re.sub(r'^#+\s*', '', first_line)[:100]

    # Parse allowed-tools (can be string or list)
    allowed_tools = frontmatter.get("allowed-tools", [])
    if isinstance(allowed_tools, str):
        allowed_tools = [t.strip() for t in allowed_tools.split(",")]

    # Parse argument-hint (normalize to string if it's a list)
    argument_hint = frontmatter.get("argument-hint")
    if isinstance(argument_hint, list):
        # Join list items with space - common format is [arg1] [arg2]
        argument_hint = " ".join(f"[{item}]" if not item.startswith("[") else item for item in argument_hint)

    return SlashCommand(
        name=name,
        description=description,
        content=prompt_content,
        file_path=str(file_path),
        source=source,
        namespace=namespace,
        allowed_tools=allowed_tools,
        argument_hint=argument_hint,
        model=frontmatter.get("model"),
        disable_model_invocation=frontmatter.get("disable-model-invocation", False),
        user_invocable=frontmatter.get("user-invocable", True) is not False,
    )


def _scan_commands_directory(
    base_dir: Path,
    source: str,
    namespace: Optional[str] = None
) -> List[SlashCommand]:
    """
    Recursively scan a commands directory for .md files.

    Args:
        base_dir: Directory to scan
        source: "project" or "user"
        namespace: Current namespace (subdirectory name)

    Returns:
        List of SlashCommand objects
    """
    commands = []

    try:
        for item in base_dir.iterdir():
            if item.is_file() and item.suffix == ".md":
                cmd = parse_command_file(item, source, namespace)
                if cmd:
                    commands.append(cmd)
            elif item.is_dir() and not item.name.startswith("."):
                # Recurse into subdirectory with namespace
                sub_namespace = item.name if not namespace else f"{namespace}/{item.name}"
                commands.extend(_scan_commands_directory(item, source, sub_namespace))
    except PermissionError as e:
        logger.warning(f"Permission denied accessing {base_dir}: {e}")
    except Exception as e:
        logger.error(f"Error scanning commands directory {base_dir}: {e}")

    return commands


def _delegated_commands(working_dir: str, setting_sources=None) -> List[SlashCommand]:
    """Read Claude's selected settings and package roots, never Fluid's plugins."""
    from fluid.context.plugins import discover_plugins
    from fluid.context.settings_stack import _load_json, merge_settings, project_dirs
    from app.core.plugin_models import _get_claude_dir

    sources = ("user", "project", "local") if setting_sources is None else setting_sources
    user_dir = _get_claude_dir()
    dirs = project_dirs(working_dir)
    settings_paths = []
    roots = []
    if "user" in sources:
        settings_paths.append(user_dir / "settings.json")
        roots.append((user_dir, "user"))
    if "project" in sources:
        settings_paths.extend(directory / ".claude" / "settings.json" for directory in dirs)
        roots.extend((directory / ".claude", "project") for directory in dirs)
    if "local" in sources:
        settings_paths.extend(directory / ".claude" / "settings.local.json" for directory in dirs)
    settings = merge_settings([data for path in settings_paths if (data := _load_json(path))])
    by_name = {}
    for root, source in roots:
        for cmd in _scan_commands_directory(root / "commands", source) if (root / "commands").is_dir() else []:
            by_name[cmd.name] = cmd
        for skill_md in sorted((root / "skills").glob("*/SKILL.md")):
            cmd = parse_command_file(skill_md, source)
            if cmd:
                cmd.name = skill_md.parent.name
                cmd.type = "skill"
                if cmd.user_invocable:
                    by_name[cmd.name] = cmd
    for plugin in discover_plugins(working_dir, settings=settings, home=user_dir / "plugins"):
        for kind, entries in (("plugin", plugin.commands), ("skill", plugin.skills)):
            for entry in entries:
                cmd = parse_command_file(Path(entry.path), plugin.ref, namespace=plugin.name)
                if cmd:
                    cmd.name = entry.name
                    cmd.type = kind
                    if kind != "skill" or cmd.user_invocable:
                        by_name.setdefault(cmd.name, cmd)
    return list(by_name.values())


def _runtime_commands(working_dir, provider_kind, setting_sources):
    if provider_kind in ("voice", "unavailable"):
        return []
    discover = _delegated_commands if provider_kind == "delegated" else _fluid_commands
    return discover(working_dir, setting_sources)


def _fluid_commands(working_dir: str, setting_sources=None) -> List[SlashCommand]:
    """Use the native runtime's discovery, including its plugin settings stack."""
    from fluid.context.instructions import discover_commands as native_commands
    from fluid.context.instructions import discover_skills as native_skills
    from fluid.context.plugins import discover_plugins

    sources = ("user", "project", "local") if setting_sources is None else setting_sources
    user_on = "user" in sources
    project_on = "project" in sources
    # Native prompt compilation and the Skill tool gate plugins on user scope.
    plugins = discover_plugins(working_dir, sources=sources) if user_on else []
    options = dict(include_global=user_on, include_project=project_on,
                   include_plugins=True, plugins=plugins)
    plugin_sources = {entry.path: plugin.ref for plugin in plugins
                      for entry in [*plugin.commands, *plugin.skills]}
    from fluid.context.plugins import plugin_data_dir

    plugin_roots = {entry.path: (plugin.root, plugin_data_dir(plugin.name, plugin.marketplace))
                    for plugin in plugins for entry in [*plugin.commands, *plugin.skills]}
    commands = []
    seen = set()
    for kind, entries in (("custom", native_commands(working_dir, **options)),
                          ("skill", native_skills(working_dir, **options))):
        for entry in entries:
            if entry.name in seen:
                continue
            source = plugin_sources.get(entry.path, "user" if entry.scope == "global" else entry.scope)
            cmd = parse_command_file(Path(entry.path), source=source)
            if cmd is None or (kind == "skill" and not cmd.user_invocable):
                continue
            cmd.name = entry.name
            cmd.type = "plugin" if kind == "custom" and entry.path in plugin_sources else kind
            cmd.namespace = entry.name.split(":", 1)[0] if entry.path in plugin_sources else None
            if entry.path in plugin_roots:
                # A plugin's model pin does not apply (the model is chosen
                # per chat) and its placeholders are filled in, as for its
                # skills (fluid.context.plugin_import).
                from fluid.context.plugin_import import expand_plugin_paths

                cmd.model = None
                cmd.content = expand_plugin_paths(cmd.content, root=plugin_roots[entry.path][0],
                                                  data=plugin_roots[entry.path][1])
            seen.add(entry.name)
            commands.append(cmd)
    return commands


def get_command_by_name(
    working_dir: str, command_name: str, *, provider_kind: str = "delegated",
    setting_sources=None,
) -> Optional[SlashCommand]:
    """
    Get a specific command by name.

    Args:
        working_dir: The project working directory
        command_name: Command name (with or without leading /)

    Returns:
        SlashCommand object or None if not found
    """
    # Normalize command name
    name = command_name.lstrip("/")

    commands = _runtime_commands(working_dir, provider_kind, setting_sources)
    for cmd in commands:
        if cmd.name == name:
            return cmd

    return None


def expand_native_command(working_dir: str, prompt: str, *, setting_sources=None) -> str:
    """Expand a native file command before submission, without executing shell code."""
    name, arguments = parse_command_input(prompt)
    if not name:
        return prompt
    cmd = get_command_by_name(working_dir, name, provider_kind="native", setting_sources=setting_sources)
    if cmd is None:
        return prompt
    return expand_command_prompt(cmd, arguments)


def expand_command_prompt(cmd: SlashCommand, arguments: str) -> str:
    expanded = cmd.expand_prompt(arguments)
    if getattr(cmd, "type", "custom") == "skill":
        if arguments and not re.search(r"\$ARGUMENTS\b|\$\d+", cmd.content):
            expanded += f"\n\nArguments: {arguments}"
        return f"# Skill: {cmd.name}\n# From: {cmd.file_path}\n\n{expanded}"
    return expanded


def is_slash_command(text: str) -> bool:
    """Check if text starts with a slash command"""
    return bool(text) and text.startswith("/") and len(text) > 1


def parse_command_input(text: str) -> tuple[str, str]:
    """
    Parse command input into command name and arguments.

    Args:
        text: Input text like "/fix-issue 123 high"

    Returns:
        Tuple of (command_name, arguments)
        e.g., ("fix-issue", "123 high")
    """
    if not is_slash_command(text):
        return "", text

    # Remove leading /
    text = text[1:]

    # Split into command and arguments
    parts = text.split(None, 1)
    command_name = parts[0] if parts else ""
    arguments = parts[1] if len(parts) > 1 else ""

    return command_name, arguments


# Built-in interactive commands that require CLI bridge (PTY-based)
# These commands genuinely need terminal interaction and cannot be handled via REST API
# Note: These are NOT shown in the popup autocomplete
INTERACTIVE_COMMANDS = {
    "resume": {
        "description": "Resume a previous conversation",
        "requires_cli": True
    }
}

# Known Claude CLI commands. The autocomplete exposes only the supported
# noninteractive subset below, and only on the delegated runtime.
SDK_BUILTIN_COMMANDS = {
    "compact": {
        "description": "Compact conversation history to free up context space",
        "type": "sdk_builtin"
    },
    "context": {
        "description": "Show current context window usage and token counts",
        "type": "sdk_builtin"
    },
    "cost": {
        "description": "Display token usage and estimated cost for this session",
        "type": "sdk_builtin"
    },
    "model": {
        "description": "Switch Claude model (sonnet, opus, fable, haiku)",
        "type": "sdk_builtin",
        "argument_hint": "<model>"
    },
    "review": {
        "description": "Review uncommitted code changes for bugs and style issues",
        "type": "sdk_builtin"
    },
    "doctor": {
        "description": "Run diagnostics on installation and connectivity",
        "type": "sdk_builtin"
    },
    "init": {
        "description": "Create an AGENTS.md file for your project",
        "type": "sdk_builtin"
    },
    "memory": {
        "description": "View and edit AGENTS.md and memory files",
        "type": "sdk_builtin"
    },
    "add-dir": {
        "description": "Add additional directories to context",
        "type": "sdk_builtin",
        "argument_hint": "<path>"
    },
    "output-style": {
        "description": "Set response formatting (concise, verbose, etc.)",
        "type": "sdk_builtin",
        "argument_hint": "<style>"
    },
    "todos": {
        "description": "List tracked TODO items",
        "type": "sdk_builtin"
    },
    "export": {
        "description": "Export conversation to file or clipboard",
        "type": "sdk_builtin"
    },
    "pr-comments": {
        "description": "View pull request comments from GitHub",
        "type": "sdk_builtin"
    },
    "stats": {
        "description": "Show usage patterns and session statistics",
        "type": "sdk_builtin"
    },
    "insights": {
        "description": "Generate an HTML report analyzing recent sessions",
        "type": "sdk_builtin"
    },
    "sandbox": {
        "description": "Toggle sandboxed execution mode",
        "type": "sdk_builtin"
    },
    "security-review": {
        "description": "Audit recent changes for security vulnerabilities",
        "type": "sdk_builtin"
    },
    "rename": {
        "description": "Rename the current session",
        "type": "sdk_builtin",
        "argument_hint": "<name>"
    },
    "release-notes": {
        "description": "Display current version and release notes",
        "type": "sdk_builtin"
    },
    "status": {
        "description": "Show version, model, account, and connectivity info",
        "type": "sdk_builtin"
    },
    "bug": {
        "description": "Report a bug to Anthropic",
        "type": "sdk_builtin"
    },
    "bashes": {
        "description": "List active background shell tasks",
        "type": "sdk_builtin"
    },
}


def is_interactive_command(command_name: str) -> bool:
    """Check if a command requires interactive CLI interaction"""
    name = command_name.lstrip("/")
    return name in INTERACTIVE_COMMANDS


NONINTERACTIVE_SDK_COMMANDS = frozenset({
    "compact", "context", "cost", "review", "init", "security-review",
    "pr-comments",
})


def get_all_commands(working_dir: str, *, provider_kind: str = "delegated", setting_sources=None) -> List[Dict[str, Any]]:
    """
    Get all available commands for the popup autocomplete.

    Returns list of command info dicts for autocomplete.
    Note: /resume and /rewind are excluded as they are handled separately.
    """
    commands = [{
        "name": cmd.name, "display": cmd.get_display_name(),
        "description": cmd.description, "argument_hint": cmd.argument_hint,
        "type": cmd.type, "source": cmd.source, "namespace": cmd.namespace,
    } for cmd in _runtime_commands(working_dir, provider_kind, setting_sources)]
    if provider_kind == "delegated":
        custom_names = {cmd["name"] for cmd in commands}
        for name, info in SDK_BUILTIN_COMMANDS.items():
            if name in NONINTERACTIVE_SDK_COMMANDS and name not in custom_names:
                commands.append({
                    "name": name, "display": f"/{name}",
                    "description": info["description"],
                    "argument_hint": info.get("argument_hint"), "type": "sdk_builtin",
                })
    elif provider_kind in NATIVE_COMPACT_KINDS and not any(c["name"] == "compact" for c in commands):
        # The native engine runs a bare "/compact" as its own compaction
        # (fluid NativeLoop manual compaction); it takes no arguments.
        commands.append({
            "name": "compact", "display": "/compact",
            "description": "Compact this chat's context now",
            "argument_hint": None, "type": "sdk_builtin",
        })
    return sorted(commands, key=lambda cmd: cmd["name"])


#: Native legs whose engine runs a bare ``/compact`` as a compaction.
NATIVE_COMPACT_KINDS = frozenset({"anthropic", "openai_responses", "openrouter"})
