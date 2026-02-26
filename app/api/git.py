"""
Git management API routes

Provides endpoints for git repository operations including:
- Repository status and diff stats
- Branch management (list, create, delete, checkout)
- Worktree management (list, create, delete, file browsing)
- Commit, push, and commit graph visualization
- AI-powered commit message and PR description generation
- Fetch from remote
"""

import asyncio
import uuid
import logging
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Depends, Request, status
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.git_service import git_service
from app.core.worktree_manager import worktree_manager
from app.core.project_utils import get_project_working_dir, get_worktree_working_dir
from app.db import database
from app.api.auth import require_auth, get_api_user_from_request
from app.api.common import check_project_access

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
    # Worktree association
    has_worktree: bool = False
    worktree_id: Optional[str] = None


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
    status: Optional[dict] = None


class OperationResponse(BaseModel):
    """Generic operation response"""
    success: bool
    message: str
    status: Optional[dict] = None
    diff_stats: Optional[dict] = None


class DiffStatsResponse(BaseModel):
    """Response for diff stats endpoint"""
    additions: int
    deletions: int
    files_changed: int


class CommitRequest(BaseModel):
    """Request to create a commit"""
    message: str = Field(..., min_length=1, max_length=5000)
    include_unstaged: bool = Field(default=True, description="Stage unstaged files before committing")


class CommitResponse(BaseModel):
    """Response for commit endpoint"""
    success: bool
    message: str
    commit_sha: Optional[str] = None
    status: Optional[dict] = None
    diff_stats: Optional[dict] = None


class PushRequest(BaseModel):
    """Request to push commits"""
    remote: str = Field(default="origin", pattern=r'^[a-zA-Z0-9._-]+$')
    branch: Optional[str] = None
    set_upstream: bool = Field(default=False)
    force: bool = Field(default=False)


class PullRequest(BaseModel):
    """Request to pull from remote"""
    remote: str = Field(default="origin", pattern=r'^[a-zA-Z0-9._-]+$')
    strategy: str = Field(default="merge", pattern=r'^(merge|rebase)$')
    auto_stash: bool = Field(default=False, description="Auto-stash uncommitted changes before pull and restore after")


class PullResponse(BaseModel):
    """Response for pull endpoint"""
    success: bool
    message: str
    conflicts: List[str] = []
    has_conflicts: bool = False
    status: Optional[dict] = None
    diff_stats: Optional[dict] = None


class InitRepoRequest(BaseModel):
    """Request to initialize a git repository"""
    create_initial_commit: bool = Field(default=True, description="Create an initial commit after init")
    create_github_repo: bool = Field(default=False, description="Create a GitHub repository and add as remote")
    github_repo_name: Optional[str] = Field(default=None, max_length=100, description="GitHub repo name (if creating)")
    github_private: bool = Field(default=True, description="Make the GitHub repo private")
    default_branch: str = Field(default="main", pattern=r'^[a-zA-Z0-9._/-]+$')


class InitRepoResponse(BaseModel):
    """Response for init repo endpoint"""
    success: bool
    message: str
    repository_id: Optional[str] = None
    github_repo_url: Optional[str] = None
    github_repo_name: Optional[str] = None


class CloneRepoRequest(BaseModel):
    """Request to clone a remote repository"""
    clone_url: str = Field(..., min_length=1, max_length=500, description="Remote repository URL (HTTPS or SSH)")


class CloneRepoResponse(BaseModel):
    """Response for clone repo endpoint"""
    success: bool
    message: str
    repository_id: Optional[str] = None


class GenerateCommitMessageRequest(BaseModel):
    """Request to generate a commit message using AI"""
    include_unstaged: bool = Field(default=True)


class GenerateCommitMessageResponse(BaseModel):
    """Response for AI-generated commit message"""
    message: str


class GeneratePRDescriptionRequest(BaseModel):
    """Request to generate a PR title and description using AI"""
    base_branch: str = Field(default="main")


class GeneratePRDescriptionResponse(BaseModel):
    """Response for AI-generated PR description"""
    title: str
    body: str


# =============================================================================
# Worktree Models
# =============================================================================

class WorktreeCreate(BaseModel):
    """Request to create a new worktree"""
    branch_name: str = Field(..., min_length=1, max_length=255)
    create_new_branch: bool = False
    base_branch: Optional[str] = None
    profile_id: Optional[str] = None
    copy_uncommitted: bool = Field(
        default=False,
        description="Copy uncommitted files (modified and untracked) from main repo to worktree"
    )


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
    # File counts for quick display (e.g., "3 modified")
    modified_count: int = 0
    staged_count: int = 0
    untracked_count: int = 0
    # Full file lists (optional, can be omitted for list endpoint)
    modified: List[str] = []
    staged: List[StagedFile] = []
    untracked: List[str] = []


