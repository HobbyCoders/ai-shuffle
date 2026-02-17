/**
 * Active Session Store — Single-session model replacing the multi-tab architecture.
 *
 * One WebSocket connection, one active session at a time.
 * Session switching saves ephemeral state to sessionEphemeralCache and restores it.
 * Polling is delegated to sessionPoller; WebSocket is notification-only for streaming.
 */

import { writable, derived, get } from 'svelte/store';
import type { PermissionRequest as PermissionRequestType, UserQuestionRequest } from '$lib/api/client';
import { api } from '$lib/api/client';
import { notifySessionComplete, notifyWaitingForResponse } from '$lib/services/notifications';

// Session infrastructure
import { sessionPoller } from './session/session-poller';
import { sessionNotifier } from './session/session-notifier';
import { sessionState, type SessionUsage } from './session/session-state';
import { createWsConnection, type WsConnection } from './session/ws-connection';
import { createMessageHandlers, type InternalMutableState } from './session/ws-message-handlers';
import {
	createSessionSwitcher,
	getPersistedProfile, getPersistedProject,
} from './session/session-switcher';

// Caches and helpers
import { messageCache } from './message-cache';
import { sessionEphemeralCache } from './session-cache';
import { appData } from './app-data';
import { git } from './git';
import { browserState } from './browser';

// Shared types
import type {
	ChatMessage, MessageType, TodoItem,
	QueuedMessage, ExecutionMode
} from './types';

// ─── Module-Level Exports ────────────────────────────────────────────────────

/** Cross-device streaming info for sidebar indicators */
export interface StreamingSessionInfo {
	projectId: string | null;
	worktreeId: string | null;
}

/** Track which sessions are streaming across all user devices */
export const streamingSessions = writable<Map<string, StreamingSessionInfo>>(new Map());

/** Track pending permissions for non-active sessions ("needs attention" indicator) */
export const backgroundPermissions = writable<Map<string, number>>(new Map());

// ─── Interfaces ──────────────────────────────────────────────────────────────

/** Full reactive state for the single active session. */
export interface ActiveSessionState {
	sessionId: string | null;
	title: string;
	messages: ChatMessage[];
	isStreaming: boolean;
	wsConnected: boolean;
	error: string | null;
	profile: string;
	sessionProfileId: string | null;
	currentQueryProfileId: string | null;
	project: string;
	totalTokensIn: number;
	totalTokensOut: number;
	totalCacheCreationTokens: number;
	totalCacheReadTokens: number;
	contextUsed: number | null;
	contextMax: number;
	modelOverride: string | null;
	permissionModeOverride: string | null;
	thinkingModeOverride: string | null;
	pendingPermissions: PermissionRequestType[];
	pendingQuestions: UserQuestionRequest[];
	todos: TodoItem[];
	queuedMessages: QueuedMessage[];
	draft: string;
	scrollTop: number;
	streamStartTime: number | null;
	executionMode: ExecutionMode;
	worktreeBranch: string;
	worktreeBaseBranch: string;
	worktreeCreateNewBranch: boolean;
	worktreeId: string | null;
	worktreeMode: 'new' | 'existing';
	selectedWorktreeBranch: string | null;
	isCreatingWorktree: boolean;
	worktreeError: string | null;
	worktreeDeleted: boolean;
	sessionLocked: boolean;
	sessionLockedBy: string | undefined;
}

/** Minimal shape persisted to the backend. */
export interface PersistedSession {
	sessionId: string | null;
	title: string;
	profile: string;
	project: string;
	modelOverride: string | null;
	permissionModeOverride: string | null;
	thinkingModeOverride: string | null;
	executionMode?: ExecutionMode;
	worktreeId?: string | null;
	worktreeBranch?: string;
	selectedWorktreeBranch?: string | null;
	worktreeMode?: 'new' | 'existing';
	worktreeDeleted?: boolean;
	queuedMessages?: QueuedMessage[];
}

// ─── Module-Level Helpers ────────────────────────────────────────────────────

