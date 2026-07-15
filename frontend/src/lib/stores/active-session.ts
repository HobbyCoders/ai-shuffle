/**
 * Active Session Store — Single-session model replacing the multi-tab architecture.
 *
 * One WebSocket connection, one active session at a time.
 * Session switching saves ephemeral state to sessionEphemeralCache and restores it.
 * The SSE-fed reducer (feed-controller) is the single writer of the transcript +
 * streaming state; WebSocket carries lifecycle, permissions, and questions.
 *
 * Uses manual subscribe() pattern for backward compatibility with
 * $storeName auto-subscription in .svelte files.
 *
 * Sub-modules (in ./session/):
 * - session-init.ts      — init/destroy lifecycle
 * - message-sender.ts    — sendMessage, worktree creation, message queue
 * - permission-handler.ts — permission/question responses
 * - draft-manager.ts     — draft, scroll, error, system messages, rewind, takeover
 */

import type { PermissionRequest as PermissionRequestType, UserQuestionRequest, Profile } from '$lib/api/client';
import { notifySessionComplete, notifyWaitingForResponse } from '$lib/services/notifications';

// Session infrastructure
import { createWsConnection, type WsConnection } from './session/ws-connection';
import { createMessageHandlers, type InternalMutableState } from './session/ws-message-handlers';
import {
	createSessionSwitcher,
	getPersistedProfile, getPersistedProject,
	getPersistedExecutionMode, setPersistedExecutionMode,
} from './session/session-switcher';

// Extracted sub-modules
import { createSessionInit, setSkipRestore, bumpInitGeneration } from './session/session-init';
// V2 Phase 1: SSE-fed reducer store for v2_stream sessions (single writer).
import { feedController } from './session/v2/feed-controller';
import { createMessageSender } from './session/message-sender';
import { createPermissionHandler } from './session/permission-handler';
import { createDraftManager } from './session/draft-manager';

