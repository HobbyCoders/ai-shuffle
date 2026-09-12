/**
 * Git API client for AI Hub
 */

import { api, type RequestOptions } from './client';

const BASE = '/projects';

// ============================================================================
// Git Status Types
// ============================================================================

/** A staged entry as it arrives on the wire (`{file, status}`). */
export interface StagedFileEntry {
    file: string;
    status: string;  // A=added, M=modified, D=deleted, R=renamed, C=copied
}

/**
 * `/git/status` EXACTLY as the backend sends it
 * (`app/api/schemas/git.py::GitStatusResponse`).
 *
 * `staged` / `modified` / `untracked` / `conflicts` are **lists**, not counts.
 * The old `GitStatus` declared them as numbers, so every poll produced fresh
 * array references that reference-equality "no-op suppression" could never
 * match — a store write + subscriber cascade fired on every 2 s poll whenever
 * any file list was non-empty. `transformGitStatus` below collapses the wire
 * shape into a flat, cheaply-comparable record so suppression actually works.
 */
export interface RawGitStatus {
    is_git_repo: boolean;
    current_branch?: string | null;
    is_detached?: boolean;
    head_commit?: string | null;
    remote_url?: string | null;
    is_clean?: boolean;
    staged?: StagedFileEntry[];
    modified?: string[];
    untracked?: string[];
    conflicts?: string[];
    ahead?: number;
    behind?: number;
    differs_from_base?: boolean;
    matches_base_tree?: boolean | null;
    /** Commits on HEAD missing from origin/<branch>; null/absent if never pushed. */
    unpushed?: number | null;
    // Repository-record fields. Present on GET /git/status, ABSENT from the
    // inline status payloads returned by the mutation endpoints (which call
    // git_service.get_status directly) — see mergeStatusRepoMeta.
    repository_id?: string | null;
    default_branch?: string | null;
    github_repo_name?: string | null;
    last_synced_at?: string | null;
}

/**
 * Canonical git status: flat scalars plus one digest of the file lists.
 *
 * Every consumer in the app reads this shape, and `gitStatusEqual` can decide
 * "nothing changed" with a handful of primitive comparisons. Nothing here is a
 * fresh object/array per poll, so an unchanged status never notifies.
 */
export interface GitStatus {
    is_git_repo: boolean;
    current_branch: string | null;
    is_detached: boolean;
    head_commit: string | null;
    remote_url: string | null;
    is_clean: boolean;
    /** Any uncommitted work at all (staged, modified, untracked or conflicted). */
    has_changes: boolean;
    /** Counts, derived from the wire's lists. */
    staged: number;
    modified: number;
    untracked: number;
    conflicts: number;
    ahead: number;
    behind: number;
    /** True when the branch has real content over its base branch (non-empty
     *  base...HEAD diff). False for branches whose commits net to no change,
     *  so the UI can hide "Ship It" when there is nothing left to ship. */
    differs_from_base: boolean;
    matches_base_tree?: boolean | null;
    /**
     * Commits on HEAD that origin/<branch> does not have. `null` when the branch
     * has no remote counterpart (never pushed) — distinct from `0`, which means
     * fully pushed. `ahead` is vs the default branch and cannot tell these apart.
     */
    unpushed: number | null;
    /** Repository record (GET /git/status only — null on inline payloads). */
    repository_id: string | null;
    /** The repo's real default branch. Authoritative source for "am I on the
     *  default branch?" — do NOT fall back to a hardcoded 'main'. */
    default_branch: string | null;
    github_repo_name: string | null;
    /**
     * Digest of the staged/modified/untracked/conflict file lists. Two statuses
     * with the same digest describe the same working tree, so equality checks
     * never have to walk the lists. Not order-normalized: the backend emits a
     * deterministic order, and a spurious mismatch costs one extra update.
     */
    files_fingerprint: string;
}

function _fingerprint(raw: RawGitStatus): string {
    const staged = (raw.staged ?? []).map((s) => `${s.status}:${s.file}`).join(',');
    const modified = (raw.modified ?? []).join(',');
    const untracked = (raw.untracked ?? []).join(',');
    const conflicts = (raw.conflicts ?? []).join(',');
    return `${staged}|${modified}|${untracked}|${conflicts}`;
}

