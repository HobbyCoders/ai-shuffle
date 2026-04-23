"""
JSONL History Parser

Parses Claude SDK session history files (*.jsonl) to reconstruct chat messages
in the same format as live streaming, ensuring visual consistency between
resumed and live sessions.

JSONL Format (from Claude SDK ~/.claude/projects/[project]/[session_id].jsonl):
- User messages: {"type":"user", "message":{"role":"user","content":"..."}, "uuid":"...", "timestamp":"..."}
- Assistant messages: {"type":"assistant", "message":{"role":"assistant","content":[blocks]}, "uuid":"...", "timestamp":"..."}
- Tool results: {"type":"user", "message":{"role":"user","content":[{"tool_use_id":"...","type":"tool_result","content":"..."}]}, "toolUseResult":{...}}
- Queue operations: {"type":"queue-operation", ...} - ignored
- File snapshots: {"type":"file-history-snapshot", ...} - ignored

Key fields that affect parsing:
- isMeta: Boolean - meta messages like slash commands, system prompts (should be skipped for display)
- isSidechain: Boolean - messages in alternate conversation branches (should be skipped)
- parentUuid: String - points to parent message in conversation tree
"""

import json
import logging
import threading
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import List, Dict, Any, Optional, Generator
from datetime import datetime, timezone

from app.core.config import settings

logger = logging.getLogger(__name__)

# ─── Parse Result Cache ─────────────────────────────────────────────────────
# Cache parsed JSONL results to avoid re-reading files on every poll/load.
# Validated by file mtime + size - if file changes, cache entry is invalidated.
_PARSE_CACHE_MAX_SIZE = 50

# Cache: sdk_session_id -> (file_mtime, file_size, messages_copy, usage_data_copy)
_parse_cache: OrderedDict = OrderedDict()
_parse_cache_lock = threading.Lock()

# Cache: sdk_session_id -> (dir_mtime, agent_files_dict)
_agent_paths_cache: OrderedDict = OrderedDict()
_agent_paths_cache_lock = threading.Lock()

# Maximum display size for tool output content (matches streaming behavior)
MAX_DISPLAY_OUTPUT_SIZE = 2000


def _truncate_for_display(content: str, max_size: int = MAX_DISPLAY_OUTPUT_SIZE) -> str:
    """
    Truncate content for display purposes.

    Large content like base64-encoded images or file contents should be truncated
    early in the parsing process to avoid memory issues when sending via WebSocket.
    """
    if not content or len(content) <= max_size:
        return content

    # Check if it looks like base64 image data
    is_likely_base64 = (
        'data:image/' in content[:100] or
        content[:10].startswith('iVBOR') or  # PNG base64 signature
        content[:10].startswith('/9j/') or   # JPEG base64 signature
        content[:10].startswith('R0lGOD') or # GIF base64 signature
        (len(content) > 10000 and sum(c.isalnum() or c in '+/=' for c in content[:1000]) > 950)
    )

    if is_likely_base64:
        original_size_kb = int(len(content) * 0.75 / 1024)
        return f"[Image/binary content - {original_size_kb}KB - truncated for display]"

    return f"{content[:max_size]}\n\n[... truncated - {len(content) // 1024}KB total]"


def get_project_dir_name(working_dir: str) -> str:
    """
    Convert a working directory path to Claude's project directory name.

    Claude converts paths like /workspace/github-projects to -workspace-github-projects
    The leading dash is kept as that's how Claude encodes absolute paths.

    On Windows, backslashes and colons are also replaced with dashes, e.g.:
    D:\\Projects\\ai-shuffle -> D--Projects-ai-shuffle

    The colon after drive letter (D:) becomes a dash, and each backslash becomes a dash,
    resulting in D-- at the start for D:\\ paths.
    """
    import os
    # Replace Windows drive colon (D: -> D-), path separators (\, /), all with dashes
    # Order matters: os.sep first for native, then / for any mixed paths, then : for Windows drives
    return working_dir.replace(os.sep, "-").replace("/", "-").replace(":", "-")


def get_session_jsonl_path(sdk_session_id: str, working_dir: str = "/workspace") -> Optional[Path]:
    """
    Get the path to a session's JSONL history file.

    Args:
        sdk_session_id: The Claude SDK session ID (UUID)
        working_dir: The working directory used when the session was created

    Returns:
        Path to the JSONL file, or None if not found
    """
    # Get Claude projects directory from config (supports custom paths via CLAUDE_PROJECTS_DIR env var)
    claude_projects_dir = settings.get_claude_projects_dir

    logger.debug(f"Looking for JSONL in claude_projects_dir: {claude_projects_dir}")

    # Try to find the project directory
    project_dir_name = get_project_dir_name(working_dir)
    project_dir = claude_projects_dir / project_dir_name

    logger.debug(f"Checking project dir: {project_dir}")

    if project_dir.exists():
        jsonl_path = project_dir / f"{sdk_session_id}.jsonl"
        logger.debug(f"Checking JSONL path: {jsonl_path}")
        if jsonl_path.exists():
            return jsonl_path

    # Try to search all project directories for this session
    if claude_projects_dir.exists():
        for proj_dir in claude_projects_dir.iterdir():
            if proj_dir.is_dir():
                jsonl_path = proj_dir / f"{sdk_session_id}.jsonl"
                if jsonl_path.exists():
                    logger.debug(f"Found JSONL in alternate project dir: {jsonl_path}")
                    return jsonl_path

    logger.warning(f"JSONL file not found for session {sdk_session_id} in {claude_projects_dir}")
    return None


def get_hooks_jsonl_path(sdk_session_id: str, working_dir: str) -> Path:
    """
    Get the path to a session's hooks JSONL file.

    This file stores hook stderr output (plugin warnings) that aren't captured
    in the main session JSONL.

    Args:
        sdk_session_id: The Claude SDK session ID (UUID)
        working_dir: The working directory for the project

    Returns:
        Path to the hooks JSONL file (may not exist yet)
    """
    claude_projects_dir = settings.get_claude_projects_dir
    project_dir_name = get_project_dir_name(working_dir)
    project_dir = claude_projects_dir / project_dir_name

    # Ensure directory exists
    project_dir.mkdir(parents=True, exist_ok=True)

    return project_dir / f"{sdk_session_id}.hooks.jsonl"


