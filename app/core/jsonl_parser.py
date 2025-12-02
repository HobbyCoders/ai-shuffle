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
from pathlib import Path
from typing import List, Dict, Any, Optional, Generator
from datetime import datetime

from app.core.config import settings

logger = logging.getLogger(__name__)


def get_project_dir_name(working_dir: str) -> str:
    """
    Convert a working directory path to Claude's project directory name.

    Claude converts paths like /workspace/github-projects to -workspace-github-projects
    The leading dash is kept as that's how Claude encodes absolute paths.
    """
    # Replace path separators with dashes (keeps leading dash from absolute path)
    return working_dir.replace("/", "-")


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
    Check if content is a system/meta message that should be filtered out.

    These include:
    - Command-related tags like <command-name>, <command-message>, etc.
    - Local command stdout markers
    - Interrupt messages
    - Empty XML tags
    """
    if not content:
        return True

    # Check for system/meta content patterns
    system_patterns = [
        "<command-",
        "<local-command-stdout>",
        "[Request interrupted by user]",
        "Caveat: The messages below were generated",
    ]

    content_lower = content.strip()
    for pattern in system_patterns:
        if content_lower.startswith(pattern):
            return True

    return False


def parse_session_history(
    sdk_session_id: str,
    working_dir: str = "/workspace"
) -> List[Dict[str, Any]]:
    """
    Parse a session's JSONL history file and return messages in streaming format.

    This transforms the JSONL format to match the format used by live streaming,
    ensuring visual consistency between resumed and live sessions.

    Args:
        sdk_session_id: The Claude SDK session ID
        working_dir: The working directory for finding the project

    Returns:
        List of messages in the same format as WebSocket streaming events
        Each message has: id, role, content, type, toolName, toolId, toolInput, metadata, streaming
    """
    jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
    if not jsonl_path:
        logger.warning(f"JSONL file not found for session {sdk_session_id}")
        return []

    logger.info(f"Parsing JSONL history from {jsonl_path}")

    messages: List[Dict[str, Any]] = []
    msg_counter = 0

    # Track tool names by ID for matching tool_result to tool_use
    tool_names_by_id: Dict[str, str] = {}

    for entry in parse_jsonl_file(jsonl_path):
        entry_type = entry.get("type")

        # Skip non-message entries
        if entry_type in ("queue-operation", "file-history-snapshot"):
            continue

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

        if entry_type == "user" and role == "user":
            # User message - can be plain text, tool results, or array with text blocks
            if isinstance(content, str):
                # Plain user message - skip empty content and system/command-related messages
                if content and not _is_system_content(content):
                    msg_counter += 1
                    messages.append({
                        "id": uuid,
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
                                        output = extract_text_from_content(tr_content)
                                    elif isinstance(tr_content, str):
                                        output = tr_content
                                    else:
                                        output = str(tr_content)
                                elif tool_result.get("result"):
                                    output = str(tool_result.get("result", ""))
                            elif tool_result and isinstance(tool_result, str):
                                # Sometimes toolUseResult is just a string (error messages)
                                output = tool_result

                            messages.append({
                                "id": f"result-{uuid}-{tool_use_id}",
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
                            if text and not _is_system_content(text):
                                has_text_content = True
                                text_parts.append(text)

                # If we collected text blocks, create a single user message
                if has_text_content and text_parts:
                    combined_text = "\n".join(text_parts)
                    msg_counter += 1
                    messages.append({
                        "id": uuid,
                        "role": "user",
                        "content": combined_text,
                        "type": None,
                        "metadata": {"timestamp": timestamp},
                        "streaming": False
                    })

        elif entry_type == "assistant" and role == "assistant":
            # Assistant message - contains text blocks and tool use blocks
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict):
                        block_type = block.get("type")

                        if block_type == "text":
                            text = block.get("text", "")
                            if text:  # Only add non-empty text blocks
                                msg_counter += 1
                                messages.append({
                                    "id": f"text-{uuid}-{msg_counter}",
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

                            # Track tool name by ID for matching tool results
                            if tool_id and tool_name:
                                tool_names_by_id[tool_id] = tool_name

                            messages.append({
                                "id": f"tool-{uuid}-{tool_id or msg_counter}",
                                "role": "assistant",
                                "content": "",
                                "type": "tool_use",
                                "toolName": tool_name,
                                "toolId": tool_id,
                                "toolInput": block.get("input", {}),
                                "metadata": {"timestamp": timestamp},
                                "streaming": False
                            })

            elif isinstance(content, str) and content:
                # Plain string content (less common)
                msg_counter += 1
                messages.append({
                    "id": f"text-{uuid}",
                    "role": "assistant",
                    "content": content,
                    "type": "text",
                    "metadata": {
                        "timestamp": timestamp,
                        "model": message_data.get("model")
                    },
                    "streaming": False
                })

    logger.info(f"Parsed {len(messages)} messages from JSONL history")
    return messages


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
    cache_creation_tokens = 0
    cache_read_tokens = 0
    model = None

    for entry in parse_jsonl_file(jsonl_path):
        if entry.get("type") == "assistant":
            message_data = entry.get("message", {})
            usage = message_data.get("usage", {})

            # Get model
            if not model:
                model = message_data.get("model")

            # Sum up tokens - keep cache tokens separate
            total_input_tokens += usage.get("input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)
            cache_creation_tokens += usage.get("cache_creation_input_tokens", 0)
            cache_read_tokens += usage.get("cache_read_input_tokens", 0)

    return {
        "total_tokens_in": total_input_tokens,
        "total_tokens_out": total_output_tokens,
        "cache_creation_tokens": cache_creation_tokens,
        "cache_read_tokens": cache_read_tokens,
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
