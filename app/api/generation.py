"""
AI Generation API for profiles and sub-agents

Uses Claude to generate configurations based on user descriptions.
"""

import json
import logging
import re
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException, Depends, status
from pydantic import BaseModel, Field

from claude_agent_sdk import query, ClaudeAgentOptions
from claude_agent_sdk import AssistantMessage, TextBlock

from app.api.auth import require_auth
from app.core.models import AgentExport, AgentExportData

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/generate", tags=["Generation"])


# ============================================================================
# Request/Response Models
# ============================================================================

class GenerateProfileRequest(BaseModel):
    """Request to generate a profile configuration"""
    description: str = Field(..., min_length=10, max_length=2000, description="Description of the desired profile")
    model: Optional[str] = Field(None, description="Model to use for generation (haiku, sonnet, opus)")


class GenerateSubagentRequest(BaseModel):
    """Request to generate a subagent configuration"""
    description: str = Field(..., min_length=10, max_length=2000, description="Description of the desired subagent")
    model: Optional[str] = Field(None, description="Model to use for generation (haiku, sonnet, opus)")


class SubagentExportData(BaseModel):
    """Subagent configuration data for export"""
    id: str
    name: str
    description: str
    prompt: str
    tools: Optional[list[str]] = None
    model: Optional[str] = None


class SubagentExport(BaseModel):
    """Full subagent export format"""
    version: str = "1.0"
    type: str = "ai-shuffle-subagent"
    exported_at: str
    subagent: SubagentExportData


# ============================================================================
# Generation Prompts
# ============================================================================

PROFILE_GENERATION_PROMPT = '''You are generating a Claude profile configuration. Based on the user's description, create a complete profile with appropriate settings.

## Available Configuration Fields

### Required Fields
- **name**: Display name for the profile (string, 1-100 chars)
- **description**: Brief description of the profile's purpose (string, can be null)

### Model Settings
- **model**: One of "haiku", "sonnet", "sonnet-1m", "opus" (default: "sonnet")
- **permission_mode**: One of "default", "acceptEdits", "plan", "bypassPermissions"
  - default: Ask for permission on sensitive actions
  - acceptEdits: Auto-accept file edits
  - plan: Plan-only mode, no execution
  - bypassPermissions: Skip all permission prompts
- **thinking_mode**: One of "default", "think", "think_harder", "ultra_think"
  - default: Standard thinking
  - think: Extended thinking with more reasoning
  - think_harder: Even more thorough reasoning
  - ultra_think: Maximum reasoning depth

### Tool Configuration
- **allowed_tools**: Array of tool names this profile can use (null = all tools)
- **disallowed_tools**: Array of tool names to block (null = none blocked)

### System Prompt
- **system_prompt_type**: Either "preset" or "custom"
- **system_prompt_preset**: When type is "preset", one of: "claude_code", "code_review", "explain_code", "debug_help", "write_tests", "refactor_code", "write_documentation"
- **system_prompt_content**: When type is "custom", the full system prompt text
- **system_prompt_append**: Additional text to append to the system prompt (works with both types)

### Session Behavior
- **max_turns**: Maximum conversation turns (integer, null = unlimited)
- **include_partial_messages**: Enable streaming partial messages (boolean, default: true)
- **continue_conversation**: Continue most recent conversation (boolean, default: false)
- **fork_session**: Fork instead of continuing when resuming (boolean, default: false)

### Advanced
- **enabled_agents**: Array of subagent IDs this profile can use (null = none)
- **setting_sources**: Array of setting sources to load from (["user", "project", "local"])
- **env**: Dictionary of environment variables to set (string keys and values)

## User's Description
{description}

## Output Format
Output ONLY a valid JSON object matching this exact structure (no markdown, no explanation, just JSON):
{{
  "name": "...",
  "description": "...",
  "model": "sonnet",
  "permission_mode": "default",
  "thinking_mode": "default",
  "system_prompt_type": "preset",
  "system_prompt_preset": "claude_code",
  "system_prompt_content": null,
  "system_prompt_append": null,
  "allowed_tools": null,
  "disallowed_tools": null,
  "enabled_agents": null,
  "max_turns": null,
  "include_partial_messages": true,
  "continue_conversation": false,
  "fork_session": false,
  "setting_sources": null,
  "env": null
}}

Generate settings that best match the user's description. Be creative with the name and description. If they mention specific use cases, adjust the model, permission_mode, and system_prompt accordingly.'''


