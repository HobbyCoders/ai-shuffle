/**
 * Active Session Store — Single-session model replacing the multi-tab architecture.
 *
 * One WebSocket connection, one active session at a time.
 * Session switching saves ephemeral state to sessionEphemeralCache and restores it.
 * Polling is delegated to sessionPoller; WebSocket is notification-only for streaming.
 *
 * Uses manual subscribe() pattern for backward compatibility with
 * $storeName auto-subscription in .svelte files.
 *
 * Sub-modules (in ./session/):
 * - session-init.ts      — init/destroy lifecycle, poll & stream-end callbacks
 * - message-sender.ts    — sendMessage, worktree creation, message queue
 * - permission-handler.ts — permission/question responses
 * - draft-manager.ts     — draft, scroll, error, system messages, rewind, takeover
 */

import type { PermissionRequest as PermissionRequestType, UserQuestionRequest } from '$lib/api/client';
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

// Extracted sub-modules
import { createSessionInit, setSkipRestore, bumpInitGeneration } from './session/session-init';
import { createMessageSender } from './session/message-sender';
import { createPermissionHandler } from './session/permission-handler';
import { createDraftManager } from './session/draft-manager';

// Caches and helpers
import { messageCache } from './message-cache';
import { sessionEphemeralCache } from './session-cache';
import { getOrCreateDeviceId } from './device-id';
import { appData } from './app-data';
import { git } from './git.svelte';
import { untrack } from 'svelte';
import { createStore, createSubscribers } from './store-utils';
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

// ─── streamingSessions ───────────────────────────────────────────────────────

const _ssStore = createStore<Map<string, StreamingSessionInfo>>(new Map());

/** Track which sessions are streaming across all user devices */
export const streamingSessions = {
	get current() { return _ssStore.current; },
	subscribe: _ssStore.subscribe,
	set: _ssStore.set,
	update: _ssStore.update,
};

// ─── backgroundPermissions ────────────────────────────────────────────────────

const _bpStore = createStore<Map<string, number>>(new Map());

/** Track pending permissions for non-active sessions ("needs attention" indicator) */
export const backgroundPermissions = {
	get current() { return _bpStore.current; },
	subscribe: _bpStore.subscribe,
	set: _bpStore.set,
	update: _bpStore.update,
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
	effortLevelOverride: string | null;
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
	isReopeningWorktree: boolean;
	fastMode: boolean;
	sessionLocked: boolean;
	sessionLockedBy: string | undefined;
	/** Accumulated text from WebSocket stream_delta events for instant rendering.
	 *  Cleared when the poller catches up or streaming ends. */
	wsStreamContent: string;
	/** Holds the user's message text while waiting for interrupt confirmation.
	 *  If the interrupt fails/times out, ChatInput restores this to the editor
	 *  so the user doesn't lose their message. Cleared on success or session switch. */
	pendingInterruptPrompt: string | null;
	/** Session type: 'chat' (default) or 'voice' for realtime voice sessions */
	sessionType: 'chat' | 'voice';
	/** URL of the latest TTS audio response from the voice pipeline */
	voiceResponseAudioUrl: string | null;
	/** Text of the latest voice response (for transcript display) */
	voiceResponseText: string | null;
}

/** Minimal shape persisted to the backend. */
export interface PersistedSession {
	sessionId: string | null;
	title: string;
	profile: string;
	project: string;
	modelOverride: string | null;
	permissionModeOverride: string | null;
	effortLevelOverride: string | null;
	executionMode?: ExecutionMode;
	worktreeId?: string | null;
	worktreeBranch?: string;
	worktreeBaseBranch?: string;
	selectedWorktreeBranch?: string | null;
	worktreeMode?: 'new' | 'existing';
	worktreeDeleted?: boolean;
	sessionType?: 'chat' | 'voice';
}

// ─── Module-Level Helpers ────────────────────────────────────────────────────

