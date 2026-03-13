/**
 * Active Usage Derived Store (Svelte 5 Runes)
 *
 * Provides token usage for the currently active session.
 * Uses sessionState when available, falls back to activeSession store.
 *
 * Uses createMultiDerived to subscribe to multiple Svelte 4 stores and
 * expose a runes-compatible interface (.current + .subscribe).
 */

import { untrack } from 'svelte';
import { activeSession } from '../active-session';
import { sessionState } from '../session/session-state';
import type { SessionUsage } from '../session/session-state.svelte';

// ─── Multi-Store Derived Helper ──────────────────────────────────────────────

type Subscriber<T> = (value: T) => void;

/**
 * Create a derived store that depends on multiple parent stores.
 * Each parent must implement subscribe(fn: (value: T) => void): () => void.
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

// ─── Constants ───────────────────────────────────────────────────────────────

const EMPTY_USAGE: SessionUsage = {
	inputTokens: 0,
	outputTokens: 0,
	cacheCreationTokens: 0,
	cacheReadTokens: 0,
	contextUsed: 0,
};

// ─── Derived Store ───────────────────────────────────────────────────────────

/**
 * Token usage for the currently active session.
 */
export const activeUsage = createMultiDerived<SessionUsage>(
	[activeSession, sessionState],
	() => {
		const active = activeSession.current;
		const state = sessionState.current;

		if (active.sessionId) {
			const session = state.sessions.get(active.sessionId);
			if (session && session.usage.contextUsed > 0) {
				return session.usage;
			}
		}

		// Fallback to activeSession store values
		return {
			inputTokens: active.totalTokensIn,
			outputTokens: active.totalTokensOut,
			cacheCreationTokens: active.totalCacheCreationTokens,
			cacheReadTokens: active.totalCacheReadTokens,
			contextUsed: active.contextUsed ?? 0,
		};
	}
);
