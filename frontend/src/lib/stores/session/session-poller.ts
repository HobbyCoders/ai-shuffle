/**
 * Session Poller — SOLE authority for session state from the server.
 *
 * Architecture (polling-only, no dual-authority):
 * - This is the ONLY module that writes to session-state and activeSession.
 * - WebSocket is notification-only: it triggers pollNow() or startStreaming().
 * - Session loading (on switch, reconnect) uses HTTP fetch, not WS messages.
 * - All state transitions (streaming start/end) are detected from poll responses.
 *
 * Polling rates:
 * - 300ms during streaming (real-time feel)
 * - Stops entirely when streaming ends (no idle polling)
 *
 * Key behaviors:
 * - Sends ?since_update_id=N → gets 304 if nothing changed (cheap)
 * - On update: replaces session state entirely (no merging)
 * - Extracts TodoWrite inputs for todo list display
 * - Stops automatically when streaming ends
 */

import type { ChatMessage, TodoItem, SubagentChildMessage, MessageType } from '../types';
import { sessionState, type SessionUsage } from './session-state';

// ─── Configuration ───────────────────────────────────────────────────────────

const STREAMING_POLL_INTERVAL_MS = 300;

// ─── Types ───────────────────────────────────────────────────────────────────

interface PollTimer {
	timer: ReturnType<typeof setInterval>;
	sessionId: string;
	interval: number;
}

type StreamEndCallback = (sessionId: string, usage: SessionUsage) => void;
type PollErrorCallback = (sessionId: string, consecutiveErrors: number, error: unknown) => void;

/** Worktree metadata from the server, included in /live responses. */
export interface SessionMeta {
	title?: string;
	worktree_id?: string | null;
	worktree_branch?: string | null;
	worktree_deleted?: boolean;
	execution_mode?: string;
}

/** Data shape returned by the onPollApplied callback */
export interface PollAppliedData {
	messages: ChatMessage[];
	isStreaming: boolean;
	usage: SessionUsage;
	todos: TodoItem[];
	sessionMeta?: SessionMeta;
}

/** Number of consecutive failures before notifying the error callback */
const CONSECUTIVE_ERROR_THRESHOLD = 3;

// ─── Poller Class ────────────────────────────────────────────────────────────

class SessionPoller {
	/** Active polling timers keyed by sessionId */
	private _timers: Map<string, PollTimer> = new Map();

	/** In-flight poll requests (prevent overlapping) */
	private _inFlight: Set<string> = new Set();

	/** Pending debounced pollNow timers keyed by sessionId */
	private _pendingPollNow: Map<string, ReturnType<typeof setTimeout>> = new Map();

	/** Consecutive error counts per session */
	private _errorCounts: Map<string, number> = new Map();

	/**
	 * Streaming generation counter per session. Incremented each time
	 * startStreaming() is called. Captured before each _poll() so that
	 * _applyPollResult can detect stale responses — if the generation
	 * changed between poll start and response, a new streaming session
	 * was started and the old poll's streamingJustEnded is invalid.
	 */
	private _streamingGeneration: Map<string, number> = new Map();

	/** Callback when streaming ends (cleanup: cache, remove session, refresh list) */
	private _onStreamEnd: StreamEndCallback | null = null;

	/** Callback after each successful poll applies state (direct path to activeSession) */
	private _onPollApplied: ((sessionId: string, data: PollAppliedData) => void) | null = null;

	/** Callback when consecutive poll errors exceed threshold */
	private _onPollError: PollErrorCallback | null = null;

	/**
	 * Register a callback for when streaming ends.
	 * Used to trigger notifications, session list refresh, etc.
	 */
	onStreamEnd(callback: StreamEndCallback): void {
		this._onStreamEnd = callback;
	}

	/**
	 * Register a callback invoked after each successful poll writes to sessionState.
	 * This is the SOLE path from server → activeSession state.
	 */
	onPollApplied(callback: (sessionId: string, data: PollAppliedData) => void): void {
		this._onPollApplied = callback;
	}

