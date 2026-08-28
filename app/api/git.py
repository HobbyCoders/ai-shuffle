"""
Git management API routes

Provides endpoints for git repository operations including:
- Repository status and diff stats
- Branch management (list, create, delete, checkout)
- Worktree management (list, create, delete, file browsing)
- Commit, push, and commit graph visualization
- AI-powered commit message and PR description generation
- Fetch from remote

Sub-modules:
- git_helpers: Thread pool, request coalescing, path utilities
- git_worktrees: Worktree CRUD and file browsing endpoints
- git_ai: AI-powered commit message, PR description, and branch name generation
"""

import asyncio
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Depends, Request, status

from app.core.config import settings
from app.core.git_service import git_service
from app.core.worktree_manager import worktree_manager
from app.core.project_utils import get_project_working_dir, get_worktree_working_dir
from app.db import database
from app.db.async_database import run_sync
from app.db.base import WorktreeStatus
from app.api.auth import require_auth, get_api_user_from_request
from app.api.common import check_project_access, resolve_project_path

# Import from sub-modules
from app.api.git_helpers import (  # noqa: F401
    _git_executor,
    run_git,
    _RequestCoalescer,
    _coalescer,
    get_project_path,
    get_working_dir,
    get_working_dir_async,
    stage_and_commit,
    _repo_record_cache,
    _REPO_RECORD_TTL,
    ensure_git_repository_record,
    peek_repo_record_cache,
    _CACHE_MISS,
)

from app.api.git_worktrees import (  # noqa: F401
    _convert_worktree_to_info,
    _copy_uncommitted_files,
    _get_file_extension,
    _format_file_size,
)

from app.api.git_ai import (  # noqa: F401
    STOP_WORDS,
    FIX_KEYWORDS,
    CHORE_KEYWORDS,
    DOCS_KEYWORDS,
    REFACTOR_KEYWORDS,
    TEST_KEYWORDS,
    infer_prefix,
    slugify_branch,
)

# Import sub-routers
from app.api import git_worktrees as _git_worktrees_mod
from app.api import git_ai as _git_ai_mod

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/projects/{project_id}/git", tags=["Git"])

# Include sub-routers (no extra prefix — they share the same base path)
router.include_router(_git_worktrees_mod.router)
router.include_router(_git_ai_mod.router)


# =============================================================================
# Request/Response Models (extracted to app.api.schemas.git)
# =============================================================================

from app.api.schemas.git import (  # noqa: E402
    BranchInfo,
    BranchListResponse,
    CreateBranchRequest,
    CheckoutRequest,
    CommitInfo,
    CommitGraphResponse,
    StagedFile,
    GitStatusResponse,
    FetchResponse,
    OperationResponse,
    DiffStatsResponse,
    CommitRequest,
    CommitResponse,
    PushRequest,
    PullRequest,
    PullResponse,
    InitRepoRequest,
    InitRepoResponse,
    CloneRepoRequest,
    CloneRepoResponse,
    GenerateCommitMessageRequest,
    GenerateCommitMessageResponse,
    GeneratePRDescriptionRequest,
    GeneratePRDescriptionResponse,
    FileChangeInfo,
    ChangedFilesResponse,
    FileDiffRequest,
    FileDiffResponse,
    StageRequest,
    UnstageRequest,
    DiscardRequest,
    WorktreeCreate,
    SessionInfo,
    WorktreeGitStatus,
    WorktreeInfo,
    WorktreeListResponse,
    WorktreeCreateResponse,
    WorktreeSyncResponse,
    GenerateBranchNameRequest,
    GenerateBranchNameResponse,
    MigrateUploadsRequest,
    MigrateUploadsResponse,
    WorktreeCreateStandalone,
    WorktreeFileInfo,
    WorktreeFilesResponse,
)


# =============================================================================
# Endpoints
# =============================================================================

