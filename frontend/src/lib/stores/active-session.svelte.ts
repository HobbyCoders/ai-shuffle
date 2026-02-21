/**
 * Active Session Store — Single-session model replacing the multi-tab architecture.
 *
 * One WebSocket connection, one active session at a time.
 * Session switching saves ephemeral state to sessionEphemeralCache and restores it.
 * Polling is delegated to sessionPoller; WebSocket is notification-only for streaming.
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

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
import { git } from './git.svelte';
import { browserState } from './browser';
import { untrack } from 'svelte';

// Shared types
import type {
	ChatMessage, MessageType, TodoItem,
	QueuedMessage, ExecutionMode
} from './types';

// ─── Subscriber Type ─────────────────────────────────────────────────────────

type Subscriber<T> = (value: T) => void;

// ─── Module-Level Exports ────────────────────────────────────────────────────

/** Cross-device streaming info for sidebar indicators */
export interface StreamingSessionInfo {
	projectId: string | null;
	worktreeId: string | null;
}

// ─── streamingSessions (runes store) ─────────────────────────────────────────

let _streamingSessions = $state<Map<string, StreamingSessionInfo>>(new Map());
const _ssSubs = new Set<Subscriber<Map<string, StreamingSessionInfo>>>();

function _notifySsSubs() {
	untrack(() => { for (const fn of _ssSubs) fn(_streamingSessions); });
}

/** Track which sessions are streaming across all user devices */
export const streamingSessions = {
	get current() { return _streamingSessions; },
	subscribe(fn: Subscriber<Map<string, StreamingSessionInfo>>) {
		_ssSubs.add(fn);
		untrack(() => fn(_streamingSessions));
		return () => { _ssSubs.delete(fn); };
	},
	set(value: Map<string, StreamingSessionInfo>) {
		_streamingSessions = value;
		_notifySsSubs();
	},
	update(fn: (v: Map<string, StreamingSessionInfo>) => Map<string, StreamingSessionInfo>) {
		_streamingSessions = fn(_streamingSessions);
		_notifySsSubs();
	}
};

// ─── backgroundPermissions (runes store) ─────────────────────────────────────

let _backgroundPermissions = $state<Map<string, number>>(new Map());
const _bpSubs = new Set<Subscriber<Map<string, number>>>();

function _notifyBpSubs() {
	untrack(() => { for (const fn of _bpSubs) fn(_backgroundPermissions); });
}

/** Track pending permissions for non-active sessions ("needs attention" indicator) */
export const backgroundPermissions = {
	get current() { return _backgroundPermissions; },
	subscribe(fn: Subscriber<Map<string, number>>) {
		_bpSubs.add(fn);
		untrack(() => fn(_backgroundPermissions));
		return () => { _bpSubs.delete(fn); };
	},
	set(value: Map<string, number>) {
		_backgroundPermissions = value;
		_notifyBpSubs();
	},
	update(fn: (v: Map<string, number>) => Map<string, number>) {
		_backgroundPermissions = fn(_backgroundPermissions);
		_notifyBpSubs();
	}
};

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
	executionMode?: ExecutionMode;
	worktreeId?: string | null;
	worktreeBranch?: string;
	worktreeBaseBranch?: string;
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

// ─── Reactive State ──────────────────────────────────────────────────────────

let _state = $state<ActiveSessionState>(defaultState());

// ─── Subscribe shim ──────────────────────────────────────────────────────────

const _subscribers = new Set<Subscriber<ActiveSessionState>>();

function _notifySubscribers() {
	untrack(() => { for (const fn of _subscribers) fn(_state); });
}

function _update(fn: (s: ActiveSessionState) => ActiveSessionState) {
	_state = fn(untrack(() => _state));
	_notifySubscribers();
}

// ─── Module-level scalars (replace per-tab Maps) ─────────────────────────────