	/**
	 * Register a callback for poll errors.
	 * Called when consecutive errors reach the threshold (3 by default).
	 * Used to show error indicators in the UI.
	 */
	onPollError(callback: PollErrorCallback): void {
		this._onPollError = callback;
	}

	/**
	 * Start polling for a session at the streaming rate.
	 * If already polling this session at streaming rate, this is a no-op.
	 *
	 * Preserves existing session state (messages, usage) when restarting
	 * polling for an existing session. Only creates fresh state for sessions
	 * not yet tracked. This prevents the context usage bar from flashing
	 * to 0% on visibility changes, WS reconnects, or fallback recovery.
	 */
	startStreaming(sessionId: string): void {
		if (this._timers.has(sessionId)) {
			const existing = this._timers.get(sessionId)!;
			if (existing.interval !== STREAMING_POLL_INTERVAL_MS) {
				this.stop(sessionId);
				// Fall through to create new timer
			} else {
				// Already polling at streaming rate — no-op.
				return;
			}
		}

		// Bump generation ONLY when actually starting new polling.
		// Any in-flight polls from the old generation will be discarded
		// in _applyPollResult via stale-response detection.
		const nextGen = (this._streamingGeneration.get(sessionId) ?? 0) + 1;
		this._streamingGeneration.set(sessionId, nextGen);

		if (nextGen === 1) console.debug(`[Poller] startStreaming(${sessionId}): gen=${nextGen}`);

		// Clear pending pollNow timers and in-flight flag so our fresh
		// since_update_id=0 poll can proceed immediately. Without this,
		// a pollNow() from a message_added event can block startStreaming's
		// initial poll — the _inFlight guard in _poll() would skip it.
		const pendingTimer = this._pendingPollNow.get(sessionId);
		if (pendingTimer) {
			clearTimeout(pendingTimer);
			this._pendingPollNow.delete(sessionId);
		}
		this._inFlight.delete(sessionId);

		// Prepare session state for polling.
		// If the session already has state (e.g., resuming after visibility
		// change or WS reconnect), preserve it and only reset the lastUpdateId
		// so the next poll fetches fresh data. This avoids the destructive
		// removeSession→initSession pattern that wiped contextUsed to 0,
		// causing the context usage bar to flash on every tab switch.
		const existingSession = sessionState.getSession(sessionId);
		if (existingSession) {
			sessionState.resetForPolling(sessionId);
		} else {
			sessionState.initSession(sessionId);
		}

		// Poll immediately, then on interval
		this._poll(sessionId);
		const timer = setInterval(() => this._poll(sessionId), STREAMING_POLL_INTERVAL_MS);
		this._timers.set(sessionId, {
			timer,
			sessionId,
			interval: STREAMING_POLL_INTERVAL_MS,
		});
	}

	/**
	 * Trigger an immediate poll for a session (called by WebSocket notifier).
	 * Does not start a timer — call startStreaming() for ongoing polling.
	 */
	pollNow(sessionId: string): void {
		// Debounce: coalesce rapid pollNow calls within 100ms
		if (this._pendingPollNow.has(sessionId)) return;

		const timer = setTimeout(() => {
			this._pendingPollNow.delete(sessionId);
			this._poll(sessionId);
		}, 100);
		this._pendingPollNow.set(sessionId, timer);
	}

	/**
	 * Stop polling for a session.
	 */
	stop(sessionId: string): void {
		const entry = this._timers.get(sessionId);
		if (entry) {
			clearInterval(entry.timer);
			this._timers.delete(sessionId);
		}
		this._inFlight.delete(sessionId);
		this._errorCounts.delete(sessionId);
		this._streamingGeneration.delete(sessionId);

		const pending = this._pendingPollNow.get(sessionId);
		if (pending) {
			clearTimeout(pending);
			this._pendingPollNow.delete(sessionId);
		}
	}

