import { describe, expect, it, vi, beforeEach } from 'vitest';
import { createSessionSwitcher, type SessionSwitcherDeps } from './session-switcher';
import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import type { SessionEphemeral } from '../session-cache';

// session-switcher looks up the session row in appData to tailor the load-error
// message for remote (cross-install) sessions (B4). Mock it so tests can seed the
// row without pulling in the real runes store / API layer.
const _mockSessions: { current: Array<Record<string, unknown>> } = { current: [] };
vi.mock('../app-data.svelte', () => ({
	appData: {
		get current() {
			return { sessions: _mockSessions.current };
		},
	},
}));

/**
 * Regression tests for cross-session state bleed on switch.
 *
 * switchSession() reuses a single activeSession store for whatever session is
 * on screen. If it fails to reset a per-session field, the session being LEFT
 * leaks its value into the session being switched TO (wrong profile used for the
 * next message, stale token/context usage, lingering stream timer, etc.).
 */

function fullState(overrides: Partial<ActiveSessionState> = {}): ActiveSessionState {
	return {
		sessionId: 'session-A',
		title: 'Session A',
		messages: [{ id: 'm1', role: 'user', content: 'hi' }],
		isStreaming: false,
		wsConnected: true,
		error: null,
		profile: 'profile-global',
		sessionProfileId: 'profile-A',
		currentQueryProfileId: 'profile-A-query',
		project: 'proj-A',
		totalTokensIn: 5000,
		totalTokensOut: 4000,
		totalCacheCreationTokens: 3000,
		totalCacheReadTokens: 2000,
		contextUsed: 14000,
		contextMax: 1_000_000,
		modelOverride: 'model-A',
		permissionModeOverride: 'plan',
		effortLevelOverride: 'high',
		pendingPermissions: [],
		deferredPermissions: [],
		pendingQuestions: [],
		queuedMessages: [],
		activeTasks: [],
		draft: 'draft from session A',
		scrollTop: 250,
		streamStartTime: 123456,
		executionMode: 'local',
		cloudTargetInstanceId: null,
		cloudWorktree: false,
		cloudBranch: null,
		cloudCreateNewBranch: false,
		worktreeBranch: '',
		worktreeBaseBranch: 'main',
		worktreeCreateNewBranch: true,
		worktreeId: null,
		worktreeMode: 'new',
		selectedWorktreeBranch: null,
		isCreatingWorktree: false,
		worktreeError: null,
		worktreeDeleted: false,
		isReopeningWorktree: false,
		sessionLocked: false,
		sessionLockedBy: undefined,
		wsStreamContent: '',
		pendingInterruptPrompt: null,
		remoteControllable: true,
		relayError: undefined,
		isRestoringSession: false,
		...overrides,
	};
}

function makeSwitcher(initial: ActiveSessionState, opts: { loadSessionNull?: boolean } = {}) {
	let state = initial;
	const update = (fn: (s: ActiveSessionState) => ActiveSessionState) => { state = fn(state); };
	const getState = () => state;

	const internal: InternalMutableState = {
		pendingTools: new Set<string>(),
		interruptPending: false,
		awaitingNewSession: false,
		pendingRequestId: null,
		stopRequestedBeforeSession: false,
		worktreeCreationGeneration: 0,
		worktreeCreateAbort: null,
		interruptConfirmResolve: null,
	};

	const ephemeralStore = new Map<string, SessionEphemeral>();

	const deps: SessionSwitcherDeps = {
		update,
		getState,
		wsSend: vi.fn(() => true),
		internal,
		sessionEphemeralCache: {
			save: (id, e) => { ephemeralStore.set(id, e); },
			restore: (id) => ephemeralStore.get(id) ?? null,
		},
		sessionPoller: {
			stop: vi.fn(),
			// Return a non-null, non-streaming poll result so switchSession does
			// not fall into its error branch — unless the test asks for the null
			// (load-failed) path via opts.loadSessionNull.
			loadSession: vi.fn(async () =>
				opts.loadSessionNull
					? null
					: {
						messages: [],
						isStreaming: false,
						lastUpdateId: 0,
						usage: { inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 },
					}
			) as unknown as SessionSwitcherDeps['sessionPoller']['loadSession'],
			startStreaming: vi.fn(),
		},
		sessionState: { removeSession: vi.fn() },
		onPersistTimerChanged: vi.fn(),
	};

	const switcher = createSessionSwitcher(deps);
	return { switcher, getState, ephemeralStore, deps };
}

