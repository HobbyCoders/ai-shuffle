/**
 * Active Session Store — Single-session model replacing the multi-tab architecture.
 *
 * One WebSocket connection, one active session at a time.
 * Session switching saves ephemeral state to sessionEphemeralCache and restores it.
 * Polling is delegated to sessionPoller; WebSocket is notification-only for streaming.
 *
 * Uses manual subscribe() pattern for backward compatibility with
 * $storeName auto-subscription in .svelte files.
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

// ─── streamingSessions ───────────────────────────────────────────────────────

let _streamingSessions: Map<string, StreamingSessionInfo> = new Map();
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

// ─── backgroundPermissions ────────────────────────────────────────────────────

let _backgroundPermissions: Map<string, number> = new Map();
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
	fastMode: boolean;
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
}

// ─── Module-Level Helpers ────────────────────────────────────────────────────

function getOrCreateDeviceId(): string {
	const CANONICAL_KEY = 'ai-shuffle-device-id';
	const LEGACY_KEY = 'aihub_device_id';

	if (typeof window === 'undefined') {
		return `device-${Date.now()}-${Math.random().toString(36).substring(2, 11)}`;
	}

	let deviceId = localStorage.getItem(CANONICAL_KEY);
	if (deviceId) return deviceId;

	const legacyId = localStorage.getItem(LEGACY_KEY);
	if (legacyId) {
		localStorage.setItem(CANONICAL_KEY, legacyId);
		localStorage.removeItem(LEGACY_KEY);
		return legacyId;
	}

	deviceId = `device-${Date.now()}-${Math.random().toString(36).substring(2, 11)}`;
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

/** Build a partial store update from SessionUsage.
 *
 * Always returns token fields so that contextUsed is set even when the
 * server reports 0 (e.g. session with no JSONL data). Without this,
 * switchSession's contextUsed:null reset is never overwritten and the
 * component shows stale/wrong values on the next render.
 */
function usageStoreUpdate(usage: SessionUsage): Partial<ActiveSessionState> {
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
		fastMode: false,
		sessionLocked: false,
		sessionLockedBy: undefined,
	};
}

// ─── State ───────────────────────────────────────────────────────────────────

let _state: ActiveSessionState = defaultState();

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

// Fast mode is per-session — stored in _state.fastMode, not global preferences.

// Shared mutable state — passed by reference to ws-message-handlers and session-switcher.
// Simplified: removed waitingForHistory, expectedSessionId, streamEndCleanedUp,
// streamingOnDisconnect — these were only needed for the dual-authority WS+poller pattern.
const internal: InternalMutableState = {
	pendingTools: new Set(),
	interruptPending: false,
	awaitingNewSession: false,
};

// ─── WebSocket Connection (extracted module) ─────────────────────────────────

