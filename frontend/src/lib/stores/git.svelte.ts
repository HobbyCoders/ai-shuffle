/**
 * Git Store for AI Hub
 *
 * Manages git repository state including branches, commits, and worktrees.
 * Features per-key caching for instant tab switches and deferred remote fetch.
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import * as gitApi from '$lib/api/git';
import { untrack } from 'svelte';

// Re-export types from API
export type {
	Branch,
	CommitNode,
	Worktree,
	GitStatus,
	CreateWorktreeRequest,
	DiffStats,
	PullResponse
} from '$lib/api/git';

// ============================================================================
// Store State
// ============================================================================

export type GitMode = 'project' | 'worktree';

interface GitState {
	projectId: string | null;
	worktreeId: string | null;
	mode: GitMode;
	status: gitApi.GitStatus | null;
	branches: gitApi.Branch[];
	commits: gitApi.CommitNode[];
	worktrees: gitApi.Worktree[];
	diffStats: gitApi.DiffStats | null;
	loading: boolean;
	error: string | null;
}

// Subset of GitState that we cache per key
interface CachedState {
	status: gitApi.GitStatus | null;
	branches: gitApi.Branch[];
	commits: gitApi.CommitNode[];
	worktrees: gitApi.Worktree[];
	diffStats: gitApi.DiffStats | null;
}

const EMPTY_STATE: GitState = {
	projectId: null,
	worktreeId: null,
	mode: 'project',
	status: null,
	branches: [],
	commits: [],
	worktrees: [],
	diffStats: null,
	loading: false,
	error: null
};

const MAX_CACHE_ENTRIES = 10;

// ============================================================================
// Reactive State
// ============================================================================

let _state = $state<GitState>({ ...EMPTY_STATE });

// ============================================================================
// Subscribe shim
// ============================================================================

type Subscriber = (value: GitState) => void;

const _subscribers = new Set<Subscriber>();

function _notifySubscribers() {
	untrack(() => { for (const fn of _subscribers) fn(_state); });
}

function _update(fn: (s: GitState) => GitState) {
	_state = fn(untrack(() => _state));
	_notifySubscribers();
}

function _set(newState: GitState) {
	_state = newState;
	_notifySubscribers();
}

// ============================================================================
// Internal state (non-reactive)
// ============================================================================

let _loadedKey: string | null = null;
let _loadInFlight: string | null = null;
let _bgFetchRunning = false;

const _cache = new Map<string, CachedState>();

function _makeKey(projectId: string, worktreeId?: string | null): string {
	return worktreeId ? `${projectId}:${worktreeId}` : projectId;
}

function _saveToCache(key: string, state: CachedState) {
	if (_cache.size >= MAX_CACHE_ENTRIES && !_cache.has(key)) {
		const oldest = _cache.keys().next().value;
		if (oldest !== undefined) _cache.delete(oldest);
	}
	_cache.set(key, state);
}

function _restoreFromCache(key: string): CachedState | null {
	const cached = _cache.get(key);
	if (!cached) return null;
	_cache.delete(key);
	_cache.set(key, cached);
	return cached;
}

/**
 * Core load logic shared by loadRepository and loadForWorktree.
 */
async function _loadCore(
	key: string,
	projectId: string,
	worktreeId: string | undefined,
	mode: GitMode
) {
	const cached = _restoreFromCache(key);
	_update((s) => ({
		...s,
		projectId,
		worktreeId: worktreeId ?? null,
		mode,
		...cached,
		loading: true,
		error: null
	}));

	try {
		const status = await gitApi.getGitStatus(projectId, worktreeId);
		if (_loadInFlight !== key) return;

		if (status.is_git_repo) {
			const [branches, graphResponse, worktrees, diffStats] = await Promise.all([
				gitApi.getBranches(projectId, true, worktreeId),
				gitApi.getCommitGraph(projectId, 50),
				gitApi.getWorktrees(projectId),
				gitApi.getDiffStats(projectId, true, worktreeId).catch(() => null)
			]);
			if (_loadInFlight !== key) return;

			const newState: CachedState = {
				status,
				branches,
				commits: graphResponse.commits,
				worktrees,
				diffStats: diffStats ?? null
			};

			_update((s) => ({
				...s,
				...newState,
				loading: false
			}));

			_loadedKey = key;
			_saveToCache(key, newState);

			_backgroundFetch(key, projectId, worktreeId);
		} else {
			_update((s) => ({ ...s, status, loading: false }));
			_loadedKey = key;
		}
	} catch (e) {
		if (_loadInFlight !== key) return;
		console.error('Failed to load git repository:', e);
		const error = e as { detail?: string; message?: string };
		_update((s) => ({
			...s,
			loading: false,
			error: error.detail || error.message || 'Failed to load repository'
		}));
	} finally {
		if (_loadInFlight === key) _loadInFlight = null;
	}
}

