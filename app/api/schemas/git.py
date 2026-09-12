"""
Pydantic request/response models for the Git management API.

Extracted from app.api.git to keep route handlers and data models separate.
"""

from datetime import datetime
from typing import Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_serializer


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
    auto_stash: bool = Field(default=False, description="Auto-stash uncommitted changes before checkout instead of failing with 409")


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
    # True when the branch carries real content over its base branch (a
    # three-dot base...HEAD diff is non-empty). Unlike `ahead` (a raw commit
    # count), this is False for branches whose commits net to no change — so the
    # UI can hide "Ship It" when there is nothing left to ship.
    differs_from_base: bool = False
    matches_base_tree: Optional[bool] = None
    # Commits on HEAD that origin/<branch> does not have. None when the branch
    # has no remote counterpart yet (never pushed). Distinct from `ahead`, which
    # counts commits vs the default branch — a brand-new worktree is ahead=0
    # and unpushed=None; a fully-pushed feature branch is ahead>0 and unpushed=0.
    unpushed: Optional[int] = None
    conflicts: List[str] = []
    # Repository info from database
    repository_id: Optional[str] = None
    default_branch: Optional[str] = None
    github_repo_name: Optional[str] = None
    # DB returns datetime; accept both and serialize to ISO string.
    last_synced_at: Optional[Union[str, datetime]] = None

    @field_serializer('last_synced_at')
    def _serialize_last_synced_at(self, v: Optional[Union[str, datetime]]) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.isoformat()
        return v


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


class OpenPrInfo(BaseModel):
    """The open pull request whose head is the current branch."""
    number: int
    title: str
    url: str
    draft: bool = False
    state: Optional[str] = None
    # GitHub computes this lazily; None means "unknown yet" — the UI must NOT
    # treat an unknown-mergeability PR as unmergeable.
    mergeable: Optional[bool] = None


class PrStateResponse(BaseModel):
    """Response for the pr-state endpoint (PR lifecycle for the current branch).

    ``open_pr is None`` is only meaningful when ``pr_lookup_failed`` is False.
    The two flags together give the client a tri-state instead of the old
    lie-by-omission where an unreachable ``gh`` looked exactly like "this branch
    has no pull request" (finding G2):

    * ``pr_lookup_failed=False`` — the answer is authoritative.
    * ``pr_lookup_failed=True``, ``stale=True`` — the refresh failed and
      ``open_pr`` is the last known-good value (stale-while-revalidate). Render
      it, optionally marked stale; do NOT fall back to "Create PR".
    * ``pr_lookup_failed=True``, ``stale=False`` — nothing is known. Keep
      whatever the client last rendered; do NOT render "no PR exists".
    """
    current_branch: Optional[str] = None
    has_github: bool = False
    open_pr: Optional[OpenPrInfo] = None
    # Original head of the latest merged PR, not the squash commit on the base.
    merged_head_sha: Optional[str] = None
    # True when GitHub could not be asked this cycle (gh offline, rate-limited,
    # timed out). Never cached as a negative result.
    pr_lookup_failed: bool = False
    # True when open_pr is a previously known-good value served past its TTL
    # because the refresh failed.
    stale: bool = False


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
# Changed Files / Diff Models
# =============================================================================

class FileChangeInfo(BaseModel):
    """Per-file change information for the changed files panel."""
    file: str
    status: str  # A=added, M=modified, D=deleted, R=renamed, C=copied, ?=untracked
    category: str  # 'staged', 'modified', 'untracked', 'conflict'
    additions: int = 0
    deletions: int = 0
    is_binary: bool = False


class ChangedFilesResponse(BaseModel):
    """All changed files with per-file stats."""
    files: List[FileChangeInfo]
    total_additions: int = 0
    total_deletions: int = 0


class FileDiffRequest(BaseModel):
    """Request for per-file diff."""
    file_path: str = Field(..., min_length=1, max_length=1000)
    staged: bool = False


class FileDiffResponse(BaseModel):
    """Response with unified diff content for a single file."""
    file: str
    diff: str
    is_binary: bool = False
    truncated: bool = False


class StageRequest(BaseModel):
    """Request to stage files."""
    files: List[str] = Field(..., min_length=1)


class UnstageRequest(BaseModel):
    """Request to unstage files."""
    files: List[str] = Field(..., min_length=1)


class DiscardRequest(BaseModel):
    """Request to discard changes."""
    files: List[str] = Field(..., min_length=1)
    file_statuses: Dict[str, str] = Field(
        default_factory=dict,
        description="Map of file path to category (modified/staged/untracked)"
    )


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
    # DB returns datetime, legacy callers pass str. Accept both and serialize to ISO string.
    updated_at: Optional[Union[str, datetime]] = None

    @field_serializer('updated_at')
    def _serialize_updated_at(self, v: Optional[Union[str, datetime]]) -> Optional[str]:
        if v is None:
            return None
        if isinstance(v, datetime):
            return v.isoformat()
        return v


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
    # DB returns datetime; accept both and serialize to ISO string.
    created_at: Union[str, datetime]

    @field_serializer('created_at')
    def _serialize_created_at(self, v: Union[str, datetime]) -> str:
        if isinstance(v, datetime):
            return v.isoformat()
        return v
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
# Branch Name Generation Models
# =============================================================================

class GenerateBranchNameRequest(BaseModel):
    """Request to generate a branch name from a user message"""
    message: str = Field(..., min_length=1, max_length=2000)
    prefix: Optional[str] = Field(default=None, description="Force a specific branch prefix (feat, fix, chore, etc.). If omitted, AI chooses the prefix based on message intent.")

class GenerateBranchNameResponse(BaseModel):
    """Generated branch name"""
    branch_name: str
    display_name: str  # Human-readable version


# =============================================================================
# Upload Migration Models
# =============================================================================

class MigrateUploadsRequest(BaseModel):
    """Request to copy pending uploads from main project to worktree"""
    worktree_id: str
    file_paths: List[str] = Field(..., description="Absolute paths of files to copy to worktree")

class MigrateUploadsResponse(BaseModel):
    """Result of upload migration"""
    migrated: List[Dict[str, str]]  # List of {original_path, new_path, relative_path}
    errors: List[str]


# =============================================================================
# Standalone Worktree Models
# =============================================================================

class WorktreeCreateStandalone(BaseModel):
    """Request to create a standalone worktree (no session)"""
    branch_name: str = Field(..., min_length=1, max_length=255)
    create_new_branch: bool = False
    base_branch: Optional[str] = None


# =============================================================================
# Worktree File Browser Models
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
