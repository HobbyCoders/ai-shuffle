/**
 * Git Store for AI Hub
 *
 * Manages git repository state including branches, commits, and worktrees.
 *
 * Design notes (see docs/PERFORMANCE_REVIEW_2026-08.md §3.3):
 *
 * - **Cache identity is the checkout, not the session** (G4). Two sessions that
 *   resolve to the same checkout directory share one cache entry, so switching
 *   between them is instant. Within a project a branch is checked out in exactly
 *   one directory, so `project#br:<branch>` identifies the checkout; a scope
 *   (project / worktree / session) is aliased onto that identity as soon as a
 *   status response tells us which branch it resolves to.
 *
 * - **Never reset to empty on a switch** (G4). An unknown context keeps the
 *   last-known values as a *stale placeholder* (`stale: true`,
 *   `hydrated: false`) while it revalidates, so the header greys out the
 *   previous real state instead of flashing "no repo / Init Repo".
 *
 * - **One coherent snapshot** (G4). `gitSnapshot` exposes status + branches +
 *   diff + PR state together with the coherence flags, so the header's primary
 *   action is computed from a single object rather than four independently
 *   arriving fields.
 *
 * - **Per-key request versioning** (G4). Every network write carries a
 *   monotonic sequence number; a response whose sequence is older than the last
 *   applied one for that field is discarded, so a slow in-flight response can
 *   never overwrite a newer one for the same key.
 *
 * - **Real equality suppression** (G7). Status/PR-state arrive in a canonical
 *   flat shape (see `$lib/api/git`), so an unchanged poll genuinely does not
 *   notify subscribers.
 *
 * - **Inline mutation payloads are applied** (G6) instead of being discarded in
 *   favour of 3–5 follow-up refresh requests.
 *
 * - **Local git does not require GitHub auth** (G10c). `githubAvailable` is a
 *   separate, hysteresis-guarded flag; a transient `gh auth status` blip never
 *   clears local repository state.
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import { untrack } from 'svelte';
import * as gitApi from '$lib/api/git';
import { isApiError } from '$lib/api/client';
import { github } from './github.svelte';
import { createSubscribers } from './store-utils';

// Re-export types from API
export type {
	Branch,
	CommitNode,
	Worktree,
	GitStatus,
	DiffStats,
	PullResponse,
	FileChangeInfo,
	ChangedFilesResponse,
	FileDiffResponse,
	OperationResponse,
	PrState,
	OpenPrInfo
} from '$lib/api/git';

// ============================================================================
// Store State
// ============================================================================

type GitMode = 'project' | 'worktree';

interface GitState {
	projectId: string | null;
	worktreeId: string | null;
	/** Active session id — scopes git calls to a v2 branch session's own checkout
	 *  when there is no legacy worktree row (worktreeId null). */
	sessionId: string | null;
	mode: GitMode;
	status: gitApi.GitStatus | null;
	branches: gitApi.Branch[];
	commits: gitApi.CommitNode[];
	worktrees: gitApi.Worktree[];
	diffStats: gitApi.DiffStats | null;
	prState: gitApi.PrState | null;
	changedFiles: gitApi.FileChangeInfo[] | null;
	changedFilesLoading: boolean;
	loading: boolean;
	error: string | null;
	/** Identity of the checkout the data above belongs to (see module docs). */
	contextKey: string | null;
	/** The displayed values are a placeholder being revalidated: either carried
	 *  over from another context, or a cache hit whose refresh is in flight. */
	stale: boolean;
	/** A complete, coherent load has landed for the CURRENT context. While this
	 *  is false the header must keep showing its previous action rather than
	 *  deriving a new one from half-filled state. */
	hydrated: boolean;
	/** The last PR lookup did not complete (backend flag or transport error), so
	 *  `prState.open_pr` is the previous known value, not a fresh verdict. */
	prStateStale: boolean;
	/** GitHub-backed features (PR / CI) are usable. Independent of whether LOCAL
	 *  git works — see `setGithubAuth`. */
	githubAvailable: boolean;
}

// Subset of GitState that we cache per checkout identity
interface CachedState {
	status: gitApi.GitStatus | null;
	branches: gitApi.Branch[];
	commits: gitApi.CommitNode[];
	worktrees: gitApi.Worktree[];
	diffStats: gitApi.DiffStats | null;
	prState: gitApi.PrState | null;
	changedFiles: gitApi.FileChangeInfo[] | null;
	prStateStale: boolean;
}

const EMPTY_STATE: GitState = {
	projectId: null,
	worktreeId: null,
	sessionId: null,
	mode: 'project',
	status: null,
	branches: [],
	commits: [],
	worktrees: [],
	diffStats: null,
	prState: null,
	changedFiles: null,
	changedFilesLoading: false,
	loading: false,
	error: null,
	contextKey: null,
	stale: false,
	hydrated: false,
	prStateStale: false,
	githubAvailable: true
};

const MAX_CACHE_ENTRIES = 10;

// ============================================================================
// Reactive State
// ============================================================================

let _state = $state<GitState>({ ...EMPTY_STATE });

// ============================================================================
// Subscribe shim (centralized via store-utils)
// ============================================================================

const { subscribe, update: _update, set: _set, notifySubscribers: _notifySubscribers, createDerived } =
	createSubscribers<GitState>(
		() => _state,
		(v) => { _state = v; }
	);

// ============================================================================
// Equality helpers — prevent no-op poll updates from triggering subscriber
// cascades and Svelte re-renders when the data genuinely hasn't changed.
// ============================================================================

/** Shallow (primitive-field) equality. Correct for DiffStats, whose three
 *  fields are all numbers. Status and PR state have their own structural
 *  comparators in `$lib/api/git` — a shallow compare on those never matched. */
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

/** Identity of the checkout the store is currently pointed at. */
let _loadedKey: string | null = null;
/** The raw scope (project / worktree / session) that resolved to `_loadedKey`. */
let _loadedScope: string | null = null;
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

