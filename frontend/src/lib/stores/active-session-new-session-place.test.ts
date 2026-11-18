/**
 * Every new session starts without a project (owner decision 2026-11-13). The
 * new-session screen's "Runs on <worker> in <project> on <branch> in <a
 * worktree>" choices come back only with a kept draft: typed text or a staged
 * checkout, the compose in progress. An untyped compose left for another chat
 * starts over without a project; its worker carries over. Coming back is New
 * session again: the compose left is kept under the shared '__new_chat__' slot.
 * Drives the real activeSession store with only Git and the catalog stubbed.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mocks = vi.hoisted(() => ({
	git: { projectId: null as string | null, hydrated: true, stale: false, status: null as { is_git_repo: boolean } | null },
	gitListeners: new Set<(state: unknown) => void>(),
	catalog: { projects: [{ id: 'a' }, { id: 'b' }], clusterProjects: [], profiles: [], sessions: [] },
}));
vi.mock('./git.svelte', () => ({ git: {
	get current() { return mocks.git; },
	subscribe(listener: (state: unknown) => void) { mocks.gitListeners.add(listener); listener(mocks.git); return () => mocks.gitListeners.delete(listener); },
} }));
vi.mock('./app-data.svelte', () => ({ appData: { get current() { return mocks.catalog; }, projectCatalogLoaded: true } }));
vi.mock('./session/ws-connection', () => ({ createWsConnection: () => ({
	connect: vi.fn(), disconnect: vi.fn(), send: vi.fn(), isConnected: () => false,
	resetLastMessageTime: vi.fn(), resetReconnectAttempts: vi.fn(), destroy: vi.fn(),
}) }));

import { workerGitTargetKey } from '$lib/components/layout/worker-git-target';

let active: (typeof import('./active-session'))['activeSession'];
let ephemeral: (typeof import('./session-cache'))['sessionEphemeralCache'];

function gitRepo(projectId: string) {
	mocks.git = { projectId, hydrated: true, stale: false, status: { is_git_repo: true } };
	for (const listener of mocks.gitListeners) listener(mocks.git);
}

const typed = JSON.stringify({ type: 'doc', content: [{ type: 'paragraph', content: [{ type: 'text', text: 'half-typed' }] }] });

/** Open another chat, then New session to come back. */
function awayAndBack() {
	active.switchSession('other-chat', null, null, 'Other chat', null);
	active.newChat();
}

const PEER = { id: 'peer-1', name: 'Workstation' };

beforeEach(async () => {
	vi.resetModules(); localStorage.clear(); sessionStorage.clear(); mocks.gitListeners.clear();
	mocks.git = { projectId: null, hydrated: true, stale: false, status: null };
	vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 200 })));
	active = (await import('./active-session')).activeSession;
	ephemeral = (await import('./session-cache')).sessionEphemeralCache;
	active.newChat();
});

it('keeps the worker, project, base branch and worktree of a typed draft after opening another chat', () => {
	active.setDeviceIntent(PEER);
	gitRepo('a'); active.setProject('a');
	active.setWorktreeIntent('worktree');
	active.setWorktreeBaseBranch('develop');
	active.setDraft(typed);

	awayAndBack();

	expect(active.current).toMatchObject({
		sessionId: null, mode: 'code', deviceIntent: PEER, project: 'a', draft: typed,
		worktreeIntent: 'worktree', worktreeBranch: '', worktreeBaseBranch: 'develop',
	});
});

it('starts an untyped compose left for another chat over without a project, keeping its worker', () => {
	active.setDeviceIntent(PEER);
	gitRepo('a'); active.setProject('a', null, { user: true });
	active.setWorktreeIntent('worktree');
	active.setWorktreeBaseBranch('develop');

	awayAndBack();

	expect(active.current).toMatchObject({
		sessionId: null, mode: 'chat', deviceIntent: PEER, project: '', projectV2Id: null,
		worktreeIntent: 'local', worktreeBranch: '', worktreeBaseBranch: 'main',
	});
});

