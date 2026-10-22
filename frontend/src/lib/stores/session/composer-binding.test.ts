/**
 * #418: opening a hidden Task worker's transcript showed the PREVIOUS chat's
 * agent, a fabricated Ask and Fast off — and the next owner message sent them.
 *
 * The switcher and the sender run here on one shared state, the way the
 * active-session store wires them, so each test drives the real path: switch
 * from a parent chat to a worker that has no sidebar row, let `/live` answer
 * (or not), then send and read the `query` frame that leaves the browser.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
	ALL_BINDING_UNHYDRATED,
	cacheComposerBinding,
	liveComposerBindingPatch,
	restoreComposerBinding,
	sendableComposerBinding,
} from './composer-binding';
import { createSessionSwitcher, normalizePermissionMode, type SessionSwitcherDeps } from './session-switcher';
import { createMessageSender, type MessageSenderDeps } from './message-sender';
import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import type { SessionEphemeral } from '../session-cache';
import { api } from '$lib/api/client';

const _mockSessions: { current: Array<Record<string, unknown>> } = { current: [] };
vi.mock('../app-data.svelte', () => ({
	appData: {
		get current() {
			return { sessions: _mockSessions.current, projects: [], clusterProjects: [] };
		},
	},
}));

vi.mock('$lib/api/client', () => ({
	api: { get: vi.fn(async () => null), put: vi.fn(async () => null), post: vi.fn(async () => null) },
}));

vi.mock('$lib/services/voiceCall', () => ({ isCallActiveFor: vi.fn(() => false), sendVoiceText: vi.fn(() => true) }));
vi.mock('$lib/services/voiceExecutionContext', () => ({ persistVoiceContext: vi.fn(async () => {}), voiceContextFor: vi.fn(() => ({})) }));
vi.mock('$lib/api/git', () => ({
	reopenWorktree: vi.fn(async () => {}),
	generateBranchName: vi.fn(async () => ({ branch_name: 'auto/x' })),
	generateBranchNameLocal: vi.fn(() => 'auto/x'),
}));

vi.mock('./v2/feed-controller', () => ({
	feedController: {
		attach: vi.fn(),
		start: vi.fn(),
		stop: vi.fn(),
		isFeeding: vi.fn(() => false),
		cachedPatch: vi.fn(() => null),
		hasCachedTranscript: vi.fn(() => false),
		invalidate: vi.fn(),
		hasAppliedUsage: vi.fn(() => false),
		isReducerStreaming: vi.fn(() => false),
		reconcileRunningTasks: vi.fn(() => false),
		noteOptimisticSend: vi.fn(),
		clearOptimisticSend: vi.fn(),
	},
	OPTIMISTIC_STREAMING_ON: { isStreaming: true, optimisticStreaming: true },
}));

// ─── Fixtures ────────────────────────────────────────────────────────────────

/** The parent: an Overseer chat on Ask with Fast off. */
function parentState(overrides: Partial<ActiveSessionState> = {}): ActiveSessionState {
	return {
		sessionId: 'parent',
		mode: 'code',
		title: 'Parent',
		messages: [{ id: 'm1', role: 'user', content: 'hi' }],
		isStreaming: false,
		optimisticStreaming: false,
		liveTurnId: null,
		liveModel: null,
		liveRequestedModel: null,
		liveProvider: null,
		wsConnected: true,
		wsReconnectRequired: false,
		error: null,
		profile: 'overseer',
		sessionProfileId: null,
		currentQueryProfileId: null,
		project: 'proj-1',
		projectV2Id: null,
		totalTokensIn: 0,
		totalTokensOut: 0,
		totalCacheCreationTokens: 0,
		totalCacheReadTokens: 0,
		contextUsed: null,
		liveContextUsage: null,
		contextMax: 200_000,
		modelOverride: 'prov:codex:gpt-6.1-sol',
		permissionModeOverride: 'default',
		effortLevelOverride: 'high',
		fastMode: false,
		bindingUnhydrated: {},
		bindingPicks: {},
		contextWindowChoice: 'standard',
		pendingPermissions: [],
		deferredPermissions: [],
		pendingQuestions: [],
		queuedMessages: [],
		pendingPeerMessages: [],
		activeTasks: [],
		draft: '',
		scrollTop: 0,
		streamStartTime: null,
		worktreeIntent: 'local',
		deviceIntent: null,
		worktreeBranch: '',
		worktreeBaseBranch: 'main',
		worktreeCreateNewBranch: false,
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
	} as ActiveSessionState;
}