/**
 * Background fetch from remote + silent refresh of status/branches.
 */
function _backgroundFetch(key: string, projectId: string, worktreeId?: string) {
	if (_bgFetchRunning) return;
	_bgFetchRunning = true;

	(async () => {
		try {
			await gitApi.fetchRemote(projectId, worktreeId);

			if (_loadedKey !== key) return;

			const [status, branches] = await Promise.all([
				gitApi.getGitStatus(projectId, worktreeId),
				gitApi.getBranches(projectId, true, worktreeId)
			]);

			const cached = _cache.get(key);
			if (cached) {
				cached.status = status;
				cached.branches = branches;
			}

			if (_loadedKey === key && !_loadInFlight) {
				_update((s) => ({ ...s, status, branches }));
			}
		} catch {
			// Silent -- background refresh is non-critical
		} finally {
			_bgFetchRunning = false;
		}
	})();
}

// ============================================================================
// Exported Store
// ============================================================================

export const git = {
	get current() {
		return _state;
	},

	subscribe(fn: Subscriber) {
		_subscribers.add(fn);
		untrack(() => fn(_state));
		return () => {
			_subscribers.delete(fn);
		};
	},

	async loadRepository(projectId: string, _forceReload = false) {
		const key = _makeKey(projectId);
		if (_loadInFlight === key) return;
		_loadInFlight = key;
		await _loadCore(key, projectId, undefined, 'project');
	},

	async loadForWorktree(projectId: string, worktreeId: string, _forceReload = false) {
		const key = _makeKey(projectId, worktreeId);
		if (_loadInFlight === key) return;
		_loadInFlight = key;
		await _loadCore(key, projectId, worktreeId, 'worktree');
	},

	restoreCacheImmediate(
		projectId: string,
		worktreeId?: string | null,
		mode: GitMode = 'project'
	) {
		const key = _makeKey(projectId, worktreeId ?? undefined);
		_loadedKey = key;
		const cached = _cache.get(key);
		if (cached) {
			_set({
				projectId,
				worktreeId: worktreeId ?? null,
				mode,
				...cached,
				loading: true,
				error: null
			});
		} else {
			_set({
				...EMPTY_STATE,
				projectId,
				worktreeId: worktreeId ?? null,
				mode,
				loading: true,
				error: null
			});
		}
	},

	clearRepository() {
		_loadedKey = null;
		_set({ ...EMPTY_STATE });
	},

	async refreshStatus() {
		if (!_state.projectId) return;
		const key = _makeKey(_state.projectId, _state.worktreeId ?? undefined);

		try {
			const status = await gitApi.getGitStatus(
				_state.projectId,
				_state.worktreeId ?? undefined
			);
			if (_loadedKey !== key) return;
			_update((s) => ({ ...s, status }));
		} catch (e) {
			if (_loadedKey !== key) return;
			const error = e as { detail?: string };
			_update((s) => ({ ...s, error: error.detail || 'Failed to refresh status' }));
		}
	},

	async loadBranches() {
		if (!_state.projectId) return;
		const key = _makeKey(_state.projectId, _state.worktreeId ?? undefined);

		try {
			const branches = await gitApi.getBranches(
				_state.projectId,
				true,
				_state.worktreeId ?? undefined
			);
			if (_loadedKey !== key) return;
			_update((s) => ({ ...s, branches }));
		} catch (e) {
			if (_loadedKey !== key) return;
			const error = e as { detail?: string };
			_update((s) => ({ ...s, error: error.detail || 'Failed to load branches' }));
		}
	},

	async loadGraph(limit?: number) {
		if (!_state.projectId) return;
		const key = _makeKey(_state.projectId, _state.worktreeId ?? undefined);

		try {
			const graphResponse = await gitApi.getCommitGraph(_state.projectId, limit);
			if (_loadedKey !== key) return;
			_update((s) => ({ ...s, commits: graphResponse.commits }));
		} catch (e) {
			if (_loadedKey !== key) return;
			const error = e as { detail?: string };
			_update((s) => ({ ...s, error: error.detail || 'Failed to load commit graph' }));
		}
	},

	async loadWorktrees() {
		if (!_state.projectId) return;
		const key = _makeKey(_state.projectId, _state.worktreeId ?? undefined);

		try {
			const worktrees = await gitApi.getWorktrees(_state.projectId);
			if (_loadedKey !== key) return;
			_update((s) => ({ ...s, worktrees }));
		} catch (e) {
			if (_loadedKey !== key) return;
			const error = e as { detail?: string };
			_update((s) => ({ ...s, error: error.detail || 'Failed to load worktrees' }));
		}
	},

	async createBranch(name: string, startPoint?: string) {
		if (!_state.projectId) return;

		_update((s) => ({ ...s, loading: true, error: null }));

		try {
			await gitApi.createBranch(_state.projectId, name, startPoint);
			await git.loadBranches();
			await git.refreshStatus();
			_update((s) => ({ ...s, loading: false }));
		} catch (e) {
			const error = e as { detail?: string };
			_update((s) => ({
				...s,
				loading: false,
				error: error.detail || 'Failed to create branch'
			}));
			throw e;
		}
	},

	async deleteBranch(name: string) {
		if (!_state.projectId) return;

		_update((s) => ({ ...s, loading: true, error: null }));

		try {
			await gitApi.deleteBranch(_state.projectId, name);
			await git.loadBranches();
			_update((s) => ({ ...s, loading: false }));
		} catch (e) {
			const error = e as { detail?: string };
			_update((s) => ({
				...s,
				loading: false,
				error: error.detail || 'Failed to delete branch'
			}));
			throw e;
		}
	},

	async checkout(ref: string) {
		if (!_state.projectId) return;

		_update((s) => ({ ...s, loading: true, error: null }));

		try {
			await gitApi.checkout(_state.projectId, ref);
			await git.loadBranches();
			await git.refreshStatus();
			await git.loadGraph();
			_update((s) => ({ ...s, loading: false }));
		} catch (e) {
			const error = e as { detail?: string };
			_update((s) => ({
				...s,
				loading: false,
				error: error.detail || 'Failed to checkout'
			}));
			throw e;
		}
	},

	async fetch() {
		if (!_state.projectId) return;

		_update((s) => ({ ...s, loading: true, error: null }));

		try {
			await gitApi.fetchRemote(_state.projectId, _state.worktreeId ?? undefined);
			await git.loadBranches();
			await git.loadGraph();
			_update((s) => ({ ...s, loading: false }));
		} catch (e) {
			const error = e as { detail?: string };
			_update((s) => ({
				...s,
				loading: false,
				error: error.detail || 'Failed to fetch'
			}));
			throw e;
		}
	},

	async pull(
		strategy: 'merge' | 'rebase' = 'merge',
		autoStash: boolean = false
	): Promise<gitApi.PullResponse> {
		if (!_state.projectId) throw new Error('No project selected');

		try {
			const response = await gitApi.pullFromRemote(
				_state.projectId,
				{ strategy, auto_stash: autoStash },
				_state.worktreeId ?? undefined
			);

			await Promise.all([
				git.refreshStatus(),
				git.loadBranches(),
				git.refreshDiffStats(),
				git.loadGraph()
			]);

			return response;
		} catch (e) {
			const error = e as { detail?: string };
			_update((s) => ({
				...s,
				error: error.detail || 'Failed to pull'
			}));
			throw e;
		}
	},

	async createWorktree(
		branchName: string,
		createNew?: boolean,
		baseBranch?: string,
		profileId?: string
	): Promise<gitApi.Worktree | undefined> {
		if (!_state.projectId) return undefined;

		_update((s) => ({ ...s, loading: true, error: null }));

		try {
			const response = await gitApi.createWorktree(_state.projectId, {
				branch_name: branchName,
				create_new_branch: createNew,
				base_branch: baseBranch,
				profile_id: profileId
			});
			await git.loadWorktrees();
			await git.loadBranches();
			_update((s) => ({ ...s, loading: false }));
			return response.worktree || undefined;
		} catch (e) {
			const error = e as { detail?: string };
			_update((s) => ({
				...s,
				loading: false,
				error: error.detail || 'Failed to create worktree'
			}));
			throw e;
		}
	},

	async deleteWorktree(worktreeId: string, keepBranch: boolean = true) {
		if (!_state.projectId) return;

		_update((s) => ({ ...s, loading: true, error: null }));

		try {
			await gitApi.deleteWorktree(_state.projectId, worktreeId, keepBranch);
			await git.loadWorktrees();
			await git.loadBranches();
			_update((s) => ({ ...s, loading: false }));
		} catch (e) {
			const error = e as { detail?: string };
			_update((s) => ({
				...s,
				loading: false,
				error: error.detail || 'Failed to delete worktree'
			}));
			throw e;
		}
	},

	async refreshDiffStats() {
		if (!_state.projectId) return;
		const key = _makeKey(_state.projectId, _state.worktreeId ?? undefined);

		try {
			const diffStats = await gitApi.getDiffStats(
				_state.projectId,
				true,
				_state.worktreeId ?? undefined
			);
			if (_loadedKey !== key) return;
			_update((s) => ({ ...s, diffStats }));
		} catch (e) {
			if (_loadedKey !== key) return;
			console.debug('Failed to refresh diff stats:', e);
		}
	},

	async commitChanges(
		message: string,
		includeUnstaged: boolean = true
	): Promise<gitApi.CommitResponse> {
		if (!_state.projectId) throw new Error('No project selected');

		try {
			const response = await gitApi.commitChanges(
				_state.projectId,
				{
					message,
					include_unstaged: includeUnstaged
				},
				_state.worktreeId ?? undefined
			);

			await Promise.all([git.refreshStatus(), git.refreshDiffStats()]);

			return response;
		} catch (e) {
			const error = e as { detail?: string };
			_update((s) => ({
				...s,
				error: error.detail || 'Failed to commit changes'
			}));
			throw e;
		}
	},

	async pushCommits(
		branch?: string,
		setUpstream: boolean = false,
		force: boolean = false
	): Promise<{ success: boolean; message: string }> {
		if (!_state.projectId) throw new Error('No project selected');

		try {
			const response = await gitApi.pushCommits(
				_state.projectId,
				{
					branch,
					set_upstream: setUpstream,
					force
				},
				_state.worktreeId ?? undefined
			);

			await git.refreshStatus();

			return response;
		} catch (e) {
			const error = e as { detail?: string };
			_update((s) => ({
				...s,
				error: error.detail || 'Failed to push commits'
			}));
			throw e;
		}
	},

	clearError() {
		_update((s) => ({ ...s, error: null }));
	},

	clear() {
		_loadedKey = null;
		_set({ ...EMPTY_STATE });
	}
};

// ============================================================================
// Derived stores with subscribe shim
// ============================================================================

function createDerived<T>(getter: () => T) {
	return {
		get current() {
			return getter();
		},
		subscribe(fn: (value: T) => void) {
			const handler: Subscriber = () => fn(getter());
			_subscribers.add(handler);
			untrack(() => fn(getter()));
			return () => {
				_subscribers.delete(handler);
			};
		}
	};
}

export const branches = createDerived(() => _state.branches);
export const commits = createDerived(() => _state.commits);
export const worktrees = createDerived(() => _state.worktrees);
export const gitStatus = createDerived(() => _state.status);
export const diffStats = createDerived(() => _state.diffStats);
export const gitLoading = createDerived(() => _state.loading);
export const gitError = createDerived(() => _state.error);
export const gitMode = createDerived(() => _state.mode);

export const currentBranch = createDerived(
	() => _state.branches.find((b) => b.current) || null
);

export const localBranches = createDerived(() => _state.branches.filter((b) => !b.remote));

export const remoteBranches = createDerived(() => _state.branches.filter((b) => b.remote));
