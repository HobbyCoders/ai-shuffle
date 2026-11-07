/**
 * #1528 live-test failure: a branch made with the composer's New Branch modal
 * stayed staged for the next New Chat, so the next send silently opened a
 * second session on the same branch and worktree. Owner decision: every branch
 * pick in the new-session composer (New branch... or a branch from the list)
 * is one-time. These tests drive the real
 * activeSession store (staging, send-time creation, New Chat and the remembered
 * composer defaults) with only the network stubbed.
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

const PROJECT = 'shuffle-scratch';
const BRANCH = 'live/1528-explicit';
type Created = { session_id: string; branch_name: string; base_branch: string; worktree_path: string; status: string };
const created = (id: string, branch = BRANCH): Created =>
	({ session_id: id, branch_name: branch, base_branch: 'main', worktree_path: `/w/${id}`, status: 'ok' });

let post: ReturnType<typeof vi.spyOn>;

function deferred() {
	let resolve!: (v: Created) => void; let reject!: (e: unknown) => void;
	const promise = new Promise<Created>((res, rej) => { resolve = res; reject = rej; });
	return { promise, resolve, reject };
}

beforeEach(() => {
	// Session persistence is a background PUT; it is not under test here.
	vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 200 })));
	// Start from a clean compose: no draft or staged branch left by the last test.
	Object.assign(activeSession.current, { sessionId: null, draft: '', worktreeBranch: '', worktreeId: null, isCreatingWorktree: false });
	sessionEphemeralCache.clear();
	appMode.set('code', false);
	vi.spyOn(appData, 'current', 'get').mockReturnValue({
		...appData.current, projects: [{ id: PROJECT }], clusterProjects: [],
	} as unknown as typeof appData.current);
	vi.spyOn(appData, 'loadSessions').mockResolvedValue(undefined as never);
	vi.spyOn(appData, 'loadApiUserSessions').mockResolvedValue(undefined as never);
	post = vi.spyOn(api, 'post');
	vi.spyOn(git, 'current', 'get').mockReturnValue({projectId:PROJECT, hydrated:true, stale:false, status:{is_git_repo:true}} as typeof git.current);
	composerPreferences.set('profile', 'claude-code');
	composerPreferences.set('project', { projectId: PROJECT, projectV2Id: null });
	composerPreferences.set('worktreeIntent', 'worktree');
	composerPreferences.set('checkout', null);
	activeSession.newChat();
	Object.assign(activeSession.current, { profile: 'claude-code', project: PROJECT, projectV2Id: null, worktreeIntent: 'worktree' });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

/**
 * A new session starts without a project (#371 step 4); the user adds it on
 * the new-session screen, which re-reads that project's checkout defaults —
 * the same path the one-time branch choice rides on.
 */
function newProjectChat() {
	activeSession.newChat();
	activeSession.setProject(PROJECT, null);
}

/**
 * ExecutionModeBar stages both a branch the New branch modal just created and a
 * branch picked from the list with these same calls.
 */
function stagePick(name = BRANCH) {
	activeSession.setWorktreeBranch(name);
	activeSession.setWorktreeMode('new');
	activeSession.setWorktreeCreateNewBranch(false);
	expect(activeSession.current).toMatchObject({ worktreeBranch: name, worktreeCreateNewBranch: false, worktreeMode: 'new' });
}

