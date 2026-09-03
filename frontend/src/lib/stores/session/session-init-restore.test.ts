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

import { bumpInitGeneration, createSessionInit, type SessionInitDeps } from './session-init';
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
			connect: vi.fn(),
			disconnect: vi.fn(),
			destroy: vi.fn(),
		},
		switcher: {
			loadSelectionFromBackend: vi.fn(async () => null),
			loadPersistedSession,
			getPersistedProfile: () => 'profile-1',
			getPersistedProject: () => 'proj-1',
			destroy: vi.fn(),
		},
		appData: { loadSessions: vi.fn() },
		disconnect: vi.fn(),
		persistSession: vi.fn(),
		persistTimer: { get: () => null, clear: vi.fn() },
	};

	return { init: createSessionInit(deps).init, getState, deps };
}

describe('session-init · isRestoringSession on persisted restore', () => {
	beforeEach(() => {
		sessionStorage.clear();
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
		});

		await init();

		expect(getState().fastMode).toBe(true);
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

	it('leaves an unbound compose on the sidepanel lane', async () => {
		// No sessionId means no session to defer to — there, the sidepanel's
		// pick IS the truth, and newChat has already stamped it.
		const { init, getState, deps } = makeInit(persistedIn('code', null));
		deps.update((s) => ({ ...s, mode: 'chat' }));

		await init();

		expect(getState().mode).toBe('chat');
	});
});