// PR state refreshes at a slower cadence than status (gh CLI cost) — track the
// last fetch PER CHECKOUT so one context's fetch can't throttle another's.
const PR_STATE_MIN_INTERVAL_MS = 30_000;
let _prStateInFlight = false;
const _lastPrStateFetch = new Map<string, number>();

// ── Request versioning ──────────────────────────────────────────────────────
// Every network write takes a ticket from a single monotonic counter and is
// applied only if no NEWER response for the same field has already landed. The
// context key check ("is this still the checkout we're looking at?") guards
// across keys; this guards within a key, where two overlapping requests for the
// same field can otherwise complete out of order and snap the UI backwards.
let _requestSeq = 0;
const _appliedSeq = new Map<string, number>();

function _nextSeq(): number {
	return ++_requestSeq;
}

/** Claim the right to write `field`. False when a newer response already won. */
function _acceptSeq(field: string, seq: number): boolean {
	if (seq <= (_appliedSeq.get(field) ?? 0)) return false;
	_appliedSeq.set(field, seq);
	return true;
}

/** Mark `field` as written right now (used when state is applied from a source
 *  other than a tracked request, e.g. an inline mutation payload). */
function _claimSeq(...fields: string[]): void {
	const seq = _nextSeq();
	for (const field of fields) _appliedSeq.set(field, seq);
}

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

// ============================================================================
// Checkout identity + cache
// ============================================================================

const _cache = new Map<string, CachedState>();
/** scope key → resolved checkout identity (learned from status responses). */
const _scopeAlias = new Map<string, string>();

/**
 * The raw scope the caller asked for. Stable for a given
 * project/worktree/session triple, and used only as a lookup into `_scopeAlias`
 * (and as the provisional identity until the alias is learned).
 */
const _remoteSessions = new Set<string>();

function _scopeKey(projectId: string, worktreeId?: string | null, sessionId?: string | null): string {
	if (sessionId && _remoteSessions.has(sessionId)) return `${projectId}#remote:${sessionId}`;
	if (worktreeId) return `${projectId}#wt:${worktreeId}`;
	if (sessionId) return `${projectId}#ses:${sessionId}`;
	return `${projectId}#base`;
}

/** The checkout identity a scope currently resolves to. */
function _identityFor(scope: string): string {
	return _scopeAlias.get(scope) ?? scope;
}

/**
 * Point `scope` at the canonical checkout identity for `branch`.
 *
 * Within a project a branch lives in exactly one checkout directory (git
 * refuses to check the same branch out twice), so the branch names the
 * directory — which is what makes two sessions on the same checkout share one
 * cache entry instead of each flashing empty on switch.
 *
 * Returns the identity the scope now resolves to.
 */
function _learnIdentity(scope: string, projectId: string, branch: string | null | undefined): string {
	const current = _identityFor(scope);
	if (!branch || scope.includes("#remote:")) return current;
	const canonical = `${projectId}#br:${branch}`;
	if (canonical === current) return current;

	// Only fold the PROVISIONAL entry into the canonical one. Re-pointing from
	// one canonical identity to another means the branch changed under us (a
	// checkout); the old branch's entry still describes that branch and must not
	// be moved or dropped.
	const wasProvisional = current === scope;
	_scopeAlias.set(scope, canonical);

	if (wasProvisional) {
		const provisional = _cache.get(current);
		if (provisional) {
			_cache.set(canonical, { ...(_cache.get(canonical) ?? provisional), ...provisional });
			_cache.delete(current);
		}
		const lastPr = _lastPrStateFetch.get(current);
		if (lastPr !== undefined) {
			_lastPrStateFetch.set(canonical, Math.max(lastPr, _lastPrStateFetch.get(canonical) ?? 0));
			_lastPrStateFetch.delete(current);
		}
	}

	if (_loadedKey === current) {
		_loadedKey = canonical;
		if (_state.contextKey === current) _update((s) => ({ ...s, contextKey: canonical }));
	}
	return canonical;
}

