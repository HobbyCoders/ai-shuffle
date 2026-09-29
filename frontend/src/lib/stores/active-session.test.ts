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

import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

// Capture the onMessage callback active-session injects into its WS connection so
// the test can drive the real message handlers to seed pending queues.
const hoisted = vi.hoisted(() => ({
	onMessage: null as ((data: Record<string, unknown>) => void) | null,
	onDisconnected: null as ((code: number, reason: string, wasClean: boolean) => void) | null,
	onConnected: null as (() => void) | null,
	connect: vi.fn(),
	reconnect: vi.fn(),
	resetReconnectAttempts: vi.fn(),
	goto: vi.fn(),
}));

vi.mock('./session/ws-connection', () => ({
	createWsConnection: (config: {
		onMessage: (data: Record<string, unknown>) => void;
		onDisconnected: (code: number, reason: string, wasClean: boolean) => void;
		onConnected: () => void;
	}) => {
		hoisted.onMessage = config.onMessage;
		hoisted.onDisconnected = config.onDisconnected;
		hoisted.onConnected = config.onConnected;
		return {
			connect: hoisted.connect,
			reconnect: hoisted.reconnect,
			disconnect: vi.fn(),
			send: vi.fn(() => true),
			isConnected: vi.fn(() => false),
			resetLastMessageTime: vi.fn(),
			resetReconnectAttempts: hoisted.resetReconnectAttempts,
			destroy: vi.fn(),
		};
	},
}));

vi.mock('$app/navigation', () => ({ goto: hoisted.goto }));

import { activeSession } from './active-session';
import { api, recheckAuthOn401 } from '$lib/api/client';
import { auth } from '$lib/stores/auth.svelte';

describe('chat authentication close recovery', () => {
	afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); });

	it.each([true, false])('confirms the current worker session after 4001 (authenticated=%s)', async (authenticated) => {
		vi.stubGlobal('fetch', vi.fn().mockResolvedValue(Response.json({ authenticated })));
		Object.defineProperty(window, 'location', { configurable: true, value: { pathname: '/', assign: vi.fn() } });
		hoisted.onDisconnected!(4001, 'Authentication required', true);
		await recheckAuthOn401();
		expect(fetch).toHaveBeenCalledExactlyOnceWith('/api/v1/auth/status', expect.objectContaining({ cache: 'no-store' }));
		expect(activeSession.current.wsConnected).toBe(false);
		expect(activeSession.current.isRestoringSession).toBe(false);
		expect(hoisted.connect).not.toHaveBeenCalled();
		if (authenticated) {
			expect(hoisted.goto).not.toHaveBeenCalled();
			expect(activeSession.current.wsReconnectRequired).toBe(true);
			activeSession.reconnect();
			expect(hoisted.reconnect).toHaveBeenCalledOnce();
			expect(hoisted.connect).not.toHaveBeenCalled();
			hoisted.onConnected!();
			expect(activeSession.current.wsReconnectRequired).toBe(false);
		} else {
			// No login page exists: the refreshed auth store drives the root layout instead.
			expect(hoisted.goto).not.toHaveBeenCalled();
			expect(auth.current.status?.authenticated).toBe(false);
			expect(activeSession.current.wsReconnectRequired).toBe(true);
		}
	});
});

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

describe('live permission selection', () => {
	beforeEach(() => {
		activeSession.current.sessionId = 'live-permissions';
		activeSession.current.permissionModeOverride = 'default';
		activeSession.current.isStreaming = true;
	});
	afterEach(() => { vi.restoreAllMocks(); activeSession.current.sessionId = null; });

	it('waits for the running device acknowledgement without stopping the stream', async () => {
		let acknowledge!: (value: { permission_mode: string }) => void;
		const put = vi.spyOn(api, 'put').mockImplementation(() => new Promise(resolve => { acknowledge = resolve; }));
		const pending = activeSession.setPermissionModeOverride('bypassPermissions');
		expect(put).toHaveBeenCalledWith('/sessions/live-permissions/permission-mode', { permission_mode: 'bypassPermissions' });
		expect(activeSession.current.permissionModeOverride).toBe('default');
		acknowledge({ permission_mode: 'bypassPermissions' });
		await pending;
		expect(activeSession.current.permissionModeOverride).toBe('bypassPermissions');
		expect(activeSession.current.isStreaming).toBe(true);
	});

	it('keeps the previous selection when the owner rejects the update', async () => {
		vi.spyOn(api, 'put').mockRejectedValue(new Error('offline'));
		await expect(activeSession.setPermissionModeOverride('bypassPermissions')).rejects.toThrow('offline');
		expect(activeSession.current.permissionModeOverride).toBe('default');
	});

	it('does not apply a slow reply to a different session', async () => {
		let acknowledge!: (value: { permission_mode: string }) => void;
		vi.spyOn(api, 'put').mockImplementation(() => new Promise(resolve => { acknowledge = resolve; }));
		const pending = activeSession.setPermissionModeOverride('bypassPermissions');
		activeSession.current.sessionId = 'other-session';
		acknowledge({ permission_mode: 'bypassPermissions' });
		await pending;
		expect(activeSession.current.permissionModeOverride).toBe('default');
	});

	it('keeps a new unsent chat selection local and does not remember bypass as a default', async () => {
		activeSession.current.sessionId = null;
		const put = vi.spyOn(api, 'put');
		await activeSession.setPermissionModeOverride('bypassPermissions');
		expect(put).not.toHaveBeenCalled();
		expect(activeSession.current.permissionModeOverride).toBe('bypassPermissions');
		expect(localStorage.getItem('aihub_lastPermissionMode')).toBe('default');
	});
});


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
