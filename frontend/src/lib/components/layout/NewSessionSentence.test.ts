/**
 * The new-session sentence: "Runs on <worker> in <project> on <branch> in
 * <a worktree>." / "Runs on <worker> with <no project>." Each part is a real
 * button that opens its menu; branch and worktree only for a git project; the
 * worker's online dot never shows in the sentence.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';

const m = vi.hoisted(() => ({
	setWorktreeIntent: vi.fn(),
	clearProject: vi.fn(),
	setProject: vi.fn(),
	listInstalls: vi.fn()
}));

const stores = vi.hoisted(() => ({}) as Record<string, import('svelte/store').Writable<unknown>>);

vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	stores.session = writable({});
	return {
		activeSession: {
			subscribe: stores.session.subscribe,
			setWorktreeIntent: (intent: string) => {
				m.setWorktreeIntent(intent);
				stores.session.update((s) => ({ ...(s as object), worktreeIntent: intent }));
			},
			clearProject: m.clearProject,
			setProject: m.setProject,
			setDeviceIntent: vi.fn(),
			setWorktreeBranch: vi.fn(),
			setWorktreeMode: vi.fn(),
			clearWorktreeSelection: vi.fn(),
			setWorktreeBaseBranch: vi.fn(),
			setWorktreeCreateNewBranch: vi.fn(),
			selectWorktreeBranch: vi.fn(),
			clearWorktreeError: vi.fn(),
			setWorkspaceProjectIds: vi.fn()
		}
	};
});
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	stores.projects = writable([]);
	return {
		projects: stores.projects,
		clusterProjects: writable([]),
		profiles: writable([]),
		profilesLoaded: writable(true),
		appData: { loadClusterProjects: vi.fn(), current: { projects: [], clusterProjects: [] } }
	};
});
vi.mock('$lib/stores/auth.svelte', async () => {
	const { writable } = await import('svelte/store');
	stores.github = writable(true);
	return { githubAuthenticated: stores.github };
});
vi.mock('$lib/stores/git.svelte', async () => {
	const { writable } = await import('svelte/store');
	stores.git = writable({});
	const git = {
		subscribe: stores.git.subscribe,
		loadWorktrees: vi.fn(async () => {}),
		loadBranches: vi.fn(async () => {}),
		refreshStatus: vi.fn(),
		refreshDiffStats: vi.fn(),
		forgetWorktree: vi.fn()
	};
	return {
		git,
		worktrees: writable([]),
		branches: writable([]),
		currentBranch: writable(null),
		gitStatus: writable({ current_branch: 'main' }),
		gitMode: writable('local')
	};
});
vi.mock('$lib/api/installs', () => ({
	listInstalls: m.listInstalls,
	getClusterGitState: vi.fn().mockResolvedValue(null)
}));

vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });

import NewSessionSentence from './NewSessionSentence.svelte';
import { resetWorkerPresence } from '$lib/stores/worker-presence';

const install = (id: string, name: string, extra: Record<string, unknown> = {}) => ({
	id, device_name: name, base_url: `http://${id}`, platform: 'linux', version: '1', capabilities: {},
	last_seen: null, online: true, approved: true, ...extra
});

function session(over: Record<string, unknown> = {}) {
	stores.session.set({
		sessionId: null, project: 'p1', projectV2Id: null, worktreeIntent: 'worktree', worktreeId: null,
		worktreeBranch: null, selectedWorktreeBranch: null, worktreeBaseBranch: null, deviceIntent: null,
		workspaceProjectIds: [], worktreeError: null, isStreaming: false, ...over
	});
}

/**
 * The sentence's words. Whitespace inside each word's wrapper is markup only
 * (the wrappers are inline-flex, so it never renders), hence the " ." fold.
 */
const text = () => (screen.getByRole('group', { name: 'Where this session runs' }).textContent ?? '')
	.replace(/\s+/g, ' ').replace(/ \./g, '.').trim();

