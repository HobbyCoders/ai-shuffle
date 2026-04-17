/**
 * Session Switcher — Session switching, caching, and persistence logic.
 *
 * Architecture (polling-only):
 * - Session loading uses HTTP via sessionPoller.loadSession() — NOT WS messages.
 * - No more waitingForHistory, expectedSessionId, or __reject_all__ sentinels.
 * - All session state (messages, isStreaming, etc.) comes from the poller.
 */

import type { ActiveSessionState, PersistedSession } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import type { SessionEphemeral } from '../session-cache';
import type { ChatMessage } from '../types';
import type { PollAppliedData } from './session-poller';
import { api } from '$lib/api/client';
import { getOrCreateDeviceId } from '../device-id';

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

	// Shared mutable state
	internal: InternalMutableState;

	// External dependencies
	sessionEphemeralCache: {
		save: (sessionId: string, ephemeral: SessionEphemeral) => void;
		restore: (sessionId: string) => SessionEphemeral | null;
	};
	messageCache: {
		get: (sessionId: string) => Promise<ChatMessage[] | null>;
		delete: (sessionId: string) => Promise<void>;
	};
	sessionPoller: {
		stop: (sessionId: string) => void;
		loadSession: (sessionId: string) => Promise<PollAppliedData | null>;
		startStreaming: (sessionId: string) => void;
	};
	sessionState?: {
		removeSession: (sessionId: string) => void;
	};

	// Callbacks
	onPersistTimerChanged: (timer: ReturnType<typeof setTimeout> | null) => void;
}

/** Worktree metadata from the session list, used to restore worktree state
 * when the ephemeral cache doesn't have an entry for the target session. */
export interface SessionWorktreeInfo {
	worktreeId: string | null;
	worktreeBranch: string;
	worktreeDeleted: boolean;
	executionMode: 'local' | 'worktree';
}

export interface SessionSwitcher {
	switchSession: (sessionId: string, projectId?: string | null, worktreeInfo?: SessionWorktreeInfo | null, title?: string | null) => Promise<void>;
	newChat: () => void;
	persistSession: () => void;
	loadPersistedSession: () => Promise<PersistedSession | null>;
	getPersistedProfile: () => string;
	getPersistedProject: () => string;
	loadSelectionFromBackend: () => Promise<{ selectedProfile: string; selectedProject: string } | null>;
	saveSelectionToBackend: (profile: string, project: string) => void;
	destroy: () => void;
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
		const deviceId = getOrCreateDeviceId();
		const prefKey = `selection:${deviceId}`;
		const response = await fetch(`/api/v1/preferences/${encodeURIComponent(prefKey)}`, { credentials: 'include' });
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
	const { update, getState, wsSend, internal, sessionEphemeralCache, messageCache, sessionPoller, onPersistTimerChanged } = deps;

	let persistTimer: ReturnType<typeof setTimeout> | null = null;
	let pendingSelection: { profile: string; project: string } | null = null;
	let selectionSyncTimeout: ReturnType<typeof setTimeout> | null = null;
	let _switchGeneration = 0;

	// ── Helpers ──────────────────────────────────────────────────────────