const wsConn: WsConnection = createWsConnection({
	getAuthToken,
	getDeviceId: getOrCreateDeviceId,
	onMessage: (data) => {
		handleMessage(data);
	},
	onConnected: () => {
		console.debug(`[ActiveSession] onConnected: sessionId=${_state.sessionId}, isStreaming=${_state.isStreaming}, hasMessages=${_state.messages.length > 0}`);
		_update(s => ({ ...s, wsConnected: true, error: null }));

		if (_state.sessionId) {
			const sessionId = _state.sessionId;
			// Register with SyncEngine to receive broadcast events from other devices
			wsConn.send({ type: 'watch_session', session_id: sessionId });

			// Only re-fetch from server if the session might have changed:
			// - Currently streaming (need live updates)
			// - No messages loaded yet (first connection or stale state)
			// For completed sessions with messages already loaded, skip the
			// re-fetch to avoid resetting contextUsed and causing phantom
			// usage jumps. The data is already correct in the frontend state.
			if (_state.isStreaming || _state.messages.length === 0) {
				sessionPoller.loadSession(sessionId).then(pollData => {
					console.debug(`[ActiveSession] onConnected loadSession result: streaming=${pollData?.isStreaming}, msgs=${pollData?.messages?.length ?? 0}`);
					if (pollData && pollData.isStreaming) {
						sessionPoller.startStreaming(sessionId);
					} else if (!pollData && _state.sessionId === sessionId && _state.messages.length === 0) {
						// loadSession failed and we have no cached messages —
						// show error instead of leaving the UI in a frozen limbo state.
						_update(s => ({
							...s,
							error: 'Failed to load session. Try starting a new chat.',
							contextUsed: 0,
						}));
					}
				});
			} else {
				console.debug(`[ActiveSession] onConnected: skipping re-fetch for completed session ${sessionId} (${_state.messages.length} msgs already loaded)`);
			}
		}
	},
	onDisconnected: (code, _reason, wasClean) => {
		const wasStreaming = _state.isStreaming;
		const sessionId = _state.sessionId;

		_update(s => ({ ...s, wsConnected: false }));

		// If streaming was in progress, ensure poller keeps running.
		// The poller works over HTTP — doesn't need WS — so it will
		// continue delivering updates through Docker/Cloudflare disruptions.
		if (wasStreaming && sessionId) {
			sessionPoller.startStreaming(sessionId);
		}

		if (stopTimeout) { clearTimeout(stopTimeout); stopTimeout = null; }

		if (code === 4001) {
			import('$app/navigation').then(({ goto }) => goto('/login'));
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
	internal,
	sessionEphemeralCache,
	messageCache,
	sessionPoller,
	sessionState,
	onPersistTimerChanged: (timer) => { persistTimer = timer; },
});

// Alias for internal use (message handlers, queued messages, etc.)
const persistSession = switcher.persistSession;

// ─── WebSocket helpers (delegated to wsConn) ─────────────────────────────────

function disconnect() {
	if (_state.sessionId) sessionPoller.stop(_state.sessionId);

	internal.pendingTools.clear();
	internal.interruptPending = false;
	internal.awaitingNewSession = false;

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
		if (sessionId !== _state.sessionId) {
			console.debug(`[PollCB] skipped: poll for ${sessionId} but active is ${_state.sessionId}`);
			return;
		}

		// Build usage delta — don't overwrite a valid contextUsed with 0.
		// During the brief window between sending a message and the first
		// stream_start event, the server may return 0 usage. Slash commands
		// may never trigger a stream_start at all. In both cases, preserve
		// the last known context usage rather than flashing to 0%.
		const usageDelta = usageStoreUpdate(data.usage);
		if (usageDelta.contextUsed === 0 && _state.contextUsed && _state.contextUsed > 0) {
			usageDelta.contextUsed = _state.contextUsed;
		}
		const usageHasUpdate = usageDelta.contextUsed !== _state.contextUsed;

		const todosHaveUpdate = data.todos.length > 0;
		const streamingChanged = _state.isStreaming !== data.isStreaming;

		// Build worktree metadata delta from server (corrects stale client state)
		const metaDelta: Partial<ActiveSessionState> = {};
		if (data.sessionMeta) {
			const meta = data.sessionMeta;
			// Sync title from server — corrects stale 'New Chat' or ephemeral cache values
			if (meta.title && meta.title !== _state.title) {
				metaDelta.title = meta.title;
			}
			// Only apply if the server has definitive worktree info
			if (meta.worktree_deleted !== undefined && meta.worktree_deleted !== _state.worktreeDeleted) {
				metaDelta.worktreeDeleted = meta.worktree_deleted;
			}
			if (meta.worktree_id !== undefined && meta.worktree_id !== _state.worktreeId) {
				metaDelta.worktreeId = meta.worktree_id;
			}
			if (meta.worktree_branch !== undefined && meta.worktree_branch !== null && meta.worktree_branch !== _state.worktreeBranch) {
				metaDelta.worktreeBranch = meta.worktree_branch;
			}
			if (meta.execution_mode !== undefined && meta.execution_mode !== _state.executionMode) {
				metaDelta.executionMode = meta.execution_mode as 'local' | 'worktree';
			}
		}
		const metaHasUpdate = Object.keys(metaDelta).length > 0;

		// Skip if nothing actually changed
		const msgsMatch = _state.messages === data.messages;
		if (msgsMatch && !todosHaveUpdate && !usageHasUpdate && !streamingChanged && !metaHasUpdate) {
			console.debug(`[PollCB] skipped: no changes for ${sessionId}`);
			return;
		}

		if (metaHasUpdate) {
			console.debug(`[PollCB] applying worktree correction:`, metaDelta);
		}
		console.debug(`[PollCB] applying: ${data.messages.length} msgs, streaming=${data.isStreaming}, msgsChanged=${!msgsMatch}, streamingChanged=${streamingChanged}`);

		// Single authoritative update — poller is the sole writer of session state.
		_update(s => ({
			...s,
			messages: data.messages,
			isStreaming: data.isStreaming,
			todos: todosHaveUpdate ? data.todos : s.todos,
			// Clear streamStartTime when streaming ends
			streamStartTime: data.isStreaming ? s.streamStartTime : null,
			...usageDelta,
			...metaDelta,
		}));

		// Persist corrected worktree state so the fix survives page refresh.
		// This is rare (only when server corrects stale client state) and prevents
		// the stale worktreeDeleted from being re-loaded on next page load.
		if (metaHasUpdate) {
			persistSession();
		}
	});
}

