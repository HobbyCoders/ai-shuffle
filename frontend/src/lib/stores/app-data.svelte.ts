/**
 * App Data Store
 *
 * Global application data: profiles, projects, sessions, and selection state.
 * Extracted from tabs.ts — these are not per-session state.
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import type { Session, Profile, PaginatedSessions, CursorPaginatedSessions, SessionSearchResult, PaginatedSessionSearchResults } from '$lib/api/client';
import { feedController } from './session/v2/feed-controller';
import { api, searchSessionsPaginated, renameSession as apiRenameSession, setSessionActive as apiSetSessionActive, setSessionArchived as apiSetSessionArchived } from '$lib/api/client';
import { listClusterProjects, type ClusterProject } from '$lib/api/installs';
import type { Project } from './types';
import { createSubscribers } from './store-utils';
import { activeSession } from './active-session';
import { archivedChats } from './archived-chats';

// ─── Types ───────────────────────────────────────────────────────────────────

interface AppDataState {
	profiles: Profile[];
	/** The agent roster has answered at least once. Until then an empty
	 *  `profiles` means "not loaded yet", not "no agents". */
	profilesLoaded: boolean;
	projects: Project[];
	/** Origin-keyed catalog of projects available on approved online installs. */
	clusterProjects: ClusterProject[];
	sessions: Session[];
	// Loading state
	sessionsLoading: boolean;
	// Tag filter state
	sessionsTagFilter: string | null;
	// Pagination state for sessions
	sessionsTotal: number;
	sessionsOffset: number;
	sessionsHasMore: boolean;
	sessionsLoadingMore: boolean;
	// Cursor-based pagination state
	sessionsCursor: string | null;
	sessionsCursorId: string | null;
	// Search state
	searchQuery: string;
	searchResults: SessionSearchResult[];
	searchTotal: number;
	searchOffset: number;
	searchHasMore: boolean;
	searchLoading: boolean;
	searchLoadingMore: boolean;
	// API user sessions (separate from admin sessions)
	apiUserSessions: Session[];
	apiUserSessionsLoading: boolean;
}

// ─── Constants ───────────────────────────────────────────────────────────────

// Per-request page size when paging through sessions. 100 is the max the
// /sessions/cursor endpoint allows per call.
const SESSIONS_PAGE_SIZE = 100;

/**
 * Pages fetched up front by `loadSessions`.
 *
 * The list used to be uncapped: every refresh paged through the ENTIRE session
 * history (⌈N/100⌉ sequential round-trips) and committed a fresh array per
 * page. With a WS-driven refresh on every turn completion that was the sidebar
 * refetch storm (PERFORMANCE_REVIEW_2026-08 W1). The initial load is now capped
 * and the rest is behind the sidebar's explicit "load more" shelf, which calls
 * `loadMoreSessions()` and appends the next cursor page.
 */
const INITIAL_SESSION_PAGES = 1;

/**
 * Upper bound on pages a background refresh re-fetches to keep what the user
 * already loaded with "Load older sessions". A refresh used to drop back to
 * one page, which threw the older rows away and brought the shelf back after
 * the user had reached the end.
 */
const MAX_RELOAD_SESSION_PAGES = 10;

/**
 * Every sidebar list request. Archived chats live in Settings, never in the
 * sidebar, so they are excluded on the server: otherwise `has_more` counts
 * rows the sidebar will drop and "Load older sessions" fetches pages that
 * add nothing visible.
 */
const SESSION_LIST_BASE = `/sessions/cursor?limit=${SESSIONS_PAGE_SIZE}&admin_only=true&exclude_archived=true`;
const SEARCH_PAGE_SIZE = 20;

// ─── Reactive State ──────────────────────────────────────────────────────────

let _state = $state<AppDataState>({
	profiles: [],
	profilesLoaded: false,
	projects: [],
	clusterProjects: [],
	sessions: [],
	sessionsLoading: false,
	sessionsTagFilter: null,
	sessionsTotal: 0,
	sessionsOffset: 0,
	sessionsHasMore: false,
	sessionsLoadingMore: false,
	sessionsCursor: null,
	sessionsCursorId: null,
	searchQuery: '',
	searchResults: [],
	searchTotal: 0,
	searchOffset: 0,
	searchHasMore: false,
	searchLoading: false,
	searchLoadingMore: false,
	apiUserSessions: [],
	apiUserSessionsLoading: false,
});

// ─── Subscribe Shim (centralized via store-utils) ───────────────────────────

const { subscribe, update: _update, createDerived } =
	createSubscribers<AppDataState>(
		() => _state,
		(v) => { _state = v; }
	);

// ─── Optimistic Local Edits ──────────────────────────────────────────────────