def write_hook_message(
    sdk_session_id: str,
    working_dir: str,
    plugin: str,
    message: str,
    severity: str
) -> None:
    """
    Append a plugin hook message to the session's main JSONL file.

    This is a wrapper around write_system_message() that writes plugin warnings
    to the main session JSONL instead of a separate hooks file. This ensures
    hook messages are part of the chat history and persist with the session.

    Args:
        sdk_session_id: The Claude SDK session ID
        working_dir: The working directory for the project
        plugin: Name of the plugin that generated the message
        message: The stderr output from the hook
        severity: Severity level (warning, error, info)
    """
    write_system_message(
        sdk_session_id=sdk_session_id,
        working_dir=working_dir,
        subtype="plugin_warning",
        content=message,
        data={
            "plugin": plugin,
            "message": message,
            "severity": severity
        }
    )


def write_system_message(
    sdk_session_id: str,
    working_dir: str,
    subtype: str,
    content: str,
    data: Optional[Dict[str, Any]] = None
) -> None:
    """
    Append a system message to the session's main JSONL file.

    This writes messages in the Claude SDK's JSONL format for system entries.
    These messages are displayed in chat history and persist across session loads.

    Args:
        sdk_session_id: The Claude SDK session ID (UUID)
        working_dir: The working directory for the project
        subtype: Message subtype (e.g., 'worktree_context', 'plugin_warning')
        content: Human-readable message content
        data: Optional structured data for the message
    """
    jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
    if not jsonl_path:
        logger.warning(f"Cannot write system message - JSONL not found for session {sdk_session_id}")
        return

    entry = {
        "type": "system",
        "subtype": subtype,
        "content": content,
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "uuid": f"sys-{uuid.uuid4().hex[:12]}"
    }

    if data:
        entry["data"] = data

    try:
        with open(jsonl_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(entry) + '\n')
        logger.debug(f"Wrote system message ({subtype}) to {jsonl_path}")
    except Exception as e:
        logger.error(f"Failed to write system message to {jsonl_path}: {e}")


def parse_hooks_jsonl(sdk_session_id: str, working_dir: str) -> List[Dict[str, Any]]:
    """
    Parse a session's hooks JSONL file and return plugin warning messages.

    Args:
        sdk_session_id: The Claude SDK session ID
        working_dir: The working directory for the project

    Returns:
        List of plugin warning messages with: id, role, content, type, metadata
    """
    hooks_path = get_hooks_jsonl_path(sdk_session_id, working_dir)

    if not hooks_path.exists():
        return []

    messages = []
    for entry in parse_jsonl_file(hooks_path):
        if entry.get("type") == "plugin_warning":
            plugin = entry.get("plugin", "Plugin")
            message_content = entry.get("message", "")
            severity = entry.get("severity", "warning")
            timestamp = entry.get("timestamp", "")

            messages.append({
                "id": f"plugin-{timestamp}",
                "role": "system",
                "content": message_content,
                "type": "system",
                "subtype": "plugin_warning",
                "systemSubtype": "plugin_warning",
                "systemData": {
                    "plugin": plugin,
                    "message": message_content,
                    "severity": severity
                },
                "metadata": {
                    "plugin": plugin,
                    "severity": severity,
                    "timestamp": timestamp
                },
                "streaming": False
            })

    logger.debug(f"Parsed {len(messages)} hook messages from {hooks_path}")
    return messages


def parse_jsonl_file(jsonl_path: Path) -> Generator[Dict[str, Any], None, None]:
    """
    Parse a JSONL file line by line.

    Yields each parsed JSON object.
    """
    try:
        with open(jsonl_path, 'r', encoding='utf-8') as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as e:
                    logger.warning(f"Failed to parse line {line_num} in {jsonl_path}: {e}")
                    continue
    except Exception as e:
        logger.error(f"Failed to read JSONL file {jsonl_path}: {e}")


def extract_text_from_content(content: Any) -> str:
    """
    Extract text content from message content which can be string or list of blocks.
    """
    if isinstance(content, str):
        return content

    if isinstance(content, list):
        text_parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
        return "".join(text_parts)

    return ""


def _is_system_content(content: str) -> bool:
    """
    Check if content is empty or whitespace-only.

    Previously this filtered out various system messages, but we now display
    all content to the user. Slash commands are handled by _parse_slash_command(),
    and other system content is shown as-is.
    """
    if not content or not content.strip():
        return True
    return False


def _parse_slash_command(content: str) -> Optional[Dict[str, Any]]:
    """
    Parse slash command XML tags into structured data.

    Slash commands from Claude SDK have this format:
    <command-message>plugin:command</command-message>
    <command-name>/plugin:command</command-name>
    <command-args>arguments here</command-args>

    Returns:
        Dict with command_id, command_name, command_args, or None if not a slash command
    """
    import re

    if not content or not content.strip().startswith("<command-"):
        return None

    result = {}

    # Extract command-message (the internal command identifier)
    msg_match = re.search(r'<command-message>(.*?)</command-message>', content, re.DOTALL)
    if msg_match:
        result["command_id"] = msg_match.group(1).strip()

    # Extract command-name (the display name with slash prefix)
    name_match = re.search(r'<command-name>(.*?)</command-name>', content, re.DOTALL)
    if name_match:
        result["command_name"] = name_match.group(1).strip()

    # Extract command-args (the arguments passed to the command)
    args_match = re.search(r'<command-args>(.*?)</command-args>', content, re.DOTALL)
    if args_match:
        result["command_args"] = args_match.group(1).strip()

    # Must have at least command_name to be valid
    if "command_name" in result:
        return result

    return None


def _is_task_notification(content: str) -> bool:
    """
    Check if content is a Claude SDK task-notification message.

    These are internal async completion callbacks from the Task/subagent system.
    The SDK injects them as user-role messages when a background agent finishes,
    but they're only meant for Claude's internal context — the subagent's work
    is already displayed via the tool_use/tool_result pair in SubagentMessage.
    """
    if not content:
        return False
    return "<task-notification>" in content


def _extract_local_command_output(content: str) -> Optional[str]:
    """
    Extract content from <local-command-stdout> tags if present.

    Returns the extracted content, or None if no tags found.
    """
    import re
    match = re.search(r'<local-command-stdout>(.*?)</local-command-stdout>', content, re.DOTALL)
    if match:
        return match.group(1).strip()
    return None


