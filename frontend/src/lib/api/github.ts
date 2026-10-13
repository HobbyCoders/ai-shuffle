/**
 * GitHub API client for AI Hub
 *
 * Provides functions for interacting with GitHub through the backend API.
 */

import { api } from './client';

const BASE = '/projects';

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
    draft?: boolean;
    session_id?: string;
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
// Cross-project pull request inbox
// ============================================================================

export type InboxPrState = 'open' | 'draft' | 'merged' | 'closed';
export type InboxReview = 'approved' | 'changes_requested' | 'review_required' | 'none';
export type InboxChecks = 'passing' | 'failing' | 'pending' | 'none';

export interface InboxPullRequest {
    repo: string;
    number: number;
    title: string;
    url: string;
    author: string;
    head_branch: string;
    base_branch: string;
    state: InboxPrState;
    review: InboxReview;
    checks: InboxChecks;
    updated_at: string;
    projects: { id: string; name: string }[];
}

export interface PullRequestInbox {
    items: InboxPullRequest[];
    /** Projects (or repositories) that could not be read, with why. */
    skipped: { project_id?: string | null; project_name?: string | null; repo?: string | null; reason: string }[];
    repos: string[];
}

/**
 * Pull requests across every project's GitHub repository, newest activity
 * first. `state` is gh's filter: open (includes drafts), closed, merged, all.
 */
export async function getPullRequestInbox(
    state: 'open' | 'closed' | 'merged' | 'all' = 'open',
    limit = 30
): Promise<PullRequestInbox> {
    return api.get<PullRequestInbox>(`/github/inbox?state=${state}&limit=${limit}`);
}

// ============================================================================
// The connected account's repositories (clone picker)
// ============================================================================

export interface GitHubRepo {
    full_name: string;
    name: string;
    owner: string;
    private: boolean;
    fork: boolean;
    archived: boolean;
    description: string | null;
    /** HTTPS clone URL; private repos authenticate through the server's gh sign-in. */
    clone_url: string;
    html_url: string;
    default_branch: string | null;
    /** Last push (or update) time, ISO 8601. */
    updated_at: string | null;
}

export interface GitHubRepoPage {
    /** False when GitHub is not signed in on this server. */
    connected: boolean;
    items: GitHubRepo[];
    total: number;
    page: number;
    per_page: number;
    has_more: boolean;
    /** The account has more repositories than were fetched; the oldest are left out. */
    truncated: boolean;
}

/**
 * Repositories the server's GitHub sign-in can clone. Search and paging run
 * on the server's cached list; `refresh` refetches it from GitHub.
 */
export async function listGitHubRepos(
    params: { q?: string; page?: number; perPage?: number; refresh?: boolean } = {},
    options?: { signal?: AbortSignal }
): Promise<GitHubRepoPage> {
    const qs = new URLSearchParams();
    if (params.q?.trim()) qs.set('q', params.q.trim());
    qs.set('page', String(params.page ?? 1));
    qs.set('per_page', String(params.perPage ?? 30));
    if (params.refresh) qs.set('refresh', 'true');
    return api.get<GitHubRepoPage>(`/github/repos?${qs}`, options);
}
