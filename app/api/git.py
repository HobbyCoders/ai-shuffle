"""
Git management API routes

Provides endpoints for git repository operations including:
- Repository status
- Branch management (list, create, delete, checkout)
- Worktree management (list, create, delete)
- Commit graph for visualization
- Fetch from remote
"""

import uuid
import logging
from typing import List, Optional, Any

from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.git_service import git_service
from app.core.worktree_manager import worktree_manager
from app.db import database
from app.api.auth import require_auth, get_api_user_from_request

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/projects/{project_id}/git", tags=["Git"])


# =============================================================================
# Request/Response Models
# =============================================================================

class BranchInfo(BaseModel):
    """Branch information"""
    name: str
    is_current: bool = False
    is_remote: bool = False
    commit: Optional[str] = None
    commit_message: Optional[str] = None
    upstream: Optional[str] = None
    ahead: int = 0
    behind: int = 0


class BranchListResponse(BaseModel):
    """Response for branch list endpoint"""
    branches: List[BranchInfo]
    current_branch: Optional[str] = None


class CreateBranchRequest(BaseModel):
    """Request to create a new branch"""
    name: str = Field(..., min_length=1, max_length=255)
    start_point: Optional[str] = None


class CheckoutRequest(BaseModel):
    """Request to checkout a branch"""
    ref: str = Field(..., min_length=1, max_length=255)


class CommitInfo(BaseModel):
    """Commit information for graph visualization"""
    sha: str
    short_sha: str
    message: str
    author: str
    author_email: str
    timestamp: str
    parents: List[str] = []
    refs: List[str] = []


class CommitGraphResponse(BaseModel):
    """Response for commit graph endpoint"""
    commits: List[CommitInfo]
    total: int


class StagedFile(BaseModel):
    """Staged file info"""
    file: str
    status: str  # A=added, M=modified, D=deleted, R=renamed, C=copied


class GitStatusResponse(BaseModel):
    """Response for git status endpoint"""
    is_git_repo: bool
    current_branch: Optional[str] = None
    is_detached: bool = False
    head_commit: Optional[str] = None
    remote_url: Optional[str] = None
    is_clean: bool = True
    staged: List[StagedFile] = []
    modified: List[str] = []
    untracked: List[str] = []
    ahead: int = 0
    behind: int = 0
    conflicts: List[str] = []
    # Repository info from database
    repository_id: Optional[str] = None
    default_branch: Optional[str] = None
    github_repo_name: Optional[str] = None
    last_synced_at: Optional[str] = None


class FetchResponse(BaseModel):
    """Response for fetch endpoint"""
    success: bool
    message: str


class OperationResponse(BaseModel):
    """Generic operation response"""
    success: bool
    message: str


# =============================================================================
# Worktree Models
# =============================================================================

class WorktreeCreate(BaseModel):
    """Request to create a new worktree"""
    branch_name: str = Field(..., min_length=1, max_length=255)
    create_new_branch: bool = False
    base_branch: Optional[str] = None
    profile_id: Optional[str] = None


class SessionInfo(BaseModel):
    """Session info embedded in worktree response"""
    id: str
    title: Optional[str] = None
    status: str
    updated_at: Optional[str] = None


class WorktreeGitStatus(BaseModel):
    """Git status for a worktree"""
    is_clean: bool = True
    current_branch: Optional[str] = None
    modified: List[str] = []
    staged: List[StagedFile] = []
    untracked: List[str] = []


class WorktreeInfo(BaseModel):
    """Worktree information"""
    id: str
    repository_id: str
    session_id: Optional[str] = None
    branch_name: str
    worktree_path: str
    base_branch: Optional[str] = None
    status: str
    created_at: str
    session: Optional[SessionInfo] = None
    exists: Optional[bool] = None
    git_status: Optional[WorktreeGitStatus] = None


class WorktreeListResponse(BaseModel):
    """Response for worktree list endpoint"""
    worktrees: List[WorktreeInfo]
    total: int


class WorktreeCreateResponse(BaseModel):
    """Response for worktree creation"""
    success: bool
    message: str
    worktree: Optional[WorktreeInfo] = None
    session: Optional[dict] = None


class WorktreeSyncResponse(BaseModel):
    """Response for worktree sync endpoint"""
    synced: int
    orphaned: int
    cleaned_up: int
    errors: List[str] = []


# =============================================================================
# Helper Functions
# =============================================================================

def get_project_path(project_id: str) -> str:
    """Get the filesystem path for a project."""
    project = database.get_project(project_id)
    if not project:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Project not found: {project_id}"
        )
    return str(settings.workspace_dir / project["path"])


