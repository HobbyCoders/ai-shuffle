"""
GitHub integration API routes

Provides endpoints for managing GitHub pull requests and workflow runs
through the gh CLI.
"""

import logging
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, Field

from app.core.github_service import github_service, PullRequest, WorkflowRun
from app.core.config import settings
from app.db import database
from app.api.auth import require_auth, get_api_user_from_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/projects/{project_id}/github", tags=["GitHub"])


# =============================================================================
# Pydantic Models for API
# =============================================================================

class PullRequestResponse(BaseModel):
    """Pull request response model"""
    number: int
    title: str
    body: str
    state: str
    head_branch: str
    base_branch: str
    author: str
    url: str
    created_at: str
    updated_at: str
    mergeable: Optional[bool] = None


class WorkflowRunResponse(BaseModel):
    """Workflow run response model"""
    id: int
    name: str
    status: str
    conclusion: Optional[str]
    branch: str
    event: str
    url: str
    created_at: str


class CreatePullRequest(BaseModel):
    """Request body for creating a pull request"""
    title: str = Field(..., min_length=1, max_length=256)
    body: str = Field(default="")
    head: str = Field(..., min_length=1, description="Head branch name")
    base: str = Field(..., min_length=1, description="Base branch name")


class MergePullRequest(BaseModel):
    """Request body for merging a pull request"""
    method: str = Field(default="merge", pattern="^(merge|squash|rebase)$")


class GitHubStatusResponse(BaseModel):
    """GitHub status response"""
    authenticated: bool
    repo: Optional[str] = None
    repo_info: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


# =============================================================================
# Helper Functions
# =============================================================================

def check_project_access(request: Request, project_id: str) -> None:
    """Check if the user has access to the project. Raises HTTPException if not."""
    api_user = get_api_user_from_request(request)
    if api_user and api_user.get("project_id"):
        if api_user["project_id"] != project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this project"
            )


def get_project_path(project_id: str) -> str:
    """Get the filesystem path for a project."""
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )

    project_path = settings.workspace_dir / project["path"]
    if not project_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project directory not found: {project_id}"
        )

    return str(project_path)


def get_repo_for_project(project_id: str) -> str:
    """Get the GitHub repository for a project from git remote."""
    project_path = get_project_path(project_id)
    repo = github_service.get_repo_from_remote(project_path)

    if not repo:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not determine GitHub repository. Ensure project has a GitHub remote configured."
        )

    return repo


def check_github_auth() -> None:
    """Check if GitHub CLI is authenticated. Raises HTTPException if not."""
    if not github_service.is_authenticated():
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="GitHub CLI is not authenticated. Please configure GitHub authentication."
        )


def pr_to_response(pr: PullRequest) -> PullRequestResponse:
    """Convert PullRequest dataclass to response model."""
    return PullRequestResponse(
        number=pr.number,
        title=pr.title,
        body=pr.body,
        state=pr.state,
        head_branch=pr.head_branch,
        base_branch=pr.base_branch,
        author=pr.author,
        url=pr.url,
        created_at=pr.created_at,
        updated_at=pr.updated_at,
        mergeable=pr.mergeable
    )


def run_to_response(run: WorkflowRun) -> WorkflowRunResponse:
    """Convert WorkflowRun dataclass to response model."""
    return WorkflowRunResponse(
        id=run.id,
        name=run.name,
        status=run.status,
        conclusion=run.conclusion,
        branch=run.branch,
        event=run.event,
        url=run.url,
        created_at=run.created_at
    )


# =============================================================================
# Status Endpoint
# =============================================================================

@router.get("/status", response_model=GitHubStatusResponse)
async def get_github_status(
    request: Request,
    project_id: str,
    token: str = Depends(require_auth)
):
    """
    Check GitHub authentication status and get repository info for the project.

    Returns authentication status and repository information if available.
    """
    check_project_access(request, project_id)

    response = GitHubStatusResponse(
        authenticated=github_service.is_authenticated()
    )

    if not response.authenticated:
        response.error = "GitHub CLI is not authenticated"
        return response

    try:
        project_path = get_project_path(project_id)
        repo = github_service.get_repo_from_remote(project_path)

        if repo:
            response.repo = repo
            response.repo_info = github_service.get_repo_info(repo)
        else:
            response.error = "No GitHub remote found in project"

    except HTTPException as e:
        response.error = e.detail
    except Exception as e:
        logger.error(f"Error getting GitHub status: {e}")
        response.error = str(e)

    return response


# =============================================================================
# Pull Request Endpoints
# =============================================================================

