/**
 * App Data Store
 *
 * Global application data: profiles, projects, sessions, and selection state.
 * Extracted from tabs.ts — these are not per-session state.
 */

import { writable, derived, get } from 'svelte/store';
import type { Session, Profile, PaginatedSessions, SessionSearchResult, PaginatedSessionSearchResults } from '$lib/api/client';
import { api, searchSessionsPaginated } from '$lib/api/client';
import type { Project } from './types';
import { messageCache } from './message-cache';

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

// ─── Store ───────────────────────────────────────────────────────────────────

function createAppDataStore() {
	const store = writable<AppDataState>({
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

	const { subscribe, update } = store;

	// ─── Data Loading ─────────────────────────────────────────────────────────

	async function loadSessionsInternal(tagId?: string | null) {
		update(s => ({ ...s, sessionsLoading: true }));
		try {
			let url = `/sessions/paginated?limit=${SESSIONS_PAGE_SIZE}&offset=0&admin_only=true`;
			if (tagId) {
				url += `&tag_id=${encodeURIComponent(tagId)}`;
			}
			const result = await api.get<PaginatedSessions>(url);
			update(s => ({
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
			update(s => ({ ...s, sessionsLoading: false }));
		}
	}

	async function loadMoreSessionsInternal() {
		const state = get(store);
		if (state.sessionsLoadingMore || !state.sessionsHasMore) return;

		update(s => ({ ...s, sessionsLoadingMore: true }));
		try {
			const newOffset = state.sessionsOffset + SESSIONS_PAGE_SIZE;
			let url = `/sessions/paginated?limit=${SESSIONS_PAGE_SIZE}&offset=${newOffset}&admin_only=true`;
			if (state.sessionsTagFilter) {
				url += `&tag_id=${encodeURIComponent(state.sessionsTagFilter)}`;
			}
			const result = await api.get<PaginatedSessions>(url);
			update(s => ({
				...s,
				sessions: [...s.sessions, ...result.items],
				sessionsOffset: newOffset,
				sessionsHasMore: result.has_more,
				sessionsLoadingMore: false,
			}));
		} catch (e) {
			console.error('Failed to load more sessions:', e);
			update(s => ({ ...s, sessionsLoadingMore: false }));
		}
	}

	async function searchSessionsInternal(query: string) {
		if (!query.trim()) {
			update(s => ({
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

		update(s => ({ ...s, searchLoading: true, searchQuery: query }));
		try {
			const result = await searchSessionsPaginated(query, {
				adminOnly: true,
				limit: SEARCH_PAGE_SIZE,
				offset: 0,
			});
			update(s => ({
				...s,
				searchResults: result.items,
				searchTotal: result.total,
				searchOffset: 0,
				searchHasMore: result.has_more,
				searchLoading: false,
			}));
		} catch (e) {
			console.error('Failed to search sessions:', e);
			update(s => ({ ...s, searchLoading: false }));
		}
	}

	async function loadMoreSearchResultsInternal() {
		const state = get(store);
		if (state.searchLoadingMore || !state.searchHasMore || !state.searchQuery) return;

		update(s => ({ ...s, searchLoadingMore: true }));
		try {
			const newOffset = state.searchOffset + SEARCH_PAGE_SIZE;
			const result = await searchSessionsPaginated(state.searchQuery, {
				adminOnly: true,
				limit: SEARCH_PAGE_SIZE,
				offset: newOffset,
			});
			update(s => ({
				...s,
				searchResults: [...s.searchResults, ...result.items],
				searchOffset: newOffset,
				searchHasMore: result.has_more,
				searchLoadingMore: false,
			}));
		} catch (e) {
			console.error('Failed to load more search results:', e);
			update(s => ({ ...s, searchLoadingMore: false }));
		}
	}

	async function loadApiUserSessionsInternal() {
		update(s => ({ ...s, apiUserSessionsLoading: true }));
		try {
			const result = await api.get<PaginatedSessions>(
				`/sessions/paginated?limit=100&offset=0&api_users_only=true&admin_only=false`
			);
			update(s => ({
				...s,
				apiUserSessions: result.items,
				apiUserSessionsLoading: false,
			}));
		} catch (e) {
			console.error('Failed to load API user sessions:', e);
			update(s => ({ ...s, apiUserSessionsLoading: false }));
		}
	}

	// ─── Public API ───────────────────────────────────────────────────────────

	return {
		subscribe,

		// ── Profile Operations ──────────────────────────────────────────────

		async loadProfiles() {
			try {
				const profiles = await api.get<Profile[]>('/profiles');
				update(s => ({ ...s, profiles }));
			} catch (e) {
				console.error('Failed to load profiles:', e);
			}
		},

		async createProfile(data: { id: string; name: string; description?: string; config: Record<string, unknown> }) {
			await api.post('/profiles', data);
			await this.loadProfiles();
		},

		async updateProfile(profileId: string, data: { name: string; description?: string; config: Record<string, unknown> }) {
			await api.put(`/profiles/${profileId}`, data);
			await this.loadProfiles();
		},

		async deleteProfile(profileId: string) {
			await api.delete(`/profiles/${profileId}`);
			await this.loadProfiles();
		},

		// ── Project Operations ──────────────────────────────────────────────

		async loadProjects() {
			try {
				const projects = await api.get<Project[]>('/projects');
				update(s => ({ ...s, projects }));
			} catch (e) {
				console.error('Failed to load projects:', e);
			}
		},

		async createProject(data: { id: string; name: string; description?: string; path?: string }) {
			await api.post('/projects', data);
			await this.loadProjects();
		},

		async deleteProject(projectId: string) {
			await api.delete(`/projects/${projectId}`);
			await this.loadProjects();
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
			update(s => ({ ...s, sessionsTagFilter: tagId }));
			loadSessionsInternal(tagId);
		},

		async deleteSession(sessionId: string) {
			await api.delete(`/sessions/${sessionId}`);
			await this.loadSessions();
			messageCache.delete(sessionId);
		},

		async deleteSelectedSessions() {
			const state = get(store);
			const sessionIds = Array.from(state.selectedSessionIds);
			if (sessionIds.length === 0) return;

			await api.post('/sessions/batch-delete', { session_ids: sessionIds });
			await this.loadSessions();

			for (const id of sessionIds) {
				messageCache.delete(id);
			}

			update(s => ({
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
			update(s => ({
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
			update(s => ({
				...s,
				selectionMode: !s.selectionMode,
				selectedSessionIds: new Set<string>(),
			}));
		},

		exitSelectionMode() {
			update(s => ({
				...s,
				selectionMode: false,
				selectedSessionIds: new Set<string>(),
			}));
		},

		toggleSessionSelection(sessionId: string) {
			update(s => {
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
			update(s => {
				const allIds = new Set(s.sessions.map(session => session.id));
				return { ...s, selectedSessionIds: allIds };
			});
		},

		deselectAllSessions() {
			update(s => ({ ...s, selectedSessionIds: new Set<string>() }));
		},
	};
}

// ─── Export store and derived convenience stores ─────────────────────────────

export const appData = createAppDataStore();

export const profiles = derived(appData, $d => $d.profiles);
export const projects = derived(appData, $d => $d.projects);
export const sessions = derived(appData, $d => $d.sessions);
export const selectedSessionIds = derived(appData, $d => $d.selectedSessionIds);
export const selectionMode = derived(appData, $d => $d.selectionMode);
export const sessionsLoading = derived(appData, $d => $d.sessionsLoading);
export const sessionsTagFilter = derived(appData, $d => $d.sessionsTagFilter);
export const apiUserSessions = derived(appData, $d => $d.apiUserSessions);
