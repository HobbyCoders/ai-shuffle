"""
Plugin Hooks - Hook discovery and parsing for the plugin system.

Provides module-level functions for extracting hooks from plugins,
and agent frontmatter parsing.
"""

import json
import logging
from pathlib import Path
from typing import List, Optional, Dict, Any

logger = logging.getLogger(__name__)


def get_file_based_agents(svc) -> List[Dict[str, Any]]:
    """
    Get all file-based agents from installed and enabled plugins.

    Args:
        svc: PluginService instance

    Returns:
        List of agent definitions with their source plugin info
    """
    installed = svc.get_installed_plugins()
    enabled = svc.get_enabled_plugins()
    agents = []

    for plugin in installed:
        if not enabled.get(plugin.id, False):
            continue

        install_path = Path(plugin.install_path)
        agents_dir = install_path / "agents"

        if not agents_dir.exists():
            continue

        for agent_file in agents_dir.glob("**/*.md"):
            try:
                content = agent_file.read_text(encoding='utf-8')

                # Parse YAML frontmatter
                agent_data = _parse_agent_frontmatter(content)
                agent_data["id"] = f"{plugin.id}:{agent_file.stem}"
                agent_data["name"] = agent_file.stem
                agent_data["source_plugin"] = plugin.id
                agent_data["file_path"] = str(agent_file)

                agents.append(agent_data)
            except (OSError, UnicodeDecodeError) as e:
                # One unreadable or non-UTF-8 file must not hide every agent.
                logger.error(f"Failed to read agent file {agent_file}: {e}")

    return agents


def _parse_agent_frontmatter(content: str) -> Dict[str, Any]:
    """Parse YAML frontmatter from an agent markdown file"""
    result = {
        "description": "",
        "model": None,
        "tools": None,
        "prompt": ""
    }

    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        result["prompt"] = content
        return result

    # Find end of frontmatter
    end_idx = -1
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx == -1:
        result["prompt"] = content
        return result

    # Parse frontmatter (simple YAML parsing)
    frontmatter_lines = lines[1:end_idx]
    for line in frontmatter_lines:
        if ":" in line:
            key, value = line.split(":", 1)
            key = key.strip().lower()
            value = value.strip()

            if key == "description":
                result["description"] = value
            elif key == "model":
                result["model"] = value
            elif key == "tools":
                # Parse comma-separated tools
                result["tools"] = [t.strip() for t in value.split(",") if t.strip()]

    # Rest is the prompt
    result["prompt"] = "\n".join(lines[end_idx + 1:]).strip()

    return result


def get_all_plugin_hooks(get_plugin_service_func) -> List[Dict[str, Any]]:
    """
    Get all hooks contributed by globally installed plugins.

    Args:
        get_plugin_service_func: The get_plugin_service factory function

    Returns a list of hook info dicts with:
    - plugin_id: Plugin ID
    - plugin_name: Plugin display name
    - event: Hook event type
    - hook_type: 'prompt' or 'command'
    - description: Optional description
    - tool_matcher: Optional tool name pattern
    """
    service = get_plugin_service_func()
    plugins = service.get_installed_plugins()
    return get_hooks_from_plugins(plugins)