/** Collapse a wire status into the canonical flat shape. */
export function transformGitStatus(raw: RawGitStatus): GitStatus {
    const staged = raw.staged?.length ?? 0;
    const modified = raw.modified?.length ?? 0;
    const untracked = raw.untracked?.length ?? 0;
    const conflicts = raw.conflicts?.length ?? 0;
    const isClean = raw.is_clean ?? (staged + modified + untracked + conflicts === 0);
    return {
        is_git_repo: raw.is_git_repo ?? false,
        current_branch: raw.current_branch ?? null,
        is_detached: raw.is_detached ?? false,
        head_commit: raw.head_commit ?? null,
        remote_url: raw.remote_url ?? null,
        is_clean: isClean,
        has_changes: !isClean || staged + modified + untracked + conflicts > 0,
        staged,
        modified,
        untracked,
        conflicts,
        ahead: raw.ahead ?? 0,
        behind: raw.behind ?? 0,
        differs_from_base: raw.differs_from_base ?? false,
        matches_base_tree: raw.matches_base_tree ?? null,
        // Preserve null (never pushed) — only absent/undefined becomes null.
        unpushed: raw.unpushed ?? null,
        repository_id: raw.repository_id ?? null,
        default_branch: raw.default_branch ?? null,
        github_repo_name: raw.github_repo_name ?? null,
        files_fingerprint: _fingerprint(raw)
    };
}

/**
 * The definitive "this folder has no repo" status, in the SAME shape every
 * other status has. Used when we positively learn the working folder is gone
 * (e.g. a cleaned-up worktree whose directory 404s) so the header fails closed
 * instead of keeping a stale `is_git_repo: true`.
 */
export function notARepoStatus(): GitStatus {
    return transformGitStatus({ is_git_repo: false });
}

/**
 * True when two canonical statuses describe the same repository state.
 * Every field is a primitive, so this is a handful of `===` comparisons —
 * the whole point of the canonical shape (see RawGitStatus docs).
 */
export function gitStatusEqual(a: GitStatus | null, b: GitStatus | null): boolean {
    if (a === b) return true;
    if (!a || !b) return false;
    return (
        a.is_git_repo === b.is_git_repo &&
        a.current_branch === b.current_branch &&
        a.is_detached === b.is_detached &&
        a.head_commit === b.head_commit &&
        a.remote_url === b.remote_url &&
        a.is_clean === b.is_clean &&
        a.has_changes === b.has_changes &&
        a.staged === b.staged &&
        a.modified === b.modified &&
        a.untracked === b.untracked &&
        a.conflicts === b.conflicts &&
        a.ahead === b.ahead &&
        a.behind === b.behind &&
        a.differs_from_base === b.differs_from_base &&
        a.matches_base_tree === b.matches_base_tree &&
        a.unpushed === b.unpushed &&
        a.repository_id === b.repository_id &&
        a.default_branch === b.default_branch &&
        a.github_repo_name === b.github_repo_name &&
        a.files_fingerprint === b.files_fingerprint
    );
}

/**
 * Carry the repository-record fields (repository_id / default_branch /
 * github_repo_name) forward from `prev` onto an inline mutation payload.
 *
 * The mutation endpoints build their inline status from
 * `git_service.get_status` alone, which knows nothing about the DB repo record,
 * so applying such a payload verbatim would blank `default_branch` — and the
 * header would immediately mis-decide "am I on the default branch?".
 */
