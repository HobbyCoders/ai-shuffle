/**
 * GitHub Store for AI Hub
 *
 * Manages GitHub integration state including pull requests and workflow runs.
 */

import { writable, derived } from 'svelte/store';
import * as githubApi from '$lib/api/github';

// Re-export types from API
export type {
    PullRequest,
    WorkflowRun,
    GitHubStatus,
    CreatePullRequestRequest
} from '$lib/api/github';

// ============================================================================
// Store State
// ============================================================================

interface GitHubState {
    projectId: string | null;
    authenticated: boolean;
    repoName: string | null;
    repoInfo: {
        name?: string;
        full_name?: string;
        description?: string;
        default_branch?: string;
        private?: boolean;
        fork?: boolean;
        stargazers_count?: number;
        open_issues_count?: number;
    } | null;
    pullRequests: githubApi.PullRequest[];
    workflowRuns: githubApi.WorkflowRun[];
    loading: boolean;
    loadingPRs: boolean;
    loadingRuns: boolean;
    error: string | null;
}

// ============================================================================
// GitHub Store
// ============================================================================

function createGitHubStore() {
    const { subscribe, set, update } = writable<GitHubState>({
        projectId: null,
        authenticated: false,
        repoName: null,
        repoInfo: null,
        pullRequests: [],
        workflowRuns: [],
        loading: false,
        loadingPRs: false,
        loadingRuns: false,
        error: null
    });

    return {
        subscribe,

        /**
         * Load GitHub status for a project
         */
        async loadStatus(projectId: string) {
            update(s => ({ ...s, projectId, loading: true, error: null }));

            try {
                const status = await githubApi.getGitHubStatus(projectId);

                update(s => ({
                    ...s,
                    authenticated: status.authenticated,
                    repoName: status.repo,
                    repoInfo: status.repo_info,
                    error: status.error,
                    loading: false
                }));

                return status.authenticated;
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    loading: false,
                    error: error.detail || 'Failed to load GitHub status'
                }));
                return false;
            }
        },

        /**
         * Load pull requests for the current project
         */
        async loadPullRequests(state: 'open' | 'closed' | 'merged' | 'all' = 'open') {
            const currentState = getCurrentState();
            if (!currentState.projectId) return;

            update(s => ({ ...s, loadingPRs: true, error: null }));

            try {
                const pullRequests = await githubApi.getPullRequests(
                    currentState.projectId,
                    state
                );
                update(s => ({ ...s, pullRequests, loadingPRs: false }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    loadingPRs: false,
                    error: error.detail || 'Failed to load pull requests'
                }));
            }
        },

        /**
         * Create a new pull request
         */
        async createPullRequest(data: githubApi.CreatePullRequestRequest) {
            const currentState = getCurrentState();
            if (!currentState.projectId) {
                throw new Error('No project selected');
            }

            update(s => ({ ...s, loading: true, error: null }));

            try {
                const pr = await githubApi.createPullRequest(currentState.projectId, data);
                // Refresh pull requests list
                await this.loadPullRequests();
                update(s => ({ ...s, loading: false }));
                return pr;
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    loading: false,
                    error: error.detail || 'Failed to create pull request'
                }));
                throw e;
            }
        },

        /**
         * Merge a pull request
         */
        async mergePullRequest(
            prNumber: number,
            method: 'merge' | 'squash' | 'rebase' = 'merge'
        ) {
            const currentState = getCurrentState();
            if (!currentState.projectId) {
                throw new Error('No project selected');
            }

            update(s => ({ ...s, loading: true, error: null }));

            try {
                await githubApi.mergePullRequest(currentState.projectId, prNumber, method);
                // Refresh pull requests list
                await this.loadPullRequests();
                update(s => ({ ...s, loading: false }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    loading: false,
                    error: error.detail || 'Failed to merge pull request'
                }));
                throw e;
            }
        },

        /**
         * Close a pull request
         */
        async closePullRequest(prNumber: number) {
            const currentState = getCurrentState();
            if (!currentState.projectId) {
                throw new Error('No project selected');
            }

            update(s => ({ ...s, loading: true, error: null }));

            try {
                await githubApi.closePullRequest(currentState.projectId, prNumber);
                // Refresh pull requests list
                await this.loadPullRequests();
                update(s => ({ ...s, loading: false }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    loading: false,
                    error: error.detail || 'Failed to close pull request'
                }));
                throw e;
            }
        },

        /**
         * Load workflow runs for the current project
         */
        async loadWorkflowRuns(limit: number = 10) {
            const currentState = getCurrentState();
            if (!currentState.projectId) return;

            update(s => ({ ...s, loadingRuns: true, error: null }));

            try {
                const workflowRuns = await githubApi.getWorkflowRuns(
                    currentState.projectId,
                    limit
                );
                update(s => ({ ...s, workflowRuns, loadingRuns: false }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    loadingRuns: false,
                    error: error.detail || 'Failed to load workflow runs'
                }));
            }
        },

        /**
         * Re-run a workflow
         */
        async rerunWorkflow(runId: number) {
            const currentState = getCurrentState();
            if (!currentState.projectId) {
                throw new Error('No project selected');
            }

            update(s => ({ ...s, loading: true, error: null }));

            try {
                await githubApi.rerunWorkflow(currentState.projectId, runId);
                // Refresh workflow runs list
                await this.loadWorkflowRuns();
                update(s => ({ ...s, loading: false }));
            } catch (e) {
                const error = e as { detail?: string };
                update(s => ({
                    ...s,
                    loading: false,
                    error: error.detail || 'Failed to re-run workflow'
                }));
                throw e;
            }
        },

        /**
         * Clear error
         */
        clearError() {
            update(s => ({ ...s, error: null }));
        },

        /**
         * Clear store state
         */
        clear() {
            set({
                projectId: null,
                authenticated: false,
                repoName: null,
                repoInfo: null,
                pullRequests: [],
                workflowRuns: [],
                loading: false,
                loadingPRs: false,
                loadingRuns: false,
                error: null
            });
        }
    };

    // Helper to get current state synchronously
    function getCurrentState(): GitHubState {
        let state: GitHubState = {
            projectId: null,
            authenticated: false,
            repoName: null,
            repoInfo: null,
            pullRequests: [],
            workflowRuns: [],
            loading: false,
            loadingPRs: false,
            loadingRuns: false,
            error: null
        };
        subscribe(s => { state = s; })();
        return state;
    }
}

export const github = createGitHubStore();

// ============================================================================
// Derived Stores
// ============================================================================

export const githubAuthenticated = derived(github, $gh => $gh.authenticated);
export const githubRepoName = derived(github, $gh => $gh.repoName);
export const githubRepoInfo = derived(github, $gh => $gh.repoInfo);
export const pullRequests = derived(github, $gh => $gh.pullRequests);
export const workflowRuns = derived(github, $gh => $gh.workflowRuns);
export const githubLoading = derived(github, $gh => $gh.loading);
export const githubLoadingPRs = derived(github, $gh => $gh.loadingPRs);
export const githubLoadingRuns = derived(github, $gh => $gh.loadingRuns);
export const githubError = derived(github, $gh => $gh.error);

// Derived stores for PR states
export const openPullRequests = derived(github, $gh =>
    $gh.pullRequests.filter(pr => pr.state === 'open')
);

export const mergedPullRequests = derived(github, $gh =>
    $gh.pullRequests.filter(pr => pr.state === 'merged')
);

// Derived store for active/pending workflow runs
export const activeWorkflowRuns = derived(github, $gh =>
    $gh.workflowRuns.filter(run => run.status === 'in_progress' || run.status === 'queued')
);
