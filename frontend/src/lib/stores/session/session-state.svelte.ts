/**
 * Session State (Svelte 5 Runes) - Reactive store for session data.
 *
 * This is the single source of truth for session state during streaming.
 * ONLY the session-poller writes to this store. The tabs getter reads
 * from it on demand — no bridge subscription needed.
 *
 * Key principles:
 * 1. One writer (poller) - no race conditions
 * 2. Server-authoritative - backend's live_session_store is ground truth
 * 3. Fine-grained reactivity via $state - only affected components re-render
 */

import type { ChatMessage, TodoItem } from '../types';

// ─── Types ───────────────────────────────────────────────────────────────────

export interface SessionUsage {
	inputTokens: number;
	outputTokens: number;
	cacheCreationTokens: number;
	cacheReadTokens: number;
	/** Computed: inputTokens + cacheCreationTokens + cacheReadTokens */
	contextUsed: number;
}

export interface SessionData {
	sessionId: string;
	messages: ChatMessage[];
	isStreaming: boolean;
	lastUpdateId: number;
	usage: SessionUsage;
	todos: TodoItem[];
}

// ─── Default values ──────────────────────────────────────────────────────────

const EMPTY_USAGE: SessionUsage = {
	inputTokens: 0,
	outputTokens: 0,
	cacheCreationTokens: 0,
	cacheReadTokens: 0,
	contextUsed: 0,
};

