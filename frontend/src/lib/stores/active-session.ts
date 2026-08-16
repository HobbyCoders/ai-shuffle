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
	getPersistedWorktreeIntent, setPersistedWorktreeIntent,
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
import { ensureModelRegistryHydrated } from '$lib/constants/models.svelte';
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
	/** Id of the turn currently believed live (from the v2 feed's `turn.*`
	 *  events; null when no turn is live). Read at Stop-press time so the stop
	 *  targets that turn — a stop delivered after its turn settled then no-ops
	 *  server-side instead of interrupting the successor turn (GitHub #585). */
	liveTurnId: string | null;
	/** Model the SDK's MAIN lane is ACTUALLY running for the live/last turn,
	 *  surfaced by the v2 feed's `system.note`/`model_info` events (null when no
	 *  note has arrived). Compared against the effective model selection to flag
	 *  a mid-turn reroute in the composer. Transient — reset on every session
	 *  switch / new chat and re-derived from the feed replay. */
	liveModel: string | null;
	/** The model that was REQUESTED for the turn `liveModel` reports on (from
	 *  the same `model_info` note). Transient — reset on every session switch /
	 *  new chat and re-derived from the feed replay, same as `liveModel`. */
	liveRequestedModel: string | null;
	/** Resolved model provider for the turn `liveModel` reports on (from the same
	 *  `model_info` note). null = native Anthropic; non-null = a gateway provider
	 *  session, in which case the composer suppresses the reroute pill (a provider
	 *  model never matches a Claude family). Transient — same lifecycle as
	 *  `liveModel`. */
	liveProvider: string | null;
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
	draft: string;
	scrollTop: number;
	streamStartTime: number | null;
	/** Compose-time Local/Worktree toggle intent, chosen by the user before the
	 *  first message. No metadata source of its own (a new session has no
	 *  worktree yet) — this is pure UI state, never derived from the session's
	 *  worktree fields (contrast with the existing-session worktree-ness checks
	 *  elsewhere, which read worktreeId/worktreeBranch directly). */
	worktreeIntent: ExecutionMode;
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
	worktreeIntent?: ExecutionMode;
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
		liveTurnId: null,
		liveModel: null,
		liveRequestedModel: null,
		liveProvider: null,
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
		draft: '',
		scrollTop: 0,
		streamStartTime: null,
		worktreeIntent: getPersistedWorktreeIntent(),
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

// ─── Feed reconcile watchdog ─────────────────────────────────────────────────
// Self-heal for a stalled transcript. The feed-controller's SSE feed is normally
// (re)started from onConnected, the session-switcher, and the WS start /
// session_created handlers. If none of those fire while a session stays open
// (e.g. the feed got stopped and nothing re-armed it while the WS stayed up),
// the transcript goes permanently stale until the user sends a message. This
// interval notices "a session is open but its feed is not feeding" and restarts
// the feed — the same from_seq=0 replay + `caught_up` batch fold a WS `start`
// event uses, which repaints the full snapshot correctly.
const FEED_RECONCILE_INTERVAL_MS = 10_000;
let feedReconcileTimer: ReturnType<typeof setInterval> | null = null;

/**
 * Feed-reconcile decision (pure, exported for unit tests): the feed must be
 * (re)started only when a session is open AND its feed is not currently feeding.
 * Returns false for a null/empty sessionId (New Chat / cleared — never start a
 * feed here) and for a session already being fed (healthy — nothing to do).
 */
export function shouldStartFeedReconcile(
	sessionId: string | null,
	isFeeding: (id: string) => boolean
): boolean {
	if (!sessionId) return false;
	return !isFeeding(sessionId);
}

function reconcileFeed(): void {
	const sessionId = _state.sessionId;
	if (!shouldStartFeedReconcile(sessionId, (id) => feedController.isFeeding(id))) return;
	console.warn(
		`[ActiveSession] feed-reconcile: session ${sessionId} is open but its feed ` +
		`is not feeding — restarting the SSE feed`
	);
	feedController.start(sessionId as string);
}

function startFeedReconcile(): void {
	if (feedReconcileTimer !== null) return; // idempotent — never two intervals
	if (typeof setInterval === 'undefined') return;
	feedReconcileTimer = setInterval(reconcileFeed, FEED_RECONCILE_INTERVAL_MS);
}

function stopFeedReconcile(): void {
	if (feedReconcileTimer !== null) {
		clearInterval(feedReconcileTimer);
		feedReconcileTimer = null;
	}
}

