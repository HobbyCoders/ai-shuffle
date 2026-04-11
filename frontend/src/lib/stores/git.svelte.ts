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
import { isApiError } from '$lib/api/client';
import { untrack } from 'svelte';

// Re-export types from API
export type {
	Branch,
	CommitNode,
	Worktree,
	GitStatus,
	CreateWorktreeRequest,
	DiffStats,
	PullResponse,
	FileChangeInfo,
	ChangedFilesResponse,
	FileDiffResponse,
	OperationResponse
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
	changedFiles: gitApi.FileChangeInfo[] | null;
	changedFilesLoading: boolean;
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
	changedFiles: gitApi.FileChangeInfo[] | null;
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
	changedFiles: null,
	changedFilesLoading: false,
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
// Shallow equality helpers — prevent no-op poll updates from triggering
// subscriber cascades and Svelte re-renders when data hasn't changed.
// ============================================================================

function _shallowEqual<T extends Record<string, unknown>>(a: T | null, b: T | null): boolean {
	if (a === b) return true;
	if (!a || !b) return false;
	const keysA = Object.keys(a);
	const keysB = Object.keys(b);
	if (keysA.length !== keysB.length) return false;
	for (const key of keysA) {
		if (a[key] !== b[key]) return false;
	}
	return true;
}

// ============================================================================
// Internal state (non-reactive)
// ============================================================================

let _loadedKey: string | null = null;
let _loadInFlight: string | null = null;
let _bgFetchRunning = false;

// In-flight guards for poll-triggered refreshes to prevent HTTP request accumulation.
// Without these, the git poller (2–5s interval) can queue overlapping requests
// that saturate the browser's connection pool (6 per host), causing commit/push
// operations to hang waiting for a free connection.
let _statusInFlight = false;
let _diffStatsInFlight = false;
let _branchesInFlight = false;
let _changedFilesInFlight = false;

// AbortControllers for poll-triggered fetches so they can be cancelled when
// an operation starts or the component unmounts, freeing browser connections.
let _statusAbort: AbortController | null = null;
let _diffStatsAbort: AbortController | null = null;
let _branchesAbort: AbortController | null = null;

// Timeout (ms) for poll-triggered git fetches. If a git status/diff call takes
// longer than this, abort it to free the browser connection for higher-priority
// requests (commits, pushes). The next poll cycle will retry.
const GIT_POLL_FETCH_TIMEOUT_MS = 8_000;

// Flag to pause polling during commit/push/PR operations.
// Operations set this to true at start and false at end, so the poller
// knows not to fire competing HTTP requests.
let _operationInProgress = false;

// Flag set when a polling request returns 404 for a worktree that no longer
// exists. Once set, all poll methods early-return to stop the 404 spam that
// saturates the browser connection pool and causes progressive slowdown.
// Reset when the store loads a new repository/worktree context.
let _worktreeGone = false;

// Exponential backoff for server errors (5xx). When the backend is down or
// overloaded, hammering it every 2-5s with poll requests saturates the
// browser's connection pool (6 per host) and blocks user-initiated actions.
// Tracks consecutive 5xx errors and delays retries exponentially.
const _BACKOFF_BASE_MS = 2_000;
const _BACKOFF_MAX_MS = 60_000;
let _serverErrorCount = 0;
let _serverBackoffUntil = 0;

function _recordServerError(): void {
	_serverErrorCount = Math.min(_serverErrorCount + 1, 10); // cap at ~60s
	const delay = Math.min(_BACKOFF_BASE_MS * Math.pow(2, _serverErrorCount - 1), _BACKOFF_MAX_MS);
	_serverBackoffUntil = Date.now() + delay;
	console.warn(`[git] Server error #${_serverErrorCount}, backing off ${delay}ms`);
}

function _clearServerBackoff(): void {
	if (_serverErrorCount > 0) {
		console.debug(`[git] Server recovered after ${_serverErrorCount} consecutive errors`);
		_serverErrorCount = 0;
		_serverBackoffUntil = 0;
	}
}

function _isBackingOff(): boolean {
	return Date.now() < _serverBackoffUntil;
}

function _isServerError(e: unknown): boolean {
	return isApiError(e) && e.status !== undefined && e.status >= 500;
}

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

/** Sync a subset of current UI state back into the cache for the active key.
 *  Call this after any refresh operation so restoreCacheImmediate() never
 *  serves stale pre-operation data. */
function _syncToCache(fields: Partial<CachedState>) {
	if (!_loadedKey) return;
	const cached = _cache.get(_loadedKey);
	if (cached) Object.assign(cached, fields);
}

/**
 * Check if an error is a 404 for a worktree that no longer exists.
 * When detected, sets _worktreeGone to stop all further polling.
 */
function _checkWorktreeGone(e: unknown): boolean {
	if (_state.worktreeId && isApiError(e) && e.status === 404) {
		_worktreeGone = true;
		console.warn(`[git] Worktree ${_state.worktreeId} no longer exists, stopping git polling`);
		return true;
	}
	return false;
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
	_worktreeGone = false; // Reset on context switch
	_clearServerBackoff(); // Reset backoff on context switch
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
			const [branches, graphResponse, worktrees, diffStats, changedFilesResp] = await Promise.all([
				gitApi.getBranches(projectId, true, worktreeId),
				gitApi.getCommitGraph(projectId, 50),
				gitApi.getWorktrees(projectId),
				gitApi.getDiffStats(projectId, true, worktreeId).catch(() => null),
				gitApi.getChangedFiles(projectId, worktreeId).catch(() => null)
			]);
			if (_loadInFlight !== key) return;

			const newState: CachedState = {
				status,
				branches,
				commits: graphResponse.commits,
				worktrees,
				diffStats: diffStats ?? null,
				changedFiles: changedFilesResp?.files ?? null
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
			// Not a git repo — clear ALL git state so stale cached values
			// (diffStats, branches, etc.) from a previous load don't persist.
			_update((s) => ({
				...s,
				status,
				branches: [],
				commits: [],
				worktrees: [],
				diffStats: null,
				changedFiles: null,
				loading: false
			}));
			_loadedKey = key;
			// Evict any stale cache entry for this project so
			// restoreCacheImmediate() won't resurrect old data.
			_cache.delete(key);
		}
	} catch (e) {
		if (_loadInFlight !== key) return;
		const errObj = e as { detail?: string; message?: string; status?: number };
		console.error('Failed to load git repository:', errObj.detail || errObj.message || e, errObj.status ?? '');
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
				changedFilesLoading: false,
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
		_worktreeGone = false;
		_clearServerBackoff();
		_set({ ...EMPTY_STATE });
	},

	async refreshStatus() {
		if (!_state.projectId) return;
		if (_statusInFlight) return; // Prevent overlapping requests
		if (_worktreeGone) return; // Worktree deleted — stop polling
		if (_isBackingOff()) return; // Server error backoff — skip this cycle
		const key = _makeKey(_state.projectId, _state.worktreeId ?? undefined);

		_statusAbort?.abort();
		const controller = new AbortController();
		_statusAbort = controller;
		const timeout = setTimeout(() => controller.abort(), GIT_POLL_FETCH_TIMEOUT_MS);

		_statusInFlight = true;
		try {
			const status = await gitApi.getGitStatus(
				_state.projectId,
				_state.worktreeId ?? undefined,
				{ signal: controller.signal }
			);
			if (_loadedKey !== key) return;
			_clearServerBackoff();
			// Skip update if status hasn't changed — prevents no-op reactive cascades
			// that cause progressive slowdown from GC pressure during idle polling.
			if (_shallowEqual(status as unknown as Record<string, unknown>, _state.status as unknown as Record<string, unknown>)) return;
			_update((s) => ({ ...s, status }));
			_syncToCache({ status });
		} catch (e) {
			if (controller.signal.aborted) return; // Timeout or manual abort — silently skip
			if (_loadedKey !== key) return;
			if (_checkWorktreeGone(e)) return; // Worktree deleted — stop polling
			if (_isServerError(e)) { _recordServerError(); return; }
			const error = e as { detail?: string };
			_update((s) => ({ ...s, error: error.detail || 'Failed to refresh status' }));
		} finally {
			clearTimeout(timeout);
			_statusInFlight = false;
			if (_statusAbort === controller) _statusAbort = null;
		}
	},

	async loadBranches() {
		if (!_state.projectId) return;
		if (_branchesInFlight) return; // Prevent overlapping requests
		if (_worktreeGone) return; // Worktree deleted — stop polling
		const key = _makeKey(_state.projectId, _state.worktreeId ?? undefined);

		_branchesAbort?.abort();
		const controller = new AbortController();
		_branchesAbort = controller;
		const timeout = setTimeout(() => controller.abort(), GIT_POLL_FETCH_TIMEOUT_MS);

		_branchesInFlight = true;
		try {
			const branches = await gitApi.getBranches(
				_state.projectId,
				true,
				_state.worktreeId ?? undefined,
				{ signal: controller.signal }
			);
			if (_loadedKey !== key) return;
			_update((s) => ({ ...s, branches }));
			_syncToCache({ branches });
		} catch (e) {
			if (controller.signal.aborted) return; // Timeout or manual abort — silently skip
			if (_loadedKey !== key) return;
			if (_checkWorktreeGone(e)) return; // Worktree deleted — stop polling
			const error = e as { detail?: string };
			_update((s) => ({ ...s, error: error.detail || 'Failed to load branches' }));
		} finally {
			clearTimeout(timeout);
			_branchesInFlight = false;
			if (_branchesAbort === controller) _branchesAbort = null;
		}
	},

	async loadGraph(limit?: number) {
		if (!_state.projectId) return;
		const key = _makeKey(_state.projectId, _state.worktreeId ?? undefined);

		try {
			const graphResponse = await gitApi.getCommitGraph(_state.projectId, limit);
			if (_loadedKey !== key) return;
			_update((s) => ({ ...s, commits: graphResponse.commits }));
			_syncToCache({ commits: graphResponse.commits });
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
			_syncToCache({ worktrees });
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
			// Abort any in-flight poll requests so the post-checkout refreshes
			// below aren't blocked by stale _branchesInFlight / _statusInFlight guards.
			// The poller can fire during the checkout API call, setting these flags
			// and causing loadBranches/refreshStatus to return early with stale data.
			_statusAbort?.abort();
			_branchesAbort?.abort();
			_statusAbort = null;
			_branchesAbort = null;
			_statusInFlight = false;
			_branchesInFlight = false;
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
		if (_diffStatsInFlight) return; // Prevent overlapping requests
		if (_worktreeGone) return; // Worktree deleted — stop polling
		if (_isBackingOff()) return; // Server error backoff — skip this cycle
		const key = _makeKey(_state.projectId, _state.worktreeId ?? undefined);

		_diffStatsAbort?.abort();
		const controller = new AbortController();
		_diffStatsAbort = controller;
		const timeout = setTimeout(() => controller.abort(), GIT_POLL_FETCH_TIMEOUT_MS);

		_diffStatsInFlight = true;
		try {
			const diffStats = await gitApi.getDiffStats(
				_state.projectId,
				true,
				_state.worktreeId ?? undefined,
				{ signal: controller.signal }
			);
			if (_loadedKey !== key) return;
			_clearServerBackoff();
			// Skip update if diff stats haven't changed — prevents no-op reactive cascades
			if (_shallowEqual(diffStats as unknown as Record<string, unknown>, _state.diffStats as unknown as Record<string, unknown>)) return;
			_update((s) => ({ ...s, diffStats }));
			_syncToCache({ diffStats });
		} catch (e) {
			if (controller.signal.aborted) return; // Timeout or manual abort — silently skip
			if (_loadedKey !== key) return;
			if (_checkWorktreeGone(e)) return; // Worktree deleted — stop polling
			if (_isServerError(e)) { _recordServerError(); return; }
			console.debug('Failed to refresh diff stats:', e);
		} finally {
			clearTimeout(timeout);
			_diffStatsInFlight = false;
			if (_diffStatsAbort === controller) _diffStatsAbort = null;
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

			await Promise.all([
				git.refreshStatus(),
				git.refreshDiffStats(),
				git.loadBranches()
			]);

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

			// Refresh both status and branches so the Push badge
			// (which reads branch.ahead) reflects the new state.
			await Promise.all([
				git.refreshStatus(),
				git.loadBranches()
			]);

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

	async refreshChangedFiles() {
		if (!_state.projectId) return;
		if (_changedFilesInFlight) return;
		if (_worktreeGone) return; // Worktree deleted — stop polling
		if (_isBackingOff()) return; // Server error backoff — skip this cycle
		const key = _makeKey(_state.projectId, _state.worktreeId ?? undefined);

		_changedFilesInFlight = true;
		_update((s) => ({ ...s, changedFilesLoading: true }));
		try {
			const response = await gitApi.getChangedFiles(
				_state.projectId,
				_state.worktreeId ?? undefined
			);
			if (_loadedKey !== key) return;
			_clearServerBackoff();
			_update((s) => ({ ...s, changedFiles: response.files, changedFilesLoading: false }));

			// Update cache
			const cached = _cache.get(key);
			if (cached) cached.changedFiles = response.files;
		} catch (e) {
			if (_loadedKey !== key) return;
			if (_checkWorktreeGone(e)) return; // Worktree deleted — stop polling
			if (_isServerError(e)) { _recordServerError(); _update((s) => ({ ...s, changedFilesLoading: false })); return; }
			console.debug('Failed to refresh changed files:', e);
			_update((s) => ({ ...s, changedFilesLoading: false }));
		} finally {
			_changedFilesInFlight = false;
		}
	},

	async stageFiles(files: string[]) {
		if (!_state.projectId) return;

		try {
			await gitApi.stageFiles(
				_state.projectId,
				files,
				_state.worktreeId ?? undefined
			);
			// Refresh all related state in parallel
			await Promise.all([
				git.refreshChangedFiles(),
				git.refreshStatus(),
				git.refreshDiffStats()
			]);
		} catch (e) {
			const error = e as { detail?: string };
			_update((s) => ({ ...s, error: error.detail || 'Failed to stage files' }));
			throw e;
		}
	},

	async unstageFiles(files: string[]) {
		if (!_state.projectId) return;

		try {
			await gitApi.unstageFiles(
				_state.projectId,
				files,
				_state.worktreeId ?? undefined
			);
			await Promise.all([
				git.refreshChangedFiles(),
				git.refreshStatus(),
				git.refreshDiffStats()
			]);
		} catch (e) {
			const error = e as { detail?: string };
			_update((s) => ({ ...s, error: error.detail || 'Failed to unstage files' }));
			throw e;
		}
	},

	async discardChanges(files: string[], fileStatuses: Record<string, string>) {
		if (!_state.projectId) return;

		try {
			await gitApi.discardChanges(
				_state.projectId,
				files,
				fileStatuses,
				_state.worktreeId ?? undefined
			);
			await Promise.all([
				git.refreshChangedFiles(),
				git.refreshStatus(),
				git.refreshDiffStats()
			]);
		} catch (e) {
			const error = e as { detail?: string };
			_update((s) => ({ ...s, error: error.detail || 'Failed to discard changes' }));
			throw e;
		}
	},

	clearError() {
		_update((s) => ({ ...s, error: null }));
	},

	clear() {
		_loadedKey = null;
		_set({ ...EMPTY_STATE });
	},

	/**
	 * Whether the current worktree has been detected as deleted (404 from backend).
	 * When true, all polling methods early-return to stop 404 spam.
	 */
	get worktreeGone() {
		return _worktreeGone;
	},

	/**
	 * Whether the store is in exponential backoff due to server errors (5xx).
	 * When true, polling methods silently skip to avoid flooding a down server.
	 */
	get serverBackoff() {
		return _isBackingOff();
	},

	/**
	 * Whether a commit/push/PR operation is currently in progress.
	 * Used by the git poller to skip firing while these operations run,
	 * preventing HTTP request accumulation that saturates the browser
	 * connection pool and causes progressive UI slowdown.
	 */
	get operationInProgress() {
		return _operationInProgress;
	},

	/**
	 * Set the operation-in-progress flag. Call with `true` before starting
	 * a commit/push/PR/pull operation, and `false` when it completes or fails.
	 *
	 * When set to true, aborts any in-flight poll fetches to immediately free
	 * browser connections for the operation's own HTTP requests.
	 */
	setOperationInProgress(value: boolean) {
		_operationInProgress = value;
		if (value) {
			// Abort in-flight poll fetches to free browser connections
			_statusAbort?.abort();
			_diffStatsAbort?.abort();
			_branchesAbort?.abort();
			_statusAbort = null;
			_diffStatsAbort = null;
			_branchesAbort = null;
			// Reset in-flight guards so post-operation refreshes aren't blocked
			_statusInFlight = false;
			_diffStatsInFlight = false;
			_branchesInFlight = false;
		}
	},

	/**
	 * Abort any in-flight git poll requests and clear their guards,
	 * then immediately refresh status, diff stats, and branches.
	 *
	 * Used when streaming ends — a concurrent poll may have started
	 * before the session's final git operations completed, and its
	 * _inFlight guard would silently block the post-streaming refresh.
	 */
	forceRefresh() {
		_statusAbort?.abort();
		_diffStatsAbort?.abort();
		_branchesAbort?.abort();
		_statusAbort = null;
		_diffStatsAbort = null;
		_branchesAbort = null;
		_statusInFlight = false;
		_diffStatsInFlight = false;
		_branchesInFlight = false;
		git.refreshStatus();
		git.refreshDiffStats();
		git.loadBranches();
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

export const changedFiles = createDerived(() => _state.changedFiles);
export const changedFilesLoading = createDerived(() => _state.changedFilesLoading);
