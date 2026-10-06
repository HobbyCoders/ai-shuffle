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
import { appData } from './app-data.svelte';
import { auth } from '$lib/stores/auth.svelte';
import { composerPreferences } from './composer-preferences';
import { sessionEphemeralCache } from './session-cache';
import { appMode } from './sessionMode.svelte';
import { bindSessionOwner } from './session-owner';

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
		vi.spyOn(globalThis, 'fetch').mockImplementation(async () => Response.json({}));
		composerPreferences.set('permissionMode', 'default');
		sessionEphemeralCache.clear();
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
		expect(composerPreferences.current.permissionMode).toBe('bypassPermissions');
	});

	it('keeps the previous selection when the owner rejects the update', async () => {
		vi.spyOn(api, 'put').mockRejectedValue(new Error('offline'));
		await expect(activeSession.setPermissionModeOverride('bypassPermissions')).rejects.toThrow('offline');
		expect(activeSession.current.permissionModeOverride).toBe('default');
		expect(composerPreferences.current.permissionMode).toBe('default');
	});

	it('does not apply a slow reply to a different session', async () => {
		let acknowledge!: (value: { permission_mode: string }) => void;
		vi.spyOn(api, 'put').mockImplementation(() => new Promise(resolve => { acknowledge = resolve; }));
		const pending = activeSession.setPermissionModeOverride('bypassPermissions');
		activeSession.current.sessionId = 'other-session';
		acknowledge({ permission_mode: 'bypassPermissions' });
		await pending;
		expect(activeSession.current.permissionModeOverride).toBe('default');
		expect(composerPreferences.current.permissionMode).toBe('bypassPermissions');
	});
	it('does not let an older acknowledgement replace a newer explicit preference', async () => {
		let acknowledge!: (value: { permission_mode: string }) => void;
		vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise(resolve => { acknowledge = resolve; }));
		const pending = activeSession.setPermissionModeOverride('bypassPermissions');
		activeSession.current.sessionId = null;
		await activeSession.setPermissionModeOverride('acceptEdits');
		acknowledge({ permission_mode: 'bypassPermissions' });
		await pending;
		expect(activeSession.current.permissionModeOverride).toBe('acceptEdits');
		expect(composerPreferences.current.permissionMode).toBe('acceptEdits');
	});

	it('retires an acknowledgement from a previous account', async () => {
		let acknowledge!: (value: { permission_mode: string }) => void;
		vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise(resolve => { acknowledge = resolve; }));
		const pending = activeSession.setPermissionModeOverride('bypassPermissions');
		bindSessionOwner('previous-owner', 'new-owner');
		acknowledge({ permission_mode: 'bypassPermissions' });
		await pending;
		expect(activeSession.current.permissionModeOverride).toBe('default');
		expect(composerPreferences.current.permissionMode).toBe('default');
	});

	it.each(['code', 'chat'] as const)('inherits the explicit selection in a fresh %s composer without changing another session', async (lane) => {
		appMode.set(lane, false);
		activeSession.current.sessionId = null;
		await activeSession.setPermissionModeOverride('bypassPermissions');
		// Viewing an existing Ask session must neither widen it nor erase the default.
		activeSession.current.sessionId = 'existing-ask';
		activeSession.current.permissionModeOverride = 'default';
		activeSession.applyComposerDefaults();
		expect(activeSession.current.permissionModeOverride).toBe('default');
		Object.assign(activeSession.current, { draft: '', worktreeBranch: '', worktreeId: null });
		activeSession.newChat();
		expect(activeSession.current.permissionModeOverride).toBe(lane === 'chat' ? null : 'bypassPermissions');
		expect(composerPreferences.current.permissionMode).toBe('bypassPermissions');
		appMode.set('code', false);
	});

	it.each([
		['', false],
		['{"type":"doc","content":[{"type":"paragraph"}]}', false],
		['typed draft', true],
		['{"type":"doc","content":[{"type":"paragraph","content":[{"type":"text","text":"Keep Ask"}]}]}', true],
		['{"type":"doc","content":[{"type":"paragraph","content":[{"type":"fileChip"}]}]}', true],
	] as const)('applies defaults only to semantically empty drafts (%j)', (draft, meaningful) => {
		composerPreferences.set('permissionMode', 'bypassPermissions');
		Object.assign(activeSession.current, {
			sessionId: null, mode: 'code', draft, permissionModeOverride: 'default',
			worktreeBranch: '', worktreeId: null, isStreaming: false,
			optimisticStreaming: false, isCreatingWorktree: false, isRestoringSession: false,
		});
		activeSession.applyComposerDefaults();
		expect(activeSession.current.permissionModeOverride).toBe(meaningful ? 'default' : 'bypassPermissions');
	});

	it('keeps a new unsent chat selection local and remembers explicit bypass for new sessions', async () => {
		activeSession.current.sessionId = null;
		const put = vi.spyOn(api, 'put');
		await activeSession.setPermissionModeOverride('bypassPermissions');
		expect(put).not.toHaveBeenCalled();
		expect(activeSession.current.permissionModeOverride).toBe('bypassPermissions');
		expect(localStorage.getItem('aihub_lastPermissionMode')).toBe('bypassPermissions');
	});
});