SUBAGENT_GENERATION_PROMPT = '''You are generating a Claude sub-agent configuration. Sub-agents are specialized AI assistants that the main agent can delegate tasks to.

## Available Configuration Fields

### Required Fields
- **id**: Unique identifier (lowercase letters, numbers, and hyphens only, e.g., "code-reviewer")
- **name**: Display name for the subagent (string, 1-100 chars)
- **description**: When to use this agent - this is shown to the main agent to help it decide when to delegate (string, very important!)
- **prompt**: System prompt/instructions for the sub-agent (string, detailed instructions for the agent)

### Optional Fields
- **tools**: Array of tool names this subagent can use. If null, inherits all tools.
  Available tools include: Read, Write, Edit, Bash, Glob, Grep, WebFetch, WebSearch, Task, TodoWrite, NotebookEdit, KillShell
- **model**: Model override for this subagent. One of "haiku", "sonnet", "opus", or null to inherit from parent.
  - Use "haiku" for fast, simple tasks (searches, summaries)
  - Use "sonnet" for balanced tasks (analysis, moderate complexity)
  - Use "opus" for complex tasks (code writing, architecture)

## User's Description
{description}

## Output Format
Output ONLY a valid JSON object matching this exact structure (no markdown, no explanation, just JSON):
{{
  "id": "...",
  "name": "...",
  "description": "...",
  "prompt": "...",
  "tools": null,
  "model": null
}}

Important guidelines:
1. The **description** field is critical - it tells the main agent WHEN to use this subagent. Be specific about the use cases.
2. The **prompt** field should be detailed instructions for HOW the subagent should behave and what it should do.
3. Choose an appropriate model based on task complexity - use "haiku" for quick tasks, "sonnet" for balanced work, "opus" for complex reasoning.
4. Only specify tools if the subagent needs a limited set - otherwise leave as null to inherit all.
5. Generate a meaningful id that reflects the subagent's purpose.'''


# ============================================================================
# Helper Functions
# ============================================================================