beforeEach(() => {
	resetWorkerPresence();
	m.listInstalls.mockResolvedValue({ installs: [install('self', 'Unraid', { is_self: true }), install('pc', 'GamingPC', { online: false })], self_instance_id: 'self' });
	stores.projects.set([{ id: 'p1', name: 'ai-shuffle', path: '/workspace/ai-shuffle' }]);
	stores.github.set(true);
	stores.git.set({ hydrated: false });
	session();
});
afterEach(() => {
	cleanup();
	vi.clearAllMocks();
	resetWorkerPresence();
});

describe('NewSessionSentence', () => {
	it('reads the whole sentence for a git project in a worktree, each part a button', async () => {
		render(NewSessionSentence);
		await waitFor(() => expect(text()).toBe('Runs on Unraid in ai-shuffle on a new branch from main in a worktree.'));
		for (const name of [/^Runs on Unraid$/, /^Workspace folders: ai-shuffle$/, /^Worktree branch: a new branch from main$/, /Runs in a worktree/]) {
			expect(screen.getByRole('button', { name })).toBeTruthy();
		}
		// The online dot is the worker menu's, never the sentence's.
		expect(document.querySelector('.ns-sentence .worker-status-dot, .ns-sentence [class*="dot"]')).toBeNull();
	});

	it('names a picked branch instead of "a new branch"', async () => {
		session({ selectedWorktreeBranch: 'feat/redesign' });
		render(NewSessionSentence);
		await waitFor(() => expect(text()).toContain('on feat/redesign in a worktree.'));
	});

	it('toggles the worktree word to the main checkout and shows the checkout branch', async () => {
		render(NewSessionSentence);
		const wt = await screen.findByRole('button', { name: /Runs in a worktree/ });
		await fireEvent.click(wt);
		expect(m.setWorktreeIntent).toHaveBeenCalledWith('local');
		await waitFor(() => expect(text()).toBe('Runs on Unraid in ai-shuffle on main in the main checkout.'));
	});

	it('shows extra workspace folders as +N on the project word', async () => {
		session({ workspaceProjectIds: ['p2'] });
		render(NewSessionSentence);
		const project = await screen.findByRole('button', { name: /Workspace folders: ai-shuffle and 1 more/ });
		expect(project.textContent).toBe('ai-shuffle+1');
	});

	it('drops branch and worktree for a folder that is not a git repository', async () => {
		session({ worktreeIntent: 'local' });
		stores.git.set({ hydrated: true, projectId: 'p1', sessionId: null, worktreeId: null, status: { is_git_repo: false } });
		render(NewSessionSentence);
		await waitFor(() => expect(text()).toBe('Runs on Unraid in ai-shuffle.'));
	});

	it('reads "with no project" without one, and that word opens the project picker', async () => {
		session({ project: '', projectV2Id: null });
		render(NewSessionSentence);
		await waitFor(() => expect(text()).toBe('Runs on Unraid with no project.'));
		const none = screen.getByRole('button', { name: /^No project\. Add a project/ });
		await fireEvent.click(none);
		await tick();
		expect(none.getAttribute('aria-expanded')).toBe('true');
	});

	it('opens the worker list from the worker word, offline workers disabled', async () => {
		render(NewSessionSentence);
		await fireEvent.click(await screen.findByRole('button', { name: /^Runs on Unraid$/ }));
		await waitFor(() => expect(screen.getAllByRole('menuitemradio')).toHaveLength(2));
		const rows = screen.getAllByRole('menuitemradio') as HTMLButtonElement[];
		expect(rows[0].textContent).toContain('Unraid');
		expect(rows[0].getAttribute('aria-checked')).toBe('true');
		expect(rows[1].textContent).toContain('GamingPC');
		expect(rows[1].disabled || rows[1].getAttribute('aria-disabled') === 'true').toBe(true);
		// The menu hangs from <body>, centred under the word.
		expect(screen.getByRole('menu').parentElement).toBe(document.body);
	});
});
