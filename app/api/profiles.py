"""
Profile management API routes
"""

from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException, Depends, status, Request
from pydantic import BaseModel, Field

from app.core.models import Profile, ProfileCreate, ProfileUpdate, SubagentDefinition
from app.db import database
from app.api.auth import require_auth, require_admin, get_api_user_from_request, is_admin_request

router = APIRouter(prefix="/api/v1/profiles", tags=["Profiles"])


# Subagent-specific request/response models
class SubagentResponse(BaseModel):
    """Subagent definition response"""
    name: str
    description: str
    prompt: str
    tools: Optional[List[str]] = None
    model: Optional[str] = None


class SubagentCreateRequest(BaseModel):
    """Request to create a new subagent"""
    name: str = Field(..., pattern=r'^[a-z0-9-]+$', min_length=1, max_length=50)
    description: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    tools: Optional[List[str]] = None
    model: Optional[str] = None


class SubagentUpdateRequest(BaseModel):
    """Request to update a subagent"""
    description: Optional[str] = None
    prompt: Optional[str] = None
    tools: Optional[List[str]] = None
    model: Optional[str] = None


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
# Subagent Management Endpoints
# ============================================================================

@router.get("/{profile_id}/agents", response_model=List[SubagentResponse])
async def list_subagents(
    request: Request,
    profile_id: str,
    token: str = Depends(require_auth)
):
    """List all subagents for a profile"""
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

    agents = profile.get("config", {}).get("agents", {})
    return [
        SubagentResponse(
            name=name,
            description=agent.get("description", ""),
            prompt=agent.get("prompt", ""),
            tools=agent.get("tools"),
            model=agent.get("model")
        )
        for name, agent in agents.items()
    ]


@router.get("/{profile_id}/agents/{agent_name}", response_model=SubagentResponse)
async def get_subagent(
    request: Request,
    profile_id: str,
    agent_name: str,
    token: str = Depends(require_auth)
):
    """Get a specific subagent by name"""
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

    agents = profile.get("config", {}).get("agents", {})
    if agent_name not in agents:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subagent not found: {agent_name}"
        )

    agent = agents[agent_name]
    return SubagentResponse(
        name=agent_name,
        description=agent.get("description", ""),
        prompt=agent.get("prompt", ""),
        tools=agent.get("tools"),
        model=agent.get("model")
    )


@router.post("/{profile_id}/agents", response_model=SubagentResponse, status_code=status.HTTP_201_CREATED)
async def create_subagent(
    profile_id: str,
    request: SubagentCreateRequest,
    token: str = Depends(require_admin)
):
    """Create a new subagent for a profile - Admin only"""
    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    config = profile.get("config", {})
    agents = config.get("agents", {})

    if request.name in agents:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Subagent already exists: {request.name}"
        )

    # Add new agent
    new_agent = {
        "description": request.description,
        "prompt": request.prompt,
    }
    if request.tools:
        new_agent["tools"] = request.tools
    if request.model:
        new_agent["model"] = request.model

    agents[request.name] = new_agent
    config["agents"] = agents

    # Update profile
    database.update_profile(profile_id=profile_id, config=config, allow_builtin=True)

    return SubagentResponse(
        name=request.name,
        description=request.description,
        prompt=request.prompt,
        tools=request.tools,
        model=request.model
    )


@router.put("/{profile_id}/agents/{agent_name}", response_model=SubagentResponse)
async def update_subagent(
    profile_id: str,
    agent_name: str,
    request: SubagentUpdateRequest,
    token: str = Depends(require_admin)
):
    """Update a subagent - Admin only"""
    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    config = profile.get("config", {})
    agents = config.get("agents", {})

    if agent_name not in agents:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subagent not found: {agent_name}"
        )

    # Update agent fields
    agent = agents[agent_name]
    if request.description is not None:
        agent["description"] = request.description
    if request.prompt is not None:
        agent["prompt"] = request.prompt
    if request.tools is not None:
        agent["tools"] = request.tools
    if request.model is not None:
        agent["model"] = request.model

    agents[agent_name] = agent
    config["agents"] = agents

    # Update profile
    database.update_profile(profile_id=profile_id, config=config, allow_builtin=True)

    return SubagentResponse(
        name=agent_name,
        description=agent.get("description", ""),
        prompt=agent.get("prompt", ""),
        tools=agent.get("tools"),
        model=agent.get("model")
    )


@router.delete("/{profile_id}/agents/{agent_name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_subagent(
    profile_id: str,
    agent_name: str,
    token: str = Depends(require_admin)
):
    """Delete a subagent - Admin only"""
    profile = database.get_profile(profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    config = profile.get("config", {})
    agents = config.get("agents", {})

    if agent_name not in agents:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Subagent not found: {agent_name}"
        )

    # Remove agent
    del agents[agent_name]
    config["agents"] = agents

    # Update profile
    database.update_profile(profile_id=profile_id, config=config, allow_builtin=True)
