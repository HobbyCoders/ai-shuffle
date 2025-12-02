"""
Custom Slash Command Discovery and Execution

This module handles discovering and parsing custom slash commands
from .claude/commands/ directories in project working directories.
"""

import logging
import os
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

        # Split arguments
        args = arguments.split() if arguments else []

        # Replace $ARGUMENTS with all arguments
        prompt = prompt.replace("$ARGUMENTS", arguments)

        # Replace positional arguments $1, $2, etc.
        for i, arg in enumerate(args, 1):
            prompt = prompt.replace(f"${i}", arg)

        # Remove any unreplaced positional arguments
        prompt = re.sub(r'\$\d+', '', prompt)

        return prompt.strip()


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

    return SlashCommand(
        name=name,
        description=description,
        content=prompt_content,
        file_path=str(file_path),
        source=source,
        namespace=namespace,
        allowed_tools=allowed_tools,
        argument_hint=frontmatter.get("argument-hint"),
        model=frontmatter.get("model"),
        disable_model_invocation=frontmatter.get("disable-model-invocation", False)
    )


def discover_commands(working_dir: str) -> List[SlashCommand]:
    """
    Discover custom slash commands from a working directory.

    Scans:
    - {working_dir}/.claude/commands/ - Project commands
    - Subdirectories for namespaced commands

    Args:
        working_dir: The project working directory

    Returns:
        List of SlashCommand objects
    """
    commands = []
    working_path = Path(working_dir)

    # Project commands directory
    project_commands_dir = working_path / ".claude" / "commands"

    if project_commands_dir.exists() and project_commands_dir.is_dir():
        commands.extend(_scan_commands_directory(project_commands_dir, "project"))

    return commands


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


def get_command_by_name(working_dir: str, command_name: str) -> Optional[SlashCommand]:
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

    commands = discover_commands(working_dir)
    for cmd in commands:
        if cmd.name == name:
            return cmd

    return None


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

# Built-in commands that use REST API instead of interactive CLI
# V2: /rewind now uses direct JSONL manipulation - bulletproof, no PTY needed
# Note: These are NOT shown in the popup autocomplete
REST_API_COMMANDS = {
    "rewind": {
        "description": "Rewind conversation and/or code to a previous point",
        "api_endpoint": "/api/v1/commands/rewind/checkpoints/{session_id}",
        "requires_session": True,
        "method": "direct_jsonl",  # V2: No CLI interaction, direct file manipulation
        "note": "V2 implementation uses direct JSONL truncation for bulletproof rewind"
    }
}

# SDK built-in commands that are shown in the popup autocomplete
# These are passed directly to the SDK and handled internally
SDK_BUILTIN_COMMANDS = {
    "context": {
        "description": "View current context including system prompt and files",
        "type": "sdk_builtin"
    }
}


def is_interactive_command(command_name: str) -> bool:
    """Check if a command requires interactive CLI interaction"""
    name = command_name.lstrip("/")
    return name in INTERACTIVE_COMMANDS


def get_interactive_command_info(command_name: str) -> Optional[Dict[str, Any]]:
    """Get information about an interactive command"""
    name = command_name.lstrip("/")
    return INTERACTIVE_COMMANDS.get(name)


def is_rest_api_command(command_name: str) -> bool:
    """Check if a command is handled via REST API (non-interactive)"""
    name = command_name.lstrip("/")
    return name in REST_API_COMMANDS


def get_rest_api_command_info(command_name: str) -> Optional[Dict[str, Any]]:
    """Get information about a REST API command"""
    name = command_name.lstrip("/")
    return REST_API_COMMANDS.get(name)


def is_sdk_builtin_command(command_name: str) -> bool:
    """Check if a command is an SDK built-in command"""
    name = command_name.lstrip("/")
    return name in SDK_BUILTIN_COMMANDS


def get_sdk_builtin_command_info(command_name: str) -> Optional[Dict[str, Any]]:
    """Get information about an SDK built-in command"""
    name = command_name.lstrip("/")
    return SDK_BUILTIN_COMMANDS.get(name)


def get_all_commands(working_dir: str) -> List[Dict[str, Any]]:
    """
    Get all available commands for the popup autocomplete.

    Returns list of command info dicts for autocomplete.
    Note: /resume and /rewind are excluded as they are handled separately.
    """
    commands = []

    # Add custom commands from .claude/commands/
    for cmd in discover_commands(working_dir):
        commands.append({
            "name": cmd.name,
            "display": cmd.get_display_name(),
            "description": cmd.get_description_with_source(),
            "argument_hint": cmd.argument_hint,
            "type": "custom"
        })

    # Add SDK built-in commands (like /context)
    for name, info in SDK_BUILTIN_COMMANDS.items():
        commands.append({
            "name": name,
            "display": f"/{name}",
            "description": info["description"],
            "argument_hint": None,
            "type": "sdk_builtin"
        })

    # Note: INTERACTIVE_COMMANDS (/resume) and REST_API_COMMANDS (/rewind)
    # are intentionally excluded from the popup as they are handled separately

    # Sort by name
    commands.sort(key=lambda x: x["name"])

    return commands