// ─── onStreamEnd callback ────────────────────────────────────────────────────
// Caches messages, removes session from sessionState, refreshes sidebar,
// and ensures isStreaming is cleared. The poll callback (onPollApplied) should
// have already synced isStreaming from the server, but this is a safety net
// for cases where the WS 'done' event was lost.

function setupOnStreamEnd() {
	sessionPoller.onStreamEnd((sessionId, _usage) => {
		// If in interrupt_and_query transition for THIS session, skip cleanup.
		// Guard against interruptPending applying to a different session's stream end.
		if (internal.interruptPending && _state.sessionId === sessionId) return;

		// If a new query started for THIS session, this callback is stale — skip it.
		// Only skip when the actively-polling session matches the one that ended,
		// otherwise we'd incorrectly skip cleanup for background sessions.
		if (_state.isStreaming && _state.sessionId === sessionId && sessionPoller.isPolling(sessionId)) {
			return;
		}

		// Refresh sessions list
		appData.loadSessions();

		// Cache final messages for instant rendering on next load
		const finalSession = sessionState.getSession(sessionId);
		if (finalSession && finalSession.messages.length > 0) {
			messageCache.set(sessionId, finalSession.messages);
		}

		// Clean up sessionState
		sessionState.removeSession(sessionId);
	});
}

// ─── WebSocket Message Handler (extracted to session/ws-message-handlers.ts) ─

const { handleMessage } = createMessageHandlers({
	update: _update,
	getState: () => _state,
	wsSend: (data) => wsConn.send(data),
	sessionPoller,
	sessionNotifier,
	streamingSessions,
	backgroundPermissions,
	gitRefreshStatus: () => git.refreshStatus(),
	gitRefreshDiffStats: () => git.refreshDiffStats(),
	gitGetProjectId: () => git.current.projectId,
	gitGetLoading: () => git.current.loading,
	appLoadSessions: () => appData.loadSessions(),
	appLoadApiUserSessions: () => appData.loadApiUserSessions(),
	internal,
	onNewChat: () => activeSession.newChat(),
	onPersistSession: () => persistSession(),
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

	// Add the worktree system message, then delegate to sendMessage() for
	// the actual query. This ensures session lifecycle flags (awaitingNewSession)
	// are managed by a single code path — the same one used by local sessions.
	const branchDisplay = _state.selectedWorktreeBranch || 'existing branch';
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
		],
	}));

	await activeSession.sendMessage(prompt, { skipWorktreeSetup: true });
}

// ─── Init / Destroy ──────────────────────────────────────────────────────────

