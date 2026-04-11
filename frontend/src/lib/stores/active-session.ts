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
import { generateBranchName, generateBranchNameLocal, migrateUploadsToWorktree } from '$lib/api/git';
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
	fastMode: boolean;
	sessionLocked: boolean;
	sessionLockedBy: string | undefined;
	/** Accumulated text from WebSocket stream_delta events for instant rendering.
	 *  Cleared when the poller catches up or streaming ends. */
	wsStreamContent: string;
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
		fastMode: false,
		sessionLocked: false,
		sessionLockedBy: undefined,
		wsStreamContent: '',
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
	const next = fn(untrack(() => _state));
	if (next === _state) return; // Identity check — skip notification when nothing changed
	_state = next;
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
	worktreeCreationGeneration: 0,
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
		// Three-phase merge:
		// 1. Preserve client-side-only messages (hooks, status notifications)
		// 2. Merge WS stream buffer when ahead of poller content
		// 3. Stabilize message identity to prevent DOM recreation flicker
		_update(s => {
			let finalMessages = data.messages;
			let finalWsContent = s.wsStreamContent;

			// ── Phase 1: Preserve client-side-only messages ─────────────
			// System messages (hooks, status, etc.) are injected by the
			// frontend and don't exist in the server's live session store.
			// Without preservation, the poller wipes them every cycle.
			const serverIdSet = new Set(finalMessages.map(m => m.id));
			const clientOnlyMsgs = s.messages.filter(m =>
				m.type === 'system' && !serverIdSet.has(m.id)
			);
			if (clientOnlyMsgs.length > 0) {
				// Insert before the first streaming message to maintain
				// chronological ordering (client msgs were added pre-stream).
				let insertIdx = finalMessages.findIndex(m => m.streaming);
				if (insertIdx >= 0) {
					// Walk backward to avoid splitting a run of consecutive
					// tool_use messages. During streaming, some tools are
					// complete (streaming=false) while later tools are still
					// running (streaming=true). Inserting at firstStreamIdx
					// would land between complete and running tools, breaking
					// the frontend's adjacency-based tool grouping.
					while (insertIdx > 0 && finalMessages[insertIdx - 1].type === 'tool_use') {
						insertIdx--;
					}
					finalMessages = [
						...finalMessages.slice(0, insertIdx),
						...clientOnlyMsgs,
						...finalMessages.slice(insertIdx),
					];
				} else {
					finalMessages = [...finalMessages, ...clientOnlyMsgs];
				}
			}

			// ── Phase 2: WS stream buffer merge ────────────────────────
			if (data.isStreaming) {
				// Find a streaming assistant text message in the poller's data
				let pollerStreamIdx = -1;
				for (let i = finalMessages.length - 1; i >= 0; i--) {
					if (finalMessages[i].role === 'assistant' && finalMessages[i].type === 'text' && finalMessages[i].streaming) {
						pollerStreamIdx = i;
						break;
					}
				}

				if (pollerStreamIdx >= 0 && finalWsContent) {
					// Poller has a streaming message — merge WS content if ahead
					const pollerContent = finalMessages[pollerStreamIdx].content || '';
					if (finalWsContent.length > pollerContent.length) {
						finalMessages = [...finalMessages];
						finalMessages[pollerStreamIdx] = { ...finalMessages[pollerStreamIdx], content: finalWsContent };
					} else {
						// Poller caught up — clear WS buffer
						finalWsContent = '';
					}
				} else if (pollerStreamIdx < 0) {
					// Poller doesn't have a streaming assistant message yet.
					// Preserve the synthetic one from our current state so the
					// streaming indicator stays visible until real content arrives.
					//
					// Guard: only inject if the server hasn't already finalized
					// a text message that matches the synthetic. Without this
					// check, a stale streaming text from s.messages gets appended
					// as a ghost duplicate when the real text was already finalized
					// in the same poll cycle (text finalized + tool started).
					const currentSynthetic = s.messages.find(
						(m: ChatMessage) => m.role === 'assistant' && m.streaming && m.type === 'text'
					);
					if (currentSynthetic) {
						const alreadyFinalized = finalMessages.some(
							(m: ChatMessage) => m.role === 'assistant' && m.type === 'text' && !m.streaming &&
								m.content === currentSynthetic.content
						);
						if (!alreadyFinalized) {
							finalMessages = [...finalMessages, {
								...currentSynthetic,
								content: finalWsContent || currentSynthetic.content,
							}];
						}
					}
				} else {
					// Poller has streaming message but no WS content — clear buffer
					finalWsContent = '';
				}
			} else {
				// Not streaming — clear buffer
				finalWsContent = '';
			}

			// ── Phase 3: Stabilize message identity ────────────────────
			// When IDs transition from client-side (user-*, ws-stream-*)
			// to server-side (live-*), Svelte's keyed {#each} destroys and
			// recreates DOM elements, causing scroll-position jumps.
			//
			// Strategy: ID-first lookup with position fallback.
			// 1. Try exact ID match in existing messages (handles position shifts)
			// 2. Fall back to position-based match (handles synthetic→real transitions)
			// This avoids the pure position-based approach that breaks when messages
			// are inserted mid-array (thinking blocks, system messages).
			if (s.messages.length > 0 && finalMessages.length > 0) {
				// Build ID→message lookup from previous state
				const existingById = new Map<string, ChatMessage>();
				for (const m of s.messages) {
					if (m.id) existingById.set(m.id, m);
				}

				let needsStabilization = false;
				const stabilized = finalMessages.map((msg, i) => {
					// 1. Try exact ID match first (position-independent)
					let existing = existingById.get(msg.id);

					// 2. Fall back to position-based match for ID transitions
					//    (e.g., synthetic ws-stream-* → server live-*)
					if (!existing) {
						const positional = s.messages[i];
						if (positional && positional.role === msg.role && positional.type === msg.type) {
							existing = positional;
						}
					}

					if (!existing) return msg;

					// Identical content — reuse exact same object to skip re-render
					if (
						existing.content === msg.content &&
						existing.streaming === msg.streaming &&
						existing.toolResult === msg.toolResult &&
						existing.toolStatus === msg.toolStatus
					) {
						if (existing !== msg) needsStabilization = true;
						return existing;
					}
					// Content changed but same logical message — preserve ID for key stability
					if (existing.id !== msg.id) {
						needsStabilization = true;
						return { ...msg, id: existing.id };
					}
					return msg;
				});
				if (needsStabilization) finalMessages = stabilized;
			}

			// ── No-op detection ────────────────────────────────────────
			// If nothing visibly changed, return same state reference so
			// _update's identity check skips subscriber notification.
			const msgsUnchanged = finalMessages === s.messages ||
				(finalMessages.length === s.messages.length &&
				 finalMessages.every((m, i) => m === s.messages[i]));
			if (
				msgsUnchanged &&
				finalWsContent === s.wsStreamContent &&
				data.isStreaming === s.isStreaming &&
				!todosHaveUpdate && !usageHasUpdate && !streamingChanged && !metaHasUpdate
			) {
				return s;
			}

			return {
				...s,
				messages: msgsUnchanged ? s.messages : finalMessages,
				isStreaming: data.isStreaming,
				todos: todosHaveUpdate ? data.todos : s.todos,
				streamStartTime: data.isStreaming ? s.streamStartTime : null,
				wsStreamContent: finalWsContent,
				...usageDelta,
				...metaDelta,
			};
		});

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

	// Delegate to sendMessage() for the actual query. This ensures session
	// lifecycle flags (awaitingNewSession) are managed by a single code path
	// — the same one used by local sessions.
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
					effortLevelOverride: persisted.effortLevelOverride ?? null,
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

	async sendMessage(prompt: string, options?: { skipWorktreeSetup?: boolean; skipMessageAppend?: boolean; pendingUploadPaths?: string[] }) {
		// Worktree setup on first message
		if (!options?.skipWorktreeSetup && _state.executionMode === 'worktree' && !_state.sessionId && _state.messages.length === 0) {
			if (_state.worktreeMode === 'existing' && _state.worktreeId) {
				await startSessionOnExistingWorktree(prompt);
				return;
			} else if (_state.worktreeBranch) {
				// Branch already selected (manually or from dropdown) — create worktree
				await activeSession.createWorktree(prompt, options?.pendingUploadPaths);
				return;
			} else {
				// Auto-worktree: no branch selected — generate branch name from message
				await activeSession.autoCreateWorktree(prompt, options?.pendingUploadPaths);
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
					wsStreamContent: '',
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

		// When called from autoCreateWorktree, the user message is already in the
		// messages array (added optimistically). Skip appending a duplicate.
		if (options?.skipMessageAppend) {
			_update(s => ({
				...s,
				isStreaming: true,
				error: null,
				title: newTitle,
				currentQueryProfileId: queryProfileId,
				wsStreamContent: '',
			}));
		} else {
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
				wsStreamContent: '',
			}));
		}

		const overrides: Record<string, string> = {};
		if (_state.modelOverride) overrides.model = _state.modelOverride;
		if (_state.permissionModeOverride) overrides.permission_mode = _state.permissionModeOverride;
		if (_state.effortLevelOverride) overrides.effort = _state.effortLevelOverride;
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
	setEffortLevelOverride(level: string | null) { _update(s => ({ ...s, effortLevelOverride: level })); persistSession(); },
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

	/** Reset worktree selection to auto mode (no branch selected) */
	clearWorktreeSelection() {
		_update(s => ({ ...s, worktreeId: null, selectedWorktreeBranch: null, worktreeBranch: '', worktreeMode: 'new' as const }));
	},

	markWorktreeDeleted() {
		_update(s => ({ ...s, worktreeDeleted: true, worktreeId: null }));
	},

	async createWorktree(prompt?: string, pendingUploadPaths?: string[]) {
		if (!_state.profile) { _update(s => ({ ...s, worktreeError: 'Please select a profile' })); return; }
		if (!_state.project) { _update(s => ({ ...s, worktreeError: 'Please select a project' })); return; }
		if (!_state.worktreeBranch) { _update(s => ({ ...s, worktreeError: 'Please select a branch' })); return; }

		const creationGen = internal.worktreeCreationGeneration;
		const projectId = _state.project;
		const queryProfileId = _state.profile;
		const branchName = _state.worktreeBranch;

		_update(s => ({ ...s, isCreatingWorktree: true, worktreeError: null }));

		try {
			const response = await api.post<{
				session_id: string; worktree_id: string; branch_name: string;
				base_branch: string; worktree_path: string; status: string;
			}>('/sessions/with-worktree', {
				project_id: projectId,
				branch_name: branchName,
				base_branch: _state.worktreeBaseBranch || 'main',
				profile_id: queryProfileId,
				create_new_branch: _state.worktreeCreateNewBranch,
			});

			// Always refresh sidebar so the new session appears
			appData.loadSessions();
			appData.loadApiUserSessions();

			// User navigated away during worktree creation — send query
			// in background but don't clobber active session state.
			if (creationGen !== internal.worktreeCreationGeneration) {
				console.debug('[Worktree] User navigated away during createWorktree, sending query in background');
				if (prompt && wsConn.isConnected()) {
					wsConn.send({
						type: 'query',
						prompt,
						session_id: response.session_id,
						profile: queryProfileId,
						project: projectId,
						worktree_id: response.worktree_id,
					});
				}
				return;
			}

			_update(s => ({
				...s,
				sessionId: response.session_id,
				worktreeId: response.worktree_id,
				isCreatingWorktree: false,
			}));

			persistSession();

			// Migrate pending uploads to the new worktree
			if (pendingUploadPaths?.length && projectId) {
				try {
					await migrateUploadsToWorktree(projectId, response.worktree_id, pendingUploadPaths);
				} catch (e) {
					console.warn('[Worktree] Failed to migrate uploads:', e);
				}
			}

			// Send the user's message if provided. No need to disconnect/reconnect
			// the WS — the query handler registers the device automatically.
			if (prompt) {
				if (!wsConn.isConnected()) {
					wsConn.connect();
					await new Promise<void>((resolve) => {
						if (wsConn.isConnected()) { resolve(); return; }
						const timeout = setTimeout(() => { clearInterval(check); resolve(); }, 3000);
						const check = setInterval(() => {
							if (wsConn.isConnected()) { clearTimeout(timeout); clearInterval(check); resolve(); }
						}, 50);
					});
				}

				// Final staleness check
				if (creationGen !== internal.worktreeCreationGeneration) {
					console.debug('[Worktree] User navigated away during WS connect wait');
					wsConn.send({
						type: 'query',
						prompt,
						session_id: response.session_id,
						profile: queryProfileId,
						project: projectId,
						worktree_id: response.worktree_id,
					});
					return;
				}

				activeSession.sendMessage(prompt, { skipWorktreeSetup: true });
			}
		} catch (error) {
			if (creationGen !== internal.worktreeCreationGeneration) return;

			const err = error as { detail?: string; message?: string; status?: number };
			const rawError = err.detail || err.message || '';
			const formatted = rawError
				? formatWorktreeError(rawError, branchName)
				: `Failed to create worktree session for '${branchName}'. Check the backend logs for details.`;
			_update(s => ({
				...s,
				isCreatingWorktree: false,
				worktreeError: formatted,
			}));
		}
	},

	/**
	 * Auto-worktree: Generate a branch name from the user's message and create a worktree.
	 * Used when user sends a message in worktree mode without selecting a branch.
	 */
	async autoCreateWorktree(prompt: string, pendingUploadPaths?: string[]) {
		if (!_state.profile) { _update(s => ({ ...s, worktreeError: 'Please select a profile' })); return; }
		if (!_state.project) { _update(s => ({ ...s, worktreeError: 'Please select a project' })); return; }

		const queryProfileId = _state.profile;
		const projectId = _state.project;
		const userMsgId = `user-${Date.now()}`;

		// Capture the creation generation so we can detect if the user
		// switched sessions during any of our await points below.
		const creationGen = internal.worktreeCreationGeneration;

		// Issue 1 fix: Show the user message immediately so it doesn't disappear
		// while we create the worktree and generate a branch name.
		_update(s => ({
			...s,
			isCreatingWorktree: true,
			worktreeError: null,
			messages: [...s.messages, {
				id: userMsgId,
				role: 'user' as const,
				content: prompt,
				profileId: queryProfileId,
			}],
		}));

		try {
			// Generate branch name from the message
			let branchName: string;
			try {
				const result = await generateBranchName(projectId, prompt);
				branchName = result.branch_name;
			} catch {
				// Fallback to client-side generation
				branchName = generateBranchNameLocal(prompt);
			}

			// User switched sessions during branch name generation — abandon.
			// No worktree was created yet, so nothing to clean up.
			if (creationGen !== internal.worktreeCreationGeneration) {
				console.debug('[Worktree] User navigated away during branch name generation, aborting');
				return;
			}

			// Set the generated branch name and create the worktree
			_update(s => ({
				...s,
				worktreeBranch: branchName,
				worktreeCreateNewBranch: true,
			}));

			// Now create the worktree with the generated branch name
			const response = await api.post<{
				session_id: string; worktree_id: string; branch_name: string;
				base_branch: string; worktree_path: string; status: string;
			}>('/sessions/with-worktree', {
				project_id: projectId,
				branch_name: branchName,
				base_branch: _state.worktreeBaseBranch || 'main',
				profile_id: _state.profile,
				create_new_branch: true,
			});

			// Always refresh sidebar so the new session appears, even if user navigated away.
			appData.loadSessions();
			appData.loadApiUserSessions();

			// User switched sessions during worktree creation — don't clobber
			// the active session state.  The worktree + session exist on the
			// backend, so fire the query in the background (the response will
			// be persisted and visible when the user opens this session later).
			if (creationGen !== internal.worktreeCreationGeneration) {
				console.debug('[Worktree] User navigated away during worktree creation, sending query in background');
				if (wsConn.isConnected()) {
					const overrides: Record<string, string> = {};
					if (_state.modelOverride) overrides.model = _state.modelOverride;
					if (_state.permissionModeOverride) overrides.permission_mode = _state.permissionModeOverride;
					if (_state.effortLevelOverride) overrides.effort = _state.effortLevelOverride;
					if (_state.fastMode) overrides.fast_mode = 'true';
					wsConn.send({
						type: 'query',
						prompt,
						session_id: response.session_id,
						profile: queryProfileId,
						project: projectId,
						worktree_id: response.worktree_id,
						overrides: Object.keys(overrides).length > 0 ? overrides : undefined,
					});
				}
				return;
			}

			_update(s => ({
				...s,
				sessionId: response.session_id,
				worktreeId: response.worktree_id,
				selectedWorktreeBranch: response.branch_name,
				isCreatingWorktree: false,
			}));

			persistSession();

			// Migrate pending uploads to the new worktree
			if (pendingUploadPaths?.length && projectId) {
				try {
					await migrateUploadsToWorktree(projectId, response.worktree_id, pendingUploadPaths);
				} catch (e) {
					console.warn('[Worktree] Failed to migrate uploads:', e);
				}
			}

			// Issue 2 fix: Don't disconnect/reconnect the WebSocket — it causes
			// message flicker and a racy 500ms setTimeout. The WS query handler
			// registers the device with the session automatically when it receives
			// the query with session_id. Just send the message directly.
			if (!wsConn.isConnected()) {
				wsConn.connect();
				// Wait for connection before sending
				await new Promise<void>((resolve) => {
					if (wsConn.isConnected()) { resolve(); return; }
					const timeout = setTimeout(() => { clearInterval(check); resolve(); }, 3000);
					const check = setInterval(() => {
						if (wsConn.isConnected()) { clearTimeout(timeout); clearInterval(check); resolve(); }
					}, 50);
				});
			}

			// Final staleness check after potential WS reconnection wait
			if (creationGen !== internal.worktreeCreationGeneration) {
				console.debug('[Worktree] User navigated away during WS connect wait, sending query in background');
				wsConn.send({
					type: 'query',
					prompt,
					session_id: response.session_id,
					profile: queryProfileId,
					project: projectId,
					worktree_id: response.worktree_id,
				});
				return;
			}

			// Send the user's message — skip worktree setup since we just did it.
			// skipMessageAppend avoids duplicating the user message we already
			// added optimistically at the start of autoCreateWorktree.
			activeSession.sendMessage(prompt, { skipWorktreeSetup: true, skipMessageAppend: true });
		} catch (error) {
			// If user navigated away, don't show error in the new session's UI
			if (creationGen !== internal.worktreeCreationGeneration) return;

			const err = error as { detail?: string; message?: string; status?: number };
			const rawError = err.detail || err.message || '';
			const branchDisplay = _state.worktreeBranch || 'auto-generated branch';
			const formatted = rawError
				? formatWorktreeError(rawError, branchDisplay)
				: `Failed to auto-create worktree. Check the backend logs for details.`;
			_update(s => ({
				...s,
				isCreatingWorktree: false,
				worktreeError: formatted,
				// Remove the optimistic user message on failure
				messages: s.messages.filter(m => m.id !== userMsgId),
			}));
		}
	},

	// ── Queued Messages (server-side queue) ─────────────────────────

	queueMessage(content: string) {
		if (!_state.sessionId) return;

		// Optimistic local update — show the message in the queue bar immediately
		// rather than waiting for the server roundtrip.  When the server's
		// queue_updated event arrives it replaces this with the authoritative state.
		const optimisticId = `queued-local-${Date.now()}`;
		_update(s => ({
			...s,
			queuedMessages: [...s.queuedMessages, {
				id: optimisticId,
				content,
				queuedAt: Date.now(),
			}],
		}));

		const sent = wsConn.send({
			type: 'queue_message',
			session_id: _state.sessionId,
			content,
			profile: _state.currentQueryProfileId || _state.profile,
			project: _state.project || undefined,
		});

		if (!sent) {
			// WS send failed — remove the optimistic entry and surface an error
			// so the user knows to retry.  The message text is preserved in the
			// optimistic queue entry briefly; removing it here keeps state clean.
			_update(s => ({
				...s,
				queuedMessages: s.queuedMessages.filter(m => m.id !== optimisticId),
				error: 'Failed to queue message — connection lost. Please try again.',
			}));
		}
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