/**
 * Local, not-yet-superseded mutations of session rows.
 *
 * The problem this solves: every optimistic mutation (rename, settle/unsettle,
 * delete, a server-pushed row patch) writes straight into `sessions`, while a
 * paginated load may already be in flight carrying PRE-mutation rows. When that
 * load commits, the row snaps back — the rename reverts, the settled card jumps
 * between groups (PERFORMANCE_REVIEW_2026-08 S12 / W7).
 *
 * The old fix was per-call-site: `setSessionActive` invalidated in-flight loads,
 * `renameSession` forgot to, and every future mutation had to remember. Instead
 * the edit is recorded here and RE-APPLIED on top of every committed page, so
 * no call site can forget and no genuinely fresh data has to be thrown away.
 *
 * An edit retires once the server is known to agree AND a load that started
 * after that moment has committed (`supersedeAtLoadId`) — from then on the
 * server rows carry the change themselves. Rolled-back edits are dropped
 * immediately; anything left behind by a failure path expires on its own.
 */
interface PendingRowEdit {
	/** Field patch to re-apply, or null for a pure removal. */
	patch: Partial<Session> | null;
	/** True when the row must stay out of the list (delete / local hide). */
	removed: boolean;
	/** Load request id from which this edit may be dropped; null = still
	 *  unconfirmed by the server, so it is re-applied indefinitely. */
	supersedeAtLoadId: number | null;
	createdAt: number;
}

/** Backstop so a failure path can never leave an edit pinned forever. */
const PENDING_EDIT_TTL_MS = 5 * 60 * 1000;

const _pendingEdits = new Map<string, PendingRowEdit>();

// Monotonic token so that if a newer load (tag change, manual reload) starts
// while an earlier one is still paging, the stale loop bails out instead of
// clobbering the newer results.
let _loadSessionsRequestId = 0;

function recordLocalEdit(sessionId: string, patch: Partial<Session>): void {
	const prev = _pendingEdits.get(sessionId);
	_pendingEdits.set(sessionId, {
		patch: { ...(prev?.patch ?? {}), ...patch },
		removed: prev?.removed ?? false,
		// A fresh local change is unconfirmed again, even if an earlier edit for
		// the same row had been confirmed.
		supersedeAtLoadId: null,
		createdAt: Date.now(),
	});
}

function recordLocalRemoval(sessionIds: Iterable<string>): void {
	for (const id of sessionIds) {
		_pendingEdits.set(id, { patch: null, removed: true, supersedeAtLoadId: null, createdAt: Date.now() });
		// R3: drop any cached transcript for a row that is going away, so the
		// feed's LRU never holds state for a session that no longer exists.
		feedController.invalidate(id);
	}
}

/** The server now agrees with these edits: the next load STARTED from here on
 *  carries the change itself, so the edit may retire when that load commits. */
function confirmLocalEdits(sessionIds: Iterable<string>): void {
	for (const id of sessionIds) {
		const edit = _pendingEdits.get(id);
		if (edit) edit.supersedeAtLoadId = _loadSessionsRequestId + 1;
	}
}

/** Drop edits outright (the mutation failed and was rolled back locally). */
function discardLocalEdits(sessionIds: Iterable<string>): void {
	for (const id of sessionIds) _pendingEdits.delete(id);
}

/** Re-apply outstanding local edits on top of a server page. */
function applyPendingEdits(rows: Session[]): Session[] {
	if (_pendingEdits.size === 0) return rows;
	const cutoff = Date.now() - PENDING_EDIT_TTL_MS;
	for (const [id, edit] of _pendingEdits) {
		if (edit.createdAt < cutoff) _pendingEdits.delete(id);
	}
	if (_pendingEdits.size === 0) return rows;
	const out: Session[] = [];
	for (const row of rows) {
		const edit = _pendingEdits.get(row.id);
		if (!edit) { out.push(row); continue; }
		if (edit.removed) continue;
		out.push(edit.patch ? { ...row, ...edit.patch } : row);
	}
	return out;
}

/** Retire the edits a just-committed load is known to already carry. */
function retireSupersededEdits(loadRequestId: number): void {
	for (const [id, edit] of _pendingEdits) {
		if (edit.supersedeAtLoadId !== null && loadRequestId >= edit.supersedeAtLoadId) {
			_pendingEdits.delete(id);
		}
	}
}

// ─── Commit Helpers ──────────────────────────────────────────────────────────

function valueEqual(a: unknown, b: unknown): boolean {
	if (a === b) return true;
	if (Array.isArray(a) && Array.isArray(b)) {
		return a.length === b.length && a.every((item, i) => valueEqual(item, b[i]));
	}
	if (a && b && typeof a === 'object' && typeof b === 'object') {
		const ak = Object.keys(a as Record<string, unknown>);
		const bk = Object.keys(b as Record<string, unknown>);
		if (ak.length !== bk.length) return false;
		return ak.every(k => (a as Record<string, unknown>)[k] === (b as Record<string, unknown>)[k]);
	}
	return false;
}

/** True when two session lists carry the same rows with the same field values.
 *  Committing an equal list would hand every subscriber a NEW array identity
 *  and re-derive the whole sidebar (filter + map + sort) for nothing. */
export function sessionListsEqual(a: Session[], b: Session[]): boolean {
	if (a === b) return true;
	if (a.length !== b.length) return false;
	for (let i = 0; i < a.length; i++) {
		const x = a[i] as unknown as Record<string, unknown>;
		const y = b[i] as unknown as Record<string, unknown>;
		if (x === y) continue;
		if (x.id !== y.id) return false;
		const xk = Object.keys(x);
		if (xk.length !== Object.keys(y).length) return false;
		for (const k of xk) {
			if (!valueEqual(x[k], y[k])) return false;
		}
	}
	return true;
}

