"""
Profile management API routes
"""

from typing import List, Optional
from datetime import datetime
import re

from fastapi import APIRouter, HTTPException, Depends, status, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.models import (
    Profile, ProfileCreate, ProfileUpdate,
    AgentExport, AgentExportData, AgentImportRequest
)
from app.db import database
from app.api.auth import require_auth, require_admin, get_api_user_from_request

router = APIRouter(prefix="/api/v1/profiles", tags=["Profiles"])


# Response model for subagent (matches frontend expectations)
class SubagentResponse(BaseModel):
    """Subagent response model for profile agents endpoint"""
    id: str
    name: str
    description: str
    prompt: str
    tools: Optional[List[str]] = None
    model: Optional[str] = None
    is_builtin: bool = False
    created_at: datetime
    updated_at: datetime


# Request model for updating enabled agents
class EnabledAgentsRequest(BaseModel):
    """Request to update enabled agents for a profile"""
    enabled_agents: List[str] = Field(..., description="List of subagent IDs to enable")


@router.get("", response_model=List[Profile])
async def list_profiles(request: Request, token: str = Depends(require_auth)):
    """List all agent profiles. API users only see their assigned profile."""
    api_user = get_api_user_from_request(request)

    if api_user and api_user.get("profile_id"):
        # API user with assigned profile - only return that profile
        profile = database.get_profile(api_user["profile_id"])
        return [profile] if profile else []

    # Admin or unrestricted API user - return all profiles
    profiles = database.get_all_profiles()
    return profiles


@router.get("/{profile_id}", response_model=Profile)
async def get_profile(request: Request, profile_id: str, token: str = Depends(require_auth)):
    """Get a specific profile. API users can only access their assigned profile."""
    api_user = get_api_user_from_request(request)

    # Check if API user is restricted to a specific profile
    if api_user and api_user.get("profile_id"):
        if api_user["profile_id"] != profile_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this profile"
            )

    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )
    return profile


@router.get("/{profile_id}/agents", response_model=List[SubagentResponse])
async def get_profile_agents(
    request: Request,
    profile_id: str,
    token: str = Depends(require_auth)
):
    """
    Get the full subagent objects enabled for a profile.
    Returns the complete subagent data for each enabled agent ID.
    """
    api_user = get_api_user_from_request(request)

    # Check if API user is restricted to a specific profile
    if api_user and api_user.get("profile_id"):
        if api_user["profile_id"] != profile_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this profile"
            )

    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    # Get enabled agent IDs from profile config
    enabled_agent_ids = profile.get("config", {}).get("enabled_agents", [])

    # Fetch full subagent data for each enabled ID
    agents = []
    for agent_id in enabled_agent_ids:
        subagent = database.get_subagent(agent_id)
        if subagent:
            agents.append(SubagentResponse(
                id=subagent["id"],
                name=subagent["name"],
                description=subagent["description"],
                prompt=subagent["prompt"],
                tools=subagent.get("tools"),
                model=subagent.get("model"),
                is_builtin=subagent.get("is_builtin", False),
                created_at=subagent["created_at"],
                updated_at=subagent["updated_at"]
            ))

    return agents


# Request model for creating/updating a subagent via profile
class ProfileSubagentRequest(BaseModel):
    """Request to create or update a subagent via profile endpoint"""
    name: Optional[str] = None  # Required for create, optional for update
    description: str
    prompt: str
    tools: Optional[List[str]] = None
    model: Optional[str] = None


@router.post("/{profile_id}/agents", response_model=SubagentResponse, status_code=status.HTTP_201_CREATED)
async def create_profile_agent(
    profile_id: str,
    request: ProfileSubagentRequest,
    token: str = Depends(require_admin)
):
    """
    Create a new subagent and enable it for the profile.
    This creates a global subagent and adds it to the profile's enabled_agents.
    """
    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    if not request.name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Name is required for creating a new subagent"
        )

    # Generate an ID from the name
    subagent_id = request.name.lower().replace(" ", "-").replace("_", "-")

    # Check if subagent already exists
    existing = database.get_subagent(subagent_id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Subagent already exists: {subagent_id}"
        )

    # Create the global subagent
    subagent = database.create_subagent(
        subagent_id=subagent_id,
        name=request.name,
        description=request.description,
        prompt=request.prompt,
        tools=request.tools,
        model=request.model,
        is_builtin=False
    )

    # Add to profile's enabled agents
    config = profile.get("config", {})
    enabled_agents = config.get("enabled_agents", [])
    if subagent_id not in enabled_agents:
        enabled_agents.append(subagent_id)
        config["enabled_agents"] = enabled_agents
        database.update_profile(profile_id=profile_id, config=config, allow_builtin=True)

    return SubagentResponse(
        id=subagent["id"],
        name=subagent["name"],
        description=subagent["description"],
        prompt=subagent["prompt"],
        tools=subagent.get("tools"),
        model=subagent.get("model"),
        is_builtin=subagent.get("is_builtin", False),
        created_at=subagent["created_at"],
        updated_at=subagent["updated_at"]
    )