function getAuthToken(): string | null {
	const cookies = document.cookie.split(';');
	for (const cookie of cookies) {
		const [name, value] = cookie.trim().split('=');
		if (name === 'session') return value;
	}
	return null;
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
		effortLevelOverride: null,
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
		isReopeningWorktree: false,
		fastMode: false,
		sessionLocked: false,
		sessionLockedBy: undefined,
		wsStreamContent: '',
		pendingInterruptPrompt: null,
		sessionType: 'chat',
		voiceResponseAudioUrl: null,
		voiceResponseText: null,
	};
}

// ─── State ───────────────────────────────────────────────────────────────────

let _state: ActiveSessionState = defaultState();

// ─── Subscribe shim (centralized via store-utils) ───────────────────────────

const { subscribe: _subscribe, notifySubscribers: _notifySubscribers, createDerived } =
	createSubscribers<ActiveSessionState>(
		() => _state,
		(v) => { _state = v; }
	);

function _update(fn: (s: ActiveSessionState) => ActiveSessionState) {
	const prev = untrack(() => _state);
	const next = fn(prev);
	if (next === _state) {
		// Diagnostic: detect skipped streaming→ended transitions
		if (prev.isStreaming && !next.isStreaming) {
			console.error(`[_update] IDENTITY SKIP on streaming→false transition! This should not happen.`);
		}
		return; // Identity check — skip notification when nothing changed
	}
	// Diagnostic: log streaming transitions
	if (prev.isStreaming !== next.isStreaming) {
		console.warn(`[_update] isStreaming ${prev.isStreaming} → ${next.isStreaming}, msgs: ${prev.messages.length} → ${next.messages.length}, msgsChanged: ${prev.messages !== next.messages}`);
	}
	_state = next;
	_notifySubscribers();
}

// ─── Module-level scalars (replace per-tab Maps) ─────────────────────────────

let stopTimeout: ReturnType<typeof setTimeout> | null = null;
let persistTimer: ReturnType<typeof setTimeout> | null = null;

