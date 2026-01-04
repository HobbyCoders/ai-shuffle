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
    D:\\Development\\ai-shuffle -> D--Development-ai-shuffle

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
    - Interrupt messages
    - Empty XML tags

    NOTE: <local-command-stdout> is NOT filtered - it contains actual command output
    like /context and /compact results that should be displayed.
    """
    if not content:
        return True

    # Check for system/meta content patterns
    # NOTE: <local-command-stdout> is intentionally NOT in this list
    system_patterns = [
        "<command-name>",
        "<command-message>",
        "<command-args>",
        "[Request interrupted by user]",
        "Caveat: The messages below were generated",
    ]

    content_lower = content.strip()
    for pattern in system_patterns:
        if content_lower.startswith(pattern):
            return True

    return False


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
) -> List[Dict[str, Any]]:
    """
    Parse a session's JSONL history file and return messages in streaming format.

    This transforms the JSONL format to match the format used by live streaming,
    ensuring visual consistency between resumed and live sessions.

    Also parses associated agent JSONL files for Task tool calls and creates
    subagent message groups.

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

    # Find all agent files for this session
    agent_files = get_agent_jsonl_paths(sdk_session_id, working_dir)
    agent_children_cache: Dict[str, List[Dict[str, Any]]] = {}

    # Pre-parse all agent files
    for agent_id, agent_path in agent_files.items():
        agent_children_cache[agent_id] = parse_agent_history(agent_path)
        logger.debug(f"Parsed {len(agent_children_cache[agent_id])} messages from agent {agent_id}")

    messages: List[Dict[str, Any]] = []
    msg_counter = 0

    # Track tool names by ID for matching tool_result to tool_use
    tool_names_by_id: Dict[str, str] = {}

    # Track Task tool uses that have been converted to subagent messages
    # Maps tool_use_id to agent info for matching with tool_result
    task_tool_uses: Dict[str, Dict[str, Any]] = {}

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

        # Handle system messages (e.g., compact_boundary)
        if entry_type == "system":
            subtype = entry.get("subtype")
            system_content = entry.get("content", "")
            msg_counter += 1

            messages.append({
                "id": uuid,
                "role": "system",
                "content": system_content,
                "type": "system",
                "subtype": subtype,
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
                # Check for local command output (e.g., /context, /compact)
                local_output = _extract_local_command_output(content)
                if local_output is not None:
                    msg_counter += 1
                    messages.append({
                        "id": uuid,
                        "role": "system",
                        "content": local_output,
                        "type": "system",
                        "subtype": "local_command",
                        "metadata": {"timestamp": timestamp},
                        "streaming": False
                    })
                # Plain user message - skip empty content and system/command-related messages
                elif content and not _is_system_content(content):
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
                                # task_info available for future extended metadata handling
                                _task_info = task_tool_uses[tool_use_id]

                                # Extract agentId from toolUseResult if available
                                tool_use_result = entry.get("toolUseResult", {})
                                agent_id = tool_use_result.get("agentId") if isinstance(tool_use_result, dict) else None

                                # Find and update the subagent message
                                for msg in messages:
                                    if msg.get("type") == "subagent" and msg.get("toolId") == tool_use_id:
                                        msg["content"] = output[:2000] if output else ""
                                        msg["agentStatus"] = "error" if is_error else "completed"

                                        # Use agentId from toolUseResult to find the right agent children
                                        if agent_id and agent_id in agent_children_cache:
                                            msg["agentId"] = agent_id
                                            msg["agentChildren"] = agent_children_cache[agent_id]
                                            # Clear from cache to avoid double-assignment
                                            agent_children_cache[agent_id] = []
                                        break

                                # Don't add a separate tool_result message for Task tools
                                continue

                            # Group tool result with its corresponding tool_use message
                            # Find the matching tool_use and embed the result
                            found_tool_use = False
                            for msg in messages:
                                if msg.get("type") == "tool_use" and msg.get("toolId") == tool_use_id:
                                    msg["toolResult"] = output[:2000] if output else ""
                                    msg["toolStatus"] = "error" if is_error else "complete"
                                    found_tool_use = True
                                    break

                            # Only create separate tool_result if no matching tool_use found
                            if not found_tool_use:
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
                            tool_input = block.get("input", {})

                            # Track tool name by ID for matching tool results
                            if tool_id and tool_name:
                                tool_names_by_id[tool_id] = tool_name

                            # Check if this is a Task tool - create subagent message instead
                            if tool_name == "Task":
                                agent_type = tool_input.get("subagent_type", "unknown")
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
                            else:
                                messages.append({
                                    "id": f"tool-{uuid}-{tool_id or msg_counter}",
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
                if isinstance(content, str) and not _is_system_content(content):
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
                            if text and not _is_system_content(text):
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
