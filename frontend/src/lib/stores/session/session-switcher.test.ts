import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import {
	createSessionSwitcher,
	setPersistedRunBinding,
	gateSnapshotPatch,
	reconciledSessionMode,
	type SessionSwitcherDeps,
} from './session-switcher';
import { feedController } from './v2/feed-controller';
import type { ActiveSessionState } from '../active-session';
import type { InternalMutableState } from './ws-message-handlers';
import type { SessionEphemeral } from '../session-cache';
import type { ChatMessage } from '../types';
import { appMode } from '../sessionMode.svelte';
import { composerPreferences } from '../composer-preferences';
import { api } from '$lib/api/client';
import { get } from 'svelte/store';
import { openDropdown, openHeaderDropdown } from '../header-dropdowns';
import { getPersistedDeviceIntent, setPersistedDeviceIntent } from '../worker-selection';
import { _resetProseQuestions, observeLastUserMessage, proseQuestionSessions } from './prose-questions';

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
		mode: 'code',
		title: 'Session A',
		messages: [{ id: 'm1', role: 'user', content: 'hi' }],
		isStreaming: false,
		liveTurnId: null,
		liveModel: null,
		liveRequestedModel: null,
		liveProvider: null,
		wsConnected: true,
		wsReconnectRequired: false,
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
		pendingPeerMessages: [],
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

describe('remembered new-chat worker', () => {
	it('restores the preference after send cleared the active intent, without rerouting existing sessions', async () => {
		const pick = { id: 'runner-1', name: 'Unraid' };
		setPersistedDeviceIntent(pick);
		const { switcher, getState } = makeSwitcher(fullState({ deviceIntent: null }));
		try {
			switcher.newChat();
			expect(getState().deviceIntent).toEqual(pick);
			await switcher.switchSession('session-B');
			expect(getState().deviceIntent).toBeNull();
			expect(getPersistedDeviceIntent()).toEqual(pick);
			switcher.newChat();
			expect(getState().deviceIntent).toEqual(pick);
		} finally {
			switcher.destroy();
			setPersistedDeviceIntent(null);
		}
	});

	it('preserves a cached draft explicitly targeting Local instead of the remote default', () => {
		setPersistedDeviceIntent({ id: 'runner-1', name: 'Unraid' });
		const { switcher, getState } = makeSwitcher(fullState({ sessionId: null, draft: 'Local draft', deviceIntent: null }));
		try {
			switcher.newChat();
			expect(getState().deviceIntent).toBeNull();
		} finally {
			switcher.destroy();
			setPersistedDeviceIntent(null);
		}
	});
});

describe('back-to-back worktree sessions keep independent checkout identity (#1528)', () => {
	// A first worktree send is still being created (multi-second branch naming +
	// POST) when the user starts the next chat. The in-flight creation owns its
	// branch; the next compose must not inherit it, or the second task is bound
	// to the first task's branch and checkout.
	const inFlight = () => fullState({
		sessionId: null,
		title: 'New Chat',
		draft: '',
		messages: [{ id: 'user-1', role: 'user', content: 'Fix desktop update feedback' }],
		worktreeIntent: 'worktree',
		worktreeBranch: 'fix/desktop-update-feedback-and-fresh-ui',
		worktreeBaseBranch: 'develop',
		worktreeCreateNewBranch: true,
		isCreatingWorktree: true,
	});

	it('a new chat started mid-creation does not stage the first task\'s branch', () => {
		const { switcher, getState, ephemeralStore } = makeSwitcher(inFlight());
		try {
			switcher.newChat();
			expect(getState().sessionId).toBeNull();
			expect(getState().worktreeBranch).toBe('');
			expect(getState().worktreeCreateNewBranch).toBe(false);
			expect(getState().worktreeId).toBeNull();
			expect(getState().selectedWorktreeBranch).toBeNull();
			expect(getState().isCreatingWorktree).toBe(false);
			expect(getState().messages).toEqual([]);
			expect(ephemeralStore.get('__new_chat__')?.worktreeBranch).toBe('');
		} finally {
			switcher.destroy();
		}
	});

	it('switching away mid-creation and back to New Chat does not stage it either', async () => {
		const { switcher, getState, ephemeralStore } = makeSwitcher(inFlight());
		try {
			await switcher.switchSession('session-B');
			expect(ephemeralStore.get('__new_chat__')?.worktreeBranch).toBe('');
			expect(ephemeralStore.get('__new_chat__')?.worktreeCreateNewBranch).toBe(false);
			switcher.newChat();
			expect(getState().worktreeBranch).toBe('');
			expect(getState().worktreeCreateNewBranch).toBe(false);
		} finally {
			switcher.destroy();
		}
	});

	it('keeps text typed into the next compose while the first creation runs', () => {
		const { switcher, getState } = makeSwitcher({ ...inFlight(), draft: 'Live session effort and Fast mode' });
		try {
			switcher.newChat();
			expect(getState().draft).toBe('Live session effort and Fast mode');
			expect(getState().worktreeBranch).toBe('');
		} finally {
			switcher.destroy();
		}
	});

	it('still restores a staged branch on an idle (unsent) compose', () => {
		const { switcher, getState } = makeSwitcher({ ...inFlight(), isCreatingWorktree: false, messages: [] });
		try {
			switcher.newChat();
			expect(getState().worktreeBranch).toBe('fix/desktop-update-feedback-and-fresh-ui');
			expect(getState().worktreeCreateNewBranch).toBe(true);
		} finally {
			switcher.destroy();
		}
	});
});

