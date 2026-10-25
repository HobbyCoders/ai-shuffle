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
			activate: vi.fn(),
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
import { selectedContextWindow, standardContextCap } from './session/context-window';
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
		activeSession.setProject('proj-accept', null);
		expect(activeSession.current.mode).toBe('code');
		expect(activeSession.current.permissionModeOverride).toBe('acceptEdits');
		activeSession.clearProject();
		// #641: a session without a project keeps Accept Edits.
		expect(activeSession.current.permissionModeOverride).toBe('acceptEdits');
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
		// A new session starts without a project (#371 step 4), whose Access
		// menu offers Bypass — so the pick carries over whichever lane was shown.
		expect(activeSession.current.permissionModeOverride).toBe('bypassPermissions');
		expect(composerPreferences.current.permissionMode).toBe('bypassPermissions');
		appMode.set('code', false);
	});

	it('keeps Accept Edits on a fresh composer without a project (#641)', async () => {
		activeSession.current.sessionId = null;
		await activeSession.setPermissionModeOverride('acceptEdits');
		Object.assign(activeSession.current, { draft: '', worktreeBranch: '', worktreeId: null });
		activeSession.newChat();
		// A session without a project is a normal session: Accept Edits carries over.
		expect(activeSession.current.mode).toBe('chat');
		expect(activeSession.current.permissionModeOverride).toBe('acceptEdits');
		expect(composerPreferences.current.permissionMode).toBe('acceptEdits');
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


describe('attaching a project (#371)', () => {
	beforeEach(() => {
		composerPreferences.set('permissionMode', 'bypassPermissions');
		sessionEphemeralCache.clear();
		activeSession.current.sessionId = 'attach-me';
		activeSession.current.mode = 'chat';
		activeSession.current.project = '';
		activeSession.current.projectV2Id = null;
		activeSession.current.permissionModeOverride = 'bypassPermissions';
	});
	afterEach(() => { activeSession.current.sessionId = null; });

	it('shows the open session as a project session on Ask, and keeps the pick for new sessions', () => {
		activeSession.applyAttachedProject('attach-me', 'proj-1', 'v2-1', 'default');
		expect(activeSession.current.mode).toBe('code');
		expect(activeSession.current.project).toBe('proj-1');
		expect(activeSession.current.projectV2Id).toBe('v2-1');
		expect(activeSession.current.permissionModeOverride).toBe('default');
		// The owner's remembered pick for NEW sessions is theirs to change.
		expect(composerPreferences.current.permissionMode).toBe('bypassPermissions');
	});

	it('a session restored from a snapshot saved before an attach elsewhere takes the row\'s project and Ask', () => {
		// init() restored mode 'chat' and Bypass from this tab's snapshot; the
		// session list then shows a project was attached from another device.
		const mode = activeSession.reconcileSessionMode([
			{ id: 'attach-me', mode: null, project_id: 'proj-1', project_v2_id: 'v2-1', permission_mode: 'default' }
		]);
		expect(mode).toBe('code');
		expect(activeSession.current.mode).toBe('code');
		expect(activeSession.current.project).toBe('proj-1');
		expect(activeSession.current.projectV2Id).toBe('v2-1');
		expect(activeSession.current.permissionModeOverride).toBe('default');
		expect(composerPreferences.current.permissionMode).toBe('bypassPermissions');
	});

	it('a reload restores a chat without the device default project, so a later attach takes the row\'s project and Ask', async () => {
		// The normal case: this device remembers a default project. Restoring
		// the chat must not fill it in, or reconcile reads it as the session's
		// own project, misses the attach, and keeps Bypass on the wrong project.
		localStorage.setItem('aihub_selectedProject', 'device-default');
		// An earlier test's New Chat may have left the skip-restore flag.
		sessionStorage.removeItem('__aihub_skipSessionRestore');
		sessionStorage.setItem('ai-shuffle-active-session', JSON.stringify({
			sessionId: 'attach-me', mode: 'chat', title: 'Chat', profile: 'p', project: '', projectV2Id: null,
			permissionModeOverride: 'bypassPermissions', modelOverride: null, effortLevelOverride: null,
			// Written by a build that records which binding fields are real (#418).
			bindingHydrated: { profile: true, permission: true, fast: true },
		}));
		vi.spyOn(api, 'get').mockRejectedValue(new Error('offline'));
		activeSession.current.sessionId = null;
		try {
			await activeSession.init();
			expect(activeSession.current).toMatchObject({
				sessionId: 'attach-me', mode: 'chat', project: '', permissionModeOverride: 'bypassPermissions',
			});

			const mode = activeSession.reconcileSessionMode([
				{ id: 'attach-me', mode: null, project_id: 'proj-1', project_v2_id: 'v2-1', permission_mode: 'default' }
			]);
			expect(mode).toBe('code');
			expect(activeSession.current).toMatchObject({
				mode: 'code', project: 'proj-1', projectV2Id: 'v2-1', permissionModeOverride: 'default',
			});
		} finally {
			activeSession.destroy();
			sessionStorage.removeItem('ai-shuffle-active-session');
			localStorage.removeItem('aihub_selectedProject');
			vi.restoreAllMocks();
		}
	});

	it('a chat whose state holds a filled-in default project still adopts an attach', () => {
		// Same guard in reconcile itself: in a chat, `project` is never the
		// session's own, so it cannot hide the attach.
		activeSession.current.project = 'device-default';
		activeSession.reconcileSessionMode([
			{ id: 'attach-me', mode: null, project_id: 'proj-1', project_v2_id: null, permission_mode: 'default' }
		]);
		expect(activeSession.current).toMatchObject({ mode: 'code', project: 'proj-1', permissionModeOverride: 'default' });
	});

	it('leaves a restored pick alone when nothing was attached', () => {
		activeSession.reconcileSessionMode([{ id: 'attach-me', mode: 'chat', project_id: null, permission_mode: 'default' }]);
		expect(activeSession.current.mode).toBe('chat');
		expect(activeSession.current.permissionModeOverride).toBe('bypassPermissions');
	});

	it('also resets the cached copy of a session that is no longer on screen', () => {
		sessionEphemeralCache.save('attach-me', { ...activeSession.current, permissionModeOverride: 'auto' });
		activeSession.current.sessionId = 'something-else';
		activeSession.applyAttachedProject('attach-me', 'proj-1', null);
		expect(sessionEphemeralCache.restore('attach-me')?.permissionModeOverride).toBe('default');
		expect(activeSession.current.permissionModeOverride).toBe('bypassPermissions');
		expect(activeSession.current.project).toBe('');
	});
});

describe('per-model effort selection', () => {
 it('remembers explicit composer picks and restores them on model switches', async () => {
  Object.assign(activeSession.current, {sessionId:null, modelOverride:'opus', effortLevelOverride:null});
  composerPreferences.set('modelEfforts', {});
  await activeSession.setEffortLevelOverride('max');
  activeSession.setModelOverride('sonnet');
  expect(activeSession.current.effortLevelOverride).toBe('max');
  await activeSession.setEffortLevelOverride('low');
  activeSession.setModelOverride('opus');
  expect(activeSession.current.effortLevelOverride).toBe('max');
  activeSession.setModelOverride('sonnet');
  expect(activeSession.current.effortLevelOverride).toBe('low');
  activeSession.setModelOverride('never-used');
  expect(activeSession.current.effortLevelOverride).toBe('low');
 });
 it('remembers the acknowledged model without replacing a newly selected model effort', async () => {
  Object.assign(activeSession.current, {sessionId:'late-effort', modelOverride:'opus'});
  composerPreferences.set('modelEfforts', {sonnet:'low'});
  let acknowledge!: (value: {effort:string}) => void;
  const put = vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise(resolve => {acknowledge=resolve;}));
  const pending = activeSession.setEffortLevelOverride('max');
  activeSession.setModelOverride('sonnet');
  acknowledge({effort:'max'}); await pending;
  expect(activeSession.current.effortLevelOverride).toBe('low');
  expect(composerPreferences.current.modelEfforts['claude-opus-5']).toBe('max');
  put.mockRestore(); activeSession.current.sessionId=null;
 });
 it('does not save capability reconciliation as an owner pick', async () => {
  Object.assign(activeSession.current, {sessionId:null, modelOverride:'opus'});
  composerPreferences.set('modelEfforts', {opus:'ultra'});
  await activeSession.setEffortLevelOverride('max', false);
  expect(composerPreferences.current.modelEfforts['claude-opus-5']).toBe('ultra');
 });
});

describe('per-model fast mode selection', () => {
 beforeEach(() => { for (const model of ['opus', 'prov:codex:gpt-6.1-sol']) composerPreferences.rememberFast(model, null); });
 it('restores each model pick, carries the current pick to a never-used model and forces it off where unsupported', async () => {
  Object.assign(activeSession.current, {sessionId:null, modelOverride:'opus', fastMode:false});
  await activeSession.setFastMode(true);
  expect(composerPreferences.fastForModel('opus')).toBe(true);
  activeSession.setModelOverride('prov:codex:gpt-6.1-sol');
  expect(activeSession.current.fastMode).toBe(true);
  await activeSession.setFastMode(false);
  activeSession.setModelOverride('sonnet');
  expect(activeSession.current.fastMode).toBe(false);
  activeSession.setModelOverride('opus');
  expect(activeSession.current.fastMode).toBe(true);
  activeSession.setModelOverride('prov:codex:gpt-6.1-sol');
  expect(activeSession.current.fastMode).toBe(false);
 });
 it('does not forget the pick when an unsupported model forces fast off', async () => {
  Object.assign(activeSession.current, {sessionId:null, modelOverride:'opus', fastMode:false});
  await activeSession.setFastMode(true);
  activeSession.current.modelOverride = 'sonnet';
  await activeSession.setFastMode(false, false);
  expect(activeSession.current.fastMode).toBe(false);
  expect(composerPreferences.fastForModel('opus')).toBe(true);
  activeSession.setModelOverride('opus');
  expect(activeSession.current.fastMode).toBe(true);
 });
 it('remembers the acknowledged model without replacing the newly selected model pick', async () => {
  Object.assign(activeSession.current, {sessionId:'late-fast', modelOverride:'opus', fastMode:false});
  composerPreferences.rememberFast('prov:codex:gpt-6.1-sol', false);
  let acknowledge!: (value: {fast:boolean}) => void;
  const put = vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise(resolve => {acknowledge=resolve;}));
  const pending = activeSession.setFastMode(true);
  activeSession.setModelOverride('prov:codex:gpt-6.1-sol');
  acknowledge({fast:true}); await pending;
  expect(activeSession.current.fastMode).toBe(false);
  expect(composerPreferences.fastForModel('opus')).toBe(true);
  put.mockRestore(); activeSession.current.sessionId=null;
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
		it('saves a raised Standard cap as the chat pin and remembers it for the model', async () => {
			Object.assign(activeSession.current, { modelOverride: 'fable', contextWindowChoice: '1m', contextWindowPin: 1_000_000 });
			const put = vi.spyOn(api, 'put').mockResolvedValue({ context_window: 450_000 });
			try {
				await activeSession.setContextStandardCap(450_400);
				expect(put).toHaveBeenCalledWith('/sessions/live-knobs/run-settings', { context_window: 450_000 });
				expect(activeSession.current.contextWindowChoice).toBe('standard');
				expect(activeSession.current.contextWindowPin).toBe(450_000);
				expect(composerPreferences.contextCapForModel('fable')).toBe(450_000);
				expect(standardContextCap(activeSession.current)).toBe(450_000);
				expect(selectedContextWindow(activeSession.current)).toBe(450_000);
			} finally {
				composerPreferences.rememberContextCap('fable', null);
			}
		});
		it('switching an Extended chat to Standard uses the remembered cap', async () => {
			composerPreferences.rememberContextCap('fable', 600_000);
			Object.assign(activeSession.current, { modelOverride: 'fable', contextWindowChoice: '1m', contextWindowPin: 1_000_000 });
			const put = vi.spyOn(api, 'put').mockResolvedValue({ context_window: 600_000 });
			try {
				await activeSession.setContextWindowChoice('standard');
				expect(put).toHaveBeenCalledWith('/sessions/live-knobs/run-settings', { context_window: 600_000 });
			} finally {
				composerPreferences.rememberContextCap('fable', null);
			}
		});
		it('a new chat keeps the cap as a preference and sends it on Standard', () => {
			Object.assign(activeSession.current, { sessionId: null, modelOverride: 'fable', contextWindowChoice: '1m', contextWindowPin: null });
			const put = vi.spyOn(api, 'put');
			try {
				activeSession.setContextStandardCap(5_000_000);
				expect(put).not.toHaveBeenCalled();
				expect(activeSession.current.contextWindowChoice).toBe('standard');
				// Clamped one step below the full window: Standard never equals Extended.
				expect(selectedContextWindow(activeSession.current)).toBe(990_000);
				activeSession.setContextStandardCap(200_000);
				// The family default is not stored as a raised cap.
				expect(composerPreferences.contextCapForModel('fable')).toBeNull();
				expect(selectedContextWindow(activeSession.current)).toBe(200_000);
			} finally {
				composerPreferences.rememberContextCap('fable', null);
			}
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