def get_agent_jsonl_paths(sdk_session_id: str, working_dir: str = "/workspace") -> Dict[str, Path]:
    """
    Find all agent JSONL files for a session.

    Agent files are named agent-{agent_id}.jsonl and contain entries with
    matching sessionId field.

    Returns:
        Dict mapping agent_id to Path
    """
    claude_projects_dir = settings.get_claude_projects_dir
    agent_files: Dict[str, Path] = {}

    # First try the specified working_dir
    project_dir_name = get_project_dir_name(working_dir)
    project_dir = claude_projects_dir / project_dir_name

    # If not found, search all project directories (fallback)
    project_dirs_to_search = []
    if project_dir.exists():
        project_dirs_to_search.append(project_dir)
    else:
        # Fallback: search all project directories
        if claude_projects_dir.exists():
            for proj_dir in claude_projects_dir.iterdir():
                if proj_dir.is_dir():
                    # Check if this project dir has the session file
                    session_file = proj_dir / f"{sdk_session_id}.jsonl"
                    if session_file.exists():
                        project_dirs_to_search.append(proj_dir)
                        break

    # Check cache - validated by project directory mtime
    for search_dir in project_dirs_to_search:
        try:
            dir_mtime = search_dir.stat().st_mtime
            with _agent_paths_cache_lock:
                cached = _agent_paths_cache.get(sdk_session_id)
                if cached and cached[0] == dir_mtime:
                    _agent_paths_cache.move_to_end(sdk_session_id)
                    logger.debug(f"Agent paths cache hit for session {sdk_session_id}")
                    return dict(cached[1])  # Return copy to prevent mutation of cached data
        except OSError:
            pass

    for search_dir in project_dirs_to_search:
        for agent_file in search_dir.glob("agent-*.jsonl"):
            # Extract agent_id from filename (agent-{id}.jsonl)
            agent_id = agent_file.stem.replace("agent-", "")

            # Verify this agent file belongs to our session by checking first entry
            try:
                with open(agent_file, 'r', encoding='utf-8') as f:
                    first_line = f.readline().strip()
                    if first_line:
                        entry = json.loads(first_line)
                        if entry.get("sessionId") == sdk_session_id:
                            agent_files[agent_id] = agent_file
                            logger.debug(f"Found agent file {agent_id} for session {sdk_session_id}")
            except Exception as e:
                logger.warning(f"Failed to check agent file {agent_file}: {e}")

    # Cache the results (with LRU eviction)
    if project_dirs_to_search:
        try:
            dir_mtime = project_dirs_to_search[0].stat().st_mtime
            with _agent_paths_cache_lock:
                _agent_paths_cache[sdk_session_id] = (dir_mtime, agent_files)
                while len(_agent_paths_cache) > _PARSE_CACHE_MAX_SIZE:
                    _agent_paths_cache.popitem(last=False)
        except OSError:
            pass

    return agent_files


def parse_agent_history(agent_path: Path) -> List[Dict[str, Any]]:
    """
    Parse an agent's JSONL file and return child messages.

    Returns list of child messages with: id, type, content, toolName, toolId, toolInput, toolResult, toolStatus, timestamp
    Tool results are grouped with their corresponding tool_use entries.
    """
    children: List[Dict[str, Any]] = []
    tool_names_by_id: Dict[str, str] = {}

    for entry in parse_jsonl_file(agent_path):
        entry_type = entry.get("type")
        message_data = entry.get("message", {})
        role = message_data.get("role")
        content = message_data.get("content")
        timestamp = entry.get("timestamp")
        uuid = entry.get("uuid", "")

        if entry_type == "assistant" and role == "assistant":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get("type")

                        if block_type == "text":
                            text = block.get("text", "")
                            if text:
                                children.append({
                                    "id": f"agent-text-{uuid}",
                                    "type": "text",
                                    "content": text,
                                    "timestamp": timestamp
                                })

                        elif block_type == "tool_use":
                            tool_id = block.get("id")
                            tool_name = block.get("name")
                            if tool_id and tool_name:
                                tool_names_by_id[tool_id] = tool_name

                            children.append({
                                "id": f"agent-tool-{tool_id}",
                                "type": "tool_use",
                                "content": "",
                                "toolName": tool_name,
                                "toolId": tool_id,
                                "toolInput": block.get("input", {}),
                                "toolStatus": "running",  # Will be updated when result arrives
                                "timestamp": timestamp
                            })

        elif entry_type == "user" and role == "user":
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        tool_use_id = block.get("tool_use_id")
                        raw_content = block.get("content", "")
                        is_error = block.get("is_error", False)

                        # Extract text from content
                        if isinstance(raw_content, list):
                            output = extract_text_from_content(raw_content)
                        else:
                            output = raw_content if isinstance(raw_content, str) else ""

                        # Truncate for display
                        output = output[:2000] if output else ""

                        # Group tool result with its corresponding tool_use
                        found_tool_use = False
                        for child in children:
                            if child.get("type") == "tool_use" and child.get("toolId") == tool_use_id:
                                child["toolResult"] = output
                                child["toolStatus"] = "error" if is_error else "complete"
                                found_tool_use = True
                                break

                        # Only create separate tool_result if no matching tool_use found
                        if not found_tool_use:
                            children.append({
                                "id": f"agent-result-{tool_use_id}",
                                "type": "tool_result",
                                "content": output,
                                "toolName": tool_names_by_id.get(tool_use_id),
                                "toolId": tool_use_id,
                                "timestamp": timestamp
                            })

    return children