function getOrCreateDeviceId(): string {
	const CANONICAL_KEY = 'ai-shuffle-device-id';
	const LEGACY_KEY = 'aihub_device_id';

	if (typeof window === 'undefined') {
		return `device-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
	}

	let deviceId = localStorage.getItem(CANONICAL_KEY);
	if (deviceId) return deviceId;

	const legacyId = localStorage.getItem(LEGACY_KEY);
	if (legacyId) {
		localStorage.setItem(CANONICAL_KEY, legacyId);
		localStorage.removeItem(LEGACY_KEY);
		return legacyId;
	}

	deviceId = `device-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
	localStorage.setItem(CANONICAL_KEY, deviceId);
	return deviceId;
}

function getAuthToken(): string | null {
	const cookies = document.cookie.split(';');
	for (const cookie of cookies) {
		const [name, value] = cookie.trim().split('=');
		if (name === 'session') return value;
	}
	return null;
}

function formatWorktreeError(rawError: string, branchName: string): string {
	const lower = rawError.toLowerCase();

	// Git-specific errors
	if (lower.includes('already checked out') || lower.includes('is already used by worktree')) {
		return `Branch '${branchName}' already has an active worktree. Select it from Active Worktrees instead.`;
	}
	if (lower.includes('not a valid branch') || lower.includes('branch not found') ||
		lower.includes('unknown revision') || (lower.includes('pathspec') && lower.includes('did not match'))) {
		return `Branch '${branchName}' doesn't exist. Fetch from remote first or create a new branch.`;
	}
	if (lower.includes('already exists') && (lower.includes('path') || lower.includes('folder') || lower.includes('directory'))) {
		return `A folder already exists at the worktree path for '${branchName}'. Try a different branch name.`;
	}
	if (lower.includes('.lock') || lower.includes('unable to lock') || lower.includes('index.lock')) {
		return 'A git operation is in progress. Wait a moment and try again.';
	}
	if (lower.includes('permission denied') || lower.includes('access denied')) {
		return 'Permission denied creating worktree. Check folder permissions on the server.';
	}
	if (lower.includes('not a git repository')) {
		return 'The project directory is not a git repository. Initialize git first.';
	}
	if (lower.includes('dirty') || lower.includes('uncommitted') || lower.includes('unstaged')) {
		return 'Uncommitted changes detected. Commit or stash your changes before creating a worktree.';
	}

	// Network / HTTP errors
	if (lower.includes('could not resolve') || lower.includes('connection refused') ||
		lower.includes('failed to fetch') || lower.includes('networkerror')) {
		return 'Network error — cannot reach the server. Check your connection.';
	}
	if (lower.includes('timeout') || lower.includes('aborted')) {
		return 'Request timed out. The server may be overloaded — try again.';
	}

	// HTTP status-based errors from the API client
	if (lower.includes('bad request')) {
		return `Invalid worktree request for '${branchName}'. Check the branch name and try again.`;
	}
	if (lower.includes('authentication required') || lower.includes('permission denied')) {
		return 'Authentication error. Try refreshing the page and logging in again.';
	}
	if (lower.includes('not found') && lower.includes('resource')) {
		return 'Project not found. It may have been deleted or moved.';
	}
	if (lower.includes('internal server error') || lower.includes('server error')) {
		return `Server error creating worktree for '${branchName}'. Check the backend logs.`;
	}

	// Fallback with the raw error for debugging
	return `Failed to create worktree for '${branchName}': ${rawError}`;
}

/** Build a partial store update from SessionUsage, skipping if no context data. */
function usageStoreUpdate(usage: SessionUsage): Partial<ActiveSessionState> {
	if (usage.contextUsed <= 0) return {};
	return {
		totalTokensIn: usage.inputTokens,
		totalTokensOut: usage.outputTokens,
		totalCacheCreationTokens: usage.cacheCreationTokens,
		totalCacheReadTokens: usage.cacheReadTokens,
		contextUsed: usage.contextUsed,
	};
}

// ─── Default state ───────────────────────────────────────────────────────────

function defaultState(): ActiveSessionState {
	return {
		sessionId: null,
		title: 'New Chat',
		messages: [],
		isStreaming: false,
		wsConnected: false,
		error: null,
		profile: getPersistedProfile(),
		sessionProfileId: null,
		currentQueryProfileId: null,
		project: getPersistedProject(),
		totalTokensIn: 0,
		totalTokensOut: 0,
		totalCacheCreationTokens: 0,
		totalCacheReadTokens: 0,
		contextUsed: null,
		contextMax: 200_000,
		modelOverride: null,
		permissionModeOverride: null,
		thinkingModeOverride: null,
		pendingPermissions: [],
		pendingQuestions: [],
		todos: [],
		queuedMessages: [],
		draft: '',
		scrollTop: 0,
		streamStartTime: null,
		executionMode: 'local',
		worktreeBranch: '',
		worktreeBaseBranch: 'main',
		worktreeCreateNewBranch: false,
		worktreeId: null,
		worktreeMode: 'new',
		selectedWorktreeBranch: null,
		isCreatingWorktree: false,
		worktreeError: null,
		worktreeDeleted: false,
		sessionLocked: false,
		sessionLockedBy: undefined,
	};
}

// ─── Store Factory ───────────────────────────────────────────────────────────

function createActiveSessionStore() {
	const store = writable<ActiveSessionState>(defaultState());
	const { subscribe, update, set } = store;

	// ── Module-level scalars (replace per-tab Maps) ────────────────────────

	let stopTimeout: ReturnType<typeof setTimeout> | null = null;
	let persistTimer: ReturnType<typeof setTimeout> | null = null;

	// Tracked removeSession timers so they can be cancelled when a new
	// streaming session starts for the same sessionId. Without this,
	// onStreamEnd's removeSession fires 100ms later and deletes the
	// new session's data from sessionState.
	const removeSessionTimers: Map<string, ReturnType<typeof setTimeout>> = new Map();

	// Shared mutable state — passed by reference to ws-message-handlers and session-switcher
	const internal: InternalMutableState = {
		waitingForHistory: null,
		expectedSessionId: null,
		pendingTools: new Set(),
		streamEndCleanedUp: false,
		streamingOnDisconnect: null,
		interruptPending: false,
	};

	// ── WebSocket Connection (extracted module) ──────────────────────────

	const wsConn: WsConnection = createWsConnection({
		getAuthToken,
		getDeviceId: getOrCreateDeviceId,
		onMessage: (data) => {
			handleMessage(data);
		},
		onConnected: () => {
			update(s => ({ ...s, wsConnected: true, error: null }));

			const state = get(store);
			if (state.sessionId) {
				internal.expectedSessionId = state.sessionId;
				internal.waitingForHistory = [];
				wsConn.send({ type: 'load_session', session_id: state.sessionId });
			}
		},
		onDisconnected: (code, _reason, wasClean) => {
			const state = get(store);
			const wasStreaming = state.isStreaming;
			const sessionId = state.sessionId;

			update(s => ({ ...s, wsConnected: false }));

			if (wasStreaming && sessionId) {
				sessionPoller.startStreaming(sessionId);
				internal.streamingOnDisconnect = sessionId;
			}

			if (stopTimeout) { clearTimeout(stopTimeout); stopTimeout = null; }

			if (code === 4001) {
				import('$app/navigation').then(({ goto }) => goto('/login'));
			} else if (wasClean) {
				// User-initiated disconnect (newChat, destroy, session switch, etc.)
				// No action needed — intentionalDisconnect already reset by ws-connection
			} else {
				// Unexpected close — ws-connection handles reconnect scheduling.
				// We only need to surface an error if max attempts were reached.
				// (ws-connection logs the error; we check isConnected later)
			}
		},
		isStreaming: () => {
			const state = get(store);
			// Also check if poller is active — WS is secondary during streaming
			return state.isStreaming && (!state.sessionId || !sessionPoller.isPolling(state.sessionId));
		},
		hasPendingTools: () => internal.pendingTools.size > 0,
		onMaxReconnectAttempts: () => {
			update(s => ({ ...s, error: 'Connection lost. Please refresh the page.' }));
		},
		onVisibilityHidden: () => {
			// Pause session polling — no one is looking at the UI
			const state = get(store);
			if (state.sessionId && sessionPoller.isPolling(state.sessionId)) {
				sessionPoller.stop(state.sessionId);
			}
		},
		onVisibilityVisible: () => {
			// Resume streaming poll if session was streaming
			const state = get(store);
			if (state.isStreaming && state.sessionId) {
				sessionPoller.startStreaming(state.sessionId);
			}
		},
	});

	// ── Session Switcher (extracted module) ─────────────────────────────

	const switcher = createSessionSwitcher({
		update,
		getState: () => get(store),
		wsSend: (data) => wsConn.send(data),
		wsIsConnected: () => wsConn.isConnected(),
		internal,
		sessionEphemeralCache,
		messageCache,
		browserState,
		onPersistTimerChanged: (timer) => { persistTimer = timer; },
	});

	// Alias for internal use (message handlers, queued messages, etc.)
	const persistSession = switcher.persistSession;

	// ── Internal helpers ───────────────────────────────────────────────────

	function startPolling(sessionId: string) {
		// Cancel any pending removeSession timer from a previous onStreamEnd.
		// Without this, the 100ms removeSession timer fires and deletes the
		// session data that the new streaming session just initialized.
		const pendingRemove = removeSessionTimers.get(sessionId);
		if (pendingRemove) {
			clearTimeout(pendingRemove);
			removeSessionTimers.delete(sessionId);
		}
		sessionPoller.startStreaming(sessionId);
	}

	function stopPolling() {
		const state = get(store);
		if (state.sessionId) {
			sessionPoller.stop(state.sessionId);
		}
	}

	function processNextQueuedMessage() {
		const state = get(store);
		if (state.queuedMessages.length === 0) return;
		if (!wsConn.isConnected()) return;
		if (!state.sessionId) return;

		const nextQueued = state.queuedMessages[0];

		update(s => ({
			...s,
			queuedMessages: s.queuedMessages.slice(1)
		}));

		const userMsgId = `user-${Date.now()}`;
		const queryProfileId = state.currentQueryProfileId || state.profile;

		if (state.isStreaming && state.sessionId) {
			// Currently streaming — use interrupt_and_query for clean transition.
			// This mirrors the interrupt path in sendMessage().
			sessionPoller.stop(state.sessionId);
			sessionState.removeSession(state.sessionId);
			internal.interruptPending = true;

			update(s => {
				const updatedMessages = s.messages.map(m => {
					if (m.type === 'tool_use' && m.toolStatus === 'running') {
						return { ...m, streaming: false, toolStatus: 'error' as const };
					}
					return m.streaming ? { ...m, streaming: false } : m;
				});
				return {
					...s,
					messages: [...updatedMessages, {
						id: userMsgId,
						role: 'user' as const,
						content: nextQueued.content,
						profileId: queryProfileId,
					}],
					isStreaming: true,
					error: null,
					streamStartTime: Date.now(),
				};
			});

			internal.pendingTools.clear();
			wsConn.resetLastMessageTime();
			wsConn.send({
				type: 'interrupt_and_query',
				prompt: nextQueued.content,
				session_id: state.sessionId,
				profile: state.profile,
				project: state.project || undefined,
				worktree_id: state.worktreeId || undefined,
			});
		} else {
			// Not streaming — normal query (safety net from done/stopped handler)
			update(s => ({
				...s,
				messages: [...s.messages, {
					id: userMsgId,
					role: 'user' as const,
					content: nextQueued.content,
					profileId: queryProfileId
				}],
				isStreaming: true,
				error: null,
				streamStartTime: Date.now()
			}));

			const overrides: Record<string, string> = {};
			const current = get(store);
			if (current.modelOverride) overrides.model = current.modelOverride;
			if (current.permissionModeOverride) overrides.permission_mode = current.permissionModeOverride;
			if (current.thinkingModeOverride) overrides.thinking_mode = current.thinkingModeOverride;

			wsConn.resetLastMessageTime();
			wsConn.send({
				type: 'query',
				prompt: nextQueued.content,
				session_id: state.sessionId,
				profile: state.profile,
				project: state.project || undefined,
				worktree_id: state.worktreeId || undefined,
				...(Object.keys(overrides).length > 0 ? { overrides } : {})
			});
		}

		persistSession();
	}

	function processWaitingEvents() {
		if (internal.waitingForHistory && internal.waitingForHistory.length > 0) {
			const queued = internal.waitingForHistory;
			internal.waitingForHistory = null;
			for (const queuedData of queued) {
				// Re-route through notifier for poller triggers
				const state = get(store);
				if (state.sessionId) {
					sessionNotifier.handleMessage(state.sessionId, queuedData);
				}
			}
		} else {
			internal.waitingForHistory = null;
		}
	}

	// ── Streaming bridge ──────────────────────────────────────────────────
	// Subscribes to sessionState so that polling updates flow to
	// activeSession.messages in real-time during streaming — not just at
	// stream end.

	function setupStreamingBridge() {
		sessionState.subscribe(($sessionState) => {
			const currentState = get(store);
			if (!currentState.sessionId) return;

			const sessionData = $sessionState.sessions.get(currentState.sessionId);
			if (!sessionData || sessionData.messages.length === 0) return;

			// Forward if the store says we're streaming OR the poller still has
			// data (sessionState tracks server-side streaming independently).
			// This closes the gap where `done` sets store.isStreaming=false but
			// the poller's final poll hasn't been applied yet.
			if (!currentState.isStreaming && !sessionData.isStreaming && !sessionPoller.isPolling(currentState.sessionId)) return;

			// Skip if messages reference hasn't changed. During compaction's full
			// refresh, preserveMessageIds may return the same array when content
			// matches. Skipping here avoids a redundant reactive update that the
			// deferred onStreamEnd would also trigger, reducing DOM churn.
			if (currentState.messages === sessionData.messages) return;

			update(s => {
				if (s.sessionId !== currentState.sessionId) return s;
				return {
					...s,
					messages: sessionData.messages,
					todos: sessionData.todos.length > 0 ? sessionData.todos : s.todos,
					...usageStoreUpdate(sessionData.usage),
				};
			});
		});
	}

	// ── onStreamEnd callback ───────────────────────────────────────────────
	// Wired up after factory creation in the returned init().
	// Flushes final messages from sessionState to the store before cleanup.

	function setupOnStreamEnd() {
		sessionPoller.onStreamEnd((sessionId, usage) => {
			// If in interrupt_and_query transition, skip cleanup
			if (internal.interruptPending) return;

			// If the store says isStreaming=true AND the poller is already
			// running for this session, a new query was started while this
			// onStreamEnd was deferred. This callback is stale — skip it
			// to avoid tearing down the new query's state.
			const currentState = get(store);
			if (currentState.isStreaming && currentState.sessionId === sessionId && sessionPoller.isPolling(sessionId)) {
				console.debug(`[ActiveSession] Skipping stale onStreamEnd for ${sessionId} — new streaming in progress`);
				return;
			}

			// Refresh sessions list
			appData.loadSessions();

			// Flush final messages AND usage from sessionState to the store.
			// NOTE: This callback is deferred via queueMicrotask() from the poller
			// to avoid a double-update with the streaming bridge in the same
			// synchronous call stack (which can corrupt Svelte's effect list
			// during compaction full refresh + stream end).
			const finalSession = sessionState.getSession(sessionId);
			if (finalSession && finalSession.messages.length > 0) {
				update(s => {
					if (s.sessionId !== sessionId) return s;
					// Defense-in-depth: if the store is currently streaming
					// (a new query was started since onStreamEnd was deferred),
					// do NOT overwrite isStreaming or messages.
					if (s.isStreaming) {
						console.debug(`[ActiveSession] onStreamEnd: store is streaming, preserving state`);
						return s;
					}
					// If the bridge already flushed these exact messages, only
					// update isStreaming and usage — skip the messages reference
					// change to minimize reactive DOM churn.
					const messagesChanged = s.messages !== finalSession.messages;
					return {
						...s,
						messages: messagesChanged ? finalSession.messages : s.messages,
						isStreaming: false,
						todos: finalSession.todos.length > 0 ? finalSession.todos : s.todos,
						...usageStoreUpdate(finalSession.usage),
					};
				});
			}

			// Update cache with final post-streaming messages
			if (finalSession && finalSession.messages.length > 0) {
				messageCache.set(sessionId, finalSession.messages);
			}

			// Clean up sessionState after reactive cycle completes.
			// Use the tracked timer map so startStreaming can cancel it.
			removeSessionTimers.set(sessionId, setTimeout(() => {
				removeSessionTimers.delete(sessionId);
				sessionState.removeSession(sessionId);
			}, 100));

			// Mark as handled so the safety timeout doesn't send redundant load_session
			internal.streamEndCleanedUp = true;
		});
	}

	// ── WebSocket Message Handler (extracted to session/ws-message-handlers.ts) ──

	const { handleMessage } = createMessageHandlers({
		update,
		getState: () => get(store),
		wsSend: (data) => wsConn.send(data),
		sessionPoller,
		sessionNotifier,
		sessionState: {
			getSession: (id) => sessionState.getSession(id),
			removeSession: (id) => sessionState.removeSession(id),
		},
		streamingSessions,
		backgroundPermissions,
		gitRefreshStatus: () => git.refreshStatus(),
		gitRefreshDiffStats: () => git.refreshDiffStats(),
		gitGetProjectId: () => get(git).projectId,
		gitGetLoading: () => get(git).loading,
		gitApplyServerUpdate: (projectId, worktreeId, changes) => git.applyServerUpdate(projectId, worktreeId, changes),
		appLoadSessions: () => appData.loadSessions(),
		appLoadApiUserSessions: () => appData.loadApiUserSessions(),
		internal,
		onNewChat: () => storeApi.newChat(),
		onPersistSession: () => persistSession(),
		onStartPolling: (sessionId) => startPolling(sessionId),
		onStopPolling: () => stopPolling(),
		onProcessNextQueuedMessage: () => processNextQueuedMessage(),
		onProcessWaitingEvents: () => processWaitingEvents(),
		onNotifySessionComplete: (info) => notifySessionComplete(info),
		onNotifyWaitingForResponse: (type, details) => notifyWaitingForResponse(type, details),
		wsIsConnected: () => wsConn.isConnected(),
		clearStopTimeout: () => { if (stopTimeout) { clearTimeout(stopTimeout); stopTimeout = null; } },
	});

	// ── WebSocket helpers (delegated to wsConn) ──────────────────────────

	function disconnect() {
		const state = get(store);
		if (state.sessionId) sessionPoller.stop(state.sessionId);

		internal.waitingForHistory = null;
		internal.expectedSessionId = null;
		internal.pendingTools = new Set();
		internal.streamingOnDisconnect = null;
		internal.interruptPending = false;

		if (stopTimeout) { clearTimeout(stopTimeout); stopTimeout = null; }

		wsConn.disconnect();
		update(s => ({ ...s, wsConnected: false }));
	}

	// ── Init / Destroy ─────────────────────────────────────────────────────

	async function init() {
		setupOnStreamEnd();
		setupStreamingBridge();

		// Visibility change handling is managed by wsConn (ping/pong, reconnect).
		// Polling pause/resume is handled via onVisibilityHidden/onVisibilityVisible callbacks.

		try {
			const backendSelection = await switcher.loadSelectionFromBackend();
			const defaultProfile = backendSelection?.selectedProfile || switcher.getPersistedProfile();
			const defaultProject = backendSelection?.selectedProject || switcher.getPersistedProject();

			const persisted = await switcher.loadPersistedSession();

			if (persisted) {
				update(s => ({
					...s,
					sessionId: persisted.sessionId,
					title: persisted.title || 'New Chat',
					profile: persisted.profile || defaultProfile,
					project: persisted.project || defaultProject,
					modelOverride: persisted.modelOverride ?? null,
					permissionModeOverride: persisted.permissionModeOverride ?? null,
					thinkingModeOverride: persisted.thinkingModeOverride ?? null,
					executionMode: persisted.executionMode || 'local',
					worktreeBranch: persisted.worktreeBranch || '',
					worktreeId: persisted.worktreeId ?? null,
					worktreeMode: persisted.worktreeMode || 'new',
					selectedWorktreeBranch: persisted.selectedWorktreeBranch ?? null,
					worktreeDeleted: persisted.worktreeDeleted ?? false,
					queuedMessages: persisted.queuedMessages || [],
				}));

				if (persisted.sessionId) {
					const cached = await messageCache.get(persisted.sessionId);
					if (cached && cached.length > 0) {
						update(s => ({ ...s, messages: cached }));
					}
				}
			} else {
				update(s => ({ ...s, profile: defaultProfile, project: defaultProject }));
			}

			wsConn.connect();
		} catch (error) {
			console.error('[ActiveSession] Failed to initialize:', error);
			wsConn.connect();
		}
	}

	function destroy() {
		disconnect();
		wsConn.destroy();
		if (persistTimer) { clearTimeout(persistTimer); persistTimer = null; }
		// Cancel all pending removeSession timers
		for (const timer of removeSessionTimers.values()) clearTimeout(timer);
		removeSessionTimers.clear();
		set(defaultState());
	}

	// ── Public API ─────────────────────────────────────────────────────────

	return {
		subscribe,

		init,
		destroy,

		// ── Session Navigation (delegated to session-switcher) ─────────

		switchSession: (sessionId: string) => switcher.switchSession(sessionId),
		newChat: () => switcher.newChat(),

		// ── Messaging ───────────────────────────────────────────────────

		async sendMessage(prompt: string, options?: { skipWorktreeSetup?: boolean }) {
			const state = get(store);

			// Worktree setup on first message
			if (!options?.skipWorktreeSetup && state.executionMode === 'worktree' && !state.sessionId && state.messages.length === 0) {
				if (state.worktreeMode === 'new') {
					await storeApi.createWorktree(prompt);
					return;
				} else if (state.worktreeMode === 'existing' && state.worktreeId) {
					await startSessionOnExistingWorktree(prompt);
					return;
				} else {
					update(s => ({ ...s, error: 'Please select an existing worktree or create a new one' }));
					return;
				}
			}

			if (!wsConn.isConnected()) {
				// Attempt reconnection instead of just showing an error
				wsConn.resetReconnectAttempts();
				wsConn.connect();
				const connected = await new Promise<boolean>((resolve) => {
					if (wsConn.isConnected()) { resolve(true); return; }
					const timeout = setTimeout(() => { clearInterval(check); resolve(false); }, 3000);
					const check = setInterval(() => {
						if (wsConn.isConnected()) { clearTimeout(timeout); clearInterval(check); resolve(true); }
					}, 100);
				});
				if (!connected) {
					update(s => ({ ...s, error: 'Unable to connect. Please check your connection and try again.' }));
					return;
				}
			}

			const userMsgId = `user-${Date.now()}`;
			const isFirstMessage = state.title === 'New Chat';
			const newTitle = isFirstMessage
				? prompt.substring(0, 100) + (prompt.length > 100 ? '...' : '')
				: state.title;
			const queryProfileId = state.profile;

			// Interrupt and re-query if currently streaming
			if (state.isStreaming && state.sessionId) {
				// Cleanup first: stop poller and remove session to prevent
				// the sessionState subscriber from overwriting our update
				sessionPoller.stop(state.sessionId);
				sessionState.removeSession(state.sessionId);
				internal.interruptPending = true;

				// Single atomic update: mark running tools as errored + append user message.
				// Uses s.messages (current store state) instead of stale sessionState data.
				update(s => {
					const updatedMessages = s.messages.map(m => {
						if (m.type === 'tool_use' && m.toolStatus === 'running') {
							return { ...m, streaming: false, toolStatus: 'error' as const };
						}
						return m.streaming ? { ...m, streaming: false } : m;
					});
					return {
						...s,
						messages: [...updatedMessages, {
							id: userMsgId,
							role: 'user' as const,
							content: prompt,
							profileId: queryProfileId,
						}],
						isStreaming: true,
						error: null,
						title: newTitle,
						currentQueryProfileId: queryProfileId,
					};
				});

				internal.pendingTools.clear();
				wsConn.resetLastMessageTime();
				wsConn.send({
					type: 'interrupt_and_query',
					prompt,
					session_id: state.sessionId,
					profile: state.profile,
					project: state.project || undefined,
					worktree_id: state.worktreeId || undefined,
				});
				return;
			}

			// Normal path (not currently streaming)
			update(s => ({
				...s,
				messages: [...s.messages, {
					id: userMsgId,
					role: 'user' as const,
					content: prompt,
					profileId: queryProfileId,
				}],
				isStreaming: true,
				error: null,
				title: newTitle,
				currentQueryProfileId: queryProfileId,
			}));

			const overrides: Record<string, string> = {};
			if (state.modelOverride) overrides.model = state.modelOverride;
			if (state.permissionModeOverride) overrides.permission_mode = state.permissionModeOverride;
			if (state.thinkingModeOverride) overrides.thinking_mode = state.thinkingModeOverride;

			wsConn.resetLastMessageTime();
			wsConn.send({
				type: 'query',
				prompt,
				session_id: state.sessionId,
				profile: state.profile,
				project: state.project || undefined,
				worktree_id: state.worktreeId || undefined,
				overrides: Object.keys(overrides).length > 0 ? overrides : undefined,
			});
		},

		stopGeneration() {
			const state = get(store);
			if (state.sessionId) {
				wsConn.send({ type: 'stop', session_id: state.sessionId });
			}

			if (stopTimeout) clearTimeout(stopTimeout);
			stopTimeout = setTimeout(() => {
				const current = get(store);
				if (current.isStreaming) {
					console.warn('[ActiveSession] Stop confirmation timeout - forcing UI recovery');
					update(s => {
						let messages = s.messages.map(m => {
							if (m.type === 'tool_use' && m.toolStatus === 'running') return { ...m, streaming: false, toolStatus: 'error' as const };
							return m.streaming ? { ...m, streaming: false } : m;
						});
						messages = messages.filter(m => !(m.type === 'text' && m.role === 'assistant' && !m.content));
						return { ...s, messages, isStreaming: false };
					});
				}
				stopTimeout = null;
			}, 10000);
		},

		// ── Permission & Question Responses ─────────────────────────────

		sendPermissionResponse(requestId: string, decision: 'allow' | 'deny', remember?: 'none' | 'session' | 'profile', pattern?: string) {
			wsConn.send({ type: 'permission_response', request_id: requestId, decision, remember: remember || 'none', pattern });
		},

		sendUserQuestionResponse(requestId: string, toolUseId: string, answers: Record<string, string | string[]>) {
			wsConn.send({ type: 'user_question_response', request_id: requestId, tool_use_id: toolUseId, answers });
		},

		syncPendingPermissions() {
			wsConn.send({ type: 'get_pending_permissions' });
		},

		syncPendingQuestions() {
			wsConn.send({ type: 'get_pending_questions' });
		},

		// ── Profile & Project ───────────────────────────────────────────

		setProfile(profileId: string) {
			update(s => ({ ...s, profile: profileId }));
			const state = get(store);
			switcher.saveSelectionToBackend(profileId, state.project);
			persistSession();
		},

		setProject(projectId: string) {
			const state = get(store);
			if (state.executionMode === 'local' && state.messages.length > 0) {
				console.warn('[ActiveSession] Cannot change project after session has started');
				return;
			}
			update(s => ({ ...s, project: projectId }));
			switcher.saveSelectionToBackend(state.profile, projectId);
			persistSession();
		},

		// ── Session Overrides ───────────────────────────────────────────

		setModelOverride(model: string | null) { update(s => ({ ...s, modelOverride: model })); persistSession(); },
		setPermissionModeOverride(mode: string | null) { update(s => ({ ...s, permissionModeOverride: mode })); persistSession(); },
		setThinkingModeOverride(mode: string | null) { update(s => ({ ...s, thinkingModeOverride: mode })); persistSession(); },

		// ── Ephemeral UI State ──────────────────────────────────────────

		setDraft(text: string) { update(s => ({ ...s, draft: text })); },
		setScrollTop(pos: number) { update(s => ({ ...s, scrollTop: pos })); },
		clearError() { update(s => ({ ...s, error: null })); },
		clearWorktreeError() { update(s => ({ ...s, worktreeError: null })); },
		/** Clear all messages (e.g. before reloading after rewind). */
		clearMessages() { update(s => ({ ...s, messages: [], todos: [] })); },

		/** Reload the current session from backend (e.g. after rewind).
		 *  @param forceJsonl - Skip live_session_store and read directly from JSONL.
		 *                      Use after rewind to avoid stale in-memory data.
		 */
		reloadSession(forceJsonl = false) {
			const state = get(store);
			if (state.sessionId) {
				wsConn.send({
					type: 'load_session',
					session_id: state.sessionId,
					...(forceJsonl && { force_jsonl: true }),
				});
			}
		},

		// ── Execution Mode & Worktree ───────────────────────────────────

		setExecutionMode(mode: ExecutionMode) { update(s => ({ ...s, executionMode: mode })); },
		setWorktreeBranch(branch: string) { update(s => ({ ...s, worktreeBranch: branch })); },
		setWorktreeBaseBranch(branch: string) { update(s => ({ ...s, worktreeBaseBranch: branch })); },
		setWorktreeMode(mode: 'new' | 'existing') { update(s => ({ ...s, worktreeMode: mode })); },
		setWorktreeCreateNewBranch(val: boolean) { update(s => ({ ...s, worktreeCreateNewBranch: val })); },

		selectWorktreeBranch(branch: string, worktreeId: string) {
			update(s => ({ ...s, worktreeId, selectedWorktreeBranch: branch, worktreeMode: 'existing' as const }));
		},

		markWorktreeDeleted() {
			update(s => ({ ...s, worktreeDeleted: true, worktreeId: null }));
		},

		async createWorktree(prompt?: string) {
			const state = get(store);
			if (!state.profile) { update(s => ({ ...s, worktreeError: 'Please select a profile' })); return; }
			if (!state.project) { update(s => ({ ...s, worktreeError: 'Please select a project' })); return; }
			if (!state.worktreeBranch) { update(s => ({ ...s, worktreeError: 'Please select a branch' })); return; }

			update(s => ({ ...s, isCreatingWorktree: true, worktreeError: null }));

			try {
				const response = await api.post<{
					session_id: string; worktree_id: string; branch_name: string;
					base_branch: string; worktree_path: string; status: string;
				}>('/sessions/with-worktree', {
					project_id: state.project,
					branch_name: state.worktreeBranch,
					base_branch: state.worktreeBaseBranch || 'main',
					profile_id: state.profile,
					create_new_branch: state.worktreeCreateNewBranch,
				});

				update(s => ({
					...s,
					sessionId: response.session_id,
					worktreeId: response.worktree_id,
					isCreatingWorktree: false,
					messages: [...s.messages, {
						id: `system-${Date.now()}`,
						role: 'system' as const,
						content: `Branch: \`${response.branch_name}\` (based on \`${response.base_branch}\`)\n\nPath: \`${response.worktree_path}\``,
						type: 'system' as MessageType,
						systemSubtype: 'worktree_created',
						systemData: { branch_name: response.branch_name, base_branch: response.base_branch, worktree_path: response.worktree_path, worktree_id: response.worktree_id },
					}],
				}));

				// Reconnect WS to register the new session
				disconnect();
				wsConn.connect();

				if (prompt) {
					setTimeout(() => storeApi.sendMessage(prompt, { skipWorktreeSetup: true }), 500);
				}
			} catch (error) {
				const err = error as { detail?: string; message?: string; status?: number };
				const rawError = err.detail || err.message || '';
				const formatted = rawError
					? formatWorktreeError(rawError, state.worktreeBranch)
					: `Failed to create worktree session for '${state.worktreeBranch}'. Check the backend logs for details.`;
				update(s => ({
					...s,
					isCreatingWorktree: false,
					worktreeError: formatted,
				}));
			}
		},

		// ── Queued Messages ─────────────────────────────────────────────

		queueMessage(content: string) {
			const id = `queued-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
			update(s => ({ ...s, queuedMessages: [...s.queuedMessages, { id, content, queuedAt: Date.now() }] }));
			persistSession();
		},

		removeQueuedMessage(id: string) {
			update(s => ({ ...s, queuedMessages: s.queuedMessages.filter(m => m.id !== id) }));
			persistSession();
		},

		clearQueuedMessages() {
			update(s => ({ ...s, queuedMessages: [] }));
			persistSession();
		},

		// ── System Messages & Error Handling ────────────────────────────

		addSystemMessage(content: string, subtype?: string) {
			update(s => ({
				...s,
				messages: [...s.messages, {
					id: `system-${Date.now()}`,
					role: 'system' as const,
					content,
					type: 'system' as MessageType,
					systemSubtype: subtype,
				}]
			}));
		},

		acknowledgeError() { update(s => ({ ...s, error: null })); },

		// ── Rewind ──────────────────────────────────────────────────────

		async rewindToMessage(_messageId: string) {
			const state = get(store);
			if (!state.sessionId) return;
			// After rewind completes externally (via MessageActions component),
			// reload the session to pick up the truncated message list
			if (wsConn.isConnected()) {
				internal.expectedSessionId = state.sessionId;
				internal.waitingForHistory = [];
				wsConn.send({ type: 'load_session', session_id: state.sessionId });
			}
		},

		// ── Session Takeover ────────────────────────────────────────────

		takeOverSession() {
			const state = get(store);
			if (!state.sessionId) return;
			if (wsConn.isConnected()) {
				update(s => ({ ...s, error: null, sessionLocked: false, sessionLockedBy: undefined }));
				internal.expectedSessionId = state.sessionId;
				internal.waitingForHistory = [];
				wsConn.send({ type: 'load_session', session_id: state.sessionId, force: true });
			}
		},

		// ── Getters for derived stores ──────────────────────────────────

		getDeviceId: () => typeof window !== 'undefined' ? getOrCreateDeviceId() : '',
	};

	// Internal helper for worktree existing branch sessions
	async function startSessionOnExistingWorktree(prompt: string) {
		const state = get(store);
		if (!state.profile) { update(s => ({ ...s, error: 'Please select a profile' })); return; }
		if (!state.project) { update(s => ({ ...s, error: 'Please select a project' })); return; }
		if (!state.worktreeId) { update(s => ({ ...s, error: 'Please select an existing worktree' })); return; }
		if (!wsConn.isConnected()) {
			wsConn.resetReconnectAttempts();
			wsConn.connect();
			const connected = await new Promise<boolean>((resolve) => {
				if (wsConn.isConnected()) { resolve(true); return; }
				const timeout = setTimeout(() => { clearInterval(check); resolve(false); }, 3000);
				const check = setInterval(() => {
					if (wsConn.isConnected()) { clearTimeout(timeout); clearInterval(check); resolve(true); }
				}, 100);
			});
			if (!connected) { update(s => ({ ...s, error: 'Unable to connect. Please check your connection and try again.' })); return; }
		}

		const branchDisplay = state.selectedWorktreeBranch || 'existing branch';
		const userMsgId = `user-${Date.now()}`;
		const newTitle = prompt.substring(0, 100) + (prompt.length > 100 ? '...' : '');

		update(s => ({
			...s,
			messages: [
				...s.messages,
				{
					id: `system-${Date.now()}`,
					role: 'system' as const,
					content: `Using existing worktree on branch \`${branchDisplay}\``,
					type: 'system' as MessageType,
					systemSubtype: 'worktree_selected',
					systemData: { branch_name: branchDisplay, worktree_id: state.worktreeId },
				},
				{
					id: userMsgId,
					role: 'user' as const,
					content: prompt,
				}
			],
			isStreaming: true,
			error: null,
			title: newTitle,
		}));

		const overrides: Record<string, string> = {};
		if (state.modelOverride) overrides.model = state.modelOverride;
		if (state.permissionModeOverride) overrides.permission_mode = state.permissionModeOverride;
		if (state.thinkingModeOverride) overrides.thinking_mode = state.thinkingModeOverride;

		wsConn.resetLastMessageTime();
		wsConn.send({
			type: 'query',
			prompt,
			profile: state.profile,
			project: state.project,
			worktree_id: state.worktreeId,
			overrides: Object.keys(overrides).length > 0 ? overrides : undefined,
		});
	}
}

// The store reference is needed before the factory returns.
// We hoist the variable so the factory's methods can reference it via the returned object.
const storeApi = createActiveSessionStore();
export const activeSession = storeApi;

// ─── Derived Convenience Stores ──────────────────────────────────────────────

export const activeSessionId = derived(activeSession, $s => $s.sessionId);
export const activeMessages = derived(activeSession, $s => $s.messages);
export const activeIsStreaming = derived(activeSession, $s => $s.isStreaming);
export const activeWsConnected = derived(activeSession, $s => $s.wsConnected);
export const activePendingPermissions = derived(activeSession, $s => $s.pendingPermissions);
export const activePendingQuestions = derived(activeSession, $s => $s.pendingQuestions);
export const activeTodos = derived(activeSession, $s => $s.todos);
export const activeError = derived(activeSession, $s => $s.error);
