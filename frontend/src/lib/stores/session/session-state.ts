/**
 * Session State - Compatibility shim.
 *
 * Delegates to the new $state-based SessionStateManager while exposing
 * the old writable store interface (subscribe). The bridge subscription
 * in tabs.ts and derived stores still import from this file.
 *
 * This shim will be deleted once the bridge is fully eliminated.
 */

import { writable } from 'svelte/store';
import type { ChatMessage, TodoItem } from '../types';
import { sessionStateManager } from './session-state.svelte';
import type { SessionUsage, SessionData } from './session-state.svelte';

// Re-export types so existing imports keep working
export type { SessionUsage, SessionData } from './session-state.svelte';

export interface SessionStateMap {
	/** Map of sessionId -> SessionData */
	sessions: Map<string, SessionData>;
}

// ─── Writable store wrapper ──────────────────────────────────────────────────

// Writable store that mirrors the manager's state for subscribe() consumers.
const _store = writable<SessionStateMap>({
	sessions: new Map(),
});

/** Push current manager state into the writable store. */
function _sync(): void {
	_store.set({ sessions: sessionStateManager.sessions });
}

export const sessionState = {
	subscribe: _store.subscribe,

	get current(): SessionStateMap {
		return { sessions: sessionStateManager.sessions };
	},

	getSession(sessionId: string): SessionData | undefined {
		return sessionStateManager.getSession(sessionId);
	},

	getLastUpdateId(sessionId: string): number {
		return sessionStateManager.getLastUpdateId(sessionId);
	},

	applyServerState(
		sessionId: string,
		serverMessages: ChatMessage[],
		isStreaming: boolean,
		lastUpdateId: number,
		usage: SessionUsage,
		todos: TodoItem[],
		existingMessages?: ChatMessage[]
	): void {
		const beforeId = sessionStateManager.getLastUpdateId(sessionId);
		const beforeSession = sessionStateManager.getSession(sessionId);
		sessionStateManager.applyServerState(
			sessionId, serverMessages, isStreaming, lastUpdateId, usage, todos, existingMessages
		);
		// Notify writable store subscribers when data changed.
		// Check both lastUpdateId AND session reference — for non-streaming
		// sessions the server returns lastUpdateId=0 on every response, but
		// the session data (messages, usage) still needs to propagate to
		// derived stores that subscribe to the writable store.
		const afterId = sessionStateManager.getLastUpdateId(sessionId);
		const afterSession = sessionStateManager.getSession(sessionId);
		if (afterId !== beforeId || afterSession !== beforeSession) {
			_sync();
		}
	},

	initSession(sessionId: string): void {
		sessionStateManager.initSession(sessionId);
		_sync();
	},

	removeSession(sessionId: string): void {
		sessionStateManager.removeSession(sessionId);
		_sync();
	},

	resetForPolling(sessionId: string): void {
		sessionStateManager.resetForPolling(sessionId);
		_sync();
	},

	markStreamingEnded(sessionId: string): void {
		sessionStateManager.markStreamingEnded(sessionId);
		_sync();
	},
};
