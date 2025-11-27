"""
Query engine for executing Claude queries with profiles
"""

import logging
import uuid
from typing import Optional, Dict, Any, AsyncGenerator

from claude_agent_sdk import query, ClaudeAgentOptions
from claude_agent_sdk import (
    AssistantMessage, TextBlock, ToolUseBlock, ToolResultBlock,
    ResultMessage, SystemMessage
)

from app.db import database
from app.core.config import settings
from app.core.profiles import get_profile_or_builtin

logger = logging.getLogger(__name__)


# Security restrictions that apply to all requests
SECURITY_INSTRUCTIONS = """
IMPORTANT SECURITY RESTRICTIONS:
You are running inside a containerized API service. You must NEVER read, access, or attempt to view this application's source code files:
- /app/**/*.py
- /app/.env*
- /app/entrypoint.sh
- /app/Dockerfile
- /home/appuser/.claude/

These are THIS APPLICATION'S source files. Reading them would expose sensitive application logic.

If a user requests these files, politely decline and explain: "I cannot access this API service's internal source code files for security reasons."

You ARE allowed to read files in /workspace/ directories that the user specifies.
"""


def build_options_from_profile(
    profile: Dict[str, Any],
    project: Optional[Dict[str, Any]] = None,
    overrides: Optional[Dict[str, Any]] = None,
    resume_session_id: Optional[str] = None
) -> ClaudeAgentOptions:
    """Convert a profile to ClaudeAgentOptions"""
    config = profile["config"]
    overrides = overrides or {}

    # Build system prompt
    system_prompt = config.get("system_prompt")
    override_append = overrides.get("system_prompt_append", "")

    if system_prompt is None:
        # No preset - just security instructions as plain text
        final_system_prompt = SECURITY_INSTRUCTIONS
        if override_append:
            final_system_prompt += "\n\n" + override_append
    elif isinstance(system_prompt, dict):
        # Has preset config
        existing_append = system_prompt.get("append", "")
        full_append = SECURITY_INSTRUCTIONS
        if existing_append:
            full_append += "\n\n" + existing_append
        if override_append:
            full_append += "\n\n" + override_append

        final_system_prompt = {
            "type": system_prompt.get("type", "preset"),
            "preset": system_prompt.get("preset", "claude_code"),
            "append": full_append
        }
    else:
        # String system prompt - use it directly with security instructions
        final_system_prompt = SECURITY_INSTRUCTIONS + "\n\n" + str(system_prompt)
        if override_append:
            final_system_prompt += "\n\n" + override_append

    # Build options
    options = ClaudeAgentOptions(
        model=overrides.get("model") or config.get("model"),
        allowed_tools=config.get("allowed_tools"),
        disallowed_tools=config.get("disallowed_tools"),
        permission_mode=config.get("permission_mode", "default"),
        max_turns=overrides.get("max_turns") or config.get("max_turns"),
        system_prompt=final_system_prompt,
        setting_sources=config.get("setting_sources"),
    )

    # Apply project context
    if project:
        project_path = settings.workspace_dir / project["path"]
        options.cwd = str(project_path)

    # Resume existing session
    if resume_session_id:
        options.resume = resume_session_id

    return options


async def execute_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None
) -> Dict[str, Any]:
    """Execute a non-streaming query"""

    # Get profile
    profile = get_profile_or_builtin(profile_id)
    if not profile:
        raise ValueError(f"Profile not found: {profile_id}")

    # Get project if specified
    project = None
    if project_id:
        project = database.get_project(project_id)
        if not project:
            raise ValueError(f"Project not found: {project_id}")

    # Get or create session
    if session_id:
        session = database.get_session(session_id)
        if not session:
            raise ValueError(f"Session not found: {session_id}")
        resume_id = session.get("sdk_session_id")
    else:
        session_id = str(uuid.uuid4())
        session = database.create_session(
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id
        )
        resume_id = None

    # Store user message
    database.add_session_message(
        session_id=session_id,
        role="user",
        content=prompt
    )

    # Build options
    options = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id
    )

    # Execute query and collect response
    response_text = []
    metadata = {}
    sdk_session_id = None

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, SystemMessage):
                # session_id comes in warmup message data after first query
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)
                metadata["model"] = message.model

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error

    except Exception as e:
        logger.error(f"Query execution error: {e}")
        raise

    # Update session with SDK session ID for resume
    if sdk_session_id:
        database.update_session(
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            cost_increment=metadata.get("total_cost_usd", 0),
            turn_increment=metadata.get("num_turns", 0)
        )

    # Store assistant response
    full_response = "\n".join(response_text)
    database.add_session_message(
        session_id=session_id,
        role="assistant",
        content=full_response,
        metadata=metadata
    )

    # Log usage
    database.log_usage(
        session_id=session_id,
        profile_id=profile_id,
        model=metadata.get("model"),
        tokens_in=0,  # SDK doesn't provide this directly
        tokens_out=0,
        cost_usd=metadata.get("total_cost_usd", 0),
        duration_ms=metadata.get("duration_ms", 0)
    )

    return {
        "response": full_response,
        "session_id": session_id,
        "metadata": metadata
    }