export function mergeStatusRepoMeta(prev: GitStatus | null, next: GitStatus): GitStatus {
    if (!prev) return next;
    if (next.repository_id !== null || next.default_branch !== null) return next;
    return {
        ...next,
        repository_id: prev.repository_id,
        default_branch: prev.default_branch,
        github_repo_name: prev.github_repo_name
    };
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

/** @deprecated Alias kept for the worktree-list shape below; new code uses
 *  {@link StagedFileEntry}. */
export type StagedFile = StagedFileEntry;

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

// ============================================================================
// Git Status API
// ============================================================================

/** Query-string fragment scoping a git call to a legacy worktree row and/or a
 *  v2 branch session. The backend prefers worktree_id; session_id resolves the
 *  session's own checkout (needed for v2 branch sessions with no legacy row). */
function scopeQs(worktreeId?: string, sessionId?: string): string {
    const sp = new URLSearchParams();
    if (worktreeId) sp.set('worktree_id', worktreeId);
    if (sessionId) sp.set('session_id', sessionId);
    const qs = sp.toString();
    return qs ? `?${qs}` : '';
}

/**
 * Get the git status for a project or worktree.
 *
 * The wire response is normalized into the canonical flat `GitStatus` here, at
 * the API boundary, so no consumer ever sees the raw list shape.
 */
export async function getGitStatus(projectId: string, worktreeId?: string, options?: RequestOptions, sessionId?: string): Promise<GitStatus> {
    const raw = await api.get<RawGitStatus>(`${BASE}/${projectId}/git/status${scopeQs(worktreeId, sessionId)}`, options);
    return transformGitStatus(raw);
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
export async function getBranches(projectId: string, includeRemote: boolean = false, worktreeId?: string, options?: RequestOptions, sessionId?: string): Promise<Branch[]> {
    const searchParams = new URLSearchParams();
    if (!includeRemote) searchParams.set('include_remote', 'false');
    if (worktreeId) searchParams.set('worktree_id', worktreeId);
    if (sessionId) searchParams.set('session_id', sessionId);
    const qs = searchParams.toString();
    const response = await api.get<RawBranchListResponse>(`${BASE}/${projectId}/git/branches${qs ? `?${qs}` : ''}`, options);
    return deduplicateBranches(response.branches.map(transformBranch));
}

/**
 * Create a new branch
 */
export async function createBranch(projectId: string, name: string, startPoint?: string, sessionId?: string): Promise<Branch> {
    const body: CreateBranchRequest = { name };
    if (startPoint) {
        body.start_point = startPoint;
    }
    return api.post<Branch>(`${BASE}/${projectId}/git/branches${scopeQs(undefined, sessionId)}`, body);
}

/**
 * Delete a branch
 */
export async function deleteBranch(projectId: string, name: string, sessionId?: string): Promise<void> {
    await api.delete(`${BASE}/${projectId}/git/branches/${encodeURIComponent(name)}${scopeQs(undefined, sessionId)}`);
}

/**
 * Checkout a branch or ref.
 *
 * When `autoStash` is true, the backend stashes uncommitted changes before
 * checking out (instead of returning a 409). Mirrors the pull auto_stash flow.
 */
export async function checkout(projectId: string, ref: string, worktreeId?: string, autoStash: boolean = false, sessionId?: string): Promise<{ success: boolean; message: string }> {
    const params = scopeQs(worktreeId, sessionId);
    return api.post<{ success: boolean; message: string }>(`${BASE}/${projectId}/git/checkout${params}`, { ref, auto_stash: autoStash });
}

/**
 * Fetch from remote
 */
export async function fetchRemote(projectId: string, worktreeId?: string, sessionId?: string): Promise<{ success: boolean; message: string }> {
    return api.post<{ success: boolean; message: string }>(`${BASE}/${projectId}/git/fetch${scopeQs(worktreeId, sessionId)}`);
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

export async function getCommitGraph(projectId: string, limit?: number, worktreeId?: string, sessionId?: string): Promise<CommitGraphResponse> {
    const query = new URLSearchParams(scopeQs(worktreeId, sessionId));
    if (limit) query.set('limit', String(limit));
    const params = query.size ? `?${query}` : '';
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

/**
 * Generic mutation response.
 *
 * The stage / unstage / discard / push endpoints compute a fresh status (and,
 * where relevant, diff stats) inline "for instant frontend update". These are
 * normalized into the canonical shapes here so the store can APPLY them instead
 * of throwing them away and re-issuing refresh requests (finding G6).
 */
export interface OperationResponse {
    success: boolean;
    message: string;
    /** Inline post-mutation status. Lacks the repository-record fields — run it
     *  through {@link mergeStatusRepoMeta} before storing. */
    status?: GitStatus | null;
    diff_stats?: DiffStats | null;
}

/** The same response before normalization. */
interface RawOperationResponse {
    success: boolean;
    message: string;
    status?: RawGitStatus | null;
    diff_stats?: DiffStats | null;
}

function transformOperationResponse(raw: RawOperationResponse): OperationResponse {
    return {
        success: raw.success,
        message: raw.message,
        status: raw.status ? transformGitStatus(raw.status) : null,
        diff_stats: raw.diff_stats ?? null
    };
}

/**
 * Get all changed files with per-file diff stats
 */
export async function getChangedFiles(projectId: string, worktreeId?: string, sessionId?: string): Promise<ChangedFilesResponse> {
    return api.get<ChangedFilesResponse>(`${BASE}/${projectId}/git/changed-files${scopeQs(worktreeId, sessionId)}`);
}

/**
 * Get unified diff for a single file (lazy, on-demand)
 */
export async function getFileDiff(
    projectId: string,
    filePath: string,
    staged: boolean = false,
    worktreeId?: string,
    sessionId?: string
): Promise<FileDiffResponse> {
    return api.post<FileDiffResponse>(`${BASE}/${projectId}/git/file-diff${scopeQs(worktreeId, sessionId)}`, {
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
    worktreeId?: string,
    sessionId?: string
): Promise<OperationResponse> {
    return transformOperationResponse(
        await api.post<RawOperationResponse>(`${BASE}/${projectId}/git/stage${scopeQs(worktreeId, sessionId)}`, { files })
    );
}

/**
 * Unstage specific files (remove from index, keep working tree changes)
 */
export async function unstageFiles(
    projectId: string,
    files: string[],
    worktreeId?: string,
    sessionId?: string
): Promise<OperationResponse> {
    return transformOperationResponse(
        await api.post<RawOperationResponse>(`${BASE}/${projectId}/git/unstage${scopeQs(worktreeId, sessionId)}`, { files })
    );
}

/**
 * Discard changes for specific files
 */
export async function discardChanges(
    projectId: string,
    files: string[],
    fileStatuses: Record<string, string>,
    worktreeId?: string,
    sessionId?: string
): Promise<OperationResponse> {
    return transformOperationResponse(
        await api.post<RawOperationResponse>(`${BASE}/${projectId}/git/discard${scopeQs(worktreeId, sessionId)}`, {
            files,
            file_statuses: fileStatuses,
        })
    );
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
export async function getDiffStats(projectId: string, includeUnstaged: boolean = true, worktreeId?: string, options?: RequestOptions, sessionId?: string): Promise<DiffStats> {
    const searchParams = new URLSearchParams();
    if (!includeUnstaged) searchParams.set('include_unstaged', 'false');
    if (worktreeId) searchParams.set('worktree_id', worktreeId);
    if (sessionId) searchParams.set('session_id', sessionId);
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
    /** Post-commit status computed inline by the backend. */
    status?: GitStatus | null;
    diff_stats?: DiffStats | null;
}

interface RawCommitResponse {
    success: boolean;
    message: string;
    commit_sha: string | null;
    status?: RawGitStatus | null;
    diff_stats?: DiffStats | null;
}

/**
 * Create a commit. The response carries the backend's inline post-commit
 * status + diff stats, which the store applies directly (finding G6).
 */
export async function commitChanges(projectId: string, request: CommitRequest, worktreeId?: string, sessionId?: string): Promise<CommitResponse> {
    const raw = await api.post<RawCommitResponse>(`${BASE}/${projectId}/git/commit${scopeQs(worktreeId, sessionId)}`, request);
    return {
        success: raw.success,
        message: raw.message,
        commit_sha: raw.commit_sha ?? null,
        status: raw.status ? transformGitStatus(raw.status) : null,
        diff_stats: raw.diff_stats ?? null
    };
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
 * Push commits to remote. Returns the backend's inline post-push status.
 */
export async function pushCommits(projectId: string, request: PushRequest = {}, worktreeId?: string, sessionId?: string): Promise<OperationResponse> {
    return transformOperationResponse(
        await api.post<RawOperationResponse>(`${BASE}/${projectId}/git/push${scopeQs(worktreeId, sessionId)}`, request)
    );
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
    /** Post-pull status + diff stats computed inline by the backend. */
    status?: GitStatus | null;
    diff_stats?: DiffStats | null;
}

interface RawPullResponse {
    success: boolean;
    message: string;
    conflicts?: string[];
    has_conflicts?: boolean;
    status?: RawGitStatus | null;
    diff_stats?: DiffStats | null;
}

/**
 * Pull from remote (fetch + merge/rebase)
 */
export async function pullFromRemote(
    projectId: string,
    request: PullRequest = {},
    worktreeId?: string,
    sessionId?: string
): Promise<PullResponse> {
    const raw = await api.post<RawPullResponse>(`${BASE}/${projectId}/git/pull${scopeQs(worktreeId, sessionId)}`, request);
    return {
        success: raw.success,
        message: raw.message,
        conflicts: raw.conflicts ?? [],
        has_conflicts: raw.has_conflicts ?? false,
        status: raw.status ? transformGitStatus(raw.status) : null,
        diff_stats: raw.diff_stats ?? null
    };
}

// ============================================================================
// PR State API
// ============================================================================

export interface OpenPrInfo {
    number: number;
    title: string;
    url: string;
    draft: boolean;
    state: string | null;
    /** GitHub computes this lazily; null = unknown yet — NOT unmergeable. */
    mergeable: boolean | null;
}

/**
 * `/git/pr-state` as it arrives on the wire.
 *
 * The failure flag is accepted under several spellings: the backend is adding
 * an explicit "the GitHub lookup did not complete" signal, and older builds
 * ship none at all. Absent ⇒ `false` (treat as a completed lookup) — see
 * {@link transformPrState}.
 */
export interface RawPrState {
    current_branch?: string | null;
    has_github?: boolean;
    open_pr?: OpenPrInfo | null;
    merged_head_sha?: string | null;
    pr_lookup_failed?: boolean;
    /** Companion flag: `open_pr` is a last-known-good value the server served
     *  past its TTL because the refresh failed. */
    stale?: boolean;
    // Alternative spellings tolerated so the client works against builds that
    // predate (or rename) the flag.
    lookup_failed?: boolean;
    lookupFailed?: boolean;
}

export interface PrState {
    current_branch: string | null;
    has_github: boolean;
    open_pr: OpenPrInfo | null;
    /** Original head of the latest merged PR for this branch. */
    merged_head_sha?: string | null;
    /**
     * True when the backend could NOT complete the GitHub PR lookup this cycle
     * (gh unavailable / rate limited / errored). A failed lookup is NOT the
     * same as "no open PR": the store keeps the previous `open_pr` and flags it
     * stale, so the header never flips "Merge PR #12" → "Create PR" because a
     * lookup blipped.
     */
    lookup_failed: boolean;
    /**
     * True when `open_pr` is a last-known-good value the SERVER served past its
     * TTL because the refresh failed. Only ever set together with
     * `lookup_failed`; it tells the client the payload already carries a usable
     * previous value rather than nothing at all.
     */
    served_stale: boolean;
}

export function transformPrState(raw: RawPrState): PrState {
    return {
        current_branch: raw.current_branch ?? null,
        has_github: raw.has_github ?? false,
        open_pr: raw.open_pr ?? null,
        merged_head_sha: raw.merged_head_sha ?? null,
        lookup_failed: Boolean(raw.pr_lookup_failed ?? raw.lookup_failed ?? raw.lookupFailed ?? false),
        served_stale: Boolean(raw.stale ?? false)
    };
}

/** Deep equality for PR state — `open_pr` is a nested object rebuilt on every
 *  poll, so a shallow reference compare never suppressed anything. */
export function prStateEqual(a: PrState | null, b: PrState | null): boolean {
    if (a === b) return true;
    if (!a || !b) return false;
    if (
        a.current_branch !== b.current_branch ||
        a.has_github !== b.has_github ||
        a.merged_head_sha !== b.merged_head_sha ||
        a.lookup_failed !== b.lookup_failed ||
        a.served_stale !== b.served_stale
    ) {
        return false;
    }
    const x = a.open_pr;
    const y = b.open_pr;
    if (x === y) return true;
    if (!x || !y) return false;
    return (
        x.number === y.number &&
        x.title === y.title &&
        x.url === y.url &&
        x.draft === y.draft &&
        x.state === y.state &&
        x.mergeable === y.mergeable
    );
}

/**
 * PR lifecycle for the current branch of the resolved checkout
 * (session-scoped like every other git call).
 */
export async function getPrState(projectId: string, worktreeId?: string, sessionId?: string, options?: RequestOptions): Promise<PrState> {
    const raw = await api.get<RawPrState>(`${BASE}/${projectId}/git/pr-state${scopeQs(worktreeId, sessionId)}`, options);
    return transformPrState(raw);
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
    request: InitRepoRequest = {},
    sessionId?: string
): Promise<InitRepoResponse> {
    return api.post<InitRepoResponse>(`${BASE}/${projectId}/git/init${scopeQs(undefined, sessionId)}`, request);
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
    request: CloneRepoRequest,
    sessionId?: string
): Promise<CloneRepoResponse> {
    return api.post<CloneRepoResponse>(`${BASE}/${projectId}/git/clone${scopeQs(undefined, sessionId)}`, request);
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
    worktreeId?: string,
    sessionId?: string
): Promise<GenerateCommitMessageResponse> {
    return api.post<GenerateCommitMessageResponse>(
        `${BASE}/${projectId}/git/generate-commit-message${scopeQs(worktreeId, sessionId)}`, request
    );
}

/**
 * Generate a PR title and description using AI based on branch commits
 */
export async function generatePRDescription(
    projectId: string,
    request: GeneratePRDescriptionRequest = {},
    worktreeId?: string,
    sessionId?: string
): Promise<GeneratePRDescriptionResponse> {
    return api.post<GeneratePRDescriptionResponse>(
        `${BASE}/${projectId}/git/generate-pr-description${scopeQs(worktreeId, sessionId)}`, request
    );
}

// ============================================================================
// Multi-step Git Operations API (Finish / Ship It)
// ============================================================================
//
// Finish and Ship It run as server-side operations composed of ordered steps.
// The POST endpoint kicks off the operation and returns its initial snapshot;
// the client then polls the GET endpoint until the operation reaches a terminal
// status (`done` or `error`), rendering per-step progress in GitProgressPopover.

export type MergeMethod = 'squash' | 'merge' | 'rebase';

export type GitOperationStatus = 'running' | 'done' | 'error';

export type GitOperationStepStatus =
    | 'pending'
    | 'running'
    | 'done'
    | 'error'
    | 'warning'
    | 'skipped';

export interface GitOperationStep {
    id: string;
    label: string;
    status: GitOperationStepStatus;
    error?: string;
    detail?: string;
}

export interface GitOperationResult {
    pr_url?: string;
    pr_number?: number;
    worktree_deleted?: boolean;
}

export interface GitOperation {
    operation_id: string;
    status: GitOperationStatus;
    steps: GitOperationStep[];
    result?: GitOperationResult;
}

export interface FinishOperationRequest {
    worktree_id?: string;
    session_id?: string;
    merge_method: MergeMethod;
    cleanup: boolean;
}

export interface ShipOperationRequest {
    worktree_id?: string;
    session_id?: string;
    merge_method: MergeMethod;
    cleanup: boolean;
    include_unstaged: boolean;
    /** Canonical wire field the backend expects (`base_branch`). The nicer `base`
     *  name lives on ShipItOptions and is mapped to this at the client boundary. */
    base_branch?: string;
    draft?: boolean;
}

/**
 * Start a "Finish" operation — merge the branch's open PR into the default
 * branch (and optionally clean up). Returns the operation's initial snapshot.
 */
export async function startFinishOperation(
    projectId: string,
    request: FinishOperationRequest
): Promise<GitOperation> {
    return api.post<GitOperation>(`${BASE}/${projectId}/git/finish`, request);
}

/**
 * Start a "Ship It" operation — commit → push → open/merge PR → clean up, end
 * to end. Returns the operation's initial snapshot.
 */
export async function startShipOperation(
    projectId: string,
    request: ShipOperationRequest
): Promise<GitOperation> {
    return api.post<GitOperation>(`${BASE}/${projectId}/git/ship`, request);
}

/**
 * Poll the current state of a running git operation.
 */
export async function getGitOperation(
    projectId: string,
    operationId: string,
    options?: RequestOptions,
    sessionId?: string
): Promise<GitOperation> {
    return api.get<GitOperation>(
        `${BASE}/${projectId}/git/operations/${encodeURIComponent(operationId)}${scopeQs(undefined, sessionId)}`,
        options
    );
}