describe('session navigation closes header dropdowns', () => {
	it('clears peer pending state on switch and on new chat instead of sharing it across sessions', async () => {
		const { switcher, getState } = makeSwitcher({ ...fullState(), pendingPeerMessages: [
			{ id: 'peer-old', content: 'coordinate', senderLabel: 'Reviewer', queuedAt: 1 }
		] });
		await switcher.switchSession('session-B', 'proj-A');
		expect(getState().pendingPeerMessages).toEqual([]);
		switcher.newChat();
		expect(getState().pendingPeerMessages).toEqual([]);
	});

	const dropdowns = [
		'profile', 'branch', 'workspace-folders', 'sheet-workspace-folders',
		'sheet-profile', 'sheet-branch', 'runner', 'notifications', 'session-title',
		'git-commit', 'git-push', 'git-pull', 'git-pr', 'git-finish', 'git-shipit',
	] as const;

	beforeEach(() => {
		openHeaderDropdown.set(null);
		_mockSessions.current = [];
		vi.mocked(api.get).mockReset().mockResolvedValue(null);
	});

	it.each(dropdowns)('closes %s before the destination session loads and keeps it closed on return', async (id) => {
		const { switcher, getState } = makeSwitcher(fullState());
		openDropdown(id);
		const switching = switcher.switchSession('session-B', 'proj-A');
		expect(getState().sessionId).toBe('session-B');
		expect(get(openHeaderDropdown)).toBeNull();
		await switching;
		await switcher.switchSession('session-A', 'proj-A');
		expect(get(openHeaderDropdown)).toBeNull();
		switcher.destroy();
	});

	it.each(dropdowns)('closes %s when starting a new chat', (id) => {
		const { switcher, getState } = makeSwitcher(fullState());
		openDropdown(id);
		switcher.newChat();
		expect(getState().sessionId).toBeNull();
		expect(get(openHeaderDropdown)).toBeNull();
		switcher.destroy();
	});

	it('closes a composer dropdown when navigating from a new chat to a session', async () => {
		const { switcher } = makeSwitcher(fullState({ sessionId: null }));
		openDropdown('profile');
		await switcher.switchSession('session-B', 'proj-A');
		expect(get(openHeaderDropdown)).toBeNull();
		switcher.destroy();
	});

	it('preserves an open menu when reloading the same session', async () => {
		const { switcher } = makeSwitcher(fullState());
		openDropdown('git-shipit');
		await switcher.switchSession('session-A', 'proj-A');
		expect(get(openHeaderDropdown)).toBe('git-shipit');
		openHeaderDropdown.set(null);
		switcher.destroy();
	});
});

describe('session-switcher · authoritative lane hydration', () => {
	beforeEach(() => {
		_mockSessions.current = [];
		vi.mocked(api.get).mockReset().mockResolvedValue(null);
		appMode.set('code', false);
	});

	it.each([
		['chat', 'chat'], ['voice', 'voice'], ['code', 'code'], [null, 'code'], ['unknown', 'code'],
	] as const)('opens an unlisted deep link in its persisted %s lane', async (mode, expected) => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: { mode } });
		const { switcher, getState } = makeSwitcher(fullState({ mode: 'voice' }));
		const setMode = vi.spyOn(appMode, 'set');
		await switcher.switchSession('unlisted-session');
		await vi.waitFor(() => expect(setMode).toHaveBeenCalledWith(expected, false));
		expect(getState().mode).toBe(expected);
		expect(appMode.current).toBe(expected);
		setMode.mockRestore();
		switcher.destroy();
	});

	it.each([false, true])('loads ownership for an unlisted child (remote=%s) and clears it on navigation', async (is_remote) => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: { is_remote, device_name: 'Owner' } });
		const { switcher, getState } = makeSwitcher(fullState());
		await switcher.switchSession('unlisted-child');
		await vi.waitFor(() => expect(getState().isRemote).toBe(is_remote));
		expect(getState().ownerDeviceName).toBe('Owner');
		expect(_mockSessions.current).toEqual([]);
		expect(api.get).toHaveBeenCalledWith('/sessions/unlisted-child/live?since_update_id=0&include_messages=false&owner_fields=true');
		await switcher.switchSession('unknown-owner');
		expect(getState().isRemote).toBeUndefined();
		expect(getState().ownerDeviceName).toBeUndefined();
		switcher.destroy();
	});

	it('corrects a stale lane on reload without changing the saved default', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: { mode: 'chat' } });
		const { switcher, getState } = makeSwitcher(fullState({ mode: 'voice' }));
		const setMode = vi.spyOn(appMode, 'set');
		await switcher.refreshSessionMetadata('session-A');
		expect(getState().mode).toBe('chat');
		expect(setMode).toHaveBeenCalledExactlyOnceWith('chat', false);
		setMode.mockRestore();
		switcher.destroy();
	});

	it('preserves the lane when an older worker omits mode', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: { title: 'Chat' } });
		appMode.set('chat', false);
		const { switcher, getState } = makeSwitcher(fullState({ mode: 'chat' }));
		await switcher.refreshSessionMetadata('session-A');
		expect(getState().mode).toBe('chat');
		expect(appMode.current).toBe('chat');
		switcher.destroy();
	});

	it('does not let a late snapshot change the newly selected session or lane', async () => {
		let resolve!: (value: unknown) => void;
		vi.mocked(api.get).mockReturnValueOnce(new Promise(done => resolve = done));
		const { switcher, getState } = makeSwitcher(fullState());
		const refresh = switcher.refreshSessionMetadata('session-A');
		_mockSessions.current = [{ id: 'session-B', mode: 'voice' }];
		await switcher.switchSession('session-B');
		appMode.set('voice', false);
		resolve({ session_meta: { mode: 'chat' } });
		await refresh;
		expect(getState()).toMatchObject({ sessionId: 'session-B', mode: 'voice' });
		expect(appMode.current).toBe('voice');
		switcher.destroy();
	});
});

