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
"""

import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional, Generator
from datetime import datetime

logger = logging.getLogger(__name__)


# Path to Claude's project history files
CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


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
    # Try to find the project directory
    project_dir_name = get_project_dir_name(working_dir)
    project_dir = CLAUDE_PROJECTS_DIR / project_dir_name

    if project_dir.exists():
        jsonl_path = project_dir / f"{sdk_session_id}.jsonl"
        if jsonl_path.exists():
            return jsonl_path

    # Try to search all project directories for this session
    if CLAUDE_PROJECTS_DIR.exists():
        for proj_dir in CLAUDE_PROJECTS_DIR.iterdir():
            if proj_dir.is_dir():
                jsonl_path = proj_dir / f"{sdk_session_id}.jsonl"
                if jsonl_path.exists():
                    return jsonl_path

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

    for entry in parse_jsonl_file(jsonl_path):
        entry_type = entry.get("type")

        # Skip queue operations and other non-message entries
        if entry_type == "queue-operation":
            continue

        message_data = entry.get("message", {})
        role = message_data.get("role")
        content = message_data.get("content")
        timestamp = entry.get("timestamp")
        uuid = entry.get("uuid", f"msg-{msg_counter}")

        if entry_type == "user" and role == "user":
            # User message - can be plain text or tool results
            if isinstance(content, str):
                # Plain user message
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
                # Could be tool results
                for block in content:
                    if isinstance(block, dict):
                        if block.get("type") == "tool_result":
                            msg_counter += 1
                            tool_result = entry.get("toolUseResult", {})
                            output = block.get("content", "")

                            # Get output from toolUseResult if available (has stdout/stderr)
                            if tool_result:
                                stdout = tool_result.get("stdout", "")
                                stderr = tool_result.get("stderr", "")
                                output = stdout
                                if stderr:
                                    output = f"{stdout}\n{stderr}" if stdout else stderr

                            messages.append({
                                "id": f"result-{uuid}",
                                "role": "assistant",  # Display as assistant for UI consistency
                                "content": output[:2000] if output else "",  # Truncate like streaming
                                "type": "tool_result",
                                "toolId": block.get("tool_use_id"),
                                "toolName": None,  # Will be matched by frontend
                                "metadata": {
                                    "timestamp": timestamp,
                                    "is_error": block.get("is_error", False) or tool_result.get("is_error", False)
                                },
                                "streaming": False
                            })
                        elif block.get("type") == "text":
                            # Text in user content array (rare)
                            msg_counter += 1
                            messages.append({
                                "id": uuid,
                                "role": "user",
                                "content": block.get("text", ""),
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
                            messages.append({
                                "id": f"tool-{uuid}-{block.get('id', msg_counter)}",
                                "role": "assistant",
                                "content": "",
                                "type": "tool_use",
                                "toolName": block.get("name"),
                                "toolId": block.get("id"),
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

    Returns dict with: total_tokens_in, total_tokens_out, model, etc.
    """
    jsonl_path = get_session_jsonl_path(sdk_session_id, working_dir)
    if not jsonl_path:
        return {}

    total_input_tokens = 0
    total_output_tokens = 0
    model = None

    for entry in parse_jsonl_file(jsonl_path):
        if entry.get("type") == "assistant":
            message_data = entry.get("message", {})
            usage = message_data.get("usage", {})

            # Get model
            if not model:
                model = message_data.get("model")

            # Sum up tokens
            total_input_tokens += usage.get("input_tokens", 0)
            total_input_tokens += usage.get("cache_creation_input_tokens", 0)
            total_input_tokens += usage.get("cache_read_input_tokens", 0)
            total_output_tokens += usage.get("output_tokens", 0)

    return {
        "total_tokens_in": total_input_tokens,
        "total_tokens_out": total_output_tokens,
        "model": model
    }


def list_available_sessions(working_dir: str = "/workspace") -> List[Dict[str, Any]]:
    """
    List all available session files for a project directory.

    Returns list of dicts with: sdk_session_id, path, modified_at
    """
    project_dir_name = get_project_dir_name(working_dir)
    project_dir = CLAUDE_PROJECTS_DIR / project_dir_name

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