def parse_session_history(
    sdk_session_id: str,
    working_dir: str = "/workspace"
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Parse a session's JSONL history file and return messages + usage data.

    Results are cached in memory and validated by file mtime + size.
    Cache is invalidated when the file changes on disk.
    """
    jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
    if not jsonl_path:
        logger.warning(f"JSONL file not found for session {sdk_session_id}")
        return [], {}

    # Check file stats for cache validation
    try:
        stat = jsonl_path.stat()
        file_mtime = stat.st_mtime
        file_size = stat.st_size
    except OSError:
        return [], {}

    # Check cache
    with _parse_cache_lock:
        cached = _parse_cache.get(sdk_session_id)
        if cached is not None:
            c_mtime, c_size, c_messages_json, c_usage_json = cached
            if c_mtime == file_mtime and c_size == file_size:
                _parse_cache.move_to_end(sdk_session_id)
                logger.debug(f"Parse cache hit for session {sdk_session_id}")
                # Deserialize from JSON strings (much faster than deepcopy under lock)
                return json.loads(c_messages_json), json.loads(c_usage_json)

    # Cache miss - do full parse
    logger.debug(f"Parse cache miss for session {sdk_session_id}, parsing JSONL")
    messages, usage_data = _parse_session_history_uncached(sdk_session_id, working_dir, jsonl_path)

    # Store in cache as serialized JSON strings (fast to copy on read, no deepcopy needed)
    with _parse_cache_lock:
        _parse_cache[sdk_session_id] = (file_mtime, file_size, json.dumps(messages), json.dumps(usage_data))
        while len(_parse_cache) > _PARSE_CACHE_MAX_SIZE:
            _parse_cache.popitem(last=False)

    return messages, usage_data


def invalidate_parse_cache(sdk_session_id: str = None) -> None:
    """
    Invalidate cached parse results.

    Args:
        sdk_session_id: If provided, invalidate only this session. If None, clear all.
    """
    with _parse_cache_lock:
        if sdk_session_id:
            _parse_cache.pop(sdk_session_id, None)
            logger.debug(f"Invalidated parse cache for session {sdk_session_id}")
        else:
            _parse_cache.clear()
            logger.debug("Cleared entire parse cache")

    with _agent_paths_cache_lock:
        if sdk_session_id:
            _agent_paths_cache.pop(sdk_session_id, None)
        else:
            _agent_paths_cache.clear()


def pre_seed_parse_cache(
    sdk_session_id: str,
    messages: List[Dict[str, Any]],
    usage_data: Dict[str, Any],
    working_dir: str = "/workspace"
) -> bool:
    """
    Pre-seed the parse cache with messages from the live session store.

    Called after end_streaming() to avoid JSONL re-parsing on the next load.
    Instead of invalidating the cache (forcing an expensive JSONL re-parse),
    we update it with the messages already in memory.

    The cache entry is stored with the current JSONL file's mtime/size so it
    will be treated as valid on the next read. If the JSONL file changes again
    (e.g., another query starts), the mtime check will detect the change and
    the cache will be re-populated normally.

    Args:
        sdk_session_id: The Claude SDK session ID
        messages: Final messages from live_session_store
        usage_data: Usage data dict
        working_dir: Working directory for finding the JSONL file

    Returns:
        True if cache was pre-seeded, False on failure (falls back to invalidation)
    """
    try:
        jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
        if not jsonl_path:
            logger.warning(f"Cannot pre-seed cache: JSONL path not found for {sdk_session_id}")
            invalidate_parse_cache(sdk_session_id)
            return False

        stat = jsonl_path.stat()
        file_mtime = stat.st_mtime
        file_size = stat.st_size

        with _parse_cache_lock:
            _parse_cache[sdk_session_id] = (
                file_mtime,
                file_size,
                json.dumps(messages),
                json.dumps(usage_data)
            )
            _parse_cache.move_to_end(sdk_session_id)
            while len(_parse_cache) > _PARSE_CACHE_MAX_SIZE:
                _parse_cache.popitem(last=False)

        logger.debug(f"Pre-seeded parse cache for session {sdk_session_id} with {len(messages)} messages")
        return True

    except Exception as e:
        logger.error(f"Failed to pre-seed parse cache for {sdk_session_id}: {e}")
        invalidate_parse_cache(sdk_session_id)
        return False


def _parse_session_history_uncached(
    sdk_session_id: str,
    working_dir: str,
    jsonl_path: Path
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    Parse a session's JSONL history file and return messages + usage data in a single pass.

    This transforms the JSONL format to match the format used by live streaming,
    ensuring visual consistency between resumed and live sessions.

    Also parses associated agent JSONL files for Task tool calls and creates
    subagent message groups.

    Args:
        sdk_session_id: The Claude SDK session ID
        working_dir: The working directory for finding the project
        jsonl_path: Path to the JSONL file (caller is responsible for resolving this)

    Returns:
        Tuple of (messages, usage_data):
        - messages: List of messages in the same format as WebSocket streaming events
        - usage_data: Dict with total_tokens_in, total_tokens_out, cache_creation_tokens,
          cache_read_tokens, last_input_tokens, model (empty dict if no usage found)
    """
    logger.debug(f"Parsing JSONL history from {jsonl_path}")

    # Find agent file paths (lazy — files are parsed on-demand when needed)
    agent_files = get_agent_jsonl_paths(sdk_session_id, working_dir)
    agent_children_cache: Dict[str, List[Dict[str, Any]]] = {}

    messages: List[Dict[str, Any]] = []
    msg_counter = 0

    # Track tool names by ID for matching tool_result to tool_use
    tool_names_by_id: Dict[str, str] = {}

    # Dict-based index: toolId → index in messages list for O(1) lookup
    messages_by_tool_id: Dict[str, int] = {}

    # Track Task tool uses that have been converted to subagent messages
    # Maps tool_use_id to agent info for matching with tool_result
    task_tool_uses: Dict[str, Dict[str, Any]] = {}

    # Usage accumulation (replaces separate get_session_cost_from_jsonl call)
    total_input_tokens = 0
    total_output_tokens = 0
    usage_model = None
    last_usage = {}

    for entry in parse_jsonl_file(jsonl_path):
        entry_type = entry.get("type")

        # Skip non-message entries
        if entry_type in ("queue-operation", "file-history-snapshot"):
            continue

        # Accumulate usage data from all assistant entries
        if entry_type == "assistant":
            message_data_for_usage = entry.get("message", {})
            usage = message_data_for_usage.get("usage", {})
            if not usage_model:
                usage_model = message_data_for_usage.get("model")
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)
            # Only track last_usage from main conversation turns (not subagents/sidechain).
            # last_usage represents the current context window size, so it must come from
            # the main conversation — subagent usage reflects a different context window.
            if usage and not entry.get("isMeta") and not entry.get("isSidechain"):
                last_usage = usage

        # Skip meta messages (slash commands, system prompts, etc.)
        if entry.get("isMeta"):
            continue

        # Skip sidechain messages (alternate conversation branches)
        if entry.get("isSidechain"):
            continue

        message_data = entry.get("message", {})
        role = message_data.get("role")
        content = message_data.get("content")
        timestamp = entry.get("timestamp")
        uuid = entry.get("uuid", f"msg-{msg_counter}")

        # Handle system messages (e.g., compact_boundary, worktree_context, plugin_warning)
        if entry_type == "system":
            subtype = entry.get("subtype")
            system_content = entry.get("content", "")
            # SDK system messages store fields at the top level of the entry,
            # not under a nested "data" key. Pass the entire entry as systemData
            # so the frontend has access to all fields (e.g., compactMetadata,
            # durationMs, permissionMode, etc.)
            system_data = entry.get("data") or entry
            msg_counter += 1

            # Normalize field names: JSONL uses camelCase (compactMetadata),
            # but the frontend expects snake_case (compact_metadata)
            if subtype == "compact_boundary" and "compactMetadata" in system_data:
                cm = system_data["compactMetadata"]
                system_data = dict(system_data)
                system_data["compact_metadata"] = {
                    "trigger": cm.get("trigger"),
                    "pre_tokens": cm.get("preTokens")
                }
            elif subtype == "microcompact_boundary" and "microcompactMetadata" in system_data:
                mm = system_data["microcompactMetadata"]
                system_data = dict(system_data)
                system_data["microcompactMetadata"] = {
                    "trigger": mm.get("trigger"),
                    "tokensSaved": mm.get("tokensSaved"),
                    "preTokens": mm.get("preTokens")
                }

            messages.append({
                "id": uuid,
                "uuid": uuid,  # Raw JSONL UUID for rewind
                "role": "system",
                "content": system_content,
                "type": "system",
                "subtype": subtype,
                "systemSubtype": subtype,  # Frontend compatibility
                "systemData": system_data,  # Pass through structured data
                "metadata": {
                    "timestamp": timestamp,
                    "compactMetadata": entry.get("compactMetadata")
                },
                "streaming": False
            })
            continue

        if entry_type == "user" and role == "user":
            # User message - can be plain text, tool results, or array with text blocks
            if isinstance(content, str):
                # Check for slash command (e.g., /commit, /ralph-loop)
                slash_cmd = _parse_slash_command(content)
                if slash_cmd is not None:
                    msg_counter += 1
                    # Format display content: "/command args" or just "/command"
                    display_content = slash_cmd.get("command_name", "")
                    if slash_cmd.get("command_args"):
                        display_content = f"{display_content} {slash_cmd['command_args']}"
                    messages.append({
                        "id": uuid,
                        "uuid": uuid,  # Raw JSONL UUID for rewind
                        "role": "user",
                        "content": display_content,
                        "type": "command",
                        "commandId": slash_cmd.get("command_id"),
                        "commandName": slash_cmd.get("command_name"),
                        "commandArgs": slash_cmd.get("command_args"),
                        "metadata": {"timestamp": timestamp},
                        "streaming": False
                    })
                    continue

                # Check for local command output (e.g., /context, /compact)
                local_output = _extract_local_command_output(content)
                if local_output is not None:
                    msg_counter += 1
                    messages.append({
                        "id": uuid,
                        "uuid": uuid,  # Raw JSONL UUID for rewind
                        "role": "system",
                        "content": local_output,
                        "type": "system",
                        "subtype": "local_command",
                        "systemSubtype": "local_command",  # Frontend compatibility
                        "systemData": {"content": local_output},  # Frontend uses data.content for rendering
                        "metadata": {"timestamp": timestamp},
                        "streaming": False
                    })
                    continue

                # Skip SDK-internal task notifications (background agent callbacks)
                if _is_task_notification(content):
                    continue

                # Plain user message - skip empty content and system/command-related messages
                if content and not _is_system_content(content):
                    msg_counter += 1
                    messages.append({
                        "id": uuid,
                        "uuid": uuid,  # Raw JSONL UUID for rewind
                        "role": "user",
                        "content": content,
                        "type": None,
                        "metadata": {"timestamp": timestamp},
                        "streaming": False
                    })
            elif isinstance(content, list):
                # Array content - could be tool results or text blocks
                has_text_content = False
                text_parts = []

                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_result":
                            msg_counter += 1
                            tool_result = entry.get("toolUseResult")
                            raw_content = block.get("content", "")
                            is_error = block.get("is_error", False)
                            tool_use_id = block.get("tool_use_id")

                            # Handle content that can be string or list of content blocks
                            # Some tool results have content as [{'type': 'text', 'text': '...'}]
                            if isinstance(raw_content, list):
                                output = extract_text_from_content(raw_content)
                            else:
                                output = raw_content if isinstance(raw_content, str) else ""

                            # Get output from toolUseResult if available
                            # toolUseResult can have different formats depending on the tool:
                            # - Bash: {"stdout": "...", "stderr": "...", "is_error": bool}
                            # - Read: {"type": "text", "file": {"filePath": "...", "content": "..."}}
                            # - Other tools: may be a string directly
                            if tool_result and isinstance(tool_result, dict):
                                # Handle Bash-style results with stdout/stderr
                                if "stdout" in tool_result or "stderr" in tool_result:
                                    stdout = tool_result.get("stdout", "")
                                    stderr = tool_result.get("stderr", "")
                                    output = stdout
                                    if stderr:
                                        output = f"{stdout}\n{stderr}" if stdout else stderr
                                    is_error = is_error or tool_result.get("is_error", False)
                                # Handle Read-style results with file content
                                elif tool_result.get("type") == "text" and "file" in tool_result:
                                    file_info = tool_result.get("file", {})
                                    file_content = file_info.get("content", "")
                                    file_path = file_info.get("filePath", "")
                                    # Truncate file content early - images/large files can be huge
                                    file_content = _truncate_for_display(file_content)
                                    # Format like the streaming version does
                                    if file_path and file_content:
                                        output = f"File: {file_path}\n{file_content}"
                                    elif file_content:
                                        output = file_content
                                # Handle other dict-based results
                                elif tool_result.get("content"):
                                    tr_content = tool_result.get("content", "")
                                    # Content can be string or list of content blocks
                                    if isinstance(tr_content, list):
                                        output = _truncate_for_display(extract_text_from_content(tr_content))
                                    elif isinstance(tr_content, str):
                                        output = _truncate_for_display(tr_content)
                                    else:
                                        output = _truncate_for_display(str(tr_content))
                                elif tool_result.get("result"):
                                    output = _truncate_for_display(str(tool_result.get("result", "")))
                            elif tool_result and isinstance(tool_result, str):
                                # Sometimes toolUseResult is just a string (error messages)
                                output = _truncate_for_display(tool_result)

                            # Check if this is a result for a Task tool (subagent)
                            if tool_use_id in task_tool_uses:
                                # This is a subagent result - update the existing subagent message
                                _task_info = task_tool_uses[tool_use_id]

                                # Extract agentId from toolUseResult if available
                                tool_use_result = entry.get("toolUseResult", {})
                                agent_id = tool_use_result.get("agentId") if isinstance(tool_use_result, dict) else None

                                # O(1) lookup for the subagent message
                                msg_idx = messages_by_tool_id.get(tool_use_id)
                                if msg_idx is not None and msg_idx < len(messages):
                                    msg = messages[msg_idx]
                                    msg["content"] = output[:2000] if output else ""
                                    msg["agentStatus"] = "error" if is_error else "completed"

                                    # Lazy-load agent children on demand
                                    if agent_id:
                                        if agent_id not in agent_children_cache and agent_id in agent_files:
                                            agent_children_cache[agent_id] = parse_agent_history(agent_files[agent_id])
                                        if agent_id in agent_children_cache:
                                            msg["agentId"] = agent_id
                                            msg["agentChildren"] = agent_children_cache[agent_id]
                                            agent_children_cache[agent_id] = []

                                # Don't add a separate tool_result message for Task tools
                                continue

                            # Group tool result with its corresponding tool_use message
                            # O(1) lookup instead of linear scan
                            found_tool_use = False
                            msg_idx = messages_by_tool_id.get(tool_use_id)
                            if msg_idx is not None and msg_idx < len(messages):
                                msg = messages[msg_idx]
                                if msg.get("type") == "tool_use":
                                    msg["toolResult"] = output[:2000] if output else ""
                                    msg["toolStatus"] = "error" if is_error else "complete"
                                    found_tool_use = True

                            # Only create separate tool_result if no matching tool_use found
                            if not found_tool_use:
                                messages.append({
                                    "id": f"result-{uuid}-{tool_use_id}",
                                    "uuid": uuid,  # Raw JSONL UUID for rewind
                                    "role": "assistant",  # Display as assistant for UI consistency
                                    "content": output[:2000] if output else "",  # Truncate like streaming
                                    "type": "tool_result",
                                    "toolId": tool_use_id,
                                    "toolName": tool_names_by_id.get(tool_use_id),  # Match to tool_use
                                    "metadata": {
                                        "timestamp": timestamp,
                                        "is_error": is_error
                                    },
                                    "streaming": False
                                })
                        elif block.get("type") == "text":
                            # Text block in user content array
                            text = block.get("text", "")
                            if text and not _is_system_content(text) and not _is_task_notification(text):
                                has_text_content = True
                                text_parts.append(text)

                # If we collected text blocks, create a single user message
                if has_text_content and text_parts:
                    combined_text = "\n".join(text_parts)
                    msg_counter += 1
                    messages.append({
                        "id": uuid,
                        "uuid": uuid,  # Raw JSONL UUID for rewind
                        "role": "user",
                        "content": combined_text,
                        "type": None,
                        "metadata": {"timestamp": timestamp},
                        "streaming": False
                    })

        elif entry_type == "assistant" and role == "assistant":
            # Check for API error messages (synthetic messages from SDK)
            # These have isApiErrorMessage: true and model: "<synthetic>"
            if entry.get("isApiErrorMessage"):
                # Extract the error text from the content
                error_text = ""
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            error_text = block.get("text", "")
                            break
                elif isinstance(content, str):
                    error_text = content

                if error_text:
                    msg_counter += 1
                    messages.append({
                        "id": uuid,
                        "uuid": uuid,  # Raw JSONL UUID for rewind
                        "role": "system",
                        "content": error_text,
                        "type": "system",
                        "subtype": "api_error",
                        "systemSubtype": "api_error",  # Frontend compatibility
                        "systemData": {"content": error_text},
                        "metadata": {"timestamp": timestamp},
                        "streaming": False
                    })
                continue

            # Assistant message - contains thinking, text blocks, and tool use blocks
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get("type")

                        if block_type == "thinking":
                            # Extended thinking block
                            thinking_text = block.get("thinking", "")
                            if thinking_text:  # Only add non-empty thinking blocks
                                msg_counter += 1
                                messages.append({
                                    "id": f"thinking-{uuid}-{msg_counter}",
                                    "uuid": uuid,  # Raw JSONL UUID for rewind
                                    "role": "assistant",
                                    "content": thinking_text,
                                    "type": "thinking",
                                    "metadata": {
                                        "timestamp": timestamp,
                                        "model": message_data.get("model")
                                    },
                                    "streaming": False
                                })

                        elif block_type == "text":
                            text = block.get("text", "")
                            if text and text.strip():  # Only add non-empty, non-whitespace text blocks
                                msg_counter += 1
                                messages.append({
                                    "id": f"text-{uuid}-{msg_counter}",
                                    "uuid": uuid,  # Raw JSONL UUID for rewind
                                    "role": "assistant",
                                    "content": text,
                                    "type": "text",
                                    "metadata": {
                                        "timestamp": timestamp,
                                        "model": message_data.get("model")
                                    },
                                    "streaming": False
                                })

                        elif block_type == "tool_use":
                            msg_counter += 1
                            tool_id = block.get("id")
                            tool_name = block.get("name")
                            tool_input = block.get("input", {})

                            # Track tool name by ID for matching tool results
                            if tool_id and tool_name:
                                tool_names_by_id[tool_id] = tool_name

                            # Check if this is a subagent tool - create subagent message instead
                            # SDK ≤0.1.30 uses "Task", SDK ≥0.1.48 uses "Agent"
                            if tool_name in ("Task", "Agent"):
                                agent_type = (
                                    tool_input.get("subagent_type")
                                    or tool_input.get("description")
                                    or "Agent"
                                )
                                description = tool_input.get("description", "")
                                prompt = tool_input.get("prompt", "")

                                # Track this Task tool use for later matching with result
                                task_tool_uses[tool_id] = {
                                    "agent_type": agent_type,
                                    "description": description,
                                    "prompt": prompt
                                }

                                # For now, we don't have the agent_id from tool_use alone
                                # We'll need to match it from tool_result or find a pattern
                                # Create a placeholder subagent message
                                messages.append({
                                    "id": f"subagent-{tool_id}",
                                    "uuid": uuid,  # Raw JSONL UUID for rewind
                                    "role": "assistant",
                                    "content": "",
                                    "type": "subagent",
                                    "toolId": tool_id,
                                    "toolInput": tool_input,
                                    "agentType": agent_type,
                                    "agentDescription": description,
                                    "agentPrompt": prompt,
                                    "agentStatus": "pending",  # Will be updated when we find the result
                                    "agentChildren": [],
                                    "metadata": {"timestamp": timestamp},
                                    "streaming": False
                                })
                                if tool_id:
                                    messages_by_tool_id[tool_id] = len(messages) - 1
                            else:
                                messages.append({
                                    "id": f"tool-{uuid}-{tool_id or msg_counter}",
                                    "uuid": uuid,  # Raw JSONL UUID for rewind
                                    "role": "assistant",
                                    "content": "",
                                    "type": "tool_use",
                                    "toolName": tool_name,
                                    "toolId": tool_id,
                                    "toolInput": tool_input,
                                    "toolStatus": "running",  # Will be updated when result arrives
                                    "metadata": {"timestamp": timestamp},
                                    "streaming": False
                                })
                                if tool_id:
                                    messages_by_tool_id[tool_id] = len(messages) - 1

                        elif block_type == "server_tool_use":
                            # SDK 0.1.65+: server-executed tool calls (advisor,
                            # web_search, web_fetch). Render identically to a
                            # tool_use with a server_side badge; we never get a
                            # caller-side result, only the matching
                            # server_tool_result block below.
                            msg_counter += 1
                            tool_id = block.get("id")
                            tool_name = block.get("name")
                            tool_input = block.get("input", {})
                            if tool_id and tool_name:
                                tool_names_by_id[tool_id] = tool_name
                            messages.append({
                                "id": f"tool-{uuid}-{tool_id or msg_counter}",
                                "uuid": uuid,
                                "role": "assistant",
                                "content": "",
                                "type": "tool_use",
                                "toolName": tool_name,
                                "toolId": tool_id,
                                "toolInput": tool_input,
                                "toolStatus": "running",
                                "serverSide": True,
                                "metadata": {"timestamp": timestamp},
                                "streaming": False
                            })
                            if tool_id:
                                messages_by_tool_id[tool_id] = len(messages) - 1

                        elif block_type == "server_tool_result":
                            # SDK 0.1.65+: paired with server_tool_use above.
                            # Content is a dict, not a string. Attach to the
                            # prior tool_use row if present, else emit standalone.
                            tool_use_id = block.get("tool_use_id")
                            raw_content = block.get("content")
                            if isinstance(raw_content, (dict, list)):
                                import json as _json
                                output_text = _json.dumps(raw_content)
                            else:
                                output_text = str(raw_content) if raw_content is not None else ""
                            if tool_use_id and tool_use_id in messages_by_tool_id:
                                idx = messages_by_tool_id[tool_use_id]
                                messages[idx]["toolResult"] = output_text
                                messages[idx]["toolStatus"] = "complete"
                            else:
                                # Emit as a synthetic tool_use row with the
                                # result pre-attached. MessageArea.svelte
                                # unconditionally skips `type: "tool_result"`
                                # (standalone results are expected to merge
                                # into a prior tool_use), so a standalone
                                # tool_result here would silently render
                                # nothing. Rendering through the tool_use
                                # path preserves the server badge and result
                                # panel. ToolUseMessage.svelte gates its
                                # input section on Object.keys(input).length,
                                # so an empty toolInput renders cleanly.
                                msg_counter += 1
                                messages.append({
                                    "id": f"tool-{uuid}-{tool_use_id or msg_counter}",
                                    "uuid": uuid,
                                    "role": "assistant",
                                    "content": "",
                                    "type": "tool_use",
                                    "toolName": tool_names_by_id.get(tool_use_id, "server_tool"),
                                    "toolId": tool_use_id,
                                    "toolInput": {},
                                    "toolResult": output_text,
                                    "toolStatus": "complete",
                                    "serverSide": True,
                                    "metadata": {"timestamp": timestamp},
                                    "streaming": False
                                })

            elif isinstance(content, str) and content:
                # Plain string content (less common)
                msg_counter += 1
                messages.append({
                    "id": f"text-{uuid}",
                    "uuid": uuid,  # Raw JSONL UUID for rewind
                    "role": "assistant",
                    "content": content,
                    "type": "text",
                    "metadata": {
                        "timestamp": timestamp,
                        "model": message_data.get("model")
                    },
                    "streaming": False
                })

    logger.debug(f"Parsed {len(messages)} messages from JSONL history")

    # Merge plugin warning messages from hooks JSONL file using O(N+M) merge
    hook_messages = parse_hooks_jsonl(sdk_session_id, working_dir)
    if hook_messages:
        logger.debug(f"Merging {len(hook_messages)} hook messages from hooks.jsonl")
        # Both lists are roughly in timestamp order — merge them
        merged = []
        mi, hi = 0, 0
        while mi < len(messages) and hi < len(hook_messages):
            msg_ts = messages[mi].get("metadata", {}).get("timestamp", "")
            hook_ts = hook_messages[hi].get("metadata", {}).get("timestamp", "")
            if hook_ts and msg_ts and hook_ts < msg_ts:
                merged.append(hook_messages[hi])
                hi += 1
            else:
                merged.append(messages[mi])
                mi += 1
        merged.extend(messages[mi:])
        merged.extend(hook_messages[hi:])
        messages = merged
        logger.debug(f"Total messages after merging hooks: {len(messages)}")

    # Build usage_data from accumulated values
    usage_data = {}
    if total_input_tokens or total_output_tokens or last_usage:
        usage_data = {
            "total_tokens_in": total_input_tokens,
            "total_tokens_out": total_output_tokens,
            "cache_creation_tokens": last_usage.get("cache_creation_input_tokens", 0),
            "cache_read_tokens": last_usage.get("cache_read_input_tokens", 0),
            "last_input_tokens": last_usage.get("input_tokens", 0),
            "model": usage_model
        }

    return messages, usage_data


