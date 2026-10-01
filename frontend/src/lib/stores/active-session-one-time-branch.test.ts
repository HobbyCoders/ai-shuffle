/**
 * #1528 live-test failure: a branch made with the composer's New Branch modal
 * stayed staged for the next New Chat, so the next send silently opened a
 * second session on the same branch and worktree. These tests drive the real
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
	composerPreferences.set('profile', 'claude-code');
	composerPreferences.set('project', { projectId: PROJECT, projectV2Id: null });
	composerPreferences.set('worktreeIntent', 'worktree');
	composerPreferences.set('checkout', null);
	activeSession.newChat();
	Object.assign(activeSession.current, { profile: 'claude-code', project: PROJECT, projectV2Id: null, worktreeIntent: 'worktree' });
});
afterEach(() => { vi.restoreAllMocks(); vi.unstubAllGlobals(); });

/** The New Branch modal's success path in ExecutionModeBar. */
function stageFromModal(name = BRANCH) {
	activeSession.stageCreatedBranch(name);
	expect(activeSession.current).toMatchObject({ worktreeBranch: name, worktreeCreateNewBranch: false, worktreeMode: 'new' });
}

describe('one-time New Branch choice (#1528)', () => {
	it('is consumed after the session is created; the next New Chat falls back to Auto', async () => {
		stageFromModal();
		// A later base-branch edit keeps the choice one-time.
		activeSession.setWorktreeBaseBranch('main');
		expect(composerPreferences.current.checkout).toMatchObject({ branch: BRANCH, createNew: false, oneTime: true });
		post.mockResolvedValueOnce(created('session-c'));
		await activeSession.createWorktree();
		expect(post).toHaveBeenCalledWith('/sessions/with-worktree', expect.objectContaining({
			project_id: PROJECT, branch_name: BRANCH, create_new_branch: false }));
		expect(activeSession.current.sessionId).toBe('session-c');
		expect(composerPreferences.current.checkout).toBeNull();
		expect(JSON.parse(localStorage.getItem('ai-shuffle-composer:checkout')!)).toBeNull();

		activeSession.newChat();
		expect(activeSession.current).toMatchObject({ sessionId: null, project: PROJECT,
			worktreeIntent: 'worktree', worktreeBranch: '', worktreeId: null, worktreeCreateNewBranch: false });
	});

	it('is not staged by a New Chat opened while the first create is still in flight', async () => {
		stageFromModal();
		const first = deferred();
		post.mockReturnValueOnce(first.promise);
		const pending = activeSession.createWorktree();
		expect(activeSession.current.isCreatingWorktree).toBe(true);

		activeSession.newChat();
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
		activeSession.newChat();
		expect(activeSession.current.worktreeBranch).toBe('');
	});

	it('keeps the choice when the create fails, in place and for the next New Chat', async () => {
		stageFromModal();
		post.mockRejectedValueOnce({ status: 500, detail: 'worktree add failed' });
		await activeSession.createWorktree();
		expect(activeSession.current).toMatchObject({ sessionId: null, worktreeBranch: BRANCH, isCreatingWorktree: false });
		expect(activeSession.current.worktreeError).toBeTruthy();
		expect(composerPreferences.current.checkout).toMatchObject({ branch: BRANCH, oneTime: true });

		Object.assign(activeSession.current, { worktreeBranch: '', draft: '' });
		activeSession.newChat();
		expect(activeSession.current.worktreeBranch).toBe(BRANCH);
	});

	it('keeps the choice when an in-flight create fails after the user started a New Chat', async () => {
		stageFromModal();
		const first = deferred();
		post.mockReturnValueOnce(first.promise);
		const pending = activeSession.createWorktree();
		activeSession.newChat();
		first.reject({ status: 500, detail: 'worktree add failed' });
		await pending;
		expect(composerPreferences.checkoutClaimed).toBe(false);
		expect(composerPreferences.current.checkout).toMatchObject({ branch: BRANCH, oneTime: true });
		activeSession.newChat();
		expect(activeSession.current.worktreeBranch).toBe(BRANCH);
	});

	it('keeps a newer pick made while the first create was in flight', async () => {
		stageFromModal();
		const first = deferred();
		post.mockReturnValueOnce(first.promise);
		const pending = activeSession.createWorktree();
		activeSession.newChat();
		stageFromModal('live/next-task');
		first.resolve(created('session-c'));
		await pending;
		expect(composerPreferences.current.checkout).toMatchObject({ branch: 'live/next-task', oneTime: true });
		expect(activeSession.current.worktreeBranch).toBe('live/next-task');
	});

	it('leaves an existing branch picked from the list as the default for the next New Chat', async () => {
		activeSession.setWorktreeBranch('feature/existing');
		activeSession.setWorktreeMode('new');
		activeSession.setWorktreeCreateNewBranch(false);
		expect(composerPreferences.current.checkout).not.toHaveProperty('oneTime');
		post.mockResolvedValueOnce(created('session-e', 'feature/existing'));
		await activeSession.createWorktree();
		expect(composerPreferences.current.checkout?.branch).toBe('feature/existing');
		activeSession.newChat();
		expect(activeSession.current.worktreeBranch).toBe('feature/existing');
	});

	it('drops the one-time flag when the user then picks a listed branch', () => {
		stageFromModal();
		activeSession.setWorktreeBranch('feature/existing');
		expect(composerPreferences.current.checkout).toMatchObject({ branch: 'feature/existing', createNew: false });
		expect(composerPreferences.current.checkout).not.toHaveProperty('oneTime');
	});
});