def get_hooks_from_plugins(plugins: List) -> List[Dict[str, Any]]:
    """
    Get all hooks from a list of plugins.

    This is a generalized version that works with any list of plugins,
    whether global or project-scoped.

    Returns a list of hook info dicts.
    """
    result = []

    logger.debug(f"[get_hooks_from_plugins] Processing {len(plugins)} plugins")

    for plugin in plugins:
        plugin_dir = Path(plugin.install_path) if plugin.install_path else None
        logger.debug(f"[get_hooks_from_plugins] Plugin {plugin.id}: install_path={plugin.install_path}, dir_exists={plugin_dir.exists() if plugin_dir else False}")
        if not plugin_dir or not plugin_dir.exists():
            continue

        # Check for hooks directory
        hooks_dir = plugin_dir / "hooks"
        if hooks_dir.exists() and hooks_dir.is_dir():
            for hook_file in hooks_dir.glob("*.md"):
                # Parse hook file to extract metadata
                hook_info = _parse_plugin_hook_file(hook_file, plugin)
                if hook_info:
                    result.append(hook_info)

        # Check hooks/hooks.json for hook definitions
        hooks_json = hooks_dir / "hooks.json" if hooks_dir and hooks_dir.exists() else None
        logger.debug(f"[get_hooks_from_plugins] Plugin {plugin.id}: hooks_json={hooks_json}, exists={hooks_json.exists() if hooks_json else False}")
        if hooks_json and hooks_json.exists():
            try:
                with open(hooks_json) as f:
                    hooks_data = json.load(f)

                logger.debug(f"[get_hooks_from_plugins] Plugin {plugin.id}: hooks_data type={type(hooks_data)}, content={hooks_data}")

                # hooks.json can be in multiple formats:
                # 1. List of event names: ["PreToolUse", "PostToolUse"]
                # 2. Dict mapping events to files: {"PreToolUse": "pretooluse.py"}
                # 3. List of hook objects: [{"event": "PreToolUse", "type": "prompt"}]
                # 4. Object with "hooks" key: {"hooks": [...]}

                if isinstance(hooks_data, dict) and "hooks" in hooks_data:
                    hooks_data = hooks_data["hooks"]
                result.extend(_hook_rows(plugin, hooks_data))
            except Exception as e:
                logger.error(f"[get_hooks_from_plugins] Error parsing hooks.json for {plugin.id}: {e}")

        # Also check the plugin manifest (either format) for a hooks section.
        # Same shapes as hooks.json. A string ``hooks`` is a path to a hooks
        # file, not a list of events; the standard object form
        # ({"PreToolUse": [...]}) used to raise on the first key and silently
        # drop every hook the plugin declared.
        try:
            from fluid.context.plugin_formats import read_manifest

            result.extend(_hook_rows(plugin, read_manifest(plugin_dir).get("hooks")))
        except Exception:
            pass

    return result


def _hook_rows(plugin, hooks_data: Any) -> List[Dict[str, Any]]:
    """Hook info rows for one plugin's hook declaration.

    Accepts a list of event names or hook objects, or a dict mapping event
    names to their definitions (the Claude Code format). Anything else,
    including a string path to a separate hooks file, contributes nothing.
    """
    if isinstance(hooks_data, dict):
        hooks_list: List[Any] = list(hooks_data.keys())
    elif isinstance(hooks_data, list):
        hooks_list = hooks_data
    else:
        return []

    rows: List[Dict[str, Any]] = []
    for hook in hooks_list:
        if isinstance(hook, str):
            # Simple event name string
            rows.append({
                "plugin_id": plugin.id,
                "plugin_name": plugin.name,
                "event": hook,
                "hook_type": "prompt",
                "description": None,
                "tool_matcher": None,
            })
        elif isinstance(hook, dict):
            # Full hook object
            rows.append({
                "plugin_id": plugin.id,
                "plugin_name": plugin.name,
                "event": hook.get("event", "Unknown"),
                "hook_type": hook.get("type", "prompt"),
                "description": hook.get("description"),
                "tool_matcher": hook.get("toolMatcher"),
            })
    return rows


def _parse_plugin_hook_file(hook_file: Path, plugin) -> Optional[Dict[str, Any]]:
    """
    Parse a plugin hook markdown file and extract metadata.

    Hook files use YAML frontmatter like:
    ---
    event: PreToolUse
    toolMatcher: Bash|Edit
    description: Validates tool usage
    ---
    Hook prompt content...
    """
    try:
        content = hook_file.read_text()

        # Check for frontmatter
        if not content.startswith("---"):
            return None

        # Find end of frontmatter
        end_marker = content.find("---", 3)
        if end_marker == -1:
            return None

        frontmatter = content[3:end_marker].strip()

        # Parse frontmatter
        event = None
        tool_matcher = None
        description = None

        for line in frontmatter.split("\n"):
            if ":" in line:
                key, value = line.split(":", 1)
                key = key.strip().lower()
                value = value.strip().strip('"').strip("'")

                if key == "event":
                    event = value
                elif key in ("toolmatcher", "tool_matcher"):
                    tool_matcher = value
                elif key == "description":
                    description = value

        if not event:
            return None

        return {
            "plugin_id": plugin.id,
            "plugin_name": plugin.name,
            "event": event,
            "hook_type": "prompt",
            "description": description,
            "tool_matcher": tool_matcher,
        }

    except Exception:
        return None