@router.put("/{profile_id}/agents/{agent_name}", response_model=SubagentResponse)
async def update_profile_agent(
    profile_id: str,
    agent_name: str,
    request: ProfileSubagentRequest,
    token: str = Depends(require_admin)
):
    """
    Update a subagent. The agent_name can be the subagent ID or name.
    """
    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    # Try to find subagent by ID first, then by name
    subagent = database.get_subagent(agent_name)
    if not subagent:
        # Try to find by name in the enabled agents
        enabled_agent_ids = profile.get("config", {}).get("enabled_agents", [])
        for agent_id in enabled_agent_ids:
            s = database.get_subagent(agent_id)
            if s and s.get("name") == agent_name:
                subagent = s
                break

    if not subagent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subagent not found: {agent_name}"
        )

    # Update the subagent
    updated = database.update_subagent(
        subagent_id=subagent["id"],
        name=request.name,
        description=request.description,
        prompt=request.prompt,
        tools=request.tools,
        model=request.model
    )

    return SubagentResponse(
        id=updated["id"],
        name=updated["name"],
        description=updated["description"],
        prompt=updated["prompt"],
        tools=updated.get("tools"),
        model=updated.get("model"),
        is_builtin=updated.get("is_builtin", False),
        created_at=updated["created_at"],
        updated_at=updated["updated_at"]
    )


@router.delete("/{profile_id}/agents/{agent_name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile_agent(
    profile_id: str,
    agent_name: str,
    token: str = Depends(require_admin)
):
    """
    Delete a subagent and remove it from the profile's enabled agents.
    The agent_name can be the subagent ID or name.
    """
    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    # Try to find subagent by ID first, then by name
    subagent = database.get_subagent(agent_name)
    if not subagent:
        # Try to find by name in the enabled agents
        enabled_agent_ids = profile.get("config", {}).get("enabled_agents", [])
        for agent_id in enabled_agent_ids:
            s = database.get_subagent(agent_id)
            if s and s.get("name") == agent_name:
                subagent = s
                break

    if not subagent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subagent not found: {agent_name}"
        )

    # Remove from profile's enabled agents
    config = profile.get("config", {})
    enabled_agents = config.get("enabled_agents", [])
    if subagent["id"] in enabled_agents:
        enabled_agents.remove(subagent["id"])
        config["enabled_agents"] = enabled_agents
        database.update_profile(profile_id=profile_id, config=config, allow_builtin=True)

    # Delete the global subagent
    database.delete_subagent(subagent["id"])


@router.post("", response_model=Profile, status_code=status.HTTP_201_CREATED)
async def create_profile(request: ProfileCreate, token: str = Depends(require_admin)):
    """Create a custom profile - Admin only"""
    # Check if ID already exists
    existing = database.get_profile(request.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Profile already exists: {request.id}"
        )

    profile = database.create_profile(
        profile_id=request.id,
        name=request.name,
        description=request.description,
        config=request.config.model_dump(exclude_none=True),
        is_builtin=False
    )

    return profile