it('keeps the worktree and base branch while Git status still describes the chat that was open', () => {
	gitRepo('a'); active.setProject('a');
	active.setWorktreeIntent('worktree');
	active.setWorktreeBaseBranch('develop');
	active.setDraft(typed);
	active.switchSession('other-chat', null, null, 'Other chat', null);
	// The other chat's status replaced the project's.
	mocks.git = { projectId: null, hydrated: false, stale: false, status: null };
	for (const listener of mocks.gitListeners) listener(mocks.git);

	active.newChat();
	expect(active.current).toMatchObject({ project: 'a', worktreeIntent: 'worktree', worktreeBaseBranch: 'develop' });
	// The project's own status arriving afterwards changes nothing.
	gitRepo('a');
	expect(active.current).toMatchObject({ project: 'a', worktreeIntent: 'worktree', worktreeBaseBranch: 'develop' });
});

it('drops the worktree when the kept project turns out not to be a Git repository', () => {
	gitRepo('a'); active.setProject('a'); active.setWorktreeIntent('worktree'); active.setDraft(typed);
	awayAndBack();
	mocks.git = { projectId: 'a', hydrated: true, stale: false, status: { is_git_repo: false } };
	for (const listener of mocks.gitListeners) listener(mocks.git);
	expect(active.current).toMatchObject({ project: 'a', worktreeIntent: 'local' });
});

it('keeps a typed draft\'s project in the main checkout (worktree off)', () => {
	gitRepo('a'); active.setProject('a');
	active.setWorktreeIntent('local');
	active.setDraft(typed);

	awayAndBack();

	expect(active.current).toMatchObject({ mode: 'code', project: 'a', worktreeIntent: 'local' });
});

it('keeps a picked branch and a one-time new branch until a session spends it', () => {
	active.setDeviceIntent(PEER);
	gitRepo('a'); active.setProject('a'); active.setWorktreeIntent('worktree');
	active.setWorktreeBranch('topic/a'); active.setWorktreeMode('new'); active.setWorktreeCreateNewBranch(true);

	awayAndBack();

	expect(active.current).toMatchObject({
		mode: 'code', deviceIntent: PEER, project: 'a', worktreeIntent: 'worktree',
		worktreeBranch: 'topic/a', worktreeCreateNewBranch: true,
	});
});

it('stages a local worktree moved to another worker as a checkout still to be made', () => {
	gitRepo('a'); active.setProject('a'); active.setWorktreeIntent('worktree');
	active.selectWorktreeBranch('feat/local', 'wt-1');

	active.setDeviceIntent(PEER);

	expect(active.current).toMatchObject({ worktreeId: null, worktreeBranch: 'feat/local', worktreeCheckoutPending: true, worktreeCreateNewBranch: false });
	expect(workerGitTargetKey(active.current)).toBe(JSON.stringify([PEER.id, null]));

	awayAndBack();

	expect(active.current).toMatchObject({ worktreeBranch: 'feat/local', worktreeCheckoutPending: true });
	expect(workerGitTargetKey(active.current)).toBe(JSON.stringify([PEER.id, null]));
});

it('stages this worker\'s checkout moved to another worker as one still to be made', () => {
	gitRepo('a'); active.setProject('a'); active.setWorktreeIntent('worktree');
	active.setWorktreeBranch('feat/here');
	expect(active.current.worktreeCheckoutPending).toBe(false);

	active.setDeviceIntent(PEER);

	expect(active.current).toMatchObject({ worktreeBranch: 'feat/here', worktreeCheckoutPending: true });
	expect(workerGitTargetKey(active.current)).toBe(JSON.stringify([PEER.id, null]));
});

it('leaves a branch the first send creates alone when the worker changes', () => {
	gitRepo('a'); active.setProject('a'); active.setWorktreeIntent('worktree');
	active.setWorktreeBranch('feat/new');
	active.setWorktreeCreateNewBranch(true);

	active.setDeviceIntent(PEER);

	expect(active.current).toMatchObject({ worktreeBranch: 'feat/new', worktreeCreateNewBranch: true, worktreeCheckoutPending: false });
});