describe('session-switcher · authoritative project hydration', () => {
	beforeEach(() => {
		_mockSessions.current = [];
		vi.mocked(api.get).mockReset().mockResolvedValue(null);
	});

	it('reopens a worktree by deep link without a sidebar row or ephemeral cache', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: {
			project_id: 'ai-shuffle', project_v2_id: 'pv2-local',
			worktree_id: null, worktree_branch: 'fix/reopened-worktree', worktree_deleted: false,
		} });
		const { switcher, getState, ephemeralStore } = makeSwitcher(fullState());
		expect(ephemeralStore.has('new-worktree')).toBe(false);
		await switcher.switchSession('new-worktree');
		await vi.waitFor(() => expect(getState().project).toBe('ai-shuffle'));
		expect(getState()).toMatchObject({
			projectV2Id: 'pv2-local', worktreeBranch: 'fix/reopened-worktree', worktreeDeleted: false,
		});
		expect(_mockSessions.current).toEqual([]);
		switcher.destroy();
	});

	it.each([
		{ project_id: 'ai-shuffle', project_v2_id: 'pv2-local', project: 'ai-shuffle', projectV2Id: 'pv2-local' },
		{ project_id: null, project_v2_id: 'pv2-remote', project: '', projectV2Id: 'pv2-remote' },
		{ project_id: null, project_v2_id: null, project: '', projectV2Id: null },
	])('corrects stale persisted project state on reload ($project_v2_id)', async (row) => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: row });
		const { switcher, getState } = makeSwitcher(fullState({ project: 'stale', projectV2Id: 'pv2-stale' }));
		await switcher.refreshSessionMetadata('session-A');
		expect(getState()).toMatchObject({ project: row.project, projectV2Id: row.projectV2Id });
		switcher.destroy();
	});

	it('preserves project state when older metadata omits project fields', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: { worktree_branch: 'fix/branch' } });
		const { switcher, getState } = makeSwitcher(fullState({ project: 'ai-shuffle', projectV2Id: 'pv2-local' }));
		await switcher.refreshSessionMetadata('session-A');
		expect(getState()).toMatchObject({ project: 'ai-shuffle', projectV2Id: 'pv2-local' });
		switcher.destroy();
	});

	it('keeps a project selected while metadata was in flight', async () => {
		let resolve!: (value: unknown) => void;
		vi.mocked(api.get).mockReturnValueOnce(new Promise(done => resolve = done));
		const { switcher, getState, deps } = makeSwitcher(fullState());
		await switcher.switchSession('new-worktree');
		deps.update(s => ({ ...s, project: 'edited', projectV2Id: 'pv2-edited' }));
		resolve({ session_meta: { project_id: 'ai-shuffle', project_v2_id: 'pv2-local' } });
		await Promise.resolve();
		expect(getState()).toMatchObject({ project: 'edited', projectV2Id: 'pv2-edited' });
		switcher.destroy();
	});

	it('ignores the previous visit response after leaving and reopening the same session', async () => {
		let resolve!: (value: unknown) => void;
		vi.mocked(api.get).mockReturnValueOnce(new Promise(done => resolve = done));
		const { switcher, getState } = makeSwitcher(fullState());
		await switcher.switchSession('new-worktree');
		await switcher.switchSession('session-C', 'other');
		await switcher.switchSession('new-worktree', 'current', null, null, 'pv2-current');
		resolve({ session_meta: { project_id: 'stale', project_v2_id: 'pv2-stale' } });
		await Promise.resolve();
		expect(getState()).toMatchObject({ sessionId: 'new-worktree', project: 'current', projectV2Id: 'pv2-current' });
		switcher.destroy();
	});
});