def get_session_cost_from_jsonl(
    sdk_session_id: str,
    working_dir: str = "/workspace"
) -> Dict[str, Any]:
    """
    Extract cost/usage information from JSONL file.

    Note: JSONL files don't contain total cost, only per-message usage.
    This extracts what we can find.

    Returns dict with:
    - total_tokens_in: Input tokens (not including cache tokens)
    - total_tokens_out: Output tokens
    - cache_creation_tokens: Tokens used to create cache entries
    - cache_read_tokens: Tokens read from cache (doesn't count toward context)
    - model: The model used
    """
    jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
    if not jsonl_path:
        return {}

    total_input_tokens = 0
    total_output_tokens = 0
    model = None

    # Track the last usage data - for cache tokens we want the final state
    last_usage = {}

    for entry in parse_jsonl_file(jsonl_path):
        if entry.get("type") == "assistant":
            message_data = entry.get("message", {})
            usage = message_data.get("usage", {})

            # Get model
            if not model:
                model = message_data.get("model")

            # Sum up input/output tokens (these are incremental per turn)
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)

            # Keep track of latest usage for cache tokens
            if usage:
                last_usage = usage

    # Cache tokens from final message represent current cache state
    # - cache_creation_input_tokens: take from last message (represents final cache size)
    # - cache_read_input_tokens: take from last message (represents what was read last)
    # For context window calculation, we need last turn's input tokens (not cumulative)
    return {
        "total_tokens_in": total_input_tokens,
        "total_tokens_out": total_output_tokens,
        "cache_creation_tokens": last_usage.get("cache_creation_input_tokens", 0),
        "cache_read_tokens": last_usage.get("cache_read_input_tokens", 0),
        # Last turn's input tokens for context calculation
        "last_input_tokens": last_usage.get("input_tokens", 0),
        "model": model
    }