def check_project_access(request: Request, project_id: str) -> None:
    """Check if the user has access to the project. Raises HTTPException if not."""
    api_user = get_api_user_from_request(request)
    if api_user and api_user.get("project_id"):
        if api_user["project_id"] != project_id:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Access denied to this project"
            )


def ensure_git_repository_record(project_id: str, working_dir: str) -> Optional[dict]:
    """
    Ensure a git_repositories record exists for this project if it's a git repo.
    Creates one if needed, returns the record or None if not a git repo.
    """
    if not git_service.is_git_repo(working_dir):
        return None

    repo = database.get_git_repository_by_project(project_id)
    if repo:
        return repo

    # Create a new record
    repo_id = f"repo-{uuid.uuid4().hex[:12]}"
    remote_url = git_service.get_remote_url(working_dir)
    default_branch = git_service.get_default_branch(working_dir)

    # Try to extract GitHub repo name from URL
    github_repo_name = None
    if remote_url:
        # Handle both HTTPS and SSH URLs
        # https://github.com/owner/repo.git or git@github.com:owner/repo.git
        if "github.com" in remote_url:
            try:
                if remote_url.startswith("git@"):
                    # git@github.com:owner/repo.git
                    github_repo_name = remote_url.split(":")[1].replace(".git", "")
                else:
                    # https://github.com/owner/repo.git
                    parts = remote_url.replace(".git", "").split("github.com/")
                    if len(parts) > 1:
                        github_repo_name = parts[1]
            except Exception:
                pass

    repo = database.create_git_repository(
        repository_id=repo_id,
        project_id=project_id,
        remote_url=remote_url,
        default_branch=default_branch,
        github_repo_name=github_repo_name
    )
    logger.info(f"Created git repository record for project {project_id}: {repo_id}")
    return repo


# =============================================================================
# Endpoints
# =============================================================================

@router.get("/status", response_model=GitStatusResponse)
async def get_git_status(
    request: Request,
    project_id: str,
    token: str = Depends(require_auth)
):
    """
    Get git repository status for a project.

    Returns current branch, uncommitted changes, and sync status with remote.
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    # Get git status
    status_data = git_service.get_status(working_dir)

    # Convert staged files to proper format
    staged_files = [
        StagedFile(file=s["file"], status=s["status"])
        for s in status_data.get("staged", [])
    ]

    # Ensure repository record exists and get additional info
    repo = ensure_git_repository_record(project_id, working_dir)

    return GitStatusResponse(
        is_git_repo=status_data["is_git_repo"],
        current_branch=status_data["current_branch"],
        is_detached=status_data["is_detached"],
        head_commit=status_data["head_commit"],
        remote_url=status_data["remote_url"],
        is_clean=status_data["is_clean"],
        staged=staged_files,
        modified=status_data["modified"],
        untracked=status_data["untracked"],
        ahead=status_data["ahead"],
        behind=status_data["behind"],
        conflicts=status_data["conflicts"],
        repository_id=repo["id"] if repo else None,
        default_branch=repo["default_branch"] if repo else None,
        github_repo_name=repo["github_repo_name"] if repo else None,
        last_synced_at=repo["last_synced_at"] if repo else None
    )


@router.get("/branches", response_model=BranchListResponse)
async def list_branches(
    request: Request,
    project_id: str,
    include_remote: bool = True,
    token: str = Depends(require_auth)
):
    """
    List all branches in the repository.

    Args:
        project_id: Project ID
        include_remote: Include remote tracking branches (default: true)
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    if not git_service.is_git_repo(working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    branches = git_service.list_branches(working_dir, include_remote=include_remote)
    current_branch = git_service.get_current_branch(working_dir)

    branch_infos = [
        BranchInfo(
            name=b["name"],
            is_current=b["is_current"],
            is_remote=b["is_remote"],
            commit=b["commit"],
            commit_message=b["commit_message"],
            upstream=b["upstream"],
            ahead=b["ahead"],
            behind=b["behind"]
        )
        for b in branches
    ]

    return BranchListResponse(
        branches=branch_infos,
        current_branch=current_branch
    )


@router.post("/branches", response_model=OperationResponse)
async def create_branch(
    request: Request,
    project_id: str,
    body: CreateBranchRequest,
    token: str = Depends(require_auth)
):
    """
    Create a new branch.

    Args:
        project_id: Project ID
        body: Branch creation request with name and optional start point
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    if not git_service.is_git_repo(working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Validate branch name doesn't have invalid characters
    invalid_chars = [" ", "~", "^", ":", "\\", "?", "*", "[", "..", "@{"]
    for char in invalid_chars:
        if char in body.name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid branch name: contains '{char}'"
            )

    success = git_service.create_branch(
        working_dir,
        body.name,
        start_point=body.start_point
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create branch '{body.name}'. Branch may already exist or start point is invalid."
        )

    return OperationResponse(
        success=True,
        message=f"Created branch '{body.name}'"
    )


@router.delete("/branches/{branch_name:path}", response_model=OperationResponse)
async def delete_branch(
    request: Request,
    project_id: str,
    branch_name: str,
    force: bool = False,
    token: str = Depends(require_auth)
):
    """
    Delete a branch.

    Args:
        project_id: Project ID
        branch_name: Name of the branch to delete
        force: Force delete even if not fully merged
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    if not git_service.is_git_repo(working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Don't allow deleting current branch
    current = git_service.get_current_branch(working_dir)
    if current == branch_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete the currently checked out branch"
        )

    success = git_service.delete_branch(working_dir, branch_name, force=force)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to delete branch '{branch_name}'. It may not exist or is not fully merged (use force=true)."
        )

    return OperationResponse(
        success=True,
        message=f"Deleted branch '{branch_name}'"
    )