/** The worker's `/live` metadata: an Investigator on Bypass with Fast on. */
const WORKER_META = {
	is_worker: true,
	mode: 'code',
	project_id: 'proj-1',
	model: 'gpt-6.1-sol',
	provider_id: 'codex',
	effort: 'high',
	profile_id: 'investigator',
	permission_mode: 'bypassPermissions',
	fast: true,
};

function harness(initial: ActiveSessionState) {
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
	const switcherDeps: SessionSwitcherDeps = {
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
	const wsConn = {
		isConnected: vi.fn(() => true),
		connect: vi.fn(),
		send: vi.fn((_data: Record<string, unknown>) => true),
		resetReconnectAttempts: vi.fn(),
	};
	const senderDeps: MessageSenderDeps = {
		update,
		getState,
		internal,
		wsConn,
		sessionEphemeralCache: { remove: vi.fn() },
		appData: { loadSessions: vi.fn(), loadApiUserSessions: vi.fn() },
		persistSession: vi.fn(),
		stopTimeout: { get: () => null, set: vi.fn() },
		getActiveSession: () => ({
			sendMessage: vi.fn(async () => true),
			createWorktree: vi.fn(async () => null),
			autoCreateWorktree: vi.fn(async () => true),
		}),
	};
	const switcher = createSessionSwitcher(switcherDeps);
	const sender = createMessageSender(senderDeps);
	const lastQuery = () => wsConn.send.mock.calls.map(c => c[0]).filter(f => f.type === 'query').at(-1) as Record<string, any>;
	return { switcher, sender, getState, update, ephemeralStore, wsConn, lastQuery };
}

/** A `/live` reply the test releases by hand, so the window before it is observable. */
function deferredLive() {
	let release!: (value: unknown) => void;
	const reply = new Promise(resolve => { release = resolve; });
	vi.mocked(api.get).mockImplementationOnce(() => reply as Promise<never>);
	return (meta: Record<string, unknown>) => release({ session_meta: meta });
}

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

/** How many `/live` requests went out for `sessionId`. */
const liveCalls = (sessionId: string) => vi.mocked(api.get).mock.calls
	.filter(([path]) => String(path).startsWith(`/sessions/${sessionId}/live`)).length;

beforeEach(() => {
	_mockSessions.current = [{ id: 'parent', profile_id: 'overseer', permission_mode: 'default', fast: false, mode: 'code', project_id: 'proj-1' }];
	vi.mocked(api.get).mockReset();
	vi.mocked(api.get).mockResolvedValue(null as never);
	localStorage.clear();
});

afterEach(() => {
	vi.restoreAllMocks();
});

// ─── Parent → hidden worker ─────────────────────────────────────────────────

describe('opening a hidden Task worker from its parent (#418)', () => {
	it('never shows the parent\'s agent or a made-up Ask / Fast off while /live is in flight', async () => {
		const answer = deferredLive();
		const { switcher, getState } = harness(parentState());

		const switching = switcher.switchSession('worker');
		// The worker has no sidebar row and no cache: nothing is known yet.
		expect(getState().sessionId).toBe('worker');
		expect(getState().profile).toBe('');
		expect(getState().profile).not.toBe('overseer');
		expect(getState().bindingUnhydrated).toEqual(ALL_BINDING_UNHYDRATED);

		answer(WORKER_META);
		await switching;
		await flush();
		switcher.destroy();
	});

	it('shows the worker\'s own agent, access and Fast once /live answers', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: WORKER_META } as never);
		const { switcher, getState } = harness(parentState());

		await switcher.switchSession('worker');
		await flush();

		expect(getState()).toMatchObject({
			profile: 'investigator',
			permissionModeOverride: 'bypassPermissions',
			fastMode: true,
			effortLevelOverride: 'high',
			modelOverride: 'prov:codex:gpt-6.1-sol',
			bindingUnhydrated: {},
		});
		switcher.destroy();
	});

	it('the next send carries the worker\'s own binding, not the parent\'s or defaults', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: WORKER_META } as never);
		const { switcher, sender, lastQuery } = harness(parentState());
		await switcher.switchSession('worker');
		await flush();

		expect(await sender.sendMessage('status?')).toBe(true);

		const query = lastQuery();
		expect(query.session_id).toBe('worker');
		expect(query.profile).toBe('investigator');
		expect(query.overrides.permission_mode).toBe('bypassPermissions');
		expect(query.overrides.fast).toBe(true);
		switcher.destroy();
	});

	it('a send before /live answers leaves agent, access and Fast to the worker\'s row', async () => {
		const answer = deferredLive();
		const { switcher, sender, lastQuery } = harness(parentState());
		const switching = switcher.switchSession('worker');

		expect(await sender.sendMessage('quick question')).toBe(true);

		const query = lastQuery();
		expect(query.session_id).toBe('worker');
		// Omitted keys: the backend resolves the session's own agent, and the
		// row's access and Fast (chat_ws_handler / query_options).
		expect(query.profile).toBeUndefined();
		expect(query.overrides).not.toHaveProperty('fast');
		expect(query.overrides).not.toHaveProperty('permission_mode');

		answer(WORKER_META);
		await switching;
		await flush();
		switcher.destroy();
	});

	it('an old worker that does not report the binding keeps it unhydrated (never sent)', async () => {
		const { profile_id: _p, permission_mode: _m, fast: _f, ...older } = WORKER_META;
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: older } as never);
		const { switcher, sender, getState, lastQuery } = harness(parentState());
		await switcher.switchSession('worker');
		await flush();

		expect(getState().bindingUnhydrated).toEqual(ALL_BINDING_UNHYDRATED);
		await sender.sendMessage('hello');
		expect(lastQuery().profile).toBeUndefined();
		expect(lastQuery().overrides).not.toHaveProperty('fast');
		switcher.destroy();
	});

	it('going back to the parent restores the parent\'s own binding', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: WORKER_META } as never);
		const { switcher, getState } = harness(parentState());
		await switcher.switchSession('worker');
		await flush();

		await switcher.switchSession('parent');
		expect(getState()).toMatchObject({ profile: 'overseer', permissionModeOverride: 'default', fastMode: false, bindingUnhydrated: {} });
		switcher.destroy();
	});
});