describe('session-switcher · authoritative binding hydration', () => {
	beforeEach(() => {
		_mockSessions.current = [];
		vi.mocked(api.get).mockReset().mockResolvedValue(null);
	});

	it('restores the saved context pin rather than a stale acknowledged cache', async () => {
		_mockSessions.current = [{ id: 'session-B', model: 'fable', context_window: 200_000 }];
		const { switcher, getState, ephemeralStore } = makeSwitcher(fullState());
		ephemeralStore.set('session-B', { contextWindowChoice: '1m' } as SessionEphemeral);
		await switcher.switchSession('session-B');
		expect(getState().contextWindowChoice).toBe('standard');
		expect(getState().contextWindowPin).toBe(200_000);
		switcher.destroy();
	});

	it('refreshes a stale restored context choice from metadata on reload', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: { context_window: 200_000 } });
		const { switcher, getState } = makeSwitcher(fullState({ contextWindowChoice: '1m' }));
		await switcher.refreshSessionMetadata('session-A');
		expect(getState().contextWindowChoice).toBe('standard');
		expect(getState().contextWindowPin).toBe(200_000);
		switcher.destroy();
	});

	it('keeps a context choice acknowledged while metadata was in flight', async () => {
		let resolve!: (value: unknown) => void;
		vi.mocked(api.get).mockReturnValueOnce(new Promise(done => resolve = done));
		const { switcher, getState, deps } = makeSwitcher(fullState({ contextWindowChoice: '1m' }));
		const pending = switcher.refreshSessionMetadata('session-A');
		deps.update(s => ({ ...s, contextWindowChoice: 'standard', contextWindowPin: 200_000 }));
		resolve({ session_meta: { context_window: 1_000_000 } });
		await pending;
		expect(getState().contextWindowChoice).toBe('standard');
		expect(getState().contextWindowPin).toBe(200_000);
		switcher.destroy();
	});

	it('hydrates a hidden worker outside the sidebar with its provider and effort', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: { model: 'gpt-5.6-terra', provider_id: 'openai', effort: 'high' } });
		const { switcher, getState } = makeSwitcher(fullState());
		await switcher.switchSession('hidden-child');
		await vi.waitFor(() => expect(getState().effortLevelOverride).toBe('high'));
		expect(getState().modelOverride).toBe('prov:openai:gpt-5.6-terra');
		expect(_mockSessions.current).toEqual([]);
		switcher.destroy();
	});

	it('fills missing persisted bindings on reload without changing the open session', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: { model: 'prov:openai:gpt-5.6-terra', provider_id: 'openai', effort: 'high' } });
		const { switcher, getState } = makeSwitcher(fullState({ modelOverride: null, effortLevelOverride: null }));
		await switcher.refreshSessionMetadata('session-A');
		expect(getState()).toMatchObject({ sessionId: 'session-A', modelOverride: 'prov:openai:gpt-5.6-terra', effortLevelOverride: 'high' });
		switcher.destroy();
	});

	it('keeps edits made while the binding snapshot is in flight', async () => {
		let resolve!: (value: unknown) => void;
		vi.mocked(api.get).mockReturnValueOnce(new Promise(done => resolve = done));
		const { switcher, getState, deps } = makeSwitcher(fullState());
		await switcher.switchSession('hidden-child');
		deps.update(s => ({ ...s, modelOverride: 'opus', effortLevelOverride: 'low' }));
		resolve({ session_meta: { model: 'gpt-5.6-terra', provider_id: 'openai', effort: 'high' } });
		await Promise.resolve();
		expect(getState()).toMatchObject({ modelOverride: 'opus', effortLevelOverride: 'low' });
		switcher.destroy();
	});

	it('never applies an old child snapshot to a newer selection', async () => {
		let resolve!: (value: unknown) => void;
		vi.mocked(api.get).mockReturnValueOnce(new Promise(done => resolve = done));
		const { switcher, getState } = makeSwitcher(fullState());
		await switcher.switchSession('hidden-child');
		_mockSessions.current = [{ id: 'session-C', model: 'opus', effort: 'low' }];
		await switcher.switchSession('session-C');
		resolve({ session_meta: { model: 'gpt-5.6-terra', provider_id: 'openai', effort: 'high' } });
		await Promise.resolve();
		expect(getState()).toMatchObject({ sessionId: 'session-C', modelOverride: 'opus', effortLevelOverride: 'low' });
		switcher.destroy();
	});

	it('invalidates a pending restore when its owner is destroyed', async () => {
		let resolve!: (value: unknown) => void;
		vi.mocked(api.get).mockReturnValueOnce(new Promise(done => resolve = done));
		const { switcher, getState } = makeSwitcher(fullState({ modelOverride: null, effortLevelOverride: null }));
		const pending = switcher.refreshSessionMetadata('session-A');
		switcher.destroy();
		resolve({ session_meta: { model: 'opus', effort: 'high' } });
		await pending;
		expect(getState()).toMatchObject({ modelOverride: null, effortLevelOverride: null });
	});

	// A deep-linked chat opens with its binding cleared (session-init), so /live
	// is the whole truth: model, provider, window choice and pin all come from it.
	it('fills a deep-linked chat\'s model and window from its own row', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: {
			model: 'gpt-6.1-sol', provider_id: 'prov-gpt', effort: null, context_window: 272_000,
		} });
		const { switcher, getState } = makeSwitcher(fullState({
			sessionId: 'chat-b', modelOverride: null, effortLevelOverride: null,
			contextWindowChoice: '1m', contextWindowPin: undefined,
		}));
		await switcher.refreshSessionMetadata('chat-b');
		expect(getState()).toMatchObject({
			modelOverride: 'prov:prov-gpt:gpt-6.1-sol', contextWindowChoice: 'standard', contextWindowPin: 272_000,
		});
		switcher.destroy();
	});

	it('switching away from a deep-linked chat and back keeps that chat\'s model', async () => {
		_mockSessions.current = [
			{ id: 'chat-c', model: 'sonnet', context_window: null },
			{ id: 'chat-a', model: 'gpt-6.1-sol', provider_id: 'prov-gpt', context_window: 272_000 },
		];
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: { model: 'sonnet', provider_id: null, context_window: null } });
		const { switcher, getState } = makeSwitcher(fullState({
			sessionId: 'chat-c', modelOverride: null, effortLevelOverride: null, contextWindowPin: undefined,
		}));
		await switcher.refreshSessionMetadata('chat-c');
		expect(getState().modelOverride).toBe('sonnet');
		await switcher.switchSession('chat-a');
		expect(getState()).toMatchObject({ modelOverride: 'prov:prov-gpt:gpt-6.1-sol', contextWindowChoice: 'standard' });
		await switcher.switchSession('chat-c');
		// The switch-back cache holds the row's model, never a tab default.
		expect(getState().modelOverride).toBe('sonnet');
		switcher.destroy();
		_mockSessions.current = [];
	});

	it('keeps a window pushed while a deep link\'s metadata was in flight', async () => {
		let resolve!: (value: unknown) => void;
		vi.mocked(api.get).mockReturnValueOnce(new Promise(done => resolve = done));
		const { switcher, getState, deps } = makeSwitcher(fullState({
			sessionId: 'chat-b', modelOverride: null, contextWindowChoice: '1m', contextWindowPin: undefined,
		}));
		const pending = switcher.refreshSessionMetadata('chat-b');
		// session_updated from another client (ws-message-handlers adopts it so).
		deps.update(s => ({ ...s, contextWindowChoice: 'standard', contextWindowPin: 272_000 }));
		resolve({ session_meta: { model: 'gpt-6.1-sol', provider_id: 'prov-gpt', context_window: null } });
		await pending;
		expect(getState()).toMatchObject({
			modelOverride: 'prov:prov-gpt:gpt-6.1-sol', contextWindowChoice: 'standard', contextWindowPin: 272_000,
		});
		switcher.destroy();
	});

	it('retains unsent picks while restoring metadata and canonicalizes sidebar models', async () => {
		_mockSessions.current = [{ id: 'child', model: 'gpt-5.6-terra', provider_id: 'openai', effort: 'high' }];
		const { switcher, getState, deps } = makeSwitcher(fullState());
		await switcher.switchSession('child');
		expect(getState().modelOverride).toBe('prov:openai:gpt-5.6-terra');
		deps.update(s => ({ ...s, modelOverride: 'opus', effortLevelOverride: 'low' }));
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: { model: 'gpt-5.6-terra', provider_id: 'openai', effort: 'high' } });
		await switcher.refreshSessionMetadata('child');
		expect(getState()).toMatchObject({ modelOverride: 'opus', effortLevelOverride: 'low' });
		switcher.destroy();
		_mockSessions.current = [];
	});
});

