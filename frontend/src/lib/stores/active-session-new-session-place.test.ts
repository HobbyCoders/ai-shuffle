/**
 * The new-session screen's "Runs on <worker> in <project> on <branch> in <a
 * worktree>" choices stay what the owner picked when they open another chat
 * and come back, typed message or not. Coming back is New session again: the
 * compose they left is kept under the shared '__new_chat__' slot. Drives the
 * real activeSession store with only Git and the catalog stubbed.
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

let active: (typeof import('./active-session'))['activeSession'];
let ephemeral: (typeof import('./session-cache'))['sessionEphemeralCache'];

function gitRepo(projectId: string) {
	mocks.git = { projectId, hydrated: true, stale: false, status: { is_git_repo: true } };
	for (const listener of mocks.gitListeners) listener(mocks.git);
}

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

it('keeps the worker, project, base branch and worktree after opening another chat', () => {
	active.setDeviceIntent(PEER);
	gitRepo('a'); active.setProject('a');
	active.setWorktreeIntent('worktree');
	active.setWorktreeBaseBranch('develop');

	awayAndBack();

	expect(active.current).toMatchObject({
		sessionId: null, mode: 'code', deviceIntent: PEER, project: 'a',
		worktreeIntent: 'worktree', worktreeBranch: '', worktreeBaseBranch: 'develop',
	});
});

it('keeps the worktree and base branch while Git status still describes the chat that was open', () => {
	gitRepo('a'); active.setProject('a');
	active.setWorktreeIntent('worktree');
	active.setWorktreeBaseBranch('develop');
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
	gitRepo('a'); active.setProject('a'); active.setWorktreeIntent('worktree');
	awayAndBack();
	mocks.git = { projectId: 'a', hydrated: true, stale: false, status: { is_git_repo: false } };
	for (const listener of mocks.gitListeners) listener(mocks.git);
	expect(active.current).toMatchObject({ project: 'a', worktreeIntent: 'local' });
});

it('keeps a project in the main checkout (worktree off)', () => {
	gitRepo('a'); active.setProject('a');
	active.setWorktreeIntent('local');

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

it('keeps the worker and "no project" when the owner cleared the project', () => {
	gitRepo('a'); active.setProject('a'); active.setWorktreeIntent('worktree');
	active.setDeviceIntent(PEER);
	active.clearProject();

	awayAndBack();

	expect(active.current).toMatchObject({ mode: 'chat', project: '', projectV2Id: null, worktreeIntent: 'local', deviceIntent: PEER });
});

it('still starts a fresh compose without a project once a session was created from it', () => {
	gitRepo('a'); active.setProject('a'); active.setWorktreeIntent('worktree');
	// A first send spends the compose (message-sender clears the slot).
	active.switchSession('created-chat', 'a', null, 'Created', null);
	ephemeral.remove('__new_chat__');

	active.newChat();

	expect(active.current).toMatchObject({ mode: 'chat', project: '' });
});

it('starts fresh when New session is pressed on the new-session screen itself', () => {
	gitRepo('a'); active.setProject('a'); active.setWorktreeIntent('worktree');

	active.newChat();

	expect(active.current).toMatchObject({ mode: 'chat', project: '' });
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

	it('is cleared once the session is created; project and worktree stay', async () => {
		const { claim, composerPreferences } = await sendAndLeave();
		claim.consume();
		active.newChat();
		expect(composerPreferences.current.checkout).toBeNull();
		expect(active.current).toMatchObject({
			mode: 'code', project: 'a', worktreeIntent: 'worktree',
			worktreeBranch: '', worktreeBaseBranch: 'main', worktreeCreateNewBranch: false,
		});
	});

	it('comes back when that create failed', async () => {
		const { claim } = await sendAndLeave();
		claim.release();
		active.newChat();
		expect(active.current).toMatchObject({
			mode: 'code', project: 'a', worktreeIntent: 'worktree',
			worktreeBranch: 'topic/new', worktreeBaseBranch: 'develop', worktreeCreateNewBranch: true,
		});
	});
});
