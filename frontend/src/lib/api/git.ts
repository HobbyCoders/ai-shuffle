/**
 * Git API client for AI Hub
 */

import { api } from './client';

const BASE = '/projects';

// ============================================================================
// Git Status Types
// ============================================================================

export interface GitStatus {
    is_git_repo: boolean;
    current_branch: string | null;
    remote_url: string | null;
    has_changes: boolean;
    staged: number;
    modified: number;
    untracked: number;
    ahead: number;
    behind: number;
}

// ============================================================================
// Branch Types
// ============================================================================

// Raw branch data from API (snake_case)
interface RawBranch {
    name: string;
    is_current: boolean;
    is_remote: boolean;
    upstream?: string;
    ahead?: number;
    behind?: number;
    commit?: string;
    commit_message?: string;
    has_worktree?: boolean;
    worktree_id?: string;
}

// Transformed branch for frontend (camelCase)
export interface Branch {
    name: string;
    current: boolean;
    remote: boolean;
    upstream?: string;
    ahead?: number;
    behind?: number;
    lastCommit?: string;
    // Worktree association
    hasWorktree?: boolean;
    worktreeId?: string;
}

// Transform raw branch to frontend format
function transformBranch(raw: RawBranch): Branch {
    return {
        name: raw.name,
        current: raw.is_current,
        remote: raw.is_remote,
        upstream: raw.upstream,
        ahead: raw.ahead,
        behind: raw.behind,
        lastCommit: raw.commit_message,
        hasWorktree: raw.has_worktree,
        worktreeId: raw.worktree_id
    };
}

export interface CreateBranchRequest {
    name: string;
    start_point?: string;
}

// ============================================================================
// Commit Types
// ============================================================================

export interface CommitNode {
    sha: string;
    shortSha: string;
    message: string;
    author: string;
    email: string;
    date: string;
    parents: string[];
    branches: string[];
    tags: string[];
    x?: number;  // Calculated for graph
    y?: number;
}

// Raw commit from API (different field names)
interface RawCommitNode {
    sha: string;
    short_sha: string;
    message: string;
    author: string;
    author_email: string;
    timestamp: string;
    parents: string[];
    refs: string[];
}

// Transform raw commit to CommitNode
function transformCommit(raw: RawCommitNode): CommitNode {
    // Parse refs into branches and tags
    const branches: string[] = [];
    const tags: string[] = [];

    for (const ref of raw.refs || []) {
        // Skip HEAD pointer references like "HEAD -> main"
        const cleanRef = ref.replace(/^HEAD -> /, '');

        if (ref.startsWith('tag: ')) {
            tags.push(ref.replace('tag: ', ''));
        } else if (cleanRef.startsWith('origin/')) {
            // Skip remote refs for display (or include them if desired)
            // branches.push(cleanRef);
        } else if (cleanRef && cleanRef !== 'HEAD') {
            branches.push(cleanRef);
        }
    }

    return {
        sha: raw.sha,
        shortSha: raw.short_sha,
        message: raw.message,
        author: raw.author,
        email: raw.author_email,
        date: raw.timestamp,
        parents: raw.parents || [],
        branches,
        tags
    };
}

export interface CommitGraphResponse {
    commits: CommitNode[];
    total: number;
}

// ============================================================================
// Worktree Types
// ============================================================================

export interface StagedFile {
    file: string;
    status: string;  // A=added, M=modified, D=deleted, R=renamed, C=copied
}

export interface WorktreeGitStatus {
    is_clean: boolean;
    current_branch: string | null;
    // File counts for quick display (e.g., "3 modified")
    modified_count: number;
    staged_count: number;
    untracked_count: number;
    // Full file lists (may be empty for list endpoint)
    modified: string[];
    staged: StagedFile[];
    untracked: string[];
}

export interface Worktree {
    id: string;
    repository_id: string;
    session_id: string | null;
    branch_name: string;
    worktree_path: string;
    base_branch: string | null;
    status: string;
    created_at: string;
    // New fields from worktree-session decoupling
    exists?: boolean;  // Whether worktree folder exists on disk
    session_count?: number;  // Number of sessions that have used this worktree
    // Git status (dirty state)
    git_status?: WorktreeGitStatus;
}

export interface CreateWorktreeRequest {
    branch_name: string;
    create_new_branch?: boolean;
    base_branch?: string;
    profile_id?: string;
    copy_uncommitted?: boolean;  // Copy uncommitted files from main repo to worktree
}

export interface CreateWorktreeResponse {
    success: boolean;
    message: string;
    worktree: Worktree | null;
    session?: Record<string, unknown> | null;
    errors?: string[];
}

// ============================================================================
// Git Status API
// ============================================================================

/**
 * Get the git status for a project or worktree
 */
export async function getGitStatus(projectId: string, worktreeId?: string): Promise<GitStatus> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.get<GitStatus>(`${BASE}/${projectId}/git/status${params}`);
}