// Shared mutable state — passed by reference to ws-message-handlers and session-switcher.
const internal: InternalMutableState = {
	pendingTools: new Set(),
	interruptPending: false,
	awaitingNewSession: false,
	worktreeCreationGeneration: 0,
	interruptConfirmResolve: null,
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
			wsConn.send({ type: 'watch_session', session_id: sessionId });

			if (_state.isStreaming || _state.messages.length === 0) {
				sessionPoller.loadSession(sessionId).then(pollData => {
					console.debug(`[ActiveSession] onConnected loadSession result: streaming=${pollData?.isStreaming}, msgs=${pollData?.messages?.length ?? 0}`);
					if (pollData && pollData.isStreaming) {
						sessionPoller.startStreaming(sessionId);
					} else if (!pollData && _state.sessionId === sessionId && _state.messages.length === 0) {
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

		if (wasStreaming && sessionId) {
			sessionPoller.startStreaming(sessionId);
		}

		if (stopTimeout) { clearTimeout(stopTimeout); stopTimeout = null; }

		if (code === 4001) {
			import('$app/navigation').then(({ goto }) => goto('/login'));
		}
	},
	isStreaming: () => {
		return _state.isStreaming && (!_state.sessionId || !sessionPoller.isPolling(_state.sessionId));
	},
	hasPendingTools: () => internal.pendingTools.size > 0,
	onMaxReconnectAttempts: () => {
		_update(s => ({ ...s, error: 'Connection lost. Please refresh the page.' }));
	},
	onVisibilityHidden: () => {
		if (_state.sessionId && sessionPoller.isPolling(_state.sessionId)) {
			sessionPoller.stop(_state.sessionId);
		}
	},
	onVisibilityVisible: () => {
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

const persistSession = switcher.persistSession;

// ─── WebSocket helpers (delegated to wsConn) ─────────────────────────────────

function disconnect() {
	if (_state.sessionId) sessionPoller.stop(_state.sessionId);

	internal.pendingTools.clear();
	internal.interruptPending = false;
	internal.awaitingNewSession = false;
	if (internal.interruptConfirmResolve) {
		internal.interruptConfirmResolve(false);
		internal.interruptConfirmResolve = null;
	}

	if (stopTimeout) { clearTimeout(stopTimeout); stopTimeout = null; }

	wsConn.disconnect();
	_update(s => ({ ...s, wsConnected: false }));
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

// ─── Sub-module instances ────────────────────────────────────────────────────

const sessionInitModule = createSessionInit({
	update: _update,
	getState: () => _state,
	notifySubscribers: _notifySubscribers,
	defaultState,
	setState: (s) => { _state = s; },
	internal,
	sessionPoller,
	sessionState,
	wsConn,
	switcher,
	messageCache,
	appData,
	disconnect,
	persistSession,
	persistTimer: {
		get: () => persistTimer,
		clear: () => { if (persistTimer) { clearTimeout(persistTimer); persistTimer = null; } },
	},
});

const permissionHandler = createPermissionHandler({
	wsSend: (data) => wsConn.send(data),
});

const draftManager = createDraftManager({
	update: _update,
	getState: () => _state,
	wsSend: (data) => wsConn.send(data),
	wsIsConnected: () => wsConn.isConnected(),
	sessionStateRemoveSession: (id) => sessionState.removeSession(id),
	sessionPollerLoadSession: (id) => sessionPoller.loadSession(id),
});

// Message sender needs a reference to activeSession for recursive calls
// (sendMessage → createWorktree → sendMessage). We use a lazy getter.
const messageSender = createMessageSender({
	update: _update,
	getState: () => _state,
	internal,
	wsConn: {
		isConnected: () => wsConn.isConnected(),
		connect: () => wsConn.connect(),
		send: (data) => wsConn.send(data),
		resetLastMessageTime: () => wsConn.resetLastMessageTime(),
		resetReconnectAttempts: () => wsConn.resetReconnectAttempts(),
	},
	sessionPoller,
	sessionState,
	sessionEphemeralCache,
	appData,
	persistSession,
	stopTimeout: {
		get: () => stopTimeout,
		set: (t) => { stopTimeout = t; },
	},
	getActiveSession: () => activeSession,
});

// ─── Exported Store ──────────────────────────────────────────────────────────

export const activeSession = {
	get current() { return _state; },

	subscribe: _subscribe,

	init: sessionInitModule.init,
	destroy: sessionInitModule.destroy,

	// ── Session Navigation (delegated to session-switcher) ─────────

	switchSession: (sessionId: string, projectId?: string | null, worktreeInfo?: import('./session/session-switcher').SessionWorktreeInfo | null, title?: string | null) => {
		setSkipRestore(false);
		switcher.switchSession(sessionId, projectId, worktreeInfo, title);
	},

	newChat: () => {
		setSkipRestore(true);
		bumpInitGeneration();
		switcher.newChat();
	},

	newVoiceSession: () => {
		setSkipRestore(true);
		bumpInitGeneration();
		switcher.newVoiceSession();
	},

	// ── Messaging (delegated to message-sender) ─────────────────────

	sendMessage: (prompt: string, options?: { skipWorktreeSetup?: boolean; skipMessageAppend?: boolean; pendingUploadPaths?: string[] }) =>
		messageSender.sendMessage(prompt, options),

	stopGeneration: () => messageSender.stopGeneration(),

	/** Send voice audio to the backend for STT → Claude → TTS pipeline */
	sendVoiceAudio: async (audioBlob: Blob) => {
		// If no session exists yet, signal that we expect a session_created
		// response — mirrors what sendMessage() does in message-sender.ts.
		if (!_state.sessionId) {
			internal.awaitingNewSession = true;
		}

		const reader = new FileReader();
		const base64 = await new Promise<string>((resolve, reject) => {
			reader.onload = () => {
				const dataUrl = reader.result as string;
				// Strip data URI prefix to get raw base64
				const base64Data = dataUrl.split(',')[1] || '';
				resolve(base64Data);
			};
			reader.onerror = reject;
			reader.readAsDataURL(audioBlob);
		});

		wsConn.send({
			type: 'voice_audio',
			session_id: _state.sessionId,
			audio_data: base64,
			mime_type: audioBlob.type || 'audio/webm',
			session_type: 'voice',
			profile: _state.profile,
			project: _state.project,
		});

		// Clear previous voice response
		_update(s => ({ ...s, voiceResponseAudioUrl: null, voiceResponseText: null }));
	},

	// ── Permission & Question Responses (delegated to permission-handler) ──

	sendPermissionResponse: permissionHandler.sendPermissionResponse,
	sendUserQuestionResponse: permissionHandler.sendUserQuestionResponse,
	syncPendingPermissions: permissionHandler.syncPendingPermissions,
	syncPendingQuestions: permissionHandler.syncPendingQuestions,

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
		if (_state.project === projectId) {
			switcher.saveSelectionToBackend(_state.profile, projectId);
			return;
		}
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
	setEffortLevelOverride(level: string | null) { _update(s => ({ ...s, effortLevelOverride: level })); persistSession(); },
	setFastMode(enabled: boolean) { _update(s => ({ ...s, fastMode: enabled })); },

	// ── Ephemeral UI State (delegated to draft-manager) ─────────────

	setDraft: draftManager.setDraft,
	clearPendingInterruptPrompt: draftManager.clearPendingInterruptPrompt,
	setScrollTop: draftManager.setScrollTop,
	clearError: draftManager.clearError,
	clearWorktreeError: draftManager.clearWorktreeError,
	clearMessages: draftManager.clearMessages,
	reloadSession: draftManager.reloadSession,
	addSystemMessage: draftManager.addSystemMessage,
	acknowledgeError: draftManager.acknowledgeError,
	rewindToMessage: draftManager.rewindToMessage,
	takeOverSession: draftManager.takeOverSession,

	// ── Execution Mode & Worktree ───────────────────────────────────

	setExecutionMode(mode: ExecutionMode) { _update(s => ({ ...s, executionMode: mode })); },
	setWorktreeBranch(branch: string) { _update(s => ({ ...s, worktreeBranch: branch })); },
	setWorktreeBaseBranch(branch: string) { _update(s => ({ ...s, worktreeBaseBranch: branch })); },
	setWorktreeMode(mode: 'new' | 'existing') { _update(s => ({ ...s, worktreeMode: mode })); },
	setWorktreeCreateNewBranch(val: boolean) { _update(s => ({ ...s, worktreeCreateNewBranch: val })); },

	selectWorktreeBranch(branch: string, worktreeId: string) {
		_update(s => ({ ...s, worktreeId, selectedWorktreeBranch: branch, worktreeMode: 'existing' as const }));
	},

	clearWorktreeSelection() {
		_update(s => ({ ...s, worktreeId: null, selectedWorktreeBranch: null, worktreeBranch: '', worktreeMode: 'new' as const }));
	},

	markWorktreeDeleted() {
		_update(s => ({ ...s, worktreeDeleted: true }));
	},

	createWorktree: (prompt?: string, pendingUploadPaths?: string[]) =>
		messageSender.createWorktree(prompt, pendingUploadPaths),

	autoCreateWorktree: (prompt: string, pendingUploadPaths?: string[]) =>
		messageSender.autoCreateWorktree(prompt, pendingUploadPaths),

	// ── Queued Messages (delegated to message-sender) ──────────────

	queueMessage: (content: string) => messageSender.queueMessage(content),
	removeQueuedMessage: (id: string) => messageSender.removeQueuedMessage(id),
	clearQueuedMessages: () => messageSender.clearQueuedMessages(),

	// ── System Messages & Error Handling (delegated to draft-manager) ──

	// (already delegated above)

	// ── Rewind (delegated to draft-manager) ─────────────────────────

	// (already delegated above)

	// ── Session Takeover (delegated to draft-manager) ───────────────

	// (already delegated above)

	// ── Getters for derived stores ──────────────────────────────────

	getDeviceId: () => typeof window !== 'undefined' ? getOrCreateDeviceId() : '',
};

// ─── Derived Convenience Stores (via store-utils createDerived) ─────────────

export const activeSessionId = createDerived(() => _state.sessionId);
export const activeMessages = createDerived(() => _state.messages);
export const activeIsStreaming = createDerived(() => _state.isStreaming);
export const activeWsConnected = createDerived(() => _state.wsConnected);
export const activePendingPermissions = createDerived(() => _state.pendingPermissions);
export const activePendingQuestions = createDerived(() => _state.pendingQuestions);
export const activeTodos = createDerived(() => _state.todos);
export const activeError = createDerived(() => _state.error);