// ─── Poisoned cache ─────────────────────────────────────────────────────────

describe('reopening a worker after a poisoned cache (#418)', () => {
	it('a legacy cache entry with the fabricated values is not trusted', async () => {
		const answer = deferredLive();
		const { switcher, getState, ephemeralStore } = harness(parentState());
		// What an older build saved when the worker was left: the parent's
		// agent, Ask and Fast off, with no record of which values were real.
		ephemeralStore.set('worker', {
			profile: 'overseer', permissionModeOverride: null, fastMode: false,
			title: 'Worker', draft: '', scrollTop: 0, project: 'proj-1', projectV2Id: null,
			modelOverride: 'prov:codex:gpt-6.1-sol', effortLevelOverride: 'high',
			worktreeIntent: 'local', deviceIntent: null, worktreeId: null, worktreeBranch: '',
			worktreeBaseBranch: 'main', selectedWorktreeBranch: null, worktreeMode: 'new', worktreeDeleted: false,
		});

		const switching = switcher.switchSession('worker');
		expect(getState().profile).toBe('');
		expect(getState().bindingUnhydrated).toEqual(ALL_BINDING_UNHYDRATED);

		answer(WORKER_META);
		await switching;
		await flush();
		expect(getState()).toMatchObject({ profile: 'investigator', permissionModeOverride: 'bypassPermissions', fastMode: true });
		switcher.destroy();
	});

	it('leaving before /live answers does not cache the placeholders as real values', async () => {
		const answer = deferredLive();
		const { switcher, getState, ephemeralStore } = harness(parentState());
		const opening = switcher.switchSession('worker');
		await switcher.switchSession('parent');

		const cached = ephemeralStore.get('worker')!;
		expect(cached.bindingHydrated).toEqual({});

		// Reopen: still unhydrated, and this time /live answers.
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: WORKER_META } as never);
		const reopening = switcher.switchSession('worker');
		expect(getState().bindingUnhydrated).toEqual(ALL_BINDING_UNHYDRATED);
		await reopening;
		await flush();
		expect(getState()).toMatchObject({ profile: 'investigator', permissionModeOverride: 'bypassPermissions', fastMode: true });

		answer(WORKER_META);
		await opening;
		switcher.destroy();
	});

	it('server truth replaces a stale real cache value that is not an unsent pick', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: WORKER_META } as never);
		const { switcher, getState, ephemeralStore } = harness(parentState());
		ephemeralStore.set('worker', {
			...cacheComposerBinding({ profile: 'investigator', permissionModeOverride: 'default', fastMode: false }),
			permissionModeOverride: 'default',
			title: 'Worker', draft: '', scrollTop: 0, project: 'proj-1', projectV2Id: null,
			modelOverride: null, effortLevelOverride: null, worktreeIntent: 'local', deviceIntent: null,
			worktreeId: null, worktreeBranch: '', worktreeBaseBranch: 'main', selectedWorktreeBranch: null,
			worktreeMode: 'new', worktreeDeleted: false,
		});

		const opening = switcher.switchSession('worker');
		// The cached copy shows at once…
		expect(getState()).toMatchObject({ permissionModeOverride: 'default', fastMode: false, bindingUnhydrated: {} });
		await opening;
		await flush();
		// …and the worker's actual binding replaces it.
		expect(getState()).toMatchObject({ permissionModeOverride: 'bypassPermissions', fastMode: true });
		switcher.destroy();
	});
});