describe('live effort and Fast mode selection', () => {
	describe('context window selection', () => {
		it('saves the pin without sending a message and waits for acknowledgement', async () => {
			Object.assign(activeSession.current, { modelOverride: 'fable', contextWindowChoice: '1m' });
			const patchRow = vi.spyOn(appData, 'mergeSessionPatch');
			let acknowledge!: (value: { context_window: number }) => void;
			const put = vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise(resolve => { acknowledge = resolve; }));
			const pending = activeSession.setContextWindowChoice('standard');
			expect(put).toHaveBeenCalledWith('/sessions/live-knobs/run-settings', { context_window: 200_000 });
			expect(activeSession.current.contextWindowChoice).toBe('1m');
			expect(patchRow).not.toHaveBeenCalled();
			acknowledge({ context_window: 200_000 });
			await pending;
			expect(activeSession.current.contextWindowChoice).toBe('standard');
			expect(patchRow).toHaveBeenCalledWith('live-knobs', { context_window: 200_000 });
			// The next send names this pin as its base (#1895).
			expect(activeSession.current.contextWindowPin).toBe(200_000);
		});
		it('keeps the previous pin when saving fails', async () => {
			Object.assign(activeSession.current, { modelOverride: 'fable', contextWindowChoice: '1m', contextWindowPin: 1_000_000 });
			vi.spyOn(api, 'put').mockRejectedValue(new Error('unreachable'));
			await activeSession.setContextWindowChoice('standard');
			expect(activeSession.current.contextWindowChoice).toBe('1m');
			expect(activeSession.current.contextWindowPin).toBe(1_000_000);
		});
	});

	beforeEach(() => {
		vi.spyOn(globalThis, 'fetch').mockImplementation(async () => Response.json({}));
		sessionEphemeralCache.clear();
		Object.assign(activeSession.current, { sessionId: 'live-knobs', effortLevelOverride: 'high', fastMode: false, isStreaming: true });
	});
	afterEach(() => { vi.restoreAllMocks(); activeSession.current.sessionId = null; });

	it('waits for acknowledgement and changes both knobs without stopping', async () => {
		let acknowledge!: (value: { effort: string; fast: boolean }) => void;
		const put = vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise(resolve => { acknowledge = resolve; }));
		const pending = activeSession.setEffortLevelOverride('low');
		expect(put).toHaveBeenCalledWith('/sessions/live-knobs/run-settings', { effort: 'low' });
		expect(activeSession.current.effortLevelOverride).toBe('high');
		acknowledge({ effort: 'low', fast: false });
		await pending;
		put.mockResolvedValueOnce({ effort: 'low', fast: true });
		await activeSession.setFastMode(true);
		expect(activeSession.current.effortLevelOverride).toBe('low');
		expect(activeSession.current.fastMode).toBe(true);
		expect(activeSession.current.isStreaming).toBe(true);
		expect(hoisted.reconnect).not.toHaveBeenCalled();
	});

	it('retains picks after rejection and does not submit a message', async () => {
		vi.spyOn(api, 'put').mockRejectedValue(new Error('unreachable'));
		await activeSession.setFastMode(true);
		expect(activeSession.current.fastMode).toBe(false);
		expect(activeSession.current.isStreaming).toBe(true);
	});

	it('does not apply replies to another open session', async () => {
		let acknowledge!: (value: { effort: string; fast: boolean }) => void;
		vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise(resolve => { acknowledge = resolve; }));
		const pending = activeSession.setEffortLevelOverride('low');
		activeSession.current.sessionId = 'different-session';
		acknowledge({ effort: 'low', fast: false });
		await pending;
		expect(activeSession.current.effortLevelOverride).toBe('high');
	});

	it('retires pending picks and queued requests at an account boundary', async () => {
		let acknowledge!: (value: { effort: string; fast: boolean }) => void;
		const put = vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise(resolve => { acknowledge = resolve; }));
		const pending = activeSession.setEffortLevelOverride('low');
		const queued = activeSession.setFastMode(true);
		bindSessionOwner('account-before', 'account-after');
		acknowledge({ effort: 'low', fast: false });
		await Promise.all([pending, queued]);
		expect(put).toHaveBeenCalledTimes(1);
		expect(activeSession.current.effortLevelOverride).toBe('high');
		expect(activeSession.current.fastMode).toBe(false);
	});

	it('serializes rapid picks and lets the latest acknowledged pick win', async () => {
		let acknowledge!: (value: { effort: string; fast: boolean }) => void;
		const put = vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise(resolve => { acknowledge = resolve; }))
			.mockResolvedValueOnce({ effort: 'medium', fast: false });
		const first = activeSession.setEffortLevelOverride('low');
		const second = activeSession.setEffortLevelOverride('medium');
		expect(put).toHaveBeenCalledTimes(1);
		acknowledge({ effort: 'low', fast: false });
		await Promise.all([first, second]);
		expect(put).toHaveBeenLastCalledWith('/sessions/live-knobs/run-settings', { effort: 'medium' });
		expect(activeSession.current.effortLevelOverride).toBe('medium');
	});

	it('supports explicit default effort and turning Fast mode off', async () => {
		const put = vi.spyOn(api, 'put').mockResolvedValue({ effort: null, fast: false });
		activeSession.current.fastMode = true;
		await activeSession.setEffortLevelOverride(null);
		await activeSession.setFastMode(false);
		expect(put).toHaveBeenCalledWith('/sessions/live-knobs/run-settings', { effort: null });
		expect(put).toHaveBeenCalledWith('/sessions/live-knobs/run-settings', { fast: false });
		expect(activeSession.current.effortLevelOverride).toBeNull();
		expect(activeSession.current.fastMode).toBe(false);
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
