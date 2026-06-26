/**
 * Git API client for AI Hub
 */

import { api, type RequestOptions } from './client';

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
    /** True when the branch has real content over its base branch (non-empty
     *  base...HEAD diff). False for branches whose commits net to no change,
     *  so the UI can hide "Ship It" when there is nothing left to ship. */
    differs_from_base: boolean;
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

/**
 * Deduplicate branches by name, keeping the first occurrence.
 * Guards against duplicate entries regardless of source (API, WebSocket, cache).
 */
function deduplicateBranches(branches: Branch[]): Branch[] {
    const seen = new Set<string>();
    return branches.filter(b => {
        if (seen.has(b.name)) return false;
        seen.add(b.name);
        return true;
    });
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
export async function getGitStatus(projectId: string, worktreeId?: string, options?: RequestOptions): Promise<GitStatus> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.get<GitStatus>(`${BASE}/${projectId}/git/status${params}`, options);
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
export async function getBranches(projectId: string, includeRemote: boolean = false, worktreeId?: string, options?: RequestOptions): Promise<Branch[]> {
    const searchParams = new URLSearchParams();
    if (!includeRemote) searchParams.set('include_remote', 'false');
    if (worktreeId) searchParams.set('worktree_id', worktreeId);
    const qs = searchParams.toString();
    const response = await api.get<RawBranchListResponse>(`${BASE}/${projectId}/git/branches${qs ? `?${qs}` : ''}`, options);
    return deduplicateBranches(response.branches.map(transformBranch));
}

// ============================================================================
// Cloud (cross-install) git state
// ============================================================================

interface RawCloudGitState {
    present: boolean;
    default_branch: string;
    branches: RawBranch[];
    current_branch: string | null;
}

export interface CloudGitState {
    /** Whether the repo is already cloned on the target device. When false the
     *  worker will clone it on first send, so the picker offers just the default. */
    present: boolean;
    /** The repo's default branch (used when the target has no clone yet). */
    defaultBranch: string;
    /** The branches the target device offers (mirrors its own selector). */
    branches: Branch[];
    /** The branch the target currently has checked out (picker default). */
    currentBranch: string | null;
}

/**
 * Preview a target install's git state for the cloud branch picker.
 *
 * Resolves the project's GitHub remote and asks the chosen device (via the
 * cluster relay) which branches it has and what it currently has checked out, so
 * the picker can mirror the remote's selector and default to its current branch.
 */
export async function getCloudGitState(
    projectId: string,
    targetInstanceId: string,
    options?: RequestOptions
): Promise<CloudGitState> {
    const qs = new URLSearchParams({
        project_id: projectId,
        target_instance_id: targetInstanceId
    }).toString();
    const raw = await api.get<RawCloudGitState>(`/sessions/cloud/git-state?${qs}`, options);
    return {
        present: raw.present,
        defaultBranch: raw.default_branch,
        branches: deduplicateBranches((raw.branches || []).map(transformBranch)),
        currentBranch: raw.current_branch
    };
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
 * Checkout a branch or ref.
 *
 * When `autoStash` is true, the backend stashes uncommitted changes before
 * checking out (instead of returning a 409). Mirrors the pull auto_stash flow.
 */
export async function checkout(projectId: string, ref: string, worktreeId?: string, autoStash: boolean = false): Promise<{ success: boolean; message: string }> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.post<{ success: boolean; message: string }>(`${BASE}/${projectId}/git/checkout${params}`, { ref, auto_stash: autoStash });
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

/**
 * Reopen a completed worktree — recreates the git worktree on disk
 * and sets the DB record back to active so the session can resume.
 */
export async function reopenWorktree(projectId: string, worktreeId: string): Promise<Worktree> {
    return api.post<Worktree>(`${BASE}/${projectId}/git/worktrees/${worktreeId}/reopen`);
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
// Branch Name Generation
// ============================================================================

export interface GenerateBranchNameResponse {
    branch_name: string;
    display_name: string;
}

/**
 * Generate a branch name from a user message using AI.
 * Falls back to slugification on the backend if AI fails.
 */
export async function generateBranchName(
    projectId: string,
    message: string,
    prefix?: string
): Promise<GenerateBranchNameResponse> {
    const body: Record<string, string> = { message };
    if (prefix) body.prefix = prefix;
    return api.post<GenerateBranchNameResponse>(
        `${BASE}/${projectId}/git/generate-branch-name`,
        body
    );
}

/**
 * Client-side fallback for branch name generation (no API call).
 * Used when the backend is unreachable or as instant fallback.
 * Filters common filler/stop words to produce meaningful branch names.
 */
const STOP_WORDS = new Set([
    'a', 'an', 'the', 'is', 'it', 'in', 'on', 'at', 'to', 'for', 'of',
    'and', 'or', 'but', 'not', 'with', 'from', 'by', 'as', 'be', 'was',
    'are', 'been', 'has', 'have', 'had', 'do', 'does', 'did', 'will',
    'would', 'could', 'should', 'can', 'may', 'might', 'shall',
    'i', 'me', 'my', 'we', 'us', 'our', 'you', 'your', 'he', 'she',
    'they', 'them', 'their', 'this', 'that', 'these', 'those',
    'please', 'help', 'want', 'need', 'like', 'just', 'also',
    'hi', 'hello', 'hey', 'thanks', 'thank',
]);

const FIX_WORDS = new Set(['fix', 'bug', 'broken', 'crash', 'error', 'issue', 'wrong', 'fail', 'failing', 'regression', 'hotfix', 'patch']);
const REFACTOR_WORDS = new Set(['refactor', 'restructure', 'reorganize', 'simplify', 'extract', 'rename', 'move', 'split']);
const CHORE_WORDS = new Set(['chore', 'deps', 'dependency', 'upgrade', 'bump', 'ci', 'lint', 'format', 'cleanup', 'clean']);
const DOCS_WORDS = new Set(['docs', 'documentation', 'readme', 'comment', 'comments', 'jsdoc', 'docstring']);
const TEST_WORDS = new Set(['test', 'tests', 'testing', 'spec', 'coverage']);

function inferPrefix(message: string): string {
    const words = new Set(message.toLowerCase().split(/\s+/));
    for (const w of words) {
        if (FIX_WORDS.has(w)) return 'fix';
        if (REFACTOR_WORDS.has(w)) return 'refactor';
        if (CHORE_WORDS.has(w)) return 'chore';
        if (DOCS_WORDS.has(w)) return 'docs';
        if (TEST_WORDS.has(w)) return 'test';
    }
    return 'feat';
}

export function generateBranchNameLocal(message: string, prefix?: string): string {
    const resolvedPrefix = prefix || inferPrefix(message);
    const slug = message
        .toLowerCase()
        .trim()
        .replace(/[^\w\s-]/g, '')  // Remove special chars
        .split(/\s+/)              // Split on whitespace
        .filter(w => w.length > 1 && !STOP_WORDS.has(w)) // Skip single chars and stop words
        .slice(0, 5)               // Take first 5 meaningful words
        .join('-');
    const clean = slug.slice(0, 50).replace(/-+$/, '');
    return clean ? `${resolvedPrefix}/${clean}` : `${resolvedPrefix}/auto-${Date.now().toString(36)}`;
}

// ============================================================================
// Upload Migration
// ============================================================================

export interface MigrateUploadsResponse {
    migrated: Array<{ original_path: string; new_path: string; relative_path: string }>;
    errors: string[];
}

/**
 * Copy pending uploads from main project to a worktree directory.
 */
export async function migrateUploadsToWorktree(
    projectId: string,
    worktreeId: string,
    filePaths: string[]
): Promise<MigrateUploadsResponse> {
    return api.post<MigrateUploadsResponse>(
        `${BASE}/${projectId}/git/migrate-uploads`,
        { worktree_id: worktreeId, file_paths: filePaths }
    );
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
// Changed Files & File-Level Operations API
// ============================================================================

export interface FileChangeInfo {
    file: string;
    status: string;       // A=added, M=modified, D=deleted, R=renamed, ?=untracked, U=conflict
    category: string;     // 'staged' | 'modified' | 'untracked' | 'conflict'
    additions: number;
    deletions: number;
    is_binary: boolean;
}

export interface ChangedFilesResponse {
    files: FileChangeInfo[];
    total_additions: number;
    total_deletions: number;
}

export interface FileDiffResponse {
    file: string;
    diff: string;
    is_binary: boolean;
    truncated: boolean;
}

export interface OperationResponse {
    success: boolean;
    message: string;
    status?: Record<string, unknown>;
    diff_stats?: DiffStats;
}

/**
 * Get all changed files with per-file diff stats
 */
export async function getChangedFiles(projectId: string, worktreeId?: string): Promise<ChangedFilesResponse> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.get<ChangedFilesResponse>(`${BASE}/${projectId}/git/changed-files${params}`);
}

/**
 * Get unified diff for a single file (lazy, on-demand)
 */
export async function getFileDiff(
    projectId: string,
    filePath: string,
    staged: boolean = false,
    worktreeId?: string
): Promise<FileDiffResponse> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.post<FileDiffResponse>(`${BASE}/${projectId}/git/file-diff${params}`, {
        file_path: filePath,
        staged,
    });
}

/**
 * Stage specific files
 */
export async function stageFiles(
    projectId: string,
    files: string[],
    worktreeId?: string
): Promise<OperationResponse> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.post<OperationResponse>(`${BASE}/${projectId}/git/stage${params}`, { files });
}

/**
 * Unstage specific files (remove from index, keep working tree changes)
 */
export async function unstageFiles(
    projectId: string,
    files: string[],
    worktreeId?: string
): Promise<OperationResponse> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.post<OperationResponse>(`${BASE}/${projectId}/git/unstage${params}`, { files });
}

