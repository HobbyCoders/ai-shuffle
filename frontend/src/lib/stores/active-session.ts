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
import { api, recheckAuthOn401 } from '$lib/api/client';
import { acceptUserEvent } from '$lib/services/user-event-dedupe';
import type { SessionAttention } from './session/ws-message-handlers';
import type { SdkContextUsage } from '$lib/api/sessions';
import { notifySessionComplete, notifyWaitingForResponse } from '$lib/services/notifications';

// Session infrastructure
import { createWsConnection, type WsConnection } from './session/ws-connection';
import { createMessageHandlers, type InternalMutableState } from './session/ws-message-handlers';
import {
	createSessionSwitcher,
	getPersistedProfile, getPersistedProject,
	getPersistedWorktreeIntent, setPersistedWorktreeIntent,
	getPersistedModel, getPersistedEffort, getPersistedPermissionMode, normalizePermissionMode, getPersistedFastMode,
	getPersistedContextWindowChoice, setPersistedRunBinding,
	reconciledSessionMode,
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
import { composerPreferences, resolveComposerProject } from './composer-preferences';
import { appMode, type SessionMode } from './sessionMode.svelte';
import { git } from './git.svelte';
import { untrack } from 'svelte';
import { createStore, createSubscribers, shallowEqual } from './store-utils';
import { ensureModelRegistryHydrated } from '$lib/constants/models.svelte';
import type { ContextWindowChoice } from '$lib/constants/model-context';
// Shared types
import type {
	ChatMessage, MessageType,
	QueuedMessage, ExecutionMode, DeviceIntent, Task
} from './types';

// ─── Module-Level Exports ────────────────────────────────────────────────────

/** Cross-device streaming info for sidebar indicators */
export interface StreamingSessionInfo {
	projectId: string | null;
	worktreeId: string | null;
	/** The session's logical branch (legacy worktree_branch or the v2
	 *  sessions.branch). A streaming session with a branch runs in its OWN
	 *  checkout, so it must not lock main-checkout git controls. */
	worktreeBranch?: string | null;
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

const _bpStore = createStore<Map<string, SessionAttention>>(new Map());

/** Track pending prompts (permissions + questions) for non-active sessions.
 *  The value carries the outstanding count AND which kind arrived last, so the
 *  sidebar can distinguish "Needs Permission" from "Has Question". */
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
	/** Which sidepanel lane this surface belongs to — the single thing every
	 *  component reads to decide what to render, because it covers both cases
	 *  uniformly: for an OPEN session it is the session row's own mode, and for
	 *  a brand-new compose screen it is `appMode.current`. Components must not
	 *  read `appMode` directly; doing so would repaint an open code session as
	 *  chat the moment the user flipped the sidepanel switcher. */
	mode: SessionMode;
	title: string;
	messages: ChatMessage[];
	/** Whether a turn is live for this session.
	 *
	 *  SINGLE WRITER (finding S6): the SSE feed's reducer, folded through
	 *  feed-controller. The one sanctioned exception is `optimisticStreaming`
	 *  below, which the fold ORs in and clears. Nothing else may write this
	 *  field — competing writers produced the turn-start indicator stutter
	 *  (a WS `start` flipped it true, an SSE frame folded before `turn.started`
	 *  flipped it back, `turn.started` flipped it true again). */
	isStreaming: boolean;
	/** Optimistic streaming flip: the user hit send (or a WS `start` landed) and
	 *  the server has not yet reported `turn.started` on the event feed.
	 *
	 *  Set through `feedController.noteOptimisticSend()` / by the send paths in
	 *  message-sender; READ AND CLEARED by the feed fold, which publishes
	 *  `isStreaming = reducer.isStreaming || optimisticStreaming` and drops the
	 *  flag the moment the reducer confirms a live turn. Never write
	 *  `isStreaming` directly instead of this.
	 *
	 *  Optional in the type only so existing full-state literals (and other
	 *  modules' fixtures) keep compiling; `defaultState()` always sets it, and
	 *  every read treats `undefined` as false. */
	optimisticStreaming?: boolean;
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
	wsReconnectRequired: boolean;
	error: string | null;
	/** Session the server offered to fork locally: an `error` frame with
	 *  `offer_fork` means the worker that owns the session is unreachable.
	 *  Cleared together with `error`. */
	forkOfferSessionId?: string | null;
	profile: string;
	sessionProfileId: string | null;
	currentQueryProfileId: string | null;
	project: string;
	/** Origin-keyed `projects_v2` id. Set alongside `project` for a local
	 *  catalog row; set alone (with `project` empty) for remote-only compose. */
	projectV2Id: string | null;
	totalTokensIn: number;
	totalTokensOut: number;
	totalCacheCreationTokens: number;
	totalCacheReadTokens: number;
	contextUsed: number | null;
	contextMax: number;
	/**
	 * Context breakdown for the most recent model call, delivered on the
	 * `turn.usage` event stream rather than fetched. Lets the context bar
	 * refresh per model call while a turn runs; the turn-end fetch still
	 * overrides it with the persisted final. Null until a snapshot arrives.
	 */
	liveContextUsage: SdkContextUsage | null;
	modelOverride: string | null;
	permissionModeOverride: string | null;
	effortLevelOverride: string | null;
	/** Fast service tier for this session — faster output at a higher price, and
	 *  a cleared prompt cache whenever it flips. Only offered on models that
	 *  support it (`isFastModeCapable`); false everywhere else. */
	fastMode: boolean;
	/** Additional projects for this chat only. The main project remains its cwd. */
	workspaceProjectIds?: string[];
	/** Semantic 1M vs model-family standard-window choice for eligible GPT/Claude models. */
	contextWindowChoice: ContextWindowChoice;
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
	/** Compose-time device pick for a NEW session — `null` = this device.
	 *  Composes freely with worktreeIntent 'worktree' (ruling D7): the worktree
	 *  request travels to the picked install, which materializes the checkout on
	 *  its own disk via the durable git-ops queue. Cleared as soon as the query
	 *  is sent: the session row itself then carries the ownership. */
	deviceIntent: DeviceIntent | null;
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
	/** User prompt text waiting to be restored into the composer so the user
	 *  can edit or resend it. Set when a stopped turn's orphan message is
	 *  discarded (`user_message_discarded`) and when a rewind removes the
	 *  target user message. ChatInput refills an EMPTY editor and clears this,
	 *  so it fires exactly once and never clobbers typed text. */
	pendingRestorePrompt: string | null;
	/** True from the moment init() adopts a persisted sessionId until that
	 *  session's SSE history reaches its first caught_up boundary.
	 *  During this window the UI looks like "New Chat" (messages=[]) while
	 *  sessionId silently points at the restored session — sends would land in
	 *  the old session invisibly. ChatView suppresses the empty-state hero and
	 *  sendMessage waits for this to clear. Cleared by the feed's caught_up fold,
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
	 *  the matching feed's caught_up fold and by newChat navigation. */
	isLoadingSession: boolean;
	/** Degraded hydration state (finding S1): the session's SSE feed has been
	 *  open for longer than the pre-first-frame budget without delivering a
	 *  single event. `isLoadingSession` stays TRUE throughout — this only says
	 *  the wait is abnormal, so the surface can show "still loading" rather than
	 *  either a bare spinner or, worse, an empty transcript read as "cleared".
	 *  Written only by feed-controller; cleared when hydration completes.
	 *  Optional for the same reason as `optimisticStreaming` above. */
	isHydrationSlow?: boolean;
}

/** Minimal shape persisted to the backend.
 *
 *  `mode` is a SNAPSHOT, not the authority. The row still decides a session's
 *  lane (switchSession reads it, and `reconcileSessionMode` re-reads it once
 *  the session list lands). It is carried here anyway because the boot restore
 *  adopts a sessionId long before any row is available, and the only other
 *  value on hand at that moment is `appMode` — which lives in device-wide
 *  localStorage and may have been moved by a different tab, repainting a
 *  restored code session as chat (and vice versa). Restoring the snapshot
 *  makes that first frame right; the reconcile makes it authoritative. */
export interface PersistedSession {
	sessionId: string | null;
	mode?: SessionMode;
	title: string;
	profile: string;
	project: string;
	projectV2Id?: string | null;
	modelOverride: string | null;
	permissionModeOverride: string | null;
	effortLevelOverride: string | null;
	fastMode?: boolean;
	workspaceProjectIds?: string[];
	contextWindowChoice?: ContextWindowChoice;
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
		// A fresh surface opens in whichever lane the sidepanel is showing;
		// switchSession overwrites this from the session's own row.
		mode: appMode.current,
		title: 'New Chat',
		messages: [],
		isStreaming: false,
		optimisticStreaming: false,
		liveTurnId: null,
		liveModel: null,
		liveRequestedModel: null,
		liveProvider: null,
		wsConnected: false,
		wsReconnectRequired: false,
		error: null,
		forkOfferSessionId: null,
		profile: getPersistedProfile(),
		sessionProfileId: null,
		currentQueryProfileId: null,
		project: getPersistedProject(),
		projectV2Id: null,
		totalTokensIn: 0,
		totalTokensOut: 0,
		totalCacheCreationTokens: 0,
		totalCacheReadTokens: 0,
		contextUsed: null,
		contextMax: 200_000,
		liveContextUsage: null,
		// A new chat opens on the last binding the user picked (agents no
		// longer supply one); an existing session overwrites these from its
		// own row when it loads.
		modelOverride: getPersistedModel(),
		permissionModeOverride: getPersistedPermissionMode(),
		effortLevelOverride: getPersistedEffort(),
		fastMode: getPersistedFastMode(),
		workspaceProjectIds: [],
		contextWindowChoice: getPersistedContextWindowChoice(),
		pendingPermissions: [],
		deferredPermissions: [],
		pendingQuestions: [],
		queuedMessages: [],
		activeTasks: [],
		draft: '',
		scrollTop: 0,
		streamStartTime: null,
		worktreeIntent: getPersistedWorktreeIntent(),
		// Never restored from a preference: every new chat starts on this device.
		deviceIntent: null,
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
		pendingRestorePrompt: null,
		isRestoringSession: false,
		isLoadingSession: false,
		isHydrationSlow: false,
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
	awaitingNewSession: false,
	pendingRequestId: null,
	stopRequestedBeforeSession: false,
	worktreeCreationGeneration: 0,
	worktreeCreateAbort: null,
};

// ─── WebSocket Connection (extracted module) ─────────────────────────────────

const wsConn: WsConnection = createWsConnection({
	getAuthToken,
	getDeviceId: getOrCreateDeviceId,
	onMessage: (data) => {
		if (acceptUserEvent(data, 'chat')) handleMessage(data);
	},
	onConnected: () => {
		console.debug(`[ActiveSession] onConnected: sessionId=${_state.sessionId}, isStreaming=${_state.isStreaming}, hasMessages=${_state.messages.length > 0}`);
		_update(s => ({ ...s, wsConnected: true, wsReconnectRequired: false, error: null }));

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
			// The feed clears isRestoringSession only when its first caught_up
			// atomically publishes the complete transcript. A connected socket alone
			// does not mean session history is loaded.
		}
	},
	onDisconnected: (code, _reason, _wasClean) => {
		_update(s => ({ ...s, wsConnected: false }));

		// The feed controller owns reconnect (SSE backoff + visibility/online
		// revive), so a WS drop needs no polling revival here.

		if (stopTimeout) { clearTimeout(stopTimeout); stopTimeout = null; }

		if (code === 4001) {
			// This close code stops automatic retries. Keep the current session and
			// draft available for the existing manual Reconnect action if worker
			// authentication is still valid (or cannot currently be checked).
			_update(s => ({ ...s, wsReconnectRequired: true, isRestoringSession: false }));
			void recheckAuthOn401();
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
	internal.awaitingNewSession = false;

	if (stopTimeout) { clearTimeout(stopTimeout); stopTimeout = null; }

	wsConn.disconnect();
	_update(s => ({ ...s, wsConnected: false }));
}

// ─── WebSocket Message Handler (extracted to session/ws-message-handlers.ts) ─

export function handleGlobalUserEvent(data: Record<string, unknown>) {
 if (acceptUserEvent(data, 'global')) handleMessage(data);
}

const { handleMessage } = createMessageHandlers({
	update: _update,
	getState: () => _state,
	wsSend: (data) => wsConn.send(data),
	streamingSessions,
	backgroundPermissions,
	gitRefreshStatus: () => git.refreshStatus(),
	gitRefreshDiffStats: () => git.refreshDiffStats(),
	gitRefreshPrState: () => { git.refreshPrState(true).catch(() => {}); },
	gitGetProjectId: () => git.current.projectId,
	gitGetLoading: () => git.current.loading,
	appLoadSessions: () => appData.loadSessions(),
	appLoadApiUserSessions: () => appData.loadApiUserSessions(),
	mergeSessionPatch: (sessionId, patch) => appData.mergeSessionPatch(sessionId, patch),
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
	feedRestart: (id) => feedController.restart(id),
	feedRevive: (id) => feedController.revive(id),
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

function rememberBranchIntent() {
	composerPreferences.set('checkout', _state.worktreeBranch ? {
		kind:'branch', projectId:_state.project, projectV2Id:_state.projectV2Id,
		branch:_state.worktreeBranch, baseBranch:_state.worktreeBaseBranch,
		createNew:_state.worktreeCreateNewBranch,
	} : null);
}

export const activeSession = {
	get current() { return _state; },

	subscribe: _subscribe,

	init: async () => { await sessionInitModule.init(); activeSession.applyComposerDefaults(); },
	destroy: sessionInitModule.destroy,
	resetForAccountChange() {
		bumpInitGeneration();
		setSkipRestore(false);
		sessionInitModule.destroy();
	},

	// ── Session Navigation (delegated to session-switcher) ─────────

	switchSession: (sessionId: string, projectId?: string | null, worktreeInfo?: import('./session/session-switcher').SessionWorktreeInfo | null, title?: string | null, projectV2Id?: string | null) => {
		setSkipRestore(false);
		// Explicit navigation wins over an async boot restore still reading the
		// persisted session. Its generation checks will now discard that stale load.
		bumpInitGeneration();
		void switcher.switchSession(sessionId, projectId, worktreeInfo, title, projectV2Id);
	},

	newChat: () => {
		setSkipRestore(true);
		bumpInitGeneration();
		const restoredDraft = switcher.newChat();
		if (!restoredDraft) activeSession.applyComposerDefaults();
	},

	// ── Messaging (delegated to message-sender) ─────────────────────

	sendMessage: (prompt: string, options?: { skipWorktreeSetup?: boolean; skipMessageAppend?: boolean; pendingUploadPaths?: string[] }) =>
		messageSender.sendMessage(prompt, options),

	stopGeneration: () => messageSender.stopGeneration(),
	interruptForQueuedMessages: () => messageSender.interruptForQueuedMessages(),

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
		composerPreferences.set('profile', profileId);
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
	 * Catalog validation is local presentation, not a new account-wide choice.
	 * Existing sessions retain their own binding even if a catalog is unavailable.
	 */
	validateSelectedProfile(profilesList: Profile[]): boolean {
		const current = _state.profile;
		if (!current) return false;
		if (profilesList.some(p => p.id === current)) return false;
		if (_state.sessionId) return false;

		const fallback =
			profilesList.find(p => p.is_builtin)?.id ??
			profilesList[0]?.id ??
			'';

		console.warn(
			`[ActiveSession] Selected profile "${current}" no longer exists. ` +
			`Falling back to "${fallback || '(none)'}".`
		);
		_update(s => ({...s, profile:fallback}));
		return true;
	},

	/**
	 * Re-derive the open session's lane from its row once the session list has
	 * loaded, and report the lane the surface settled in.
	 *
	 * The boot restore adopts a sessionId from a persisted snapshot, not from a
	 * row — the row does not exist yet at that point in init(). The snapshot is
	 * usually right, but it can be stale (it may come from the device-wide
	 * server key written by a different tab), and the row is the stated
	 * authority for a session's lane. So the caller reconciles once, right
	 * after loadSessions() resolves, exactly like validateSelectedProfile above.
	 *
	 * Returns the settled mode for an open session so the caller can align the
	 * sidepanel lane with it (a restored session the switcher is not showing is
	 * a session the user cannot find in the list). Returns null for an unbound
	 * compose or a sessionId that is not in the list — neither has a row to
	 * defer to, so `mode` keeps whatever it already holds.
	 */
	reconcileSessionMode(sessionsList: { id: string; mode?: string | null }[]): SessionMode | null {
		const sessionId = _state.sessionId;
		const mode = reconciledSessionMode(sessionId, sessionsList);
		if (!mode) return null;
		if (mode !== _state.mode) {
			console.debug(`[ActiveSession] reconcileSessionMode: ${_state.mode} -> ${mode} for ${sessionId}`);
			_update(s => ({ ...s, mode }));
		}
		return mode;
	},

	setProject(projectId: string, projectV2Id?: string | null) {
		if (_state.worktreeIntent === 'local' && _state.messages.length > 0) {
			console.warn('[ActiveSession] Cannot change project after session has started');
			return;
		}
		const nextV2 = projectV2Id === undefined
			? appData.current.clusterProjects.find(p => p.local_project_id === projectId)?.project_v2_id ?? null
			: projectV2Id;
		composerPreferences.set('project', {projectId, projectV2Id:nextV2});
		if (_state.project === projectId && _state.projectV2Id === nextV2) {
			return;
		}
		_update(s => ({
			...s,
			project: projectId,
			projectV2Id: nextV2,
			workspaceProjectIds: (s.workspaceProjectIds ?? []).filter(id => id !== projectId),
			// Keep the user's preferred mode; only the project-specific worktree
			// selection below is reset since it doesn't apply to the new project.
			worktreeIntent: getPersistedWorktreeIntent(),
			// The picked device was validated against the OLD project's checkout.
			deviceIntent: null,
			worktreeBranch: '',
			worktreeBaseBranch: 'main',
			worktreeCreateNewBranch: false,
			worktreeId: null,
			worktreeMode: 'new' as const,
			selectedWorktreeBranch: null,
			isCreatingWorktree: false,
			worktreeError: null,
		}));
		composerPreferences.set('checkout', null);
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

	// ── Session Run Binding ─────────────────────────────────────────
	//
	// The model / effort / permission mode this session runs on. Picked when
	// the session is created and changeable mid-session; the backend stores it
	// on the session row from the overrides every send carries, so the choice
	// outlives this tab. Each pick is also remembered as the default for the
	// NEXT new chat.

	setModelOverride(model: string | null) { _update(s => ({ ...s, modelOverride: model })); setPersistedRunBinding('model', model); persistSession(); },
	setPermissionModeOverride(mode: string | null) { mode = normalizePermissionMode(mode); _update(s => ({ ...s, permissionModeOverride: mode })); setPersistedRunBinding('permissionMode', mode); persistSession(); },
	setEffortLevelOverride(level: string | null) { _update(s => ({ ...s, effortLevelOverride: level })); setPersistedRunBinding('effort', level); persistSession(); },
	setFastMode(on: boolean) { _update(s => ({ ...s, fastMode: on })); setPersistedRunBinding('fastMode', on ? '1' : ''); persistSession(); },
	async setWorkspaceProjectIds(ids: string[]) {
		const owner = { sessionId: _state.sessionId, project: _state.project, projectV2Id: _state.projectV2Id };
		let next = [...new Set(ids)].filter(id => id !== owner.project);
		if (owner.sessionId) {
			const saved = await api.put<{ workspace_project_ids: string[] }>(`/sessions/${owner.sessionId}/workspace-folders`, { workspace_project_ids: next });
			next = saved.workspace_project_ids;
			const cached = sessionEphemeralCache.restore(owner.sessionId);
			if (cached) sessionEphemeralCache.save(owner.sessionId, { ...cached, workspaceProjectIds: next });
		}
		if (_state.sessionId === owner.sessionId && _state.project === owner.project && _state.projectV2Id === owner.projectV2Id) {
			_update(s => ({ ...s, workspaceProjectIds: next }));
			persistSession();
		}
	},
	setContextWindowChoice(choice: ContextWindowChoice) { _update(s => ({ ...s, contextWindowChoice: choice })); setPersistedRunBinding('contextWindowChoice', choice); persistSession(); },

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
	setPendingRestorePrompt: draftManager.setPendingRestorePrompt,
	clearPendingRestorePrompt: draftManager.clearPendingRestorePrompt,
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
	/**
	 * Pick the device a NEW session runs on (`null` = this device).
	 *
	 * Deliberately NOT persisted anywhere durable (contrast with
	 * setWorktreeIntent's localStorage preference): a device pick applies to the
	 * chat being composed, never to the next one.
	 */
	setDeviceIntent(intent: DeviceIntent | null) {
		_update(s => ({ ...s, deviceIntent: intent }));
	},

	setWorktreeBranch(branch: string) { _update(s => ({ ...s, worktreeBranch: branch })); rememberBranchIntent(); },
	setWorktreeBaseBranch(branch: string) { _update(s => ({ ...s, worktreeBaseBranch: branch })); if (_state.worktreeBranch) rememberBranchIntent(); },
	setWorktreeMode(mode: 'new' | 'existing') { _update(s => ({ ...s, worktreeMode: mode })); if (mode === 'new') rememberBranchIntent(); },
	setWorktreeCreateNewBranch(val: boolean) { _update(s => ({ ...s, worktreeCreateNewBranch: val })); if (_state.worktreeBranch) rememberBranchIntent(); },

	selectWorktreeBranch(branch: string, worktreeId: string) {
		composerPreferences.set('checkout', {kind:'existing',projectId:_state.project, projectV2Id:_state.projectV2Id, worktreeId, branch});
		_update(s => ({ ...s, worktreeId, worktreeBranch:'', selectedWorktreeBranch: branch, worktreeMode: 'existing' as const }));
	},

	clearWorktreeSelection() {
		composerPreferences.set('checkout', null);
		_update(s => ({ ...s, worktreeId: null, selectedWorktreeBranch: null, worktreeBranch: '', worktreeMode: 'new' as const }));
	},

	markWorktreeDeleted() {
		_update(s => ({ ...s, worktreeDeleted: true }));
	},

	/** Remote defaults may update an idle composer, never an existing session or typed draft. */
	applyComposerDefaults() {
		if (_state.sessionId || _state.draft || _state.worktreeBranch || _state.isStreaming || _state.optimisticStreaming || _state.isCreatingWorktree || _state.isRestoringSession || internal.awaitingNewSession) return;
		const picks = composerPreferences.current;
		const project = resolveComposerProject(picks.project, appData.current.projects, appData.current.clusterProjects);
		const code = _state.mode !== 'chat';
		const checkout = code && picks.worktreeIntent === 'worktree' && picks.checkout
			&& (picks.checkout.projectV2Id ? picks.checkout.projectV2Id === project.projectV2Id : picks.checkout.projectId === project.project)
			? picks.checkout : null;
		// A legacy worktree ID belongs to its original checkout. Never transplant
		// it into a different runner's clone, even when both share a Git origin.
		const existing = checkout?.kind === 'existing' ? checkout : null;
		// Staging a branch protects this composer from later defaults. Wait for
		// its project to resolve first, or that guard also blocks a late catalog
		// from mapping a shared UUID to this worker's local clone.
		const projectResolved = !!project.project || (!!project.projectV2Id
			&& appData.current.clusterProjects.some(p => p.project_v2_id === project.projectV2Id
				&& p.local_project_id === null));
		const branch = checkout?.kind === 'branch' && projectResolved ? checkout : null;
		const localCheckout = existing && existing.projectId === project.project && !!project.project;
		_update(s => ({...s,
			profile:picks.profile, modelOverride:picks.model, effortLevelOverride:picks.effort,
			permissionModeOverride:code ? normalizePermissionMode(picks.permissionMode) : null, fastMode:picks.fastMode === '1',
			contextWindowChoice:getPersistedContextWindowChoice(),
			...(code ? project : {project:'', projectV2Id:null}),
			worktreeIntent:code ? picks.worktreeIntent : 'local',
			worktreeId:localCheckout ? existing!.worktreeId : null,
			selectedWorktreeBranch:existing?.branch ?? null,
			worktreeBranch:branch?.branch ?? '', worktreeBaseBranch:branch?.baseBranch ?? 'main',
			worktreeCreateNewBranch:branch?.createNew ?? false, worktreeMode:existing ? 'existing' : 'new',
			worktreeError:existing && !localCheckout ? 'The selected worktree belongs to another device. Select an available worktree to continue.' : null,
		}));
	},

	createWorktree: (prompt?: string, pendingUploadPaths?: string[]) =>
		messageSender.createWorktree(prompt, pendingUploadPaths),

	autoCreateWorktree: (prompt: string, pendingUploadPaths?: string[]) =>
		messageSender.autoCreateWorktree(prompt, pendingUploadPaths),

	// ── Pending Messages (delegated to message-sender) ─────────────

	removeQueuedMessage: (id: string) => messageSender.removeQueuedMessage(id),

	// ── System Messages & Error Handling (delegated to draft-manager) ──

	// (already delegated above)

	// ── Rewind (delegated to draft-manager) ─────────────────────────

	// (already delegated above)

	// ── Session Takeover (delegated to draft-manager) ───────────────

	// (already delegated above)

	// ── Connection ──────────────────────────────────────────────────

	/**
	 * Replace a failed control socket immediately, even when the browser still
	 * reports OPEN/CONNECTING. The independent transcript feed keeps running.
	 */
	reconnect: () => {
		wsConn.reconnect();
	},

	// ── Getters for derived stores ──────────────────────────────────

	getDeviceId: () => typeof window !== 'undefined' ? getOrCreateDeviceId() : '',
};

// ─── Derived Convenience Stores (via store-utils createDerived) ─────────────
//
// FINE-GRAINED SUBSCRIPTIONS (finding R1/W10).
//
// `activeSession` is one large state object whose subscribers are ALL notified
// on every write — and the SSE fold writes it many times a second while a turn
// streams. Seventeen components subscribe to it today; most read one or two
// fields, so every streamed token re-invokes every one of them.
//
// These derived stores subscribe to the same notifications but re-invoke their
// own subscriber ONLY when the value they select actually changed (the equality
// short-circuit in store-utils' createDerived). A component that needs just the
// streaming flag, or just the header metadata, does zero work while text
// streams in.
//
// PURELY ADDITIVE: `activeSession` itself is unchanged, so consumers migrate one
// at a time. Migration guide — replace `$activeSession.messages` with
// `$activeSessionMessages`, `$activeSession.isStreaming` with
// `$activeSessionStreaming`, and a header/title/profile read with
// `$activeSessionMeta`.

export const activeWsConnected = createDerived(() => _state.wsConnected);

/** The active session's id (null on a New Chat compose screen). */
export const activeSessionId = createDerived(() => _state.sessionId);

/** The transcript. Reference-stable between folds that did not change it, so
 *  the identity check alone is the right equality here. */
export const activeSessionMessages = createDerived(() => _state.messages);

/** Whether a turn is live — the reducer's value, already unioned with the
 *  optimistic flip by the fold (see ActiveSessionState.isStreaming). */
export const activeSessionStreaming = createDerived(() => _state.isStreaming);

/** Header/identity metadata: everything a title bar or a tab needs, and nothing
 *  that changes while a turn streams. */
export const activeSessionMeta = createDerived(
	() => ({
		sessionId: _state.sessionId,
		title: _state.title,
		profile: _state.profile,
		sessionProfileId: _state.sessionProfileId,
		project: _state.project,
		projectV2Id: _state.projectV2Id,
	}),
	shallowEqual
);

/** Hydration state of the session surface: whether history is still loading and
 *  whether that load has gone abnormally slow (S1). */
export const activeSessionLoading = createDerived(
	() => ({
		isLoadingSession: _state.isLoadingSession,
		isRestoringSession: _state.isRestoringSession,
		isHydrationSlow: _state.isHydrationSlow,
	}),
	shallowEqual
);

/** Token / context gauge inputs, for the context bar only. */
export const activeSessionUsage = createDerived(
	() => ({
		totalTokensIn: _state.totalTokensIn,
		totalTokensOut: _state.totalTokensOut,
		totalCacheCreationTokens: _state.totalCacheCreationTokens,
		totalCacheReadTokens: _state.totalCacheReadTokens,
		contextUsed: _state.contextUsed,
		contextMax: _state.contextMax,
		liveContextUsage: _state.liveContextUsage,
	}),
	shallowEqual
);

composerPreferences.onRemote(() => activeSession.applyComposerDefaults());