// ── Session-restore guard ────────────────────────────────────────────────────
// IMPORTANT: These use sessionStorage, NOT module-level variables.
// Module-level `let` variables in .svelte.ts files reset when the component
// remounts (Svelte re-evaluates the module), which defeated the previous
// design.  sessionStorage survives module re-evaluation and component
// destroy/init cycles within the same browser tab.

const SKIP_RESTORE_KEY = '__aihub_skipSessionRestore';
const INIT_GEN_KEY = '__aihub_initGeneration';

function getSkipRestore(): boolean {
	if (typeof window === 'undefined') return false;
	return sessionStorage.getItem(SKIP_RESTORE_KEY) === '1';
}
function setSkipRestore(value: boolean) {
	if (typeof window === 'undefined') return;
	if (value) {
		sessionStorage.setItem(SKIP_RESTORE_KEY, '1');
	} else {
		sessionStorage.removeItem(SKIP_RESTORE_KEY);
	}
}
function getInitGeneration(): number {
	if (typeof window === 'undefined') return 0;
	return parseInt(sessionStorage.getItem(INIT_GEN_KEY) || '0', 10);
}
function bumpInitGeneration(): number {
	const next = getInitGeneration() + 1;
	if (typeof window !== 'undefined') {
		sessionStorage.setItem(INIT_GEN_KEY, String(next));
	}
	return next;
}

