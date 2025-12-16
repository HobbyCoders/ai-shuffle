/**
 * GitHub API client for AI Hub
 *
 * Provides functions for interacting with GitHub through the backend API.
 */

import { api } from './client';

const BASE = '/api/v1/projects';

// ============================================================================
// Types
// ============================================================================

export interface PullRequest {
    number: number;
    title: string;
    body: string;
    state: string;
    head_branch: string;
    base_branch: string;
    author: string;
    url: string;
    created_at: string;
    updated_at: string;
    mergeable: boolean | null;
}

export interface WorkflowRun {
    id: number;
    name: string;
    status: string;
    conclusion: string | null;
    branch: string;
    event: string;
    url: string;
    created_at: string;
}

export interface GitHubStatus {
    authenticated: boolean;
    repo: string | null;
    repo_info: {
        name?: string;
        full_name?: string;
        description?: string;
        default_branch?: string;
        private?: boolean;
        fork?: boolean;
        stargazers_count?: number;
        open_issues_count?: number;
    } | null;
    error: string | null;
}

export interface CreatePullRequestRequest {
    title: string;
    body?: string;
    head: string;
    base: string;
}

export interface MergePullRequestRequest {
    method?: 'merge' | 'squash' | 'rebase';
}

// ============================================================================
// Status API
// ============================================================================

/**
 * Get GitHub status for a project (authentication, repo info)
 */
export async function getGitHubStatus(projectId: string): Promise<GitHubStatus> {
    return api.get<GitHubStatus>(`${BASE}/${projectId}/github/status`);
}

// ============================================================================
// Pull Request API
// ============================================================================

/**
 * List pull requests for a project
 */
export async function getPullRequests(
    projectId: string,
    state: 'open' | 'closed' | 'merged' | 'all' = 'open',
    limit: number = 30
): Promise<PullRequest[]> {
    return api.get<PullRequest[]>(`${BASE}/${projectId}/github/pulls?state=${state}&limit=${limit}`);
}

/**
 * Get a specific pull request
 */
export async function getPullRequest(projectId: string, number: number): Promise<PullRequest> {
    return api.get<PullRequest>(`${BASE}/${projectId}/github/pulls/${number}`);
}

/**
 * Create a new pull request
 */
export async function createPullRequest(
    projectId: string,
    data: CreatePullRequestRequest
): Promise<PullRequest> {
    return api.post<PullRequest>(`${BASE}/${projectId}/github/pulls`, data);
}

/**
 * Merge a pull request
 */
export async function mergePullRequest(
    projectId: string,
    number: number,
    method: 'merge' | 'squash' | 'rebase' = 'merge'
): Promise<{ status: string; message: string }> {
    return api.post<{ status: string; message: string }>(
        `${BASE}/${projectId}/github/pulls/${number}/merge`,
        { method }
    );
}

/**
 * Close a pull request without merging
 */
export async function closePullRequest(
    projectId: string,
    number: number
): Promise<{ status: string; message: string }> {
    return api.delete<{ status: string; message: string }>(
        `${BASE}/${projectId}/github/pulls/${number}`
    ) as Promise<{ status: string; message: string }>;
}

// ============================================================================
// GitHub Actions API
// ============================================================================

/**
 * List recent workflow runs
 */
export async function getWorkflowRuns(
    projectId: string,
    limit: number = 10
): Promise<WorkflowRun[]> {
    return api.get<WorkflowRun[]>(`${BASE}/${projectId}/github/actions?limit=${limit}`);
}

/**
 * Get a specific workflow run
 */
export async function getWorkflowRun(projectId: string, runId: number): Promise<WorkflowRun> {
    return api.get<WorkflowRun>(`${BASE}/${projectId}/github/actions/${runId}`);
}

/**
 * Re-run a workflow
 */
export async function rerunWorkflow(
    projectId: string,
    runId: number
): Promise<{ status: string; message: string }> {
    return api.post<{ status: string; message: string }>(
        `${BASE}/${projectId}/github/actions/${runId}/rerun`
    );
}