it('does not let the pending mark outlive its branch', () => {
	gitRepo('a'); active.setProject('a'); active.setWorktreeIntent('worktree');
	active.setWorktreeBranch('feat/x', { checkoutPending: true });
	active.selectWorktreeBranch('feat/y', 'wt-2');
	expect(active.current.worktreeCheckoutPending).toBe(false);

	active.setWorktreeBranch('feat/x', { checkoutPending: true });
	active.clearWorktreeSelection();
	expect(active.current.worktreeCheckoutPending).toBe(false);
});

it('keeps the worker and "no project" when the owner cleared the project', () => {
	gitRepo('a'); active.setProject('a'); active.setWorktreeIntent('worktree');
	active.setDeviceIntent(PEER);
	active.clearProject({ user: true });

	awayAndBack();

	expect(active.current).toMatchObject({ mode: 'chat', project: '', projectV2Id: null, worktreeIntent: 'local', deviceIntent: PEER });
});

// The owner's last project pick is still recorded (per-project picks, other
// devices) but a new session never opens with it (owner decision 2026-11-13).
it('starts the next new session without a project after one was created in a project', async () => {
	gitRepo('a'); active.setProject('a', null, { user: true }); active.setWorktreeIntent('worktree');
	// A first send spends the compose (message-sender clears the slot).
	active.switchSession('created-chat', 'a', null, 'Created', null);
	ephemeral.remove('__new_chat__');

	active.newChat();

	expect(active.current).toMatchObject({ sessionId: null, mode: 'chat', project: '', projectV2Id: null, worktreeIntent: 'local' });
	const { composerPreferences } = await import('./composer-preferences');
	expect(composerPreferences.current.project).toEqual({ projectId: 'a', projectV2Id: null });
});

it('clears an untyped project when New session is pressed on the new-session screen itself', () => {
	gitRepo('a'); active.setProject('a', null, { user: true });

	active.newChat();

	expect(active.current).toMatchObject({ mode: 'chat', project: '' });
});

it('keeps a typed draft\'s project when New session is pressed on the new-session screen itself', () => {
	gitRepo('a'); active.setProject('a', null, { user: true });
	active.setDraft(typed);

	active.newChat();

	expect(active.current).toMatchObject({ mode: 'code', project: 'a', draft: typed });
});

it('never opens a new session in the project of the chat being left', () => {
	active.clearProject({ user: true });
	active.switchSession('project-chat', 'b', null, 'In b', null);

	active.newChat();

	expect(active.current).toMatchObject({ mode: 'chat', project: '' });
});

it('never adds the remembered project when defaults or the catalog are re-applied', async () => {
	const { composerPreferences } = await import('./composer-preferences');
	composerPreferences.set('project', { projectId: 'a', projectV2Id: null });

	active.applyComposerDefaults();
	expect(active.current).toMatchObject({ mode: 'chat', project: '' });
	active.resolveComposerDefaultsAfterCatalog();
	expect(active.current).toMatchObject({ mode: 'chat', project: '' });
	active.dropMissingProject();
	expect(active.current).toMatchObject({ mode: 'chat', project: '', projectV2Id: null });
});

it('keeps the project an idle compose picked when defaults are re-applied', async () => {
	active.setProject('a', null, { user: true });
	const { composerPreferences } = await import('./composer-preferences');
	// Another device picks a different project: this compose keeps its own.
	composerPreferences.set('project', { projectId: 'b', projectV2Id: null });

	active.applyComposerDefaults();

	expect(active.current).toMatchObject({ mode: 'code', project: 'a' });
});

it('drops a project this worker no longer lists from a compose, without adding another', () => {
	active.setProject('a', null, { user: true });
	mocks.catalog.projects = [{ id: 'b' }];
	try {
		active.applyComposerDefaults();
		expect(active.current).toMatchObject({ mode: 'chat', project: '', projectV2Id: null });
	} finally {
		mocks.catalog.projects = [{ id: 'a' }, { id: 'b' }];
	}
});