let stopTimeout: ReturnType<typeof setTimeout> | null = null;
let persistTimer: ReturnType<typeof setTimeout> | null = null;

// Shared mutable state — passed by reference to ws-message-handlers and session-switcher
const internal: InternalMutableState = {
	waitingForHistory: null,
	expectedSessionId: null,
	pendingTools: new Set(),
	streamEndCleanedUp: null,
	streamingOnDisconnect: null,
	interruptPending: false,
};

// ─── WebSocket Connection (extracted module) ─────────────────────────────────

const wsConn: WsConnection = createWsConnection({
	getAuthToken,
	getDeviceId: getOrCreateDeviceId,
	onMessage: (data) => {
		handleMessage(data);
	},
	onConnected: () => {
		_update(s => ({ ...s, wsConnected: true, error: null }));

		if (_state.sessionId) {
			internal.expectedSessionId = _state.sessionId;
			internal.waitingForHistory = [];
			wsConn.send({ type: 'load_session', session_id: _state.sessionId });
		}
	},
	onDisconnected: (code, _reason, wasClean) => {
		const wasStreaming = _state.isStreaming;
		const sessionId = _state.sessionId;

		_update(s => ({ ...s, wsConnected: false }));

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
		// Also check if poller is active — WS is secondary during streaming
		return _state.isStreaming && (!_state.sessionId || !sessionPoller.isPolling(_state.sessionId));
	},
	hasPendingTools: () => internal.pendingTools.size > 0,
	onMaxReconnectAttempts: () => {
		_update(s => ({ ...s, error: 'Connection lost. Please refresh the page.' }));
	},
	onVisibilityHidden: () => {
		// Pause session polling — no one is looking at the UI
		if (_state.sessionId && sessionPoller.isPolling(_state.sessionId)) {
			sessionPoller.stop(_state.sessionId);
		}
	},
	onVisibilityVisible: () => {
		// Resume streaming poll if session was streaming
		if (_state.isStreaming && _state.sessionId) {
			sessionPoller.startStreaming(_state.sessionId);
		}
	},
});

// ─── Session Switcher (extracted module) ─────────────────────────────────────

const switcher = createSessionSwitcher({
	update: _update,
	getState: () => _state,
	wsSend: (data) => wsConn.send(data),
	wsIsConnected: () => wsConn.isConnected(),
	internal,
	sessionEphemeralCache,
	messageCache,
	browserState,
	sessionPoller,
	onPersistTimerChanged: (timer) => { persistTimer = timer; },
});

// Alias for internal use (message handlers, queued messages, etc.)
const persistSession = switcher.persistSession;

// ─── Internal helpers ────────────────────────────────────────────────────────

function startPolling(sessionId: string) {
	sessionPoller.startStreaming(sessionId);
}

function stopPolling() {
	if (_state.sessionId) {
		sessionPoller.stop(_state.sessionId);
	}
}

