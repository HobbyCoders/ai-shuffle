import { describe, expect, it, vi, beforeEach } from 'vitest';

// The SSE feed is started from the restore branch now (finding S5), so the
// singleton is mocked to observe that without opening a real stream.
vi.mock('./v2/feed-controller', () => ({
	feedController: {
		start: vi.fn(),
		stop: vi.fn(),
		isFeeding: vi.fn(() => false),
	},
}));

import { bumpInitGeneration, createSessionInit, setSkipRestore, tabWillRestoreChat, type SessionInitDeps } from './session-init';
import { feedController } from './v2/feed-controller';
import type { ActiveSessionState, PersistedSession } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';

/**
 * Restored-session race (e2e Bug A): init() adopts a persisted sessionId before
 * its history has rendered, leaving the UI in a "New Chat"-looking state that
 * silently continues an old session. init() must mark the window with
 * isRestoringSession=true so ChatView can suppress the empty state and
 * sendMessage can wait for the restore to settle.
 */

function baseState(overrides: Partial<ActiveSessionState> = {}): ActiveSessionState {
	return {
		sessionId: null,
		title: 'New Chat',
		messages: [],
		isStreaming: false,
		wsConnected: false,
		error: null,
		profile: '',
		project: '',
		isRestoringSession: false,
		...overrides,
	} as unknown as ActiveSessionState;
}

function makeInit(
	persisted: PersistedSession | null,
	loadPersistedSession = vi.fn(async () => persisted),
) {
	let state = baseState();
	const update = (fn: (s: ActiveSessionState) => ActiveSessionState) => { state = fn(state); };
	const getState = () => state;

	const internal: InternalMutableState = {
		pendingTools: new Set<string>(),
		awaitingNewSession: false,
		pendingRequestId: null,
		stopRequestedBeforeSession: false,
		worktreeCreationGeneration: 0,
		worktreeCreateAbort: null,
	};

	const deps: SessionInitDeps = {
		update,
		getState,
		notifySubscribers: vi.fn(),
		defaultState: () => baseState(),
		setState: (s: ActiveSessionState) => { state = s; },
		internal,
		wsConn: {
			activate: vi.fn(),
			connect: vi.fn(),
			disconnect: vi.fn(),
			destroy: vi.fn(),
		},
		switcher: {
			loadSelectionFromBackend: vi.fn(async () => null),
			loadPersistedSession,
			getPersistedProfile: () => 'profile-1',
			getPersistedProject: () => 'proj-1',
			getPersistedWorktreeIntent: () => 'local',
			refreshSessionMetadata: vi.fn(async () => {}),
			destroy: vi.fn(),
		},
		appData: { loadSessions: vi.fn() },
		disconnect: vi.fn(),
		persistSession: vi.fn(),
		persistTimer: { get: () => null, clear: vi.fn() },
	};

	return { ...createSessionInit(deps), getState, deps };
}