@router.get("/status", response_model=GitStatusResponse)
async def get_git_status(
    request: Request,
    project_id: str,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Get git repository status for a project or worktree.

    Returns current branch, uncommitted changes, and sync status with remote.
    When worktree_id is provided, returns status for the worktree directory.
    """
    check_project_access(request, project_id)

    # During startup, the project record may not exist yet (race between
    # frontend polling and project creation).  Return an empty status
    # instead of 404 so the poller can retry silently.
    if worktree_id:
        working_dir = await get_working_dir_async(project_id, worktree_id, session_id)
    else:
        working_dir = get_project_path(project_id, allow_missing=True)
        if working_dir is None:
            return GitStatusResponse(is_git_repo=False)

    # Coalesced: concurrent polls for the same dir share one in-flight call
    status_data = await _coalescer.get_or_run(
        f"status:{working_dir}", git_service.get_status, working_dir
    )

    # Convert staged files to proper format
    staged_files = [
        StagedFile(file=s["file"], status=s["status"])
        for s in status_data.get("staged", [])
    ]

    # Ensure repository record exists and get additional info.
    # The record is cached (60s TTL); check the cache FIRST so a hit never
    # submits a GitPool job. /git/status is polled every ~2s per active session,
    # so submitting on every poll (even a cache hit) needlessly burned a queue
    # slot on the 4-worker pool. Only a genuine cache miss touches the executor.
    repo = peek_repo_record_cache(project_id)
    if repo is _CACHE_MISS:
        repo = await run_git(ensure_git_repository_record, project_id, working_dir)

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
        differs_from_base=status_data.get("differs_from_base", False),
        conflicts=status_data.get("conflicts", []),
        repository_id=repo["id"] if repo else None,
        default_branch=repo["default_branch"] if repo else None,
        github_repo_name=repo["github_repo_name"] if repo else None,
        last_synced_at=str(repo["last_synced_at"]) if repo and repo["last_synced_at"] else None
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
    if await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is already a git repository"
        )

    # Step 1: git init
    success = await run_git(
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
        staged = await run_git(git_service.stage_files, working_dir, ["."])
        if not staged:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Failed to stage files for initial commit"
            )
        committed, commit_err = await run_git(
            git_service.commit, working_dir, "Initial commit"
        )
        if not committed:
            # Surface git identity errors clearly — common in container deployments
            detail = f"Failed to create initial commit: {commit_err or 'unknown error'}"
            if commit_err and "identity unknown" in commit_err.lower():
                detail += (
                    ". Configure git identity with "
                    "'git config --global user.email \"you@example.com\"' and "
                    "'git config --global user.name \"Your Name\"'."
                )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=detail
            )

    # Step 3: Optional GitHub repo creation
    if body.create_github_repo and body.github_repo_name:
        from app.core.github_service import github_service

        if not await asyncio.to_thread(github_service.is_authenticated):
            raise HTTPException(
                status_code=400,
                detail="GitHub CLI is not authenticated. Run 'gh auth login' in your terminal first."
            )

        repo_info = await run_git(
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
        result = await run_git(
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
            push_success, push_error = await run_git(
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
    repo = await run_git(ensure_git_repository_record, project_id, working_dir)

    # Update record with GitHub info if we created a repo
    if github_full_name and repo:
        remote_url = await run_git(git_service.get_remote_url, working_dir)
        await run_sync(
            database.update_git_repository,
            repo["id"],
            remote_url=remote_url,
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
    if await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is already a git repository"
        )

    # Ensure directory exists
    working_path = Path(working_dir)
    working_path.mkdir(parents=True, exist_ok=True)

    # Clone
    success, error = await run_git(
        git_service.clone_repo, working_dir, body.clone_url
    )
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to clone repository: {error}"
        )

    # Create database record
    repo = await run_git(ensure_git_repository_record, project_id, working_dir)

    # Update record with remote info from cloned repo
    if repo:
        remote_url = await run_git(git_service.get_remote_url, working_dir)
        current_branch = await run_git(git_service.get_current_branch, working_dir)
        await run_sync(
            database.update_git_repository,
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
    session_id: Optional[str] = None,
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
    working_dir = await get_working_dir_async(project_id, worktree_id, session_id)

    if not await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Get branches directly from git CLI
    branches_list = await run_git(git_service.list_branches, working_dir, include_remote=include_remote)
    current_branch = await run_git(git_service.get_current_branch, working_dir)
    branch_data = {"branches": branches_list, "current_branch": current_branch}

    # Enrich with worktree associations from database. The repo lookup and its
    # worktree fan-out are a contiguous read pair — one hop, not two.
    def _worktree_branch_map() -> dict[str, str]:
        """branch_name -> worktree_id for this project's repository."""
        mapping: dict[str, str] = {}
        repo = database.get_git_repository_by_project(project_id)
        if repo:
            for wt in database.get_worktrees_for_repository(repo["id"]):
                if wt.get("branch_name"):
                    mapping[wt["branch_name"]] = wt["id"]
        return mapping

    worktree_branch_map = await run_sync(_worktree_branch_map)

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

    if not await run_git(git_service.is_git_repo, working_dir):
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

    success = await run_git(
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

    if not await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Don't allow deleting current branch
    current = await run_git(git_service.get_current_branch, working_dir)
    if current == branch_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete the currently checked out branch"
        )

    success = await run_git(
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
    session_id: Optional[str] = None,
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

    if not await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Check for uncommitted changes (untracked files don't block checkout).
    # When auto_stash is requested, stash them before checking out instead of
    # raising a 409 (mirrors the pull endpoint's auto_stash behavior).
    status_data = await run_git(git_service.get_status, working_dir)
    has_uncommitted = (
        status_data.get("staged") or
        status_data.get("modified") or
        status_data.get("conflicts")
    )
    if has_uncommitted:
        if not body.auto_stash:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot checkout: you have uncommitted changes. Commit or stash them first."
            )
        stashed = await run_git(
            git_service.stash_push, working_dir, f"auto-stash before checkout {body.ref}"
        )
        if not stashed:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot checkout: failed to stash your uncommitted changes."
            )

    success = await run_git(git_service.checkout, working_dir, body.ref)

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

    message = (
        f"Stashed changes and checked out '{body.ref}'"
        if (has_uncommitted and body.auto_stash)
        else f"Checked out '{body.ref}'"
    )
    return OperationResponse(
        success=True,
        message=message
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

    if not await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Cap limit to prevent excessive data
    limit = min(limit, 500)

    commits = await run_git(
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
    session_id: Optional[str] = None,
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
    working_dir = await get_working_dir_async(project_id, worktree_id, session_id)

    if not await run_git(git_service.is_git_repo, working_dir):
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

    success = await run_git(git_service.fetch, working_dir, remote=remote)

    # Update last_synced_at if we have a repository record.
    # Read-decide-write kept atomic in ONE hop: the lookup and the stamp must
    # not be split, or a repo record deleted between them would be re-stamped
    # (or the write aimed at a row that no longer exists).
    if success:
        def _stamp_synced():
            repo = database.get_git_repository_by_project(project_id)
            if repo:
                database.update_git_repository_synced(repo["id"])

        await run_sync(_stamp_synced)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch from remote '{remote}'. Check network connection and authentication."
        )

    # Fetch inline git state for instant frontend update
    inline_status = await run_git(git_service.get_status, working_dir)

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
    session_id: Optional[str] = None,
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

    # Gracefully handle missing project during startup race
    if worktree_id or session_id:
        working_dir = await get_working_dir_async(project_id, worktree_id, session_id)
    else:
        working_dir = get_project_path(project_id, allow_missing=True)
        if working_dir is None:
            return DiffStatsResponse(additions=0, deletions=0, files_changed=0)

    # get_diff_stats handles non-git-repo gracefully (returns zeros),
    # so skip the separate is_git_repo check to avoid an extra subprocess call.
    # Coalesced: concurrent polls share one in-flight call.
    stats = await _coalescer.get_or_run(
        f"diff_stats:{working_dir}:{include_unstaged}",
        git_service.get_diff_stats, working_dir, include_unstaged,
    )

    return DiffStatsResponse(
        additions=stats.get("additions", 0),
        deletions=stats.get("deletions", 0),
        files_changed=stats.get("files_changed", 0),
    )


# =============================================================================
# Changed Files & File-Level Operations
# =============================================================================

@router.get("/changed-files", response_model=ChangedFilesResponse)
async def get_changed_files(
    request: Request,
    project_id: str,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
    token: str = Depends(require_auth),
):
    """
    Get all changed files with per-file diff stats.

    Returns files grouped by category (staged, modified, untracked, conflict)
    with per-file addition/deletion counts for display in the changed files panel.
    """
    check_project_access(request, project_id)
    working_dir = await get_working_dir_async(project_id, worktree_id, session_id)

    # Fetch status and per-file stats in parallel via the thread pool
    git_status, per_file_stats = await asyncio.gather(
        _coalescer.get_or_run(
            f"status:{working_dir}",
            git_service.get_status, working_dir,
        ),
        _coalescer.get_or_run(
            f"per_file_diff:{working_dir}",
            git_service.get_per_file_diff_stats, working_dir,
        ),
    )

    if not git_status.get("is_git_repo"):
        return ChangedFilesResponse(files=[])

    # Build a lookup from per-file stats: file -> {additions, deletions, is_binary}
    stats_by_file: Dict[str, Dict[str, Any]] = {}
    for entry in per_file_stats:
        fname = entry["file"]
        # Merge staged and unstaged stats for the same file
        if fname in stats_by_file:
            stats_by_file[fname]["additions"] += entry["additions"]
            stats_by_file[fname]["deletions"] += entry["deletions"]
            stats_by_file[fname]["is_binary"] = (
                stats_by_file[fname]["is_binary"] or entry["is_binary"]
            )
        else:
            stats_by_file[fname] = {
                "additions": entry["additions"],
                "deletions": entry["deletions"],
                "is_binary": entry["is_binary"],
            }

    files: List[FileChangeInfo] = []
    total_add = 0
    total_del = 0

    # Conflicts
    for fname in git_status.get("conflicts", []):
        fstats = stats_by_file.get(fname, {})
        add = fstats.get("additions", 0)
        dele = fstats.get("deletions", 0)
        total_add += add
        total_del += dele
        files.append(FileChangeInfo(
            file=fname, status="U", category="conflict",
            additions=add, deletions=dele,
            is_binary=fstats.get("is_binary", False),
        ))

    # Staged files
    for entry in git_status.get("staged", []):
        fname = entry["file"] if isinstance(entry, dict) else entry
        file_status = entry.get("status", "M") if isinstance(entry, dict) else "M"
        fstats = stats_by_file.get(fname, {})
        add = fstats.get("additions", 0)
        dele = fstats.get("deletions", 0)
        total_add += add
        total_del += dele
        files.append(FileChangeInfo(
            file=fname, status=file_status, category="staged",
            additions=add, deletions=dele,
            is_binary=fstats.get("is_binary", False),
        ))

    # Modified (unstaged)
    for fname in git_status.get("modified", []):
        fstats = stats_by_file.get(fname, {})
        add = fstats.get("additions", 0)
        dele = fstats.get("deletions", 0)
        total_add += add
        total_del += dele
        files.append(FileChangeInfo(
            file=fname, status="M", category="modified",
            additions=add, deletions=dele,
            is_binary=fstats.get("is_binary", False),
        ))

    # Untracked
    for fname in git_status.get("untracked", []):
        files.append(FileChangeInfo(
            file=fname, status="?", category="untracked",
            additions=0, deletions=0, is_binary=False,
        ))

    return ChangedFilesResponse(
        files=files,
        total_additions=total_add,
        total_deletions=total_del,
    )


@router.post("/file-diff", response_model=FileDiffResponse)
async def get_file_diff(
    request: Request,
    project_id: str,
    body: FileDiffRequest,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
    token: str = Depends(require_auth),
):
    """
    Get unified diff for a single file.

    Uses POST to avoid URL-encoding issues with file paths.
    Fetches lazily (on user click), not pre-loaded.
    """
    check_project_access(request, project_id)
    working_dir = await get_working_dir_async(project_id, worktree_id, session_id)

    result = await run_git(
        git_service.get_file_diff,
        working_dir, body.file_path, body.staged,
    )

    return FileDiffResponse(
        file=result["file"],
        diff=result["diff"],
        is_binary=result.get("is_binary", False),
        truncated=result.get("truncated", False),
    )


@router.post("/stage", response_model=OperationResponse)
async def stage_files(
    request: Request,
    project_id: str,
    body: StageRequest,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
    token: str = Depends(require_auth),
):
    """Stage specific files for commit."""
    check_project_access(request, project_id)
    working_dir = await get_working_dir_async(project_id, worktree_id, session_id)

    success = await run_git(git_service.stage_files, working_dir, body.files)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to stage files",
        )

    # Return fresh status + diff stats for instant UI update
    new_status = await run_git(git_service.get_status, working_dir)
    new_diff_stats = await run_git(git_service.get_diff_stats, working_dir)

    return OperationResponse(
        success=True,
        message=f"Staged {len(body.files)} file(s)",
        status=new_status,
        diff_stats=new_diff_stats,
    )


@router.post("/unstage", response_model=OperationResponse)
async def unstage_files(
    request: Request,
    project_id: str,
    body: UnstageRequest,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
    token: str = Depends(require_auth),
):
    """Unstage specific files (remove from index, keep working tree changes)."""
    check_project_access(request, project_id)
    working_dir = await get_working_dir_async(project_id, worktree_id, session_id)

    success = await run_git(git_service.unstage_files, working_dir, body.files)

    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to unstage files",
        )

    new_status = await run_git(git_service.get_status, working_dir)
    new_diff_stats = await run_git(git_service.get_diff_stats, working_dir)

    return OperationResponse(
        success=True,
        message=f"Unstaged {len(body.files)} file(s)",
        status=new_status,
        diff_stats=new_diff_stats,
    )


