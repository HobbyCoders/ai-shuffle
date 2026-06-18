/**
 * Active Streaming Derived Store (Svelte 5 Runes)
 *
 * Provides streaming state for the currently active session and workspace.
 * Reads from activeSession store, sessionState, and streamingSessions.
 *
 * Uses createMultiDerived to subscribe to multiple Svelte 4 stores and
 * expose a runes-compatible interface (.current + .subscribe).
 */

import { untrack } from 'svelte';
import { activeSession, streamingSessions } from '../active-session';
import { sessionState } from '../session/session-state';
import { computeGitControlsStreaming } from './git-controls-streaming';

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
	let parentUnsubs: Array<() => void> = [];

	function subscribeToParents() {
		if (parentUnsubs.length > 0) return; // already subscribed
		parentUnsubs = parents.map((parent) =>
			parent.subscribe(() => {
				untrack(() => {
					const value = getter();
					for (const fn of subscribers) fn(value);
				});
			})
		);
	}

	function unsubscribeFromParents() {
		for (const unsub of parentUnsubs) unsub();
		parentUnsubs = [];
	}

	return {
		get current() {
			return getter();
		},
		subscribe(fn: Subscriber<T>) {
			subscribeToParents();
			subscribers.add(fn);
			untrack(() => fn(getter()));
			return () => {
				subscribers.delete(fn);
				if (subscribers.size === 0) {
					unsubscribeFromParents();
				}
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
		const active = activeSession.current;
		const state = sessionState.current;

		// Store-level isStreaming is set immediately when user sends a message
		if (active.isStreaming) return true;

		if (active.sessionId) {
			const session = state.sessions.get(active.sessionId);
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
		const active = activeSession.current;
		const state = sessionState.current;
		const streaming = streamingSessions.current;

		// Active session is streaming
		if (active.isStreaming) return true;
		if (active.sessionId) {
			const session = state.sessions.get(active.sessionId);
			if (session?.isStreaming) return true;
		}

		// Any session in the same project is streaming (cross-device)
		if (active.project) {
			for (const [, info] of streaming) {
				if (info.projectId === active.project) return true;
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
		const active = activeSession.current;
		// Pure decision logic lives in ./git-controls-streaming so it can be
		// unit-tested without the store plumbing (see git-controls-streaming.test.ts).
		return computeGitControlsStreaming({
			activeIsStreaming: active.isStreaming,
			activeSessionId: active.sessionId,
			activeExecutionMode: active.executionMode,
			activeProject: active.project || null,
			sessionStreaming: sessionState.current.sessions,
			crossDeviceStreaming: streamingSessions.current
		});
	}
);

/**
 * Whether the active session's WebSocket is connected.
 * Single-parent derivation — uses createMultiDerived with one parent for consistency.
 */
export const activeWsConnected = createMultiDerived(
	[activeSession],
	() => {
		const active = activeSession.current;
		return active.wsConnected;
	}
);