describe('session-init · deep link replaces the restored chat (#1923 follow-up)', () => {
	beforeEach(() => {
		sessionStorage.clear();
		vi.mocked(feedController.start).mockClear();
	});

	it('binds the linked chat before init awaits anything, never the previous one', async () => {
		const load = vi.fn(async () => ({ sessionId: 'chat-a', title: 'Chat A', project: 'proj-a' } as PersistedSession));
		const { init, getState, deps } = makeInit(null, load);
		const initializing = init({ deepLinkSessionId: 'chat-b' });
		// Synchronously, before the first await settles.
		expect(getState().sessionId).toBe('chat-b');
		expect(getState().title).toBe('');
		expect(getState().isRestoringSession).toBe(true);
		expect(feedController.start).toHaveBeenCalledWith('chat-b');
		expect(deps.switcher.refreshSessionMetadata).toHaveBeenCalledWith('chat-b');
		await initializing;
		// The tab's previous chat is not even read, let alone adopted.
		expect(load).not.toHaveBeenCalled();
		expect(feedController.start).not.toHaveBeenCalledWith('chat-a');
		expect(getState().sessionId).toBe('chat-b');
		expect(getState().title).toBe('');
		expect(getState().isRestoringSession).toBe(true);
	});

	it('leaves the agent and the project for the linked chat\'s own row (#418)', async () => {
		const { init, getState } = makeInit(null);
		await init({ deepLinkSessionId: 'chat-b' });
		// The device default agent is not this chat's: /live fills it.
		expect(getState().profile).toBe('');
		expect(getState().bindingUnhydrated).toEqual({ profile: true, permission: true, fast: true });
		// A device default here would block /live's project as an in-flight pick.
		expect(getState().project).toBe('');
	});

	it('opens the linked chat on its own binding, not the tab\'s last-used picks', async () => {
		// A fresh tab is seeded with the last compose picks (aihub_lastModel …).
		const { init, getState, deps } = makeInit(null);
		deps.setState(baseState({
			modelOverride: 'fable', effortLevelOverride: 'high', permissionModeOverride: 'acceptEdits',
			contextWindowChoice: 'standard', contextWindowPin: 200_000,
		} as Partial<ActiveSessionState>));
		let atRefresh: Partial<ActiveSessionState> | null = null;
		vi.mocked(deps.switcher.refreshSessionMetadata).mockImplementation(async () => {
			const { modelOverride, effortLevelOverride, permissionModeOverride, contextWindowChoice, contextWindowPin } = getState();
			atRefresh = { modelOverride, effortLevelOverride, permissionModeOverride, contextWindowChoice, contextWindowPin };
		});
		await init({ deepLinkSessionId: 'chat-b' });
		// Cleared before /live snapshots them, so the row fills every one.
		expect(atRefresh).toEqual({
			modelOverride: null, effortLevelOverride: null, permissionModeOverride: null,
			contextWindowChoice: '1m', contextWindowPin: undefined,
		});
		expect(getState()).toMatchObject({ sessionId: 'chat-b', modelOverride: null, effortLevelOverride: null });
	});

	it('wins over a pending skip-restore from an earlier New Chat', async () => {
		sessionStorage.setItem('__aihub_skipSessionRestore', '1');
		const { init, getState } = makeInit(null);
		await init({ deepLinkSessionId: 'chat-b' });
		expect(getState().sessionId).toBe('chat-b');
	});
});

