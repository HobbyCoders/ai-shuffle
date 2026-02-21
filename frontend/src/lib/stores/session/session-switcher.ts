/**
 * Session Switcher — Session switching, caching, and persistence logic.
 *
 * Extracted from active-session.ts to isolate session navigation
 * (switchSession, newChat) and persistence (persistSession, loadPersistedSession)
 * from the main store factory.
 *
 * Design principle:
 * - All session-switching side effects (ephemeral save/restore, browser panel swap,
 *   WS load_session, persistence) are co-located here.
 * - The store factory injects dependencies via SessionSwitcherDeps so this module
 *   stays decoupled from the concrete store, WS connection, and caches.
 */

import type { ActiveSessionState, PersistedSession } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import type { SessionEphemeral } from '../session-cache';
import type { ChatMessage } from '../types';
import { api } from '$lib/api/client';

// ─── Constants ───────────────────────────────────────────────────────────────

const PERSIST_DEBOUNCE_MS = 500;
const SELECTION_SYNC_DEBOUNCE_MS = 1000;

// ─── Interfaces ──────────────────────────────────────────────────────────────

export interface SessionSwitcherDeps {
	// Store access
	update: (fn: (s: ActiveSessionState) => ActiveSessionState) => void;
	getState: () => ActiveSessionState;

	// WebSocket
	wsSend: (data: Record<string, unknown>) => boolean;
	wsIsConnected: () => boolean;

	// Shared mutable state
	internal: InternalMutableState;

	// External dependencies
	sessionEphemeralCache: {
		save: (sessionId: string, ephemeral: SessionEphemeral) => void;
		restore: (sessionId: string) => SessionEphemeral | null;
	};
	messageCache: {
		get: (sessionId: string) => Promise<ChatMessage[] | null>;
	};
	browserState: {
		switchSession: (fromSessionId: string | null, toSessionId: string | null) => void;
	};
	sessionPoller: {
		stop: (sessionId: string) => void;
	};

	// Callbacks
	onPersistTimerChanged: (timer: ReturnType<typeof setTimeout> | null) => void;
}

export interface SessionSwitcher {
	switchSession: (sessionId: string, projectId?: string | null) => Promise<void>;
	newChat: () => void;
	persistSession: () => void;
	loadPersistedSession: () => Promise<PersistedSession | null>;
	getPersistedProfile: () => string;
	getPersistedProject: () => string;
	loadSelectionFromBackend: () => Promise<{ selectedProfile: string; selectedProject: string } | null>;
	saveSelectionToBackend: (profile: string, project: string) => void;
}

// ─── Module-Level Selection Helpers ──────────────────────────────────────────

export function getPersistedProfile(): string {
	if (typeof window !== 'undefined') {
		return localStorage.getItem('aihub_selectedProfile') || '';
	}
	return '';
}

export function getPersistedProject(): string {
	if (typeof window !== 'undefined') {
		return localStorage.getItem('aihub_selectedProject') || '';
	}
	return '';
}

export async function loadSelectionFromBackend(): Promise<{ selectedProfile: string; selectedProject: string } | null> {
	if (typeof window === 'undefined') return null;
	try {
		const response = await fetch('/api/v1/preferences/selection', { credentials: 'include' });
		if (response.ok) {
			const data = await response.json();
			if (data?.value) {
				const sel = data.value as { selectedProfile?: string; selectedProject?: string };
				const selectedProfile = sel.selectedProfile || '';
				const selectedProject = sel.selectedProject || '';
				if (selectedProfile) localStorage.setItem('aihub_selectedProfile', selectedProfile);
				if (selectedProject) localStorage.setItem('aihub_selectedProject', selectedProject);
				return { selectedProfile, selectedProject };
			}
		}
	} catch (e) {
		console.warn('[ActiveSession] Failed to load selection from backend:', e);
	}
	return null;
}

// ─── Factory ─────────────────────────────────────────────────────────────────

