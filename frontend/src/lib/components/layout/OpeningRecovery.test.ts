/**
 * #1756 review: once the receipt check confirms a reloaded tab's opening, the
 * compose it came from must not keep that opening's auto-named branch. On a
 * phone with the drawer open `openSession` switches on the next animation
 * frame, after the note and the recovery are already retired, so the switch
 * itself cannot tell the leaving compose held the opening. Mounts the real
 * component over the real activeSession store with only the network stubbed.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/svelte';

vi.mock('$lib/stores/session/ws-connection', () => ({ createWsConnection: () => ({
	connect: vi.fn(), reconnect: vi.fn(), disconnect: vi.fn(), send: vi.fn(() => true), isConnected: () => true,
	resetLastMessageTime: vi.fn(), resetReconnectAttempts: vi.fn(), destroy: vi.fn(),
}) }));
vi.mock('$app/navigation', () => ({ goto: vi.fn(), pushState: vi.fn(), replaceState: vi.fn() }));

import OpeningRecovery from './OpeningRecovery.svelte';
import { activeSession } from '$lib/stores/active-session';
import { api } from '$lib/api/client';
import { appData } from '$lib/stores/app-data.svelte';
import { appMode } from '$lib/stores/sessionMode.svelte';
import { composerPreferences } from '$lib/stores/composer-preferences';
import { git } from '$lib/stores/git.svelte';
import { layout } from '$lib/stores/layout.svelte';
import { sessionEphemeralCache } from '$lib/stores/session-cache';
import { TAB_ACTIVE_SESSION_KEY } from '$lib/stores/device-id';
import { getInitGeneration } from '$lib/stores/session/session-init';
import { finishOpening, keepOpening, pendingOpening, recoveringOpening, type Opening } from '$lib/stores/session/worktree-opening';

const PROJECT = 'shuffle-scratch';
const OPENING_BRANCH = 'feat/auto-named-opening';
const ADMITTED = 'session-admitted';

/** The compose a reload restored from a snapshot saved mid-opening. */
function restoreComposeMidOpening() {
	Object.assign(activeSession.current, {
		sessionId: null, mode: 'code', title: 'New Chat', draft: '', messages: [],
		profile: 'claude-code', project: PROJECT, projectV2Id: null, worktreeIntent: 'worktree',
		worktreeBranch: OPENING_BRANCH, worktreeCreateNewBranch: true, worktreeCheckoutPending: false,
		worktreeId: null, selectedWorktreeBranch: null, worktreeMode: 'new', isCreatingWorktree: false,
	});
}

/** The dispatched opening boot adopted for this page (AppPage). */
function recoverOpening(): Opening {
	const note: Opening = { id: 'opening-1', project: PROJECT, shared: null, generation: getInitGeneration(),
		prompt: 'Fix the flaky journey', dispatched: true };
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
		sessions: [{ id: ADMITTED, project_id: PROJECT, worktree_id: 'wt-1', worktree_branch: OPENING_BRANCH,
			title: 'Fix the flaky journey', mode: 'code' }],
	} as unknown as typeof appData.current);
	vi.spyOn(appData, 'loadSessions').mockResolvedValue(undefined as never);
	vi.spyOn(appData, 'loadApiUserSessions').mockResolvedValue(undefined as never);
	vi.spyOn(api, 'get').mockResolvedValue(null as never);
	vi.spyOn(api, 'post').mockImplementation(async (path: string) =>
		(path === '/sessions/with-worktree/receipt' ? { session_id: ADMITTED } : {}) as never);
	vi.spyOn(git, 'current', 'get').mockReturnValue({projectId:PROJECT, hydrated:true, stale:false, status:{is_git_repo:true}} as typeof git.current);
	composerPreferences.set('profile', 'claude-code');
	composerPreferences.set('project', { projectId: PROJECT, projectV2Id: null });
	composerPreferences.set('worktreeIntent', 'worktree');
	composerPreferences.set('checkout', null);
});
afterEach(() => {
	finishOpening('opening-1');
	recoveringOpening.set(null);
	layout.setSidebarOverlay(false);
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
});

describe('OpeningRecovery confirming a reloaded opening (#1756)', () => {
	it.each([
		['the phone drawer is open (switch on the next frame)', true],
		['the switch is immediate', false],
	])('opens the admitted chat and leaves no opening branch on New Chat when %s', async (_name, drawerOpen) => {
		const frames: FrameRequestCallback[] = [];
		vi.stubGlobal('requestAnimationFrame', (frame: FrameRequestCallback) => frames.push(frame));
		layout.setSidebarOverlay(drawerOpen);
		if (drawerOpen) layout.setSidebarCollapsed(false);
		restoreComposeMidOpening();
		const note = recoverOpening();

		render(OpeningRecovery, { props: { note, ready: true } });

		await waitFor(() => expect(pendingOpening()).toBeNull());
		if (drawerOpen) {
			// Note and recovery retired, switch not run yet: the compose is clean
			// already, and Worktree stays on.
			expect(activeSession.current).toMatchObject({ sessionId: null, worktreeIntent: 'worktree', worktreeBranch: '',
				worktreeCreateNewBranch: false });
			expect(savedTabSlot()).toMatchObject({ sessionId: null, worktreeBranch: '', worktreeCreateNewBranch: false });
		}
		frames.splice(0).forEach(frame => frame(0));
		expect(activeSession.current.sessionId).toBe(ADMITTED);
		// The admitted chat shows its own branch, from its row.
		expect(activeSession.current.worktreeBranch).toBe(OPENING_BRANCH);

		activeSession.newChat();

		expect(activeSession.current).toMatchObject({ sessionId: null, worktreeBranch: '',
			worktreeCreateNewBranch: false, worktreeCheckoutPending: false });
		expect(savedTabSlot()).toMatchObject({ worktreeBranch: '', worktreeCreateNewBranch: false });
	});

	it('clears nothing when the user left the compose before the receipt answered', async () => {
		restoreComposeMidOpening();
		const note = recoverOpening();
		let answer!: (reply: { session_id: string }) => void;
		vi.mocked(api.post).mockImplementation(() => new Promise(done => { answer = done; }) as never);

		render(OpeningRecovery, { props: { note, ready: true } });
		await waitFor(() => expect(answer).toBeTypeOf('function'));
		// Another project's compose, with a branch of its own.
		Object.assign(activeSession.current, { project: 'other-project', worktreeBranch: 'feat/mine' });
		answer({ session_id: ADMITTED });
		await new Promise(done => setTimeout(done, 0));

		expect(activeSession.current).toMatchObject({ sessionId: null, project: 'other-project', worktreeBranch: 'feat/mine',
			worktreeCreateNewBranch: true });
	});
});