async function init() {
	const initGen = bumpInitGeneration();
	setupOnStreamEnd();
	setupPollCallback();

	// Visibility change handling is managed by wsConn (ping/pong, reconnect).
	// Polling pause/resume is handled via onVisibilityHidden/onVisibilityVisible callbacks.

	const skipRestore = getSkipRestore();
	// NOTE: Do NOT clear skipRestore here — let it persist until
	// switchSession() explicitly clears it.  This ensures that if the component
	// remounts multiple times, every init() call respects the "new chat" intent.
	console.debug(`[ActiveSession] init(): skipRestore=${skipRestore}, initGen=${initGen}, currentSessionId=${_state.sessionId}, messages=${_state.messages.length}`);

	try {
		const backendSelection = await switcher.loadSelectionFromBackend();

		// Stale init — a newer init() or newChat() was called while we awaited.
		if (initGen !== getInitGeneration()) {
			console.debug(`[ActiveSession] init(): stale (gen ${initGen} < ${getInitGeneration()}), bailing`);
			return;
		}

		const defaultProfile = backendSelection?.selectedProfile || switcher.getPersistedProfile();
		const defaultProject = backendSelection?.selectedProject || switcher.getPersistedProject();

		if (skipRestore) {
			// newChat() was called before this init — keep the cleared state,
			// just apply default profile/project if not already set.
			console.debug('[ActiveSession] init(): skipRestore=true, keeping cleared state');
			_update(s => ({
				...s,
				profile: s.profile || defaultProfile,
				project: s.project || defaultProject,
			}));
		} else {
			const persisted = await switcher.loadPersistedSession();

			// Stale init check after second await
			if (initGen !== getInitGeneration()) {
				console.debug(`[ActiveSession] init(): stale after loadPersistedSession (gen ${initGen} < ${getInitGeneration()}), bailing`);
				return;
			}

			console.debug(`[ActiveSession] init(): persisted sessionId=${persisted?.sessionId ?? 'null'}`);

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
				}));

				if (persisted.sessionId) {
					const cached = await messageCache.get(persisted.sessionId);

					// Stale init check after third await
					if (initGen !== getInitGeneration()) {
						console.debug(`[ActiveSession] init(): stale after messageCache (gen ${initGen} < ${getInitGeneration()}), bailing`);
						return;
					}

					console.debug(`[ActiveSession] init(): cached messages for ${persisted.sessionId}: ${cached?.length ?? 0}`);
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
	sessionPoller.stopAll();
	wsConn.destroy();
	switcher.destroy();
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

	switchSession: (sessionId: string, projectId?: string | null, worktreeInfo?: import('./session/session-switcher').SessionWorktreeInfo | null, title?: string | null) => {
		// User explicitly navigated to a session — clear the "new chat" flag
		// so that future init() calls can restore from the backend normally.
		setSkipRestore(false);
		switcher.switchSession(sessionId, projectId, worktreeInfo, title);
	},
	newChat: () => {
		// Tell init() to skip restoring the old session if the component remounts.
		// This must be set BEFORE switcher.newChat() because destroy() runs
		// synchronously during remount and must not clear this flag.
		setSkipRestore(true);
		// Bump init generation to invalidate any in-flight init() calls that
		// might overwrite the cleared state with stale backend data.
		const gen = bumpInitGeneration();
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

		// Signal that we're awaiting a new session from the backend.
		// The session_created/start WS handlers only accept events when this
		// flag is true (or when the event matches the current sessionId).
		// This prevents unsolicited streaming events from hijacking the chat.
		internal.awaitingNewSession = true;
		// If sending from new chat, clear the cached ephemeral so that the
		// next "new session" click doesn't resurrect the draft we just sent.
		if (!_state.sessionId) {
			sessionEphemeralCache.remove('__new_chat__');
		}
		console.debug(`[ActiveSession] sendMessage: sessionId=${_state.sessionId}, awaitingNewSession=true, wsConnected=${wsConn.isConnected()}`);

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
		if (_state.fastMode) overrides.fast_mode = 'true';

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
	setFastMode(enabled: boolean) { _update(s => ({ ...s, fastMode: enabled })); },

	// ── Ephemeral UI State ──────────────────────────────────────────

	setDraft(text: string) { _update(s => ({ ...s, draft: text })); },
	setScrollTop(pos: number) { _update(s => ({ ...s, scrollTop: pos })); },
	clearError() { _update(s => ({ ...s, error: null })); },
	clearWorktreeError() { _update(s => ({ ...s, worktreeError: null })); },
	/** Clear all messages (e.g. before reloading after rewind). */
	clearMessages() { _update(s => ({ ...s, messages: [], todos: [] })); },

	/** Reload the current session from backend (e.g. after rewind).
	 * Unlike WS-reconnect reloads, this is an explicit user action that
	 * should do a full reset to pick up truncated/changed data. */
	async reloadSession(_forceJsonl = false) {
		if (_state.sessionId) {
			// Force a fresh fetch by removing stale session state first
			sessionState.removeSession(_state.sessionId);
			await sessionPoller.loadSession(_state.sessionId);
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

	// ── Queued Messages (server-side queue) ─────────────────────────

	queueMessage(content: string) {
		if (!_state.sessionId) return;
		wsConn.send({
			type: 'queue_message',
			session_id: _state.sessionId,
			content,
			profile: _state.currentQueryProfileId || _state.profile,
			project: _state.project || undefined,
		});
	},

	removeQueuedMessage(id: string) {
		wsConn.send({
			type: 'remove_queued_message',
			message_id: id,
			session_id: _state.sessionId,
		});
	},

	clearQueuedMessages() {
		if (!_state.sessionId) return;
		wsConn.send({
			type: 'clear_queued_messages',
			session_id: _state.sessionId,
		});
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
		// reload the session to pick up the truncated message list.
		// Force reset since the JSONL was modified by the rewind.
		sessionState.removeSession(_state.sessionId);
		await sessionPoller.loadSession(_state.sessionId);
	},

	// ── Session Takeover ────────────────────────────────────────────

	async takeOverSession() {
		if (!_state.sessionId) return;
		_update(s => ({ ...s, error: null, sessionLocked: false, sessionLockedBy: undefined }));
		// Send takeover via WS (to acquire the lock), then load state via HTTP
		if (wsConn.isConnected()) {
			wsConn.send({ type: 'load_session', session_id: _state.sessionId, force: true });
		}
		// Force reset — takeover may load different state from another device
		sessionState.removeSession(_state.sessionId);
		await sessionPoller.loadSession(_state.sessionId);
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