describe('one-time new branch picked, sent, and left while the session is still being created', () => {
	async function sendAndLeave() {
		const { composerPreferences } = await import('./composer-preferences');
		gitRepo('a'); active.setProject('a'); active.setWorktreeIntent('worktree');
		active.setWorktreeBranch('topic/new'); active.setWorktreeMode('new');
		active.setWorktreeCreateNewBranch(true); active.setWorktreeBaseBranch('develop');
		// The send claims the pick and spends the compose (message-sender).
		const claim = composerPreferences.claimBranchChoice({ projectId: 'a', projectV2Id: null }, 'topic/new', true);
		ephemeral.remove('__new_chat__');
		Object.assign(active.current, { isCreatingWorktree: true, draft: '' });
		active.switchSession('other-chat', null, null, 'Other chat', null);
		return { claim, composerPreferences };
	}

	it('is cleared once the session is created; the next new session starts without a project', async () => {
		const { claim, composerPreferences } = await sendAndLeave();
		claim.consume();
		active.newChat();
		expect(composerPreferences.current.checkout).toBeNull();
		expect(active.current).toMatchObject({ mode: 'chat', project: '', worktreeIntent: 'local', worktreeBranch: '' });
		active.setProject('a');
		expect(active.current).toMatchObject({
			mode: 'code', project: 'a', worktreeIntent: 'worktree',
			worktreeBranch: '', worktreeBaseBranch: 'main', worktreeCreateNewBranch: false,
		});
	});

	it('comes back with the project when that create failed', async () => {
		const { claim } = await sendAndLeave();
		claim.release();
		active.newChat();
		expect(active.current).toMatchObject({ mode: 'chat', project: '' });
		active.setProject('a');
		expect(active.current).toMatchObject({
			mode: 'code', project: 'a', worktreeIntent: 'worktree',
			worktreeBranch: 'topic/new', worktreeBaseBranch: 'develop', worktreeCreateNewBranch: true,
		});
	});
});

describe('a draft restored by a reload keeps the place it was typed in', () => {
	// After a reload the text is only in this tab's draft slot until the
	// composer reads it back; another tab or device may have saved a different
	// project choice meanwhile.

	it('keeps no project', async () => {
		active.clearProject({ user: true });
		const { composerPreferences } = await import('./composer-preferences');
		composerPreferences.set('project', { projectId: 'a', projectV2Id: null });
		sessionStorage.setItem('__aihub_draft_new', typed);

		active.applyComposerDefaults();

		expect(active.current).toMatchObject({ sessionId: null, mode: 'chat', project: '' });
	});

	it('keeps its own project', async () => {
		active.setProject('a', null, { user: true });
		const { composerPreferences } = await import('./composer-preferences');
		composerPreferences.set('project', { projectId: 'b', projectV2Id: null });
		sessionStorage.setItem('__aihub_draft_new', typed);

		active.applyComposerDefaults();

		expect(active.current).toMatchObject({ mode: 'code', project: 'a' });
	});

	it('a blank editor slot is not a draft', async () => {
		active.clearProject({ user: true });
		const { composerPreferences } = await import('./composer-preferences');
		composerPreferences.set('project', { projectId: 'a', projectV2Id: null });
		sessionStorage.setItem('__aihub_draft_new', JSON.stringify({ type: 'doc', content: [{ type: 'paragraph' }] }));

		active.applyComposerDefaults();

		// Not a draft, so the defaults apply: and they carry no project.
		expect(active.current).toMatchObject({ mode: 'chat', project: '' });
	});
});

it('keeps the shared id of a typed compose\'s project this worker has no local copy of', () => {
	const remote = { project_v2_id: 'repo-x', local_project_id: null, remote_only: true, origin: 'git@example:x', name: 'X' };
	(mocks.catalog.clusterProjects as unknown[]).push(remote);
	try {
		active.setProject('', 'repo-x', { user: true });
		active.setDraft(typed);

		awayAndBack();

		expect(active.current).toMatchObject({ mode: 'code', project: '', projectV2Id: 'repo-x' });
	} finally {
		mocks.catalog.clusterProjects.length = 0;
	}
});
