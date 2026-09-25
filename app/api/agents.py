"""
Profile management API routes
"""

from typing import List, Optional
from datetime import datetime, timezone
import re
import logging

from fastapi import APIRouter, HTTPException, Depends, status, Request, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.core.models import (
    Profile, ProfileCreate, ProfileUpdate,
    AgentExport, AgentExportData, AgentImportRequest
)
from app.db import database
from app.db.async_database import run_sync
from app.api.auth import require_auth, require_admin, get_api_user_from_request
from app.core.plugin_service import get_plugin_service
from app.core.project_utils import get_project_working_dir_as_path, get_worktree_working_dir_as_path
from app.core.project_agent_service import get_project_agent_service, get_user_agent_service
from app.core.profiles import get_builtin_profile_defaults

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/agents", tags=["Agents"])


# Response model for subagent (matches frontend expectations)
class SubagentResponse(BaseModel):
    """Subagent response model for profile agents endpoint"""
    id: str
    name: str
    description: str
    prompt: str
    tools: Optional[List[str]] = None
    model: Optional[str] = None
    skills: Optional[List[str]] = None
    memory: Optional[str] = None
    mcp_servers: Optional[list] = None
    is_builtin: bool = False
    created_at: datetime
    updated_at: datetime


# Request model for updating enabled agents
class EnabledAgentsRequest(BaseModel):
    """Request to update enabled agents for a profile"""
    enabled_agents: List[str] = Field(..., description="List of subagent IDs to enable")


# Unified subagent item for autocomplete
class UnifiedSubagentItem(BaseModel):
    """Unified subagent item for autocomplete - aggregates from all sources"""
    id: str
    name: str
    description: str
    prompt: str
    tools: Optional[List[str]] = None
    model: Optional[str] = None
    source: str = Field(..., description="Source: 'profile', 'plugin', or 'project'")
    source_name: Optional[str] = Field(None, description="Name of the source (e.g., plugin name)")


def _check_profile_modified(profile: dict) -> None:
    """Set is_modified on a built-in agent by comparing to its defaults.

    Built-in subagents (general-purpose, Explore, Plan, frontend-designer)
    compare against their code definitions in BUILTIN_SUBAGENTS; the
    profile-defaults seam is kept for any future built-in profile.
    """
    if not profile.get("is_builtin"):
        return
    defaults = get_builtin_profile_defaults(profile["id"])
    if defaults:
        profile["is_modified"] = (
            profile.get("name") != defaults["name"]
            or profile.get("description") != defaults["description"]
            or profile.get("config") != defaults["config"]
        )
        return
    modified = database.builtin_subagent_is_modified(profile)
    if modified is not None:
        profile["is_modified"] = modified


@router.get("/templates")
async def list_agent_templates(token: str = Depends(require_auth)):
    """System-prompt templates for agent creation (ruling A12).

    New agents start with an EMPTY prompt; the built-in subagent prompts are
    offered as selectable starting points — templates are the ceiling, nothing
    is ever injected implicitly. Served from the in-repo code definitions
    (BUILTIN_SUBAGENTS), not the database.
    """
    from app.core.builtin_subagents import BUILTIN_SUBAGENTS

    return [
        {
            "id": d["id"],
            "name": d["name"],
            "description": d.get("description") or "",
            "prompt": d.get("prompt") or "",
            "tools": d.get("tools"),
        }
        for d in BUILTIN_SUBAGENTS
    ]