class WorktreeInfo(BaseModel):
    """Worktree information"""
    id: str
    repository_id: str
    session_id: Optional[str] = None  # Deprecated - use sessions list
    branch_name: str
    worktree_path: str
    base_branch: Optional[str] = None
    status: str
    created_at: str
    # New session relationship model
    sessions: List[SessionInfo] = []  # All sessions that have used this worktree
    session_count: int = 0
    active_session: Optional[SessionInfo] = None  # Currently active session
    exists: Optional[bool] = None  # Whether worktree folder exists on disk
    git_status: Optional[WorktreeGitStatus] = None
    # Legacy - deprecated
    session: Optional[SessionInfo] = None


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
    errors: Optional[List[str]] = None  # Errors from copy_uncommitted operation


class WorktreeSyncResponse(BaseModel):
    """Response for worktree sync endpoint"""
    synced: int
    orphaned: int
    cleaned_up: int
    imported: int = 0  # External worktrees imported into database
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
    return get_project_working_dir(project)


def get_working_dir(project_id: str, worktree_id: Optional[str] = None) -> str:
    """
    Get the working directory for git operations.

    When worktree_id is provided, returns the worktree's path.
    Otherwise returns the main project path.
    """
    if worktree_id:
        worktree = database.get_worktree(worktree_id)
        if not worktree:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Worktree not found: {worktree_id}"
            )
        return get_worktree_working_dir(worktree)

    return get_project_path(project_id)


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
            except (IndexError, ValueError) as e:
                logger.warning(f"Failed to parse GitHub repo name from URL '{remote_url}': {e}")

    repo = database.create_git_repository(
        repository_id=repo_id,
        project_id=project_id,
        remote_url=remote_url,
        default_branch=default_branch,
        github_repo_name=github_repo_name
    )
    logger.debug(f"Created git repository record for project {project_id}: {repo_id}")
    return repo


# =============================================================================
# Endpoints
# =============================================================================

@router.get("/status", response_model=GitStatusResponse)
async def get_git_status(
    request: Request,
    project_id: str,
    worktree_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Get git repository status for a project or worktree.

    Returns current branch, uncommitted changes, and sync status with remote.
    When worktree_id is provided, returns status for the worktree directory.
    """
    check_project_access(request, project_id)
    working_dir = get_working_dir(project_id, worktree_id)

    # Get git status directly from git CLI
    status_data = await asyncio.to_thread(git_service.get_status, working_dir)

    # Convert staged files to proper format
    staged_files = [
        StagedFile(file=s["file"], status=s["status"])
        for s in status_data.get("staged", [])
    ]

    # Ensure repository record exists and get additional info (DB-level, not cached)
    repo = await asyncio.to_thread(ensure_git_repository_record, project_id, working_dir)

    return GitStatusResponse(
        is_git_repo=status_data.get("is_git_repo", False),
        current_branch=status_data.get("current_branch"),
        is_detached=status_data.get("is_detached", False),
        head_commit=status_data.get("head_commit"),
        remote_url=status_data.get("remote_url"),
        is_clean=status_data.get("is_clean", True),
        staged=staged_files,
        modified=status_data.get("modified", []),
        untracked=status_data.get("untracked", []),
        ahead=status_data.get("ahead", 0),
        behind=status_data.get("behind", 0),
        conflicts=status_data.get("conflicts", []),
        repository_id=repo["id"] if repo else None,
        default_branch=repo["default_branch"] if repo else None,
        github_repo_name=repo["github_repo_name"] if repo else None,
        last_synced_at=repo["last_synced_at"] if repo else None
    )


@router.post("/init", response_model=InitRepoResponse)
async def init_repository(
    request: Request,
    project_id: str,
    body: InitRepoRequest,
    token: str = Depends(require_auth)
):
    """
    Initialize a new git repository for a project.

    Optionally creates an initial commit and/or a GitHub repository with remote.
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    # Guard: don't re-init an existing repo
    if await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is already a git repository"
        )

    # Step 1: git init
    success = await asyncio.to_thread(
        git_service.init_repo, working_dir, default_branch=body.default_branch
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to initialize git repository"
        )

    github_repo_url = None
    github_full_name = None

    # Step 2: Optional initial commit
    if body.create_initial_commit:
        staged = await asyncio.to_thread(git_service.stage_files, working_dir, ["."])
        if staged:
            await asyncio.to_thread(git_service.commit, working_dir, "Initial commit")

    # Step 3: Optional GitHub repo creation
    if body.create_github_repo and body.github_repo_name:
        from app.core.github_service import github_service

        if not github_service.is_authenticated():
            raise HTTPException(
                status_code=400,
                detail="GitHub CLI is not authenticated. Run 'gh auth login' in your terminal first."
            )

        repo_info = await asyncio.to_thread(
            github_service.create_repo,
            name=body.github_repo_name,
            private=body.github_private,
        )
        if not repo_info:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to create GitHub repository '{body.github_repo_name}'. Check logs for details."
            )

        github_repo_url = repo_info['url']
        github_full_name = repo_info['full_name']
        clone_url = repo_info['clone_url']

        # Add remote origin
        result = await asyncio.to_thread(
            git_service._run_git,
            working_dir,
            ["remote", "add", "origin", clone_url],
            timeout=10
        )
        if result.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to add git remote: {result.stderr}"
            )

        # Push initial commit if we made one
        if body.create_initial_commit:
            push_success, push_error = await asyncio.to_thread(
                git_service.push,
                working_dir,
                remote="origin",
                branch=body.default_branch,
                set_upstream=True
            )
            if not push_success:
                raise HTTPException(
                    status_code=500,
                    detail=f"Failed to push to GitHub: {push_error or 'Unknown error'}"
                )

    # Step 4: Create database record
    repo = await asyncio.to_thread(ensure_git_repository_record, project_id, working_dir)

    # Update record with GitHub info if we created a repo
    if github_full_name and repo:
        database.update_git_repository(
            repo["id"],
            remote_url=await asyncio.to_thread(git_service.get_remote_url, working_dir),
            github_repo_name=github_full_name,
        )

    message = "Git repository initialized"
    if body.create_initial_commit:
        message += " with initial commit"
    if github_repo_url:
        message += f" and linked to {github_full_name}"

    return InitRepoResponse(
        success=True,
        message=message,
        repository_id=repo["id"] if repo else None,
        github_repo_url=github_repo_url,
        github_repo_name=github_full_name,
    )