describe('session-init · isRestoringSession on persisted restore', () => {
	beforeEach(() => {
		sessionStorage.clear();
	});

	it('hydrates the restored session binding even when no session switch occurs', async () => {
		const { init, deps } = makeInit({ sessionId: 'restored-parent', modelOverride: null, effortLevelOverride: null } as PersistedSession);
		await init();
		expect(deps.switcher.refreshSessionMetadata).toHaveBeenCalledWith('restored-parent');
	});

	it('a reloaded chat with no saved binding never takes the tab\'s last-used picks', async () => {
		const { init, getState, deps } = makeInit({ sessionId: 'restored-parent', title: 'Chat' } as PersistedSession);
		deps.setState(baseState({ modelOverride: 'fable', effortLevelOverride: 'high', contextWindowChoice: 'standard' } as Partial<ActiveSessionState>));
		await init();
		expect(getState()).toMatchObject({
			sessionId: 'restored-parent', modelOverride: null, effortLevelOverride: null, contextWindowChoice: '1m',
		});
	});

	it('explicitly reactivates transport after each owner remount', async () => {
		const { init, destroy, deps } = makeInit(null);
		await init();
		destroy();
		await init();
		expect(deps.wsConn.activate).toHaveBeenCalledTimes(2);
		expect(deps.wsConn.connect).toHaveBeenCalledTimes(2);
		expect(deps.wsConn.activate).toHaveBeenCalledBefore(vi.mocked(deps.wsConn.connect));
	});

	it('does not reactivate or restore state when an initialization finishes after destruction', async () => {
		let resolve!: (value: PersistedSession | null) => void;
		const load = vi.fn(() => new Promise<PersistedSession | null>(done => { resolve = done; }));
		const { init, destroy, deps, getState } = makeInit(null, load);
		const initializing = init();
		destroy();
		resolve({ sessionId: 'stale-session' } as PersistedSession);
		await initializing;
		expect(deps.wsConn.activate).not.toHaveBeenCalled();
		expect(deps.wsConn.connect).not.toHaveBeenCalled();
		expect(getState().sessionId).toBeNull();
	});

	it('ignores a stale failed initialization after a later remount has succeeded', async () => {
		let reject!: (error: Error) => void;
		const load = vi.fn<() => Promise<PersistedSession | null>>()
			.mockImplementationOnce(() => new Promise((_resolve, fail) => { reject = fail; }))
			.mockResolvedValue(null);
		const { init, destroy, deps } = makeInit(null, load);
		const first = init();
		destroy();
		await init();
		reject(new Error('Old request failed'));
		await first;
		expect(deps.wsConn.activate).toHaveBeenCalledTimes(1);
		expect(deps.wsConn.connect).toHaveBeenCalledTimes(1);
	});

	it('marks the restore window when a persisted sessionId is adopted', async () => {
		const { init, getState } = makeInit({
			sessionId: 'persisted-1',
			title: 'Old chat',
			profile: 'profile-1',
			project: 'proj-1',
			modelOverride: null,
			permissionModeOverride: null,
			effortLevelOverride: null,
		});

		await init();

		const s = getState();
		expect(s.sessionId).toBe('persisted-1');
		expect(s.isRestoringSession).toBe(true);
	});

	it('a delayed persisted restore cannot replace explicit navigation', async () => {
		let release!: (value: PersistedSession | null) => void;
		const delayed = new Promise<PersistedSession | null>((resolve) => {
			release = resolve;
		});
		const { init, getState, deps } = makeInit(
			null,
			vi.fn(() => delayed),
		);

		const initializing = init();
		// Let init pass its first await and begin the persisted-session read.
		await Promise.resolve();
		deps.update((s) => ({ ...s, sessionId: 'explicit-target', title: 'Chosen' }));
		bumpInitGeneration();
		release({
			sessionId: 'stale-persisted',
			title: 'Old chat',
			profile: 'profile-1',
			project: 'proj-1',
			modelOverride: null,
			permissionModeOverride: null,
			effortLevelOverride: null,
		});
		await initializing;

		expect(getState().sessionId).toBe('explicit-target');
		expect(getState().title).toBe('Chosen');
	});

	it.each([null, 'existing'])('keeps an existing worktree binding but does not restore a new composer’s unscoped intent (session=%s)', async sessionId => {
		const {init,getState}=makeInit({sessionId, title:'Chat', profile:'p', project:'',
		 worktreeIntent:'worktree', worktreeId:'wt', worktreeBranch:'session/branch'} as PersistedSession);
		await init();
		expect(getState().worktreeIntent).toBe(sessionId ? 'worktree' : 'local');
		if (sessionId) expect(getState().worktreeId).toBe('wt');
	});

	it('does not mark the window when the persisted selection has no sessionId', async () => {
		const { init, getState } = makeInit({
			sessionId: null,
			title: 'New Chat',
			profile: 'profile-1',
			project: 'proj-1',
			modelOverride: null,
			permissionModeOverride: null,
			effortLevelOverride: null,
		});

		await init();

		expect(getState().isRestoringSession).toBe(false);
	});

	it('treats a persisted entry with an undefined sessionId as nothing to restore', async () => {
		// A persisted blob missing the key yields undefined, not null. The old
		// `!== null` check stranded isRestoringSession=true with nothing to
		// restore, permanently suppressing the empty state and blocking sends.
		const { init, getState } = makeInit({
			sessionId: undefined,
			title: 'New Chat',
			profile: 'profile-1',
			project: 'proj-1',
		} as unknown as PersistedSession);

		await init();

		expect(getState().sessionId).toBeNull();
		expect(getState().isRestoringSession).toBe(false);
	});

	it('does not mark the window when there is nothing persisted', async () => {
		const { init, getState } = makeInit(null);

		await init();

		expect(getState().sessionId).toBeNull();
		expect(getState().isRestoringSession).toBe(false);
	});

	it('restores a persisted fast-tier pick', async () => {
		const { init, getState } = makeInit({
			sessionId: 'persisted-fast',
			title: 'Fast chat',
			profile: 'profile-1',
			project: 'proj-1',
			modelOverride: 'opus',
			permissionModeOverride: null,
			effortLevelOverride: null,
			fastMode: true,
			bindingHydrated: { profile: true, permission: true, fast: true },
		});

		await init();

		expect(getState().fastMode).toBe(true);
		expect(getState().bindingUnhydrated).toEqual({});
	});

	it('does not trust a snapshot written before the binding record (#418)', async () => {
		// Such a snapshot may hold a worker's fabricated Ask / Fast off and the
		// previous chat's agent. They wait for /live instead of being sent.
		const { init, getState } = makeInit({
			sessionId: 'legacy-worker',
			title: 'Worker',
			profile: 'overseer',
			project: 'proj-1',
			modelOverride: null,
			permissionModeOverride: null,
			effortLevelOverride: null,
			fastMode: false,
		});

		await init();

		expect(getState().profile).toBe('');
		expect(getState().bindingUnhydrated).toEqual({ profile: true, permission: true, fast: true });
	});

	it('clears fast mode for a restored session that never stored one', async () => {
		// A session predating the flag must not inherit the seeded last-used pick —
		// same rule the rest of the run binding follows.
		const { init, getState } = makeInit({
			sessionId: 'persisted-old',
			title: 'Old chat',
			profile: 'profile-1',
			project: 'proj-1',
			modelOverride: null,
			permissionModeOverride: null,
			effortLevelOverride: null,
		});

		await init();

		expect(getState().fastMode).toBe(false);
	});

	it('does not replace a v2-only persisted session with the last local default', async () => {
		const { init, getState } = makeInit({
			sessionId: 'persisted-v2',
			title: 'Remote chat',
			profile: 'profile-1',
			project: '',
			projectV2Id: 'pv2-remote',
			modelOverride: null,
			permissionModeOverride: null,
			effortLevelOverride: null,
		});

		await init();

		const s = getState();
		expect(s.sessionId).toBe('persisted-v2');
		expect(s.project).toBe('');
		expect(s.projectV2Id).toBe('pv2-remote');
	});
});