@router.post("/checkout", response_model=OperationResponse)
async def checkout_branch(
    request: Request,
    project_id: str,
    body: CheckoutRequest,
    token: str = Depends(require_auth)
):
    """
    Checkout a branch or commit.

    Args:
        project_id: Project ID
        body: Checkout request with ref (branch name or commit SHA)
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    if not git_service.is_git_repo(working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Check for uncommitted changes
    status_data = git_service.get_status(working_dir)
    if not status_data["is_clean"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot checkout: you have uncommitted changes. Commit or stash them first."
        )

    success = git_service.checkout(working_dir, body.ref)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to checkout '{body.ref}'. It may not exist."
        )

    return OperationResponse(
        success=True,
        message=f"Checked out '{body.ref}'"
    )


@router.get("/graph", response_model=CommitGraphResponse)
async def get_commit_graph(
    request: Request,
    project_id: str,
    limit: int = 100,
    branch: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Get commit graph data for visualization.

    Args:
        project_id: Project ID
        limit: Maximum number of commits to return (default: 100)
        branch: Filter to specific branch (default: all branches)
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    if not git_service.is_git_repo(working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Cap limit to prevent excessive data
    limit = min(limit, 500)

    commits = git_service.get_commit_graph(working_dir, limit=limit, branch=branch)

    commit_infos = [
        CommitInfo(
            sha=c["sha"],
            short_sha=c["short_sha"],
            message=c["message"],
            author=c["author"],
            author_email=c["author_email"],
            timestamp=c["timestamp"],
            parents=c["parents"],
            refs=c["refs"]
        )
        for c in commits
    ]

    return CommitGraphResponse(
        commits=commit_infos,
        total=len(commit_infos)
    )


@router.post("/fetch", response_model=FetchResponse)
async def fetch_from_remote(
    request: Request,
    project_id: str,
    remote: str = "origin",
    token: str = Depends(require_auth)
):
    """
    Fetch from remote repository.

    Args:
        project_id: Project ID
        remote: Remote name (default: origin)
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    if not git_service.is_git_repo(working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    success = git_service.fetch(working_dir, remote=remote)

    # Update last_synced_at if we have a repository record
    if success:
        repo = database.get_git_repository_by_project(project_id)
        if repo:
            database.update_git_repository_synced(repo["id"])

    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch from remote '{remote}'. Check network connection and authentication."
        )

    return FetchResponse(
        success=True,
        message=f"Fetched from '{remote}'"
    )


# =============================================================================
# Worktree Endpoints
# =============================================================================

def _convert_worktree_to_info(wt: dict) -> WorktreeInfo:
    """Convert worktree dict to WorktreeInfo model"""
    session_info = None
    if wt.get("session"):
        session_info = SessionInfo(
            id=wt["session"]["id"],
            title=wt["session"].get("title"),
            status=wt["session"]["status"],
            updated_at=wt["session"].get("updated_at")
        )

    git_status_info = None
    if wt.get("git_status"):
        gs = wt["git_status"]
        staged_files = [
            StagedFile(file=s["file"], status=s["status"])
            for s in gs.get("staged", [])
        ]
        git_status_info = WorktreeGitStatus(
            is_clean=gs.get("is_clean", True),
            current_branch=gs.get("current_branch"),
            modified=gs.get("modified", []),
            staged=staged_files,
            untracked=gs.get("untracked", [])
        )

    return WorktreeInfo(
        id=wt["id"],
        repository_id=wt["repository_id"],
        session_id=wt.get("session_id"),
        branch_name=wt["branch_name"],
        worktree_path=wt["worktree_path"],
        base_branch=wt.get("base_branch"),
        status=wt["status"],
        created_at=wt["created_at"],
        session=session_info,
        exists=wt.get("exists"),
        git_status=git_status_info
    )


