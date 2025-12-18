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
}

// ============================================================================
// Branch Types
// ============================================================================

export interface Branch {
    name: string;
    current: boolean;
    remote: boolean;
    upstream?: string;
    ahead?: number;
    behind?: number;
    lastCommit?: string;
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

export interface CommitGraphResponse {
    commits: CommitNode[];
    total: number;
}

// ============================================================================
// Worktree Types
// ============================================================================

export interface Worktree {
    id: string;
    repository_id: string;
    session_id: string | null;
    branch_name: string;
    worktree_path: string;
    base_branch: string | null;
    status: string;
    created_at: string;
}

export interface CreateWorktreeRequest {
    branch_name: string;
    create_new?: boolean;
    base_branch?: string;
    profile_id?: string;
}

// ============================================================================
// Git Status API
// ============================================================================

/**
 * Get the git status for a project
 */
export async function getGitStatus(projectId: string): Promise<GitStatus> {
    return api.get<GitStatus>(`${BASE}/${projectId}/git/status`);
}

// ============================================================================
// Branch API
// ============================================================================

export interface BranchListResponse {
    branches: Branch[];
    current_branch: string | null;
}

/**
 * Get all branches for a project
 */
export async function getBranches(projectId: string): Promise<Branch[]> {
    const response = await api.get<BranchListResponse>(`${BASE}/${projectId}/git/branches`);
    return response.branches;
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
export async function checkout(projectId: string, ref: string): Promise<{ success: boolean; message: string }> {
    return api.post<{ success: boolean; message: string }>(`${BASE}/${projectId}/git/checkout`, { ref });
}

/**
 * Fetch from remote
 */
export async function fetchRemote(projectId: string): Promise<{ success: boolean; message: string }> {
    return api.post<{ success: boolean; message: string }>(`${BASE}/${projectId}/git/fetch`);
}

// ============================================================================
// Commit Graph API
// ============================================================================

/**
 * Get the commit graph for a project
 */
export async function getCommitGraph(projectId: string, limit?: number): Promise<CommitGraphResponse> {
    const params = limit ? `?limit=${limit}` : '';
    return api.get<CommitGraphResponse>(`${BASE}/${projectId}/git/graph${params}`);
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
export async function createWorktree(projectId: string, data: CreateWorktreeRequest): Promise<Worktree> {
    return api.post<Worktree>(`${BASE}/${projectId}/git/worktrees`, data);
}

/**
 * Delete a worktree
 */
export async function deleteWorktree(projectId: string, worktreeId: string): Promise<void> {
    await api.delete(`${BASE}/${projectId}/git/worktrees/${worktreeId}`);
}

/**
 * Link a worktree to a session
 */
export async function linkWorktreeSession(
    projectId: string,
    worktreeId: string,
    sessionId: string
): Promise<Worktree> {
    return api.patch<Worktree>(`${BASE}/${projectId}/git/worktrees/${worktreeId}`, { session_id: sessionId });
}

/**
 * Unlink a worktree from its session
 */
export async function unlinkWorktreeSession(projectId: string, worktreeId: string): Promise<Worktree> {
    return api.patch<Worktree>(`${BASE}/${projectId}/git/worktrees/${worktreeId}`, { session_id: null });
}
