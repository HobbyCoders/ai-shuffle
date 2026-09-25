"""
Project agents API routes - manages file-based agents in project directories.

Agents are stored as .md files in <project>/.claude/agents/ with YAML frontmatter.
"""

from typing import List, Optional
from datetime import datetime

from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, Field

from app.api.auth import require_auth
from app.api.common import validate_project_access, get_project_path_from_dict
from app.core.project_agent_service import (
    get_project_agent_service,
    ProjectAgent,
    ProjectAgentDetail,
)

router = APIRouter(prefix="/api/v1/projects", tags=["Project Agents"])


# ============================================================================
# Request/Response Models
# ============================================================================

class ProjectAgentResponse(BaseModel):
    """Agent summary response"""
    name: str
    description: str
    model: Optional[str] = None
    tools: Optional[List[str]] = None
    file_path: str
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None


class ProjectAgentDetailResponse(ProjectAgentResponse):
    """Agent detail response including prompt"""
    prompt: str
    raw_content: str


class ProjectAgentCreate(BaseModel):
    """Request body for creating an agent"""
    name: str = Field(..., min_length=1, max_length=100, pattern=r'^[a-zA-Z][a-zA-Z0-9_-]*$')
    description: str = Field(..., min_length=1, max_length=500)
    prompt: str = Field(..., min_length=1)
    model: Optional[str] = Field(None, max_length=50)
    tools: Optional[List[str]] = None


class ProjectAgentUpdate(BaseModel):
    """Request body for updating an agent"""
    description: Optional[str] = Field(None, min_length=1, max_length=500)
    prompt: Optional[str] = Field(None, min_length=1)
    model: Optional[str] = Field(None, max_length=50)
    tools: Optional[List[str]] = None


class ProjectAgentRename(BaseModel):
    """Request body for renaming an agent"""
    new_name: str = Field(..., min_length=1, max_length=100, pattern=r'^[a-zA-Z][a-zA-Z0-9_-]*$')


# ============================================================================
# Helper Functions
# ============================================================================

def _agent_to_response(agent: ProjectAgent) -> ProjectAgentResponse:
    """Convert ProjectAgent to response model"""
    return ProjectAgentResponse(
        name=agent.name,
        description=agent.description,
        model=agent.model,
        tools=agent.tools,
        file_path=agent.file_path,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
    )


def _agent_detail_to_response(agent: ProjectAgentDetail) -> ProjectAgentDetailResponse:
    """Convert ProjectAgentDetail to response model"""
    return ProjectAgentDetailResponse(
        name=agent.name,
        description=agent.description,
        model=agent.model,
        tools=agent.tools,
        file_path=agent.file_path,
        created_at=agent.created_at,
        updated_at=agent.updated_at,
        prompt=agent.prompt,
        raw_content=agent.raw_content,
    )


# ============================================================================
# API Endpoints
# ============================================================================

@router.get("/{project_id}/agents", response_model=List[ProjectAgentResponse])
async def list_project_agents(
    project_id: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """
    List all agents in the project's .claude/agents/ directory.

    Returns agent metadata without full prompt content.
    """
    project = validate_project_access(request, project_id)
    project_path = get_project_path_from_dict(project)

    service = get_project_agent_service(project_path)
    agents = service.get_agents()

    return [_agent_to_response(a) for a in agents]


@router.get("/{project_id}/agents/{agent_name}", response_model=ProjectAgentDetailResponse)
async def get_project_agent(
    project_id: str,
    agent_name: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """
    Get detailed information about a specific agent including full prompt.
    """
    project = validate_project_access(request, project_id)
    project_path = get_project_path_from_dict(project)

    service = get_project_agent_service(project_path)
    agent = service.get_agent(agent_name)

    if not agent:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Agent not found: {agent_name}"
        )

    return _agent_detail_to_response(agent)


@router.post("/{project_id}/agents", response_model=ProjectAgentResponse, status_code=status.HTTP_201_CREATED)
async def create_project_agent(
    project_id: str,
    agent_data: ProjectAgentCreate,
    request: Request,
    token: str = Depends(require_auth)
):
    """
    Create a new agent in the project's .claude/agents/ directory.

    Creates a .md file with YAML frontmatter for metadata.
    """
    project = validate_project_access(request, project_id)
    project_path = get_project_path_from_dict(project)

    service = get_project_agent_service(project_path)

    try:
        agent = service.create_agent(
            name=agent_data.name,
            description=agent_data.description,
            prompt=agent_data.prompt,
            model=agent_data.model,
            tools=agent_data.tools,
        )
        return _agent_to_response(agent)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.put("/{project_id}/agents/{agent_name}", response_model=ProjectAgentResponse)
async def update_project_agent(
    project_id: str,
    agent_name: str,
    agent_data: ProjectAgentUpdate,
    request: Request,
    token: str = Depends(require_auth)
):
    """
    Update an existing agent.

    Only provided fields will be updated; others are preserved.
    Uses model_fields_set to distinguish between "not provided" and "set to null".
    """
    project = validate_project_access(request, project_id)
    project_path = get_project_path_from_dict(project)

    service = get_project_agent_service(project_path)

    # Only pass fields that were explicitly provided in the request
    fields = agent_data.model_fields_set
    try:
        agent = service.update_agent(
            name=agent_name,
            description=agent_data.description if 'description' in fields else ...,
            prompt=agent_data.prompt if 'prompt' in fields else ...,
            model=agent_data.model if 'model' in fields else ...,
            tools=agent_data.tools if 'tools' in fields else ...,
        )
        return _agent_to_response(agent)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )


@router.post("/{project_id}/agents/{agent_name}/rename", response_model=ProjectAgentResponse)
async def rename_project_agent(
    project_id: str,
    agent_name: str,
    rename_data: ProjectAgentRename,
    request: Request,
    token: str = Depends(require_auth)
):
    """
    Rename an agent (changes the .md filename).
    """
    project = validate_project_access(request, project_id)
    project_path = get_project_path_from_dict(project)

    service = get_project_agent_service(project_path)

    try:
        agent = service.rename_agent(agent_name, rename_data.new_name)
        return _agent_to_response(agent)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(e)
        )


@router.delete("/{project_id}/agents/{agent_name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project_agent(
    project_id: str,
    agent_name: str,
    request: Request,
    token: str = Depends(require_auth)
):
    """
    Delete an agent (removes the .md file).
    """
    project = validate_project_access(request, project_id)
    project_path = get_project_path_from_dict(project)

    service = get_project_agent_service(project_path)

    try:
        service.delete_agent(agent_name)
    except ValueError as e:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(e)
        )
