"""
GitHub integration API routes

Provides endpoints for managing GitHub pull requests and workflow runs
through the gh CLI.
"""

import asyncio
import logging
import time
from typing import Optional, List, Dict, Any

from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, Field

from app.core.github_service import (
    GitHubCliError,
    github_service,
    PullRequest,
    WorkflowRun,
)
from app.core.config import settings
from app.db import database
from app.api.auth import require_auth, get_api_user_from_request
from app.api.common import check_project_access, resolve_project_path

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
    draft: bool = Field(default=False, description="Open the pull request as a draft")


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

# Every handler below is ``async def`` and therefore runs ON the event loop.
# ``github_service`` and the project-path resolver are synchronous: they spawn
# ``gh``/``git`` subprocesses (up to a 60s timeout) and hit the database. Calling
# them inline froze the entire server — no other request, WebSocket frame or SSE
# byte could be served for the duration (finding G1). Everything blocking in
# this module now goes through ``asyncio.to_thread``; nothing here may call
# ``github_service.*``, ``resolve_project_path`` or the DB directly.

def get_project_path(request: Request, project_id: str) -> str:
    """Get the filesystem path for a project after verifying access.

    Blocking (database read + filesystem stat) — call via
    :func:`get_project_path_async` from async code.

    .. deprecated:: Use :func:`app.api.common.resolve_project_path` instead.
    """
    result = resolve_project_path(
        project_id, request=request, check_access=True, check_exists=True
    )
    return str(result)


async def get_project_path_async(request: Request, project_id: str) -> str:
    """Off-loop :func:`get_project_path`."""
    return await asyncio.to_thread(get_project_path, request, project_id)


def _resolve_repo_for_project(request: Request, project_id: str) -> str:
    """Blocking body of :func:`get_repo_for_project` (DB read + git subprocess)."""
    project_path = get_project_path(request, project_id)
    repo = github_service.get_repo_from_remote(project_path)

    if not repo:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not determine GitHub repository. Ensure project has a GitHub remote configured."
        )

    return repo


async def get_repo_for_project(request: Request, project_id: str) -> str:
    """Get the GitHub repository for a project from git remote (off-loop)."""
    return await asyncio.to_thread(_resolve_repo_for_project, request, project_id)


async def check_github_auth() -> None:
    """Check if GitHub CLI is authenticated. Raises HTTPException if not.

    ``github_service.is_authenticated`` caches its answer for minutes, so this
    is normally a dictionary lookup; the ``to_thread`` hop only matters on the
    refresh that actually spawns ``gh auth status``.
    """
    if not await asyncio.to_thread(github_service.is_authenticated):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="GitHub CLI is not authenticated. Please configure GitHub authentication."
        )


def _github_unavailable(action: str, exc: GitHubCliError) -> HTTPException:
    """Map a gh transport failure to 502 — "unknown", not "no data" (G2)."""
    detail = f"GitHub CLI could not {action}"
    if exc.stderr:
        detail = f"{detail}: {exc.stderr}"
    else:
        detail = f"{detail}: {exc}"
    logger.warning("gh unavailable while trying to %s: %s", action, exc)
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)


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