@router.post("/clone", response_model=CloneRepoResponse)
async def clone_repository(
    request: Request,
    project_id: str,
    body: CloneRepoRequest,
    token: str = Depends(require_auth)
):
    """
    Clone a remote repository into a project's working directory.

    The project directory must not already be a git repository.
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    # Guard: don't clone into an existing repo
    if await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is already a git repository"
        )

    # Ensure directory exists
    working_path = Path(working_dir)
    working_path.mkdir(parents=True, exist_ok=True)

    # Clone
    success, error = await asyncio.to_thread(
        git_service.clone_repo, working_dir, body.clone_url
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clone repository: {error}"
        )

    # Create database record
    repo = await asyncio.to_thread(ensure_git_repository_record, project_id, working_dir)

    # Update record with remote info from cloned repo
    if repo:
        remote_url = await asyncio.to_thread(git_service.get_remote_url, working_dir)
        current_branch = await asyncio.to_thread(git_service.get_current_branch, working_dir)
        database.update_git_repository(
            repo["id"],
            remote_url=remote_url,
            default_branch=current_branch or "main",
        )

    return CloneRepoResponse(
        success=True,
        message=f"Repository cloned from {body.clone_url}",
        repository_id=repo["id"] if repo else None,
    )


@router.get("/branches", response_model=BranchListResponse)
async def list_branches(
    request: Request,
    project_id: str,
    include_remote: bool = True,
    worktree_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    List all branches in the repository.

    Args:
        project_id: Project ID
        include_remote: Include remote tracking branches (default: true)
        worktree_id: Optional worktree ID - marks the worktree's branch as current
    """
    check_project_access(request, project_id)
    working_dir = get_working_dir(project_id, worktree_id)

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Get branches directly from git CLI
    branches_list = await asyncio.to_thread(git_service.list_branches, working_dir, include_remote=include_remote)
    current_branch = await asyncio.to_thread(git_service.get_current_branch, working_dir)
    branch_data = {"branches": branches_list, "current_branch": current_branch}

    # Enrich with worktree associations from database
    repo = database.get_git_repository_by_project(project_id)
    worktree_branch_map: dict[str, str] = {}  # branch_name -> worktree_id
    if repo:
        worktrees = database.get_worktrees_for_repository(repo["id"])
        for wt in worktrees:
            if wt.get("branch_name"):
                worktree_branch_map[wt["branch_name"]] = wt["id"]

    branch_infos = [
        BranchInfo(
            name=b["name"],
            is_current=b["is_current"],
            is_remote=b["is_remote"],
            commit=b.get("commit"),
            commit_message=b.get("commit_message"),
            upstream=b.get("upstream"),
            ahead=b.get("ahead", 0),
            behind=b.get("behind", 0),
            has_worktree=b["name"] in worktree_branch_map,
            worktree_id=worktree_branch_map.get(b["name"])
        )
        for b in branch_data.get("branches", [])
    ]

    return BranchListResponse(
        branches=branch_infos,
        current_branch=branch_data.get("current_branch")
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

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
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

    success = await asyncio.to_thread(
        git_service.create_branch, working_dir, body.name, start_point=body.start_point
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

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Don't allow deleting current branch
    current = await asyncio.to_thread(git_service.get_current_branch, working_dir)
    if current == branch_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete the currently checked out branch"
        )

    success = await asyncio.to_thread(
        git_service.delete_branch, working_dir, branch_name, force=force
    )

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
    worktree_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Checkout a branch or commit.

    Args:
        project_id: Project ID
        body: Checkout request with ref (branch name or commit SHA)
        worktree_id: If provided, returns error (checkout not allowed in worktrees)
    """
    check_project_access(request, project_id)

    # Checkout is not allowed in worktrees - they are bound to their branch
    if worktree_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot checkout branches in a worktree. Worktrees are bound to their branch."
        )

    working_dir = get_project_path(project_id)

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Check for uncommitted changes
    status_data = await asyncio.to_thread(git_service.get_status, working_dir)
    if not status_data["is_clean"]:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot checkout: you have uncommitted changes. Commit or stash them first."
        )

    success = await asyncio.to_thread(git_service.checkout, working_dir, body.ref)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to checkout '{body.ref}'. It may not exist."
        )

    # Broadcast branch_changed for cross-device sync
    api_user = get_api_user_from_request(request)
    user_id = api_user["id"] if api_user else "admin"
    try:
        from app.core.sync_engine import sync_engine
        await sync_engine.broadcast_branch_changed(
            user_id=user_id,
            project_id=project_id,
            branch_name=body.ref
        )
    except Exception as e:
        logger.warning(f"Failed to broadcast branch change: {e}")

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

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Cap limit to prevent excessive data
    limit = min(limit, 500)

    commits = await asyncio.to_thread(
        git_service.get_commit_graph, working_dir, limit=limit, branch=branch
    )

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
    worktree_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Fetch from remote repository.

    Args:
        project_id: Project ID
        remote: Remote name (default: origin)
        worktree_id: Optional worktree ID (fetch works from any worktree)
    """
    check_project_access(request, project_id)
    working_dir = get_working_dir(project_id, worktree_id)

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Validate remote name to prevent argument injection
    import re
    if not re.match(r'^[a-zA-Z0-9._-]+$', remote):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid remote name"
        )

    success = await asyncio.to_thread(git_service.fetch, working_dir, remote=remote)

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

    # Fetch inline git state for instant frontend update
    inline_status = await asyncio.to_thread(git_service.get_status, working_dir)

    return FetchResponse(
        success=True,
        message=f"Fetched from '{remote}'",
        status=inline_status,
    )