def list_available_sessions(working_dir: str = "/workspace") -> List[Dict[str, Any]]:
    """
    List all available session files for a project directory.

    Returns list of dicts with: sdk_session_id, path, modified_at
    """
    claude_projects_dir = settings.get_claude_projects_dir
    project_dir_name = get_project_dir_name(working_dir)
    project_dir = claude_projects_dir / project_dir_name

    sessions = []

    if project_dir.exists():
        for jsonl_file in project_dir.glob("*.jsonl"):
            try:
                stat = jsonl_file.stat()
                sessions.append({
                    "sdk_session_id": jsonl_file.stem,
                    "path": str(jsonl_file),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                    "size_bytes": stat.st_size
                })
            except Exception as e:
                logger.warning(f"Failed to stat {jsonl_file}: {e}")

    # Sort by modified time, newest first
    sessions.sort(key=lambda x: x["modified_at"], reverse=True)
    return sessions


def _get_session_title_and_preview(jsonl_path: Path) -> tuple[Optional[str], Optional[str], int]:
    """
    Extract session title (first user message) and preview from JSONL file.

    Returns:
        tuple of (title, preview, message_count)
    """
    title = None
    preview = None
    message_count = 0

    try:
        for entry in parse_jsonl_file(jsonl_path):
            entry_type = entry.get("type")

            # Skip non-message entries
            if entry_type in ("queue-operation", "file-history-snapshot", "system"):
                continue

            # Skip meta and sidechain messages
            if entry.get("isMeta") or entry.get("isSidechain"):
                continue

            message_data = entry.get("message", {})
            role = message_data.get("role")
            content = message_data.get("content")

            if entry_type == "user" and role == "user":
                if isinstance(content, str) and not _is_system_content(content) and not _is_task_notification(content):
                    message_count += 1
                    if title is None:
                        # First user message is the title
                        title = content[:100].strip()
                        if len(content) > 100:
                            title = title.rsplit(' ', 1)[0] + "..."
                elif isinstance(content, list):
                    # Check for text blocks
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if text and not _is_system_content(text) and not _is_task_notification(text):
                                message_count += 1
                                if title is None:
                                    title = text[:100].strip()
                                    if len(text) > 100:
                                        title = title.rsplit(' ', 1)[0] + "..."
                                break

            elif entry_type == "assistant" and role == "assistant":
                message_count += 1
                # Get preview from first assistant response
                if preview is None and isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "")
                            if text:
                                preview = text[:150].strip()
                                if len(text) > 150:
                                    preview = preview.rsplit(' ', 1)[0] + "..."
                                break

    except Exception as e:
        logger.warning(f"Failed to extract title/preview from {jsonl_path}: {e}")

    return title, preview, message_count