	function buildEphemeral(state: ActiveSessionState): SessionEphemeral {
		return {
			title: state.title,
			draft: state.draft,
			scrollTop: state.scrollTop,
			project: state.project,
			modelOverride: state.modelOverride,
			permissionModeOverride: state.permissionModeOverride,
			effortLevelOverride: state.effortLevelOverride,
			fastMode: state.fastMode,
			executionMode: state.executionMode,
			worktreeId: state.worktreeId,
			worktreeBranch: state.worktreeBranch,
			worktreeBaseBranch: state.worktreeBaseBranch,
			selectedWorktreeBranch: state.selectedWorktreeBranch,
			worktreeMode: state.worktreeMode,
			worktreeDeleted: state.worktreeDeleted,
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
				const deviceId = getOrCreateDeviceId();
				const prefKey = `selection:${deviceId}`;
				await api.put(`/preferences/${encodeURIComponent(prefKey)}`, {
					key: prefKey,
					value: { selectedProfile: pendingSelection.profile, selectedProject: pendingSelection.project },
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
		const deviceId = getOrCreateDeviceId();
		const prefKey = `active_session:${deviceId}`;
		return JSON.stringify({
			key: prefKey,
			value: {
				sessionId: state.sessionId,
				title: state.title,
				profile: state.profile,
				project: state.project,
				modelOverride: state.modelOverride,
				permissionModeOverride: state.permissionModeOverride,
				effortLevelOverride: state.effortLevelOverride,
				executionMode: state.executionMode,
				worktreeId: state.worktreeId,
				worktreeBranch: state.worktreeBranch,
				worktreeBaseBranch: state.worktreeBaseBranch,
				selectedWorktreeBranch: state.selectedWorktreeBranch,
				worktreeMode: state.worktreeMode,
				worktreeDeleted: state.worktreeDeleted,
			} satisfies PersistedSession,
		});
	}

	async function doPersist() {
		try {
			const deviceId = getOrCreateDeviceId();
			const prefKey = `active_session:${deviceId}`;
			await fetch(`/api/v1/preferences/${encodeURIComponent(prefKey)}`, {
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

	function persistSessionImmediate() {
		if (persistTimer) clearTimeout(persistTimer);
		persistTimer = null;
		onPersistTimerChanged(persistTimer);
		doPersist();
	}

	async function loadPersistedSession(): Promise<PersistedSession | null> {
		try {
			const deviceId = getOrCreateDeviceId();
			const prefKey = `active_session:${deviceId}`;
			const response = await api.get<{ key: string; value: PersistedSession } | null>(`/preferences/${encodeURIComponent(prefKey)}`);
			if (response?.value) return response.value;
		} catch {
			// No persisted session — fresh start
		}
		return null;
	}

	// ── Session Navigation ──────────────────────────────────────────────

	async function switchSession(sessionId: string, projectId?: string | null, worktreeInfo?: SessionWorktreeInfo | null, title?: string | null) {
		const generation = ++_switchGeneration;
		const state = getState();
		// switchSession: gen=${generation}, leaving=${state.sessionId}

		// Stop polling and clean up session state for the session we're leaving.
		// Without removeSession, the old session lingers in sessionState with
		// isStreaming: true, causing stale streaming detection if a late poll arrives.
		if (state.sessionId) {
			sessionPoller.stop(state.sessionId);
			deps.sessionState?.removeSession(state.sessionId);
		}

		// Reset cross-session internal state
		internal.pendingTools.clear();
		internal.interruptPending = false;
		internal.awaitingNewSession = false;
		internal.worktreeCreationGeneration++;
		// NOTE: do NOT abort an in-flight autoCreateWorktree POST on nav —
		// the backend now creates the worktree + session AND kicks off the
		// first query in one request. Aborting would cancel the user's
		// intended work. The generation counter still prevents stale
		// responses from mutating the now-current session.
		internal.worktreeCreateAbort = null;
		if (internal.interruptConfirmResolve) {
			internal.interruptConfirmResolve(false);
			internal.interruptConfirmResolve = null;
		}

		// Save ephemeral for session we're leaving
		const leavingKey = state.sessionId || '__new_chat__';
		sessionEphemeralCache.save(leavingKey, buildEphemeral(state));

		const ephemeral = sessionEphemeralCache.restore(sessionId);
		const targetProject = projectId || ephemeral?.project || state.project;

		// Hydrate from IndexedDB cache for instant render
		const cachedMessages = await messageCache.get(sessionId);

		// A newer switchSession/newChat call was made while we were awaiting — bail out.
		if (generation !== _switchGeneration) return;

		// Single update: set sessionId, cached messages, reset + restore ephemeral state.
		// This is one _update call instead of the previous two, preventing
		// Svelte 5 double-update issues.
		//
		// IMPORTANT: Worktree state fallback uses worktreeInfo (from session list)
		// then safe defaults — NEVER s.currentValue. Using s.currentValue caused
		// worktreeDeleted to bleed between sessions (the leaving session's state
		// would infect the target session when no ephemeral cache existed).
		// Resolve title: caller-provided (from sessions list API) > ephemeral cache > 'New Chat'
		// Caller-provided title is authoritative (from the backend); ephemeral may
		// be stale (e.g., 'New Chat' saved before the backend set the real title).
		const resolvedTitle = title || ephemeral?.title || 'New Chat';

		update(s => ({
			...s,
			sessionId,
			title: resolvedTitle,
			project: targetProject,
			messages: cachedMessages && cachedMessages.length > 0 ? cachedMessages : [],
			error: null,
			pendingPermissions: [],
			pendingQuestions: [],
			isStreaming: false,
			wsStreamContent: '',
			pendingInterruptPrompt: null,
			isCreatingWorktree: false,
			isReopeningWorktree: false,
			worktreeError: null,
			sessionLocked: false,
			sessionLockedBy: undefined,
			// Mark context as pending — null signals "not yet loaded" so the
			// context bar holds its last value instead of flashing to 0%.
			// loadSession → onPollApplied will populate with correct values.
			contextUsed: null,
			// Merge ephemeral state in the same update
			draft: ephemeral?.draft || '',
			scrollTop: ephemeral?.scrollTop || 0,
			modelOverride: ephemeral?.modelOverride ?? s.modelOverride,
			permissionModeOverride: ephemeral?.permissionModeOverride ?? s.permissionModeOverride,
			effortLevelOverride: ephemeral?.effortLevelOverride ?? s.effortLevelOverride,
			fastMode: ephemeral?.fastMode ?? false,
			// Worktree fields: ephemeral > worktreeInfo from session list > safe defaults
			executionMode: ephemeral?.executionMode ?? worktreeInfo?.executionMode ?? 'local',
			worktreeId: ephemeral?.worktreeId ?? worktreeInfo?.worktreeId ?? null,
			worktreeBranch: ephemeral?.worktreeBranch ?? worktreeInfo?.worktreeBranch ?? '',
			worktreeBaseBranch: ephemeral?.worktreeBaseBranch ?? 'main',
			selectedWorktreeBranch: ephemeral?.selectedWorktreeBranch ?? null,
			worktreeMode: ephemeral?.worktreeMode ?? 'new',
			worktreeDeleted: ephemeral?.worktreeDeleted ?? worktreeInfo?.worktreeDeleted ?? false,
			queuedMessages: [],  // Populated from backend via history/queue_updated
		}));

		// Load full session state via HTTP — sole authority for session data.
		// This replaces the old WS load_session → history flow.
		const pollData = await sessionPoller.loadSession(sessionId);

		// A newer switchSession/newChat call was made while we were awaiting — bail out.
		// Stop the poller for this now-stale session to avoid leaking resources.
		if (generation !== _switchGeneration) {
			sessionPoller.stop(sessionId);
			return;
		}

		// Register with SyncEngine to receive broadcast events from other devices.
		// Session state is loaded via HTTP, but real-time sync (streaming from
		// other devices) requires WS registration.
		wsSend({ type: 'watch_session', session_id: sessionId });

		if (pollData) {
			// If session is streaming, start the poller for ongoing updates
			if (pollData.isStreaming) {
				sessionPoller.startStreaming(sessionId);
				// Request pending permissions/questions via WS
				wsSend({ type: 'get_pending_permissions' });
				wsSend({ type: 'get_pending_questions' });
			}
		} else {
			// loadSession failed (HTTP error, timeout, network error).
			// Set error state so the UI shows an error banner instead of
			// appearing frozen in a loading limbo.
			update(s => ({
				...s,
				error: 'Failed to load session. Try starting a new chat.',
				contextUsed: 0,
			}));
		}

		persistSession();
	}

	function newChat() {
		++_switchGeneration;
		const state = getState();
		const leavingSessionId = state.sessionId;
		// newChat: leaving sessionId=${leavingSessionId}

		// Stop polling and clean up session state for the session we're leaving.
		// Without removeSession, the old session lingers with isStreaming: true,
		// causing stale streaming detection if a late poll arrives.
		if (leavingSessionId) {
			sessionPoller.stop(leavingSessionId);
			deps.sessionState?.removeSession(leavingSessionId);
		}

		// Save ephemeral for the session we're leaving
		const leavingKey = leavingSessionId || '__new_chat__';
		sessionEphemeralCache.save(leavingKey, buildEphemeral(state));

		// Tell backend to release session lock
		if (leavingSessionId) {
			wsSend({ type: 'close_session' });
		}

		// Clear stale cached messages for the old session from IndexedDB.
		// Without this, a component lifecycle edge case (e.g., init() re-running
		// after a WS reconnect/remount) could hydrate from the cache and show
		// old messages in the "new chat" view.
		if (leavingSessionId) {
			messageCache.delete(leavingSessionId).catch(() => {});
		}

		// Reset all internal state
		internal.pendingTools.clear();
		internal.interruptPending = false;
		internal.awaitingNewSession = false;
		internal.worktreeCreationGeneration++;
		// NOTE: do NOT abort an in-flight autoCreateWorktree POST on nav —
		// the backend now creates the worktree + session AND kicks off the
		// first query in one request. Aborting would cancel the user's
		// intended work. The generation counter still prevents stale
		// responses from mutating the now-current session.
		internal.worktreeCreateAbort = null;
		if (internal.interruptConfirmResolve) {
			internal.interruptConfirmResolve(false);
			internal.interruptConfirmResolve = null;
		}

		// Restore new chat draft if one was saved earlier
		const newChatEphemeral = sessionEphemeralCache.restore('__new_chat__');

		// Single update: reset everything (including draft from ephemeral cache)
		update(s => ({
			...s,
			sessionId: null,
			sessionProfileId: null,
			currentQueryProfileId: null,
			profile: getPersistedProfile() || s.profile,
			project: getPersistedProject() || s.project,
			messages: [],
			isStreaming: false,
			wsStreamContent: '',
			pendingInterruptPrompt: null,
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
			effortLevelOverride: null,
			pendingPermissions: [],
			pendingQuestions: [],
			todos: [],
			queuedMessages: [],
			draft: newChatEphemeral?.draft || '',
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
			isReopeningWorktree: false,
			worktreeError: null,
			worktreeDeleted: false,
			fastMode: false,
			sessionLocked: false,
			sessionLockedBy: undefined,
		}));

		const afterState = getState();
		// newChat complete

		// Persist immediately (not debounced)
		persistSessionImmediate();
	}

	function destroy() {
		if (selectionSyncTimeout) {
			clearTimeout(selectionSyncTimeout);
			selectionSyncTimeout = null;
		}
		if (persistTimer) {
			clearTimeout(persistTimer);
			persistTimer = null;
		}
	}

	return {
		switchSession,
		newChat,
		persistSession,
		loadPersistedSession,
		getPersistedProfile,
		getPersistedProject,
		loadSelectionFromBackend,
		saveSelectionToBackend,
		destroy,
	};
}