interface SessionsPageMeta {
	total: number;
	offset: number;
	hasMore: boolean;
	tagFilter: string | null;
	cursor: string | null;
	cursorId: string | null;
}

/** Commit a freshly loaded list, keeping the previous ARRAY IDENTITY when the
 *  rows are unchanged and skipping the store notification entirely when nothing
 *  at all moved. */
function commitSessions(rows: Session[], meta: SessionsPageMeta): void {
	const prev = _state;
	const rowsChanged = !sessionListsEqual(prev.sessions, rows);
	const metaChanged =
		prev.sessionsTotal !== meta.total ||
		prev.sessionsOffset !== meta.offset ||
		prev.sessionsHasMore !== meta.hasMore ||
		prev.sessionsTagFilter !== meta.tagFilter ||
		prev.sessionsCursor !== meta.cursor ||
		prev.sessionsCursorId !== meta.cursorId;
	if (!rowsChanged && !metaChanged) return;
	_update(s => ({
		...s,
		sessions: rowsChanged ? rows : s.sessions,
		sessionsTotal: meta.total,
		sessionsOffset: meta.offset,
		sessionsHasMore: meta.hasMore,
		sessionsTagFilter: meta.tagFilter,
		sessionsCursor: meta.cursor,
		sessionsCursorId: meta.cursorId,
	}));
}

// ─── Internal Data Loading ───────────────────────────────────────────────────

/**
 * Load the first `pages` cursor pages of the session list.
 *
 * Two deliberate properties (W1):
 * - CAPPED: only `INITIAL_SESSION_PAGES` round-trips, never the whole history.
 *   `loadMoreSessions()` appends the rest on demand.
 * - ONE COMMIT: pages are accumulated and committed once, so subscribers are
 *   notified a single time per load instead of once per page.
 */
/**
 * The Active group is the user's working set and must never be truncated by the
 * page cap: an active session that has not been touched for a while can sit far
 * below the first page of the recency-ordered list. One extra request (in
 * PARALLEL with the first page, and filtered server-side) keeps it complete.
 */
async function fetchActiveSessions(tagId?: string | null): Promise<Session[]> {
	let url = `${SESSION_LIST_BASE}&active_only=true`;
	if (tagId) url += `&tag_id=${encodeURIComponent(tagId)}`;
	try {
		const result = await api.get<CursorPaginatedSessions>(url);
		return result.items;
	} catch (e) {
		// Never fail the whole load over the supplementary request.
		console.error('Failed to load active sessions:', e);
		return [];
	}
}

// Share a refresh across callers, including different resource subscribers. Calls
// in the same turn share the first request; later invalidations retain one trailing
// refresh. A changed filter fences the old response before it can publish.
let sessionLoad: Promise<void> | null = null;
type SessionLoadRequest = { tagId: string | null; pages: number | undefined };
let pendingSessionLoad: SessionLoadRequest | null = null;
let runningSessionTag: string | null = null;

/**
 * Performance kill switch VITE_PERF_EARLY_SESSION_LIST (default on; see
 * "Performance flags" in docs/internal/DEVELOPMENT.md). `0` restores starting
 * the sidebar list only when the app page mounts.
 */
export function earlySessionListEnabled(): boolean {
	return import.meta.env.VITE_PERF_EARLY_SESSION_LIST !== '0';
}

// The sidebar list started as soon as auth answered (prefetchSessions). The
// next loadSessions() (the app page's boot load) takes it once instead of
// loading again: it joins it in flight, or accepts its committed result while
// that is recent and nothing loaded the list since.
const SESSION_PREFETCH_REUSE_MS = 15_000;
interface SessionPrefetch {
	promise: Promise<void>;
	tagId: string | null;
	request: SessionLoadRequest | null;
	commitsBefore: number;
	settledAt?: number;
}
let sessionPrefetch: SessionPrefetch | null = null;
// Successful list commits, so a settled prefetch can tell it produced the list.
let sessionListCommits = 0;
let sessionListCommitRequest = 0;

/**
 * Cursor pages the committed list currently holds: the capped initial load
 * plus every page "Load older sessions" appended. A refresh for the same tag
 * re-fetches this many (bounded), so it never truncates the list the user
 * paged through.
 */
let _loadedSessionPages = INITIAL_SESSION_PAGES;

function refreshPageCount(tagId: string | null): number {
	if (tagId !== _state.sessionsTagFilter) return INITIAL_SESSION_PAGES;
	return Math.min(MAX_RELOAD_SESSION_PAGES, Math.max(INITIAL_SESSION_PAGES, _loadedSessionPages));
}

function loadSessionsInternal(tagId?: string | null, pages?: number): Promise<void> {
	const tag = tagId ?? null;
	pendingSessionLoad = { tagId: tag, pages };
	if (sessionLoad) {
		if (runningSessionTag !== tag) ++_loadSessionsRequestId;
		return sessionLoad;
	}
	sessionLoad = Promise.resolve().then(async () => {
		try {
			while (pendingSessionLoad) {
				const next = pendingSessionLoad;
				pendingSessionLoad = null;
				runningSessionTag = next.tagId;
				await fetchSessionList(next.tagId, next.pages);
			}
		} finally { sessionLoad = null; }
	});
	return sessionLoad;
}

