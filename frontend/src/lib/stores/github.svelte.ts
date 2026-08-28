/**
 * GitHub Store for AI Hub
 *
 * Manages GitHub integration state including pull requests and workflow runs.
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

/**
 * The CI verdict for one branch, distilled from the newest workflow run on it.
 *
 * `state` collapses GitHub's (status, conclusion) pair into what the header
 * needs to render: `running` while queued/in-progress, otherwise the run's
 * conclusion bucketed into success / failure / neutral.
 */
export interface BranchChecks {
	branch: string;
	state: 'running' | 'success' | 'failure' | 'neutral';
	/** Raw conclusion (success / failure / cancelled / …), null while running. */
	conclusion: string | null;
	name: string;
	url: string;
	updatedAt: string;
}

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
	/** Newest workflow run for the branch the git header is showing. */
	checks: BranchChecks | null;
	/** `checks` is the last known-good verdict; the latest refresh could not
	 *  reach GitHub (502). Never blank the badge because of a transport blip. */
	checksStale: boolean;
	/** Same for the PR list. */
	pullRequestsStale: boolean;
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
	checks: null,
	checksStale: false,
	pullRequestsStale: false,
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
// Pure helpers
// ============================================================================

/**
 * Pick the newest workflow run on `branch` and collapse it into a BranchChecks
 * verdict. Returns null when the branch has no runs (nothing to show).
 */
export function summarizeBranchChecks(
	runs: githubApi.WorkflowRun[],
	branch: string
): BranchChecks | null {
	let newest: githubApi.WorkflowRun | null = null;
	for (const run of runs) {
		if (run.branch !== branch) continue;
		if (!newest || run.created_at > newest.created_at) newest = run;
	}
	if (!newest) return null;

	const status = (newest.status || '').toLowerCase();
	const conclusion = newest.conclusion ? newest.conclusion.toLowerCase() : null;
	let state: BranchChecks['state'];
	if (status === 'queued' || status === 'in_progress' || status === 'waiting' || status === 'pending') {
		state = 'running';
	} else if (conclusion === 'success') {
		state = 'success';
	} else if (conclusion === 'failure' || conclusion === 'timed_out' || conclusion === 'startup_failure') {
		state = 'failure';
	} else {
		state = 'neutral';
	}

	return {
		branch,
		state,
		conclusion,
		name: newest.name,
		url: newest.url,
		updatedAt: newest.created_at
	};
}

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
	 * Refresh the CI verdict for `branch` from the project's recent workflow runs.
	 *
	 * This is the (previously dead) workflow-runs pathway, now wired to the git
	 * header's CI badge. Called only from the git store's slow refresh, which
	 * runs solely while a git surface is mounted, the tab is visible, and the
	 * branch actually has an open PR — so it adds no idle cost.
	 *
	 * @returns true when a verdict (or a definitive "no runs") landed.
	 */
	async refreshChecks(projectId: string, branch: string): Promise<boolean> {
		_update(s => ({ ...s, projectId }));
		const runs = await github.loadWorkflowRuns();
		// null = the fetch failed. loadWorkflowRuns has already decided whether
		// that was a 502 ("unknown" — keep the last verdict, flag it stale) or a
		// hard error; either way the badge must not collapse into a false "no CI".
		if (!runs) return false;
		// An empty (or branch-less) success IS authoritative: there are no runs.
		_update(s => ({ ...s, checks: summarizeBranchChecks(runs, branch) }));
		return true;
	},

	/**
	 * Load workflow runs for the current project.
	 *
	 * The single fetch behind the CI pathway — `refreshChecks` (the git header's
	 * badge) and `rerunWorkflow` both go through it.
	 *
	 * @returns the runs, or null when GitHub could not be reached.
	 */
	async loadWorkflowRuns(limit: number = 10): Promise<githubApi.WorkflowRun[] | null> {
		if (!_state.projectId) return null;

		_update(s => ({ ...s, loadingRuns: true, error: null }));

		try {
			const workflowRuns = await githubApi.getWorkflowRuns(
				_state.projectId,
				limit
			);
			_update(s => ({ ...s, workflowRuns, checksStale: false, loadingRuns: false }));
			return workflowRuns;
		} catch (e) {
			const error = e as { detail?: string };
			// 502 = "could not ask GitHub" — keep the last known verdict and mark
			// it stale rather than reporting a false "no CI".
			_update(s => ({
				...s,
				loadingRuns: false,
				checksStale: isGitHubUnavailable(e),
				error: error.detail || 'Failed to load workflow runs'
			}));
			return null;
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

/** CI verdict for the branch the git header is showing (null when unknown). */
export const githubChecks = createDerived(() => _state.checks);

/** The CI verdict is a last-known-good value (the latest refresh hit a 502). */
export const githubChecksStale = createDerived(() => _state.checksStale);