# =============================================================================
# Diff Stats, Commit, Push Endpoints
# =============================================================================

@router.get("/diff-stats", response_model=DiffStatsResponse)
async def get_diff_stats(
    request: Request,
    project_id: str,
    include_unstaged: bool = True,
    worktree_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Get diff statistics (lines added/removed).

    Args:
        project_id: Project ID
        include_unstaged: Include unstaged changes (default: true)
        worktree_id: Optional worktree ID to get diff stats for worktree
    """
    check_project_access(request, project_id)
    working_dir = get_working_dir(project_id, worktree_id)

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Get diff stats directly from git CLI
    stats = await asyncio.to_thread(git_service.get_diff_stats, working_dir, include_unstaged=include_unstaged)

    return DiffStatsResponse(
        additions=stats.get("additions", 0),
        deletions=stats.get("deletions", 0),
        files_changed=stats.get("files_changed", 0),
    )


@router.post("/commit", response_model=CommitResponse)
async def create_commit(
    request: Request,
    project_id: str,
    body: CommitRequest,
    worktree_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Create a commit.

    Optionally stages all changes (including unstaged) before committing.
    When worktree_id is provided, commits in the worktree directory.
    """
    check_project_access(request, project_id)
    working_dir = get_working_dir(project_id, worktree_id)

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Stage files if requested
    if body.include_unstaged:
        success = await asyncio.to_thread(git_service.stage_files, working_dir, ["."])
        if not success:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to stage files"
            )
    else:
        # Check if there are staged changes
        status_data = await asyncio.to_thread(git_service.get_status, working_dir)
        if not status_data["staged"]:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="No staged changes to commit"
            )

    # Create commit
    success, commit_sha = await asyncio.to_thread(git_service.commit, working_dir, body.message)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Failed to create commit. There may be no changes to commit."
        )

    # Fetch inline git state for instant frontend update
    inline_status = await asyncio.to_thread(git_service.get_status, working_dir)
    inline_diff = await asyncio.to_thread(git_service.get_diff_stats, working_dir)

    return CommitResponse(
        success=True,
        message=f"Created commit {commit_sha}" if commit_sha else "Created commit",
        commit_sha=commit_sha,
        status=inline_status,
        diff_stats=inline_diff,
    )