describe('session-switcher · switchSession isolation', () => {
	it('resets per-session runtime state so the leaving session does not bleed in', async () => {
		const { switcher, getState } = makeSwitcher(fullState());

		// Switch to a session with no cached ephemeral state.
		await switcher.switchSession('session-B');
		switcher.destroy(); // cancel the debounced persist timer

		const s = getState();
		expect(s.sessionId).toBe('session-B');

		// Per-session fields must come back to defaults, NOT session A's values.
		expect(s.sessionProfileId).toBeNull();
		expect(s.currentQueryProfileId).toBeNull();
		expect(s.totalTokensIn).toBe(0);
		expect(s.totalTokensOut).toBe(0);
		expect(s.totalCacheCreationTokens).toBe(0);
		expect(s.totalCacheReadTokens).toBe(0);
		expect(s.contextMax).toBe(200_000);
		expect(s.contextUsed).toBeNull();
		expect(s.streamStartTime).toBeNull();
		expect(s.worktreeCreateNewBranch).toBe(false);

		// Overrides and draft default on a cache miss (never inherit session A's).
		expect(s.modelOverride).toBeNull();
		expect(s.permissionModeOverride).toBeNull();
		expect(s.effortLevelOverride).toBeNull();
		expect(s.draft).toBe('');
	});

	it('clears the leaving session running tasks so they do not flash on the target', async () => {
		// Reproduces the worktree bug: another worktree's running tasks linger in
		// the shared store and flash in the ActiveTasks strip before the new
		// session's first poll overwrites them.
		const leavingTasks = [{
			id: 't1',
			subject: 'Build the project',
			status: 'running' as const,
			kind: 'shell' as const,
			summary: null,
			toolUseId: null,
			startedAt: '2026-06-26T00:00:00Z',
			updatedAt: '2026-06-26T00:00:01Z',
		}];
		const { switcher, getState } = makeSwitcher(fullState({ activeTasks: leavingTasks }));

		await switcher.switchSession('session-B');
		switcher.destroy();

		expect(getState().activeTasks).toEqual([]);
	});

	it('restores the target session ephemeral state instead of resetting blindly', async () => {
		const { switcher, getState, ephemeralStore } = makeSwitcher(fullState());

		ephemeralStore.set('session-B', {
			title: 'Session B',
			draft: 'draft from session B',
			scrollTop: 99,
			project: 'proj-B',
			modelOverride: 'model-B',
			permissionModeOverride: null,
			effortLevelOverride: null,
			executionMode: 'local',
			worktreeId: null,
			worktreeBranch: '',
			worktreeBaseBranch: 'main',
			selectedWorktreeBranch: null,
			worktreeMode: 'new',
			worktreeDeleted: false,
			cloudBranch: null,
		});

		await switcher.switchSession('session-B');
		switcher.destroy();

		const s = getState();
		expect(s.draft).toBe('draft from session B');
		expect(s.scrollTop).toBe(99);
		expect(s.modelOverride).toBe('model-B');
		// Runtime usage still resets even when ephemeral UI state is restored.
		expect(s.totalTokensIn).toBe(0);
		expect(s.currentQueryProfileId).toBeNull();
	});

	it('saves the leaving session draft so returning to it restores the text', async () => {
		const { switcher, getState, ephemeralStore } = makeSwitcher(fullState({ draft: 'unsent A text' }));

		await switcher.switchSession('session-B');
		switcher.destroy();

		// Leaving-session ephemeral captured A's draft (later refined by ChatInput
		// via saveDraftForSession, but the store value is the baseline).
		expect(ephemeralStore.get('session-A')?.draft).toBe('unsent A text');
		// And the target session shows its own (empty) draft, not A's.
		expect(getState().draft).toBe('');
	});

	it('requests pending questions/permissions on switch even when NOT streaming', async () => {
		// A session parked on an AskUserQuestion/permission reports isStreaming=false.
		// Gating the resync on isStreaming (the old bug) hid the modal on switch. The
		// resync must fire unconditionally so the parked prompt surfaces immediately.
		const { switcher, deps } = makeSwitcher(fullState());
		// makeSwitcher's loadSession already returns isStreaming: false.

		await switcher.switchSession('session-B');
		switcher.destroy();

		expect(deps.wsSend).toHaveBeenCalledWith(
			expect.objectContaining({ type: 'get_pending_questions' })
		);
		expect(deps.wsSend).toHaveBeenCalledWith(
			expect.objectContaining({ type: 'get_pending_permissions' })
		);
	});
});

describe('session-switcher · load-failure error message (B4)', () => {
	beforeEach(() => {
		_mockSessions.current = [];
	});

	it('shows the generic "start a new chat" message for a LOCAL session that fails to load', async () => {
		_mockSessions.current = [{ id: 'session-B', is_remote: false }];
		const { switcher, getState } = makeSwitcher(fullState(), { loadSessionNull: true });

		await switcher.switchSession('session-B');
		switcher.destroy();

		expect(getState().error).toBe('Failed to load session. Try starting a new chat.');
	});

	it('shows a "could not reach the owning device" message when a REMOTE session fails to load', async () => {
		_mockSessions.current = [{ id: 'session-B', is_remote: true }];
		const { switcher, getState } = makeSwitcher(fullState(), { loadSessionNull: true });

		await switcher.switchSession('session-B');
		switcher.destroy();

		expect(getState().error).toBe('Could not reach the owning device — try again');
	});

	it('names the owning device in the reach-error when device_name is known', async () => {
		_mockSessions.current = [{ id: 'session-B', is_remote: true, device_name: 'Studio Mac' }];
		const { switcher, getState } = makeSwitcher(fullState(), { loadSessionNull: true });

		await switcher.switchSession('session-B');
		switcher.destroy();

		expect(getState().error).toBe('Could not reach Studio Mac — try again');
	});
});

/**
 * isRestoringSession lifecycle (e2e Bug A): the restore flag set by init() must
 * never survive a user-driven navigation. switchSession loads the target's
 * history itself and newChat leaves no session to restore — a lingering flag
 * would make ChatView hide the empty state and sendMessage stall for nothing.
 */
describe('session-switcher · isRestoringSession clears on navigation', () => {
	it('switchSession clears a pending restore flag', async () => {
		const { switcher, getState } = makeSwitcher(fullState({ isRestoringSession: true }));

		await switcher.switchSession('session-B');
		switcher.destroy();

		expect(getState().isRestoringSession).toBe(false);
	});

	it('newChat clears a pending restore flag', () => {
		const { switcher, getState } = makeSwitcher(fullState({ isRestoringSession: true }));

		switcher.newChat();
		switcher.destroy();

		expect(getState().isRestoringSession).toBe(false);
	});
});
