import { describe, expect, it, vi, beforeEach } from 'vitest';
import { createSessionInit, type SessionInitDeps } from './session-init';
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

function makeInit(persisted: PersistedSession | null) {
	let state = baseState();
	const update = (fn: (s: ActiveSessionState) => ActiveSessionState) => { state = fn(state); };
	const getState = () => state;

	const internal: InternalMutableState = {
		pendingTools: new Set<string>(),
		interruptPending: false,
		awaitingNewSession: false,
		pendingRequestId: null,
		stopRequestedBeforeSession: false,
		stopBackgroundRequested: false,
		worktreeCreationGeneration: 0,
		worktreeCreateAbort: null,
		interruptConfirmResolve: null,
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
			loadPersistedSession: vi.fn(async () => persisted),
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
});