async function fetchSessionList(tagId?: string | null, requestedPages?: number) {
	const requestId = ++_loadSessionsRequestId;
	// Resolved when the fetch starts, not when it was queued, so a page the
	// user appended in between is included.
	const pages = requestedPages ?? refreshPageCount(tagId ?? null);
	_update(s => ({ ...s, sessionsLoading: true }));
	try {
		const activeRowsPromise = fetchActiveSessions(tagId);
		const all: Session[] = [];
		let cursor: string | null = null;
		let cursorId: string | null = null;
		let hasMore = true;
		let pagesFetched = 0;

		for (let page = 0; page < Math.max(1, pages) && hasMore; page++) {
			let url = SESSION_LIST_BASE;
			if (tagId) {
				url += `&tag_id=${encodeURIComponent(tagId)}`;
			}
			if (cursor) {
				url += `&cursor=${encodeURIComponent(cursor)}`;
			}
			if (cursorId) {
				url += `&cursor_id=${encodeURIComponent(cursorId)}`;
			}

			const result = await api.get<CursorPaginatedSessions>(url);

			// A newer load superseded this one while we were fetching — discard.
			if (requestId !== _loadSessionsRequestId) return;

			all.push(...result.items);
			pagesFetched++;
			hasMore = result.has_more;
			cursor = result.next_cursor;
			cursorId = result.next_cursor_id;

			// Defend against a backend that reports has_more without advancing
			// the cursor, which would otherwise loop forever.
			if (hasMore && !cursor && !cursorId) {
				hasMore = false;
			}
		}

		// Fold in any Active row the capped page missed. They go on the END so the
		// leading rows stay strictly most-recent-first (consumers that take the
		// first few as "latest" keep working); the sidebar sorts for itself.
		const activeRows = await activeRowsPromise;
		if (requestId !== _loadSessionsRequestId) return;
		const seen = new Set(all.map(row => row.id));
		for (const row of activeRows) {
			if (seen.has(row.id)) continue;
			seen.add(row.id);
			all.push(row);
		}

		// Re-apply optimistic local edits the server hasn't caught up with yet,
		// then retire the ones this load is known to already carry.
		const rows = applyPendingEdits(all);
		retireSupersededEdits(requestId);

		_loadedSessionPages = Math.max(INITIAL_SESSION_PAGES, pagesFetched);
		sessionListCommits++;
		sessionListCommitRequest = requestId;
		commitSessions(rows, {
			total: rows.length + (hasMore ? 1 : 0),
			offset: rows.length,
			hasMore,
			tagFilter: tagId ?? null,
			cursor,
			cursorId,
		});
		_update(s => ({ ...s, sessionsLoading: false }));
	} catch (e) {
		// Don't surface a stale error over a newer in-flight load.
		if (requestId !== _loadSessionsRequestId) return;
		console.error('Failed to load sessions:', e);
		_update(s => ({ ...s, sessionsLoading: false }));
	}
}

async function loadMoreSessionsInternal() {
	if (_state.sessionsLoadingMore || !_state.sessionsHasMore) return;

	// Capture cursor and tag filter before the async fetch so we can
	// detect if a concurrent reset (e.g. tag change, reload) invalidated
	// this request while it was in flight.
	const capturedCursor = _state.sessionsCursor;
	const capturedCursorId = _state.sessionsCursorId;
	const capturedTagFilter = _state.sessionsTagFilter;

	// Capture the full-load request id too: a reload that starts while this
	// "load more" is in flight (e.g. the settle reconciliation) invalidates the
	// captured cursor, and appending the response would duplicate/stale rows.
	const requestId = _loadSessionsRequestId;
	_update(s => ({ ...s, sessionsLoadingMore: true }));
	try {
		let url = SESSION_LIST_BASE;
		if (capturedCursor) {
			url += `&cursor=${encodeURIComponent(capturedCursor)}`;
		}
		if (capturedCursorId) {
			url += `&cursor_id=${encodeURIComponent(capturedCursorId)}`;
		}
		if (capturedTagFilter) {
			url += `&tag_id=${encodeURIComponent(capturedTagFilter)}`;
		}
		const result = await api.get<CursorPaginatedSessions>(url);

		// Discard when superseded: a newer full reload bumped the request id,
		// or the cursor/tag filter changed while we were fetching (e.g. user
		// switched tag filter) — appending would duplicate/stale rows.
		if (requestId !== _loadSessionsRequestId ||
			_state.sessionsCursor !== capturedCursor || _state.sessionsCursorId !== capturedCursorId || _state.sessionsTagFilter !== capturedTagFilter) {
			_update(s => ({ ...s, sessionsLoadingMore: false }));
			return;
		}

		// The appended page goes through the same optimistic-edit re-apply as a
		// full load: a rename/settle/delete made while this page was in flight
		// must not come back undone. Rows already held locally (the Active set is
		// pulled in up front, ahead of its cursor position) are dropped —
		// duplicate ids would break the sidebar's keyed lists.
		const existingIds = new Set(_state.sessions.map(row => row.id));
		const appended = applyPendingEdits(result.items.filter(row => !existingIds.has(row.id)));
		_loadedSessionPages++;
		// Same guard as the full load: a has_more without a cursor that moves
		// would leave the shelf asking for the same page forever.
		const advanced = !!(result.next_cursor || result.next_cursor_id) &&
			(result.next_cursor !== capturedCursor || result.next_cursor_id !== capturedCursorId);
		const hasMore = result.has_more && advanced;

		_update(s => ({
			...s,
			sessions: [...s.sessions, ...appended],
			sessionsOffset: s.sessionsOffset + appended.length,
			sessionsTotal: s.sessionsOffset + appended.length + (hasMore ? 1 : 0),
			sessionsHasMore: hasMore,
			sessionsLoadingMore: false,
			sessionsCursor: result.next_cursor,
			sessionsCursorId: result.next_cursor_id,
		}));
	} catch (e) {
		console.error('Failed to load more sessions:', e);
		_update(s => ({ ...s, sessionsLoadingMore: false }));
	}
}