describe('session-switcher · switchSession isolation', () => {
	it.each(['', '{"type":"doc","content":[{"type":"paragraph"}]}'])('inherits new permissions after caching an empty composer (%j)', async (draft) => {
		appMode.set('code', false);
		const { switcher, getState } = makeSwitcher(fullState({
			sessionId: null, draft, permissionModeOverride: null,
			worktreeBranch: '', worktreeId: null, worktreeIntent: 'local',
		}));
		await switcher.switchSession('another-session');
		setPersistedRunBinding('permissionMode', 'bypassPermissions');
		expect(switcher.newChat()).toBe(false);
		expect(getState().permissionModeOverride).toBe('bypassPermissions');
		switcher.destroy();
	});


	it.each(['typed draft', ''])('restores the complete staged branch draft (%j) after visiting a session', async (draft) => {
		appMode.set('code');
		const {switcher, getState} = makeSwitcher(fullState({
			sessionId:null, draft, profile:'draft-profile', project:'draft-project', projectV2Id:'draft-v2',
			modelOverride:'draft-model', permissionModeOverride:'acceptEdits',
			worktreeBranch:'feature/draft', worktreeBaseBranch:'develop', worktreeCreateNewBranch:true,
		}));
		await switcher.switchSession('another-session');
		localStorage.setItem('aihub_last_profile', 'new-global-profile');
		expect(switcher.newChat()).toBe(true);
		expect(getState()).toMatchObject({sessionId:null, draft, profile:'draft-profile',
			project:'draft-project',projectV2Id:'draft-v2',modelOverride:'draft-model',
			permissionModeOverride:'acceptEdits',worktreeBranch:'feature/draft',
			worktreeBaseBranch:'develop',worktreeCreateNewBranch:true});
		switcher.destroy();
	});

	it('hydrates folders for a chat outside the paginated session list', async () => {
		vi.mocked(api.get).mockResolvedValueOnce({ session_meta: { workspace_project_ids: ['docs'] } });
		const { switcher, getState } = makeSwitcher(fullState());
		await switcher.switchSession('older-chat');
		await vi.waitFor(() => expect(getState().workspaceProjectIds).toEqual(['docs']));
		switcher.destroy();
	});

	it('does not overwrite a folder edit with a late metadata response', async () => {
		let resolve!: (value: unknown) => void;
		vi.mocked(api.get).mockReturnValueOnce(new Promise(done => resolve = done));
		const { switcher, getState, deps } = makeSwitcher(fullState());
		await switcher.switchSession('older-chat');
		deps.update(state => ({ ...state, workspaceProjectIds: ['new-folder'] }));
		resolve({ session_meta: { workspace_project_ids: ['old-folder'] } });
		await Promise.resolve();
		expect(getState().workspaceProjectIds).toEqual(['new-folder']);
		switcher.destroy();
	});

	it('restores each chat\'s folders without leaking them into another chat or a fresh compose', async () => {
		const { switcher, getState } = makeSwitcher(fullState({ workspaceProjectIds: ['docs'] }));
		await switcher.switchSession('session-B');
		expect(getState().workspaceProjectIds).toEqual([]);
		await switcher.switchSession('session-A');
		expect(getState().workspaceProjectIds).toEqual(['docs']);
		switcher.newChat();
		expect(getState().workspaceProjectIds).toEqual([]);
		switcher.destroy();
	});

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

	it.each(['default', 'auto', 'acceptEdits', 'bypassPermissions'])('inherits %s only for a new session, not existing sessions', async (mode) => {
		setPersistedRunBinding('permissionMode', mode);
		_mockSessions.current = [{ id: 'existing-ask', permission_mode: 'default' }, { id: 'legacy' }];
		const { switcher, getState } = makeSwitcher(fullState());
		await switcher.switchSession('existing-ask');
		expect(getState().permissionModeOverride).toBe('default');
		await switcher.switchSession('legacy');
		expect(getState().permissionModeOverride).toBeNull();
		switcher.newChat();
		expect(getState().permissionModeOverride).toBe(mode);
		switcher.destroy();
	});

	it('restores the complete last-used binding for a new chat', () => {
		localStorage.setItem('aihub_lastModel', 'prov:openai:gpt-5.6-sol');
		composerPreferences.set('modelEfforts', {'gpt-5.6-sol':'xhigh'});
		localStorage.setItem('aihub_lastPermissionMode', 'plan');
		localStorage.setItem('aihub_lastContextWindowChoice', 'standard');
		const { switcher, getState } = makeSwitcher(fullState());

		switcher.newChat();
		switcher.destroy();

		expect(getState().modelOverride).toBe('prov:openai:gpt-5.6-sol');
		expect(getState().effortLevelOverride).toBe('xhigh');
		expect(getState().permissionModeOverride).toBe('default');
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
		expect(s.permissionModeOverride).toBe('default');
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

describe('session mode lanes', () => {
	beforeEach(() => {
		_mockSessions.current = [];
		localStorage.clear();
		appMode.set('code');
	});

	it('takes an existing session lane from its row, never from the sidepanel pick', async () => {
		// The user is browsing the chat lane, but opens a code session from a
		// deep link / another device. The row wins, or the surface would render
		// the stripped chat composer over a session that owns a worktree.
		_mockSessions.current = [{ id: 'session-B' }];
		appMode.set('chat');
		const { switcher, getState } = makeSwitcher(fullState({ mode: 'chat' }));

		await switcher.switchSession('session-B');
		switcher.destroy();

		expect(getState().mode).toBe('code');
	});

	it('reads a voice row lane and does not inherit the leaving session lane', async () => {
		_mockSessions.current = [{ id: 'session-B', mode: 'voice' }];
		const { switcher, getState } = makeSwitcher(fullState({ mode: 'chat' }));

		await switcher.switchSession('session-B');
		switcher.destroy();

		expect(getState().mode).toBe('voice');
	});

	it('opens a new chat in the lane the sidepanel is showing', () => {
		appMode.set('chat');
		const { switcher, getState } = makeSwitcher(fullState());

		switcher.newChat();
		switcher.destroy();

		expect(getState().mode).toBe('chat');
	});

	it.each([true, false])('never starts a new conversation in the voice lane (persisted=%s)', (persist) => {
		// Any new-chat path (sidebar, deleting the open voice session, fix-with-AI)
		// lands here after a historical voice session put the lane on 'voice'.
		appMode.set('voice', persist);
		const { switcher, getState } = makeSwitcher(fullState({ mode: 'voice' }));

		switcher.newChat();
		switcher.destroy();

		expect(getState().mode).toBe('chat');
		expect(appMode.current).toBe('chat');
	});

	it('starts the projectless lanes with no repo, so no worktree can be cut', () => {
		// The user was last in a code session on proj-A with the worktree toggle
		// remembered as 'worktree'. Neither may follow them into chat mode: the
		// chat composer has no project or branch control to undo it with.
		localStorage.setItem('aihub_executionMode', 'worktree');
		localStorage.setItem('aihub_selectedProject', 'proj-A');
		appMode.set('chat');
		const { switcher, getState } = makeSwitcher(fullState({
			project: 'proj-A',
			projectV2Id: 'pv2-a',
			worktreeIntent: 'worktree',
			worktreeBranch: 'feature/x',
			selectedWorktreeBranch: 'feature/x',
			worktreeId: 'wt-1',
		}));

		switcher.newChat();
		switcher.destroy();

		const s = getState();
		expect(s.project).toBe('');
		expect(s.projectV2Id).toBeNull();
		expect(s.worktreeIntent).toBe('local');
		expect(s.worktreeBranch).toBe('');
		expect(s.selectedWorktreeBranch).toBeNull();
		expect(s.worktreeId).toBeNull();
	});

	it('still restores the remembered project and worktree intent in code mode', () => {
		localStorage.setItem('aihub_executionMode', 'worktree');
		localStorage.setItem('aihub_selectedProject', 'proj-A');
		const { switcher, getState } = makeSwitcher(fullState({ project: 'proj-A' }));

		switcher.newChat();
		switcher.destroy();

		expect(getState().project).toBe('proj-A');
		expect(getState().worktreeIntent).toBe('worktree');
	});

	it('does not carry a code-lane permission mode into chat', () => {
		// The chat and voice composers render no tool-access control, so an
		// inherited 'plan' would be unchangeable for the life of the session —
		// and undischargeable, the plan-exit tool not being on the chat ceiling.
		localStorage.setItem('aihub_lastPermissionMode', 'plan');
		appMode.set('chat');
		const { switcher, getState } = makeSwitcher(fullState());

		switcher.newChat();
		switcher.destroy();

		expect(getState().permissionModeOverride).toBeNull();
	});

	it.each(['code'] as const)('restores project, worktree and permission for %s compose', (mode) => {
		appMode.set(mode);
		localStorage.setItem('aihub_executionMode', 'worktree');
		localStorage.setItem('aihub_selectedProject', 'proj-A');
		localStorage.setItem('aihub_lastPermissionMode', 'plan');
		const { switcher, getState } = makeSwitcher(fullState());

		switcher.newChat();
		switcher.destroy();

		expect(getState().permissionModeOverride).toBe('default');
	});
});

describe('session-switcher · reconciledSessionMode', () => {
	const rows = [
		{ id: 'code-1', mode: null },
		{ id: 'chat-1', mode: 'chat' },
		{ id: 'voice-1', mode: 'voice' },
		{ id: 'weird-1', mode: 'nonsense' },
	];

	it('reads the lane off the row, NULL included', () => {
		expect(reconciledSessionMode('code-1', rows)).toBe('code');
		expect(reconciledSessionMode('chat-1', rows)).toBe('chat');
		expect(reconciledSessionMode('voice-1', rows)).toBe('voice');
	});

	it('normalizes an unknown value to the code lane', () => {
		expect(reconciledSessionMode('weird-1', rows)).toBe('code');
	});

	it('defers to nothing when there is no session or no row', () => {
		// Both cases must leave the caller's current mode alone rather than
		// asserting 'code': an unbound compose owns its lane, and a session
		// missing from the list (foreign, filtered, deleted) has no row to read.
		expect(reconciledSessionMode(null, rows)).toBeNull();
		expect(reconciledSessionMode('not-in-list', rows)).toBeNull();
	});
});

describe('leaving a session with an unanswered prose question (spec §5)', () => {
	const asked = [
		{ id: 'u1', role: 'user' as const, content: 'Deploy it' },
		{ id: 'a1', role: 'assistant' as const, type: 'text' as const, content: 'Staging too, or prod only?' },
	];
	beforeEach(() => { _resetProseQuestions(); _mockSessions.current = []; });

	it('A -> B -> A keeps the question remembered, and answering it clears it on the next leave', async () => {
		_mockSessions.current = [{ id: 'session-A', work_started_at: 'T1' }];
		const { switcher, deps } = makeSwitcher(fullState({ messages: asked, isStreaming: true }));
		await switcher.switchSession('session-B');
		expect(get(proseQuestionSessions).has('session-A')).toBe(true);
		await switcher.switchSession('session-A');
		// Leaving B (no question) records nothing for it and keeps A.
		expect([...get(proseQuestionSessions).keys()]).toEqual(['session-A']);
		// History hydrated, then the user answered.
		deps.update((s) => ({ ...s, isLoadingSession: false, messages: [...asked, { id: 'u2', role: 'user', content: 'Prod only' }] }));
		switcher.newChat();
		expect(get(proseQuestionSessions).has('session-A')).toBe(false);
	});

	const E = 'evt-epoch-1';
	const rev = (seq: number | null, epoch: string | null = E) => ({
		last_user_message_seq: seq,
		last_user_message_epoch: epoch
	});
	/** A transcript whose trailing question was rendered from event `seq`. */
	const positioned = (seq: number, epoch = E): ChatMessage[] => [
		{ id: 'u1', role: 'user', content: 'Deploy it', seq: seq - 1, epoch },
		{ id: 'a1', role: 'assistant', type: 'text', content: 'Staging too, or prod only?', seq, epoch }
	];

	it('only a later seq in the question\'s log clears it after leaving (#2249)', async () => {
		_mockSessions.current = [{ id: 'session-A', ...rev(4) }];
		const { switcher } = makeSwitcher(fullState({ messages: positioned(5) }));
		await switcher.switchSession('session-B');
		observeLastUserMessage('session-A', rev(4));
		observeLastUserMessage('session-A', rev(9, 'evt-other'));
		expect(get(proseQuestionSessions).has('session-A')).toBe(true);
		observeLastUserMessage('session-A', rev(6));
		expect(get(proseQuestionSessions).has('session-A')).toBe(false);
	});

	it('does not mark a question whose answer the loaded row already carries', async () => {
		_mockSessions.current = [{ id: 'session-A', ...rev(6) }];
		const { switcher } = makeSwitcher(fullState({ messages: positioned(5) }));
		await switcher.switchSession('session-B');
		expect(get(proseQuestionSessions).has('session-A')).toBe(false);
	});

	describe('the opened session\'s own revision comes from its /live snapshot (#2249)', () => {
		const isLive = (url: string) => url.startsWith('/sessions/session-A/live');
		const detailCalls = () => vi.mocked(api.get).mock.calls.filter(([url]) => /^\/sessions\/session-A(\?|$)/.test(url)).length;
		const liveCalls = () => vi.mocked(api.get).mock.calls.filter(([url]) => isLive(url)).length;
		const leaveA = async (
			switcher: ReturnType<typeof makeSwitcher>['switcher'],
			deps: ReturnType<typeof makeSwitcher>['deps'],
			messages: ChatMessage[] = positioned(5)
		) => {
			deps.update((st) => ({ ...st, messages, isLoadingSession: false, isRestoringSession: false }));
			await switcher.switchSession('session-B');
		};
		beforeEach(() => { vi.mocked(api.get).mockReset().mockResolvedValue(null); });
		afterEach(() => { vi.mocked(api.get).mockReset().mockResolvedValue(null); });

		it('reads the revision from /live session_meta when no row is loaded, without a detail request', async () => {
			_mockSessions.current = [];
			vi.mocked(api.get).mockImplementation(async (url: string) =>
				isLive(url) ? { session_meta: rev(6) } : null);
			const { switcher, deps } = makeSwitcher(fullState({ sessionId: 'session-B', messages: [] }));
			await switcher.switchSession('session-A');
			await vi.waitFor(() => expect(liveCalls()).toBe(1));
			await new Promise((r) => setTimeout(r, 0));
			// The transcript lags: the answer at seq 6 was already seen via /live.
			await leaveA(switcher, deps);
			expect(get(proseQuestionSessions).has('session-A')).toBe(false);
			expect(detailCalls()).toBe(0);
		});

		it('a pre-question /live value keeps the mark; a cross-device answer afterwards clears it', async () => {
			_mockSessions.current = [];
			vi.mocked(api.get).mockImplementation(async (url: string) =>
				isLive(url) ? { session_meta: rev(4) } : null);
			const { switcher, deps } = makeSwitcher(fullState({ sessionId: 'session-B', messages: [] }));
			await switcher.switchSession('session-A');
			await vi.waitFor(() => expect(liveCalls()).toBe(1));
			await new Promise((r) => setTimeout(r, 0));
			await leaveA(switcher, deps);
			expect(get(proseQuestionSessions).has('session-A')).toBe(true);
			observeLastUserMessage('session-A', rev(4));
			expect(get(proseQuestionSessions).has('session-A')).toBe(true);
			observeLastUserMessage('session-A', rev(6));
			expect(get(proseQuestionSessions).has('session-A')).toBe(false);
		});

		it('a late /live snapshot carrying the pre-question value never clears, nor does a refresh of it', async () => {
			_mockSessions.current = [];
			let resolveLive!: (v: unknown) => void;
			vi.mocked(api.get).mockImplementation((url: string) =>
				isLive(url) && !resolveLive ? new Promise((done) => { resolveLive = done; }) : Promise.resolve(null));
			const { switcher, deps } = makeSwitcher(fullState({ sessionId: 'session-B', messages: [] }));
			await switcher.switchSession('session-A');
			await vi.waitFor(() => expect(resolveLive).toBeDefined());
			observeLastUserMessage('session-A', rev(4));
			await leaveA(switcher, deps);
			resolveLive({ session_meta: rev(4) });
			await new Promise((r) => setTimeout(r, 0));
			observeLastUserMessage('session-A', rev(4));
			expect(get(proseQuestionSessions).has('session-A')).toBe(true);
		});

		it('a /live snapshot taken after the answer clears the mark when it lands after leaving', async () => {
			_mockSessions.current = [];
			let resolveLive!: (v: unknown) => void;
			vi.mocked(api.get).mockImplementation((url: string) =>
				isLive(url) && !resolveLive ? new Promise((done) => { resolveLive = done; }) : Promise.resolve(null));
			const { switcher, deps } = makeSwitcher(fullState({ sessionId: 'session-B', messages: [] }));
			await switcher.switchSession('session-A');
			await vi.waitFor(() => expect(resolveLive).toBeDefined());
			await leaveA(switcher, deps);
			expect(get(proseQuestionSessions).has('session-A')).toBe(true);
			resolveLive({ session_meta: rev(7) });
			await vi.waitFor(() => expect(get(proseQuestionSessions).has('session-A')).toBe(false));
		});

		it('overlapping visits: an earlier visit\'s late snapshot cannot clear a newer question', async () => {
			_mockSessions.current = [];
			const pending: Array<(v: unknown) => void> = [];
			vi.mocked(api.get).mockImplementation((url: string) =>
				isLive(url) ? new Promise((done) => { pending.push(done); }) : Promise.resolve(null));
			const { switcher, deps } = makeSwitcher(fullState({ sessionId: 'session-B', messages: [] }));
			await switcher.switchSession('session-A');
			await vi.waitFor(() => expect(pending.length).toBe(1));
			await leaveA(switcher, deps, positioned(3));
			// Answered elsewhere at seq 7; visit 2 hydrates a NEW question (seq 9) and leaves.
			await switcher.switchSession('session-A');
			await vi.waitFor(() => expect(pending.length).toBe(2));
			await leaveA(switcher, deps, [
				...positioned(3),
				{ id: 'u2', role: 'user', content: 'Prod only', seq: 7, epoch: E },
				{ id: 'a2', role: 'assistant', type: 'text', content: 'Which region?', seq: 9, epoch: E }
			]);
			pending[1]({ session_meta: rev(7) });
			pending[0]({ session_meta: rev(2) });
			await new Promise((r) => setTimeout(r, 0));
			expect(get(proseQuestionSessions).has('session-A')).toBe(true);
			observeLastUserMessage('session-A', rev(10));
			expect(get(proseQuestionSessions).has('session-A')).toBe(false);
		});

		it('records a loaded row\'s revision on switch-in, so filtering the row out before leaving keeps it', async () => {
			_mockSessions.current = [{ id: 'session-A', ...rev(6) }];
			const { switcher, deps } = makeSwitcher(fullState({ sessionId: 'session-B', messages: [] }));
			await switcher.switchSession('session-A');
			// A tag filter drops A's row while A stays open.
			_mockSessions.current = [];
			await leaveA(switcher, deps);
			await new Promise((r) => setTimeout(r, 0));
			expect(detailCalls()).toBe(0);
			// The answer (seq 6) was already seen on the row: no mark.
			expect(get(proseQuestionSessions).has('session-A')).toBe(false);
		});
	});

	it('a question without a position never clears on a revision alone', async () => {
		_mockSessions.current = [{ id: 'session-A', ...rev(null, null) }];
		const { switcher } = makeSwitcher(fullState({ messages: asked }));
		await switcher.switchSession('session-B');
		observeLastUserMessage('session-A', rev(50));
		expect(get(proseQuestionSessions).has('session-A')).toBe(true);
	});

	it('leaving A while its uncached history is still loading keeps the remembered question', async () => {
		_mockSessions.current = [{ id: 'session-A', work_started_at: 'T1' }];
		const { switcher, deps } = makeSwitcher(fullState({ messages: asked }));
		await switcher.switchSession('session-B');
		expect(get(proseQuestionSessions).has('session-A')).toBe(true);
		// Reopen A with no cache: the switch starts it empty and loading.
		deps.update((s) => ({ ...s, sessionId: 'session-A', messages: [], isLoadingSession: true }));
		await switcher.switchSession('session-B');
		expect(get(proseQuestionSessions).has('session-A')).toBe(true);
		// A partial replay is not authoritative either.
		deps.update((s) => ({ ...s, sessionId: 'session-A', messages: [asked[0]], isLoadingSession: true }));
		switcher.newChat();
		expect(get(proseQuestionSessions).has('session-A')).toBe(true);
	});
});