/**
 * Discard changes for specific files
 */
export async function discardChanges(
    projectId: string,
    files: string[],
    fileStatuses: Record<string, string>,
    worktreeId?: string
): Promise<OperationResponse> {
    const params = worktreeId ? `?worktree_id=${encodeURIComponent(worktreeId)}` : '';
    return api.post<OperationResponse>(`${BASE}/${projectId}/git/discard${params}`, {
        files,
        file_statuses: fileStatuses,
    });
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
export async function getDiffStats(projectId: string, includeUnstaged: boolean = true, worktreeId?: string, options?: RequestOptions): Promise<DiffStats> {
    const searchParams = new URLSearchParams();
    if (!includeUnstaged) searchParams.set('include_unstaged', 'false');
    if (worktreeId) searchParams.set('worktree_id', worktreeId);
    const qs = searchParams.toString();
    return api.get<DiffStats>(`${BASE}/${projectId}/git/diff-stats${qs ? `?${qs}` : ''}`, options);
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
// Clone Repo API
// ============================================================================

export interface CloneRepoRequest {
    clone_url: string;
}

export interface CloneRepoResponse {
    success: boolean;
    message: string;
    repository_id: string | null;
}

/**
 * Clone a remote repository into a project's working directory
 */
export async function cloneRepository(
    projectId: string,
    request: CloneRepoRequest
): Promise<CloneRepoResponse> {
    return api.post<CloneRepoResponse>(`${BASE}/${projectId}/git/clone`, request);
}