def extract_json_from_response(text: str) -> dict:
    """
    Extract JSON from Claude's response, handling markdown code blocks.

    Args:
        text: Raw text response from Claude

    Returns:
        Parsed JSON dictionary

    Raises:
        ValueError: If no valid JSON found
    """
    # Try to parse as-is first
    text = text.strip()

    # Remove markdown code blocks if present
    if text.startswith("```"):
        # Find the end of the code block
        lines = text.split("\n")
        # Skip first line (```json or ```)
        start_idx = 1
        end_idx = len(lines)
        for i in range(1, len(lines)):
            if lines[i].strip() == "```":
                end_idx = i
                break
        text = "\n".join(lines[start_idx:end_idx]).strip()

    # Try to find JSON object in the text
    # Look for opening { and find matching }
    start = text.find("{")
    if start == -1:
        raise ValueError("No JSON object found in response")

    # Find the matching closing brace
    depth = 0
    end = start
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    json_str = text[start:end]

    try:
        return json.loads(json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in response: {e}")


async def generate_with_claude(prompt: str, model: Optional[str] = None) -> str:
    """
    Generate a response using Claude SDK.

    Args:
        prompt: The prompt to send to Claude
        model: Optional model override (haiku, sonnet, opus)

    Returns:
        Text response from Claude
    """
    options = ClaudeAgentOptions(
        model=model or "haiku",  # Use haiku by default for fast generation
        max_turns=1,
        permission_mode="bypassPermissions",
        tools=[],  # No tools needed for generation
    )

    response_text = []

    try:
        async for message in query(prompt=prompt, options=options):
            if isinstance(message, AssistantMessage):
                for block in message.content:
                    if isinstance(block, TextBlock):
                        response_text.append(block.text)
    except Exception as e:
        logger.error(f"Claude SDK error during generation: {e}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"AI generation failed: {str(e)}"
        )

    return "".join(response_text)


# ============================================================================
# API Endpoints
# ============================================================================

@router.post("/profile", response_model=AgentExport)
async def generate_profile(
    request: GenerateProfileRequest,
    token: str = Depends(require_auth)
):
    """
    Generate a profile configuration using AI.

    Takes a natural language description and generates a complete profile
    configuration in the AgentExport format (same as profile export).
    """
    if not request.description.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Description cannot be empty"
        )

    # Build the prompt
    prompt = PROFILE_GENERATION_PROMPT.format(description=request.description)

    # Generate with Claude
    logger.info(f"Generating profile from description: {request.description[:100]}...")
    response_text = await generate_with_claude(prompt, request.model)

    # Parse the response
    try:
        generated = extract_json_from_response(response_text)
    except ValueError as e:
        logger.error(f"Failed to parse generated profile: {e}")
        logger.error(f"Raw response: {response_text[:500]}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to parse AI response: {str(e)}"
        )

    # Validate required fields
    if not generated.get("name"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI did not generate a profile name"
        )

    # Build the export format
    agent_data = AgentExportData(
        name=generated.get("name", "Generated Profile"),
        description=generated.get("description"),
        model=generated.get("model", "sonnet"),
        permission_mode=generated.get("permission_mode", "default"),
        system_prompt_type=generated.get("system_prompt_type", "preset"),
        system_prompt_preset=generated.get("system_prompt_preset", "claude_code"),
        system_prompt_content=generated.get("system_prompt_content"),
        system_prompt_append=generated.get("system_prompt_append"),
        system_prompt_inject_env=False,
        allowed_tools=generated.get("allowed_tools"),
        disallowed_tools=generated.get("disallowed_tools"),
        enabled_agents=generated.get("enabled_agents"),
        ai_tools=None,
        max_turns=generated.get("max_turns"),
        max_buffer_size=None,
        include_partial_messages=generated.get("include_partial_messages", True),
        continue_conversation=generated.get("continue_conversation", False),
        fork_session=generated.get("fork_session", False),
        setting_sources=generated.get("setting_sources"),
        env=generated.get("env"),
    )

    export_data = AgentExport(
        version="1.0",
        type="ai-shuffle-agent",
        exported_at=datetime.utcnow().isoformat() + "Z",
        agent=agent_data
    )

    logger.info(f"Successfully generated profile: {agent_data.name}")
    return export_data


@router.post("/subagent", response_model=SubagentExport)
async def generate_subagent(
    request: GenerateSubagentRequest,
    token: str = Depends(require_auth)
):
    """
    Generate a subagent configuration using AI.

    Takes a natural language description and generates a complete subagent
    configuration in a structured export format.
    """
    if not request.description.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Description cannot be empty"
        )

    # Build the prompt
    prompt = SUBAGENT_GENERATION_PROMPT.format(description=request.description)

    # Generate with Claude
    logger.info(f"Generating subagent from description: {request.description[:100]}...")
    response_text = await generate_with_claude(prompt, request.model)

    # Parse the response
    try:
        generated = extract_json_from_response(response_text)
    except ValueError as e:
        logger.error(f"Failed to parse generated subagent: {e}")
        logger.error(f"Raw response: {response_text[:500]}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to parse AI response: {str(e)}"
        )

    # Validate required fields
    if not generated.get("id"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI did not generate a subagent ID"
        )
    if not generated.get("name"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI did not generate a subagent name"
        )
    if not generated.get("description"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI did not generate a subagent description"
        )
    if not generated.get("prompt"):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="AI did not generate a subagent prompt"
        )

    # Sanitize the ID (lowercase, hyphens only)
    subagent_id = re.sub(r'[^a-z0-9-]', '-', generated["id"].lower())
    subagent_id = re.sub(r'-+', '-', subagent_id).strip('-')

    # Build the export format
    subagent_data = SubagentExportData(
        id=subagent_id,
        name=generated.get("name", "Generated Subagent"),
        description=generated.get("description", ""),
        prompt=generated.get("prompt", ""),
        tools=generated.get("tools"),
        model=generated.get("model"),
    )

    export_data = SubagentExport(
        version="1.0",
        type="ai-shuffle-subagent",
        exported_at=datetime.utcnow().isoformat() + "Z",
        subagent=subagent_data
    )

    logger.info(f"Successfully generated subagent: {subagent_data.name} ({subagent_data.id})")
    return export_data