function createEmptySession(sessionId: string): SessionData {
	return {
		sessionId,
		messages: [],
		isStreaming: false,
		lastUpdateId: 0,
		usage: { ...EMPTY_USAGE },
		todos: [],
	};
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

/**
 * Compare a single message pair and return the best reference to use.
 * Returns the existing message if nothing changed (same object reference),
 * a spread with preserved ID if content changed, or the incoming message if new.
 */
function compareMessage(incoming: ChatMessage, existing: ChatMessage | undefined): ChatMessage {
	if (
		!existing ||
		existing.type !== incoming.type ||
		existing.role !== incoming.role
	) {
		return incoming;
	}

	// Subagent messages: their agentChildren array changes frequently
	// during execution while content/streaming stay the same.
	if (incoming.type === 'subagent') {
		if (
			existing.content === incoming.content &&
			existing.streaming === incoming.streaming &&
			existing.agentStatus === incoming.agentStatus &&
			existing.agentChildren?.length === incoming.agentChildren?.length
		) {
			const existLast = existing.agentChildren?.[existing.agentChildren.length - 1];
			const incomLast = incoming.agentChildren?.[incoming.agentChildren.length - 1];
			if (
				existLast?.content === incomLast?.content &&
				existLast?.toolResult === incomLast?.toolResult &&
				existLast?.toolStatus === incomLast?.toolStatus
			) {
				return existing;
			}
		}
		return { ...incoming, id: existing.id };
	}

	// If content, streaming status, and key fields are identical,
	// reuse the entire existing object to prevent re-renders
	if (
		existing.content === incoming.content &&
		existing.streaming === incoming.streaming &&
		existing.toolResult === incoming.toolResult &&
		existing.toolStatus === incoming.toolStatus
	) {
		return existing;
	}
	// Content changed but same message — preserve ID for DOM stability
	return { ...incoming, id: existing.id };
}

/**
 * Preserve existing message objects where nothing has changed.
 * This prevents Svelte from destroying and recreating DOM elements (which
 * would reset expanded/collapsed ThinkingBlock state, scroll position, etc).
 *
 * For messages whose content/streaming state hasn't changed, we reuse the
 * exact same object reference — Svelte's fine-grained reactivity then skips
 * re-rendering those components entirely.
 *
 * Optimized: during streaming, typically only the last 1-3 messages change.
 * We scan backwards to find the first changed message and reuse the rest.
 */
function preserveMessageIds(
	incoming: ChatMessage[],
	existing: ChatMessage[]
): ChatMessage[] {
	// Fast path: same length arrays (common during streaming text append)
	if (incoming.length === existing.length && incoming.length > 0) {
		// Scan backwards to find the first message that differs
		let firstChangedFromEnd = -1;
		for (let i = incoming.length - 1; i >= 0; i--) {
			const result = compareMessage(incoming[i], existing[i]);
			if (result !== existing[i]) {
				firstChangedFromEnd = i;
				break;
			}
		}

		// Nothing changed — return the exact same array reference
		if (firstChangedFromEnd === -1) return existing;

		// Only the tail changed — reuse the unchanged prefix
		const result = existing.slice(0, firstChangedFromEnd);
		for (let i = firstChangedFromEnd; i < incoming.length; i++) {
			result.push(compareMessage(incoming[i], existing[i]));
		}
		return result;
	}

	// Different lengths: full comparison (new messages added or removed)
	return incoming.map((msg, i) => compareMessage(msg, existing[i]));
}

// ─── Session State Manager ($state-based) ────────────────────────────────────

class SessionStateManager {
	/**
	 * Reactive Map of sessionId → SessionData.
	 * Svelte 5 proxies Map operations, so .set()/.delete() trigger updates.
	 */
	sessions = $state(new Map<string, SessionData>());

	/**
	 * Get session data. Reactive when called from $derived/$effect contexts.
	 */
	getSession(sessionId: string): SessionData | undefined {
		return this.sessions.get(sessionId);
	}

	/**
	 * Get the last update ID for a session (used by poller for ?since_update_id).
	 * Non-reactive snapshot for imperative code.
	 */
	getLastUpdateId(sessionId: string): number {
		return this.sessions.get(sessionId)?.lastUpdateId ?? 0;
	}

	/**
	 * Apply a full server state replacement from polling /sessions/{id}/live.
	 *
	 * This is the ONLY function that should write session data (besides init/cleanup).
	 * The server response is authoritative - we replace, never merge.
	 */
	applyServerState(
		sessionId: string,
		serverMessages: ChatMessage[],
		isStreaming: boolean,
		lastUpdateId: number,
		usage: SessionUsage,
		todos: TodoItem[],
		existingMessages?: ChatMessage[]
	): void {
		const existing = this.sessions.get(sessionId);

		// Fast path: if updateId hasn't changed AND is non-zero, nothing to do.
		// When lastUpdateId === 0 (non-streaming session from /live endpoint),
		// always apply — otherwise the session stays at the initSession() defaults
		// and onPollApplied fires with stale empty data.
		if (existing && lastUpdateId > 0 && existing.lastUpdateId === lastUpdateId) return;

		// Preserve existing message IDs where possible to prevent
		// Svelte component recreation (keeps expanded/collapsed state stable).
		// Wrapped in try-catch: during auto-compaction the message array can
		// change shape rapidly and the comparison may hit an edge case that
		// throws RangeError. Falling back to the raw server messages is safe —
		// it just causes a one-time DOM recreation.
		let messagesWithStableIds: ChatMessage[];
		try {
			messagesWithStableIds = existingMessages
				? preserveMessageIds(serverMessages, existingMessages)
				: serverMessages;
		} catch {
			messagesWithStableIds = serverMessages;
		}

		this.sessions.set(sessionId, {
			sessionId,
			messages: messagesWithStableIds,
			isStreaming,
			lastUpdateId,
			usage,
			// Always use server-provided todos. If the server returns an empty
			// list, that means todos were cleared — don't preserve stale ones.
			todos,
		});
	}

	/**
	 * Initialize a session entry (e.g., when a tab loads a session).
	 * Does NOT overwrite if session already exists with data.
	 */
	initSession(sessionId: string): void {
		if (this.sessions.has(sessionId)) return;
		this.sessions.set(sessionId, createEmptySession(sessionId));
	}

	/**
	 * Remove a session from the store (e.g., when tab closes).
	 */
	removeSession(sessionId: string): void {
		this.sessions.delete(sessionId);
	}

	/**
	 * Reset a session's lastUpdateId to 0, forcing the next poll to fetch
	 * fresh data from the server. Unlike removeSession() + initSession(),
	 * this preserves existing session data (messages, usage, todos) so
	 * there's no visible flash of stale/zero values in the UI.
	 *
	 * Used by startStreaming() when restarting polling for an existing
	 * session (visibility change, WS reconnect, fallback recovery).
	 */
	resetForPolling(sessionId: string): void {
		const existing = this.sessions.get(sessionId);
		if (!existing) return;
		this.sessions.set(sessionId, {
			...existing,
			lastUpdateId: 0,
		});
	}

	/**
	 * Mark a session as no longer streaming (e.g., on 'done' event).
	 * The poller will confirm this on next poll, but we update eagerly
	 * for responsive UI.
	 */
	markStreamingEnded(sessionId: string): void {
		const existing = this.sessions.get(sessionId);
		if (!existing || !existing.isStreaming) return;

		this.sessions.set(sessionId, {
			...existing,
			isStreaming: false,
			messages: existing.messages.map(m =>
				m.streaming ? { ...m, streaming: false } : m
			),
		});
	}
}

// ─── Export singleton ────────────────────────────────────────────────────────

export const sessionStateManager = new SessionStateManager();