// ─── Local edits ────────────────────────────────────────────────────────────

describe('genuine local edits survive hydration (#418)', () => {
	it('an agent picked while /live is in flight is kept and sent', async () => {
		const answer = deferredLive();
		const { switcher, sender, update, getState, lastQuery } = harness(parentState());
		const switching = switcher.switchSession('worker');

		// What activeSession.setProfile does on an open chat.
		update(s => ({ ...s, profile: 'reviewer', bindingUnhydrated: { ...s.bindingUnhydrated, profile: undefined }, bindingPicks: { profile: true } }));
		answer(WORKER_META);
		await switching;
		await flush();

		expect(getState().profile).toBe('reviewer');
		// Access and Fast still came from the worker.
		expect(getState()).toMatchObject({ permissionModeOverride: 'bypassPermissions', fastMode: true });
		await sender.sendMessage('take over');
		expect(lastQuery().profile).toBe('reviewer');
		// The pick is spent: the session row now carries it.
		expect(getState().bindingPicks).toEqual({});
		switcher.destroy();
	});

	it('an unsent agent pick survives leaving and reopening the worker', async () => {
		vi.mocked(api.get).mockResolvedValue({ session_meta: WORKER_META } as never);
		const { switcher, update, getState } = harness(parentState());
		await switcher.switchSession('worker');
		await flush();
		update(s => ({ ...s, profile: 'reviewer', bindingPicks: { profile: true } }));

		await switcher.switchSession('parent');
		await flush();
		await switcher.switchSession('worker');
		await flush();

		expect(getState().profile).toBe('reviewer');
		expect(getState().bindingPicks).toEqual({ profile: true });
		switcher.destroy();
	});
});