// ─── S5: boot waterfall ─────────────────────────────────────────────────────

describe('session-init · boot waterfall (S5)', () => {
	beforeEach(() => {
		sessionStorage.clear();
		vi.mocked(feedController.start).mockClear();
	});

	function persisted(sessionId: string | null): PersistedSession {
		return {
			sessionId,
			title: 'Old chat',
			profile: 'profile-1',
			project: 'proj-1',
			modelOverride: null,
			permissionModeOverride: null,
			effortLevelOverride: null,
		} as PersistedSession;
	}

	it('reads the two independent preferences in PARALLEL, not in series', async () => {
		// Both reads are gated on the same tick: if init still awaited them one
		// after the other, the second would not have started when the first
		// resolves, and this test would hang on `started`.
		let releaseSelection!: () => void;
		const selectionGate = new Promise<void>((r) => { releaseSelection = r; });
		let sessionStarted = false;
		let releaseSession!: () => void;
		const sessionGate = new Promise<void>((r) => { releaseSession = r; });

		const loadSelectionFromBackend = vi.fn(async () => {
			await selectionGate;
			return null;
		});
		const loadPersistedSession = vi.fn(async () => {
			sessionStarted = true;
			await sessionGate;
			return persisted('persisted-1');
		});

		const { init, getState, deps } = makeInit(null, loadPersistedSession);
		deps.switcher.loadSelectionFromBackend = loadSelectionFromBackend;

		const running = init();
		await Promise.resolve();
		// The persisted-session read is already in flight while the selection
		// read is still pending — that is the whole point of the fix.
		expect(sessionStarted).toBe(true);

		releaseSelection();
		releaseSession();
		await running;
		expect(getState().sessionId).toBe('persisted-1');
	});

	it('skips the persisted read entirely when restore is skipped', async () => {
		sessionStorage.setItem('__aihub_skipSessionRestore', '1');
		const loadPersistedSession = vi.fn(async () => persisted('persisted-1'));
		const { init } = makeInit(null, loadPersistedSession);

		await init();

		expect(loadPersistedSession).not.toHaveBeenCalled();
		expect(feedController.start).not.toHaveBeenCalled();
	});

	it('starts the SSE feed as soon as the restored sessionId is known', async () => {
		// The feed is a cookie-authed fetch; it never needed the WS handshake.
		const { init, deps } = makeInit(persisted('persisted-1'));

		await init();

		expect(feedController.start).toHaveBeenCalledWith('persisted-1');
		expect(deps.wsConn.connect).toHaveBeenCalled();
		// …and the feed start must not have waited on the socket.
		const startOrder = vi.mocked(feedController.start).mock.invocationCallOrder[0];
		const connectOrder = vi.mocked(deps.wsConn.connect).mock.invocationCallOrder[0];
		expect(startOrder).toBeLessThan(connectOrder);
	});

	it('never starts a feed when there is no persisted session', async () => {
		const { init } = makeInit(persisted(null));
		await init();
		expect(feedController.start).not.toHaveBeenCalled();
	});
});