@router.post("/discard", response_model=OperationResponse)
async def discard_changes(
    request: Request,
    project_id: str,
    body: DiscardRequest,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
    token: str = Depends(require_auth),
):
    """
    Discard changes for specific files.

    Routes to appropriate git command based on file status:
    - modified: git checkout -- <file>
    - staged: git reset + git checkout
    - untracked: delete from disk
    """
    check_project_access(request, project_id)
    working_dir = await get_working_dir_async(project_id, worktree_id, session_id)

    success, failed = await run_git(
        git_service.discard_changes,
        working_dir, body.files, body.file_statuses,
    )

    if not success:
        msg = f"Failed to discard some files: {', '.join(failed)}"
    else:
        msg = f"Discarded changes in {len(body.files)} file(s)"

    new_status = await run_git(git_service.get_status, working_dir)
    new_diff_stats = await run_git(git_service.get_diff_stats, working_dir)

    return OperationResponse(
        success=success,
        message=msg,
        status=new_status,
        diff_stats=new_diff_stats,
    )


@router.post("/commit", response_model=CommitResponse)
async def create_commit(
    request: Request,
    project_id: str,
    body: CommitRequest,
    worktree_id: Optional[str] = None,
    session_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Create a commit.

    Optionally stages all changes (including unstaged) before committing.
    When worktree_id is provided, commits in the worktree directory.
    """
    t_start = time.perf_counter()
    check_project_access(request, project_id)
    working_dir = await get_working_dir_async(project_id, worktree_id, session_id)

    if not await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Stage (optionally) and commit via the shared helper so this endpoint and
    # the Ship-It flow enforce identical staging / "no staged changes" rules.
    t_commit_start = time.perf_counter()
    success, sha_or_error, reason = await stage_and_commit(
        working_dir, body.message, body.include_unstaged
    )
    t_commit_ms = (time.perf_counter() - t_commit_start) * 1000

    if not success:
        if reason == "stage_failed":
            detail = "Failed to stage files"
        elif reason == "no_staged":
            detail = "No staged changes to commit"
        else:
            detail = f"Failed to create commit: {sha_or_error or 'unknown error'}"
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)

    commit_sha = sha_or_error

    # Fetch inline git state for instant frontend update
    t_refresh_start = time.perf_counter()
    inline_status = await run_git(git_service.get_status, working_dir)
    inline_diff = await run_git(git_service.get_diff_stats, working_dir)
    t_refresh_ms = (time.perf_counter() - t_refresh_start) * 1000

    t_total_ms = (time.perf_counter() - t_start) * 1000
    logger.info(
        f"[ship-it-timing] commit project={project_id} "
        f"total={t_total_ms:.0f}ms stage_commit={t_commit_ms:.0f}ms "
        f"refresh={t_refresh_ms:.0f}ms"
    )

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
    session_id: Optional[str] = None,
    token: str = Depends(require_auth)
):
    """
    Push commits to remote.

    When worktree_id is provided, pushes from the worktree directory.
    """
    t_start = time.perf_counter()
    check_project_access(request, project_id)
    working_dir = await get_working_dir_async(project_id, worktree_id, session_id)

    if not await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Get current branch if not specified
    branch = body.branch
    if not branch:
        branch = await run_git(git_service.get_current_branch, working_dir)
        if not branch:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Could not determine current branch"
            )

    t_push_start = time.perf_counter()
    success, error_msg = await run_git(
        git_service.push,
        working_dir,
        remote=body.remote,
        branch=branch,
        set_upstream=body.set_upstream,
        force=body.force,
    )
    t_push_ms = (time.perf_counter() - t_push_start) * 1000

    if not success:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=error_msg or "Failed to push commits"
        )

    # Fetch inline git state for instant frontend update
    t_refresh_start = time.perf_counter()
    inline_status = await run_git(git_service.get_status, working_dir)
    t_refresh_ms = (time.perf_counter() - t_refresh_start) * 1000

    t_total_ms = (time.perf_counter() - t_start) * 1000
    logger.info(
        f"[ship-it-timing] push project={project_id} branch={branch} "
        f"total={t_total_ms:.0f}ms git_push={t_push_ms:.0f}ms "
        f"refresh={t_refresh_ms:.0f}ms"
    )

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
    session_id: Optional[str] = None,
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
    working_dir = await get_working_dir_async(project_id, worktree_id, session_id)

    if not await run_git(git_service.is_git_repo, working_dir):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Project is not a git repository"
        )

    # Check for uncommitted changes (allow if auto_stash is enabled)
    status_data = await run_git(git_service.get_status, working_dir)
    if not status_data["is_clean"] and not body.auto_stash:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot pull: you have uncommitted changes. Commit or stash them first."
        )

    success, error_msg, conflicts = await run_git(
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
    inline_status = await run_git(git_service.get_status, working_dir)
    inline_diff = await run_git(git_service.get_diff_stats, working_dir)

    return PullResponse(
        success=True,
        message=f"Pulled from {body.remote} using {body.strategy}",
        status=inline_status,
        diff_stats=inline_diff,
    )


# =============================================================================
# Pending Upload Migration
# =============================================================================

@router.post("/migrate-uploads")
async def migrate_uploads_to_worktree(
    request: Request,
    project_id: str,
    body: MigrateUploadsRequest,
    token: str = Depends(require_auth)
):
    """
    Copy uploaded files from the main project to a worktree directory.

    Used when files were uploaded before a worktree existed (auto-worktree flow).
    """
    import shutil

    check_project_access(request, project_id)

    def _load_project_and_worktree():
        """Both existence probes in ONE hop, same order, same short-circuit:
        an unknown project still 404s before the worktree row is ever read.
        The HTTPExceptions propagate out of ``run_sync`` unchanged."""
        project = database.get_project(project_id)
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")

        worktree = database.get_worktree(body.worktree_id)
        if not worktree:
            raise HTTPException(status_code=404, detail="Worktree not found")

        return project, worktree

    project, worktree = await run_sync(_load_project_and_worktree)

    worktree_dir = Path(get_worktree_working_dir(worktree))
    if not worktree_dir.exists():
        raise HTTPException(status_code=400, detail="Worktree directory does not exist")

    project_dir = Path(get_project_working_dir(project_id))
    migrated = []
    errors = []

    for file_path in body.file_paths:
        try:
            src = Path(file_path)
            if not src.exists():
                errors.append(f"File not found: {file_path}")
                continue

            # Ensure the file is within the project directory
            try:
                rel = src.relative_to(project_dir)
            except ValueError:
                errors.append(f"File not in project directory: {file_path}")
                continue

            dest = worktree_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dest))

            migrated.append({
                "original_path": str(src),
                "new_path": str(dest),
                "relative_path": rel.as_posix()
            })
        except Exception as e:
            errors.append(f"Failed to copy {file_path}: {str(e)}")

    return MigrateUploadsResponse(migrated=migrated, errors=errors)