export function createSessionSwitcher(deps: SessionSwitcherDeps): SessionSwitcher {
	const { update, getState, wsSend, wsIsConnected, internal, sessionEphemeralCache, messageCache, browserState, sessionPoller, onPersistTimerChanged } = deps;

	let persistTimer: ReturnType<typeof setTimeout> | null = null;
	let pendingSelection: { profile: string; project: string } | null = null;
	let selectionSyncTimeout: ReturnType<typeof setTimeout> | null = null;

	// ── Helpers ──────────────────────────────────────────────────────────

	function buildEphemeral(state: ActiveSessionState): SessionEphemeral {
		return {
			draft: state.draft,
			scrollTop: state.scrollTop,
			project: state.project,
			modelOverride: state.modelOverride,
			permissionModeOverride: state.permissionModeOverride,
			executionMode: state.executionMode,
			worktreeId: state.worktreeId,
			worktreeBranch: state.worktreeBranch,
			worktreeBaseBranch: state.worktreeBaseBranch,
			selectedWorktreeBranch: state.selectedWorktreeBranch,
			worktreeMode: state.worktreeMode,
			worktreeDeleted: state.worktreeDeleted,
			queuedMessages: state.queuedMessages,
		};
	}

	function saveSelectionToBackend(profile: string, project: string) {
		const effectiveProfile = profile || getPersistedProfile();
		const effectiveProject = project || getPersistedProject();

		if (typeof window !== 'undefined') {
			if (effectiveProfile) localStorage.setItem('aihub_selectedProfile', effectiveProfile);
			if (effectiveProject) localStorage.setItem('aihub_selectedProject', effectiveProject);
		}

		pendingSelection = { profile: effectiveProfile, project: effectiveProject };

		if (selectionSyncTimeout) clearTimeout(selectionSyncTimeout);
		selectionSyncTimeout = setTimeout(async () => {
			if (!pendingSelection) return;
			try {
				await fetch('/api/v1/preferences/selection', {
					method: 'PUT',
					headers: { 'Content-Type': 'application/json' },
					credentials: 'include',
					body: JSON.stringify({
						key: 'selection',
						value: { selectedProfile: pendingSelection.profile, selectedProject: pendingSelection.project }
					})
				});
			} catch (e) {
				console.error('[ActiveSession] Failed to sync selection to backend:', e);
			}
			pendingSelection = null;
			selectionSyncTimeout = null;
		}, SELECTION_SYNC_DEBOUNCE_MS);
	}

	// ── Persistence ─────────────────────────────────────────────────────

	function buildPersistBody() {
		const state = getState();
		return JSON.stringify({
			key: 'active_session',
			value: {
				sessionId: state.sessionId,
				title: state.title,
				profile: state.profile,
				project: state.project,
				modelOverride: state.modelOverride,
				permissionModeOverride: state.permissionModeOverride,
				executionMode: state.executionMode,
				worktreeId: state.worktreeId,
				worktreeBranch: state.worktreeBranch,
				worktreeBaseBranch: state.worktreeBaseBranch,
				selectedWorktreeBranch: state.selectedWorktreeBranch,
				worktreeMode: state.worktreeMode,
				worktreeDeleted: state.worktreeDeleted,
				queuedMessages: state.queuedMessages,
			} satisfies PersistedSession,
		});
	}

	async function doPersist() {
		try {
			await fetch('/api/v1/preferences/active_session', {
				method: 'PUT',
				headers: { 'Content-Type': 'application/json' },
				credentials: 'include',
				body: buildPersistBody(),
			});
		} catch (e) {
			console.error('[ActiveSession] Failed to persist session:', e);
		}
	}

	function persistSession() {
		if (persistTimer) clearTimeout(persistTimer);
		persistTimer = setTimeout(doPersist, PERSIST_DEBOUNCE_MS);
		onPersistTimerChanged(persistTimer);
	}

	/** Persist immediately, bypassing the debounce. Use for deliberate user
	 *  actions (e.g. newChat) where a subsequent init() must see the update. */
	function persistSessionImmediate() {
		if (persistTimer) clearTimeout(persistTimer);
		persistTimer = null;
		onPersistTimerChanged(persistTimer);
		doPersist();
	}

	async function loadPersistedSession(): Promise<PersistedSession | null> {
		try {
			const response = await api.get<{ key: string; value: PersistedSession } | null>('/preferences/active_session');
			if (response?.value) return response.value;
		} catch {
			// No persisted session — fresh start
		}
		return null;
	}

	// ── Session Navigation ──────────────────────────────────────────────

	async function switchSession(sessionId: string, projectId?: string | null) {
		const state = getState();

		// Stop polling for the session we're leaving — prevents stale poll
		// callbacks from writing into _state after sessionId changes.
		if (state.sessionId) {
			sessionPoller.stop(state.sessionId);
		}

		// Reset cross-session internal state to prevent contamination.
		// Without this, pendingTools from the old session extend WS health
		// timeouts, and streamingOnDisconnect/interruptPending from the old
		// session corrupt the new session's event handling.
		internal.pendingTools.clear();
		internal.streamEndCleanedUp = null;
		internal.streamingOnDisconnect = null;
		internal.interruptPending = false;

		// Save ephemeral for session we're leaving (use sentinel for new chat)
		const leavingKey = state.sessionId || '__new_chat__';
		sessionEphemeralCache.save(leavingKey, buildEphemeral(state));

		// Peek at ephemeral cache to get the target session's project immediately.
		// Priority: explicit projectId arg > ephemeral cache > keep current project.
		const ephemeral = sessionEphemeralCache.restore(sessionId);
		const targetProject = projectId || ephemeral?.project || state.project;

		// Hydrate from IndexedDB cache for instant render
		const cachedMessages = await messageCache.get(sessionId);
		update(s => ({
			...s,
			sessionId,
			project: targetProject,
			messages: cachedMessages && cachedMessages.length > 0 ? cachedMessages : [],
			error: null,
			// Reset session-specific state
			draft: '',
			pendingPermissions: [],
			pendingQuestions: [],
			isStreaming: false,
			isCreatingWorktree: false,
			worktreeError: null,
			sessionLocked: false,
			sessionLockedBy: undefined,
		}));

		// Switch browser panel to new session's context (saves/restores per-session).
		// Panel stays open; only the content (URL, screenshot, etc.) swaps.
		browserState.switchSession(state.sessionId, sessionId);

		// Restore remaining ephemeral state from cache (project already applied above)
		if (ephemeral) {
			update(s => ({
				...s,
				draft: ephemeral.draft,
				scrollTop: ephemeral.scrollTop,
				modelOverride: ephemeral.modelOverride,
				permissionModeOverride: ephemeral.permissionModeOverride,
				executionMode: ephemeral.executionMode,
				worktreeId: ephemeral.worktreeId,
				worktreeBranch: ephemeral.worktreeBranch,
				worktreeBaseBranch: ephemeral.worktreeBaseBranch,
				selectedWorktreeBranch: ephemeral.selectedWorktreeBranch,
				worktreeMode: ephemeral.worktreeMode,
				worktreeDeleted: ephemeral.worktreeDeleted,
				queuedMessages: ephemeral.queuedMessages,
			}));
		}

		// Load full session from backend via WS
		if (wsIsConnected()) {
			internal.expectedSessionId = sessionId;
			internal.waitingForHistory = [];
			wsSend({ type: 'load_session', session_id: sessionId });
		}

		persistSession();
	}

	function newChat() {
		const state = getState();

		// Save ephemeral for the session we're leaving
		const leavingKey = state.sessionId || '__new_chat__';
		sessionEphemeralCache.save(leavingKey, buildEphemeral(state));

		// Tell backend to release session lock
		if (state.sessionId) {
			wsSend({ type: 'close_session' });
		}

		// Reset session-specific state, restore profile/project to user defaults
		update(s => ({
			...s,
			sessionId: null,
			sessionProfileId: null,
			currentQueryProfileId: null,
			profile: getPersistedProfile() || s.profile,
			project: getPersistedProject() || s.project,
			messages: [],
			isStreaming: false,
			error: null,
			title: 'New Chat',
			totalTokensIn: 0,
			totalTokensOut: 0,
			totalCacheCreationTokens: 0,
			totalCacheReadTokens: 0,
			contextUsed: null,
			contextMax: 200_000,
			modelOverride: null,
			permissionModeOverride: null,
			pendingPermissions: [],
			pendingQuestions: [],
			todos: [],
			queuedMessages: [],
			draft: '',
			scrollTop: 0,
			streamStartTime: null,
			executionMode: 'local' as const,
			worktreeBranch: '',
			worktreeBaseBranch: 'main',
			worktreeCreateNewBranch: false,
			worktreeId: null,
			worktreeMode: 'new' as const,
			selectedWorktreeBranch: null,
			isCreatingWorktree: false,
			worktreeError: null,
			worktreeDeleted: false,
			sessionLocked: false,
			sessionLockedBy: undefined,
		}));

		internal.pendingTools.clear();
		internal.expectedSessionId = null;
		internal.waitingForHistory = null;

		// Save current browser state and show empty for the new chat
		browserState.switchSession(state.sessionId, null);

		// Restore new chat draft if one was saved earlier
		const newChatEphemeral = sessionEphemeralCache.restore('__new_chat__');
		if (newChatEphemeral?.draft) {
			update(s => ({ ...s, draft: newChatEphemeral.draft }));
		}

		// Persist immediately (not debounced) — this is a deliberate user action
		// and init() may re-run on WS reconnect, reading the persisted state.
		// If the persist is still debounced, init() would restore the old session.
		persistSessionImmediate();
	}

	// ── Return public API ───────────────────────────────────────────────

	return {
		switchSession,
		newChat,
		persistSession,
		loadPersistedSession,
		getPersistedProfile,
		getPersistedProject,
		loadSelectionFromBackend,
		saveSelectionToBackend,
	};
}