describe('one-time branch choice (#1528)', () => {
	it.each([BRANCH, 'feature/existing'])('%s is consumed after the session is created; the next New Chat falls back to Auto', async (branch) => {
		stagePick(branch);
		activeSession.setWorktreeBaseBranch('develop');
		expect(composerPreferences.current.checkout).toEqual({ kind: 'branch', projectId: PROJECT, projectV2Id: null,
			branch, baseBranch: 'develop', createNew: false });
		post.mockResolvedValueOnce(created('session-c', branch));
		await activeSession.createWorktree();
		expect(post).toHaveBeenCalledWith('/sessions/with-worktree', expect.objectContaining({
			project_id: PROJECT, branch_name: branch, base_branch: 'develop', create_new_branch: false }));
		expect(activeSession.current).toMatchObject({ sessionId: 'session-c', worktreeBranch: branch });
		expect(composerPreferences.current.checkout).toBeNull();
		expect(composerPreferences.worktreeForProject({projectId:PROJECT,projectV2Id:null}).checkout).toBeNull();

		newProjectChat();
		// Auto from the default base: the picked branch's base was part of the spent choice.
		expect(activeSession.current).toMatchObject({ sessionId: null, project: PROJECT, worktreeIntent: 'worktree',
			worktreeBranch: '', worktreeBaseBranch: 'main', worktreeId: null, worktreeCreateNewBranch: false });
	});

	it('is not staged by a New Chat opened while the first create is still in flight', async () => {
		stagePick();
		const first = deferred();
		post.mockReturnValueOnce(first.promise);
		const pending = activeSession.createWorktree();
		expect(activeSession.current.isCreatingWorktree).toBe(true);

		newProjectChat();
		expect(activeSession.current).toMatchObject({ sessionId: null, worktreeBranch: '', isCreatingWorktree: false });
		// The choice is still remembered (the create may yet fail), only spoken for.
		expect(composerPreferences.current.checkout?.branch).toBe(BRANCH);
		activeSession.applyComposerDefaults();
		expect(activeSession.current.worktreeBranch).toBe('');

		first.resolve(created('session-c'));
		await pending;
		expect(composerPreferences.current.checkout).toBeNull();
		// The new compose was never bound to session C.
		expect(activeSession.current).toMatchObject({ sessionId: null, worktreeBranch: '' });
		newProjectChat();
		expect(activeSession.current.worktreeBranch).toBe('');
	});

	it('keeps the choice when the create fails, in place and for the next New Chat', async () => {
		stagePick();
		post.mockRejectedValueOnce({ status: 500, detail: 'worktree add failed' });
		await activeSession.createWorktree();
		expect(activeSession.current).toMatchObject({ sessionId: null, worktreeBranch: BRANCH, isCreatingWorktree: false });
		expect(activeSession.current.worktreeError).toBeTruthy();
		expect(composerPreferences.current.checkout?.branch).toBe(BRANCH);

		Object.assign(activeSession.current, { worktreeBranch: '', draft: '' });
		newProjectChat();
		expect(activeSession.current.worktreeBranch).toBe(BRANCH);
	});

	it('keeps the choice when an in-flight create fails after the user started a New Chat', async () => {
		stagePick();
		const first = deferred();
		post.mockReturnValueOnce(first.promise);
		const pending = activeSession.createWorktree();
		newProjectChat();
		first.reject({ status: 500, detail: 'worktree add failed' });
		await pending;
		expect(composerPreferences.checkoutClaimed).toBe(false);
		expect(composerPreferences.current.checkout?.branch).toBe(BRANCH);
		newProjectChat();
		expect(activeSession.current.worktreeBranch).toBe(BRANCH);
	});

	it('keeps a newer pick made while the first create was in flight', async () => {
		stagePick();
		const first = deferred();
		post.mockReturnValueOnce(first.promise);
		const pending = activeSession.createWorktree();
		newProjectChat();
		stagePick('live/next-task');
		first.resolve(created('session-c'));
		await pending;
		expect(composerPreferences.current.checkout?.branch).toBe('live/next-task');
		expect(activeSession.current.worktreeBranch).toBe('live/next-task');
	});

	// #1078 review: the catalog proves the project's local identity replaced
	// while the create is in flight.
	describe('identity replaced during the create', () => {
		// Fresh identities per case: the top-level reset clears only the no-ID key.
		type CatalogRow = { project_v2_id: string; local_project_id: string; origin: string };
		let run = 0, LOCAL: CatalogRow, SHARED: CatalogRow, MOVED: CatalogRow;
		let local: { projectId: string; projectV2Id: string }, shared: typeof local, moved: typeof local;
		beforeEach(() => {
			run++;
			LOCAL = { project_v2_id: `pv2-local-${run}`, local_project_id: PROJECT, origin: `local:${PROJECT}` };
			SHARED = { project_v2_id: `pv2-shared-${run}`, local_project_id: PROJECT, origin: `git:shared-${run}` };
			MOVED = { project_v2_id: `pv2-moved-${run}`, local_project_id: PROJECT, origin: `git:moved-${run}` };
			local = { projectId: PROJECT, projectV2Id: LOCAL.project_v2_id };
			shared = { projectId: PROJECT, projectV2Id: SHARED.project_v2_id };
			moved = { projectId: PROJECT, projectV2Id: MOVED.project_v2_id };
		});
		/** The catalog replaces the listed identity with `next`. */
		function replaceWith(next: CatalogRow) {
			activeSession.linkDiscoveredProjects([next]);
			listCatalog([next]);
		}
		/** The compose holds the local identity with the branch staged. */
		function composeUnderLocal() {
			composerPreferences.set('project', local);
			Object.assign(activeSession.current, { project: PROJECT, projectV2Id: local.projectV2Id, worktreeIntent: 'worktree' });
			stagePick();
		}
		function listCatalog(clusterProjects: unknown[]) {
			const base = appData.current;
			vi.spyOn(appData, 'current', 'get').mockReturnValue({ ...base, clusterProjects } as unknown as typeof appData.current);
		}
		async function createWhileReplaced(outcome: 'fail' | 'succeed') {
			listCatalog([LOCAL]);
			composerPreferences.set('project', local);
			Object.assign(activeSession.current, { projectV2Id: local.projectV2Id });
			stagePick();
			const first = deferred();
			post.mockReturnValueOnce(first.promise);
			const pending = activeSession.createWorktree();
			activeSession.linkDiscoveredProjects([SHARED]);
			listCatalog([SHARED]);
			expect(activeSession.current.projectV2Id).toBe(local.projectV2Id);
			if (outcome === 'fail') first.reject({ status: 500, detail: 'worktree add failed' });
			else first.resolve(created('session-c'));
			await pending;
		}

		it('follows it once the create fails, and the retry spends the branch', async () => {
			await createWhileReplaced('fail');
			expect(activeSession.current).toMatchObject({ sessionId: null, project: PROJECT, projectV2Id: shared.projectV2Id, worktreeBranch: BRANCH });
			expect(composerPreferences.worktreeForProject(shared).checkout?.branch).toBe(BRANCH);

			post.mockResolvedValueOnce(created('session-r'));
			await activeSession.createWorktree();
			expect(activeSession.current).toMatchObject({ sessionId: 'session-r', worktreeBranch: BRANCH });
			expect(composerPreferences.worktreeForProject(shared).checkout).toBeNull();
			expect(composerPreferences.worktreeForProject(local).checkout).toBeNull();
		});

		it('never rewrites the chat the create bound', async () => {
			await createWhileReplaced('succeed');
			expect(activeSession.current).toMatchObject({ sessionId: 'session-c', projectV2Id: local.projectV2Id });
			expect(composerPreferences.worktreeForProject(shared).checkout).toBeNull();
		});

		it('an older create settling first leaves the swap held for the newer one', async () => {
			listCatalog([LOCAL]);
			composeUnderLocal();
			const older = deferred();
			post.mockReturnValueOnce(older.promise);
			const olderDone = activeSession.createWorktree();
			activeSession.newChat();
			composeUnderLocal();
			const newer = deferred();
			post.mockReturnValueOnce(newer.promise);
			const newerDone = activeSession.createWorktree();
			replaceWith(SHARED);
			older.reject({ status: 500, detail: 'worktree add failed' });
			await olderDone;
			expect(activeSession.current).toMatchObject({ isCreatingWorktree: true, projectV2Id: local.projectV2Id });
			newer.reject({ status: 500, detail: 'worktree add failed' });
			await newerDone;
			expect(activeSession.current).toMatchObject({ sessionId: null, projectV2Id: shared.projectV2Id, worktreeBranch: BRANCH });
			expect(composerPreferences.worktreeForProject(shared).checkout?.branch).toBe(BRANCH);

			post.mockResolvedValueOnce(created('session-r'));
			await activeSession.createWorktree();
			expect(activeSession.current.sessionId).toBe('session-r');
			expect(composerPreferences.worktreeForProject(shared).checkout).toBeNull();
		});

		it('follows a second replacement to the newest identity, branch included', async () => {
			listCatalog([LOCAL]);
			composeUnderLocal();
			const first = deferred();
			post.mockReturnValueOnce(first.promise);
			const pending = activeSession.createWorktree();
			replaceWith(SHARED);
			replaceWith(MOVED);
			first.reject({ status: 500, detail: 'worktree add failed' });
			await pending;
			expect(activeSession.current).toMatchObject({ sessionId: null, projectV2Id: moved.projectV2Id, worktreeBranch: BRANCH });
			expect(composerPreferences.current.project).toEqual(moved);
			expect(composerPreferences.worktreeForProject(moved).checkout?.branch).toBe(BRANCH);
			expect(composerPreferences.worktreeForProject(shared).checkout).toBeNull();

			post.mockResolvedValueOnce(created('session-r'));
			await activeSession.createWorktree();
			expect(activeSession.current.sessionId).toBe('session-r');
			expect(composerPreferences.worktreeForProject(moved).checkout).toBeNull();
		});
	});

	it('does not remember an Auto base branch across New Chats (unchanged)', () => {
		activeSession.setWorktreeBaseBranch('develop');
		expect(composerPreferences.current.checkout).toBeNull();
		newProjectChat();
		expect(activeSession.current).toMatchObject({ worktreeBranch: '', worktreeBaseBranch: 'main' });
	});
});