// Shared mutable state — passed by reference to ws-message-handlers and session-switcher.
const internal: InternalMutableState = {
	pendingTools: new Set(),
	interruptPending: false,
	awaitingNewSession: false,
	pendingRequestId: null,
	stopRequestedBeforeSession: false,
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

		// A WS (re)connect means the backend is reachable again — heal a boot-time
		// model-registry hydration failure (page booted during a restart window).
		void ensureModelRegistryHydrated();

		// Keep the feed-reconcile watchdog alive whenever we're connected. Idempotent:
		// re-arms after a destroy()/init() cycle (disconnect() clears it) without ever
		// creating a second interval.
		startFeedReconcile();

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
			// the SSE event log). Start it here ONLY when nothing is feeding this
			// session yet — the boot-restore path (first connect after a reload)
			// adopts the persisted sessionId without starting the feed, so this is
			// where it comes alive. Permissions and questions still flow over the
			// legacy WS above.
			//
			// Do NOT restart unconditionally on WS re-connect: the SSE feed owns its
			// own reconnect/backoff and replays from from_seq=lastSeq (see
			// sse-feed.ts), so a WS drop does not mean the feed lost events. An
			// unconditional restart tore down a healthy feed (stop + reducer reset +
			// full re-backfill), causing a visible transcript repaint ~1s after every
			// session open (#637). Mirror the isFeeding guard used at the session_created
			// / start call sites in ws-message-handlers.ts.
			if (!feedController.isFeeding(sessionId)) {
				feedController.start(sessionId);
			} else {
				// Already feeding — but a WS reconnect means the network just
				// hiccuped, and the SSE stream may have died the same death WITHOUT
				// erroring: a half-open socket parks reader.read() forever, and
				// isFeeding() only says a feed OBJECT exists, not that its stream is
				// alive. Left alone, the transcript freezes while work continues
				// server-side. revive() aborts the reader and resumes at
				// from_seq=lastSeq — no reducer reset, no repaint (unlike the
				// unconditional restart removed in #640), gap- and duplicate-free.
				feedController.revive(sessionId);
			}
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
	// Torn down here (the destroy path) so no interval outlives the store. It is
	// re-armed by onConnected on the next init()/connect.
	stopFeedReconcile();

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
feedController.attach({
	update: _update,
	getState: () => _state,
	// #606 item 4: let the SSE feed clear a permission/question card by request_id
	// when it tails a durable interaction.answered / interaction.expired event
	// (routed through the same injection the feed already uses for update/getState,
	// avoiding a circular import). Lazy ref: activeSession is defined just below and
	// this only runs at feed-event time.
	resolveInteractionByRequestId: (requestId: string) => activeSession.resolveInteractionByRequestId(requestId),
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

	// ── Messaging (delegated to message-sender) ─────────────────────

	sendMessage: (prompt: string, options?: { skipWorktreeSetup?: boolean; skipMessageAppend?: boolean; pendingUploadPaths?: string[] }) =>
		messageSender.sendMessage(prompt, options),

	stopGeneration: () => messageSender.stopGeneration(),

	// ── Permission & Question Responses (delegated to permission-handler) ──

	sendPermissionResponse: permissionHandler.sendPermissionResponse,
	requeueDeferredPermissions: permissionHandler.requeueDeferredPermissions,
	sendUserQuestionResponse: permissionHandler.sendUserQuestionResponse,
	syncPendingPermissions: permissionHandler.syncPendingPermissions,
	syncPendingQuestions: permissionHandler.syncPendingQuestions,

	/**
	 * Cross-device settle (#606 item 4): drop any pending permission, deferred
	 * permission, or pending question whose `request_id` matches. The card was
	 * seeded here from the legacy WS `get_pending_permissions` /
	 * `get_pending_questions` relay on switch-in, but the user may ANSWER it on
	 * ANOTHER device — no legacy removal frame reaches this device. The durable
	 * `interaction.answered` / `interaction.expired` events tailed off the SSE feed
	 * are then the only signal to clear the stale card (the feed-controller calls
	 * this when it sees them). Cheap no-op — no store write / notification — when
	 * nothing matches, so it is safe to fire on backfilled events too.
	 */
	resolveInteractionByRequestId(requestId: string) {
		if (!requestId) return;
		_update(s => {
			const matches =
				s.pendingPermissions.some(p => p.request_id === requestId) ||
				s.deferredPermissions.some(p => p.request_id === requestId) ||
				s.pendingQuestions.some(q => q.request_id === requestId);
			if (!matches) return s; // nothing to remove — identity return skips the write
			return {
				...s,
				pendingPermissions: s.pendingPermissions.filter(p => p.request_id !== requestId),
				deferredPermissions: s.deferredPermissions.filter(p => p.request_id !== requestId),
				pendingQuestions: s.pendingQuestions.filter(q => q.request_id !== requestId),
			};
		});
	},

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
		if (_state.worktreeIntent === 'local' && _state.messages.length > 0) {
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
			worktreeIntent: getPersistedWorktreeIntent(),
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

	/**
	 * Sync the chat header title for a session renamed elsewhere (e.g. the
	 * sessions board's inline rename). No-op unless the renamed session is the one
	 * currently open, mirroring the `session_updated` title-sync guard in
	 * ws-message-handlers — the header must only ever reflect the open session.
	 */
	setTitle(sessionId: string, title: string) {
		if (_state.sessionId !== sessionId) return;
		_update(s => ({ ...s, title }));
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

	setWorktreeIntent(mode: ExecutionMode) {
		// Remember the toggle so new sessions reopen in the same mode
		setPersistedWorktreeIntent(mode);
		_update(s => ({
			...s,
			worktreeIntent: mode,
		}));
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

	queueMessage: (content: string): Promise<boolean> => messageSender.queueMessage(content),
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