describe('session-init · the restored session keeps its own lane', () => {
	beforeEach(() => {
		sessionStorage.clear();
	});

	function persistedIn(mode: string | undefined, sessionId: string | null = 'persisted-1'): PersistedSession {
		return {
			sessionId,
			mode,
			title: 'Old chat',
			profile: 'profile-1',
			project: 'proj-1',
			modelOverride: null,
			permissionModeOverride: null,
			effortLevelOverride: null,
		} as PersistedSession;
	}

	it('restores the snapshot lane instead of the sidepanel pick', async () => {
		// `appMode` is device-wide localStorage: another tab moving the switcher
		// to Chat used to repaint THIS tab's restored code session as a chat one
		// on reload, dropping the whole context pill bar off a code session.
		const { init, getState, deps } = makeInit(persistedIn('code'));
		deps.update((s) => ({ ...s, mode: 'chat' }));

		await init();

		expect(getState().mode).toBe('code');
	});

	it('restores a chat session into the chat lane from a code sidepanel', async () => {
		const { init, getState, deps } = makeInit(persistedIn('chat'));
		deps.update((s) => ({ ...s, mode: 'code' }));

		await init();

		expect(getState().mode).toBe('chat');
	});

	it('lands a pre-modes snapshot in the code lane', async () => {
		const { init, getState, deps } = makeInit(persistedIn(undefined));
		deps.update((s) => ({ ...s, mode: 'voice' }));

		await init();

		expect(getState().mode).toBe('code');
	});

	it.each([
		['proj-1', 'code'], ['', 'chat'],
	] as const)('takes an unbound compose\'s kind from its project (%j → %s)', async (project, expected) => {
		// No sessionId means no row to defer to. One kind of session (#371
		// step 4): a compose with a project is a project session, else not.
		const { init, getState, deps } = makeInit({ ...persistedIn('chat', null), project } as PersistedSession);
		deps.update((s) => ({ ...s, mode: expected === 'code' ? 'chat' : 'code' }));

		await init();

		expect(getState().mode).toBe(expected);
		expect(getState().project).toBe(project);
	});
});

/**
 * A reload painted the new-chat screen for up to 1.5s before the tab's chat
 * came back: the restore window opened only after init's first round trip.
 * The store now starts in the window when this load will reopen a chat
 * (tabWillRestoreChat), and init closes it whenever nothing is restored.
 * Found by the reload-midstream journey.
 */
describe('session-init · the restore window from the first frame', () => {
	beforeEach(() => {
		sessionStorage.clear();
		window.history.replaceState(null, '', '/');
	});

	it('expects a restore when this tab saved a chat', () => {
		expect(tabWillRestoreChat()).toBe(false);
		sessionStorage.setItem('ai-shuffle-active-session', JSON.stringify({ sessionId: 'tab-chat' }));
		expect(tabWillRestoreChat()).toBe(true);
	});

	it('expects none for an unsent new chat, a saved compose or a corrupt slot', () => {
		sessionStorage.setItem('ai-shuffle-active-session', JSON.stringify({ sessionId: 'tab-chat' }));
		setSkipRestore(true);
		expect(tabWillRestoreChat()).toBe(false);
		setSkipRestore(false);
		sessionStorage.setItem('ai-shuffle-active-session', JSON.stringify({ sessionId: null, title: 'New Chat' }));
		expect(tabWillRestoreChat()).toBe(false);
		sessionStorage.setItem('ai-shuffle-active-session', '{not json');
		expect(tabWillRestoreChat()).toBe(false);
	});

	it('expects a restore for a link, whatever the tab saved', () => {
		setSkipRestore(true);
		window.history.replaceState(null, '', '/?session=linked');
		expect(tabWillRestoreChat()).toBe(true);
	});

	it('init closes the window when there is nothing to restore', async () => {
		const { init, getState, deps } = makeInit(null);
		deps.setState(baseState({ isRestoringSession: true }));
		await init();
		expect(getState().isRestoringSession).toBe(false);
	});

	it('init closes it on the "New chat" path too', async () => {
		setSkipRestore(true);
		const { init, getState, deps } = makeInit({ sessionId: 'old' } as PersistedSession);
		deps.setState(baseState({ isRestoringSession: true }));
		await init();
		expect(getState().isRestoringSession).toBe(false);
		expect(getState().sessionId).toBeNull();
	});

	it('init closes it when it fails before adopting a chat', async () => {
		const { init, getState, deps } = makeInit(null, vi.fn(async () => { throw new Error('offline'); }));
		deps.setState(baseState({ isRestoringSession: true }));
		await init();
		expect(getState().isRestoringSession).toBe(false);
	});

	it('keeps it open while the saved chat is being restored', async () => {
		const { init, getState, deps } = makeInit({ sessionId: 'tab-chat', title: 'Chat' } as PersistedSession);
		deps.setState(baseState({ isRestoringSession: true }));
		await init();
		expect(getState()).toMatchObject({ sessionId: 'tab-chat', isRestoringSession: true });
	});
});
