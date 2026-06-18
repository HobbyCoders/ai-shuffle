import { describe, expect, it, vi } from 'vitest';
import { createSessionSwitcher, type SessionSwitcherDeps } from './session-switcher';
import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import type { SessionEphemeral } from '../session-cache';

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
		draft: 'draft from session A',
		scrollTop: 250,
		streamStartTime: 123456,
		executionMode: 'local',
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
		...overrides,
	};
}

function makeSwitcher(initial: ActiveSessionState) {
	let state = initial;
	const update = (fn: (s: ActiveSessionState) => ActiveSessionState) => { state = fn(state); };
	const getState = () => state;

	const internal: InternalMutableState = {
		pendingTools: new Set<string>(),
		interruptPending: false,
		awaitingNewSession: false,
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
		messageCache: {
			get: vi.fn(async () => null),
			delete: vi.fn(async () => {}),
		},
		sessionPoller: {
			stop: vi.fn(),
			// Return a non-null, non-streaming poll result so switchSession does
			// not fall into its error branch.
			loadSession: vi.fn(async () => ({
				messages: [],
				isStreaming: false,
				lastUpdateId: 0,
				usage: { inputTokens: 0, outputTokens: 0, cacheCreationTokens: 0, cacheReadTokens: 0, contextUsed: 0 },
			})) as unknown as SessionSwitcherDeps['sessionPoller']['loadSession'],
			startStreaming: vi.fn(),
		},
		sessionState: { removeSession: vi.fn() },
		onPersistTimerChanged: vi.fn(),
	};

	const switcher = createSessionSwitcher(deps);
	return { switcher, getState, ephemeralStore };
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
});