function processNextQueuedMessage() {
	if (_state.queuedMessages.length === 0) return;
	if (!wsConn.isConnected()) return;
	if (!_state.sessionId) return;

	const nextQueued = _state.queuedMessages[0];

	_update(s => ({
		...s,
		queuedMessages: s.queuedMessages.slice(1)
	}));

	const userMsgId = `user-${Date.now()}`;
	const queryProfileId = _state.currentQueryProfileId || _state.profile;

	if (_state.isStreaming && _state.sessionId) {
		// Currently streaming — use interrupt_and_query for clean transition.
		// This mirrors the interrupt path in sendMessage().
		sessionPoller.stop(_state.sessionId);
		sessionState.removeSession(_state.sessionId);
		internal.interruptPending = true;

		_update(s => {
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
			session_id: _state.sessionId,
			profile: _state.profile,
			project: _state.project || undefined,
			worktree_id: _state.worktreeId || undefined,
		});
	} else {
		// Not streaming — normal query (safety net from done/stopped handler)
		_update(s => ({
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
		if (_state.modelOverride) overrides.model = _state.modelOverride;
		if (_state.permissionModeOverride) overrides.permission_mode = _state.permissionModeOverride;

		wsConn.resetLastMessageTime();
		wsConn.send({
			type: 'query',
			prompt: nextQueued.content,
			session_id: _state.sessionId,
			profile: _state.profile,
			project: _state.project || undefined,
			worktree_id: _state.worktreeId || undefined,
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
			if (_state.sessionId) {
				sessionNotifier.handleMessage(_state.sessionId, queuedData);
			}
		}
	} else {
		internal.waitingForHistory = null;
	}
}

// ─── WebSocket helpers (delegated to wsConn) ─────────────────────────────────

function disconnect() {
	if (_state.sessionId) sessionPoller.stop(_state.sessionId);

	internal.waitingForHistory = null;
	internal.expectedSessionId = null;
	internal.pendingTools = new Set();
	internal.streamingOnDisconnect = null;
	internal.interruptPending = false;

	if (stopTimeout) { clearTimeout(stopTimeout); stopTimeout = null; }

	wsConn.disconnect();
	_update(s => ({ ...s, wsConnected: false }));
}

// ─── Poll callback ───────────────────────────────────────────────────────────
// Direct callback from the poller → activeSession, replacing the old bridge
// subscription pattern. One _update() call per poll cycle — no double-update
// risk that corrupted Svelte 5's effect linked-list.

function setupPollCallback() {
	sessionPoller.onPollApplied((sessionId, data) => {
		if (sessionId !== _state.sessionId) return;

		// Forward if streaming OR poller still has data
		if (!_state.isStreaming && !data.isStreaming && !sessionPoller.isPolling(sessionId)) return;

		// Skip if nothing actually changed — messages, todos, AND usage all identical.
		// Only checking messages reference isn't enough because preserveMessageIds
		// can return the same array when content matches, which would silently
		// drop todos and usage updates on the final poll.
		const usageDelta = usageStoreUpdate(data.usage);
		const usageHasUpdate = 'totalTokensIn' in usageDelta;
		const todosHaveUpdate = data.todos.length > 0;
		if (_state.messages === data.messages && !todosHaveUpdate && !usageHasUpdate) return;

		_update(s => ({
			...s,
			messages: data.messages,
			todos: todosHaveUpdate ? data.todos : s.todos,
			...usageDelta,
		}));
	});
}

// ─── onStreamEnd callback ────────────────────────────────────────────────────
// Cleanup-only: caches messages, removes session from sessionState, refreshes
// sidebar. Does NOT call _update() — the poll callback already wrote final
// messages, and handleDone already set isStreaming: false.

function setupOnStreamEnd() {
	sessionPoller.onStreamEnd((sessionId, _usage) => {
		// If in interrupt_and_query transition, skip cleanup
		if (internal.interruptPending) return;

		// If a new query started, this callback is stale — skip it
		if (_state.isStreaming && _state.sessionId === sessionId && sessionPoller.isPolling(sessionId)) {
			console.debug(`[ActiveSession] Skipping stale onStreamEnd for ${sessionId} — new streaming in progress`);
			return;
		}

		// Refresh sessions list
		appData.loadSessions();

		// Cache final messages for instant rendering on next load
		const finalSession = sessionState.getSession(sessionId);
		if (finalSession && finalSession.messages.length > 0) {
			messageCache.set(sessionId, finalSession.messages);
		}

		// Clean up sessionState immediately — no timer needed since
		// we don't call _update(), so no Svelte reconciliation race
		sessionState.removeSession(sessionId);

		// Mark as handled so the safety timeout doesn't send redundant load_session
		internal.streamEndCleanedUp = sessionId;
	});
}

// ─── WebSocket Message Handler (extracted to session/ws-message-handlers.ts) ─

const { handleMessage } = createMessageHandlers({
	update: _update,
	getState: () => _state,
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
	gitGetProjectId: () => git.current.projectId,
	gitGetLoading: () => git.current.loading,
	gitApplyServerUpdate: (projectId, worktreeId, changes) => git.applyServerUpdate(projectId, worktreeId, changes),
	appLoadSessions: () => appData.loadSessions(),
	appLoadApiUserSessions: () => appData.loadApiUserSessions(),
	internal,
	onNewChat: () => activeSession.newChat(),
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

// ─── Internal helper for worktree existing branch sessions ───────────────────

async function startSessionOnExistingWorktree(prompt: string) {
	if (!_state.profile) { _update(s => ({ ...s, error: 'Please select a profile' })); return; }
	if (!_state.project) { _update(s => ({ ...s, error: 'Please select a project' })); return; }
	if (!_state.worktreeId) { _update(s => ({ ...s, error: 'Please select an existing worktree' })); return; }
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
		if (!connected) { _update(s => ({ ...s, error: 'Unable to connect. Please check your connection and try again.' })); return; }
	}

	const branchDisplay = _state.selectedWorktreeBranch || 'existing branch';
	const userMsgId = `user-${Date.now()}`;
	const newTitle = prompt.substring(0, 100) + (prompt.length > 100 ? '...' : '');

	_update(s => ({
		...s,
		messages: [
			...s.messages,
			{
				id: `system-${Date.now()}`,
				role: 'system' as const,
				content: `Using existing worktree on branch \`${branchDisplay}\``,
				type: 'system' as MessageType,
				systemSubtype: 'worktree_selected',
				systemData: { branch_name: branchDisplay, worktree_id: _state.worktreeId },
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
	if (_state.modelOverride) overrides.model = _state.modelOverride;
	if (_state.permissionModeOverride) overrides.permission_mode = _state.permissionModeOverride;

	wsConn.resetLastMessageTime();
	wsConn.send({
		type: 'query',
		prompt,
		profile: _state.profile,
		project: _state.project,
		worktree_id: _state.worktreeId,
		overrides: Object.keys(overrides).length > 0 ? overrides : undefined,
	});
}

// ─── Init / Destroy ──────────────────────────────────────────────────────────

// Module-level flag: when newChat() is called, it sets this so that if the
// component remounts (triggering destroy → init), init() skips restoring the
// old session from the backend.  This survives the destroy/init cycle because
// destroy() does NOT reset it — only init() consumes it.
let _skipNextSessionRestore = false;

async function init() {
	setupOnStreamEnd();
	setupPollCallback();

	// Visibility change handling is managed by wsConn (ping/pong, reconnect).
	// Polling pause/resume is handled via onVisibilityHidden/onVisibilityVisible callbacks.

	const skipRestore = _skipNextSessionRestore;
	_skipNextSessionRestore = false;

	try {
		const backendSelection = await switcher.loadSelectionFromBackend();
		const defaultProfile = backendSelection?.selectedProfile || switcher.getPersistedProfile();
		const defaultProject = backendSelection?.selectedProject || switcher.getPersistedProject();

		if (skipRestore) {
			// newChat() was called before this init — keep the cleared state,
			// just apply default profile/project if not already set.
			_update(s => ({
				...s,
				profile: s.profile || defaultProfile,
				project: s.project || defaultProject,
			}));
		} else {
			const persisted = await switcher.loadPersistedSession();

			if (persisted) {
				_update(s => ({
					...s,
					sessionId: persisted.sessionId,
					title: persisted.title || 'New Chat',
					profile: persisted.profile || defaultProfile,
					project: persisted.project || defaultProject,
					modelOverride: persisted.modelOverride ?? null,
					permissionModeOverride: persisted.permissionModeOverride ?? null,
					executionMode: persisted.executionMode || 'local',
					worktreeBranch: persisted.worktreeBranch || '',
					worktreeBaseBranch: persisted.worktreeBaseBranch || 'main',
					worktreeId: persisted.worktreeId ?? null,
					worktreeMode: persisted.worktreeMode || 'new',
					selectedWorktreeBranch: persisted.selectedWorktreeBranch ?? null,
					worktreeDeleted: persisted.worktreeDeleted ?? false,
					queuedMessages: persisted.queuedMessages || [],
				}));

				if (persisted.sessionId) {
					const cached = await messageCache.get(persisted.sessionId);
					if (cached && cached.length > 0) {
						_update(s => ({ ...s, messages: cached }));
					}
				}
			} else {
				_update(s => ({ ...s, profile: defaultProfile, project: defaultProject }));
			}
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
	_state = defaultState();
	_notifySubscribers();
}

// ─── Exported Store ──────────────────────────────────────────────────────────

export const activeSession = {
	get current() { return _state; },

	subscribe(fn: Subscriber<ActiveSessionState>) {
		_subscribers.add(fn);
		untrack(() => fn(_state));
		return () => { _subscribers.delete(fn); };
	},

	init,
	destroy,

	// ── Session Navigation (delegated to session-switcher) ─────────

	switchSession: (sessionId: string, projectId?: string | null) => switcher.switchSession(sessionId, projectId),
	newChat: () => {
		// Tell init() to skip restoring the old session if the component remounts.
		// This must be set BEFORE switcher.newChat() because destroy() runs
		// synchronously during remount and must not clear this flag.
		_skipNextSessionRestore = true;
		switcher.newChat();
	},

	// ── Messaging ───────────────────────────────────────────────────

	async sendMessage(prompt: string, options?: { skipWorktreeSetup?: boolean }) {
		// Worktree setup on first message
		if (!options?.skipWorktreeSetup && _state.executionMode === 'worktree' && !_state.sessionId && _state.messages.length === 0) {
			if (_state.worktreeMode === 'new') {
				await activeSession.createWorktree(prompt);
				return;
			} else if (_state.worktreeMode === 'existing' && _state.worktreeId) {
				await startSessionOnExistingWorktree(prompt);
				return;
			} else {
				_update(s => ({ ...s, error: 'Please select an existing worktree or create a new one' }));
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
				_update(s => ({ ...s, error: 'Unable to connect. Please check your connection and try again.' }));
				return;
			}
		}

		const userMsgId = `user-${Date.now()}`;
		const isFirstMessage = _state.title === 'New Chat';
		const newTitle = isFirstMessage
			? prompt.substring(0, 100) + (prompt.length > 100 ? '...' : '')
			: _state.title;
		const queryProfileId = _state.profile;

		// Interrupt and re-query if currently streaming
		if (_state.isStreaming && _state.sessionId) {
			// Cleanup first: stop poller and remove session to prevent
			// the sessionState subscriber from overwriting our update
			sessionPoller.stop(_state.sessionId);
			sessionState.removeSession(_state.sessionId);
			internal.interruptPending = true;

			// Single atomic update: mark running tools as errored + append user message.
			// Uses s.messages (current store state) instead of stale sessionState data.
			_update(s => {
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
				session_id: _state.sessionId,
				profile: _state.profile,
				project: _state.project || undefined,
				worktree_id: _state.worktreeId || undefined,
			});
			return;
		}

		// Normal path (not currently streaming)
		_update(s => ({
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
		if (_state.modelOverride) overrides.model = _state.modelOverride;
		if (_state.permissionModeOverride) overrides.permission_mode = _state.permissionModeOverride;

		wsConn.resetLastMessageTime();
		wsConn.send({
			type: 'query',
			prompt,
			session_id: _state.sessionId,
			profile: _state.profile,
			project: _state.project || undefined,
			worktree_id: _state.worktreeId || undefined,
			overrides: Object.keys(overrides).length > 0 ? overrides : undefined,
		});
	},

	stopGeneration() {
		if (_state.sessionId) {
			wsConn.send({ type: 'stop', session_id: _state.sessionId });
		}

		if (stopTimeout) clearTimeout(stopTimeout);
		stopTimeout = setTimeout(() => {
			if (_state.isStreaming) {
				console.warn('[ActiveSession] Stop confirmation timeout - forcing UI recovery');
				_update(s => {
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
		_update(s => ({ ...s, profile: profileId }));
		switcher.saveSelectionToBackend(profileId, _state.project);
		persistSession();
	},

	setProject(projectId: string) {
		if (_state.executionMode === 'local' && _state.messages.length > 0) {
			console.warn('[ActiveSession] Cannot change project after session has started');
			return;
		}
		// Reset execution mode & worktree state when project changes —
		// the previous worktree context is no longer valid for the new project.
		_update(s => ({
			...s,
			project: projectId,
			executionMode: 'local' as const,
			worktreeBranch: '',
			worktreeBaseBranch: 'main',
			worktreeCreateNewBranch: false,
			worktreeId: null,
			worktreeMode: 'new' as const,
			selectedWorktreeBranch: null,
			isCreatingWorktree: false,
			worktreeError: null,
		}));
		switcher.saveSelectionToBackend(_state.profile, projectId);
		persistSession();
	},

	// ── Session Overrides ───────────────────────────────────────────

	setModelOverride(model: string | null) { _update(s => ({ ...s, modelOverride: model })); persistSession(); },
	setPermissionModeOverride(mode: string | null) { _update(s => ({ ...s, permissionModeOverride: mode })); persistSession(); },

	// ── Ephemeral UI State ──────────────────────────────────────────

	setDraft(text: string) { _update(s => ({ ...s, draft: text })); },
	setScrollTop(pos: number) { _update(s => ({ ...s, scrollTop: pos })); },
	clearError() { _update(s => ({ ...s, error: null })); },
	clearWorktreeError() { _update(s => ({ ...s, worktreeError: null })); },
	/** Clear all messages (e.g. before reloading after rewind). */
	clearMessages() { _update(s => ({ ...s, messages: [], todos: [] })); },

	/** Reload the current session from backend (e.g. after rewind).
	 *  @param forceJsonl - Skip live_session_store and read directly from JSONL.
	 *                      Use after rewind to avoid stale in-memory data.
	 */
	reloadSession(forceJsonl = false) {
		if (_state.sessionId) {
			wsConn.send({
				type: 'load_session',
				session_id: _state.sessionId,
				...(forceJsonl && { force_jsonl: true }),
			});
		}
	},

	// ── Execution Mode & Worktree ───────────────────────────────────

	setExecutionMode(mode: ExecutionMode) { _update(s => ({ ...s, executionMode: mode })); },
	setWorktreeBranch(branch: string) { _update(s => ({ ...s, worktreeBranch: branch })); },
	setWorktreeBaseBranch(branch: string) { _update(s => ({ ...s, worktreeBaseBranch: branch })); },
	setWorktreeMode(mode: 'new' | 'existing') { _update(s => ({ ...s, worktreeMode: mode })); },
	setWorktreeCreateNewBranch(val: boolean) { _update(s => ({ ...s, worktreeCreateNewBranch: val })); },

	selectWorktreeBranch(branch: string, worktreeId: string) {
		_update(s => ({ ...s, worktreeId, selectedWorktreeBranch: branch, worktreeMode: 'existing' as const }));
	},

	markWorktreeDeleted() {
		_update(s => ({ ...s, worktreeDeleted: true, worktreeId: null }));
	},

	async createWorktree(prompt?: string) {
		if (!_state.profile) { _update(s => ({ ...s, worktreeError: 'Please select a profile' })); return; }
		if (!_state.project) { _update(s => ({ ...s, worktreeError: 'Please select a project' })); return; }
		if (!_state.worktreeBranch) { _update(s => ({ ...s, worktreeError: 'Please select a branch' })); return; }

		_update(s => ({ ...s, isCreatingWorktree: true, worktreeError: null }));

		try {
			const response = await api.post<{
				session_id: string; worktree_id: string; branch_name: string;
				base_branch: string; worktree_path: string; status: string;
			}>('/sessions/with-worktree', {
				project_id: _state.project,
				branch_name: _state.worktreeBranch,
				base_branch: _state.worktreeBaseBranch || 'main',
				profile_id: _state.profile,
				create_new_branch: _state.worktreeCreateNewBranch,
			});

			_update(s => ({
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
				setTimeout(() => activeSession.sendMessage(prompt, { skipWorktreeSetup: true }), 500);
			}
		} catch (error) {
			const err = error as { detail?: string; message?: string; status?: number };
			const rawError = err.detail || err.message || '';
			const formatted = rawError
				? formatWorktreeError(rawError, _state.worktreeBranch)
				: `Failed to create worktree session for '${_state.worktreeBranch}'. Check the backend logs for details.`;
			_update(s => ({
				...s,
				isCreatingWorktree: false,
				worktreeError: formatted,
			}));
		}
	},

	// ── Queued Messages ─────────────────────────────────────────────

	queueMessage(content: string) {
		const id = `queued-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
		_update(s => ({ ...s, queuedMessages: [...s.queuedMessages, { id, content, queuedAt: Date.now() }] }));
		persistSession();
	},

	removeQueuedMessage(id: string) {
		_update(s => ({ ...s, queuedMessages: s.queuedMessages.filter(m => m.id !== id) }));
		persistSession();
	},

	clearQueuedMessages() {
		_update(s => ({ ...s, queuedMessages: [] }));
		persistSession();
	},

	// ── System Messages & Error Handling ────────────────────────────

	addSystemMessage(content: string, subtype?: string) {
		_update(s => ({
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

	acknowledgeError() { _update(s => ({ ...s, error: null })); },

	// ── Rewind ──────────────────────────────────────────────────────

	async rewindToMessage(_messageId: string) {
		if (!_state.sessionId) return;
		// After rewind completes externally (via MessageActions component),
		// reload the session to pick up the truncated message list
		if (wsConn.isConnected()) {
			internal.expectedSessionId = _state.sessionId;
			internal.waitingForHistory = [];
			wsConn.send({ type: 'load_session', session_id: _state.sessionId });
		}
	},

	// ── Session Takeover ────────────────────────────────────────────

	takeOverSession() {
		if (!_state.sessionId) return;
		if (wsConn.isConnected()) {
			_update(s => ({ ...s, error: null, sessionLocked: false, sessionLockedBy: undefined }));
			internal.expectedSessionId = _state.sessionId;
			internal.waitingForHistory = [];
			wsConn.send({ type: 'load_session', session_id: _state.sessionId, force: true });
		}
	},

	// ── Getters for derived stores ──────────────────────────────────

	getDeviceId: () => typeof window !== 'undefined' ? getOrCreateDeviceId() : '',
};

// ─── Derived Convenience Stores ──────────────────────────────────────────────

function createDerived<T>(getter: () => T) {
	return {
		get current() { return getter(); },
		subscribe(fn: Subscriber<T>) {
			const handler: Subscriber<ActiveSessionState> = () => fn(getter());
			_subscribers.add(handler);
			untrack(() => fn(getter()));
			return () => { _subscribers.delete(handler); };
		}
	};
}

export const activeSessionId = createDerived(() => _state.sessionId);
export const activeMessages = createDerived(() => _state.messages);
export const activeIsStreaming = createDerived(() => _state.isStreaming);
export const activeWsConnected = createDerived(() => _state.wsConnected);
export const activePendingPermissions = createDerived(() => _state.pendingPermissions);
export const activePendingQuestions = createDerived(() => _state.pendingQuestions);
export const activeTodos = createDerived(() => _state.todos);
export const activeError = createDerived(() => _state.error);