async function searchSessionsInternal(query: string) {
	if (!query.trim()) {
		_update(s => ({
			...s,
			searchQuery: '',
			searchResults: [],
			searchTotal: 0,
			searchOffset: 0,
			searchHasMore: false,
			searchLoading: false,
		}));
		return;
	}

	_update(s => ({ ...s, searchLoading: true, searchQuery: query }));
	try {
		const result = await searchSessionsPaginated(query, {
			adminOnly: true,
			limit: SEARCH_PAGE_SIZE,
			offset: 0,
		});
		_update(s => ({
			...s,
			searchResults: result.items,
			searchTotal: result.total,
			searchOffset: 0,
			searchHasMore: result.has_more,
			searchLoading: false,
		}));
	} catch (e) {
		console.error('Failed to search sessions:', e);
		_update(s => ({ ...s, searchLoading: false }));
	}
}

async function loadApiUserSessionsInternal() {
	_update(s => ({ ...s, apiUserSessionsLoading: true }));
	try {
		const result = await api.get<PaginatedSessions>(
			`/sessions/paginated?limit=100&offset=0&api_users_only=true&admin_only=false`
		);
		_update(s => ({
			...s,
			apiUserSessions: result.items,
			apiUserSessionsLoading: false,
		}));
	} catch (e) {
		console.error('Failed to load API user sessions:', e);
		_update(s => ({ ...s, apiUserSessionsLoading: false }));
	}
}

// ─── Exported Store ──────────────────────────────────────────────────────────

// ─── Project Catalog Readiness ───────────────────────────────────────────────

/**
 * A new chat's remembered project and branch resolve against BOTH halves of
 * the project catalog (this worker's /projects and the shared /cluster/projects).
 * Before both have answered once, a cluster-linked pick resolves to no local
 * project and no branch, so a send in that window would go out on an
 * auto-named branch (#1636). A failed load still counts as answered, so a
 * held send never waits forever; `projectCatalogFailed` says it fell short.
 */
const _catalogAnswered = { projects: false, cluster: false };
const _catalogFailures = { projects: false, cluster: false };
let _catalogWaiters: (() => void)[] = [];

// The latest request of each catalog half, so a refresh can join one that
// already reflects the server state it is asked for (see loadProjectCatalog).
type CatalogRequest = { startedAt: number; done: Promise<boolean> };
const _catalogLatest: { projects: CatalogRequest | null; cluster: CatalogRequest | null } = {
	projects: null,
	cluster: null
};

function trackCatalogRequest(half: 'projects' | 'cluster', load: () => Promise<boolean>): Promise<boolean> {
	const request: CatalogRequest = { startedAt: performance.now(), done: load() };
	_catalogLatest[half] = request;
	return request.done;
}

function settleCatalogHalf(half: 'projects' | 'cluster', ok: boolean) {
	_catalogAnswered[half] = true;
	_catalogFailures[half] = !ok;
	if (!_catalogAnswered.projects || !_catalogAnswered.cluster) return;
	const waiters = _catalogWaiters;
	_catalogWaiters = [];
	for (const resolve of waiters) resolve();
}

