/**
 * Active Usage Derived Store
 *
 * Provides token usage for the currently active session.
 * Uses sessionState when available, falls back to tabs store.
 */

import { derived } from 'svelte/store';
import { tabs } from '../tabs';
import { sessionState, type SessionUsage } from '../session/session-state';

/** Read from session state (new store architecture) */
const USE_NEW_STORES = true;

const EMPTY_USAGE: SessionUsage = {
	inputTokens: 0,
	outputTokens: 0,
	cacheCreationTokens: 0,
	cacheReadTokens: 0,
	contextUsed: 0,
};

/**
 * Token usage for the currently active session.
 */
export const activeUsage = derived<[typeof tabs, typeof sessionState], SessionUsage>(
	[tabs, sessionState],
	([$tabs, $sessionState]) => {
		const tab = $tabs.tabs.find(t => t.id === $tabs.activeTabId);
		if (!tab) return EMPTY_USAGE;

		if (USE_NEW_STORES && tab.sessionId) {
			const session = $sessionState.sessions.get(tab.sessionId);
			if (session && session.usage.contextUsed > 0) {
				return session.usage;
			}
		}

		// Fallback to tabs store
		return {
			inputTokens: tab.totalTokensIn,
			outputTokens: tab.totalTokensOut,
			cacheCreationTokens: tab.totalCacheCreationTokens,
			cacheReadTokens: tab.totalCacheReadTokens,
			contextUsed: tab.contextUsed ?? 0,
		};
	}
);