// ============================================================================
// Branch API
// ============================================================================

interface RawBranchListResponse {
    branches: RawBranch[];
    current_branch: string | null;
}

export interface BranchListResponse {
    branches: Branch[];
    current_branch: string | null;
}

/**
 * Get all branches for a project
 * @param projectId - The project ID
 * @param includeRemote - Whether to include remote tracking branches (default: false for cleaner UI)
 */
export async function getBranches(projectId: string, includeRemote: boolean = false, worktreeId?: string): Promise<Branch[]> {
    const searchParams = new URLSearchParams();
    if (!includeRemote) searchParams.set('include_remote', 'false');
    if (worktreeId) searchParams.set('worktree_id', worktreeId);
    const qs = searchParams.toString();
    const response = await api.get<RawBranchListResponse>(`${BASE}/${projectId}/git/branches${qs ? `?${qs}` : ''}`);
    return response.branches.map(transformBranch);
}

/**
 * Create a new branch
 */
export async function createBranch(projectId: string, name: string, startPoint?: string): Promise<Branch> {
    const body: CreateBranchRequest = { name };
    if (startPoint) {
        body.start_point = startPoint;
    }
    return api.post<Branch>(`${BASE}/${projectId}/git/branches`, body);
}

/**
 * Delete a branch
 */
export async function deleteBranch(projectId: string, name: string): Promise<void> {
    await api.delete(`${BASE}/${projectId}/git/branches/${encodeURIComponent(name)}`);
}

/**
 * Checkout a branch or ref
 */
export async function checkout(projectId: string, ref: string, worktreeId?: string): Promise<{ success: boolean; message: string }> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.post<{ success: boolean; message: string }>(`${BASE}/${projectId}/git/checkout${params}`, { ref });
}

/**
 * Fetch from remote
 */
export async function fetchRemote(projectId: string, worktreeId?: string): Promise<{ success: boolean; message: string }> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.post<{ success: boolean; message: string }>(`${BASE}/${projectId}/git/fetch${params}`);
}

// ============================================================================
// Commit Graph API
// ============================================================================

/**
 * Get the commit graph for a project
 */
// Raw response from API
interface RawCommitGraphResponse {
    commits: RawCommitNode[];
    total: number;
}

export async function getCommitGraph(projectId: string, limit?: number): Promise<CommitGraphResponse> {
    const params = limit ? `?limit=${limit}` : '';
    const raw = await api.get<RawCommitGraphResponse>(`${BASE}/${projectId}/git/graph${params}`);

    // Transform raw commits to frontend format
    return {
        commits: raw.commits.map(transformCommit),
        total: raw.total
    };
}

// ============================================================================
// Worktree API
// ============================================================================

export interface WorktreeListResponse {
    worktrees: Worktree[];
    total: number;
}

/**
 * Get all worktrees for a project
 */
export async function getWorktrees(projectId: string): Promise<Worktree[]> {
    const response = await api.get<WorktreeListResponse>(`${BASE}/${projectId}/git/worktrees`);
    return response.worktrees;
}

/**
 * Create a new worktree
 */
export async function createWorktree(projectId: string, data: CreateWorktreeRequest): Promise<CreateWorktreeResponse> {
    return api.post<CreateWorktreeResponse>(`${BASE}/${projectId}/git/worktrees`, data);
}

/**
 * Delete a worktree
 */
export async function deleteWorktree(projectId: string, worktreeId: string, keepBranch: boolean = true): Promise<void> {
    const params = keepBranch ? '' : '?keep_branch=false';
    await api.delete(`${BASE}/${projectId}/git/worktrees/${worktreeId}${params}`);
}

// ============================================================================
// Worktree Sync API
// ============================================================================

export interface WorktreeSyncResult {
    is_git_repo: boolean;
    active_worktrees: number;
    cleaned_up: Array<{
        id: string;
        branch_name: string;
        reason: string;
    }>;
}

/**
 * Sync worktree database records with actual state on disk.
 * Cleans up stale records where worktrees were deleted outside the app.
 * Should be called when loading a chat session for a project.
 */
export async function syncWorktrees(projectId: string): Promise<WorktreeSyncResult> {
    return api.post<WorktreeSyncResult>(`${BASE}/${projectId}/sync-worktrees`);
}

// ============================================================================
// Worktree File Browsing API
// ============================================================================

export interface WorktreeFileItem {
    name: string;
    type: 'file' | 'directory';
    size: number | null;
    sizeFormatted: string | null;
    path: string;
    extension: string | null;
    modifiedAt: string;
    createdAt: string;
}

export interface WorktreeFilesResponse {
    files: WorktreeFileItem[];
    path: string;
    worktreeId: string;
    worktreePath: string;
}

