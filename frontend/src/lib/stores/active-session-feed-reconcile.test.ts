/**
 * Feed-reconcile self-heal decision (`shouldStartFeedReconcile`).
 *
 * The reconcile watchdog restarts the SSE feed when a session is open but its
 * feed is not feeding (the transcript would otherwise stay permanently stale
 * until the user sends a message). The decision is a pure predicate so it can be
 * pinned without spinning the whole store: start iff a session is open AND its
 * feed is not currently feeding; never start for a null/empty sessionId
 * (New Chat / cleared) or a session already being fed.
 *
 * `active-session.ts` constructs its WS connection at import, so — mirroring
 * active-session.test.ts — we mock ws-connection to keep import side-effect-free.
 */

import { describe, it, expect, vi } from 'vitest';

vi.mock('./session/ws-connection', () => ({
	createWsConnection: () => ({
		connect: vi.fn(),
		disconnect: vi.fn(),
		send: vi.fn(() => true),
		isConnected: vi.fn(() => false),
		resetLastMessageTime: vi.fn(),
		resetReconnectAttempts: vi.fn(),
		destroy: vi.fn(),
	}),
}));

import { shouldStartFeedReconcile } from './active-session';

describe('shouldStartFeedReconcile (feed-reconcile self-heal)', () => {
	it('returns true when a session is open and its feed is NOT feeding', () => {
		const isFeeding = vi.fn(() => false);
		expect(shouldStartFeedReconcile('sess-1', isFeeding)).toBe(true);
		expect(isFeeding).toHaveBeenCalledWith('sess-1');
	});

	it('returns false when the open session is already being fed (healthy)', () => {
		const isFeeding = vi.fn(() => true);
		expect(shouldStartFeedReconcile('sess-1', isFeeding)).toBe(false);
		expect(isFeeding).toHaveBeenCalledWith('sess-1');
	});

	it('returns false — and never probes the feed — when no session is open', () => {
		const isFeeding = vi.fn(() => false);
		expect(shouldStartFeedReconcile(null, isFeeding)).toBe(false);
		expect(shouldStartFeedReconcile('', isFeeding)).toBe(false);
		expect(isFeeding).not.toHaveBeenCalled();
	});
});
