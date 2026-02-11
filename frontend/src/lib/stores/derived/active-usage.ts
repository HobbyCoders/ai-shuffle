/**
 * Active Usage Derived Store
 *
 * Provides token usage for the currently active session.
 * Uses sessionState when available, falls back to activeSession store.
 */

import { derived } from 'svelte/store';
import { activeSession } from '../active-session';
import { sessionState, type SessionUsage } from '../session/session-state';

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
export const activeUsage = derived<[typeof activeSession, typeof sessionState], SessionUsage>(
	[activeSession, sessionState],
	([$activeSession, $sessionState]) => {
		if ($activeSession.sessionId) {
			const session = $sessionState.sessions.get($activeSession.sessionId);
			if (session && session.usage.contextUsed > 0) {
				return session.usage;
			}
		}

		// Fallback to activeSession store values
		return {
			inputTokens: $activeSession.totalTokensIn,
			outputTokens: $activeSession.totalTokensOut,
			cacheCreationTokens: $activeSession.totalCacheCreationTokens,
			cacheReadTokens: $activeSession.totalCacheReadTokens,
			contextUsed: $activeSession.contextUsed ?? 0,
		};
	}
);