export const appData = {
	get current() { return _state; },

	/** Both halves of the project catalog have answered at least once. */
	get projectCatalogLoaded() { return _catalogAnswered.projects && _catalogAnswered.cluster; },
	/** The latest load of either catalog half failed. */
	get projectCatalogFailed() { return _catalogFailures.projects || _catalogFailures.cluster; },
	/** Resolves once both catalog halves have answered (immediately if they have). */
	whenProjectCatalogLoaded(): Promise<void> {
		if (appData.projectCatalogLoaded) return Promise.resolve();
		return new Promise(resolve => { _catalogWaiters.push(resolve); });
	},

	subscribe,

	// ── Profile Operations ──────────────────────────────────────────────

	async loadProfiles() {
		try {
			const profiles = await api.get<Profile[]>('/agents');
			_update(s => ({ ...s, profiles, profilesLoaded: true }));
		} catch (e) {
			console.error('Failed to load profiles:', e);
		}
	},

	async createProfile(data: { id: string; name: string; description?: string; config: Record<string, unknown>; avatar_color?: string | null }) {
		await api.post('/agents', data);
		await appData.loadProfiles();
	},

	async updateProfile(profileId: string, data: { name: string; description?: string; config: Record<string, unknown>; avatar_color?: string | null }) {
		await api.put(`/agents/${profileId}`, data);
		await appData.loadProfiles();
	},

	async deleteProfile(profileId: string) {
		await api.delete(`/agents/${profileId}`);
		await appData.loadProfiles();
		// If the deleted profile was currently selected, pick a replacement so
		// new sessions don't load a null profile. The cycle between app-data
		// and active-session is handled by ESM live bindings — both modules
		// only reference each other inside deferred callbacks, never at
		// module-init time.
		try {
			activeSession.validateSelectedProfile(_state.profiles);
		} catch (e) {
			console.error('[appData] Failed to validate profile after delete:', e);
		}
	},

	async restoreProfile(profileId: string) {
		await api.post(`/agents/${profileId}/restore`);
		await appData.loadProfiles();
	},

	// ── Project Operations ──────────────────────────────────────────────

	async loadProjects() {
		await trackCatalogRequest('projects', async () => {
			try {
				const projects = await api.get<Project[]>('/projects');
				_update(s => ({ ...s, projects }));
				settleCatalogHalf('projects', true);
				return true;
			} catch (e) {
				console.error('Failed to load projects:', e);
				settleCatalogHalf('projects', false);
				return false;
			}
		});
	},

	async loadClusterProjects() {
		await trackCatalogRequest('cluster', async () => {
			try {
				const result = await listClusterProjects();
				_update(s => ({ ...s, clusterProjects: result.projects }));
				settleCatalogHalf('cluster', true);
				return true;
			} catch (e) {
				console.error('Failed to load cluster projects:', e);
				settleCatalogHalf('cluster', false);
				return false;
			}
		});
	},

	/**
	 * Refresh both catalog halves for an invalidation seen at `freshSince`
	 * (a performance.now() time). A half whose latest request started at or
	 * after that moment already reflects the change, so it is joined rather
	 * than repeated, unless it failed. At startup this folds the first
	 * sync_ready refresh into the boot load instead of fetching twice.
	 */
	async loadProjectCatalog(freshSince: number) {
		const reuse = async (half: 'projects' | 'cluster', load: () => Promise<void>) => {
			const latest = _catalogLatest[half];
			if (latest && latest.startedAt >= freshSince && (await latest.done)) return;
			await load();
		};
		await Promise.all([reuse('projects', appData.loadProjects), reuse('cluster', appData.loadClusterProjects)]);
	},

	async createProject(data: { id: string; name: string; description?: string; path?: string }) {
		// Return the server's created Project: the backend can rewrite the `id`
		// (auto-suffix on a cross-install collision), so callers must key off the
		// server-assigned id rather than the requested one, otherwise selecting
		// the new project resolves to nothing.
		const created = await api.post<Project>('/projects', data);
		await appData.loadProjects();
		return created;
	},

	async updateProject(projectId: string, data: { name?: string; description?: string; path?: string }) {
		await api.put(`/projects/${projectId}`, data);
		await appData.loadProjects();
	},

	async deleteProject(projectId: string) {
		await api.delete(`/projects/${projectId}`);

		// The backend deletes every session attached to the project. Mirror that
		// cascade locally as soon as the request succeeds so stale chats cannot
		// remain in the sidebar or keep the deleted project in its filter menu.
		const removedSessionIds = new Set(
			_state.sessions.filter(session => session.project_id === projectId).map(session => session.id)
		);
		_update(s => ({
			...s,
			projects: s.projects.filter(project => project.id !== projectId),
			sessions: s.sessions.filter(session => session.project_id !== projectId),
			sessionsTotal: Math.max(0, s.sessionsTotal - removedSessionIds.size),
			...(s.sessions.length === removedSessionIds.size
				? {
					sessionsCursor: null,
					sessionsCursorId: null,
					sessionsHasMore: false,
				}
				: {}),
		}));

		// The DELETE already succeeded, so the cascade is confirmed — but a list
		// load may still be in flight with the pre-delete rows. Tombstone them so
		// its commit cannot resurrect the deleted project's chats.
		recordLocalRemoval(removedSessionIds);
		confirmLocalEdits(removedSessionIds);

		if (activeSession.current.sessionId && removedSessionIds.has(activeSession.current.sessionId)) {
			activeSession.newChat();
		}
		await Promise.all([appData.loadProjects(), appData.loadClusterProjects()]);
	},

	/**
	 * Drop chats owned by a just-deleted install so they cannot linger in the
	 * sidebar or its device filter. The rows stay in the DB; the sidebar hides
	 * them because enrichment no longer resolves a device name.
	 */
	dropDeviceSessions(instanceId: string) {
		const removedSessionIds = new Set(
			_state.sessions.filter(session => session.instance_id === instanceId).map(session => session.id)
		);
		if (removedSessionIds.size === 0) return;
		_update(s => ({
			...s,
			sessions: s.sessions.filter(session => session.instance_id !== instanceId),
			sessionsTotal: Math.max(0, s.sessionsTotal - removedSessionIds.size),
		}));
		// Hold the hide across any load already in flight; the next load started
		// from here on is authoritative (enrichment stops resolving the device).
		recordLocalRemoval(removedSessionIds);
		confirmLocalEdits(removedSessionIds);
		if (activeSession.current.sessionId && removedSessionIds.has(activeSession.current.sessionId)) {
			activeSession.newChat();
		}
	},

	// ── Session Operations ──────────────────────────────────────────────

	async loadSessions(tagId: string | null = _state.sessionsTagFilter) {
		const early = sessionPrefetch;
		sessionPrefetch = null;
		if (early && early.tagId === (tagId ?? null)) {
			// In flight, and nothing asked for a newer load since: wait for it.
			if (early.settledAt === undefined && sessionLoad === early.promise
				&& (pendingSessionLoad === null || pendingSessionLoad === early.request)) {
				await early.promise;
				return;
			}
			// Settled: its list is the committed one and still recent.
			if (early.settledAt !== undefined && sessionLoad === null
				&& sessionListCommits === early.commitsBefore + 1
				&& sessionListCommitRequest === _loadSessionsRequestId
				&& _state.sessionsTagFilter === early.tagId
				&& performance.now() - early.settledAt <= SESSION_PREFETCH_REUSE_MS) return;
		}
		await loadSessionsInternal(tagId);
	},

	/**
	 * Start the sidebar's first session-list load as soon as auth allows,
	 * ahead of the rest of the bootstrap. The next loadSessions() for the same
	 * filter (the app page's boot load) awaits it rather than loading again.
	 */
	prefetchSessions(): void {
		if (!earlySessionListEnabled() || sessionLoad || sessionPrefetch) return;
		const tagId = _state.sessionsTagFilter ?? null;
		const commitsBefore = sessionListCommits;
		const promise = loadSessionsInternal(tagId);
		const entry: SessionPrefetch = { promise, tagId, request: pendingSessionLoad, commitsBefore };
		sessionPrefetch = entry;
		void promise.catch(() => {}).finally(() => { entry.settledAt = performance.now(); });
	},

	async loadMoreSessions() {
		await loadMoreSessionsInternal();
	},

	async loadApiUserSessions() {
		await loadApiUserSessionsInternal();
	},

	async clearSession(sessionId: string) {
		// Clear a session's conversation IN PLACE — the backend wipes its
		// transcript/messages but KEEPS the session row, so (unlike deleteSession)
		// there is nothing to splice out of the list here. The row stays; only its
		// conversation is reset. The caller updates the open chat's local message
		// store separately (activeSession.clearMessages()) for instant feedback.
		await api.post(`/sessions/${sessionId}/clear`);
		// R3, and load-bearing: the row survives with the same id but an emptied
		// event log, so a resume at from_seq=lastSeq matches nothing and would
		// leave the cached transcript on screen. Drop it so the next open replays
		// from seq 0. (The server also fans out `user_session_cleared`, which
		// repeats this invalidation on every device — including this one, where
		// it additionally restarts the feed if the session is open. Keeping the
		// local drop makes the clearing tab correct even with its WS down.)
		feedController.invalidate(sessionId);
		// Refresh the row so its updated_at / turn_count / cost reflect the reset.
		await appData.loadSessions();
	},

	async deleteSession(sessionId: string) {
		// Optimistic removal — splice from local state immediately for instant UI feedback.
		// Snapshot previous state so we can roll back if the API call fails.
		const prevSessions = _state.sessions;
		const prevTotal = _state.sessionsTotal;
		// Tombstone first: a paginated load already in flight still carries the
		// row, and its commit would put the deleted chat back in the sidebar.
		recordLocalRemoval([sessionId]);
		_update(s => ({
			...s,
			sessions: s.sessions.filter(x => x.id !== sessionId),
			sessionsTotal: Math.max(0, s.sessionsTotal - 1),
		}));

		try {
			await api.delete(`/sessions/${sessionId}`);
			confirmLocalEdits([sessionId]);
		} catch (err) {
			// Rollback optimistic removal on error, then resync from server.
			discardLocalEdits([sessionId]);
			_update(s => ({ ...s, sessions: prevSessions, sessionsTotal: prevTotal }));
			await appData.loadSessions();
			throw err;
		}
	},

	async renameSession(sessionId: string, title: string) {
		// Optimistic rename — patch the row's title in local state immediately for
		// instant feedback (mirrors deleteSession's optimistic splice). Snapshot the
		// previous list so we can roll back if the PATCH fails.
		//
		// Registering the edit is what keeps a list load that was ALREADY PAGING
		// when the user renamed from committing the pre-rename row and snapping
		// the title back (S12): every commit re-applies it until the server is
		// known to carry it.
		const prevSessions = _state.sessions;
		recordLocalEdit(sessionId, { title });
		_update(s => ({
			...s,
			sessions: s.sessions.map(x => (x.id === sessionId ? { ...x, title } : x)),
		}));

		try {
			await apiRenameSession(sessionId, title);
			confirmLocalEdits([sessionId]);
		} catch (err) {
			// Roll the optimistic title change back and let the caller surface the error.
			discardLocalEdits([sessionId]);
			_update(s => ({ ...s, sessions: prevSessions }));
			throw err;
		}
	},

	/** Merge a server-pushed field patch (e.g. is_active from a
	 *  user_session_updated WS event) into the matching row without a refetch.
	 *  Returns true when a row was found and updated. */
	mergeSessionPatch(sessionId: string, patch: Partial<Session>): boolean {
		let found = false;
		_update(s => ({
			...s,
			sessions: s.sessions.map(x => {
				if (x.id !== sessionId) return x;
				found = true;
				return { ...x, ...patch };
			}),
		}));
		if (found) {
			// The push came FROM the server, so it is confirmed on arrival — but a
			// list load that started before it must not commit the pre-patch row
			// over it (W7). Re-applied until a load started from here on commits.
			recordLocalEdit(sessionId, patch);
			confirmLocalEdits([sessionId]);
		}
		return found;
	},

	async setSessionActive(sessionId: string, isActive: boolean) {
		// Optimistic Active-membership flip — patch the row's is_active in local
		// state immediately (mirrors renameSession's optimistic splice) so it
		// re-groups between the Active and Recent sections without a full
		// paginated refetch. Snapshot the previous list to roll back if the PATCH
		// fails.
		const prevSessions = _state.sessions;
		// Register BEFORE the PATCH resolves. A load already in flight when the
		// user clicked (e.g. the turn-done refresh) carries the PRE-patch row;
		// re-applying this edit on its commit is what stops the card snapping
		// back into Active for the length of the round-trip. Unlike the old
		// invalidate-the-load guard, the in-flight load's OTHER rows still land.
		recordLocalEdit(sessionId, { is_active: isActive });
		_update(s => ({
			...s,
			sessions: s.sessions.map(x => (x.id === sessionId ? { ...x, is_active: isActive } : x)),
		}));

		// Only the waits that exist now are this settle's to dismiss: by the time
		// the PATCH resolves another device may have unsettled the session and
		// started a turn whose new prompt must survive.
		const promptSnapshot = isActive ? null : activeSession.snapshotPrompts(sessionId);

		try {
			await apiSetSessionActive(sessionId, isActive);
			confirmLocalEdits([sessionId]);
			// Settling dismisses the session's pending permission/question on the
			// server (#2324); drop the local copies now rather than waiting for the
			// echoed frame. Unsettle starts fresh, so it clears nothing.
			if (promptSnapshot) activeSession.dismissSettledPrompts(sessionId, promptSnapshot);
			// Reconcile with an authoritative reload.
			await loadSessionsInternal(_state.sessionsTagFilter);
		} catch (err) {
			// Roll the optimistic change back and let the caller surface the error.
			discardLocalEdits([sessionId]);
			_update(s => ({ ...s, sessions: prevSessions }));
			throw err;
		}
	},

	/**
	 * Archive a session (out of the history, and out of Active) or restore it.
	 * Optimistic like setSessionActive; `isActive` is where a restore lands, so
	 * undoing an archive puts an Active session back in Active.
	 */
	async setSessionArchived(sessionId: string, archived: boolean, isActive = false) {
		const prevSessions = _state.sessions;
		const patch: Partial<Session> = archived
			? { status: 'archived', is_active: false }
			: { status: 'active', is_active: isActive };
		recordLocalEdit(sessionId, patch);
		_update(s => ({
			...s,
			sessions: s.sessions.map(x => (x.id === sessionId ? { ...x, ...patch } : x)),
		}));
		// An open archived chat loses its composer; a restored one gets it back.
		archivedChats.mark(sessionId, archived);
		try {
			await apiSetSessionArchived(sessionId, archived, isActive);
			confirmLocalEdits([sessionId]);
			await loadSessionsInternal(_state.sessionsTagFilter);
		} catch (err) {
			discardLocalEdits([sessionId]);
			_update(s => ({ ...s, sessions: prevSessions }));
			archivedChats.mark(sessionId, !archived);
			throw err;
		}
	},

	// ── Search ──────────────────────────────────────────────────────────

	async searchSessions(query: string) {
		await searchSessionsInternal(query);
	},
};

// ─── Derived Convenience Stores (via store-utils createDerived) ─────────────

export const profiles = createDerived(() => _state.profiles);
export const profilesLoaded = createDerived(() => _state.profilesLoaded);
export const projects = createDerived(() => _state.projects);
export const clusterProjects = createDerived(() => _state.clusterProjects);
export const sessions = createDerived(() => _state.sessions);
export const sessionsTagFilter = createDerived(() => _state.sessionsTagFilter);
/** True when the server has more session pages behind the loaded ones — drives
 *  the sidebar's explicit "load more" shelf now that the initial load is capped. */
export const sessionsHasMore = createDerived(() => _state.sessionsHasMore);
export const sessionsLoadingMore = createDerived(() => _state.sessionsLoadingMore);
export const apiUserSessions = createDerived(() => _state.apiUserSessions);