async def _broadcast_pr_changed(request: Request, project_id: str, branch_name: str) -> None:
    """Nudge every device that a PR lifecycle event happened on *branch_name*.

    Reuses the branch-changed channel (frontend already refreshes git state on
    it) so other tabs/devices re-query pr-state without a dedicated contract
    addition. Failures are logged, never raised — the HTTP response already
    succeeded.
    """
    try:
        from app.api.auth import get_api_user_from_request
        from app.core.sync_engine import sync_engine

        api_user = get_api_user_from_request(request)
        user_id = api_user["id"] if api_user else "admin"
        await sync_engine.broadcast_branch_changed(
            user_id=user_id, project_id=project_id, branch_name=branch_name
        )
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to broadcast PR change: {e}")


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
        authenticated=await asyncio.to_thread(github_service.is_authenticated)
    )

    if not response.authenticated:
        response.error = "GitHub CLI is not authenticated"
        return response

    try:
        project_path = await get_project_path_async(request, project_id)
        repo = await asyncio.to_thread(github_service.get_repo_from_remote, project_path)

        if repo:
            response.repo = repo
            response.repo_info = await asyncio.to_thread(github_service.get_repo_info, repo)
        else:
            response.error = "No GitHub remote found in project"

    except HTTPException as e:
        response.error = e.detail
    except GitHubCliError as e:
        # Repo is known, gh just could not answer — say so instead of
        # reporting the repository as missing.
        logger.warning("GitHub status: gh unavailable: %s", e)
        response.error = f"GitHub CLI is unavailable: {e.stderr or e}"
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
    await check_github_auth()

    if limit > 100:
        limit = 100

    repo = await get_repo_for_project(request, project_id)
    try:
        pulls = await asyncio.to_thread(
            github_service.list_pulls, repo, state=state, limit=limit
        )
    except GitHubCliError as e:
        raise _github_unavailable("list pull requests", e)

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
    await check_github_auth()

    repo = await get_repo_for_project(request, project_id)
    try:
        pr = await asyncio.to_thread(github_service.get_pull, repo, number)
    except GitHubCliError as e:
        raise _github_unavailable(f"read pull request #{number}", e)

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
    t_start = time.perf_counter()
    check_project_access(request, project_id)
    await check_github_auth()

    repo = await get_repo_for_project(request, project_id)

    try:
        t_gh_start = time.perf_counter()
        pr, error = await asyncio.to_thread(
            lambda: github_service.create_pull(
                repo=repo,
                title=body.title,
                body=body.body,
                head=body.head,
                base=body.base,
                draft=body.draft,
            )
        )
        t_gh_ms = (time.perf_counter() - t_gh_start) * 1000
    except Exception as e:
        logger.exception("Unhandled error creating pull request")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to create pull request: {e}"
        )

    if not pr:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create pull request: {error or 'unknown error'}"
        )

    t_total_ms = (time.perf_counter() - t_start) * 1000
    logger.info(
        f"[ship-it-timing] create-pr project={project_id} repo={repo} "
        f"head={body.head} total={t_total_ms:.0f}ms gh_cli={t_gh_ms:.0f}ms"
    )

    await _broadcast_pr_changed(request, project_id, body.head)

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
    t_start = time.perf_counter()
    check_project_access(request, project_id)
    await check_github_auth()

    method = body.method if body else "merge"

    repo = await get_repo_for_project(request, project_id)

    try:
        t_gh_start = time.perf_counter()
        success, error = await asyncio.to_thread(
            github_service.merge_pull, repo, number, method=method
        )
        t_gh_ms = (time.perf_counter() - t_gh_start) * 1000
    except Exception as e:
        logger.exception(f"Unhandled error merging pull request #{number}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to merge pull request #{number}: {e}"
        )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to merge pull request #{number}: {error or 'unknown error'}"
        )

    t_total_ms = (time.perf_counter() - t_start) * 1000
    logger.info(
        f"[ship-it-timing] merge-pr project={project_id} repo={repo} "
        f"pr=#{number} method={method} total={t_total_ms:.0f}ms gh_cli={t_gh_ms:.0f}ms"
    )

    await _broadcast_pr_changed(request, project_id, f"pr-#{number}-merged")

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
    await check_github_auth()

    repo = await get_repo_for_project(request, project_id)

    try:
        success, error = await asyncio.to_thread(
            github_service.close_pull, repo, number
        )
    except Exception as e:
        logger.exception(f"Unhandled error closing pull request #{number}")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to close pull request #{number}: {e}"
        )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to close pull request #{number}: {error or 'unknown error'}"
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
    await check_github_auth()

    if limit > 50:
        limit = 50

    repo = await get_repo_for_project(request, project_id)
    try:
        runs = await asyncio.to_thread(github_service.list_runs, repo, limit=limit)
    except GitHubCliError as e:
        raise _github_unavailable("list workflow runs", e)

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
    await check_github_auth()

    repo = await get_repo_for_project(request, project_id)
    try:
        run = await asyncio.to_thread(github_service.get_run, repo, run_id)
    except GitHubCliError as e:
        raise _github_unavailable(f"read workflow run #{run_id}", e)

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
    await check_github_auth()

    repo = await get_repo_for_project(request, project_id)
    success = await asyncio.to_thread(github_service.rerun_workflow, repo, run_id)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to re-run workflow #{run_id}"
        )

    return {"status": "ok", "message": f"Workflow #{run_id} re-run triggered"}