@router.put("/{profile_id}", response_model=Profile)
async def update_profile(
    profile_id: str,
    request: ProfileUpdate,
    token: str = Depends(require_admin)
):
    """Update a profile - Admin only. All profiles (including defaults) are editable."""
    existing = database.get_profile(profile_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    config = None
    if request.config:
        config = request.config.model_dump(exclude_none=True)

    profile = database.update_profile(
        profile_id=profile_id,
        name=request.name,
        description=request.description,
        config=config,
        allow_builtin=True  # All profiles are editable
    )

    return profile


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(profile_id: str, token: str = Depends(require_admin)):
    """Delete a profile - Admin only. All profiles can be deleted."""
    existing = database.get_profile(profile_id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    database.delete_profile(profile_id)


# ============================================================================
# Profile Enabled Agents Management
# ============================================================================

@router.get("/{profile_id}/enabled-agents", response_model=List[str])
async def get_enabled_agents(
    request: Request,
    profile_id: str,
    token: str = Depends(require_auth)
):
    """Get the list of enabled subagent IDs for a profile"""
    api_user = get_api_user_from_request(request)

    # Check if API user is restricted to a specific profile
    if api_user and api_user.get("profile_id"):
        if api_user["profile_id"] != profile_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this profile"
            )

    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    return profile.get("config", {}).get("enabled_agents", [])


@router.put("/{profile_id}/enabled-agents", response_model=List[str])
async def update_enabled_agents(
    profile_id: str,
    request: EnabledAgentsRequest,
    token: str = Depends(require_admin)
):
    """Update the list of enabled subagent IDs for a profile - Admin only"""
    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    # Validate that all subagent IDs exist
    all_subagents = database.get_all_subagents()
    valid_ids = {s["id"] for s in all_subagents}
    invalid_ids = [sid for sid in request.enabled_agents if sid not in valid_ids]

    if invalid_ids:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid subagent IDs: {', '.join(invalid_ids)}"
        )

    # Update profile config
    config = profile.get("config", {})
    config["enabled_agents"] = request.enabled_agents

    database.update_profile(profile_id=profile_id, config=config, allow_builtin=True)

    return request.enabled_agents


@router.post("/{profile_id}/enabled-agents/{subagent_id}", status_code=status.HTTP_200_OK)
async def enable_subagent(
    profile_id: str,
    subagent_id: str,
    token: str = Depends(require_admin)
):
    """Enable a subagent for a profile - Admin only"""
    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    # Validate subagent exists
    subagent = database.get_subagent(subagent_id)
    if not subagent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subagent not found: {subagent_id}"
        )

    # Update profile config
    config = profile.get("config", {})
    enabled_agents = config.get("enabled_agents", [])

    if subagent_id not in enabled_agents:
        enabled_agents.append(subagent_id)
        config["enabled_agents"] = enabled_agents
        database.update_profile(profile_id=profile_id, config=config, allow_builtin=True)

    return {"enabled": True, "subagent_id": subagent_id}


@router.delete("/{profile_id}/enabled-agents/{subagent_id}", status_code=status.HTTP_200_OK)
async def disable_subagent(
    profile_id: str,
    subagent_id: str,
    token: str = Depends(require_admin)
):
    """Disable a subagent for a profile - Admin only"""
    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    # Update profile config
    config = profile.get("config", {})
    enabled_agents = config.get("enabled_agents", [])

    if subagent_id in enabled_agents:
        enabled_agents.remove(subagent_id)
        config["enabled_agents"] = enabled_agents
        database.update_profile(profile_id=profile_id, config=config, allow_builtin=True)

    return {"enabled": False, "subagent_id": subagent_id}


# ============================================================================
# Agent Export/Import Endpoints
# ============================================================================

@router.get("/{profile_id}/export", response_model=AgentExport)
async def export_profile(
    request: Request,
    profile_id: str,
    token: str = Depends(require_auth)
):
    """
    Export a profile/agent as JSON for sharing.

    Returns a downloadable JSON file containing the agent's configuration.
    Sensitive data like paths and secrets are sanitized.
    """
    api_user = get_api_user_from_request(request)

    # Check if API user is restricted to a specific profile
    if api_user and api_user.get("profile_id"):
        if api_user["profile_id"] != profile_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this profile"
            )

    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    config = profile.get("config", {})
    system_prompt = config.get("system_prompt", {}) or {}
    ai_tools = config.get("ai_tools")

    # Build export data - sanitize sensitive paths
    agent_data = AgentExportData(
        name=profile.get("name", profile_id),
        description=profile.get("description"),
        model=config.get("model", "sonnet"),
        permission_mode=config.get("permission_mode", "default"),
        system_prompt_type=system_prompt.get("type", "preset"),
        system_prompt_preset=system_prompt.get("preset", "claude_code"),
        system_prompt_content=system_prompt.get("content"),
        system_prompt_append=system_prompt.get("append"),
        system_prompt_inject_env=system_prompt.get("inject_env_details", False),
        allowed_tools=config.get("allowed_tools"),
        disallowed_tools=config.get("disallowed_tools"),
        enabled_agents=config.get("enabled_agents"),
        ai_tools=ai_tools,
        max_turns=config.get("max_turns"),
        max_buffer_size=config.get("max_buffer_size"),
        include_partial_messages=config.get("include_partial_messages", True),
        continue_conversation=config.get("continue_conversation", False),
        fork_session=config.get("fork_session", False),
        setting_sources=config.get("setting_sources"),
        # Note: We intentionally exclude sensitive data:
        # - cwd (working directory path)
        # - add_dirs (additional directory paths)
        # - user (user identifier)
        # - env (environment variables)
        # - extra_args (may contain sensitive data)
        # - enabled_plugins (plugin paths are system-specific)
    )

    export_data = AgentExport(
        version="1.0",
        type="ai-hub-agent",
        exported_at=datetime.utcnow().isoformat() + "Z",
        agent=agent_data
    )

    # Return with headers suggesting download
    filename = f"{profile_id}-agent.json"
    response = JSONResponse(
        content=export_data.model_dump(),
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        }
    )
    return response


