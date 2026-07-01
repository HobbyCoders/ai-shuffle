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
import { api, searchSessionsPaginated } from '$lib/api/client';
import type { Project } from './types';
import { createSubscribers } from './store-utils';
import { activeSession } from './active-session';

// Re-export the Project type so consumers migrating from the old file get it
export type { Project };

// ─── Types ───────────────────────────────────────────────────────────────────

interface AppDataState {
	profiles: Profile[];
	projects: Project[];
	sessions: Session[];
	// Selection state for batch operations
	selectedSessionIds: Set<string>;
	selectionMode: boolean;
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

// Per-request page size when paging through sessions. The sessions list itself
// is uncapped — loadSessionsInternal keeps fetching pages until the backend
// reports no more. 100 is the max the /sessions/cursor endpoint allows per call,
// so it minimises round-trips when loading everything.
const SESSIONS_PAGE_SIZE = 100;
const SEARCH_PAGE_SIZE = 20;

// ─── Reactive State ──────────────────────────────────────────────────────────

let _state = $state<AppDataState>({
	profiles: [],
	projects: [],
	sessions: [],
	selectedSessionIds: new Set<string>(),
	selectionMode: false,
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

// ─── Internal Data Loading ───────────────────────────────────────────────────

// Monotonic token so that if a newer load (tag change, manual reload) starts
// while an earlier one is still paging, the stale loop bails out instead of
// clobbering the newer results.
let _loadSessionsRequestId = 0;

async function loadSessionsInternal(tagId?: string | null) {
	const requestId = ++_loadSessionsRequestId;
	_update(s => ({ ...s, sessionsLoading: true }));
	try {
		const all: Session[] = [];
		let cursor: string | null = null;
		let cursorId: string | null = null;
		let hasMore = true;

		// Page through every session via cursor pagination. The list is
		// intentionally uncapped, so we keep fetching until the backend reports
		// there is nothing left. Each page is committed as it arrives so the UI
		// renders incrementally instead of waiting for the full set.
		while (hasMore) {
			let url = `/sessions/cursor?limit=${SESSIONS_PAGE_SIZE}&admin_only=true`;
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
			hasMore = result.has_more;
			cursor = result.next_cursor;
			cursorId = result.next_cursor_id;

			// Defend against a backend that reports has_more without advancing
			// the cursor, which would otherwise loop forever.
			if (hasMore && !cursor && !cursorId) {
				hasMore = false;
			}

			_update(s => ({
				...s,
				sessions: [...all],
				sessionsTotal: all.length,
				sessionsOffset: all.length,
				sessionsHasMore: hasMore,
				sessionsTagFilter: tagId ?? null,
				sessionsCursor: cursor,
				sessionsCursorId: cursorId,
			}));
		}

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

	_update(s => ({ ...s, sessionsLoadingMore: true }));
	try {
		let url = `/sessions/cursor?limit=${SESSIONS_PAGE_SIZE}&admin_only=true`;
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

		// Verify cursor and tag filter haven't changed while we were fetching.
		// If they changed (e.g. user switched tag filter, or sessions were
		// reloaded), discard this stale response to avoid duplicates.
		if (_state.sessionsCursor !== capturedCursor || _state.sessionsCursorId !== capturedCursorId || _state.sessionsTagFilter !== capturedTagFilter) {
			_update(s => ({ ...s, sessionsLoadingMore: false }));
			return;
		}

		_update(s => ({
			...s,
			sessions: [...s.sessions, ...result.items],
			sessionsOffset: s.sessionsOffset + result.items.length,
			sessionsTotal: s.sessionsOffset + result.items.length + (result.has_more ? 1 : 0),
			sessionsHasMore: result.has_more,
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

async function loadMoreSearchResultsInternal() {
	if (_state.searchLoadingMore || !_state.searchHasMore || !_state.searchQuery) return;

	_update(s => ({ ...s, searchLoadingMore: true }));
	try {
		const newOffset = _state.searchOffset + SEARCH_PAGE_SIZE;
		const result = await searchSessionsPaginated(_state.searchQuery, {
			adminOnly: true,
			limit: SEARCH_PAGE_SIZE,
			offset: newOffset,
		});
		_update(s => ({
			...s,
			searchResults: [...s.searchResults, ...result.items],
			searchOffset: newOffset,
			searchHasMore: result.has_more,
			searchLoadingMore: false,
		}));
	} catch (e) {
		console.error('Failed to load more search results:', e);
		_update(s => ({ ...s, searchLoadingMore: false }));
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

export const appData = {
	get current() { return _state; },

	subscribe,

	// ── Profile Operations ──────────────────────────────────────────────

	async loadProfiles() {
		try {
			const profiles = await api.get<Profile[]>('/profiles');
			_update(s => ({ ...s, profiles }));
		} catch (e) {
			console.error('Failed to load profiles:', e);
		}
	},

	async createProfile(data: { id: string; name: string; description?: string; config: Record<string, unknown> }) {
		await api.post('/profiles', data);
		await appData.loadProfiles();
	},

	async updateProfile(profileId: string, data: { name: string; description?: string; config: Record<string, unknown> }) {
		await api.put(`/profiles/${profileId}`, data);
		await appData.loadProfiles();
	},

	async deleteProfile(profileId: string) {
		await api.delete(`/profiles/${profileId}`);
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
		await api.post(`/profiles/${profileId}/restore`);
		await appData.loadProfiles();
	},

	// ── Project Operations ──────────────────────────────────────────────

	async loadProjects() {
		try {
			const projects = await api.get<Project[]>('/projects');
			_update(s => ({ ...s, projects }));
		} catch (e) {
			console.error('Failed to load projects:', e);
		}
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
		await appData.loadProjects();
	},

	// ── Session Operations ──────────────────────────────────────────────

	async loadSessions(tagId?: string | null) {
		await loadSessionsInternal(tagId);
	},

	async loadMoreSessions() {
		await loadMoreSessionsInternal();
	},

	async loadApiUserSessions() {
		await loadApiUserSessionsInternal();
	},

	setSessionsTagFilter(tagId: string | null) {
		_update(s => ({ ...s, sessionsTagFilter: tagId }));
		loadSessionsInternal(tagId);
	},

	async deleteSession(sessionId: string) {
		// Optimistic removal — splice from local state immediately for instant UI feedback.
		// Snapshot previous state so we can roll back if the API call fails.
		const prevSessions = _state.sessions;
		const prevTotal = _state.sessionsTotal;
		_update(s => ({
			...s,
			sessions: s.sessions.filter(x => x.id !== sessionId),
			sessionsTotal: Math.max(0, s.sessionsTotal - 1),
		}));

		try {
			await api.delete(`/sessions/${sessionId}`);
		} catch (err) {
			// Rollback optimistic removal on error, then resync from server.
			_update(s => ({ ...s, sessions: prevSessions, sessionsTotal: prevTotal }));
			await appData.loadSessions();
			throw err;
		}
	},

	async deleteSelectedSessions() {
		const sessionIds = Array.from(_state.selectedSessionIds);
		if (sessionIds.length === 0) return;

		const idSet = new Set(sessionIds);
		const prevSessions = _state.sessions;
		const prevTotal = _state.sessionsTotal;

		// Optimistic removal of all selected sessions.
		_update(s => ({
			...s,
			sessions: s.sessions.filter(x => !idSet.has(x.id)),
			sessionsTotal: Math.max(0, s.sessionsTotal - idSet.size),
			selectionMode: false,
			selectedSessionIds: new Set<string>(),
		}));

		try {
			await api.post('/sessions/batch-delete', { session_ids: sessionIds });
		} catch (err) {
			_update(s => ({ ...s, sessions: prevSessions, sessionsTotal: prevTotal }));
			await appData.loadSessions();
			throw err;
		}
	},

	// ── Search ──────────────────────────────────────────────────────────

	async searchSessions(query: string) {
		await searchSessionsInternal(query);
	},

	async loadMoreSearchResults() {
		await loadMoreSearchResultsInternal();
	},

	clearSearch() {
		_update(s => ({
			...s,
			searchQuery: '',
			searchResults: [],
			searchTotal: 0,
			searchOffset: 0,
			searchHasMore: false,
			searchLoading: false,
			searchLoadingMore: false,
		}));
	},

	// ── Selection Mode ──────────────────────────────────────────────────

	toggleSelectionMode() {
		_update(s => ({
			...s,
			selectionMode: !s.selectionMode,
			selectedSessionIds: new Set<string>(),
		}));
	},

	exitSelectionMode() {
		_update(s => ({
			...s,
			selectionMode: false,
			selectedSessionIds: new Set<string>(),
		}));
	},

	toggleSessionSelection(sessionId: string) {
		_update(s => {
			const newSet = new Set(s.selectedSessionIds);
			if (newSet.has(sessionId)) {
				newSet.delete(sessionId);
			} else {
				newSet.add(sessionId);
			}
			return { ...s, selectedSessionIds: newSet };
		});
	},

	selectAllSessions() {
		_update(s => {
			const allIds = new Set(s.sessions.map(session => session.id));
			return { ...s, selectedSessionIds: allIds };
		});
	},

	deselectAllSessions() {
		_update(s => ({ ...s, selectedSessionIds: new Set<string>() }));
	},
};

// ─── Derived Convenience Stores (via store-utils createDerived) ─────────────

export const profiles = createDerived(() => _state.profiles);
export const projects = createDerived(() => _state.projects);
export const sessions = createDerived(() => _state.sessions);
export const selectedSessionIds = createDerived(() => _state.selectedSessionIds);
export const selectionMode = createDerived(() => _state.selectionMode);
export const sessionsLoading = createDerived(() => _state.sessionsLoading);
export const sessionsTagFilter = createDerived(() => _state.sessionsTagFilter);
export const apiUserSessions = createDerived(() => _state.apiUserSessions);
