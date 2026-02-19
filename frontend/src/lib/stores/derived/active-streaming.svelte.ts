/**
 * Active Streaming Derived Store (Svelte 5 Runes)
 *
 * Provides streaming state for the currently active session and workspace.
 * Reads from activeSession store, sessionState, and streamingSessions.
 *
 * Uses createMultiDerived to subscribe to multiple Svelte 4 stores and
 * expose a runes-compatible interface (.current + .subscribe).
 */

import { get } from 'svelte/store';
import { activeSession, streamingSessions } from '../active-session';
import { sessionState } from '../session/session-state';

// ─── Multi-Store Derived Helper ──────────────────────────────────────────────

type Subscriber<T> = (value: T) => void;

/**
 * Create a derived store that depends on multiple parent stores.
 * Each parent must implement subscribe(fn: (value: T) => void): () => void.
 *
 * When any parent changes, the getter is re-evaluated and all subscribers
 * are notified with the new value.
 */
function createMultiDerived<T>(
	parents: Array<{ subscribe: (fn: (v: any) => void) => () => void }>,
	getter: () => T
): { readonly current: T; subscribe: (fn: Subscriber<T>) => () => void } {
	const subscribers = new Set<Subscriber<T>>();
	let initialized = false;

	function setup() {
		if (initialized) return;
		initialized = true;
		for (const parent of parents) {
			parent.subscribe(() => {
				const value = getter();
				for (const fn of subscribers) fn(value);
			});
		}
	}

	return {
		get current() {
			return getter();
		},
		subscribe(fn: Subscriber<T>) {
			setup();
			subscribers.add(fn);
			fn(getter());
			return () => {
				subscribers.delete(fn);
			};
		}
	};
}

// ─── Derived Stores ──────────────────────────────────────────────────────────

/**
 * Whether the active session is currently streaming.
 */
export const activeIsStreaming = createMultiDerived(
	[activeSession, sessionState],
	() => {
		const $activeSession = get(activeSession);
		const $sessionState = get(sessionState);

		// Store-level isStreaming is set immediately when user sends a message
		if ($activeSession.isStreaming) return true;

		if ($activeSession.sessionId) {
			const session = $sessionState.sessions.get($activeSession.sessionId);
			if (session?.isStreaming) return true;
		}

		return false;
	}
);

/**
 * Whether any session in the active session's workspace (project) is currently streaming.
 * Used to lock git controls so the workspace branch can't change during streaming.
 */
export const workspaceIsStreaming = createMultiDerived(
	[activeSession, sessionState, streamingSessions],
	() => {
		const $activeSession = get(activeSession);
		const $sessionState = get(sessionState);
		const $streamingSessions = get(streamingSessions);

		// Active session is streaming
		if ($activeSession.isStreaming) return true;
		if ($activeSession.sessionId) {
			const session = $sessionState.sessions.get($activeSession.sessionId);
			if (session?.isStreaming) return true;
		}

		// Any session in the same project is streaming (cross-device)
		if ($activeSession.project) {
			for (const [, info] of $streamingSessions) {
				if (info.projectId === $activeSession.project) return true;
			}
		}

		return false;
	}
);

/**
 * Whether git controls should be locked due to streaming.
 *
 * For worktree-mode: only locked if the active session's OWN session is streaming.
 * For project-mode: locked if ANY non-worktree session in the same project is streaming.
 */
export const gitControlsStreaming = createMultiDerived(
	[activeSession, sessionState, streamingSessions],
	() => {
		const $activeSession = get(activeSession);
		const $sessionState = get(sessionState);
		const $streamingSessions = get(streamingSessions);

		// Active session is streaming (always relevant)
		if ($activeSession.isStreaming) return true;
		if ($activeSession.sessionId) {
			const session = $sessionState.sessions.get($activeSession.sessionId);
			if (session?.isStreaming) return true;
		}

		// Worktree-mode has independent working directory
		if ($activeSession.executionMode === 'worktree') return false;

		// Project-mode: check cross-session streaming, skip worktree sessions
		if ($activeSession.project) {
			for (const [, info] of $streamingSessions) {
				if (info.projectId === $activeSession.project && !info.worktreeId) return true;
			}
		}

		return false;
	}
);

/**
 * Whether the active session's WebSocket is connected.
 * Single-parent derivation — uses createMultiDerived with one parent for consistency.
 */
export const activeWsConnected = createMultiDerived(
	[activeSession],
	() => {
		const $activeSession = get(activeSession);
		return $activeSession.wsConnected;
	}
);