@router.post("/import", response_model=Profile, status_code=status.HTTP_201_CREATED)
async def import_profile(
    import_request: AgentImportRequest,
    token: str = Depends(require_admin)
):
    """
    Import an agent from JSON data.

    Creates a new profile with the imported configuration.
    A new unique ID is generated to avoid conflicts.
    """
    # Validate type
    if import_request.type != "ai-hub-agent":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid export type: {import_request.type}. Expected 'ai-hub-agent'"
        )

    # Validate version
    supported_versions = ["1.0"]
    if import_request.version not in supported_versions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported export version: {import_request.version}. Supported: {', '.join(supported_versions)}"
        )

    agent_data = import_request.agent

    # Determine profile name
    profile_name = import_request.new_name or agent_data.name
    if not profile_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Agent name is required"
        )

    # Determine profile ID
    if import_request.new_id:
        profile_id = import_request.new_id
    else:
        # Generate ID from name
        profile_id = re.sub(r'[^a-z0-9-]', '-', profile_name.lower())
        profile_id = re.sub(r'-+', '-', profile_id).strip('-')
        if not profile_id:
            profile_id = "imported-agent"

    # Ensure unique ID by appending number if exists
    base_id = profile_id
    counter = 1
    while database.get_profile(profile_id):
        profile_id = f"{base_id}-{counter}"
        counter += 1
        if counter > 100:  # Safety limit
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Too many profiles with similar ID: {base_id}"
            )

    # Build config from agent data
    config = {
        "model": agent_data.model or "sonnet",
        "permission_mode": agent_data.permission_mode or "default",
        "include_partial_messages": agent_data.include_partial_messages if agent_data.include_partial_messages is not None else True,
        "continue_conversation": agent_data.continue_conversation or False,
        "fork_session": agent_data.fork_session or False,
    }

    # System prompt
    if agent_data.system_prompt_type == "custom" and agent_data.system_prompt_content:
        config["system_prompt"] = {
            "type": "custom",
            "content": agent_data.system_prompt_content,
            "inject_env_details": agent_data.system_prompt_inject_env or False
        }
    else:
        config["system_prompt"] = {
            "type": "preset",
            "preset": agent_data.system_prompt_preset or "claude_code"
        }
        if agent_data.system_prompt_append:
            config["system_prompt"]["append"] = agent_data.system_prompt_append

    # Optional fields
    if agent_data.allowed_tools:
        config["allowed_tools"] = agent_data.allowed_tools
    if agent_data.disallowed_tools:
        config["disallowed_tools"] = agent_data.disallowed_tools
    if agent_data.enabled_agents:
        # Note: Subagent IDs are preserved but may not exist in target system
        config["enabled_agents"] = agent_data.enabled_agents
    if agent_data.ai_tools:
        config["ai_tools"] = agent_data.ai_tools.model_dump()
    if agent_data.max_turns:
        config["max_turns"] = agent_data.max_turns
    if agent_data.max_buffer_size:
        config["max_buffer_size"] = agent_data.max_buffer_size
    if agent_data.setting_sources:
        config["setting_sources"] = agent_data.setting_sources

    # Create the profile
    profile = database.create_profile(
        profile_id=profile_id,
        name=profile_name,
        description=agent_data.description,
        config=config,
        is_builtin=False
    )

    return profile