@router.get("/worktrees", response_model=WorktreeListResponse)
async def list_worktrees(
    request: Request,
    project_id: str,
    token: str = Depends(require_auth)
):
    """
    List all worktrees for a project.

    Returns worktrees with their associated session information.
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    if not git_service.is_git_repo(working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    worktrees = worktree_manager.get_worktrees_for_project(project_id)

    worktree_infos = [_convert_worktree_to_info(wt) for wt in worktrees]

    return WorktreeListResponse(
        worktrees=worktree_infos,
        total=len(worktree_infos)
    )


@router.post("/worktrees", response_model=WorktreeCreateResponse)
async def create_worktree(
    request: Request,
    project_id: str,
    body: WorktreeCreate,
    token: str = Depends(require_auth)
):
    """
    Create a new worktree and associated chat session.

    This creates a git worktree for the specified branch and a new
    chat session that will work in that worktree directory.

    Args:
        project_id: Project ID
        body: Worktree creation request
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    if not git_service.is_git_repo(working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Validate branch name
    invalid_chars = [" ", "~", "^", ":", "\\", "?", "*", "[", "..", "@{"]
    for char in invalid_chars:
        if char in body.branch_name:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid branch name: contains '{char}'"
            )

    # Create worktree and session
    worktree, session = worktree_manager.create_worktree_session(
        project_id=project_id,
        branch_name=body.branch_name,
        create_new_branch=body.create_new_branch,
        base_branch=body.base_branch,
        profile_id=body.profile_id
    )

    if not worktree or not session:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to create worktree. Branch may already be in use or doesn't exist."
        )

    # Get full worktree details for response
    worktree_details = worktree_manager.get_worktree_details(worktree["id"])

    return WorktreeCreateResponse(
        success=True,
        message=f"Created worktree for branch '{body.branch_name}'",
        worktree=_convert_worktree_to_info(worktree_details) if worktree_details else None,
        session=session
    )


@router.get("/worktrees/{worktree_id}", response_model=WorktreeInfo)
async def get_worktree(
    request: Request,
    project_id: str,
    worktree_id: str,
    token: str = Depends(require_auth)
):
    """
    Get detailed information about a specific worktree.

    Includes session info and current git status of the worktree.
    """
    check_project_access(request, project_id)

    worktree = worktree_manager.get_worktree_details(worktree_id)
    if not worktree:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worktree not found: {worktree_id}"
        )

    # Verify worktree belongs to this project
    repo = database.get_git_repository_by_project(project_id)
    if not repo or worktree["repository_id"] != repo["id"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worktree not found in this project: {worktree_id}"
        )

    return _convert_worktree_to_info(worktree)


@router.delete("/worktrees/{worktree_id}", response_model=OperationResponse)
async def delete_worktree(
    request: Request,
    project_id: str,
    worktree_id: str,
    keep_branch: bool = True,
    token: str = Depends(require_auth)
):
    """
    Remove a worktree and optionally delete its branch.

    Args:
        project_id: Project ID
        worktree_id: Worktree ID to remove
        keep_branch: If true (default), keep the branch after removing worktree
    """
    check_project_access(request, project_id)

    # Get worktree to verify it exists and belongs to project
    worktree = database.get_worktree(worktree_id)
    if not worktree:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worktree not found: {worktree_id}"
        )

    # Verify worktree belongs to this project
    repo = database.get_git_repository_by_project(project_id)
    if not repo or worktree["repository_id"] != repo["id"]:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Worktree not found in this project: {worktree_id}"
        )

    success = worktree_manager.cleanup_worktree(worktree_id, keep_branch=keep_branch)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to remove worktree"
        )

    branch_msg = " (branch kept)" if keep_branch else " (branch deleted)"
    return OperationResponse(
        success=True,
        message=f"Removed worktree for branch '{worktree['branch_name']}'{branch_msg}"
    )


@router.post("/worktrees/sync", response_model=WorktreeSyncResponse)
async def sync_worktrees(
    request: Request,
    project_id: str,
    token: str = Depends(require_auth)
):
    """
    Synchronize database worktree records with actual git worktrees.

    This cleans up orphaned records and marks worktrees that no longer exist.
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    if not git_service.is_git_repo(working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    result = worktree_manager.sync_worktrees(project_id)

    return WorktreeSyncResponse(
        synced=result.get("synced", 0),
        orphaned=result.get("orphaned", 0),
        cleaned_up=result.get("cleaned_up", 0),
        errors=result.get("errors", [])
    )
