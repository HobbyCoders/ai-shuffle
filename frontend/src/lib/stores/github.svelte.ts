/**
 * GitHub Store for AI Hub
 *
 * Manages GitHub integration state including pull requests and workflow runs.
 *
 * Migrated to Svelte 5 runes.
 */

import * as githubApi from '$lib/api/github';
import { createSubscribers } from './store-utils';

// ============================================================================
// Types
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
// Initial State
// ============================================================================

const initialState: GitHubState = {
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

// ============================================================================
// Reactive State
// ============================================================================

let _state = $state<GitHubState>({ ...initialState });

// ============================================================================
// Subscribe shim (centralized via store-utils)
// ============================================================================

const { subscribe, update: _update, notifySubscribers: _notifySubscribers, createDerived } =
	createSubscribers<GitHubState>(
		() => _state,
		(v) => { _state = v; }
	);

// ============================================================================
// Exported Store
// ============================================================================

export const github = {
	get current() {
		return _state;
	},

	subscribe,

	/**
	 * Load GitHub status for a project
	 */
	async loadStatus(projectId: string) {
		_update(s => ({ ...s, projectId, loading: true, error: null }));

		try {
			const status = await githubApi.getGitHubStatus(projectId);

			_update(s => ({
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
			_update(s => ({
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
		if (!_state.projectId) return;

		_update(s => ({ ...s, loadingPRs: true, error: null }));

		try {
			const pullRequests = await githubApi.getPullRequests(
				_state.projectId,
				state
			);
			_update(s => ({ ...s, pullRequests, loadingPRs: false }));
		} catch (e) {
			const error = e as { detail?: string };
			_update(s => ({
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
		if (!_state.projectId) {
			throw new Error('No project selected');
		}

		_update(s => ({ ...s, loading: true, error: null }));

		try {
			const pr = await githubApi.createPullRequest(_state.projectId, data);
			// Refresh pull requests list
			await github.loadPullRequests();
			_update(s => ({ ...s, loading: false }));
			return pr;
		} catch (e) {
			const error = e as { detail?: string };
			_update(s => ({
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
		if (!_state.projectId) {
			throw new Error('No project selected');
		}

		_update(s => ({ ...s, loading: true, error: null }));

		try {
			await githubApi.mergePullRequest(_state.projectId, prNumber, method);
			// Refresh pull requests list
			await github.loadPullRequests();
			_update(s => ({ ...s, loading: false }));
		} catch (e) {
			const error = e as { detail?: string };
			_update(s => ({
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
		if (!_state.projectId) {
			throw new Error('No project selected');
		}

		_update(s => ({ ...s, loading: true, error: null }));

		try {
			await githubApi.closePullRequest(_state.projectId, prNumber);
			// Refresh pull requests list
			await github.loadPullRequests();
			_update(s => ({ ...s, loading: false }));
		} catch (e) {
			const error = e as { detail?: string };
			_update(s => ({
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
		if (!_state.projectId) return;

		_update(s => ({ ...s, loadingRuns: true, error: null }));

		try {
			const workflowRuns = await githubApi.getWorkflowRuns(
				_state.projectId,
				limit
			);
			_update(s => ({ ...s, workflowRuns, loadingRuns: false }));
		} catch (e) {
			const error = e as { detail?: string };
			_update(s => ({
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
		if (!_state.projectId) {
			throw new Error('No project selected');
		}

		_update(s => ({ ...s, loading: true, error: null }));

		try {
			await githubApi.rerunWorkflow(_state.projectId, runId);
			// Refresh workflow runs list
			await github.loadWorkflowRuns();
			_update(s => ({ ...s, loading: false }));
		} catch (e) {
			const error = e as { detail?: string };
			_update(s => ({
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
		_update(s => ({ ...s, error: null }));
	},

	/**
	 * Clear store state
	 */
	clear() {
		_state = { ...initialState };
		_notifySubscribers();
	}
};

// ============================================================================
// Derived stores (via store-utils createDerived)
// ============================================================================

export const githubRepoInfo = createDerived(() => _state.repoInfo);