@router.get("", response_model=List[Profile])
async def list_profiles(request: Request, token: str = Depends(require_auth)):
    """List all agent profiles. API users only see their assigned profiles."""
    api_user = get_api_user_from_request(request)

    def _load() -> list:
        """The session-count aggregate plus every profile read in ONE hop.

        For a restricted API user this was 1 + N blocking queries on the loop;
        ``_check_profile_modified`` is pure (seed comparison), so the whole
        branch collapses into one closure with the query order untouched.
        """
        # Get session counts for all profiles in one query
        session_counts = database.get_all_profile_session_counts()

        profile_ids = api_user.get("profile_ids") if api_user else None
        if profile_ids:
            # API user with assigned profiles - only return those profiles
            allowed_profiles = []
            for pid in profile_ids:
                profile = database.get_profile(pid)
                if profile:
                    profile["session_count"] = session_counts.get(pid, 0)
                    _check_profile_modified(profile)
                    allowed_profiles.append(profile)
            return allowed_profiles

        # Admin or unrestricted API user - return all profiles
        profiles = database.get_all_profiles()
        for profile in profiles:
            profile["session_count"] = session_counts.get(profile["id"], 0)
            _check_profile_modified(profile)
        return profiles

    return await run_sync(_load)


@router.get("/{profile_id}", response_model=Profile)
async def get_profile(request: Request, profile_id: str, token: str = Depends(require_auth)):
    """Get a specific profile. API users can only access their assigned profiles."""
    api_user = get_api_user_from_request(request)

    # Check if API user is restricted to specific profiles
    profile_ids = api_user.get("profile_ids") if api_user else None
    if profile_ids:
        if profile_id not in profile_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this profile"
            )

    def _load() -> dict:
        """Profile row + its session count in ONE hop.

        The 404 raised here propagates through ``to_thread`` unchanged.
        """
        profile = database.get_profile(profile_id)
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Profile not found: {profile_id}"
            )
        # Add session count and modification status
        profile["session_count"] = database.get_profile_session_count(profile_id)
        _check_profile_modified(profile)
        return profile

    return await run_sync(_load)


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

    def _load() -> list[SubagentResponse]:
        """Profile row + one read per enabled subagent, in ONE hop.

        This was 1 + N blocking queries; N is the profile's enabled-agent count.
        """
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
                    skills=subagent.get("skills"),
                    memory=subagent.get("memory"),
                    mcp_servers=subagent.get("mcp_servers"),
                    is_builtin=subagent.get("is_builtin", False),
                    created_at=subagent["created_at"],
                    updated_at=subagent["updated_at"]
                ))

        return agents

    return await run_sync(_load)


