/**
 * Session Poller - Polls /sessions/{id}/live for server-authoritative state.
 *
 * This is the ONLY module that writes to session-state. Polling is primary,
 * not a fallback. WebSocket events just trigger an immediate poll via pollNow().
 *
 * Polling rates:
 * - 300ms during streaming (real-time feel)
 * - 30s when idle (detects remote session starts)
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
const IDLE_POLL_INTERVAL_MS = 30_000;

// ─── Types ───────────────────────────────────────────────────────────────────

interface PollTimer {
	timer: ReturnType<typeof setInterval>;
	sessionId: string;
	interval: number;
}

type StreamEndCallback = (sessionId: string, usage: SessionUsage) => void;
type PollErrorCallback = (sessionId: string, consecutiveErrors: number, error: unknown) => void;

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

	/** Callback when streaming ends */
	private _onStreamEnd: StreamEndCallback | null = null;

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
	 * Register a callback for poll errors.
	 * Called when consecutive errors reach the threshold (3 by default).
	 * Used to show error indicators in the UI.
	 */
	onPollError(callback: PollErrorCallback): void {
		this._onPollError = callback;
	}

	/**
	 * Start polling for a session at the streaming rate.
	 * If already polling this session, this is a no-op.
	 */
	startStreaming(sessionId: string): void {
		// Bump generation FIRST — any in-flight or debounced polls from a
		// previous streaming session will see a stale generation and skip
		// their streamingJustEnded transition.
		const nextGen = (this._streamingGeneration.get(sessionId) ?? 0) + 1;
		this._streamingGeneration.set(sessionId, nextGen);

		if (this._timers.has(sessionId)) {
			// Already polling - maybe speed up if was idle
			const existing = this._timers.get(sessionId)!;
			if (existing.interval !== STREAMING_POLL_INTERVAL_MS) {
				this.stop(sessionId);
				// Fall through to create new timer
			} else {
				return;
			}
		}

		// Reset session state for the new streaming session.
		// Without this, a stale lastUpdateId from a previous query causes
		// all polls to get HTTP 304 (backend's new session starts at
		// last_update_id=1, but frontend sends since_update_id=50+).
		// removeSession + initSession resets lastUpdateId to 0 while
		// preserving a clean session entry. Messages are restored by
		// the first successful poll.
		sessionState.removeSession(sessionId);
		sessionState.initSession(sessionId);

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
	 * Start idle polling for a session (infrequent, for detecting remote starts).
	 */
	startIdle(sessionId: string): void {
		if (this._timers.has(sessionId)) return;

		sessionState.initSession(sessionId);

		const timer = setInterval(() => this._poll(sessionId), IDLE_POLL_INTERVAL_MS);
		this._timers.set(sessionId, {
			timer,
			sessionId,
			interval: IDLE_POLL_INTERVAL_MS,
		});
	}

	/**
	 * Trigger an immediate poll for a session (called by WebSocket notifier).
	 * Does not start a timer - call startStreaming() for ongoing polling.
	 */
	pollNow(sessionId: string): void {
		// Debounce: coalesce rapid pollNow calls within 100ms
		// This prevents stale reads when WebSocket event arrives before backend writes state
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
		// NOTE: Do NOT clear _streamingGeneration here. The generation must
		// persist across stop/start cycles so that stale poll responses from
		// a stopped session are still detected as stale when a new session starts.

		// Clear any pending debounced poll
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

		// Clear all pending debounced polls
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

	// ─── Private ─────────────────────────────────────────────────────────────

	private async _poll(sessionId: string): Promise<void> {
		// Prevent overlapping requests for the same session
		if (this._inFlight.has(sessionId)) return;
		this._inFlight.add(sessionId);

		// Capture the streaming generation BEFORE the async fetch.
		// If startStreaming() is called while this fetch is in-flight,
		// the generation will advance and _applyPollResult will know
		// this response is stale (belongs to a previous streaming session).
		const pollGeneration = this._streamingGeneration.get(sessionId) ?? 0;

		try {
			const lastUpdateId = sessionState.getLastUpdateId(sessionId);

			const response = await fetch(
				`/api/v1/sessions/${sessionId}/live?since_update_id=${lastUpdateId}`,
				{ credentials: 'include' }
			);

			if (response.status === 304) {
				// No updates since last check - nothing to do
				this._errorCounts.delete(sessionId); // Reset on success
				return;
			}

			if (!response.ok) {
				this._recordError(sessionId, new Error(`HTTP ${response.status}`));
				return;
			}

			const data = await response.json();
			this._applyPollResult(sessionId, data, pollGeneration);
			// Reset error counter on successful poll
			this._errorCounts.delete(sessionId);
		} catch (e) {
			this._recordError(sessionId, e);
		} finally {
			this._inFlight.delete(sessionId);
		}
	}

	/**
	 * Record a poll error and notify callback if threshold reached.
	 */
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
	 *   The backend skips initial history messages the frontend already has,
	 *   dramatically reducing payload for long sessions near auto-compaction.
	 */
	private _applyPollResult(sessionId: string, data: Record<string, unknown>, pollGeneration: number): void {
		const newUpdateId = data.last_update_id as number;
		const isStreaming = data.is_streaming as boolean;
		const serverMessages = (data.messages as Array<Record<string, unknown>>) || [];
		const initialMessageCount = (data.initial_message_count as number) || 0;

		// Convert server messages to ChatMessage format
		const rawMessages = serverMessages.map((m, i) => convertServerMessage(m, i));

		// Filter out noise messages that shouldn't render in the chat UI.
		const streamingMessages = rawMessages.filter(m => {
			// Whitespace-only assistant text: SDK emits "\n\n" before thinking blocks.
			// Backend's end_streaming() filters these at stream end, but during streaming
			// they leak through and cause empty "Claude [time]" headers to flash.
			if (m.type === 'text' && m.role === 'assistant' && !m.content?.trim()) {
				return false;
			}
			// SDK task-notification: async completion callbacks for background agents.
			// These are internal SDK plumbing (already displayed via SubagentMessage);
			// the backend JSONL parser filters them, but guard the frontend too.
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

		// If this is a delta response (initial_message_count > 0), merge with existing history.
		// The backend only sent streaming messages; we prepend the history we already have.
		let messages: ChatMessage[];
		if (initialMessageCount > 0 && existingMessages && existingMessages.length >= initialMessageCount) {
			// Keep existing history messages, replace only streaming portion
			const historyMessages = existingMessages.slice(0, initialMessageCount);
			messages = [...historyMessages, ...streamingMessages];
		} else if (initialMessageCount > 0 && existingMessages && existingMessages.length > 0) {
			// Boundary mismatch (existingMessages.length < initialMessageCount).
			// This can happen during reconnect races or if initial_message_count
			// was advanced beyond what the frontend has seen. Preserve existing
			// messages and append new streaming messages to avoid dropping
			// conversation history, which would cause massive DOM recreation
			// and risk corrupting Svelte's effect linked list.
			messages = [...existingMessages, ...streamingMessages];
		} else {
			// Full response or first poll — use as-is
			messages = streamingMessages;
		}

		// Check if a new streaming session was started since this poll began.
		// If so, this response is STALE — its data belongs to a previous query
		// and must NOT be written to sessionState (which would trigger the bridge
		// to overwrite the store's optimistic user message with old data).
		const currentGeneration = this._streamingGeneration.get(sessionId) ?? 0;
		const isStaleResponse = pollGeneration < currentGeneration;

		if (isStaleResponse) {
			// Stale poll response from a previous streaming session.
			// Do NOT write to sessionState, stop the poller, or fire onStreamEnd.
			// The new streaming session's polls will provide fresh data.
			console.debug(`[Poller] Discarding stale poll response for ${sessionId} (gen ${pollGeneration} < ${currentGeneration})`);
			return;
		}

		// Extract todos from all messages (including history for TodoWrite persistence)
		const todos = extractTodos(messages);

		// Extract usage
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

		// Apply to store - this is the ONLY place state gets written
		sessionState.applyServerState(
			sessionId,
			messages,
			isStreaming,
			newUpdateId,
			usage,
			todos,
			existingMessages
		);

		// Handle streaming state transitions
		if (streamingJustEnded) {
			// Streaming→not-streaming transition detected.
			// Stop timer if still running, and always fire onStreamEnd for cleanup.
			if (this._timers.has(sessionId)) {
				this.stop(sessionId);
			}
			if (this._onStreamEnd) {
				// Defer onStreamEnd to the next macrotask via setTimeout(0).
				// This gives Svelte a full task boundary to finish reconciling the
				// DOM from the streaming bridge's _update() (triggered by
				// applyServerState → _sync()) before onStreamEnd fires a second
				// _update(). Without this separation, two store updates in the
				// same microtask flush corrupt Svelte 5's effect linked-list
				// (TypeError: Cannot read 'prev' of undefined), freezing the UI.
				//
				// Previously used queueMicrotask, but microtasks run before
				// Svelte's DOM reconciliation completes — they share the same
				// microtask checkpoint, so the double-update still collides.
				const cb = this._onStreamEnd;
				setTimeout(() => cb(sessionId, usage), 0);
			}
		} else if (!isStreaming && this._timers.has(sessionId)) {
			// Non-transition case: poll returns !isStreaming but session wasn't previously
			// marked as streaming (e.g., idle poll detecting a finished session).
			const entry = this._timers.get(sessionId)!;
			if (entry.interval === STREAMING_POLL_INTERVAL_MS) {
				this.stop(sessionId);
				if (this._onStreamEnd) {
					// Defer for same reason as streamingJustEnded (avoid double-update)
					const cb = this._onStreamEnd;
					setTimeout(() => cb(sessionId, usage), 0);
				}
			}
		}
	}
}

// ─── Message conversion helpers ──────────────────────────────────────────────

/**
 * Convert a server message object to a ChatMessage.
 */
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

	// Tool fields
	if (m.toolName) msg.toolName = m.toolName as string;
	if (m.toolId) msg.toolId = m.toolId as string;
	if (m.toolInput) msg.toolInput = m.toolInput as Record<string, unknown>;
	if (m.toolResult !== undefined) msg.toolResult = m.toolResult as string;
	if (m.toolStatus) msg.toolStatus = m.toolStatus as 'running' | 'complete' | 'error';

	// For streaming tool_use messages, the content field contains accumulated
	// partial JSON from input_json_delta events (via live_session_store's
	// update_tool_input_streaming). Map it to partialToolInput so
	// ToolUseMessage can render input fields incrementally as they stream in.
	if (serverType === 'tool_use' && msg.streaming && msg.content) {
		msg.partialToolInput = msg.content;
	}

	// Subagent fields
	if (m.agentId) msg.agentId = m.agentId as string;
	if (m.agentType) msg.agentType = m.agentType as string;
	if (m.agentDescription) msg.agentDescription = m.agentDescription as string;
	if (m.agentPrompt) msg.agentPrompt = m.agentPrompt as string;
	if (m.agentStatus) msg.agentStatus = m.agentStatus as 'pending' | 'running' | 'completed' | 'error';
	if (m.agentChildren) msg.agentChildren = m.agentChildren as SubagentChildMessage[];

	// System message fields
	if (m.systemSubtype) msg.systemSubtype = m.systemSubtype as string;
	if (m.systemData) msg.systemData = m.systemData as Record<string, unknown>;

	// Metadata
	if (m.metadata) msg.metadata = m.metadata as Record<string, unknown>;

	return msg;
}

/**
 * Extract the latest TodoWrite items from messages.
 * Scans backwards since TodoWrite always replaces the full list —
 * only the last occurrence matters.
 */
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