	/**
	 * Stop all polling (e.g., on logout or page unload).
	 */
	stopAll(): void {
		for (const [, entry] of this._timers) {
			clearInterval(entry.timer);
		}
		this._timers.clear();
		this._inFlight.clear();
		this._errorCounts.clear();
		this._streamingGeneration.clear();

		for (const timer of this._pendingPollNow.values()) {
			clearTimeout(timer);
		}
		this._pendingPollNow.clear();
	}

	/**
	 * Check if currently polling a session.
	 */
	isPolling(sessionId: string): boolean {
		return this._timers.has(sessionId);
	}

	/**
	 * Load a session's state via HTTP. This replaces the old WS-based
	 * load_session → history flow. Returns the poll data on success, or
	 * null on failure.
	 *
	 * This does NOT start polling — it's a one-shot fetch that writes to
	 * sessionState and fires onPollApplied. If the session turns out to
	 * be streaming, the caller should call startStreaming() afterwards.
	 */
	async loadSession(sessionId: string): Promise<PollAppliedData | null> {
		// loadSession is a one-shot fetch — no need for per-call logging

		// Ensure session state exists. Use initSession (which is a no-op if
		// already present) instead of the destructive removeSession+initSession
		// pattern. The old pattern wiped contextUsed → 0 before the HTTP
		// response arrived, causing a visible flash (16% → correct value) and
		// risking data mismatches if the re-fetch returned different values
		// (e.g., from JSONL compaction or live store timing). Resetting the
		// lastUpdateId to 0 forces _applyPollResult to always apply the
		// response (the fast-path only skips for lastUpdateId > 0).
		if (!sessionState.getSession(sessionId)) {
			sessionState.initSession(sessionId);
		}

		try {
			// Timeout prevents UI freeze when backend is slow or hung
			// (e.g., parsing large/corrupt JSONL, resource exhaustion)
			const controller = new AbortController();
			const timeoutId = setTimeout(() => controller.abort(), 15000);

			const response = await fetch(
				`/api/v1/sessions/${sessionId}/live?since_update_id=0`,
				{ credentials: 'include', signal: controller.signal }
			);
			clearTimeout(timeoutId);

			if (!response.ok) {
				console.error(`[Poller] loadSession(${sessionId}): HTTP ${response.status}`);
				return null;
			}

			const data = await response.json();
			// Use generation 0 — loadSession is not part of a streaming cycle,
			// so stale detection isn't needed. The generation check in
			// _applyPollResult will pass because we're at generation 0 or matching.
			const currentGen = this._streamingGeneration.get(sessionId) ?? 0;
			this._applyPollResult(sessionId, data, currentGen);

			const session = sessionState.getSession(sessionId);
			if (session) {
				const sessionMeta = data.session_meta as SessionMeta | undefined;
				return {
					messages: session.messages,
					isStreaming: session.isStreaming,
					usage: session.usage,
					todos: session.todos,
					sessionMeta,
				};
			}
			return null;
		} catch (e) {
			if (e instanceof DOMException && e.name === 'AbortError') {
				console.error(`[Poller] loadSession(${sessionId}): timed out after 15s`);
			} else {
				console.error('[Poller] loadSession error:', e);
			}
			return null;
		}
	}

	// ─── Private ─────────────────────────────────────────────────────────────

	private async _poll(sessionId: string): Promise<void> {
		if (this._inFlight.has(sessionId)) return;
		this._inFlight.add(sessionId);

		const pollGeneration = this._streamingGeneration.get(sessionId) ?? 0;

		try {
			const lastUpdateId = sessionState.getLastUpdateId(sessionId);

			const response = await fetch(
				`/api/v1/sessions/${sessionId}/live?since_update_id=${lastUpdateId}`,
				{ credentials: 'include' }
			);

			if (response.status === 304) {
				this._errorCounts.delete(sessionId);
				return;
			}

			if (!response.ok) {
				console.warn(`[Poller] _poll(${sessionId}): HTTP ${response.status}`);
				this._recordError(sessionId, new Error(`HTTP ${response.status}`));
				return;
			}

			const data = await response.json();
			this._applyPollResult(sessionId, data, pollGeneration);
			this._errorCounts.delete(sessionId);
		} catch (e) {
			console.warn(`[Poller] _poll(${sessionId}): fetch error`, e);
			this._recordError(sessionId, e);
		} finally {
			this._inFlight.delete(sessionId);
		}
	}

