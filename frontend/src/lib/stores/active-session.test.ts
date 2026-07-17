/**
 * active-session store — `resolveInteractionByRequestId` (#606 item 4).
 *
 * The method drops a permission / deferred-permission / question card by
 * request_id so a card seeded from the legacy `get_pending_*` relay settles when
 * a durable interaction.answered / interaction.expired event says it was resolved
 * (possibly on another device). These tests seed the real singleton through its
 * WebSocket message handler (the only public path that writes those queues) by
 * mocking ws-connection to capture the injected `onMessage`, then assert the
 * filtering + the cheap no-op behaviour.
 */

import { describe, it, expect, beforeEach, vi } from 'vitest';

// Capture the onMessage callback active-session injects into its WS connection so
// the test can drive the real message handlers to seed pending queues.
const hoisted = vi.hoisted(() => ({
	onMessage: null as ((data: Record<string, unknown>) => void) | null,
}));

vi.mock('./session/ws-connection', () => ({
	createWsConnection: (config: { onMessage: (data: Record<string, unknown>) => void }) => {
		hoisted.onMessage = config.onMessage;
		return {
			connect: vi.fn(),
			disconnect: vi.fn(),
			send: vi.fn(() => true),
			isConnected: vi.fn(() => false),
			resetLastMessageTime: vi.fn(),
			resetReconnectAttempts: vi.fn(),
			destroy: vi.fn(),
		};
	},
}));

import { activeSession } from './active-session';

/** Seed the active queues via the real `pending_permissions` handler (REPLACES). */
function seedPermissions(pending: string[], deferred: string[] = []): void {
	hoisted.onMessage!({
		type: 'pending_permissions',
		requests: pending.map((request_id) => ({ request_id })),
		deferred: deferred.map((request_id) => ({ request_id })),
	});
}

/** Seed the active question queue via the real `pending_questions` handler. */
function seedQuestions(pending: string[]): void {
	hoisted.onMessage!({
		type: 'pending_questions',
		questions: pending.map((request_id) => ({ request_id })),
	});
}

const ids = (arr: Array<{ request_id: string }>) => arr.map((x) => x.request_id);

describe('activeSession.resolveInteractionByRequestId (#606 item 4)', () => {
	beforeEach(() => {
		// Reset the singleton's queues to a known-empty baseline before each test
		// (the handler REPLACES, so empty lists clear any prior seeding).
		seedPermissions([], []);
		seedQuestions([]);
	});

	it('removes a matching pending permission, leaving non-matching entries', () => {
		seedPermissions(['perm-1', 'perm-2']);
		expect(ids(activeSession.current.pendingPermissions)).toEqual(['perm-1', 'perm-2']);

		activeSession.resolveInteractionByRequestId('perm-1');

		expect(ids(activeSession.current.pendingPermissions)).toEqual(['perm-2']);
	});

	it('removes a matching deferred permission, leaving non-matching entries', () => {
		seedPermissions([], ['def-1', 'def-2']);
		expect(ids(activeSession.current.deferredPermissions)).toEqual(['def-1', 'def-2']);

		activeSession.resolveInteractionByRequestId('def-2');

		expect(ids(activeSession.current.deferredPermissions)).toEqual(['def-1']);
	});

	it('removes a matching pending question, leaving non-matching entries', () => {
		seedQuestions(['q-1', 'q-2']);
		expect(ids(activeSession.current.pendingQuestions)).toEqual(['q-1', 'q-2']);

		activeSession.resolveInteractionByRequestId('q-1');

		expect(ids(activeSession.current.pendingQuestions)).toEqual(['q-2']);
	});

	it('clears the request_id across all three queues at once', () => {
		seedPermissions(['shared'], ['shared']);
		seedQuestions(['shared']);

		activeSession.resolveInteractionByRequestId('shared');

		expect(activeSession.current.pendingPermissions).toHaveLength(0);
		expect(activeSession.current.deferredPermissions).toHaveLength(0);
		expect(activeSession.current.pendingQuestions).toHaveLength(0);
	});

	it('is a cheap no-op (no store write) when nothing matches', () => {
		seedPermissions(['perm-1']);
		const before = activeSession.current;

		activeSession.resolveInteractionByRequestId('does-not-exist');

		// Identity preserved — no replacement object was produced.
		expect(activeSession.current).toBe(before);
		expect(ids(activeSession.current.pendingPermissions)).toEqual(['perm-1']);
	});

	it('is a no-op on empty lists', () => {
		const before = activeSession.current;

		activeSession.resolveInteractionByRequestId('anything');

		expect(activeSession.current).toBe(before);
	});

	it('ignores an empty request_id', () => {
		seedPermissions(['perm-1']);
		const before = activeSession.current;

		activeSession.resolveInteractionByRequestId('');

		expect(activeSession.current).toBe(before);
		expect(ids(activeSession.current.pendingPermissions)).toEqual(['perm-1']);
	});
});
