/**
 * App Data Store
 *
 * Global application data: profiles, projects, sessions, and selection state.
 * Extracted from tabs.ts — these are not per-session state.
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import type { Session, Profile, PaginatedSessions, SessionSearchResult, PaginatedSessionSearchResults } from '$lib/api/client';
import { api, searchSessionsPaginated } from '$lib/api/client';
import type { Project } from './types';
import { messageCache } from './message-cache';
import { untrack } from 'svelte';

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

const SESSIONS_PAGE_SIZE = 50;
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

// ─── Subscribe Shim ──────────────────────────────────────────────────────────

type Subscriber<T> = (value: T) => void;

const _subscribers = new Set<Subscriber<AppDataState>>();

function _notifySubscribers() {
	untrack(() => { for (const fn of _subscribers) fn(_state); });
}

function _update(fn: (s: AppDataState) => AppDataState) {
	_state = fn(untrack(() => _state));
	_notifySubscribers();
}

// ─── Internal Data Loading ───────────────────────────────────────────────────

async function loadSessionsInternal(tagId?: string | null) {
	_update(s => ({ ...s, sessionsLoading: true }));
	try {
		let url = `/sessions/paginated?limit=${SESSIONS_PAGE_SIZE}&offset=0&admin_only=true`;
		if (tagId) {
			url += `&tag_id=${encodeURIComponent(tagId)}`;
		}
		const result = await api.get<PaginatedSessions>(url);
		_update(s => ({
			...s,
			sessions: result.items,
			sessionsTotal: result.total,
			sessionsOffset: 0,
			sessionsHasMore: result.has_more,
			sessionsLoading: false,
			sessionsTagFilter: tagId ?? null,
		}));
	} catch (e) {
		console.error('Failed to load sessions:', e);
		_update(s => ({ ...s, sessionsLoading: false }));
	}
}

async function loadMoreSessionsInternal() {
	if (_state.sessionsLoadingMore || !_state.sessionsHasMore) return;

	_update(s => ({ ...s, sessionsLoadingMore: true }));
	try {
		const newOffset = _state.sessionsOffset + SESSIONS_PAGE_SIZE;
		let url = `/sessions/paginated?limit=${SESSIONS_PAGE_SIZE}&offset=${newOffset}&admin_only=true`;
		if (_state.sessionsTagFilter) {
			url += `&tag_id=${encodeURIComponent(_state.sessionsTagFilter)}`;
		}
		const result = await api.get<PaginatedSessions>(url);
		_update(s => ({
			...s,
			sessions: [...s.sessions, ...result.items],
			sessionsOffset: newOffset,
			sessionsHasMore: result.has_more,
			sessionsLoadingMore: false,
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

	subscribe(fn: Subscriber<AppDataState>) {
		_subscribers.add(fn);
		untrack(() => fn(_state));
		return () => { _subscribers.delete(fn); };
	},

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
		await api.post('/projects', data);
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
		await api.delete(`/sessions/${sessionId}`);
		await appData.loadSessions();
		messageCache.delete(sessionId);
	},

	async deleteSelectedSessions() {
		const sessionIds = Array.from(_state.selectedSessionIds);
		if (sessionIds.length === 0) return;

		await api.post('/sessions/batch-delete', { session_ids: sessionIds });
		await appData.loadSessions();

		for (const id of sessionIds) {
			messageCache.delete(id);
		}

		_update(s => ({
			...s,
			selectionMode: false,
			selectedSessionIds: new Set<string>(),
		}));
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

// ─── Derived Convenience Stores ──────────────────────────────────────────────

function createDerived<T>(getter: () => T) {
	return {
		get current() { return getter(); },
		subscribe(fn: Subscriber<T>) {
			const handler: Subscriber<AppDataState> = () => fn(getter());
			_subscribers.add(handler);
			untrack(() => fn(getter()));
			return () => { _subscribers.delete(handler); };
		}
	};
}

export const profiles = createDerived(() => _state.profiles);
export const projects = createDerived(() => _state.projects);
export const sessions = createDerived(() => _state.sessions);
export const selectedSessionIds = createDerived(() => _state.selectedSessionIds);
export const selectionMode = createDerived(() => _state.selectionMode);
export const sessionsLoading = createDerived(() => _state.sessionsLoading);
export const sessionsTagFilter = createDerived(() => _state.sessionsTagFilter);
export const apiUserSessions = createDerived(() => _state.apiUserSessions);