// Caches and helpers
import { sessionEphemeralCache } from './session-cache';
import { getOrCreateDeviceId } from './device-id';
import { appData } from './app-data.svelte';
import { git } from './git.svelte';
import { untrack } from 'svelte';
import { createStore, createSubscribers } from './store-utils';
// Shared types
import type {
	ChatMessage, MessageType,
	QueuedMessage, ExecutionMode, Task
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
	/** Slice B (SDK 0.1.81): permission requests the user deferred — visible as a
	 *  "N deferred" pill below the chat. Re-broadcast on `requeue_deferred_permissions`. */
	deferredPermissions: PermissionRequestType[];
	pendingQuestions: UserQuestionRequest[];
	queuedMessages: QueuedMessage[];
	/**
	 * Currently-running background tasks (sub-agents + shell subprocesses) for
	 * the active session. Default `[]`. Written by the poller's onPollApplied
	 * callback from each /live response. Non-empty exactly while the session has
	 * background work in flight — including while a turn is PARKED (isStreaming
	 * may be false then). Drives the ActiveTasks strip + the "waiting for N
	 * background task(s)" parked indicator.
	 */
	activeTasks: Task[];
	/**
	 * When true, a user "Stop" leaves background sub-agents running (Claude
	 * Code's Esc semantics) and stops only the foreground turn; the Stop confirm
	 * dialog then defaults to preserving agents with an opt-in to also stop them.
	 * Effective = backend flag on AND scoped-interrupt patch verified — the
	 * server collapses both into this one boolean on each /live response.
	 * Default false → Stop behaves exactly as before (full stop, agents down).
	 */
	preserveBackgroundOnInterrupt: boolean;
	draft: string;
	scrollTop: number;
	streamStartTime: number | null;
	executionMode: ExecutionMode;
	/** When executionMode === 'cloud': the registered install to offload onto.
	 *  Required for cloud sessions — the device picker selects it. */
	cloudTargetInstanceId: string | null;
	/** When executionMode === 'cloud': whether to spin up an isolated worktree on
	 *  the remote device (vs. running directly on the chosen branch). Orthogonal
	 *  modifier surfaced as the "Worktree" checkbox, mirroring local worktree mode
	 *  for cloud sessions. */
	cloudWorktree: boolean;
	/** When executionMode === 'cloud': the branch to run on the remote device.
	 *  In no-worktree cloud this is an existing branch on the *remote* (the worker
	 *  checks it out), or — with `cloudCreateNewBranch` — a new branch to create
	 *  there. In worktree cloud, null = let the remote auto-derive a fresh branch
	 *  from the message; otherwise the branch to fork the worktree from. Sourced
	 *  from the remote device's branch list (`getCloudGitState`). */
	cloudBranch: string | null;
	/** When executionMode === 'cloud' && !cloudWorktree: the chosen `cloudBranch`
	 *  is a NEW branch to create on the remote device (off its current branch),
	 *  rather than an existing one to check out. Always false in worktree mode. */
	cloudCreateNewBranch: boolean;
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
	/** Holds the user's message text while waiting for interrupt confirmation.
	 *  If the interrupt fails/times out, ChatInput restores this to the editor
	 *  so the user doesn't lose their message. Cleared on success or session switch. */
	pendingInterruptPrompt: string | null;
	/** True from the moment init() adopts a persisted sessionId until that
	 *  session's history has been fetched (onConnected's loadSession settles).
	 *  During this window the UI looks like "New Chat" (messages=[]) while
	 *  sessionId silently points at the restored session — sends would land in
	 *  the old session invisibly. ChatView suppresses the empty-state hero and
	 *  sendMessage waits for this to clear. Cleared by loadSession settling,
	 *  switchSession/newChat navigation, or reconnect exhaustion. */
	isRestoringSession: boolean;
	/** True while an explicit openChat() navigation's history fetch is in flight
	 *  (the sibling of isRestoringSession for user-driven session switches rather
	 *  than the boot-restore path). During this window sessionId already points at
	 *  the target session while messages is still [] — the transient empty state
	 *  that must NOT be mistaken for a cleared/new conversation. ChatView treats
	 *  it like the restore window (renders the message column, suppresses the
	 *  empty hero) so the cleared-session hint can't flash before history loads.
	 *  Unlike isRestoringSession, sendMessage does NOT wait on this flag —
	 *  openChat resets it independently and sends are gated elsewhere. Cleared by
	 *  openChat's loadSession settling and by newChat navigation. */
	isLoadingSession: boolean;
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
		deferredPermissions: [],
		pendingQuestions: [],
		queuedMessages: [],
		activeTasks: [],
		preserveBackgroundOnInterrupt: false,
		draft: '',
		scrollTop: 0,
		streamStartTime: null,
		executionMode: getPersistedExecutionMode(),
		cloudTargetInstanceId: null,
		cloudWorktree: false,
		cloudBranch: null,
		cloudCreateNewBranch: false,
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
		pendingInterruptPrompt: null,
		isRestoringSession: false,
		isLoadingSession: false,
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
	pendingRequestId: null,
	stopRequestedBeforeSession: false,
	stopBackgroundRequested: false,
	worktreeCreationGeneration: 0,
	worktreeCreateAbort: null,
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

			// Re-sync pending permission / AskUserQuestion modals for the ACTIVE
			// session on every (re)connect. While the WS was down, the running
			// session may have emitted a permission_request or user_question that
			// we never received — pendingPermissions/pendingQuestions are written
			// ONLY by WS handlers, so the modal would stay hidden until the user
			// switched sessions and back (switchSession re-requests them). Asking
			// the server to replay the pending queues here makes the modal pop up
			// immediately on reconnect, no session switch required.
			//
			// These are idempotent: the pending_permissions/pending_questions
			// handlers REPLACE (not append to) the active queues, keyed by
			// request_id, so re-requesting never double-shows an already-visible
			// modal. We request unconditionally (not just when streaming) because
			// a session paused awaiting a permission/question reports
			// isStreaming=false.
			wsConn.send({ type: 'get_pending_permissions' });
			wsConn.send({ type: 'get_pending_questions' });

			// The feed controller owns the transcript + streaming state (fed from
			// the SSE event log). Start it on (re)connect / boot-restore; permissions
			// and questions still flow over legacy WS above. The SSE feed replays
			// from_seq=0, so any messages missed during the disconnect window are
			// re-delivered.
			feedController.start(sessionId);
			if (_state.isRestoringSession) {
				_update(s => (s.sessionId === sessionId ? { ...s, isRestoringSession: false } : s));
			}
		}
	},
	onDisconnected: (code, _reason, _wasClean) => {
		_update(s => ({ ...s, wsConnected: false }));

		// The feed controller owns reconnect (SSE backoff + visibility/online
		// revive), so a WS drop needs no polling revival here.

		if (stopTimeout) { clearTimeout(stopTimeout); stopTimeout = null; }

		if (code === 4001) {
			import('$app/navigation').then(({ goto }) => goto('/login'));
		}
	},
	isStreaming: () => {
		return _state.isStreaming;
	},
	hasPendingTools: () => internal.pendingTools.size > 0,
	onMaxReconnectAttempts: () => {
		// No connection means the restore fetch will never happen — release any
		// pending sendMessage waiters so they fail fast instead of timing out.
		_update(s => ({ ...s, error: 'Connection lost. Please refresh the page.', isRestoringSession: false }));
	},
	// The SSE feed self-revives on visibilitychange/online (see sse-feed.ts), so
	// no visibility-driven polling is wired here.
});

