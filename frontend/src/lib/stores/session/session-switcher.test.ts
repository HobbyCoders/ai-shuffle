import { describe, expect, it, vi, beforeEach } from 'vitest';
import {
	createSessionSwitcher,
	gateSnapshotPatch,
	type SessionSwitcherDeps,
} from './session-switcher';
import { feedController } from './v2/feed-controller';
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

// switchSession fetches a one-shot `/live` metadata snapshot (usage totals,
// controllability, worktree/cloud fields). Stub it out so the isolation tests
// exercise only the switch-in reset logic; a null reply is the "no metadata"
// path and leaves the reset fields untouched.
vi.mock('$lib/api/client', () => ({
	api: { get: vi.fn(async () => null), put: vi.fn(async () => null) },
}));

// The SSE-fed reducer is the transcript authority; switchSession stops the
// leaving feed and starts the target's. Mock the singleton so no real feed /
// EventSource is created during these unit tests.
vi.mock('./v2/feed-controller', () => ({
	feedController: {
		attach: vi.fn(),
		start: vi.fn(),
		stop: vi.fn(),
		isFeeding: vi.fn(() => false),
		currentSessionId: vi.fn(() => null),
		// Transcript cache (S2): switchSession primes its single update from
		// whatever the controller has cached for the target. Default = cache miss,
		// i.e. the full-replay behaviour these isolation tests assert; the cache
		// hit is covered by its own suite below.
		cachedPatch: vi.fn(() => null),
		hasCachedTranscript: vi.fn(() => false),
		invalidate: vi.fn(),
		// /live snapshot gating (W7): default = the feed has published no usage,
		// so the snapshot still applies its own.
		hasAppliedUsage: vi.fn(() => false),
		isReducerStreaming: vi.fn(() => false),
		noteOptimisticSend: vi.fn(),
		clearOptimisticSend: vi.fn(),
	},
	OPTIMISTIC_STREAMING_ON: { isStreaming: true, optimisticStreaming: true },
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
		liveTurnId: null,
		liveModel: null,
		liveRequestedModel: null,
		liveProvider: null,
		wsConnected: true,
		error: null,
		profile: 'profile-global',
		sessionProfileId: 'profile-A',
		currentQueryProfileId: 'profile-A-query',
		project: 'proj-A',
		projectV2Id: null,
		totalTokensIn: 5000,
		totalTokensOut: 4000,
		totalCacheCreationTokens: 3000,
		totalCacheReadTokens: 2000,
		contextUsed: 14000,
		liveContextUsage: null,
		contextMax: 1_000_000,
		modelOverride: 'model-A',
		permissionModeOverride: 'plan',
		effortLevelOverride: 'high',
		fastMode: false,
		contextWindowChoice: '1m',
		pendingPermissions: [],
		deferredPermissions: [],
		pendingQuestions: [],
		queuedMessages: [],
		activeTasks: [],
		draft: 'draft from session A',
		scrollTop: 250,
		streamStartTime: 123456,
		worktreeIntent: 'local',
		deviceIntent: null,
		worktreeBranch: '',
		worktreeBaseBranch: 'main',
		worktreeCreateNewBranch: true,
		worktreeId: null,
		worktreeMode: 'new',
		selectedWorktreeBranch: null,
		isCreatingWorktree: false,
		worktreeError: null,
		worktreeDeleted: false,
		sessionLocked: false,
		sessionLockedBy: undefined,
		pendingRestorePrompt: null,
		isRestoringSession: false,
		isLoadingSession: false,
		...overrides,
	};
}

function makeSwitcher(initial: ActiveSessionState) {
	let state = initial;
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
		expect(s.isLoadingSession).toBe(true);
		expect(s.worktreeCreateNewBranch).toBe(false);

		// Overrides and draft default on a cache miss (never inherit session A's).
		expect(s.modelOverride).toBeNull();
		expect(s.permissionModeOverride).toBeNull();
		expect(s.effortLevelOverride).toBeNull();
		expect(s.contextWindowChoice).toBe('1m');
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
			projectV2Id: null,
			modelOverride: 'model-B',
			permissionModeOverride: null,
			effortLevelOverride: null,
			worktreeIntent: 'local',
		deviceIntent: null,
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

	it('requests pending questions/permissions on switch even when NOT streaming', async () => {
		// A session parked on an AskUserQuestion/permission reports isStreaming=false.
		// Gating the resync on isStreaming (the old bug) hid the modal on switch. The
		// resync must fire unconditionally so the parked prompt surfaces immediately.
		const { switcher, deps } = makeSwitcher(fullState());

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

describe('session-switcher · remote-only project survives switch', () => {
	it('does not inherit the leaving local project when the target is v2-only', async () => {
		const { switcher, getState } = makeSwitcher(fullState({
			project: 'proj-A',
			projectV2Id: 'pv2-A',
		}));

		await switcher.switchSession('session-B', null, null, null, 'pv2-remote');
		switcher.destroy();

		expect(getState().project).toBe('');
		expect(getState().projectV2Id).toBe('pv2-remote');
	});

	it('restores a remote-only compose from ephemeral (empty project is set, not unset)', async () => {
		const { switcher, getState, ephemeralStore } = makeSwitcher(fullState({
			project: 'proj-A',
			projectV2Id: 'pv2-A',
		}));
		ephemeralStore.set('session-B', {
			title: 'Remote',
			draft: '',
			scrollTop: 0,
			project: '',
			projectV2Id: 'pv2-remote',
			modelOverride: null,
			permissionModeOverride: null,
			effortLevelOverride: null,
			worktreeIntent: 'local',
			deviceIntent: null,
			worktreeId: null,
			worktreeBranch: '',
			worktreeBaseBranch: 'main',
			selectedWorktreeBranch: null,
			worktreeMode: 'new',
			worktreeDeleted: false,
		});

		await switcher.switchSession('session-B');
		switcher.destroy();

		expect(getState().project).toBe('');
		expect(getState().projectV2Id).toBe('pv2-remote');
	});

	it('newChat keeps a v2-only compose instead of the last local default', () => {
		const { switcher, getState } = makeSwitcher(fullState({
			sessionId: null,
			project: '',
			projectV2Id: 'pv2-remote',
		}));

		switcher.newChat();
		switcher.destroy();

		expect(getState().project).toBe('');
		expect(getState().projectV2Id).toBe('pv2-remote');
	});
});

describe('session run binding on switch', () => {
	beforeEach(() => {
		_mockSessions.current = [];
		localStorage.clear();
	});

	it('restores the complete last-used binding for a new chat', () => {
		localStorage.setItem('aihub_lastModel', 'prov:openai:gpt-5.6-sol');
		localStorage.setItem('aihub_lastEffort', 'xhigh');
		localStorage.setItem('aihub_lastPermissionMode', 'plan');
		localStorage.setItem('aihub_lastContextWindowChoice', 'standard');
		const { switcher, getState } = makeSwitcher(fullState());

		switcher.newChat();
		switcher.destroy();

		expect(getState().modelOverride).toBe('prov:openai:gpt-5.6-sol');
		expect(getState().effortLevelOverride).toBe('xhigh');
		expect(getState().permissionModeOverride).toBe('plan');
		expect(getState().contextWindowChoice).toBe('standard');
	});

	it('restores the last-used fast-tier pick for a new chat', () => {
		localStorage.setItem('aihub_lastFastMode', '1');
		const { switcher, getState } = makeSwitcher(fullState());

		switcher.newChat();
		switcher.destroy();

		expect(getState().fastMode).toBe(true);
	});

	it('opens a new chat with fast mode off when nothing was remembered', () => {
		const { switcher, getState } = makeSwitcher(fullState());

		switcher.newChat();
		switcher.destroy();

		expect(getState().fastMode).toBe(false);
	});

	it('hydrates the target session own binding from its row', async () => {
		// Agents carry no model, so the row is the only source for a session
		// opened without a local ephemeral entry — e.g. from another device.
		_mockSessions.current = [
			{
				id: 'session-B',
				model: 'prov:op:openai/gpt-5.6',
				effort: 'high',
				permission_mode: 'plan',
				context_window: 256_000,
				fast: true
			},
		];
		const { switcher, getState } = makeSwitcher(fullState());

		await switcher.switchSession('session-B');
		switcher.destroy();

		const s = getState();
		expect(s.modelOverride).toBe('prov:op:openai/gpt-5.6');
		expect(s.effortLevelOverride).toBe('high');
		expect(s.permissionModeOverride).toBe('plan');
		expect(s.contextWindowChoice).toBe('standard');
		// Cross-device: the row is the only carrier of the fast-tier pick.
		expect(s.fastMode).toBe(true);
	});

	it('defaults fast mode to off for a row that never stored one', async () => {
		_mockSessions.current = [{ id: 'session-C', model: 'opus' }];
		const { switcher, getState } = makeSwitcher(fullState());

		await switcher.switchSession('session-C');
		switcher.destroy();

		expect(getState().fastMode).toBe(false);
	});

	it('prefers an unsent local pick over the stored binding', async () => {
		_mockSessions.current = [{ id: 'session-B', model: 'sonnet' }];
		const { switcher, getState, ephemeralStore } = makeSwitcher(fullState());
		ephemeralStore.set('session-B', {
			title: 'Session B',
			draft: '',
			scrollTop: 0,
			project: 'proj-B',
			projectV2Id: null,
			modelOverride: 'opus',
			permissionModeOverride: null,
			effortLevelOverride: null,
			worktreeIntent: 'local',
			deviceIntent: null,
			worktreeId: null,
			worktreeBranch: '',
			worktreeBaseBranch: 'main',
			selectedWorktreeBranch: null,
			worktreeMode: 'new',
			worktreeDeleted: false,
		});

		await switcher.switchSession('session-B');
		switcher.destroy();

		expect(getState().modelOverride).toBe('opus');
	});

	it('leaves the binding null for a session created before it existed', async () => {
		// The backend then falls back to launched_model / the agent legacy
		// config, so an old conversation keeps running on what it always did.
		_mockSessions.current = [{ id: 'session-B' }];
		const { switcher, getState } = makeSwitcher(fullState());

		await switcher.switchSession('session-B');
		switcher.destroy();

		expect(getState().modelOverride).toBeNull();
	});
});

// ─── S2: cached transcript primes the switch-in ──────────────────────────────

describe('session-switcher · cached transcript prime (S2)', () => {
	it('renders the cached transcript in the SAME update as the reset — no empty flash', async () => {
		const cached = [
			{ id: 'c1', role: 'user', content: 'earlier' },
			{ id: 'c2', role: 'assistant', content: 'reply' },
		];
		vi.mocked(feedController.cachedPatch).mockReturnValue({
			messages: cached as never,
			isStreaming: false,
			isLoadingSession: false,
			isRestoringSession: false,
			contextUsed: 4242,
		});

		// Every state this switch publishes, so the assertion can look for an
		// intermediate empty one rather than only the end result.
		const seen: Array<{ messages: unknown[]; isLoadingSession: boolean }> = [];
		let state = fullState();
		const getState = () => state;
		const switcher = createSessionSwitcher({
			update: (fn) => {
				state = fn(state);
				seen.push({ messages: state.messages, isLoadingSession: state.isLoadingSession });
			},
			getState,
			wsSend: vi.fn(() => true),
			internal: {
				pendingTools: new Set<string>(),
				awaitingNewSession: false,
				pendingRequestId: null,
				stopRequestedBeforeSession: false,
				worktreeCreationGeneration: 0,
				worktreeCreateAbort: null,
			},
			sessionEphemeralCache: { save: vi.fn(), restore: () => null },
			onPersistTimerChanged: vi.fn(),
		});

		await switcher.switchSession('session-B');
		switcher.destroy();

		// The switch-in reset and the cached transcript are ONE state, so no
		// observer can ever see the intermediate `messages: []` the reset alone
		// would produce (which the chat surface reads as a cleared conversation).
		expect(seen[0].messages).toHaveLength(2);
		expect(seen[0].isLoadingSession).toBe(false);
		expect(getState().messages).toHaveLength(2);
		expect(getState().contextUsed).toBe(4242);
	});

	it('falls back to the empty + loading reset on a cache miss', async () => {
		vi.mocked(feedController.cachedPatch).mockReturnValue(null);
		const { switcher, getState } = makeSwitcher(fullState());
		await switcher.switchSession('session-B');
		switcher.destroy();
		expect(getState().messages).toHaveLength(0);
		expect(getState().isLoadingSession).toBe(true);
	});
});

// ─── S10 / W7: title precedence and snapshot gating ─────────────────────────

describe('session-switcher · title resolution (S10)', () => {
	beforeEach(() => {
		vi.mocked(feedController.cachedPatch).mockReturnValue(null);
	});

	it('prefers the appData row title over a stale cached "New Chat"', async () => {
		_mockSessions.current = [{ id: 'session-B', title: 'Real backend title' }];
		const { switcher, getState, ephemeralStore } = makeSwitcher(fullState());
		// The ephemeral entry was saved before the backend set the real title.
		ephemeralStore.set('session-B', { title: 'New Chat', draft: '' } as never);

		await switcher.switchSession('session-B');
		switcher.destroy();
		expect(getState().title).toBe('Real backend title');
	});

	it('still prefers a caller-provided title over everything', async () => {
		_mockSessions.current = [{ id: 'session-B', title: 'Row title' }];
		const { switcher, getState } = makeSwitcher(fullState());
		await switcher.switchSession('session-B', undefined, undefined, 'Caller title');
		switcher.destroy();
		expect(getState().title).toBe('Caller title');
	});

	it('keeps a real cached title when no server title is known', async () => {
		_mockSessions.current = [];
		const { switcher, getState, ephemeralStore } = makeSwitcher(fullState());
		ephemeralStore.set('session-B', { title: 'A real chat', draft: '' } as never);
		await switcher.switchSession('session-B');
		switcher.destroy();
		expect(getState().title).toBe('A real chat');
	});
});

describe('gateSnapshotPatch (W7)', () => {
	const usagePatch = {
		totalTokensIn: 10,
		totalTokensOut: 20,
		totalCacheCreationTokens: 30,
		totalCacheReadTokens: 40,
		contextUsed: 80,
		worktreeId: 'wt-1',
		title: 'Snapshot title',
	} as const;

	it('drops usage fields once the feed has published its own', () => {
		const gated = gateSnapshotPatch({ ...usagePatch }, {
			feedOwnsUsage: true,
			currentTitle: 'New Chat',
		});
		expect(gated.totalTokensIn).toBeUndefined();
		expect(gated.contextUsed).toBeUndefined();
		// Fields the feed does not carry still apply.
		expect(gated.worktreeId).toBe('wt-1');
		expect(gated.title).toBe('Snapshot title');
	});

	it('applies usage when the feed has published none yet', () => {
		const gated = gateSnapshotPatch({ ...usagePatch }, {
			feedOwnsUsage: false,
			currentTitle: 'New Chat',
		});
		expect(gated.contextUsed).toBe(80);
		expect(gated.totalTokensOut).toBe(20);
	});

	it('never downgrades a title that a fresher source already set', () => {
		const gated = gateSnapshotPatch({ ...usagePatch }, {
			feedOwnsUsage: false,
			currentTitle: 'Renamed by the user',
		});
		expect(gated.title).toBeUndefined();
		expect(gated.contextUsed).toBe(80);
	});

	// Audit item 4: the switch-in reset empties the chip strip, and the feed
	// repaints it from the target's own `task.updated` rows. The snapshot is the
	// FLOOR under that — a background task the registry says is running right
	// now, in a session whose log has no running row to replay.
	it('seeds activeTasks while the strip is still empty', () => {
		const tasks = [
			{
				id: 't1',
				subject: 'npm run dev',
				status: 'running',
				kind: 'shell',
				summary: null,
				toolUseId: null,
				startedAt: '',
				updatedAt: '',
			},
		];
		const gated = gateSnapshotPatch({ activeTasks: tasks } as never, {
			feedOwnsUsage: false,
			currentTitle: 'New Chat',
			currentTaskCount: 0,
		});
		expect(gated.activeTasks).toHaveLength(1);
	});

	it('leaves the strip alone once the feed has painted it', () => {
		const gated = gateSnapshotPatch({ activeTasks: [] } as never, {
			feedOwnsUsage: false,
			currentTitle: 'New Chat',
			currentTaskCount: 2,
		});
		expect(gated.activeTasks).toBeUndefined();
	});
});