function _saveToCache(key: string, state: CachedState) {
	if (_cache.size >= MAX_CACHE_ENTRIES && !_cache.has(key)) {
		const oldest = _cache.keys().next().value;
		if (oldest !== undefined) {
			_cache.delete(oldest);
			_lastPrStateFetch.delete(oldest);
		}
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

/** Snapshot the cacheable slice of the live state. */
function _cacheableSlice(s: GitState): CachedState {
	return {
		status: s.status,
		branches: s.branches,
		commits: s.commits,
		worktrees: s.worktrees,
		diffStats: s.diffStats,
		prState: s.prState,
		changedFiles: s.changedFiles,
		prStateStale: s.prStateStale
	};
}

/**
 * Check if an error is a 404 for a worktree that no longer exists.
 * When detected, stops all further polling AND clears git state to a definitive
 * "no repo" status so the header hides Ship It and other git controls instead of
 * leaving stale is_git_repo=true behind. Worktree sessions are reusable, so a
 * cleaned-up worktree's folder can be gone while its session is reopened.
 */
function _checkWorktreeGone(e: unknown): boolean {
	if (_state.worktreeId && isApiError(e) && e.status === 404) {
		_worktreeGone = true;
		console.warn(`[git] Worktree ${_state.worktreeId} no longer exists, stopping git polling`);
		// Fail closed: drop any stale repo state (status/branches/diff) and evict
		// the cache so restoreCacheImmediate() can't resurrect it on the next switch.
		// This is a POSITIVE finding ("the folder is gone"), not a switch flicker —
		// hydrated stays true so the header renders the no-repo state immediately.
		_claimSeq('status', 'diffStats', 'branches', 'prState', 'changedFiles');
		_update((s) => ({
			...s,
			status: gitApi.notARepoStatus(),
			branches: [],
			commits: [],
			worktrees: [],
			diffStats: null,
			prState: null,
			changedFiles: null,
			loading: false,
			stale: false,
			hydrated: true,
			prStateStale: false
		}));
		if (_loadedKey) {
			_cache.delete(_loadedKey);
			_lastPrStateFetch.delete(_loadedKey);
		}
		return true;
	}
	return false;
}

/**
 * Core load logic shared by loadRepository and loadForWorktree.
 *
 * Lands as ONE atomic, coherent write: status, branches, commits, worktrees,
 * diff stats, changed files and PR state are applied together with
 * `hydrated: true, stale: false`. The header therefore never sees a half-filled
 * snapshot and never walks through an intermediate primary action.
 */
async function _loadCore(
	scope: string,
	projectId: string,
	worktreeId: string | undefined,
	mode: GitMode,
	sessionId?: string | undefined
) {
	_worktreeGone = false; // Reset on context switch
	_clearServerBackoff(); // Reset backoff on context switch
	let key = _identityFor(scope);
	const cached = _restoreFromCache(key);
	_loadedScope = scope;
	_loadedKey = key;
	_update((s) => ({
		...s,
		projectId,
		worktreeId: worktreeId ?? null,
		sessionId: sessionId ?? null,
		mode,
		...(cached ?? {}),
		contextKey: key,
		// A cache hit is real data for THIS checkout; a miss leaves whatever was
		// on screen as a placeholder. Either way we are revalidating.
		stale: true,
		hydrated: cached ? true : s.contextKey === key && s.hydrated,
		loading: true,
		error: null
	}));

	const statusSeq = _nextSeq();
	try {
		const status = await gitApi.getGitStatus(projectId, worktreeId, undefined, sessionId);
		if (_loadInFlight !== scope) return;
		// Resolve the scope onto its real checkout identity now that we know the
		// branch, so sibling sessions on the same checkout share this entry.
		key = _learnIdentity(scope, projectId, status.current_branch);
		_loadedKey = key;

		if (status.is_git_repo) {
			const [branches, graphResponse, worktrees, diffStats, changedFilesResp, prState] = await Promise.all([
				gitApi.getBranches(projectId, true, worktreeId, undefined, sessionId),
				gitApi.getCommitGraph(projectId, 50, undefined, sessionId),
				_remoteSessions.has(sessionId ?? "") ? Promise.resolve([]) : gitApi.getWorktrees(projectId),
				gitApi.getDiffStats(projectId, true, worktreeId, undefined, sessionId).catch(() => null),
				gitApi.getChangedFiles(projectId, worktreeId, sessionId).catch(() => null),
				gitApi.getPrState(projectId, worktreeId ?? undefined, sessionId ?? undefined).catch(() => null)
			]);
			if (_loadInFlight !== scope) return;
			if (prState) _lastPrStateFetch.set(key, Date.now());

			// A failed (or failing) PR lookup must NOT be read as "no open PR":
			// keep whatever we knew for this checkout and flag it stale.
			const prLookupFailed = !prState || prState.lookup_failed;
			const previousPr = cached?.prState ?? null;
			const resolvedPr = prLookupFailed
				? _keepPreviousPrState(previousPr, prState, status.current_branch)
				: prState;

			const newState: CachedState = {
				status,
				branches,
				commits: graphResponse.commits,
				worktrees,
				diffStats: diffStats ?? null,
				prState: resolvedPr,
				changedFiles: changedFilesResp?.files ?? null,
				prStateStale: prLookupFailed
			};

			// One atomic write — see the function docstring.
			_claimSeq('status', 'diffStats', 'branches', 'prState', 'changedFiles', 'commits', 'worktrees');
			_update((s) => ({
				...s,
				...newState,
				contextKey: key,
				stale: false,
				hydrated: true,
				loading: false
			}));

			_saveToCache(key, newState);

			_backgroundFetch(key, projectId, worktreeId, sessionId);
		} else {
			// Not a git repo — a POSITIVE answer, so clear ALL git state (stale
			// cached diffStats/prState/branches from a previous load must not
			// persist) and mark the context hydrated.
			if (!_acceptSeq('status', statusSeq)) return;
			_claimSeq('diffStats', 'branches', 'prState', 'changedFiles');
			_update((s) => ({
				...s,
				status,
				branches: [],
				commits: [],
				worktrees: [],
				diffStats: null,
				prState: null,
				changedFiles: null,
				loading: false,
				contextKey: key,
				stale: false,
				hydrated: true,
				prStateStale: false
			}));
			// Evict any stale cache entry for this checkout so
			// restoreCacheImmediate() won't resurrect old data.
			_cache.delete(key);
			_lastPrStateFetch.delete(key);
		}
	} catch (e) {
		if (_loadInFlight !== scope) return;
		// A 404 on the initial load means the worktree's folder is gone (cleaned-up
		// / reused worktree session). _checkWorktreeGone clears state to a definitive
		// no-repo status so the header hides Ship It rather than showing stale controls.
		if (_checkWorktreeGone(e)) {
			return;
		}
		const errObj = e as { detail?: string; message?: string; status?: number };
		console.error('Failed to load git repository:', errObj.detail || errObj.message || e, errObj.status ?? '');
		const error = e as { detail?: string; message?: string };
		// Keep whatever is on screen (stale) rather than blanking on a transient
		// failure — the next poll cycle revalidates.
		_update((s) => ({
			...s,
			loading: false,
			stale: true,
			error: error.detail || error.message || 'Failed to load repository'
		}));
	} finally {
		if (_loadInFlight === scope) _loadInFlight = null;
	}
}

/**
 * Resolve the PR state to display when the backend could not complete the
 * lookup. Keeps the previous `open_pr` (a failed lookup is not "no PR") unless
 * the branch has moved on, in which case the old PR belongs to another branch
 * and must be dropped.
 */
function _keepPreviousPrState(
	previous: gitApi.PrState | null,
	incoming: gitApi.PrState | null,
	currentBranch: string | null
): gitApi.PrState | null {
	// The backend serves its own last-known-good value when it can (stale=true).
	// Prefer it: it is at least as fresh as ours and matches the branch it
	// was cached for.
	if (incoming?.open_pr || incoming?.merged_head_sha) return incoming;
	if (!previous?.open_pr && !previous?.merged_head_sha) return incoming ?? previous;
	const branch = incoming?.current_branch ?? currentBranch ?? null;
	if (branch && previous.current_branch && branch !== previous.current_branch) {
		// The old PR belongs to another branch — showing it would be worse than
		// showing nothing.
		return incoming ?? { ...previous, open_pr: null, merged_head_sha: null, lookup_failed: true };
	}
	return { ...previous, lookup_failed: true, served_stale: false };
}

/**
 * Background fetch from remote + silent refresh of status/branches.
 */
function _backgroundFetch(key: string, projectId: string, worktreeId?: string, sessionId?: string) {
	if (_bgFetchRunning) return;
	_bgFetchRunning = true;

	(async () => {
		const seq = _nextSeq();
		try {
			await gitApi.fetchRemote(projectId, worktreeId, sessionId);

			if (_loadedKey !== key) return;

			const [status, branches] = await Promise.all([
				gitApi.getGitStatus(projectId, worktreeId, undefined, sessionId),
				gitApi.getBranches(projectId, true, worktreeId, undefined, sessionId)
			]);

			if (_loadedKey !== key) return;
			if (!_acceptSeq('status', seq)) return;
			_acceptSeq('branches', seq);

			const cached = _cache.get(key);
			if (cached) {
				cached.status = status;
				cached.branches = branches;
			}

			if (!_loadInFlight) {
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
// GitHub availability (G10c) — hysteresis so an auth blip never hides git
// ============================================================================
//
// `githubAuthenticated` comes from a 120 s `/auth/status` poll whose
// `gh auth status` subprocess can transiently fail under load. Flipping the
// store on the first false hid the ENTIRE git UI for up to two minutes — even
// though local git needs no GitHub auth at all. So: local state is never
// cleared for auth, and the GitHub-features flag only flips false after two
// consecutive failures spanning at least the grace window.

const GITHUB_AUTH_GRACE_MS = 60_000;
let _githubAuthFirstFailureAt = 0;
let _githubAuthFailures = 0;

// ============================================================================
// Visible-only PR / CI refresh (G3)
// ============================================================================
//
// PR and CI state change on GitHub, not locally — a PR merged by a teammate or
// a check that just went red is invisible until something refetches. This is a
// SLOW refresh that runs only while a git surface is mounted AND the tab is
// visible, with jitter (so many tabs don't herd) and exponential backoff.

const PR_CI_REFRESH_BASE_MS = 45_000;
const PR_CI_REFRESH_JITTER_MS = 10_000; // → 35–55 s
const PR_CI_REFRESH_MAX_MS = 5 * 60_000;
/** Repo metadata is near-static; refresh it at most this often. */
const REPO_INFO_TTL_MS = 10 * 60_000;

let _bgRefreshHolders = 0;
let _bgRefreshTimer: ReturnType<typeof setTimeout> | null = null;
let _bgRefreshFailures = 0;
let _visibilityBound = false;
let _lastRepoInfoLoad = 0;

function _bgRefreshInterval(): number {
	const base = Math.min(
		PR_CI_REFRESH_BASE_MS * Math.pow(2, Math.min(_bgRefreshFailures, 3)),
		PR_CI_REFRESH_MAX_MS
	);
	const jitter = (Math.random() * 2 - 1) * PR_CI_REFRESH_JITTER_MS;
	return Math.max(10_000, base + jitter);
}

function _tabHidden(): boolean {
	return typeof document !== 'undefined' && document.hidden;
}

async function _bgRefreshTick(): Promise<void> {
	if (_bgRefreshHolders === 0 || _tabHidden()) return;
	// Never compete with a user-initiated operation or a dead/backing-off context.
	if (_operationInProgress || _worktreeGone || _isBackingOff()) return;

	const projectId = _state.projectId;
	if (!projectId || _state.status?.is_git_repo !== true) return;

	const ok = await git.refreshPrState();
	if (ok === false) {
		_bgRefreshFailures = Math.min(_bgRefreshFailures + 1, 4);
	} else {
		_bgRefreshFailures = 0;
	}

	if (!_state.githubAvailable || _remoteSessions.has(_state.sessionId ?? "")) return;

	// Repo metadata (default branch, full name) — one call, TTL'd, not a poll.
	const hasGithub = _state.prState?.has_github === true || !!_state.status?.github_repo_name;
	if (hasGithub && Date.now() - _lastRepoInfoLoad > REPO_INFO_TTL_MS) {
		_lastRepoInfoLoad = Date.now();
		const authed = await github.ensureRepoInfo(projectId).catch(() => null);
		// `null` means "could not ask GitHub" (502) — an unknown, not a denial.
		// Only a definitive verdict feeds the availability hysteresis.
		if (typeof authed === 'boolean') git.setGithubAuth(authed);
	}

	// CI: only meaningful (and only worth a `gh` round trip) once there is an
	// open PR whose checks the user is waiting on.
	const branch = _state.status?.current_branch ?? null;
	if (hasGithub && branch && _state.prState?.open_pr) {
		await github.refreshChecks(projectId, branch).catch(() => {});
	}
}

function _scheduleBgRefresh(): void {
	if (_bgRefreshTimer) clearTimeout(_bgRefreshTimer);
	_bgRefreshTimer = null;
	if (_bgRefreshHolders === 0 || _tabHidden()) return;
	_bgRefreshTimer = setTimeout(() => {
		_bgRefreshTimer = null;
		void _bgRefreshTick().finally(_scheduleBgRefresh);
	}, _bgRefreshInterval());
}

function _handleBgVisibility(): void {
	if (_tabHidden()) {
		if (_bgRefreshTimer) clearTimeout(_bgRefreshTimer);
		_bgRefreshTimer = null;
		return;
	}
	if (_bgRefreshHolders > 0) {
		// Returning to a visible tab: catch up once, then resume the slow cadence.
		void _bgRefreshTick().finally(_scheduleBgRefresh);
	}
}

// ============================================================================
// Exported Store
// ============================================================================

export interface GitActionTarget {
	projectId: string;
	worktreeId?: string;
	sessionId?: string;
	head?: string | null;
	base?: string | null;
}

function _targetIsCurrent(target: GitActionTarget): boolean {
	return target.projectId === _state.projectId &&
		(target.sessionId ?? null) === _state.sessionId &&
		(target.worktreeId ?? null) === _state.worktreeId;
}

export const git = {
	captureTarget(): GitActionTarget {
		if (!_state.projectId) throw new Error('No project selected');
		return { projectId: _state.projectId, worktreeId: _state.worktreeId ?? undefined,
			sessionId: _state.sessionId ?? undefined, head: _state.status?.current_branch,
			base: _state.status?.default_branch };
	},

	get current() {
		return _state;
	},

	subscribe,

	setRemoteSession(sessionId: string | undefined, remote: boolean) {
		if (!sessionId) return;
		if (remote) _remoteSessions.add(sessionId);
		else _remoteSessions.delete(sessionId);
	},

	async loadRepository(projectId: string, _forceReload = false, sessionId?: string) {
		const scope = _scopeKey(projectId, undefined, sessionId);
		if (_loadInFlight === scope) return;
		_loadInFlight = scope;
		await _loadCore(scope, projectId, undefined, 'project', sessionId);
	},

	async loadForWorktree(projectId: string, worktreeId: string, _forceReload = false, sessionId?: string) {
		const scope = _scopeKey(projectId, worktreeId, sessionId);
		if (_loadInFlight === scope) return;
		_loadInFlight = scope;
		await _loadCore(scope, projectId, worktreeId, 'worktree', sessionId);
	},

	/**
	 * Point the store at a context synchronously, within the same frame as the
	 * session switch.
	 *
	 * Two sessions on the same checkout resolve to the same identity, so the
	 * switch is instant and lossless. A context we have never loaded KEEPS the
	 * values already on screen and marks them `stale` + not `hydrated` — the
	 * header greys the previous state out instead of flashing an empty repo.
	 */
	restoreCacheImmediate(
		projectId: string,
		worktreeId?: string | null,
		mode: GitMode = 'project',
		sessionId?: string | null,
		branchHint?: string | null
	) {
		const scope = _scopeKey(projectId, worktreeId ?? undefined, sessionId ?? undefined);
		// A caller that already knows the session's branch (the host has it on the
		// session row) resolves the checkout identity in the same frame, so the
		// FIRST visit to a sibling session on a known checkout is instant too —
		// without the hint we only learn the identity from the status response.
		const key = branchHint ? _learnIdentity(scope, projectId, branchHint) : _identityFor(scope);
		_loadedScope = scope;
		_loadedKey = key;
		const cached = _cache.get(key);
		// untrack: this runs synchronously inside component $effects (the session
		// switch effect in MainPanel). Reading _state tracked there while _set
		// replaces it would make that effect re-trigger itself forever
		// (effect_update_depth_exceeded).
		const prev = untrack(() => ({ ..._state }));
		const base = {
			projectId,
			worktreeId: worktreeId ?? null,
			sessionId: sessionId ?? null,
			mode,
			loading: true,
			error: null,
			contextKey: key,
			githubAvailable: prev.githubAvailable
		};
		if (cached) {
			_set({
				...prev,
				...base,
				...cached,
				changedFilesLoading: false,
				stale: true,
				hydrated: true
			});
		} else {
			// NEVER blank the UI on a switch: carry the last-known values over as a
			// placeholder while the real load runs.
			_set({
				...prev,
				...base,
				changedFilesLoading: false,
				stale: true,
				hydrated: false
			});
		}
	},

	clearRepository() {
		_loadedKey = null;
		_loadedScope = null;
		_worktreeGone = false;
		_clearServerBackoff();
		// untrack: called synchronously from component $effects — see
		// restoreCacheImmediate.
		_set({ ...EMPTY_STATE, githubAvailable: untrack(() => _state.githubAvailable) });
	},

	/**
	 * Report the latest GitHub authentication verdict.
	 *
	 * Local git NEVER depends on this — status/branch/diff keep working with no
	 * GitHub auth at all. Only the GitHub-backed surfaces (PR lookup, CI, Create
	 * PR / Ship It) consult `githubAvailable`, and a single failed check never
	 * flips it: `gh auth status` is a subprocess that transiently fails under
	 * load, so we require two consecutive failures at least
	 * GITHUB_AUTH_GRACE_MS apart before believing it.
	 */
	setGithubAuth(authenticated: boolean) {
		// untrack: called synchronously from a component $effect; a tracked read
		// of githubAvailable would re-run that effect on every flip — see
		// restoreCacheImmediate.
		const available = untrack(() => _state.githubAvailable);
		if (authenticated) {
			_githubAuthFailures = 0;
			_githubAuthFirstFailureAt = 0;
			if (!available) _update((s) => ({ ...s, githubAvailable: true }));
			return;
		}
		_githubAuthFailures += 1;
		if (_githubAuthFirstFailureAt === 0) _githubAuthFirstFailureAt = Date.now();
		const settled =
			_githubAuthFailures >= 2 && Date.now() - _githubAuthFirstFailureAt >= GITHUB_AUTH_GRACE_MS;
		if (settled && available) {
			_update((s) => ({ ...s, githubAvailable: false }));
		}
	},

	/**
	 * Register interest in the slow PR/CI refresh. Call from a git surface's
	 * mount and pair with `stopBackgroundRefresh()` on destroy — the refresh runs
	 * only while at least one holder is mounted AND the tab is visible.
	 */
	startBackgroundRefresh() {
		_bgRefreshHolders += 1;
		if (typeof document !== 'undefined' && !_visibilityBound) {
			document.addEventListener('visibilitychange', _handleBgVisibility);
			_visibilityBound = true;
		}
		if (_bgRefreshHolders === 1) _scheduleBgRefresh();
	},

	stopBackgroundRefresh() {
		_bgRefreshHolders = Math.max(0, _bgRefreshHolders - 1);
		if (_bgRefreshHolders > 0) return;
		if (_bgRefreshTimer) clearTimeout(_bgRefreshTimer);
		_bgRefreshTimer = null;
		if (typeof document !== 'undefined' && _visibilityBound) {
			document.removeEventListener('visibilitychange', _handleBgVisibility);
			_visibilityBound = false;
		}
	},

	async refreshStatus() {
		if (!_state.projectId) return;
		if (_statusInFlight) return; // Prevent overlapping requests
		if (_worktreeGone) return; // Worktree deleted — stop polling
		if (_isBackingOff()) return; // Server error backoff — skip this cycle
		// Status alone cannot hydrate the header after an initial owner/network
		// failure. Retry the coherent load (including branches and PR state),
		// preserving the session target and the load's overlap/stale-response guards.
		if (!_state.hydrated) {
			if (_state.worktreeId) {
				await git.loadForWorktree(_state.projectId, _state.worktreeId, true, _state.sessionId ?? undefined);
			} else {
				await git.loadRepository(_state.projectId, true, _state.sessionId ?? undefined);
			}
			return;
		}
		const scope = _loadedScope ?? _scopeKey(_state.projectId, _state.worktreeId, _state.sessionId);
		const key = _identityFor(scope);
		const projectId = _state.projectId;

		_statusAbort?.abort();
		const controller = new AbortController();
		_statusAbort = controller;
		const timeout = setTimeout(() => controller.abort(), GIT_POLL_FETCH_TIMEOUT_MS);

		_statusInFlight = true;
		const seq = _nextSeq();
		try {
			const status = await gitApi.getGitStatus(
				projectId,
				_state.worktreeId ?? undefined,
				{ signal: controller.signal },
				_state.sessionId ?? undefined
			);
			if (_loadedKey !== key) return;
			_clearServerBackoff();
			// Discard a response that a newer one has already superseded.
			if (!_acceptSeq('status', seq)) return;
			_learnIdentity(scope, projectId, status.current_branch);
			// Skip the write when nothing changed — with the canonical flat status
			// this genuinely suppresses the poll, instead of comparing fresh array
			// references that never match.
			if (gitApi.gitStatusEqual(status, _state.status)) return;
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
		const scope = _loadedScope ?? _scopeKey(_state.projectId, _state.worktreeId, _state.sessionId);
		const key = _identityFor(scope);

		_branchesAbort?.abort();
		const controller = new AbortController();
		_branchesAbort = controller;
		const timeout = setTimeout(() => controller.abort(), GIT_POLL_FETCH_TIMEOUT_MS);

		_branchesInFlight = true;
		const seq = _nextSeq();
		try {
			const branches = await gitApi.getBranches(
				_state.projectId,
				true,
				_state.worktreeId ?? undefined,
				{ signal: controller.signal },
				_state.sessionId ?? undefined
			);
			if (_loadedKey !== key) return;
			if (!_acceptSeq('branches', seq)) return;
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
		const key = _loadedKey;

		const seq = _nextSeq();
		try {
			const graphResponse = await gitApi.getCommitGraph(_state.projectId, limit, _state.worktreeId ?? undefined, _state.sessionId ?? undefined);
			if (_loadedKey !== key) return;
			if (!_acceptSeq('commits', seq)) return;
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
		const key = _loadedKey;

		const seq = _nextSeq();
		try {
			const worktrees = await gitApi.getWorktrees(_state.projectId);
			if (_loadedKey !== key) return;
			if (!_acceptSeq('worktrees', seq)) return;
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
			await gitApi.createBranch(_state.projectId, name, startPoint, _state.sessionId ?? undefined);
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
			await gitApi.deleteBranch(_state.projectId, name, _state.sessionId ?? undefined);
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

	async checkout(ref: string, autoStash: boolean = false): Promise<{ success: boolean; message: string }> {
		if (!_state.projectId) throw new Error('No project selected');

		_update((s) => ({ ...s, loading: true, error: null }));

		try {
			const response = await gitApi.checkout(
				_state.projectId,
				ref,
				_state.worktreeId ?? undefined,
				autoStash,
				_state.sessionId ?? undefined
			);
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
			// The branch just changed, so the open-PR verdict for the new branch is
			// unknown — re-ask rather than showing the old branch's PR.
			git.refreshPrState(true).catch(() => {});
			_update((s) => ({ ...s, loading: false }));
			return response;
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
			await gitApi.fetchRemote(_state.projectId, _state.worktreeId ?? undefined, _state.sessionId ?? undefined);
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

	/**
	 * Pull, then apply the backend's inline post-pull status + diff stats.
	 *
	 * Reconciling refetches kept on purpose: `loadBranches` (upstream
	 * ahead/behind, which the inline status does not carry), `loadGraph` (new
	 * commits) and a forced `refreshPrState` (a pull can change GitHub's
	 * mergeability verdict).
	 */
	async pull(
		strategy: 'merge' | 'rebase' = 'merge',
		autoStash: boolean = false
	): Promise<gitApi.PullResponse> {
		if (!_state.projectId) throw new Error('No project selected');

		try {
			const response = await gitApi.pullFromRemote(
				_state.projectId,
				{ strategy, auto_stash: autoStash },
				_state.worktreeId ?? undefined,
				_state.sessionId ?? undefined
			);

			_applyMutationPayload(response.status, response.diff_stats);

			await Promise.all([
				git.loadBranches(),
				git.loadGraph(),
				git.refreshPrState(true).catch(() => {})
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
		const key = _loadedKey;

		_diffStatsAbort?.abort();
		const controller = new AbortController();
		_diffStatsAbort = controller;
		const timeout = setTimeout(() => controller.abort(), GIT_POLL_FETCH_TIMEOUT_MS);

		_diffStatsInFlight = true;
		const seq = _nextSeq();
		try {
			const diffStats = await gitApi.getDiffStats(
				_state.projectId,
				true,
				_state.worktreeId ?? undefined,
				{ signal: controller.signal },
				_state.sessionId ?? undefined
			);
			if (_loadedKey !== key) return;
			_clearServerBackoff();
			if (!_acceptSeq('diffStats', seq)) return;
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

	/**
	 * Refresh the PR-lifecycle state (open PR for the current branch) for the
	 * active checkout. Throttled to PR_STATE_MIN_INTERVAL_MS per checkout when
	 * triggered by polling; explicit callers (after commit-with-PR, Create PR,
	 * Finish, Ship It, pull, checkout, streaming-end) force past the throttle.
	 *
	 * A lookup the backend could not complete NEVER downgrades to "no PR": the
	 * previous `open_pr` is kept and `prStateStale` is set, so the header cannot
	 * flip "Merge PR #12" → "Create PR" because `gh` hiccuped.
	 *
	 * @returns true when a fresh verdict landed, false when the lookup failed,
	 *          undefined when the call was skipped (throttled / guarded).
	 */
	async refreshPrState(force: boolean = false): Promise<boolean | undefined> {
		if (!_state.projectId) return undefined;
		if (_prStateInFlight) return undefined;
		if (_worktreeGone) return undefined; // Worktree deleted — stop polling
		if (_isBackingOff()) return undefined;
		const key = _loadedKey;
		if (!key) return undefined;
		const now = Date.now();
		if (!force && now - (_lastPrStateFetch.get(key) ?? 0) < PR_STATE_MIN_INTERVAL_MS) {
			return undefined;
		}

		_prStateInFlight = true;
		const seq = _nextSeq();
		try {
			const prState = await gitApi.getPrState(
				_state.projectId,
				_state.worktreeId ?? undefined,
				_state.sessionId ?? undefined
			);
			if (_loadedKey !== key) return undefined; // context switched mid-flight
			if (!_acceptSeq('prState', seq)) return undefined;
			_lastPrStateFetch.set(key, Date.now());

			if (prState.lookup_failed) {
				const kept = _keepPreviousPrState(_state.prState, prState, _state.status?.current_branch ?? null);
				if (!gitApi.prStateEqual(kept, _state.prState) || !_state.prStateStale) {
					_update((s) => ({ ...s, prState: kept, prStateStale: true }));
					_syncToCache({ prState: kept, prStateStale: true });
				}
				return false;
			}

			if (gitApi.prStateEqual(prState, _state.prState) && !_state.prStateStale) return true;
			_update((s) => ({ ...s, prState, prStateStale: false }));
			_syncToCache({ prState, prStateStale: false });
			return true;
		} catch {
			// PR state is advisory — a transport failure keeps the previous value and
			// flags it stale. It is NOT "no open PR", and it is deliberately not
			// recorded as a server error: gh hiccups shouldn't trip the status
			// poller's backoff.
			if (_loadedKey === key && !_state.prStateStale) {
				_update((s) => ({ ...s, prStateStale: true }));
				_syncToCache({ prStateStale: true });
			}
			return false;
		} finally {
			_prStateInFlight = false;
		}
	},

	/**
	 * Commit, then apply the backend's inline post-commit status + diff stats
	 * (which it computes anyway, "for instant frontend update").
	 *
	 * The only reconciling refetch kept is `loadBranches`: upstream ahead/behind
	 * drive the Push badge and are NOT part of the inline status payload.
	 */
	async commitChanges(
		message: string,
		includeUnstaged: boolean = true,
		target: GitActionTarget = git.captureTarget()
	): Promise<gitApi.CommitResponse> {

		try {
			const response = await gitApi.commitChanges(
				target.projectId,
				{
					message,
					include_unstaged: includeUnstaged
				},
				target.worktreeId,
				target.sessionId
			);

			if (!_targetIsCurrent(target)) return response;
			_applyMutationPayload(response.status, response.diff_stats);
			await git.loadBranches();

			return response;
		} catch (e) {
			const error = e as { detail?: string };
			if (_targetIsCurrent(target)) _update((s) => ({
				...s,
				error: error.detail || 'Failed to commit changes'
			}));
			throw e;
		}
	},

	/**
	 * Push, then apply the backend's inline post-push status.
	 *
	 * Reconciling refetches kept on purpose: `loadBranches` (the Push badge reads
	 * `branch.ahead`, which the inline status does not carry) and a forced
	 * `refreshPrState` (a push changes GitHub's mergeable verdict on an open PR).
	 */
	async pushCommits(
		branch?: string,
		setUpstream: boolean = false,
		force: boolean = false,
		target: GitActionTarget = git.captureTarget()
	): Promise<gitApi.OperationResponse> {

		try {
			const response = await gitApi.pushCommits(
				target.projectId,
				{
					branch,
					set_upstream: setUpstream,
					force
				},
				target.worktreeId,
				target.sessionId
			);

			if (!_targetIsCurrent(target)) return response;
			_applyMutationPayload(response.status, response.diff_stats);

			await Promise.all([
				git.loadBranches(),
				git.refreshPrState(true).catch(() => {})
			]);

			return response;
		} catch (e) {
			const error = e as { detail?: string };
			if (_targetIsCurrent(target)) _update((s) => ({
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
		const key = _loadedKey;

		_changedFilesInFlight = true;
		_update((s) => ({ ...s, changedFilesLoading: true }));
		const seq = _nextSeq();
		try {
			const response = await gitApi.getChangedFiles(
				_state.projectId,
				_state.worktreeId ?? undefined,
				_state.sessionId ?? undefined
			);
			if (_loadedKey !== key) return;
			_clearServerBackoff();
			if (!_acceptSeq('changedFiles', seq)) {
				_update((s) => ({ ...s, changedFilesLoading: false }));
				return;
			}
			_update((s) => ({ ...s, changedFiles: response.files, changedFilesLoading: false }));
			_syncToCache({ changedFiles: response.files });
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

	/**
	 * Stage files, then apply the backend's inline status + diff stats.
	 * `refreshChangedFiles` is the one reconciling refetch: the per-file list
	 * (with its per-file add/delete counts) is not part of the inline payload.
	 */
	async stageFiles(files: string[]) {
		if (!_state.projectId) return;

		try {
			const response = await gitApi.stageFiles(
				_state.projectId,
				files,
				_state.worktreeId ?? undefined,
				_state.sessionId ?? undefined
			);
			_applyMutationPayload(response.status, response.diff_stats);
			await git.refreshChangedFiles();
		} catch (e) {
			const error = e as { detail?: string };
			_update((s) => ({ ...s, error: error.detail || 'Failed to stage files' }));
			throw e;
		}
	},

	/** @see stageFiles — same inline-payload + changed-files reconcile. */
	async unstageFiles(files: string[]) {
		if (!_state.projectId) return;

		try {
			const response = await gitApi.unstageFiles(
				_state.projectId,
				files,
				_state.worktreeId ?? undefined,
				_state.sessionId ?? undefined
			);
			_applyMutationPayload(response.status, response.diff_stats);
			await git.refreshChangedFiles();
		} catch (e) {
			const error = e as { detail?: string };
			_update((s) => ({ ...s, error: error.detail || 'Failed to unstage files' }));
			throw e;
		}
	},

	/** @see stageFiles — same inline-payload + changed-files reconcile. */
	async discardChanges(files: string[], fileStatuses: Record<string, string>) {
		if (!_state.projectId) return;

		try {
			const response = await gitApi.discardChanges(
				_state.projectId,
				files,
				fileStatuses,
				_state.worktreeId ?? undefined,
				_state.sessionId ?? undefined
			);
			_applyMutationPayload(response.status, response.diff_stats);
			await git.refreshChangedFiles();
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
		_loadedScope = null;
		_cache.clear();
		_scopeAlias.clear();
		_lastPrStateFetch.clear();
		_appliedSeq.clear();
		_worktreeGone = false;
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
			// Reset in-flight guards so post-operation refreshes aren't blocked.
			// Request versioning (not these flags) is what keeps the aborted
			// requests' responses from landing on top of newer ones.
			_statusInFlight = false;
			_diffStatsInFlight = false;
			_branchesInFlight = false;
			_prStateInFlight = false;
		}
	},

	/** Retry against the current session owner after an unavailable-worker error. */
	retryLoad() {
		if (!_state.projectId) return;
		const sessionId = _state.sessionId ?? undefined;
		return _state.worktreeId
			? git.loadForWorktree(_state.projectId, _state.worktreeId, true, sessionId)
			: git.loadRepository(_state.projectId, true, sessionId);
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
		git.refreshPrState(true).catch(() => {});
	}
};

/**
 * Apply a mutation endpoint's inline status/diff payload as ONE coherent write.
 *
 * The commit/push/pull/stage/unstage/discard endpoints already compute a fresh
 * status (and usually diff stats) server-side. Applying them here removes 2–4
 * follow-up HTTP round trips per action AND closes the window in which the
 * header rendered pre-mutation state — the "post-commit button flip".
 *
 * The sequence claim makes any poll response issued BEFORE this write lose:
 * a status fetch that raced the commit can no longer resurrect the dirty tree.
 */
function _applyMutationPayload(
	status: gitApi.GitStatus | null | undefined,
	diffStats: gitApi.DiffStats | null | undefined
): void {
	if (!status && !diffStats) return;
	// Inline payloads are built from git_service.get_status alone and carry no
	// repository record, so preserve default_branch & friends.
	const merged = status ? gitApi.mergeStatusRepoMeta(_state.status, status) : null;
	const fields: string[] = [];
	if (merged) fields.push('status');
	if (diffStats) fields.push('diffStats');
	_claimSeq(...fields);
	_update((s) => ({
		...s,
		...(merged ? { status: merged } : {}),
		...(diffStats ? { diffStats } : {}),
		stale: false
	}));
	_syncToCache({
		...(merged ? { status: merged } : {}),
		...(diffStats ? { diffStats } : {})
	});
}

// ============================================================================
// Derived stores (via store-utils createDerived)
// ============================================================================

export const branches = createDerived(() => _state.branches);
export const worktrees = createDerived(() => _state.worktrees);
export const gitStatus = createDerived(() => _state.status);
export const diffStats = createDerived(() => _state.diffStats);
export const gitMode = createDerived(() => _state.mode);

export const currentBranch = createDerived(
	() => _state.branches.find((b) => b.current) || null
);

export const changedFiles = createDerived(() => _state.changedFiles);
export const changedFilesLoading = createDerived(() => _state.changedFilesLoading);
export const gitPrState = createDerived(() => _state.prState);

/** GitHub-backed features (PR lookup, CI, Create PR / Ship It) are usable.
 *  Local git status/branch/diff do NOT depend on this. */
export const githubFeaturesAvailable = createDerived(() => _state.githubAvailable);

/**
 * ONE coherent view of the git state for the current checkout.
 *
 * The header's primary action must be derived from this object, never from the
 * individual derived stores: those arrive on four independent cadences, so a
 * consumer reading them separately walks through intermediate combinations
 * after every switch and every mutation. `hydrated`/`stale` tell the consumer
 * whether the snapshot describes the CURRENT context yet.
 */
export interface GitSnapshot {
	contextKey: string | null;
	status: gitApi.GitStatus | null;
	branches: gitApi.Branch[];
	diffStats: gitApi.DiffStats | null;
	prState: gitApi.PrState | null;
	changedFiles: gitApi.FileChangeInfo[] | null;
	/** Branch from status (authoritative for the checked-out ref). */
	currentBranch: string | null;
	/** Repo default branch straight from /git/status — never a hardcoded 'main'. */
	defaultBranch: string | null;
	openPr: gitApi.OpenPrInfo | null;
	prStateStale: boolean;
	stale: boolean;
	hydrated: boolean;
	githubAvailable: boolean;
}

export const gitSnapshot = createDerived<GitSnapshot>(() => ({
	contextKey: _state.contextKey,
	status: _state.status,
	branches: _state.branches,
	diffStats: _state.diffStats,
	prState: _state.prState,
	changedFiles: _state.changedFiles,
	currentBranch: _state.status?.current_branch ?? null,
	defaultBranch: _state.status?.default_branch ?? null,
	openPr: _state.prState?.open_pr ?? null,
	prStateStale: _state.prStateStale,
	stale: _state.stale,
	hydrated: _state.hydrated,
	githubAvailable: _state.githubAvailable
}));