// ─── Session Switcher (extracted module) ─────────────────────────────────────

const switcher = createSessionSwitcher({
	update: _update,
	getState: () => _state,
	wsSend: (data) => wsConn.send(data),
	internal,
	sessionEphemeralCache,
	onPersistTimerChanged: (timer) => { persistTimer = timer; },
});

const persistSession = switcher.persistSession;

// ─── WebSocket helpers (delegated to wsConn) ─────────────────────────────────

function disconnect() {
	feedController.stop();

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
	wsConn,
	switcher,
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
	feedRestart: (id) => feedController.start(id),
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
	sessionEphemeralCache,
	appData,
	persistSession,
	stopTimeout: {
		get: () => stopTimeout,
		set: (t) => { stopTimeout = t; },
	},
	getActiveSession: () => activeSession,
});

// V2 Phase 1: inject the private writer into the feed-controller singleton so it
// can fold SSE-fed reducer state into activeSession as the single writer for
// v2_stream sessions.
feedController.attach({ update: _update, getState: () => _state });

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

	// ── Messaging (delegated to message-sender) ─────────────────────

	sendMessage: (prompt: string, options?: { skipWorktreeSetup?: boolean; skipMessageAppend?: boolean; pendingUploadPaths?: string[] }) =>
		messageSender.sendMessage(prompt, options),

	stopGeneration: (stopBackground = false) => messageSender.stopGeneration(stopBackground),

	// ── Permission & Question Responses (delegated to permission-handler) ──

	sendPermissionResponse: permissionHandler.sendPermissionResponse,
	requeueDeferredPermissions: permissionHandler.requeueDeferredPermissions,
	sendUserQuestionResponse: permissionHandler.sendUserQuestionResponse,
	syncPendingPermissions: permissionHandler.syncPendingPermissions,
	syncPendingQuestions: permissionHandler.syncPendingQuestions,

	// ── Profile & Project ───────────────────────────────────────────

	setProfile(profileId: string) {
		_update(s => ({ ...s, profile: profileId }));
		switcher.saveSelectionToBackend(profileId, _state.project);
		persistSession();
	},

	/**
	 * Validate that the currently-selected profile still exists in the given
	 * profiles list. If it's missing (e.g. deleted on this device or another),
	 * fall back to a known profile so new sessions never load a null profile.
	 *
	 * Preference order for the replacement:
	 *   1. First built-in profile (most stable default)
	 *   2. First available profile
	 *   3. Empty string if the list is empty (UI will show "Profile" fallback)
	 *
	 * Persists the correction through `setProfile` so it survives reloads.
	 * No-op when the current selection is valid or unset.
	 */
	validateSelectedProfile(profilesList: Profile[]): boolean {
		const current = _state.profile;
		if (!current) return false;
		if (profilesList.some(p => p.id === current)) return false;

		const fallback =
			profilesList.find(p => p.is_builtin)?.id ??
			profilesList[0]?.id ??
			'';

		console.warn(
			`[ActiveSession] Selected profile "${current}" no longer exists. ` +
			`Falling back to "${fallback || '(none)'}".`
		);
		activeSession.setProfile(fallback);
		return true;
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
			// Keep the user's preferred mode; only the project-specific worktree
			// selection below is reset since it doesn't apply to the new project.
			executionMode: getPersistedExecutionMode(),
			// The chosen cloud branch is project-specific; drop it on project switch
			// (the worktree preference itself is kept, like executionMode).
			cloudBranch: null,
			cloudCreateNewBranch: false,
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

	// ── Ephemeral UI State (delegated to draft-manager) ─────────────

	/**
	 * Persist a draft straight into a specific session's ephemeral cache entry,
	 * bypassing the debounced store write. ChatInput calls this with the LEAVING
	 * session's id and its live editor content at the moment the active session
	 * changes, so the draft that survives a switch is always exactly what the
	 * user last saw — never a stale debounced value, and never the wrong
	 * session's text. `null` maps to the shared new-chat slot.
	 */
	saveDraftForSession(sessionId: string | null, draft: string) {
		sessionEphemeralCache.patchDraft(sessionId ?? '__new_chat__', draft);
	},

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

	setExecutionMode(mode: ExecutionMode) {
		// Remember the toggle so new sessions reopen in the same mode
		setPersistedExecutionMode(mode);
		// ExecutionMode is now local|worktree only. Any vestigial cloud offload
		// target + branch are cleared so they can't leak into a later flow.
		_update(s => ({
			...s,
			executionMode: mode,
			cloudTargetInstanceId: null,
			cloudBranch: null,
			cloudCreateNewBranch: false,
		}));
	},
	setCloudTarget(instanceId: string | null) {
		// The branch list is per-device; switching the target device invalidates
		// the chosen branch so it can't carry over to a device that may not have
		// it (the selector reloads the new device's branches and re-defaults).
		_update(s => ({
			...s,
			cloudTargetInstanceId: instanceId,
			cloudBranch: instanceId === s.cloudTargetInstanceId ? s.cloudBranch : null,
			cloudCreateNewBranch:
				instanceId === s.cloudTargetInstanceId ? s.cloudCreateNewBranch : false,
		}));
	},
	setCloudWorktree(val: boolean) {
		_update(s => ({ ...s, cloudWorktree: val }));
	},
	setCloudBranch(branch: string | null) {
		// Selecting an existing branch clears any pending "create new" intent.
		_update(s => ({ ...s, cloudBranch: branch, cloudCreateNewBranch: false }));
	},
	setCloudNewBranch(branch: string) {
		// Stage a not-yet-created branch: the worker creates it on send.
		_update(s => ({ ...s, cloudBranch: branch, cloudCreateNewBranch: true }));
	},
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

	// ── Connection ──────────────────────────────────────────────────

	/**
	 * Manually re-establish the WebSocket after a disconnect. Resets the
	 * exponential-backoff counter so the retry is immediate (the user just
	 * asked for it), then reconnects. Idempotent: connect() is a no-op if the
	 * socket is already open/connecting.
	 */
	reconnect: () => {
		wsConn.resetReconnectAttempts();
		wsConn.connect();
	},

	// ── Getters for derived stores ──────────────────────────────────

	getDeviceId: () => typeof window !== 'undefined' ? getOrCreateDeviceId() : '',
};

// ─── Derived Convenience Stores (via store-utils createDerived) ─────────────

export const activeWsConnected = createDerived(() => _state.wsConnected);