	private _recordError(sessionId: string, error: unknown): void {
		const count = (this._errorCounts.get(sessionId) || 0) + 1;
		this._errorCounts.set(sessionId, count);

		console.warn(`[Poller] Poll error for ${sessionId} (${count} consecutive):`, error);

		if (count >= CONSECUTIVE_ERROR_THRESHOLD && this._onPollError) {
			this._onPollError(sessionId, count, error);
		}
	}

	/**
	 * Convert server response into session state and apply it.
	 *
	 * Handles two response formats:
	 * - Full: All messages (initial_message_count = 0 or absent) — first poll
	 * - Delta: Only streaming messages (initial_message_count > 0) — subsequent polls
	 */
	private _applyPollResult(sessionId: string, data: Record<string, unknown>, pollGeneration: number): void {
		const newUpdateId = data.last_update_id as number;
		const isStreaming = data.is_streaming as boolean;
		const serverMessages = (data.messages as Array<Record<string, unknown>>) || [];
		const initialMessageCount = (data.initial_message_count as number) || 0;

		const rawMessages = serverMessages.map((m, i) => convertServerMessage(m, i));

		// Filter noise messages
		const streamingMessages = rawMessages.filter(m => {
			if (m.type === 'text' && m.role === 'assistant' && !m.content?.trim()) {
				return false;
			}
			if (m.role === 'user' && m.content && typeof m.content === 'string' && m.content.includes('<task-notification>')) {
				return false;
			}
			return true;
		});

		// Get existing state for merging and transition detection
		const existing = sessionState.getSession(sessionId);
		const existingMessages = existing?.messages;
		const wasStreaming = existing?.isStreaming ?? false;
		const streamingJustEnded = wasStreaming && !isStreaming;

		// Delta merge
		let messages: ChatMessage[];
		if (initialMessageCount > 0 && existingMessages && existingMessages.length >= initialMessageCount) {
			const historyMessages = existingMessages.slice(0, initialMessageCount);
			messages = [...historyMessages, ...streamingMessages];
		} else if (initialMessageCount > 0 && existingMessages && existingMessages.length > 0) {
			// existingMessages may already contain streaming messages from a
			// previous poll. Concatenating blindly would duplicate them.
			// Deduplicate by collecting IDs from streaming messages and only
			// keeping existing messages that don't overlap.
			const streamingIds = new Set(streamingMessages.map(m => m.id));
			const safeExisting = existingMessages.filter(m => !streamingIds.has(m.id));
			messages = [...safeExisting, ...streamingMessages];
		} else {
			messages = streamingMessages;
		}

		// Stale response detection
		const currentGeneration = this._streamingGeneration.get(sessionId) ?? 0;
		const isStaleResponse = pollGeneration < currentGeneration;

		if (isStaleResponse) {
			return;
		}

		const todos = extractTodos(messages);

		const usageRaw = (data.usage as Record<string, number>) || {};
		const usage: SessionUsage = {
			inputTokens: usageRaw.input_tokens || 0,
			outputTokens: usageRaw.output_tokens || 0,
			cacheCreationTokens: usageRaw.cache_creation_tokens || 0,
			cacheReadTokens: usageRaw.cache_read_tokens || 0,
			contextUsed:
				(usageRaw.input_tokens || 0) +
				(usageRaw.cache_creation_tokens || 0) +
				(usageRaw.cache_read_tokens || 0),
		};

		// Apply to store — ONLY place state gets written
		sessionState.applyServerState(
			sessionId,
			messages,
			isStreaming,
			newUpdateId,
			usage,
			todos,
			existingMessages
		);

		// Extract session metadata (worktree state) if present in server response
		const sessionMeta = data.session_meta as SessionMeta | undefined;

		// Direct callback to activeSession — sole path from server → UI
		if (this._onPollApplied) {
			const session = sessionState.getSession(sessionId);
			if (session) {
				this._onPollApplied(sessionId, {
					messages: session.messages,
					isStreaming: session.isStreaming,
					usage: session.usage,
					todos: session.todos,
					sessionMeta,
				});
			} else {
				console.warn(`[Poller] onPollApplied(${sessionId}): session not found in sessionState after apply`);
			}
		}

		// Handle streaming state transitions
		if (streamingJustEnded) {
			if (this._timers.has(sessionId)) {
				this.stop(sessionId);
			}
			if (this._onStreamEnd) {
				this._onStreamEnd(sessionId, usage);
			}
		} else if (!isStreaming && this._timers.has(sessionId)) {
			const entry = this._timers.get(sessionId)!;
			if (entry.interval === STREAMING_POLL_INTERVAL_MS) {
				this.stop(sessionId);
				if (this._onStreamEnd) {
					this._onStreamEnd(sessionId, usage);
				}
			}
		}
	}
}

