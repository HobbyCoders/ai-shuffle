/**
 * #1756 review: a reload while an automatic-worktree opening is still being
 * created restores its auto-named branch with `worktreeCreateNewBranch`, but
 * `isCreatingWorktree` is false on the new page. New Chat (or opening another
 * chat) while the page is still checking delivery must not keep that branch
 * for the next task: it belongs to the first task's in-flight create, and the
 * next send would try to create the same branch and get 409. Drives the real
 * activeSession store with only the network stubbed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./session/ws-connection', () => ({ createWsConnection: () => ({
	connect: vi.fn(), reconnect: vi.fn(), disconnect: vi.fn(), send: vi.fn(() => true), isConnected: () => true,
	resetLastMessageTime: vi.fn(), resetReconnectAttempts: vi.fn(), destroy: vi.fn(),
}) }));
vi.mock('$app/navigation', () => ({ goto: vi.fn() }));

import { activeSession } from './active-session';
import { api } from '$lib/api/client';
import { appData } from './app-data.svelte';
import { appMode } from './sessionMode.svelte';
import { composerPreferences } from './composer-preferences';
import { git } from './git.svelte';
import { sessionEphemeralCache } from './session-cache';
import { TAB_ACTIVE_SESSION_KEY } from './device-id';
import { getInitGeneration } from './session/session-init';
import { finishOpening, keepOpening, recoveringOpening, type Opening } from './session/worktree-opening';

const PROJECT = 'shuffle-scratch';
const OPENING_BRANCH = 'feat/auto-named-opening';

/** The compose a reload restored from a snapshot saved mid-opening. */
function restoreComposeMidOpening(branch = OPENING_BRANCH) {
	Object.assign(activeSession.current, {
		sessionId: null, mode: 'code', title: 'New Chat', draft: '', messages: [],
		profile: 'claude-code', project: PROJECT, projectV2Id: null, worktreeIntent: 'worktree',
		worktreeBranch: branch, worktreeCreateNewBranch: true, worktreeCheckoutPending: false,
		worktreeId: null, selectedWorktreeBranch: null, worktreeMode: 'new', isCreatingWorktree: false,
	});
}

/** The dispatched opening boot adopted for this page (AppPage), still unconfirmed. */
function recoverOpening(overrides: Partial<Opening> = {}): Opening {
	const note: Opening = { id: 'opening-1', project: PROJECT, shared: null, generation: getInitGeneration(),
		prompt: 'Fix the flaky journey', dispatched: true, ...overrides };
	keepOpening(note);
	recoveringOpening.set({ note, ready: true });
	return note;
}

function savedTabSlot() {
	return JSON.parse(sessionStorage.getItem(TAB_ACTIVE_SESSION_KEY) as string);
}

beforeEach(() => {
	vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 200 })));
	sessionEphemeralCache.clear();
	appMode.set('code', false);
	vi.spyOn(appData, 'current', 'get').mockReturnValue({
		...appData.current, projects: [{ id: PROJECT }], clusterProjects: [],
	} as unknown as typeof appData.current);
	vi.spyOn(appData, 'loadSessions').mockResolvedValue(undefined as never);
	vi.spyOn(appData, 'loadApiUserSessions').mockResolvedValue(undefined as never);
	vi.spyOn(api, 'get').mockResolvedValue(null as never);
	vi.spyOn(git, 'current', 'get').mockReturnValue({projectId:PROJECT, hydrated:true, stale:false, status:{is_git_repo:true}} as typeof git.current);
	composerPreferences.set('profile', 'claude-code');
	composerPreferences.set('project', { projectId: PROJECT, projectV2Id: null });
	composerPreferences.set('worktreeIntent', 'worktree');
	composerPreferences.set('checkout', null);
});
afterEach(() => {
	finishOpening('opening-1');
	recoveringOpening.set(null);
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

describe('New Chat during a restored opening (#1756)', () => {
	it('does not keep or save the opening\'s auto-named branch', () => {
		restoreComposeMidOpening();
		recoverOpening();

		activeSession.newChat();

		expect(activeSession.current).toMatchObject({ sessionId: null, worktreeBranch: '', worktreeCreateNewBranch: false,
			worktreeCheckoutPending: false });
		// What a reload of the new draft reads back.
		expect(savedTabSlot()).toMatchObject({ worktreeBranch: '', worktreeCreateNewBranch: false });
		// And a later New Chat does not bring it back either.
		activeSession.newChat();
		expect(activeSession.current.worktreeBranch).toBe('');
	});

	it('does not keep it after opening another chat and coming back to New Chat', () => {
		restoreComposeMidOpening();
		recoverOpening();

		activeSession.switchSession('session-b', PROJECT);
		activeSession.newChat();

		expect(activeSession.current).toMatchObject({ sessionId: null, worktreeBranch: '', worktreeCreateNewBranch: false });
	});

	it('keeps an unsent branch pick when no opening is being recovered', () => {
		restoreComposeMidOpening('feat/my-own-pick');

		activeSession.newChat();

		expect(activeSession.current).toMatchObject({ project: PROJECT, worktreeBranch: 'feat/my-own-pick', worktreeCreateNewBranch: true });
	});

	it('keeps an unsent branch pick when the opening note is from an earlier page', () => {
		restoreComposeMidOpening('feat/my-own-pick');
		// Left over from a navigation before this page: boot does not adopt it.
		keepOpening({ id: 'opening-1', project: PROJECT, shared: null, generation: getInitGeneration() - 1,
			prompt: 'Old opening', dispatched: true });

		activeSession.newChat();

		expect(activeSession.current).toMatchObject({ project: PROJECT, worktreeBranch: 'feat/my-own-pick', worktreeCreateNewBranch: true });
	});

	it('keeps an unsent branch pick in another project than the opening\'s', () => {
		restoreComposeMidOpening('feat/my-own-pick');
		recoverOpening({ project: 'other-project' });

		activeSession.newChat();

		expect(activeSession.current).toMatchObject({ project: PROJECT, worktreeBranch: 'feat/my-own-pick' });
	});
});