/**
 * List files within a worktree directory.
 * This allows browsing files in the worktree's working directory,
 * which is separate from the main project directory.
 *
 * @param projectId - The project ID
 * @param worktreeId - The worktree ID to browse files in
 * @param path - Relative path within the worktree (empty for root)
 * @param showHidden - Whether to show hidden files
 */
export async function getWorktreeFiles(
    projectId: string,
    worktreeId: string,
    path: string = '',
    showHidden: boolean = false
): Promise<WorktreeFilesResponse> {
    const params = new URLSearchParams();
    if (path) params.set('path', path);
    if (showHidden) params.set('show_hidden', 'true');

    const queryString = params.toString();
    const url = `${BASE}/${projectId}/git/worktrees/${worktreeId}/files${queryString ? `?${queryString}` : ''}`;

    return api.get<WorktreeFilesResponse>(url);
}

// ============================================================================
// Diff Stats API
// ============================================================================

export interface DiffStats {
    additions: number;
    deletions: number;
    files_changed: number;
}

/**
 * Get diff statistics (lines added/removed)
 */
export async function getDiffStats(projectId: string, includeUnstaged: boolean = true, worktreeId?: string): Promise<DiffStats> {
    const searchParams = new URLSearchParams();
    if (!includeUnstaged) searchParams.set('include_unstaged', 'false');
    if (worktreeId) searchParams.set('worktree_id', worktreeId);
    const qs = searchParams.toString();
    return api.get<DiffStats>(`${BASE}/${projectId}/git/diff-stats${qs ? `?${qs}` : ''}`);
}

// ============================================================================
// Commit API
// ============================================================================

export interface CommitRequest {
    message: string;
    include_unstaged?: boolean;
}

export interface CommitResponse {
    success: boolean;
    message: string;
    commit_sha: string | null;
}

/**
 * Create a commit
 */
export async function commitChanges(projectId: string, request: CommitRequest, worktreeId?: string): Promise<CommitResponse> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.post<CommitResponse>(`${BASE}/${projectId}/git/commit${params}`, request);
}

// ============================================================================
// Push API
// ============================================================================

export interface PushRequest {
    remote?: string;
    branch?: string;
    set_upstream?: boolean;
    force?: boolean;
}

/**
 * Push commits to remote
 */
export async function pushCommits(projectId: string, request: PushRequest = {}, worktreeId?: string): Promise<{ success: boolean; message: string }> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.post<{ success: boolean; message: string }>(`${BASE}/${projectId}/git/push${params}`, request);
}

// ============================================================================
// Pull API
// ============================================================================

export interface PullRequest {
    remote?: string;
    strategy?: 'merge' | 'rebase';
    auto_stash?: boolean;
}

export interface PullResponse {
    success: boolean;
    message: string;
    conflicts: string[];
    has_conflicts: boolean;
}

/**
 * Pull from remote (fetch + merge/rebase)
 */
export async function pullFromRemote(
    projectId: string,
    request: PullRequest = {},
    worktreeId?: string
): Promise<PullResponse> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.post<PullResponse>(`${BASE}/${projectId}/git/pull${params}`, request);
}

// ============================================================================
// Init Repo API
// ============================================================================

export interface InitRepoRequest {
    create_initial_commit?: boolean;
    create_github_repo?: boolean;
    github_repo_name?: string;
    github_private?: boolean;
    default_branch?: string;
}

export interface InitRepoResponse {
    success: boolean;
    message: string;
    repository_id: string | null;
    github_repo_url: string | null;
    github_repo_name: string | null;
}

/**
 * Initialize a git repository for a project
 */
export async function initRepository(
    projectId: string,
    request: InitRepoRequest = {}
): Promise<InitRepoResponse> {
    return api.post<InitRepoResponse>(`${BASE}/${projectId}/git/init`, request);
}

// ============================================================================
// AI Generation API
// ============================================================================

export interface GenerateCommitMessageRequest {
    include_unstaged?: boolean;
}

export interface GenerateCommitMessageResponse {
    message: string;
}

export interface GeneratePRDescriptionRequest {
    base_branch?: string;
}

export interface GeneratePRDescriptionResponse {
    title: string;
    body: string;
}

/**
 * Generate a commit message using AI based on the current diff
 */
export async function generateCommitMessage(
    projectId: string,
    request: GenerateCommitMessageRequest = {},
    worktreeId?: string
): Promise<GenerateCommitMessageResponse> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.post<GenerateCommitMessageResponse>(
        `${BASE}/${projectId}/git/generate-commit-message${params}`, request
    );
}

/**
 * Generate a PR title and description using AI based on branch commits
 */
export async function generatePRDescription(
    projectId: string,
    request: GeneratePRDescriptionRequest = {},
    worktreeId?: string
): Promise<GeneratePRDescriptionResponse> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.post<GeneratePRDescriptionResponse>(
        `${BASE}/${projectId}/git/generate-pr-description${params}`, request
    );
}