// ─── Deleted agent ──────────────────────────────────────────────────────────

describe('a session whose agent was deleted (#418 review)', () => {
	it('/live reporting no agent settles on "pick an agent", not loading forever', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: { ...WORKER_META, profile_id: null } } as never);
		const { switcher, getState } = harness(parentState());
		await switcher.switchSession('worker');
		await flush();

		expect(getState().profile).toBe('');
		expect(getState().bindingUnhydrated).toEqual({});
		expect(getState()).toMatchObject({ permissionModeOverride: 'bypassPermissions', fastMode: true });
		switcher.destroy();
	});

	it('a row whose agent was deleted is known to have none', () => {
		const restored = restoreComposerBinding(null, { profile_id: null, permission_mode: 'auto', fast: false });
		expect(restored.profile).toBe('');
		expect(restored.bindingUnhydrated).toEqual({});
		// A row that does not carry the field at all still waits for /live.
		expect(restoreComposerBinding(null, { permission_mode: 'auto', fast: false }).bindingUnhydrated).toEqual({ profile: true });
	});
});

// ─── /live failure ──────────────────────────────────────────────────────────

describe('a failed /live on a rowless worker (#418 review)', () => {
	afterEach(() => vi.useRealTimers());

	it('retries once and then fills the worker\'s binding', async () => {
		vi.useFakeTimers();
		vi.mocked(api.get)
			.mockRejectedValueOnce(new Error('offline'))
			.mockResolvedValueOnce({ session_meta: WORKER_META } as never);
		const { switcher, getState } = harness(parentState());
		const opening = switcher.switchSession('worker');
		await vi.advanceTimersByTimeAsync(0);
		expect(getState().bindingUnhydrated).toEqual(ALL_BINDING_UNHYDRATED);

		await vi.advanceTimersByTimeAsync(1500);
		await opening;
		expect(liveCalls('worker')).toBe(2);
		expect(getState()).toMatchObject({ profile: 'investigator', permissionModeOverride: 'bypassPermissions', fastMode: true, bindingUnhydrated: {} });
		switcher.destroy();
	});

	it('gives up after one retry and still never guesses the binding', async () => {
		vi.useFakeTimers();
		vi.mocked(api.get).mockRejectedValue(new Error('offline'));
		const { switcher, getState } = harness(parentState());
		const opening = switcher.switchSession('worker');
		await vi.advanceTimersByTimeAsync(5000);
		await opening;
		expect(liveCalls('worker')).toBe(2);
		expect(getState().bindingUnhydrated).toEqual(ALL_BINDING_UNHYDRATED);
		expect(getState().profile).toBe('');
		switcher.destroy();
	});

	it('does not retry once the user has left, nor for a session its row already describes', async () => {
		vi.useFakeTimers();
		vi.mocked(api.get).mockRejectedValue(new Error('offline'));
		const { switcher } = harness(parentState());
		const opening = switcher.switchSession('worker');
		await vi.advanceTimersByTimeAsync(0);
		await switcher.switchSession('parent');
		await vi.advanceTimersByTimeAsync(5000);
		await opening;
		expect(liveCalls('worker')).toBe(1);
		// The parent's row carries its agent, access and Fast: nothing waits on /live.
		expect(liveCalls('parent')).toBe(1);
		switcher.destroy();
	});
});

// ─── Send snapshot ──────────────────────────────────────────────────────────