@router.post("/push", response_model=OperationResponse)
async def push_commits(
    request: Request,
    project_id: str,
    body: PushRequest,
    worktree_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Push commits to remote.

    When worktree_id is provided, pushes from the worktree directory.
    """
    check_project_access(request, project_id)
    working_dir = get_working_dir(project_id, worktree_id)

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Get current branch if not specified
    branch = body.branch
    if not branch:
        branch = await asyncio.to_thread(git_service.get_current_branch, working_dir)
        if not branch:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not determine current branch"
            )

    success, error_msg = await asyncio.to_thread(
        git_service.push,
        working_dir,
        remote=body.remote,
        branch=branch,
        set_upstream=body.set_upstream,
        force=body.force,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg or "Failed to push commits"
        )

    # Fetch inline git state for instant frontend update
    inline_status = await asyncio.to_thread(git_service.get_status, working_dir)

    return OperationResponse(
        success=True,
        message=f"Pushed to {body.remote}/{branch}",
        status=inline_status,
    )


# =============================================================================
# Pull Endpoint
# =============================================================================

@router.post("/pull", response_model=PullResponse)
async def pull_from_remote(
    request: Request,
    project_id: str,
    body: PullRequest,
    worktree_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Pull from remote (fetch + merge/rebase).

    Fetches from the remote, then merges or rebases. If merge conflicts are
    detected, the operation is auto-aborted and conflicted files are returned.

    Args:
        project_id: Project ID
        body: Pull request with remote and strategy
        worktree_id: Optional worktree ID
    """
    check_project_access(request, project_id)
    working_dir = get_working_dir(project_id, worktree_id)

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Check for uncommitted changes (allow if auto_stash is enabled)
    status_data = await asyncio.to_thread(git_service.get_status, working_dir)
    if not status_data["is_clean"] and not body.auto_stash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot pull: you have uncommitted changes. Commit or stash them first."
        )

    success, error_msg, conflicts = await asyncio.to_thread(
        git_service.pull,
        working_dir,
        remote=body.remote,
        strategy=body.strategy,
        auto_stash=body.auto_stash,
    )

    if not success:
        return PullResponse(
            success=False,
            message=error_msg or "Failed to pull",
            conflicts=conflicts,
            has_conflicts=len(conflicts) > 0
        )

    # Fetch inline git state for instant frontend update
    inline_status = await asyncio.to_thread(git_service.get_status, working_dir)
    inline_diff = await asyncio.to_thread(git_service.get_diff_stats, working_dir)

    return PullResponse(
        success=True,
        message=f"Pulled from {body.remote} using {body.strategy}",
        status=inline_status,
        diff_stats=inline_diff,
    )


# =============================================================================
# AI Generation Endpoints
# =============================================================================