def list_chat_history_sessions(
    working_dir: str = "/workspace",
    search: str = "",
    limit: int = 50
) -> List[Dict[str, Any]]:
    """
    List chat history sessions with rich metadata for the @ reference UI.

    Filters out agent-*.jsonl files as they are subagent logs, not main sessions.

    Args:
        working_dir: The project working directory
        search: Optional search filter for title/content
        limit: Maximum number of sessions to return

    Returns:
        List of session dicts with: name, path, title, preview, message_count,
        modified_at, size_bytes, type='chat_history'
    """
    claude_projects_dir = settings.get_claude_projects_dir
    project_dir_name = get_project_dir_name(working_dir)
    project_dir = claude_projects_dir / project_dir_name

    sessions = []
    search_lower = search.lower() if search else ""

    if not project_dir.exists():
        return sessions

    for jsonl_file in project_dir.glob("*.jsonl"):
        try:
            # Skip agent files (subagent logs)
            if jsonl_file.name.startswith("agent-"):
                continue

            stat = jsonl_file.stat()
            title, preview, message_count = _get_session_title_and_preview(jsonl_file)

            # Apply search filter
            if search_lower:
                title_match = title and search_lower in title.lower()
                preview_match = preview and search_lower in preview.lower()
                if not title_match and not preview_match:
                    continue

            # Build display name
            display_name = title or f"Session {jsonl_file.stem[:8]}..."

            sessions.append({
                "name": display_name,
                "type": "chat_history",
                "path": str(jsonl_file),
                "sdk_session_id": jsonl_file.stem,
                "title": title,
                "preview": preview,
                "message_count": message_count,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                "size_bytes": stat.st_size
            })

        except Exception as e:
            logger.warning(f"Failed to process {jsonl_file}: {e}")

    # Sort by modified time, newest first
    sessions.sort(key=lambda x: x["modified_at"], reverse=True)

    # Apply limit
    return sessions[:limit]