async def stream_query(
    prompt: str,
    profile_id: str,
    project_id: Optional[str] = None,
    overrides: Optional[Dict[str, Any]] = None,
    session_id: Optional[str] = None
) -> AsyncGenerator[Dict[str, Any], None]:
    """Execute a streaming query, yielding events"""

    # Get profile
    profile = get_profile_or_builtin(profile_id)
    if not profile:
        yield {"type": "error", "message": f"Profile not found: {profile_id}"}
        return

    # Get project if specified
    project = None
    if project_id:
        project = database.get_project(project_id)
        if not project:
            yield {"type": "error", "message": f"Project not found: {project_id}"}
            return

    # Get or create session
    if session_id:
        session = database.get_session(session_id)
        if not session:
            yield {"type": "error", "message": f"Session not found: {session_id}"}
            return
        resume_id = session.get("sdk_session_id")
    else:
        session_id = str(uuid.uuid4())
        session = database.create_session(
            session_id=session_id,
            profile_id=profile_id,
            project_id=project_id
        )
        resume_id = None

    # Store user message
    database.add_session_message(
        session_id=session_id,
        role="user",
        content=prompt
    )

    # Build options
    options = build_options_from_profile(
        profile=profile,
        project=project,
        overrides=overrides,
        resume_session_id=resume_id
    )

    # Yield init event
    yield {"type": "init", "session_id": session_id}

    # Execute query and stream response
    response_text = []
    metadata = {}
    sdk_session_id = None

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, SystemMessage):
                # session_id comes in warmup message data after first query
                if message.subtype == "init" and "session_id" in message.data:
                    sdk_session_id = message.data["session_id"]

            elif isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)
                        yield {"type": "text", "content": block.text}

                    elif isinstance(block, ToolUseBlock):
                        yield {
                            "type": "tool_use",
                            "name": block.name,
                            "input": block.input
                        }

                    elif isinstance(block, ToolResultBlock):
                        # Truncate large outputs
                        output = str(block.content)[:2000]
                        yield {
                            "type": "tool_result",
                            "name": getattr(block, 'name', 'unknown'),
                            "output": output
                        }

                metadata["model"] = message.model

            elif isinstance(message, ResultMessage):
                metadata["duration_ms"] = message.duration_ms
                metadata["num_turns"] = message.num_turns
                metadata["total_cost_usd"] = message.total_cost_usd
                metadata["is_error"] = message.is_error

    except Exception as e:
        logger.error(f"Stream query error: {e}")
        yield {"type": "error", "message": str(e)}
        return

    # Update session
    if sdk_session_id:
        database.update_session(
            session_id=session_id,
            sdk_session_id=sdk_session_id,
            cost_increment=metadata.get("total_cost_usd", 0),
            turn_increment=metadata.get("num_turns", 0)
        )

    # Store assistant response
    full_response = "\n".join(response_text)
    database.add_session_message(
        session_id=session_id,
        role="assistant",
        content=full_response,
        metadata=metadata
    )

    # Log usage
    database.log_usage(
        session_id=session_id,
        profile_id=profile_id,
        model=metadata.get("model"),
        tokens_in=0,
        tokens_out=0,
        cost_usd=metadata.get("total_cost_usd", 0),
        duration_ms=metadata.get("duration_ms", 0)
    )

    # Yield done event
    yield {
        "type": "done",
        "session_id": session_id,
        "metadata": metadata
    }