@router.post("/generate-commit-message", response_model=GenerateCommitMessageResponse)
async def generate_commit_message(
    request: Request,
    project_id: str,
    body: GenerateCommitMessageRequest,
    worktree_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Generate a commit message using AI based on the current diff.

    When worktree_id is provided, generates message based on worktree's diff.
    """
    from app.api.generation import generate_with_claude

    check_project_access(request, project_id)
    working_dir = get_working_dir(project_id, worktree_id)

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    diff_text = await asyncio.to_thread(
        git_service.get_diff_text, working_dir, include_unstaged=body.include_unstaged
    )
    if not diff_text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No changes to generate a commit message for"
        )

    prompt = f"""Based on the following git diff, write a concise commit message.
Use conventional commit format (e.g., "feat:", "fix:", "refactor:", "docs:", "chore:", "perf:", "style:", "test:").
The first line should be under 72 characters.
If the change is complex, add a blank line followed by bullet points covering all significant changes.
Return ONLY the commit message text, nothing else.

<diff>
{diff_text}
</diff>"""

    # Use the Default AI profile's model (no explicit override - respects user config)
    response_text = await generate_with_claude(prompt)

    # Clean up: remove any markdown code blocks the model may have wrapped around the message
    message = response_text.strip()
    if message.startswith("```"):
        lines = message.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        message = "\n".join(lines).strip()

    return GenerateCommitMessageResponse(message=message)


@router.post("/generate-pr-description", response_model=GeneratePRDescriptionResponse)
async def generate_pr_description(
    request: Request,
    project_id: str,
    body: GeneratePRDescriptionRequest,
    worktree_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Generate a PR title and description using AI based on branch commits.

    When worktree_id is provided, generates description from worktree's branch.
    """
    import json as json_module
    from app.api.generation import generate_with_claude, extract_json_from_response

    check_project_access(request, project_id)
    working_dir = get_working_dir(project_id, worktree_id)

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    commit_log, diff_stat = await asyncio.to_thread(
        git_service.get_branch_log, working_dir, base_branch=body.base_branch
    )

    if not commit_log.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No commits found ahead of the base branch"
        )

    prompt = f"""Based on the following git commits and change summary, generate a pull request title and description.
Return a JSON object with "title" and "body" keys.

The title should be concise (under 72 characters) and descriptive.
The body should have a "## Summary" section with bullet points describing what changed and why. Include as many bullet points as needed to cover all significant changes.

<commits>
{commit_log}
</commits>

<changes>
{diff_stat}
</changes>

Return ONLY the JSON object, no markdown code blocks or explanation."""

    # Use the Default AI profile's model (no explicit override - respects user config)
    response_text = await generate_with_claude(prompt)

    try:
        parsed = extract_json_from_response(response_text)
        title = parsed.get("title", "").strip()
        pr_body = parsed.get("body", "").strip()
    except (ValueError, KeyError) as e:
        logger.warning(f"Failed to parse AI PR description, using fallback: {e}")
        # Fallback: use first commit message as title
        first_line = commit_log.split("\n")[0] if commit_log else "Update"
        # Remove short SHA prefix if present
        title = " ".join(first_line.split()[1:]) if " " in first_line else first_line
        pr_body = ""

    if not title:
        first_line = commit_log.split("\n")[0] if commit_log else "Update"
        title = " ".join(first_line.split()[1:]) if " " in first_line else first_line

    return GeneratePRDescriptionResponse(title=title, body=pr_body)


# =============================================================================
# Worktree Endpoints
# =============================================================================