@router.get("/pulls", response_model=List[PullRequestResponse])
async def list_pull_requests(
    request: Request,
    project_id: str,
    state: str = "open",
    limit: int = 30,
    token: str = Depends(require_auth)
):
    """
    List pull requests for the project's GitHub repository.

    Args:
        state: Filter by state - 'open', 'closed', 'merged', 'all'
        limit: Maximum number of PRs to return (default 30, max 100)
    """
    check_project_access(request, project_id)
    check_github_auth()

    if limit > 100:
        limit = 100

    repo = get_repo_for_project(project_id)
    pulls = github_service.list_pulls(repo, state=state, limit=limit)

    return [pr_to_response(pr) for pr in pulls]


@router.get("/pulls/{number}", response_model=PullRequestResponse)
async def get_pull_request(
    request: Request,
    project_id: str,
    number: int,
    token: str = Depends(require_auth)
):
    """
    Get details of a specific pull request.
    """
    check_project_access(request, project_id)
    check_github_auth()

    repo = get_repo_for_project(project_id)
    pr = github_service.get_pull(repo, number)

    if not pr:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Pull request #{number} not found"
        )

    return pr_to_response(pr)


@router.post("/pulls", response_model=PullRequestResponse, status_code=status.HTTP_201_CREATED)
async def create_pull_request(
    request: Request,
    project_id: str,
    body: CreatePullRequest,
    token: str = Depends(require_auth)
):
    """
    Create a new pull request.

    The head branch must be pushed to GitHub before creating the PR.
    """
    check_project_access(request, project_id)
    check_github_auth()

    repo = get_repo_for_project(project_id)
    pr = github_service.create_pull(
        repo=repo,
        title=body.title,
        body=body.body,
        head=body.head,
        base=body.base
    )

    if not pr:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to create pull request. Ensure the head branch exists on GitHub."
        )

    return pr_to_response(pr)


@router.post("/pulls/{number}/merge")
async def merge_pull_request(
    request: Request,
    project_id: str,
    number: int,
    body: Optional[MergePullRequest] = None,
    token: str = Depends(require_auth)
):
    """
    Merge a pull request.

    Methods:
    - merge: Create a merge commit
    - squash: Squash and merge
    - rebase: Rebase and merge
    """
    check_project_access(request, project_id)
    check_github_auth()

    method = body.method if body else "merge"

    repo = get_repo_for_project(project_id)
    success = github_service.merge_pull(repo, number, method=method)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to merge pull request #{number}. It may have conflicts or require review."
        )

    return {"status": "ok", "message": f"Pull request #{number} merged successfully"}


@router.delete("/pulls/{number}")
async def close_pull_request(
    request: Request,
    project_id: str,
    number: int,
    token: str = Depends(require_auth)
):
    """
    Close a pull request without merging.
    """
    check_project_access(request, project_id)
    check_github_auth()

    repo = get_repo_for_project(project_id)
    success = github_service.close_pull(repo, number)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to close pull request #{number}"
        )

    return {"status": "ok", "message": f"Pull request #{number} closed"}


# =============================================================================
# GitHub Actions Endpoints
# =============================================================================

@router.get("/actions", response_model=List[WorkflowRunResponse])
async def list_workflow_runs(
    request: Request,
    project_id: str,
    limit: int = 10,
    token: str = Depends(require_auth)
):
    """
    List recent GitHub Actions workflow runs.

    Args:
        limit: Maximum number of runs to return (default 10, max 50)
    """
    check_project_access(request, project_id)
    check_github_auth()

    if limit > 50:
        limit = 50

    repo = get_repo_for_project(project_id)
    runs = github_service.list_runs(repo, limit=limit)

    return [run_to_response(run) for run in runs]


@router.get("/actions/{run_id}", response_model=WorkflowRunResponse)
async def get_workflow_run(
    request: Request,
    project_id: str,
    run_id: int,
    token: str = Depends(require_auth)
):
    """
    Get details of a specific workflow run.
    """
    check_project_access(request, project_id)
    check_github_auth()

    repo = get_repo_for_project(project_id)
    run = github_service.get_run(repo, run_id)

    if not run:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Workflow run #{run_id} not found"
        )

    return run_to_response(run)


@router.post("/actions/{run_id}/rerun")
async def rerun_workflow(
    request: Request,
    project_id: str,
    run_id: int,
    token: str = Depends(require_auth)
):
    """
    Re-run a workflow.
    """
    check_project_access(request, project_id)
    check_github_auth()

    repo = get_repo_for_project(project_id)
    success = github_service.rerun_workflow(repo, run_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to re-run workflow #{run_id}"
        )

    return {"status": "ok", "message": f"Workflow #{run_id} re-run triggered"}
