/**
 * GitHub Store for AI Hub
 *
 * Manages GitHub integration state including repository info and pull requests.
 *
 * Migrated to Svelte 5 runes.
 */

import * as githubApi from '$lib/api/github';
import { isApiError } from '$lib/api/client';
import { createSubscribers } from './store-utils';

/**
 * The backend distinguishes "we could not ask GitHub" from "GitHub says no":
 * a `gh` transport failure surfaces as **502**, never as an empty list (finding
 * G2). Treat 502 as UNKNOWN — keep whatever was last known and flag it stale —
 * and only an actual empty success as "there is nothing".
 */
function isGitHubUnavailable(e: unknown): boolean {
	return isApiError(e) && e.status === 502;
}

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
	/** The PR list is the last known-good value; the latest refresh could not
	 *  reach GitHub (502). */
	pullRequestsStale: boolean;
	loading: boolean;
	loadingPRs: boolean;
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
	pullRequestsStale: false,
	loading: false,
	loadingPRs: false,
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
	async loadStatus(projectId: string): Promise<boolean | null> {
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
			// A transport failure is NOT "not authenticated" — return null
			// ("unknown") and leave the previously known repo info in place so a
			// blip can't blank the header's default-branch source.
			const unknown = isGitHubUnavailable(e);
			_update(s => ({
				...s,
				loading: false,
				authenticated: unknown ? s.authenticated : false,
				error: error.detail || 'Failed to load GitHub status'
			}));
			return unknown ? null : false;
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
			_update(s => ({ ...s, pullRequests, pullRequestsStale: false, loadingPRs: false }));
		} catch (e) {
			const error = e as { detail?: string };
			// A 502 means GitHub was unreachable — the previously loaded list is
			// kept (and flagged stale) instead of collapsing to "no pull requests".
			_update(s => ({
				...s,
				loadingPRs: false,
				pullRequestsStale: isGitHubUnavailable(e),
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
	 * Load repository metadata (auth + default branch + full name) at most once
	 * per caller-enforced TTL.
	 *
	 * `/github/status` shells out to `gh`, so this is deliberately a one-shot
	 * lazy load driven by the git store's slow, visibility-gated refresh — never
	 * a poll. Repo metadata is near-static.
	 */
	async ensureRepoInfo(projectId: string): Promise<boolean | null> {
		if (_state.projectId === projectId && _state.repoInfo) return _state.authenticated;
		return github.loadStatus(projectId);
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