def _convert_worktree_to_info(wt: dict) -> WorktreeInfo:
    """Convert worktree dict to WorktreeInfo model.

    Supports both new session relationship model (sessions list) and
    legacy single session model for backward compatibility.
    """
    # Convert sessions list (new model)
    sessions_list = []
    if wt.get("sessions"):
        for s in wt["sessions"]:
            sessions_list.append(SessionInfo(
                id=s["id"],
                title=s.get("title"),
                status=s["status"],
                updated_at=s.get("updated_at")
            ))

    # Active session (new model)
    active_session_info = None
    if wt.get("active_session"):
        as_data = wt["active_session"]
        active_session_info = SessionInfo(
            id=as_data["id"],
            title=as_data.get("title"),
            status=as_data["status"],
            updated_at=as_data.get("updated_at")
        )

    # Legacy single session (deprecated, for backward compat)
    legacy_session_info = None
    if wt.get("session"):
        legacy_session_info = SessionInfo(
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
        modified_list = gs.get("modified", [])
        untracked_list = gs.get("untracked", [])
        git_status_info = WorktreeGitStatus(
            is_clean=gs.get("is_clean", True),
            current_branch=gs.get("current_branch"),
            modified_count=len(modified_list),
            staged_count=len(staged_files),
            untracked_count=len(untracked_list),
            modified=modified_list,
            staged=staged_files,
            untracked=untracked_list
        )

    return WorktreeInfo(
        id=wt["id"],
        repository_id=wt["repository_id"],
        session_id=wt.get("session_id"),  # Deprecated
        branch_name=wt["branch_name"],
        worktree_path=wt["worktree_path"],
        base_branch=wt.get("base_branch"),
        status=wt["status"],
        created_at=wt["created_at"],
        # New model fields
        sessions=sessions_list,
        session_count=wt.get("session_count", len(sessions_list)),
        active_session=active_session_info,
        exists=wt.get("exists"),
        git_status=git_status_info,
        # Legacy field
        session=legacy_session_info
    )


@router.get("/worktrees", response_model=WorktreeListResponse)
async def list_worktrees(
    request: Request,
    project_id: str,
    include_status: bool = True,
    token: str = Depends(require_auth)
):
    """
    List all worktrees for a project.

    Returns worktrees with their associated session information.

    Query params:
        include_status: If True (default), includes git status (dirty state) for each worktree
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    worktrees = worktree_manager.get_worktrees_for_project(project_id)

    # Optionally fetch git status for each existing worktree (in parallel threads)
    if include_status:
        async def _fetch_wt_status(wt):
            if wt.get("exists", True):
                worktree_full_path = get_worktree_working_dir(wt)
                try:
                    wt["git_status"] = await asyncio.to_thread(
                        git_service.get_status, worktree_full_path
                    )
                except Exception as e:
                    logger.warning(f"Failed to get git status for worktree {wt['id']}: {e}")
                    wt["git_status"] = None

        await asyncio.gather(*[_fetch_wt_status(wt) for wt in worktrees])

    worktree_infos = [_convert_worktree_to_info(wt) for wt in worktrees]

    return WorktreeListResponse(
        worktrees=worktree_infos,
        total=len(worktree_infos)
    )


class WorktreeCreateStandalone(BaseModel):
    """Request to create a standalone worktree (no session)"""
    branch_name: str = Field(..., min_length=1, max_length=255)
    create_new_branch: bool = False
    base_branch: Optional[str] = None


def _copy_uncommitted_files(main_dir: str, worktree_path: str) -> dict:
    """
    Copy uncommitted files (modified and untracked) from main repo to worktree.

    This allows users to continue working on uncommitted changes in a worktree
    without having to commit or stash first.

    Args:
        main_dir: Path to the main repository
        worktree_path: Path to the worktree

    Returns:
        Dict with copied file counts and any errors
    """
    import shutil
    from pathlib import Path

    result = {
        "modified_copied": 0,
        "untracked_copied": 0,
        "errors": []
    }

    try:
        # Get git status to find uncommitted files
        status = git_service.get_status(main_dir)

        main_path = Path(main_dir)
        worktree_abs_path = Path(worktree_path)

        # Copy modified files (staged and unstaged)
        modified_files = status.get("modified", [])
        # Also include staged files that might be renamed or added
        staged_files = [s.get("file") for s in status.get("staged", []) if s.get("file")]
        all_modified = set(modified_files + staged_files)

        for file_path in all_modified:
            try:
                src = main_path / file_path
                dest = worktree_abs_path / file_path

                if src.exists():
                    # Ensure parent directory exists
                    dest.parent.mkdir(parents=True, exist_ok=True)

                    if src.is_file():
                        shutil.copy2(src, dest)
                        result["modified_copied"] += 1
                    elif src.is_dir():
                        if dest.exists():
                            shutil.rmtree(dest)
                        shutil.copytree(src, dest)
                        result["modified_copied"] += 1
            except Exception as e:
                result["errors"].append(f"Failed to copy modified file {file_path}: {str(e)}")

        # Copy untracked files
        untracked_files = status.get("untracked", [])
        for file_path in untracked_files:
            try:
                src = main_path / file_path
                dest = worktree_abs_path / file_path

                if src.exists():
                    # Ensure parent directory exists
                    dest.parent.mkdir(parents=True, exist_ok=True)

                    if src.is_file():
                        shutil.copy2(src, dest)
                        result["untracked_copied"] += 1
                    elif src.is_dir():
                        if dest.exists():
                            shutil.rmtree(dest)
                        shutil.copytree(src, dest)
                        result["untracked_copied"] += 1
            except Exception as e:
                result["errors"].append(f"Failed to copy untracked file {file_path}: {str(e)}")

        logger.debug(
            f"Copied uncommitted files to worktree: "
            f"{result['modified_copied']} modified, {result['untracked_copied']} untracked"
        )

    except Exception as e:
        result["errors"].append(f"Failed to get git status: {str(e)}")

    return result


@router.post("/worktrees", response_model=WorktreeCreateResponse)
async def create_worktree(
    request: Request,
    project_id: str,
    body: WorktreeCreate,
    token: str = Depends(require_auth)
):
    """
    Create a new worktree.

    If profile_id is provided, also creates an associated chat session (legacy mode).
    If profile_id is not provided, creates a standalone worktree that can be used
    with any session later.

    The new model allows worktrees to exist independently from sessions. Sessions
    can be linked to worktrees via the sessions.worktree_id field.

    Args:
        project_id: Project ID
        body: Worktree creation request
    """
    check_project_access(request, project_id)
    working_dir = get_project_path(project_id)

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
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

    session = None

    if body.profile_id:
        # Legacy mode: Create worktree and session together
        worktree, session = worktree_manager.create_worktree_session(
            project_id=project_id,
            branch_name=body.branch_name,
            create_new_branch=body.create_new_branch,
            base_branch=body.base_branch,
            profile_id=body.profile_id
        )

        if not worktree:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to create worktree. Branch may already be in use or doesn't exist."
            )
    else:
        # New mode: Create standalone worktree (no session)
        worktree = worktree_manager.create_worktree(
            project_id=project_id,
            branch_name=body.branch_name,
            create_new_branch=body.create_new_branch,
            base_branch=body.base_branch
        )

        if not worktree:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Failed to create worktree. Branch may already be in use or doesn't exist."
            )

    # Copy uncommitted files if requested
    copy_result = None
    if body.copy_uncommitted and worktree:
        worktree_abs_path = get_worktree_working_dir(worktree)
        copy_result = await asyncio.to_thread(
            _copy_uncommitted_files, working_dir, worktree_abs_path
        )

    # Get full worktree details for response
    worktree_details = worktree_manager.get_worktree_details(worktree["id"])

    # Build message
    message = f"Created worktree for branch '{body.branch_name}'"
    if copy_result:
        total_copied = copy_result["modified_copied"] + copy_result["untracked_copied"]
        if total_copied > 0:
            message += f" with {total_copied} uncommitted file(s) copied"

    # Surface any copy errors to the user
    copy_errors = copy_result.get("errors") if copy_result else None

    return WorktreeCreateResponse(
        success=True,
        message=message,
        worktree=_convert_worktree_to_info(worktree_details) if worktree_details else None,
        session=session,
        errors=copy_errors if copy_errors else None
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

    if not await asyncio.to_thread(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    result = await asyncio.to_thread(worktree_manager.sync_worktrees, project_id)

    return WorktreeSyncResponse(
        synced=result.get("synced", 0),
        orphaned=result.get("orphaned", 0),
        cleaned_up=result.get("cleaned_up", 0),
        imported=result.get("imported", 0),
        errors=result.get("errors", [])
    )


# =============================================================================
# Worktree File Browsing
# =============================================================================

class WorktreeFileInfo(BaseModel):
    """File information within a worktree"""
    name: str
    type: str  # 'file' or 'directory'
    size: Optional[int] = None
    sizeFormatted: Optional[str] = None
    path: str
    extension: Optional[str] = None
    modifiedAt: str
    createdAt: str


class WorktreeFilesResponse(BaseModel):
    """Response for worktree file listing"""
    files: List[WorktreeFileInfo]
    path: str
    worktreeId: str
    worktreePath: str


def _get_file_extension(name: str) -> str:
    """Get lowercase file extension without the dot"""
    from pathlib import Path as PathLib
    ext = PathLib(name).suffix.lower()
    return ext[1:] if ext else ""


def _format_file_size(size_bytes: int) -> str:
    """Format file size in human-readable format."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"


@router.get("/worktrees/{worktree_id}/files", response_model=WorktreeFilesResponse)
async def list_worktree_files(
    request: Request,
    project_id: str,
    worktree_id: str,
    path: str = "",
    show_hidden: bool = False,
    token: str = Depends(require_auth)
):
    """
    List files within a worktree directory.

    This allows browsing files in a worktree's working directory,
    which is separate from the main project directory.

    Args:
        project_id: Project ID
        worktree_id: Worktree ID to browse files in
        path: Relative path within the worktree (empty for root)
        show_hidden: Whether to show hidden files (starting with .)

    Returns:
        List of files in the specified directory within the worktree
    """
    from datetime import datetime

    check_project_access(request, project_id)

    # Get worktree details
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

    # Build full path to worktree directory
    worktree_base = Path(get_worktree_working_dir(worktree))

    if not worktree_base.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Worktree directory not found on disk"
        )

    # Build target path within worktree
    if path:
        # Validate path doesn't escape worktree boundary
        target_path = (worktree_base / path).resolve()
        try:
            target_path.relative_to(worktree_base.resolve())
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Path escapes worktree boundary"
            )
    else:
        target_path = worktree_base

    if not target_path.exists():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Directory not found"
        )

    if not target_path.is_dir():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Path is not a directory"
        )

    # List directory contents
    files = []
    for item in target_path.iterdir():
        # Skip hidden files unless requested
        if item.name.startswith('.') and not show_hidden:
            continue

        try:
            stat_info = item.stat()
            is_dir = item.is_dir()

            file_info = WorktreeFileInfo(
                name=item.name,
                type="directory" if is_dir else "file",
                size=stat_info.st_size if not is_dir else None,
                sizeFormatted=_format_file_size(stat_info.st_size) if not is_dir else None,
                path=str(item.relative_to(worktree_base)),
                extension=_get_file_extension(item.name) if not is_dir else None,
                modifiedAt=datetime.fromtimestamp(stat_info.st_mtime).isoformat(),
                createdAt=datetime.fromtimestamp(stat_info.st_ctime).isoformat(),
            )
            files.append(file_info)
        except (OSError, PermissionError):
            # Skip files we can't access
            continue

    # Sort: directories first, then files alphabetically
    files.sort(key=lambda x: (x.type != "directory", x.name.lower()))

    return WorktreeFilesResponse(
        files=files,
        path=path,
        worktreeId=worktree_id,
        worktreePath=worktree["worktree_path"]
    )