describe('a send that waits for the socket (#418 review)', () => {
	it('carries the settings of the chat it was typed in, even if the view switches meanwhile', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: WORKER_META } as never);
		const { switcher, sender, update, wsConn, lastQuery } = harness(parentState());
		await switcher.switchSession('worker');
		await flush();

		wsConn.isConnected.mockReturnValue(false);
		const sending = sender.sendMessage('status?');
		// While it waits: the composer now shows another chat's settings.
		update(s => ({ ...s, sessionId: 'parent', profile: 'overseer', permissionModeOverride: 'default', fastMode: false }));
		wsConn.isConnected.mockReturnValue(true);
		expect(await sending).toBe(true);

		const query = lastQuery();
		expect(query.session_id).toBe('worker');
		expect(query.profile).toBe('investigator');
		expect(query.overrides).toMatchObject({ permission_mode: 'bypassPermissions', fast: true });
		switcher.destroy();
	});
});

// ─── Pure helpers ───────────────────────────────────────────────────────────

describe('composer-binding helpers', () => {
	it('restore: row beats a missing cache; nothing known means unhydrated placeholders', () => {
		expect(restoreComposerBinding(null, { profile_id: 'a', permission_mode: 'auto', fast: true })).toEqual({
			profile: 'a', permissionModeOverride: 'auto', fastMode: true, bindingUnhydrated: {}, bindingPicks: {},
		});
		expect(restoreComposerBinding(null, null)).toEqual({
			profile: '', permissionModeOverride: null, fastMode: false,
			bindingUnhydrated: { profile: true, permission: true, fast: true }, bindingPicks: {},
		});
	});

	it('restore: a row without a stored Fast is not read as Fast off', () => {
		const restored = restoreComposerBinding(null, { profile_id: 'a', permission_mode: null });
		expect(restored.bindingUnhydrated).toEqual({ fast: true });
		expect(restored.permissionModeOverride).toBeNull();
	});

	it('restore: #371 row access wins over a cached pick', () => {
		const cache = cacheComposerBinding({ profile: 'a', permissionModeOverride: 'bypassPermissions', fastMode: false });
		expect(restoreComposerBinding(cache, { permission_mode: null }, { rowPermissionWins: true }).permissionModeOverride).toBe('default');
	});

	it('live patch: applies only reported fields, never over a pick or an in-flight edit', () => {
		const current = { profile: '', permissionModeOverride: null, fastMode: false, bindingUnhydrated: { ...ALL_BINDING_UNHYDRATED }, bindingPicks: {} };
		const initial = { profile: '', permissionModeOverride: null, fastMode: false };
		expect(liveComposerBindingPatch({ profile_id: 'w', permission_mode: 'plan', fast: true }, current, initial, normalizePermissionMode)).toEqual({
			profile: 'w', permissionModeOverride: 'default', fastMode: true, bindingUnhydrated: {},
		});
		expect(liveComposerBindingPatch({ fast: true }, { ...current, fastMode: true }, initial, normalizePermissionMode)).toEqual({});
		expect(liveComposerBindingPatch({ profile_id: 'w' }, { ...current, profile: 'mine', bindingPicks: { profile: true } }, { ...initial, profile: 'mine' }, normalizePermissionMode)).toEqual({});
	});

	it('send: a new compose always sends its picks; an open chat omits unhydrated ones', () => {
		const state = { sessionId: null, profile: 'a', permissionModeOverride: 'auto', fastMode: false, bindingUnhydrated: { ...ALL_BINDING_UNHYDRATED } };
		expect(sendableComposerBinding(state)).toEqual({ profile: 'a', permissionMode: 'auto', fast: false });
		expect(sendableComposerBinding({ ...state, sessionId: 's' })).toEqual({ profile: undefined, permissionMode: undefined, fast: undefined });
		expect(sendableComposerBinding({ ...state, sessionId: 's', bindingUnhydrated: {} })).toEqual({ profile: 'a', permissionMode: 'auto', fast: false });
	});
});