// ─── Message conversion helpers ──────────────────────────────────────────────

function convertServerMessage(m: Record<string, unknown>, index: number): ChatMessage {
	const serverType = (m.type as MessageType) || 'text';
	const serverRole = (m.role as 'user' | 'assistant' | 'system') || 'assistant';

	const msg: ChatMessage = {
		id: (m.id as string) || `live-${index}`,
		role: serverRole,
		content: (m.content as string) || '',
		type: serverType,
		streaming: (m.streaming as boolean) || false,
	};

	if (m.toolName) msg.toolName = m.toolName as string;
	if (m.toolId) msg.toolId = m.toolId as string;
	if (m.toolInput) msg.toolInput = m.toolInput as Record<string, unknown>;
	if (m.toolResult !== undefined) msg.toolResult = m.toolResult as string;
	if (m.toolStatus) msg.toolStatus = m.toolStatus as 'running' | 'complete' | 'error';

	if (serverType === 'tool_use' && msg.streaming && msg.content) {
		msg.partialToolInput = msg.content;
	}

	if (m.agentId) msg.agentId = m.agentId as string;
	if (m.agentType) msg.agentType = m.agentType as string;
	if (m.agentDescription) msg.agentDescription = m.agentDescription as string;
	if (m.agentPrompt) msg.agentPrompt = m.agentPrompt as string;
	if (m.agentStatus) msg.agentStatus = m.agentStatus as 'pending' | 'running' | 'completed' | 'error';
	if (m.agentChildren) msg.agentChildren = m.agentChildren as SubagentChildMessage[];
	if (m.agentUsage) msg.agentUsage = m.agentUsage as ChatMessage['agentUsage'];

	if (m.systemSubtype) msg.systemSubtype = m.systemSubtype as string;
	if (m.systemData) msg.systemData = m.systemData as Record<string, unknown>;

	if (m.metadata) msg.metadata = m.metadata as Record<string, unknown>;

	return msg;
}

function extractTodos(messages: ChatMessage[]): TodoItem[] {
	for (let i = messages.length - 1; i >= 0; i--) {
		const msg = messages[i];
		if (msg.type === 'tool_use' && msg.toolName === 'TodoWrite' && msg.toolInput?.todos) {
			const todos = msg.toolInput.todos as TodoItem[];
			if (Array.isArray(todos)) {
				return todos.map(todo => ({
					content: todo.content || '',
					status: todo.status || 'pending',
					activeForm: todo.activeForm || todo.content || '',
				}));
			}
		}
	}
	return [];
}

// ─── Export singleton ────────────────────────────────────────────────────────

export const sessionPoller = new SessionPoller();
