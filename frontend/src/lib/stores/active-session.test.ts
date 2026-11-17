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
	onManualReconnectFailed: null as ((code: number, reason: string) => void) | null,
	onMaxReconnectAttempts: null as (() => void) | null,
	connect: vi.fn(),
	reconnect: vi.fn((): boolean => true),
	resetReconnectAttempts: vi.fn(),
	goto: vi.fn(),
}));

vi.mock('./session/ws-connection', () => ({
	createWsConnection: (config: {
		onMessage: (data: Record<string, unknown>) => void;
		onDisconnected: (code: number, reason: string, wasClean: boolean) => void;
		onConnected: () => void;
		onManualReconnectFailed?: (code: number, reason: string) => void;
		onMaxReconnectAttempts?: () => void;
	}) => {
		hoisted.onMaxReconnectAttempts = config.onMaxReconnectAttempts ?? null;
		hoisted.onManualReconnectFailed = config.onManualReconnectFailed ?? null;
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

import { get } from 'svelte/store';
import { openDropdown, openHeaderDropdown } from './header-dropdowns';
import { fastChangeWaits } from '$lib/utils/fast-outcome';
import { activeSession } from './active-session';
import { api, recheckAuthOn401 } from '$lib/api/client';
import { appData } from './app-data.svelte';
import { auth } from '$lib/stores/auth.svelte';
import { composerPreferences } from './composer-preferences';
import { sessionEphemeralCache } from './session-cache';
import { notify } from './notifications';
import { selectedContextWindow, standardContextCap } from './session/context-window';
import { appMode } from './sessionMode.svelte';
import { bindSessionOwner } from './session-owner';
import { finishBootReconciliation, getInitGeneration, hasBootProjectPick } from './session/session-init';
import { captureDestination, stillAtDestination } from '$lib/components/chat/send-destination';

describe('bootstrap new-chat landing', () => {
	it('forwards the same-surface landing intent without changing explicit New chat', () => {
		sessionEphemeralCache.clear();
		activeSession.current.sessionId = null;
		openDropdown('notifications');
		activeSession.newChat({ initialLanding: true });
		expect(get(openHeaderDropdown)).toBe('notifications');
		activeSession.newChat();
		expect(get(openHeaderDropdown)).toBeNull();
	});
});

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

describe('manual reconnect feedback', () => {
	afterEach(() => {
		// Leave the singleton connected and idle for the next suite.
		hoisted.onConnected?.();
		vi.clearAllMocks();
		hoisted.reconnect.mockImplementation(() => true);
	});

	it('marks the tap in flight at once and ignores repeat taps until it ends', () => {
		hoisted.onDisconnected!(1006, '', false);
		activeSession.reconnect();
		expect(activeSession.current.wsManualReconnect).toBe('connecting');
		activeSession.reconnect();
		activeSession.reconnect();
		expect(hoisted.reconnect).toHaveBeenCalledOnce();
		// The socket layer's own close notice during the swap keeps it in flight.
		hoisted.onDisconnected!(4000, 'Manual reconnect', false);
		expect(activeSession.current.wsManualReconnect).toBe('connecting');
		hoisted.onConnected!();
		expect(activeSession.current.wsManualReconnect).toBe('idle');
		expect(activeSession.current.wsConnected).toBe(true);
	});

	it('reports a failed tap and lets the next tap try again', () => {
		hoisted.onDisconnected!(1006, '', false);
		activeSession.reconnect();
		hoisted.onDisconnected!(1006, 'Refused', false);
		hoisted.onManualReconnectFailed!(1006, 'Refused');
		expect(activeSession.current.wsManualReconnect).toBe('failed');
		activeSession.reconnect();
		expect(hoisted.reconnect).toHaveBeenCalledTimes(2);
		expect(activeSession.current.wsManualReconnect).toBe('connecting');
	});

	it('clears a failed tap when an automatic retry connects', () => {
		hoisted.onDisconnected!(1006, '', false);
		activeSession.reconnect();
		hoisted.onManualReconnectFailed!(4000, 'Connection attempt timed out');
		hoisted.onConnected!();
		expect(activeSession.current.wsManualReconnect).toBe('idle');
	});

	it('never leaves the control stuck busy when nothing could start', () => {
		hoisted.onDisconnected!(1006, '', false);
		hoisted.reconnect.mockImplementationOnce(() => false);
		activeSession.reconnect();
		expect(activeSession.current.wsManualReconnect).toBe('failed');
		hoisted.reconnect.mockImplementationOnce(() => { throw new Error('bad url'); });
		const quiet = vi.spyOn(console, 'error').mockImplementation(() => {});
		activeSession.reconnect();
		expect(activeSession.current.wsManualReconnect).toBe('failed');
		quiet.mockRestore();
	});

	it('marks reconnect as required once automatic retries are exhausted', () => {
		hoisted.onDisconnected!(1006, '', false);
		expect(activeSession.current.wsReconnectRequired).toBe(false);
		hoisted.onMaxReconnectAttempts!();
		expect(activeSession.current.wsReconnectRequired).toBe(true);
		hoisted.onConnected!();
		expect(activeSession.current.wsReconnectRequired).toBe(false);
	});

	it('drops an in-flight tap on an intentional disconnect', () => {
		hoisted.onDisconnected!(1006, '', false);
		activeSession.reconnect();
		hoisted.onDisconnected!(1000, '', true);
		expect(activeSession.current.wsManualReconnect).toBe('idle');
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
		// New sessions open on the owner's last project choice; here, none.
		activeSession.clearProject({ user: true });
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

	it('a row this worker runs adopts its own project id', () => {
		expect(activeSession.adoptAttachedProject([
			{ id: 'attach-me', mode: null, project_id: 'proj-1', project_v2_id: 'v2-1', permission_mode: 'default', is_remote: false }
		])).toBe(true);
		expect(activeSession.current).toMatchObject({ mode: 'code', project: 'proj-1', projectV2Id: 'v2-1' });
	});

	describe('a chat another worker runs', () => {
		const adopt = (projectId: string | null, extra: Record<string, unknown> = {}) => {
			activeSession.current.mode = 'chat';
			activeSession.current.project = '';
			activeSession.current.projectV2Id = null;
			const adopted = activeSession.adoptAttachedProject([
				{ id: 'attach-me', mode: null, project_id: projectId, project_v2_id: 'v2-theirs', permission_mode: 'default', is_remote: true, ...extra }
			]);
			return { adopted, project: activeSession.current.project, projectV2Id: activeSession.current.projectV2Id };
		};
		afterEach(() => {
			appData.current.clusterProjects = [];
		});

		it('adopts the shared id alone with the catalog not loaded, whatever legacy id the row holds (W1)', () => {
			// A passive watcher: no catalog yet. The row's `app` may be an
			// unrelated project here; naming it would send uploads into it.
			appData.current.clusterProjects = [];
			for (const projectId of ['app', 'same-here', null]) {
				expect(adopt(projectId)).toEqual({ adopted: true, project: '', projectV2Id: 'v2-theirs' });
			}
			expect(activeSession.current).toMatchObject({ mode: 'code', permissionModeOverride: 'default' });
		});

		it('never names a local checkout, even one the catalog lists for the project', () => {
			appData.current.clusterProjects = [
				{ project_v2_id: 'v2-mine', origin: 'o1', name: 'mine', local_project_id: 'app', eligible: [] },
				{ project_v2_id: 'v2-theirs', origin: 'o2', name: 'theirs', local_project_id: 'theirs-here', eligible: [] },
			] as never;
			for (const projectId of ['app', 'theirs-here', null]) {
				expect(adopt(projectId)).toEqual({ adopted: true, project: '', projectV2Id: 'v2-theirs' });
			}
		});

		it('a row the store knows is remote but whose list entry has no flag yet is treated as remote', () => {
			activeSession.current.isRemote = true;
			try {
				expect(adopt('app', { is_remote: undefined })).toEqual({ adopted: true, project: '', projectV2Id: 'v2-theirs' });
			} finally {
				activeSession.current.isRemote = undefined;
			}
		});

		it('a remote row with no shared id is not adopted: its legacy id names nothing here', () => {
			activeSession.current.mode = 'chat';
			expect(activeSession.adoptAttachedProject([
				{ id: 'attach-me', mode: null, project_id: 'app', project_v2_id: null, permission_mode: 'default', is_remote: true }
			])).toBe(false);
			expect(activeSession.current).toMatchObject({ mode: 'chat', project: '' });
		});

		it('a later sessions refresh keeps the access the owner picked after the attach', () => {
			adopt('app');
			activeSession.current.permissionModeOverride = 'acceptEdits';
			expect(activeSession.adoptAttachedProject([
				{ id: 'attach-me', mode: null, project_id: 'app', project_v2_id: 'v2-theirs', permission_mode: 'default', is_remote: true }
			])).toBe(false);
			expect(activeSession.current.permissionModeOverride).toBe('acceptEdits');
		});
	});

	describe('waiting for the mirrored row after a relayed attach (W2)', () => {
		const noProjectRow = { id: 'attach-me', mode: 'chat', project_id: null, project_v2_id: null, permission_mode: 'bypassPermissions', is_remote: true };
		const attachedRow = { id: 'attach-me', mode: null, project_id: 'app', project_v2_id: 'v2-theirs', permission_mode: 'default', is_remote: true };
		let rows: Record<string, unknown>[];
		beforeEach(() => {
			rows = [noProjectRow];
			vi.spyOn(appData, 'loadSessions').mockImplementation(async () => {
				appData.current.sessions = rows as never;
			});
		});
		afterEach(() => {
			vi.useRealTimers();
			vi.restoreAllMocks();
			appData.current.sessions = [];
		});
		/** What live-sync does on a `sessions` invalidation. */
		const sync = async () => {
			await appData.loadSessions();
			activeSession.adoptAttachedProject(appData.current.sessions as never);
		};

		it('holds no project until this worker\'s row has it, then adopts the shared id', async () => {
			let outcome: string | null = null;
			const wait = activeSession.waitForAttachedRow('attach-me', 15_000).then((o) => { outcome = o; });
			await Promise.resolve();
			await Promise.resolve();
			// The owning worker confirmed, but this worker's row is still a
			// no-project chat: no project in state, so no project controls.
			expect(appData.loadSessions).toHaveBeenCalledTimes(1);
			expect(outcome).toBeNull();
			expect(activeSession.current).toMatchObject({ mode: 'chat', project: '', projectV2Id: null, permissionModeOverride: 'bypassPermissions' });

			rows = [attachedRow];
			await sync();
			await wait;
			expect(outcome).toBe('adopted');
			expect(activeSession.current).toMatchObject({ mode: 'code', project: '', projectV2Id: 'v2-theirs', permissionModeOverride: 'default' });
		});

		it('finishes at once when the row was adopted before the answer returned', async () => {
			rows = [attachedRow];
			await sync();
			await expect(activeSession.waitForAttachedRow('attach-me', 15_000)).resolves.toBe('adopted');
		});

		it('gives up at the bound with the chat still project-free, and adopts when the row arrives later', async () => {
			vi.useFakeTimers();
			const wait = activeSession.waitForAttachedRow('attach-me', 15_000);
			await vi.advanceTimersByTimeAsync(15_000);
			await expect(wait).resolves.toBe('pending');
			expect(activeSession.current).toMatchObject({ mode: 'chat', project: '', projectV2Id: null });

			rows = [attachedRow];
			await sync();
			expect(activeSession.current).toMatchObject({ mode: 'code', project: '', projectV2Id: 'v2-theirs' });
		});

		it('stops waiting when the page leaves the chat, and never applies the project to another one', async () => {
			const wait = activeSession.waitForAttachedRow('attach-me', 15_000);
			activeSession.current.sessionId = 'other-chat';
			activeSession.setTitle('other-chat', 'Other');
			await expect(wait).resolves.toBe('left');
			rows = [attachedRow];
			await sync();
			expect(activeSession.current).toMatchObject({ sessionId: 'other-chat', mode: 'chat', project: '', projectV2Id: null });
		});
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

describe('boot reopening a chat opened by link (#1350)', () => {
	afterEach(() => { activeSession.destroy(); vi.restoreAllMocks(); });

	it('does not call off a send waiting on that chat; any other open does', async () => {
		vi.spyOn(api, 'get').mockRejectedValue(new Error('offline'));
		await activeSession.init({ deepLinkSessionId: 'linked' });
		const place = () => ({ sessionId: activeSession.current.sessionId, editorOwner: 'linked' });
		// Sent while boot's first loads ran; the cold-cache check still waits.
		const destination = captureDestination(place());
		activeSession.switchSession('linked', null, null, null, null, { reopen: true });
		expect(activeSession.current.sessionId).toBe('linked');
		expect(stillAtDestination(destination, place())).toBe(true);
		// The user opening the chat again themselves is still navigation.
		activeSession.switchSession('linked');
		expect(stillAtDestination(destination, place())).toBe(false);
	});

	it('is navigation when another chat is open by then', async () => {
		vi.spyOn(api, 'get').mockRejectedValue(new Error('offline'));
		await activeSession.init({ deepLinkSessionId: 'linked' });
		activeSession.switchSession('other');
		const generation = getInitGeneration();
		activeSession.switchSession('linked', null, null, null, null, { reopen: true });
		expect(getInitGeneration()).toBe(generation + 1);
	});
});

describe('a project picked while boot loads (#930)', () => {
	afterEach(() => { activeSession.destroy(); vi.restoreAllMocks(); });

	it('is noted for the landing reconcile and survives init', async () => {
		sessionStorage.removeItem('__aihub_skipSessionRestore');
		sessionStorage.removeItem('ai-shuffle-active-session');
		vi.spyOn(api, 'get').mockRejectedValue(new Error('offline'));
		const catalog = { ...appData.current, projects: [{ id: 'proj-picked' }] } as typeof appData.current;
		vi.spyOn(appData, 'current', 'get').mockReturnValue(catalog);
		activeSession.current.sessionId = null;
		const initializing = activeSession.init();
		const generation = getInitGeneration();
		expect(hasBootProjectPick(generation)).toBe(false);
		// A system fix-up (dropMissingProject's clear) and a no-op are not picks.
		activeSession.clearProject();
		activeSession.setProject('', null, { user: true });
		expect(hasBootProjectPick(generation)).toBe(false);
		activeSession.setProject('proj-picked', null);
		expect(hasBootProjectPick(generation)).toBe(false);
		activeSession.setProject('', null);
		activeSession.setProject('proj-picked', null, { user: true });
		expect(hasBootProjectPick(generation)).toBe(true);
		await initializing;
		expect(activeSession.current).toMatchObject({ sessionId: null, mode: 'code', project: 'proj-picked' });
		finishBootReconciliation(generation);
		expect(hasBootProjectPick(generation)).toBe(false);
	});

	it('is not noted once boot has reconciled', async () => {
		vi.spyOn(api, 'get').mockRejectedValue(new Error('offline'));
		activeSession.current.sessionId = null;
		await activeSession.init();
		const generation = getInitGeneration();
		finishBootReconciliation(generation);
		activeSession.setProject('proj-later', null, { user: true });
		expect(hasBootProjectPick(generation)).toBe(false);
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

describe('a model with no level of its own keeps the level in use (#1579, owner decision)', () => {
	// Switching to a model the owner never picked a level for, where the worker
	// offers no initial level, keeps the level the chat already had. Fitting it
	// to the new model's levels (and the notice, #1641) is the composer's job:
	// see ControlsDock.effort-fit.test.ts.
	afterEach(() => { vi.restoreAllMocks(); Object.assign(activeSession.current, { sessionId: null, modelOverride: null }); });

	it('keeps the inherited level in a new chat and in an existing one, and asks the worker nothing', () => {
		const put = vi.spyOn(api, 'put');
		composerPreferences.set('modelEfforts', {});
		for (const sessionId of [null, 'kept-level']) {
			Object.assign(activeSession.current, { sessionId, modelOverride: 'opus', effortLevelOverride: 'high', effortRev: 4 });
			activeSession.setModelOverride('sonnet');
			expect(activeSession.current).toMatchObject({ modelOverride: 'sonnet', effortLevelOverride: 'high', effortRev: 4 });
			activeSession.setModelOverride('never-used');
			expect(activeSession.current.effortLevelOverride).toBe('high');
		}
		expect(put).not.toHaveBeenCalled();
	});

	it('Auto stays Auto', () => {
		composerPreferences.set('modelEfforts', {});
		Object.assign(activeSession.current, { sessionId: null, modelOverride: 'opus', effortLevelOverride: null });
		activeSession.setModelOverride('sonnet');
		expect(activeSession.current.effortLevelOverride).toBeNull();
	});

	it('a level the owner picked for the new model wins over the inherited one', () => {
		composerPreferences.set('modelEfforts', { sonnet: 'low' });
		Object.assign(activeSession.current, { sessionId: null, modelOverride: 'opus', effortLevelOverride: 'high' });
		activeSession.setModelOverride('sonnet');
		expect(activeSession.current.effortLevelOverride).toBe('low');
		composerPreferences.set('modelEfforts', {});
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
	describe('the worker says a Fast change waits for the next message', () => {
		afterEach(() => {
			vi.restoreAllMocks();
			Object.assign(activeSession.current, {
				sessionId: null, isStreaming: false, liveTurnId: null, fastAppliesTurnId: null
			});
		});
		const start = (extra = {}) => Object.assign(activeSession.current, {
			sessionId: 'waits', modelOverride: 'opus', fastMode: false, isStreaming: true,
			liveTurnId: 't1', fastAppliesTurnId: null, ...extra
		});
		const waits = () => fastChangeWaits(activeSession.current);

		it('records only what the worker answered', async () => {
			start();
			vi.spyOn(api, 'put').mockResolvedValueOnce({ effort: 'high', fast: true });
			await activeSession.setFastMode(true);
			expect(waits()).toBe(false);
			vi.spyOn(api, 'put').mockResolvedValueOnce({ effort: 'high', fast: false, fast_applies_turn: 't1' });
			await activeSession.setFastMode(false);
			expect(waits()).toBe(true);
		});

		it('clears when a later answer no longer waits, and a window answer leaves it', async () => {
			start({ fastAppliesTurnId: 't1' });
			vi.spyOn(api, 'put').mockResolvedValueOnce({ context_window: 200_000 });
			await activeSession.setContextWindowChoice('standard');
			expect(waits()).toBe(true);
			vi.spyOn(api, 'put').mockResolvedValueOnce({ effort: 'high', fast: false });
			await activeSession.setFastMode(false);
			expect(waits()).toBe(false);
		});

		it('ends with its turn when the next turn starts in the same feed flush', () => {
			start({ fastAppliesTurnId: 't1' });
			expect(waits()).toBe(true);
			// T1's end and T2's start landed together: streaming never went false.
			Object.assign(activeSession.current, { isStreaming: true, liveTurnId: 't2' });
			expect(waits()).toBe(false);
		});

		it('ignores an answer about a turn that has since ended', async () => {
			start({ liveTurnId: 't2' });
			vi.spyOn(api, 'put').mockResolvedValueOnce({ effort: 'high', fast: true, fast_applies_turn: 't1' });
			await activeSession.setFastMode(true);
			expect(waits()).toBe(false);
		});

		it('ends with the turn, even when the answer lands after it', async () => {
			start({ isStreaming: false, liveTurnId: null });
			vi.spyOn(api, 'put').mockResolvedValueOnce({ effort: 'high', fast: true, fast_applies_turn: 't1' });
			await activeSession.setFastMode(true);
			expect(waits()).toBe(false);
		});
	});

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

	it("shows the worker's reason when it refuses an effort pick", async () => {
		const error = vi.spyOn(notify, 'error').mockImplementation(() => '');
		vi.spyOn(api, 'put').mockRejectedValue({ status: 409, detail: 'The session is still connecting. Try again in a moment.' });
		await activeSession.setEffortLevelOverride('low');
		expect(error).toHaveBeenCalledWith('Could not change effort: The session is still connecting. Try again in a moment.', expect.anything());
		expect(activeSession.current.effortLevelOverride).toBe('high');
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

	// #1609: the worker counts the changes of a chat's effort. Its answer to a
	// pick and its announcements name the revision, and the composer never
	// goes back to an older one.
	it('holds the revision the worker answered a pick with', async () => {
		activeSession.current.effortRev = 2;
		sessionEphemeralCache.save('live-knobs', { ...activeSession.current } as never);
		vi.spyOn(api, 'put').mockResolvedValueOnce({ effort: 'low', effort_rev: 3, fast: false });
		await activeSession.setEffortLevelOverride('low');
		expect(activeSession.current.effortLevelOverride).toBe('low');
		expect(activeSession.current.effortRev).toBe(3);
		expect(sessionEphemeralCache.restore('live-knobs')).toMatchObject({ effortLevelOverride: 'low', effortRev: 3 });
	});

	it('an answer that arrives after a newer announcement does not undo it', async () => {
		activeSession.current.effortRev = 2;
		let acknowledge!: (value: { effort: string; effort_rev: number; fast: boolean }) => void;
		vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise(resolve => { acknowledge = resolve; }));
		const pending = activeSession.setEffortLevelOverride('low');
		// Another tab picked max after this pick was saved; its push got here first.
		Object.assign(activeSession.current, { effortLevelOverride: 'max', effortRev: 4 });
		sessionEphemeralCache.save('live-knobs', { ...activeSession.current } as never);
		acknowledge({ effort: 'low', effort_rev: 3, fast: false });
		await pending;
		expect(activeSession.current.effortLevelOverride).toBe('max');
		expect(activeSession.current.effortRev).toBe(4);
		expect(sessionEphemeralCache.restore('live-knobs')).toMatchObject({ effortLevelOverride: 'max', effortRev: 4 });
	});

	it('its own announcement arriving before the answer changes nothing', async () => {
		activeSession.current.effortRev = 2;
		let acknowledge!: (value: { effort: string; effort_rev: number; fast: boolean }) => void;
		vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise(resolve => { acknowledge = resolve; }));
		const pending = activeSession.setEffortLevelOverride('low');
		Object.assign(activeSession.current, { effortLevelOverride: 'low', effortRev: 3 });
		acknowledge({ effort: 'low', effort_rev: 3, fast: false });
		await pending;
		expect(activeSession.current.effortLevelOverride).toBe('low');
		expect(activeSession.current.effortRev).toBe(3);
	});

	it('takes the answer of a worker whose count is behind its own', async () => {
		// The database was restored from a backup under this open tab: nothing
		// newer arrived since the pick, so the lower revision is the saved one.
		activeSession.current.effortRev = 9;
		vi.spyOn(api, 'put').mockResolvedValueOnce({ effort: 'low', effort_rev: 2, fast: false });
		await activeSession.setEffortLevelOverride('low');
		expect(activeSession.current.effortLevelOverride).toBe('low');
		expect(activeSession.current.effortRev).toBe(2);
	});

	it('an answer from a worker that does not count leaves the held revision alone', async () => {
		activeSession.current.effortRev = undefined;
		vi.spyOn(api, 'put').mockResolvedValueOnce({ effort: 'low', fast: false });
		await activeSession.setEffortLevelOverride('low');
		expect(activeSession.current.effortLevelOverride).toBe('low');
		expect(activeSession.current.effortRev).toBeUndefined();
	});

	it('takes a newer level from the reloaded row of the open chat, and no older one', () => {
		// A level saved on the worker that runs the chat reaches a page on
		// another worker only as the replicated row.
		Object.assign(activeSession.current, { effortLevelOverride: 'high', effortRev: 2 });
		expect(activeSession.adoptRowEffort([{ id: 'live-knobs', effort: 'low', effort_rev: 2 }])).toBe(false);
		expect(activeSession.adoptRowEffort([{ id: 'another-chat', effort: 'low', effort_rev: 9 }])).toBe(false);
		expect(activeSession.current.effortLevelOverride).toBe('high');
		expect(activeSession.adoptRowEffort([{ id: 'live-knobs', effort: 'low', effort_rev: 3 }])).toBe(true);
		expect(activeSession.current.effortLevelOverride).toBe('low');
		expect(activeSession.current.effortRev).toBe(3);
		// The list is a reload: a response older than a push leaves the push alone.
		expect(activeSession.adoptRowEffort([{ id: 'live-knobs', effort: 'high', effort_rev: 2 }])).toBe(false);
		expect(activeSession.current.effortLevelOverride).toBe('low');
	});

	// #1633: the picker asks the worker, which judges a level against the model
	// the chat still runs. A level the PICKED model offers is held here and
	// goes with the next message, which saves the model and the level together.
	// Nothing was saved, so the held level keeps the revision held at that
	// moment: only a newer revision (a level saved elsewhere since) replaces it.
	describe('a level held for a picked model that has not run yet (#1633)', () => {
		const refusal = { status: 409, detail: 'This model does not support the selected effort level' };
		const announce = (updates: Record<string, unknown>) => hoisted.onMessage!({
			event_type: 'session_updated', session_id: 'live-knobs', data: { updates },
		});
		const deferred = () => {
			let acknowledge!: (value: { effort: string; effort_rev: number; fast: boolean }) => void;
			const promise = new Promise<{ effort: string; effort_rev: number; fast: boolean }>(resolve => { acknowledge = resolve; });
			return { promise, acknowledge };
		};
		beforeEach(() => {
			Object.assign(activeSession.current, { modelOverride: 'opus', effortLevelOverride: 'high', effortRev: 2 });
			sessionEphemeralCache.save('live-knobs', { ...activeSession.current } as never);
		});
		afterEach(() => { activeSession.current.modelOverride = null; activeSession.current.effortRev = undefined; });

		it('holds the level at the revision it has, with no error, here and in the cached copy', async () => {
			const error = vi.spyOn(notify, 'error').mockImplementation(() => '');
			vi.spyOn(api, 'put').mockRejectedValue(refusal);
			await activeSession.setEffortLevelOverride('max');
			expect(error).not.toHaveBeenCalled();
			expect(activeSession.current.effortLevelOverride).toBe('max');
			// The next send carries max with revision 2, which the worker saves.
			expect(activeSession.current.effortRev).toBe(2);
			expect(sessionEphemeralCache.restore('live-knobs')).toMatchObject({ effortLevelOverride: 'max', effortRev: 2 });
		});

		it('does not hold a level the picked model lacks, Auto, or any other refusal', async () => {
			const error = vi.spyOn(notify, 'error').mockImplementation(() => '');
			const put = vi.spyOn(api, 'put').mockRejectedValue(refusal);
			await activeSession.setEffortLevelOverride('no-such-level');
			await activeSession.setEffortLevelOverride(null);
			put.mockRejectedValue({ status: 409, detail: 'The session is still connecting. Try again in a moment.' });
			await activeSession.setEffortLevelOverride('max');
			put.mockRejectedValue({ status: 503, detail: refusal.detail });
			await activeSession.setEffortLevelOverride('max');
			expect(error).toHaveBeenCalledTimes(4);
			expect(activeSession.current.effortLevelOverride).toBe('high');
		});

		it('an announcement no newer than the held revision leaves the held level (review S7)', async () => {
			vi.spyOn(api, 'put').mockRejectedValue(refusal);
			await activeSession.setEffortLevelOverride('max');
			announce({ effort: 'low', effort_rev: 2 });
			announce({ effort: 'low', effort_rev: 1 });
			expect(activeSession.current.effortLevelOverride).toBe('max');
			expect(activeSession.current.effortRev).toBe(2);
		});

		it('a level saved elsewhere since replaces it', async () => {
			vi.spyOn(api, 'put').mockRejectedValue(refusal);
			await activeSession.setEffortLevelOverride('max');
			announce({ effort: 'low', effort_rev: 3 });
			expect(activeSession.current.effortLevelOverride).toBe('low');
			expect(activeSession.current.effortRev).toBe(3);
		});

		it('a level saved elsewhere while the refused request was under way stands', async () => {
			let refuse!: (reason: unknown) => void;
			vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise((_resolve, reject) => { refuse = reject; }));
			const pending = activeSession.setEffortLevelOverride('max');
			announce({ effort: 'low', effort_rev: 3 });
			refuse(refusal);
			await pending;
			expect(activeSession.current.effortLevelOverride).toBe('low');
			expect(activeSession.current.effortRev).toBe(3);
		});

		it('the delayed answer of an earlier pick never replaces the held newer level (review S10)', async () => {
			const low = deferred();
			vi.spyOn(api, 'put').mockImplementationOnce(() => low.promise).mockRejectedValueOnce(refusal);
			const first = activeSession.setEffortLevelOverride('low');
			const second = activeSession.setEffortLevelOverride('max');
			low.acknowledge({ effort: 'low', effort_rev: 3, fast: false });
			await Promise.all([first, second]);
			expect(activeSession.current.effortLevelOverride).toBe('max');
			// The earlier pick was saved at 3: the held level sits on that revision,
			// so the send matches and the announcement of that write is not news.
			expect(activeSession.current.effortRev).toBe(3);
			announce({ effort: 'low', effort_rev: 3 });
			expect(activeSession.current.effortLevelOverride).toBe('max');
		});

		it('ends the same when the announcement of the earlier pick arrives before its answer', async () => {
			const low = deferred();
			vi.spyOn(api, 'put').mockImplementationOnce(() => low.promise).mockRejectedValueOnce(refusal);
			const first = activeSession.setEffortLevelOverride('low');
			const second = activeSession.setEffortLevelOverride('max');
			announce({ effort: 'low', effort_rev: 3 });
			low.acknowledge({ effort: 'low', effort_rev: 3, fast: false });
			await Promise.all([first, second]);
			expect(activeSession.current.effortLevelOverride).toBe('max');
			expect(activeSession.current.effortRev).toBe(3);
		});

		it('a saved earlier pick stands when the pick after it fails for another reason', async () => {
			vi.spyOn(notify, 'error').mockImplementation(() => '');
			const low = deferred();
			vi.spyOn(api, 'put').mockImplementationOnce(() => low.promise).mockRejectedValueOnce(new TypeError('Failed to fetch'));
			const first = activeSession.setEffortLevelOverride('low');
			const second = activeSession.setEffortLevelOverride('medium');
			low.acknowledge({ effort: 'low', effort_rev: 3, fast: false });
			await Promise.all([first, second]);
			// The worker saved low at 3 and nothing since: the composer says so.
			expect(activeSession.current.effortLevelOverride).toBe('low');
			expect(activeSession.current.effortRev).toBe(3);
		});

		it('the answer of a pick made for another model records its revision and keeps the new model\'s level', async () => {
			const low = deferred();
			vi.spyOn(api, 'put').mockImplementationOnce(() => low.promise);
			const first = activeSession.setEffortLevelOverride('low');
			activeSession.setModelOverride('sonnet');
			const shown = activeSession.current.effortLevelOverride;
			low.acknowledge({ effort: 'low', effort_rev: 3, fast: false });
			await first;
			expect(activeSession.current.effortLevelOverride).toBe(shown);
			expect(activeSession.current.effortRev).toBe(3);
		});
	});

	// #1640: the saved window has its own revision; the same ordering rule.
	describe('the context-window pin and its revision (#1640)', () => {
		beforeEach(() => Object.assign(activeSession.current, {
			modelOverride: 'fable', contextWindowChoice: '1m', contextWindowPin: 1_000_000, contextWindowRev: 2,
		}));
		afterEach(() => Object.assign(activeSession.current, { modelOverride: null, contextWindowPin: undefined, contextWindowRev: undefined }));

		it('holds the revision the worker answered a window pick with', async () => {
			const patchRow = vi.spyOn(appData, 'mergeSessionPatch');
			vi.spyOn(api, 'put').mockResolvedValueOnce({ context_window: 200_000, context_window_rev: 3 });
			await activeSession.setContextWindowChoice('standard');
			expect(activeSession.current).toMatchObject({ contextWindowChoice: 'standard', contextWindowPin: 200_000, contextWindowRev: 3 });
			expect(patchRow).toHaveBeenCalledWith('live-knobs', { context_window: 200_000, context_window_rev: 3 });
		});

		it('an answer that arrives after a newer pin does not undo it', async () => {
			let acknowledge!: (value: { context_window: number; context_window_rev: number }) => void;
			vi.spyOn(api, 'put').mockImplementationOnce(() => new Promise(resolve => { acknowledge = resolve; }));
			const pending = activeSession.setContextWindowChoice('standard');
			// A model saved since dropped the pin (revision 4); its push got here first.
			hoisted.onMessage!({ event_type: 'session_updated', session_id: 'live-knobs',
				data: { updates: { context_window: null, context_window_rev: 4 } } });
			acknowledge({ context_window: 200_000, context_window_rev: 3 });
			await pending;
			expect(activeSession.current.contextWindowPin).toBeNull();
			expect(activeSession.current.contextWindowRev).toBe(4);
		});

		it('takes a newer pin, a dropped one included, from the reloaded row of the open chat', () => {
			// A model saved by any writer on any worker may have dropped the pin:
			// the row is how this page learns, and it names no older pin twice.
			expect(activeSession.adoptRowEffort([{ id: 'live-knobs', context_window: null, context_window_rev: 2 }])).toBe(false);
			expect(activeSession.adoptRowEffort([{ id: 'another-chat', context_window: null, context_window_rev: 9 }])).toBe(false);
			expect(activeSession.current.contextWindowPin).toBe(1_000_000);
			expect(activeSession.adoptRowEffort([{ id: 'live-knobs', context_window: null, context_window_rev: 3 }])).toBe(true);
			expect(activeSession.current).toMatchObject({ contextWindowPin: null, contextWindowRev: 3, contextWindowChoice: '1m' });
			expect(activeSession.adoptRowEffort([{ id: 'live-knobs', context_window: 200_000, context_window_rev: 4 }])).toBe(true);
			expect(activeSession.current).toMatchObject({ contextWindowPin: 200_000, contextWindowRev: 4, contextWindowChoice: 'standard' });
			// A list response older than what is held changes nothing.
			expect(activeSession.adoptRowEffort([{ id: 'live-knobs', context_window: 1_000_000, context_window_rev: 2 }])).toBe(false);
			expect(activeSession.current.contextWindowPin).toBe(200_000);
		});
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

// "New chat" tells a reload of the tab to stay on the new-session screen. The
// flag outlived the first send: once the new chat existed, a reload (say mid
// reply) dropped the user on an empty new chat instead of the chat they were
// in. Found by the reload-midstream journey.
describe('a reload after a new chat is sent', () => {
	const SKIP = '__aihub_skipSessionRestore';
	afterEach(() => {
		vi.useRealTimers();
		sessionStorage.removeItem(SKIP);
		sessionStorage.removeItem('ai-shuffle-active-session');
	});

	it('keeps the new-session screen while the chat is unsent', async () => {
		vi.useFakeTimers();
		activeSession.current.sessionId = null;
		activeSession.newChat();
		await vi.advanceTimersByTimeAsync(1000);
		expect(sessionStorage.getItem(SKIP)).toBe('1');
	});

	it('restores the chat once its first send created it', async () => {
		vi.useFakeTimers();
		activeSession.current.sessionId = null;
		activeSession.newChat();
		expect(sessionStorage.getItem(SKIP)).toBe('1');
		// The first send adopts the server's new session id and persists it.
		activeSession.current.sessionId = 'created-by-first-send';
		activeSession.setTitle('created-by-first-send', 'first message');
		await vi.advanceTimersByTimeAsync(1000);
		expect(JSON.parse(sessionStorage.getItem('ai-shuffle-active-session')!).sessionId).toBe('created-by-first-send');
		expect(sessionStorage.getItem(SKIP)).toBeNull();
	});
});