@router.get("/{profile_id}/subagents/all", response_model=List[UnifiedSubagentItem])
async def get_all_available_subagents(
    request: Request,
    profile_id: str,
    project_id: Optional[str] = Query(None, description="Project ID to include project agents"),
    worktree_id: Optional[str] = Query(None, description="Worktree ID (overrides project path when in worktree mode)"),
    search: str = Query("", description="Search filter for name/description"),
    limit: int = Query(30, ge=1, le=50, description="Maximum number of results"),
    token: str = Depends(require_auth)
):
    """
    Get all available subagents for autocomplete from multiple sources:
    1. Profile-enabled subagents (from database)
    2. Project agents (if project_id provided)
    3. User-global agents (~/.claude/agents/, auto-loaded by the SDK)
    4. Plugin file-based agents (from enabled plugins)

    Returns a unified list with source information for each agent.
    Deduplication priority: profile > project > user > plugin (plugin agents
    are namespaced, so they never collide by name).
    """
    api_user = get_api_user_from_request(request)

    # Check if API user is restricted to specific profiles
    profile_ids = api_user.get("profile_ids") if api_user else None
    if profile_ids:
        if profile_id not in profile_ids:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this profile"
            )

    def _load() -> list[UnifiedSubagentItem]:
        """Every source of the autocomplete list, in ONE hop.

        1 + N subagent reads plus the project/worktree rows were blocking DB
        calls; the project, user and plugin agent services on top of them are
        synchronous *disk* scans that blocked the loop just as hard. The whole
        body is straight-line synchronous work with no awaits, so it collapses
        into a single closure and the dedup/priority order is untouched.
        """
        profile = database.get_profile(profile_id)
        if not profile:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Profile not found: {profile_id}"
            )

        # Track seen IDs for deduplication (profile > project > plugin priority)
        seen_ids: set = set()
        result: List[UnifiedSubagentItem] = []
        search_lower = search.lower() if search else ""

        # 1. Profile-enabled subagents (highest priority)
        enabled_agent_ids = profile.get("config", {}).get("enabled_agents", [])
        for agent_id in enabled_agent_ids:
            subagent = database.get_subagent(agent_id)
            if subagent:
                # Apply search filter
                if search_lower:
                    name_match = search_lower in subagent["name"].lower()
                    desc_match = search_lower in subagent["description"].lower()
                    if not (name_match or desc_match):
                        continue

                seen_ids.add(agent_id)
                result.append(UnifiedSubagentItem(
                    id=subagent["id"],
                    name=subagent["name"],
                    description=subagent["description"],
                    prompt=subagent["prompt"],
                    tools=subagent.get("tools"),
                    model=subagent.get("model"),
                    source="profile",
                    source_name=profile.get("name", profile_id)
                ))

        # 2. Project agents (if project_id provided; worktree overrides project path)
        if project_id:
            project = database.get_project(project_id)
            if project:
                if worktree_id:
                    worktree = database.get_worktree(worktree_id)
                    project_path = get_worktree_working_dir_as_path(worktree) if worktree else get_project_working_dir_as_path(project)
                else:
                    project_path = get_project_working_dir_as_path(project)
                try:
                    project_agent_service = get_project_agent_service(project_path)
                    for agent in project_agent_service.get_agents():
                        agent_id = f"project:{agent.name}"

                        # Skip if already seen
                        if agent_id in seen_ids:
                            continue

                        # Apply search filter
                        if search_lower:
                            name_match = search_lower in agent.name.lower()
                            desc_match = search_lower in agent.description.lower()
                            if not (name_match or desc_match):
                                continue

                        seen_ids.add(agent_id)

                        # Need to get full agent details for prompt
                        agent_detail = project_agent_service.get_agent(agent.name)
                        result.append(UnifiedSubagentItem(
                            id=agent_id,
                            name=agent.name,
                            description=agent.description,
                            prompt=agent_detail.prompt if agent_detail else "",
                            tools=agent.tools,
                            model=agent.model,
                            source="project",
                            source_name=project.get("name", project_id)
                        ))
                except Exception as e:
                    # Log but don't fail if project agents can't be loaded
                    logging.warning(f"Failed to load project agents for {project_id}: {e}")

        # 3. User-global agents (~/.fluid/agents/) - loaded by Fluid at
        #    runtime via setting_sources "user", surfaced here for discovery.
        #    Claude Code precedence is project > user, so a project (or profile)
        #    agent of the same name shadows the user one: dedupe by name against
        #    everything added so far.
        seen_names = {item.name.lower() for item in result}
        try:
            user_agent_service = get_user_agent_service()
            for agent in user_agent_service.get_agents():
                agent_id = f"user:{agent.name}"

                # Skip if shadowed by a higher-priority agent of the same name,
                # or already seen by id.
                if agent.name.lower() in seen_names or agent_id in seen_ids:
                    continue

                # Apply search filter
                if search_lower:
                    name_match = search_lower in agent.name.lower()
                    desc_match = search_lower in agent.description.lower()
                    if not (name_match or desc_match):
                        continue

                seen_ids.add(agent_id)
                seen_names.add(agent.name.lower())

                # Need to get full agent details for prompt
                agent_detail = user_agent_service.get_agent(agent.name)
                result.append(UnifiedSubagentItem(
                    id=agent_id,
                    name=agent.name,
                    description=agent.description,
                    prompt=agent_detail.prompt if agent_detail else "",
                    tools=agent.tools,
                    model=agent.model,
                    source="user",
                    source_name="User (~/.claude)"
                ))
        except Exception as e:
            # Log but don't fail if user-global agents can't be loaded
            logging.warning(f"Failed to load user-global agents: {e}")

        # 4. Plugin file-based agents (lowest priority)
        try:
            plugin_service = get_plugin_service()
            for agent in plugin_service.get_file_based_agents():
                agent_id = agent.get("id", "")

                # Skip if already seen
                if agent_id in seen_ids:
                    continue

                agent_name = agent.get("name", "")
                agent_desc = agent.get("description", "")

                # Apply search filter
                if search_lower:
                    name_match = search_lower in agent_name.lower()
                    desc_match = search_lower in agent_desc.lower()
                    if not (name_match or desc_match):
                        continue

                seen_ids.add(agent_id)
                result.append(UnifiedSubagentItem(
                    id=agent_id,
                    name=agent_name,
                    description=agent_desc,
                    prompt=agent.get("prompt", ""),
                    tools=agent.get("tools"),
                    model=agent.get("model"),
                    source="plugin",
                    source_name=agent.get("source_plugin")
                ))
        except Exception as e:
            # Log but don't fail if plugin agents can't be loaded
            logging.warning(f"Failed to load plugin agents: {e}")

        # Apply limit
        return result[:limit]

    return await run_sync(_load)


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
    def _load() -> SubagentResponse:
        """Existence check, insert and the profile update in ONE hop.

        Read-decide-write: the ``get_subagent`` probe decides whether the insert
        happens (409 otherwise), and the enabled-agents list is read from the
        profile row fetched above before being written back. Splitting these
        across awaits would let a concurrent create win the id between the probe
        and the insert, or clobber a concurrent enabled-agents edit.
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
            skills=subagent.get("skills"),
            memory=subagent.get("memory"),
            mcp_servers=subagent.get("mcp_servers"),
            is_builtin=subagent.get("is_builtin", False),
            created_at=subagent["created_at"],
            updated_at=subagent["updated_at"]
        )

    return await run_sync(_load)


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
    def _load() -> SubagentResponse:
        """Profile read, id/name resolution and the update in ONE hop.

        Read-decide-write: which subagent row gets written is decided by the
        lookup-by-id-then-by-name walk just above the write.
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
            skills=updated.get("skills"),
            memory=updated.get("memory"),
            mcp_servers=updated.get("mcp_servers"),
            is_builtin=updated.get("is_builtin", False),
            created_at=updated["created_at"],
            updated_at=updated["updated_at"]
        )

    return await run_sync(_load)


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
    def _load() -> None:
        """Lookup, automation guard, profile update and delete in ONE hop.

        Read-decide-write, twice over: the automation guard's 409 and the
        enabled-agents rewrite both depend on rows read in this same closure. An
        await between the guard and the delete would let an automation start
        using the agent after it had been cleared to delete.
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

        blocking = database.get_automations_for_profile(subagent["id"])
        if blocking:
            names = ", ".join(a["name"] for a in blocking)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Agent is used by {len(blocking)} automation(s): {names} — delete or reassign them first"
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

    return await run_sync(_load)


@router.post("", response_model=Profile, status_code=status.HTTP_201_CREATED)
async def create_profile(request: ProfileCreate, token: str = Depends(require_admin)):
    """Create a custom profile - Admin only"""
    def _load() -> dict:
        """The id-uniqueness probe loop and the insert in ONE hop.

        Read-decide-write: the loop probes ids until one is free and then
        inserts under it. Awaiting between the last probe and the insert would
        widen that gap into a real race for the id.
        """
        # Derive ID from name if not provided
        if request.id:
            profile_id = request.id
        else:
            profile_id = re.sub(r'[^a-z0-9-]', '-', request.name.lower())
            profile_id = re.sub(r'-+', '-', profile_id).strip('-')

        # Ensure uniqueness. Bounded so a store that keeps reporting a conflict (a
        # bug, a stale read, or absurd collision counts) can never spin forever and
        # exhaust memory — an unbounded `while` calling out to the DB is a foot-gun.
        base_id = profile_id
        counter = 1
        max_attempts = 10000
        while database.get_profile(profile_id):
            if counter > max_attempts:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=f"Could not allocate a unique profile id for: {base_id}",
                )
            profile_id = f"{base_id}-{counter}"
            counter += 1

        profile = database.create_profile(
            profile_id=profile_id,
            name=request.name,
            description=request.description,
            config=request.config.model_dump(exclude_none=True),
            is_builtin=False
        )

        return profile

    return await run_sync(_load)


@router.put("/{profile_id}", response_model=Profile)
async def update_profile(
    profile_id: str,
    request: ProfileUpdate,
    token: str = Depends(require_admin)
):
    """Update a profile - Admin only. All profiles (including defaults) are editable."""
    def _load() -> dict:
        """Existence check + update in ONE hop (the check gates the write).
        """
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

    return await run_sync(_load)


@router.delete("/{profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_profile(profile_id: str, token: str = Depends(require_admin)):
    """Delete a profile - Admin only. Built-in profiles cannot be deleted."""
    def _load() -> None:
        """Existence check, built-in guard, automation guard and delete in ONE hop.

        Read-decide-write: three reads gate the delete, so they stay atomic.
        """
        existing = database.get_profile(profile_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Profile not found: {profile_id}"
            )

        if existing.get("is_builtin"):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Built-in profiles cannot be deleted"
            )

        blocking = database.get_automations_for_profile(profile_id)
        if blocking:
            names = ", ".join(a["name"] for a in blocking)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Agent is used by {len(blocking)} automation(s): {names} — delete or reassign them first"
            )

        database.delete_profile(profile_id)

    return await run_sync(_load)


@router.post("/{profile_id}/restore", response_model=Profile)
async def restore_profile_defaults(
    profile_id: str,
    token: str = Depends(require_admin)
):
    """Restore a built-in profile to its original default settings.

    Only works for built-in profiles that have known seed defaults.
    Custom profiles cannot be restored (they have no defaults to restore to).
    """
    def _load() -> dict:
        """Existence/built-in checks and the restoring update in ONE hop.

        ``get_builtin_profile_defaults`` is pure (in-repo seed data).
        """
        existing = database.get_profile(profile_id)
        if not existing:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Profile not found: {profile_id}"
            )

        if not existing.get("is_builtin"):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Only built-in profiles can be restored to defaults"
            )

        defaults = get_builtin_profile_defaults(profile_id)
        if defaults:
            profile = database.update_profile(
                profile_id=profile_id,
                name=defaults["name"],
                description=defaults["description"],
                config=defaults["config"],
                allow_builtin=True
            )
        elif database.revert_subagent_to_defaults(profile_id):
            # Built-in subagent: restore name/description/prompt/tools/model
            # from BUILTIN_SUBAGENTS, keeping user-owned skills/memory/MCP.
            profile = database.get_profile(profile_id)
        else:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"No known defaults for built-in profile: {profile_id}"
            )

        profile["session_count"] = database.get_profile_session_count(profile_id)
        _check_profile_modified(profile)
        return profile

    return await run_sync(_load)


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

    # One isolated read — one thread hop, no closure needed.
    profile = await run_sync(database.get_profile, profile_id)
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
    def _load() -> list[str]:
        """Profile read, subagent-id validation and the write in ONE hop.

        Read-decide-write: the validation reads every subagent id and its
        verdict gates the write, and the config being written comes from the
        profile row read at the top of the same closure.
        """
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

    return await run_sync(_load)


@router.post("/{profile_id}/enabled-agents/{subagent_id}", status_code=status.HTTP_200_OK)
async def enable_subagent(
    profile_id: str,
    subagent_id: str,
    token: str = Depends(require_admin)
):
    """Enable a subagent for a profile - Admin only"""
    def _load() -> dict:
        """Profile read, subagent existence check and the write in ONE hop.

        Read-decide-write: the enabled-agents list is read, appended to and
        written back — an await inside that would lose a concurrent edit.
        """
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

    return await run_sync(_load)


@router.delete("/{profile_id}/enabled-agents/{subagent_id}", status_code=status.HTTP_200_OK)
async def disable_subagent(
    profile_id: str,
    subagent_id: str,
    token: str = Depends(require_admin)
):
    """Disable a subagent for a profile - Admin only"""
    def _load() -> dict:
        """Profile read plus the read-modify-write of enabled_agents in ONE hop.
        """
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

    return await run_sync(_load)


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

    # One isolated read — everything below is pure formatting of the row.
    profile = await run_sync(database.get_profile, profile_id)
    if not profile:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Profile not found: {profile_id}"
        )

    config = profile.get("config", {})
    system_prompt = config.get("system_prompt", {}) or {}
    ai_tools = config.get("ai_tools")

    # Only the custom prompt content survives export (ruling A12) — a legacy
    # preset-shaped config exports its append text as the custom content.
    if system_prompt.get("type") == "custom":
        prompt_content = system_prompt.get("content")
    else:
        prompt_content = system_prompt.get("append")

    # Build export data - fluid-honored fields only, sanitize sensitive paths
    agent_data = AgentExportData(
        name=profile.get("name", profile_id),
        description=profile.get("description"),
        # Only present on agents that still carry a legacy binding, or on the
        # built-in Default AI agent (whose model drives internal generation) —
        # an ordinary agent no longer pins a model (v79).
        model=config.get("model"),
        provider_models=config.get("provider_models"),
        permission_mode=config.get("permission_mode"),
        effort=config.get("effort"),
        max_turns=config.get("max_turns"),
        system_prompt_content=prompt_content or None,
        allowed_tools=config.get("allowed_tools"),
        disallowed_tools=config.get("disallowed_tools"),
        tool_selection_mode=config.get("tool_selection_mode"),
        enabled_agents=config.get("enabled_agents"),
        agent_invoke_mode=config.get("agent_invoke_mode"),
        ai_tools=ai_tools,
        memory=config.get("memory"),
        include_partial_messages=config.get("include_partial_messages", True),
        app_config_tools=config.get("app_config_tools", False),
        env=config.get("env"),  # Environment variables are exported
        # Setting sources (fluid scope filter) round-trip: an explicit list
        # (including []) is portable intent, not a system-specific path.
        setting_sources=config.get("setting_sources"),
        # Note: We intentionally exclude some data:
        # - cwd (working directory path - system-specific)
        # - add_dirs (additional directory paths - system-specific)
        # - user (user identifier)
        # - extra_args (may contain sensitive data)
        # - enabled_plugins (plugin paths are system-specific)
        # SDK-era fields the fluid harness ignores (thinking_mode, preset
        # prompts, max_buffer_size, …) are not exported.
    )

    export_data = AgentExport(
        version="1.0",
        type="ai-shuffle-agent",
        exported_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
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
    if import_request.type != "ai-shuffle-agent":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid export type: {import_request.type}. Expected 'ai-shuffle-agent'"
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

    def _load() -> dict:
        """Id allocation, subagent filtering and the insert in ONE hop.

        Read-decide-write: the ``while database.get_profile(...)`` loop picks a
        free id that ``create_profile`` then claims, and the enabled-agents
        filter reads every subagent id before the config is written. The pure
        request validation above stays on the loop.
        """
        nonlocal profile_id

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

        # Build config from agent data — fluid-honored fields only. Legacy SDK-era
        # fields in old export files (thinking_mode, max_buffer_size,
        # enable_file_checkpointing, continue_conversation, fork_session) are
        # accepted but ignored; setting_sources is honored again (fluid scope
        # filter).
        config = {
            "include_partial_messages": agent_data.include_partial_messages if agent_data.include_partial_messages is not None else True,
        }
        # A model / permission mode is written only when the file actually carries
        # one. Agents stopped pinning either in v79, so importing an older export
        # must not resurrect a binding the user can no longer see or edit; sessions
        # created from this agent pick their own.
        if agent_data.model:
            config["model"] = agent_data.model
        if agent_data.permission_mode:
            config["permission_mode"] = agent_data.permission_mode
        if agent_data.provider_models:
            config["provider_models"] = agent_data.provider_models
        # Harness-honored reasoning depth / per-turn cap (also present in pre-A12
        # legacy files, where they round-trip the same way).
        if agent_data.effort in ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"):
            config["effort"] = agent_data.effort
        if isinstance(agent_data.max_turns, int) and agent_data.max_turns > 0:
            config["max_turns"] = agent_data.max_turns
        # Setting sources: an explicit list is authoritative, [] included
        # ("filesystem-isolated"). Unknown names are filtered; a NON-empty legacy
        # list that filters to nothing (old SDK vocab like "env"/"file") is dropped
        # entirely rather than silently isolating the imported agent.
        if isinstance(agent_data.setting_sources, list):
            valid = [s for s in agent_data.setting_sources if s in ("user", "project", "local")]
            if valid or not agent_data.setting_sources:
                config["setting_sources"] = valid

        # System prompt — custom content only (ruling A12). Legacy preset-shaped
        # exports map their append text onto the custom content.
        prompt_content = agent_data.system_prompt_content or agent_data.system_prompt_append
        if prompt_content:
            config["system_prompt"] = {
                "type": "custom",
                "content": prompt_content,
                "inject_env_details": agent_data.system_prompt_inject_env or False
            }

        # Optional fields
        if agent_data.allowed_tools:
            config["allowed_tools"] = agent_data.allowed_tools
        if agent_data.disallowed_tools:
            config["disallowed_tools"] = agent_data.disallowed_tools
        # Ruling T2: an explicit mode in the file is authoritative. Absent, the
        # runtime infers it from the lists exactly as the old editor did, so a
        # pre-T2 export still imports with the intent its author saw.
        from app.core.tool_access import TOOL_SELECTION_MODES
        if agent_data.tool_selection_mode in TOOL_SELECTION_MODES:
            config["tool_selection_mode"] = agent_data.tool_selection_mode
        # Invoke grants (A10). An explicit mode in the file is authoritative: under
        # "custom" the allowlist is exact, so it is written even when it filters to
        # empty ("may invoke no agents"). Files with no mode keep legacy behavior.
        invoke_mode = agent_data.agent_invoke_mode if agent_data.agent_invoke_mode in ("all", "custom") else None
        if invoke_mode:
            config["agent_invoke_mode"] = invoke_mode
        if agent_data.enabled_agents or invoke_mode == "custom":
            # Filter out subagent IDs that don't exist in the current environment
            # to prevent ghost agents from inflating the count
            incoming_agents = agent_data.enabled_agents or []
            all_subagents = database.get_all_subagents()
            valid_agent_ids = {s["id"] for s in all_subagents}
            valid_enabled = [aid for aid in incoming_agents if aid in valid_agent_ids]
            skipped = [aid for aid in incoming_agents if aid not in valid_agent_ids]
            if skipped:
                logger.warning(
                    "Import: skipped %d subagent(s) not found in environment: %s",
                    len(skipped), ", ".join(skipped)
                )
            if valid_enabled or invoke_mode == "custom":
                config["enabled_agents"] = valid_enabled
        if agent_data.ai_tools:
            config["ai_tools"] = agent_data.ai_tools.model_dump()
        if agent_data.memory:
            config["memory"] = agent_data.memory
        if agent_data.app_config_tools:
            config["app_config_tools"] = agent_data.app_config_tools
        if agent_data.env:
            config["env"] = agent_data.env

        # Create the profile
        profile = database.create_profile(
            profile_id=profile_id,
            name=profile_name,
            description=agent_data.description,
            config=config,
            is_builtin=False
        )

        return profile

    return await run_sync(_load)
