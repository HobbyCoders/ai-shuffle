import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';

const mocks = vi.hoisted(() => {
	function store<T>(initial: T) {
		let value = initial;
		const subscribers = new Set<(next: T) => void>();
		return {
			subscribe(run: (next: T) => void) {
				run(value);
				subscribers.add(run);
				return () => subscribers.delete(run);
			},
			set(next: T) {
				value = next;
				for (const run of subscribers) run(value);
			}
		};
	}

	return {
		worktrees: store([] as Array<Record<string, unknown>>),
		branches: store([
			{ name: 'main', current: true, remote: false },
			{ name: 'develop', current: false, remote: false }
		]),
		git: store({ projectId: 'repo', worktreeId: null as string | null, sessionId: null as string | null,
			hydrated: true, status: { is_git_repo: true } }),
		currentBranch: store({ name: 'main' }),
		loadWorktrees: vi.fn().mockResolvedValue(undefined),
		loadBranches: vi.fn().mockResolvedValue(undefined),
		createBranch: vi.fn().mockResolvedValue(undefined),
		syncWorktrees: vi.fn().mockResolvedValue({}),
		forgetWorktree: vi.fn().mockResolvedValue(undefined)
	};
});

vi.hoisted(() => {
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
		ok: false,
		status: 404,
		json: vi.fn().mockResolvedValue({})
	}));
});

vi.mock('$lib/api/git', async (importOriginal) => {
	const actual = await importOriginal<Record<string, unknown>>();
	return { ...actual, syncWorktrees: mocks.syncWorktrees };
});

vi.mock('$lib/api/installs', () => ({
	listInstalls: vi.fn().mockResolvedValue({ installs: [] }),
	getClusterGitState: vi.fn().mockResolvedValue(null)
}));

vi.mock('$lib/stores/git.svelte', () => ({
	worktrees: mocks.worktrees,
	branches: mocks.branches,
	currentBranch: mocks.currentBranch,
	git: {
		...mocks.git,
		loadWorktrees: mocks.loadWorktrees,
		loadBranches: mocks.loadBranches,
		createBranch: mocks.createBranch,
		forgetWorktree: mocks.forgetWorktree
	}
}));

import ExecutionModeBar from './ExecutionModeBar.svelte';
import { activeSession } from '$lib/stores/active-session';
import { getClusterGitState, listInstalls } from '$lib/api/installs';
import { getPersistedDeviceIntent } from '$lib/stores/worker-selection';
import { composerPreferences } from '$lib/stores/composer-preferences';
import { getPersistedWorktreeIntent } from '$lib/stores/session/session-switcher';
import { tick } from 'svelte';
import { refreshWorkerPresence, resetWorkerPresence } from '$lib/stores/worker-presence';

afterEach(() => {
	cleanup();
	resetWorkerPresence();
});
afterAll(() => vi.unstubAllGlobals());

beforeEach(() => {
	Object.assign(activeSession.current, { sessionId: null, project: 'repo', projectV2Id: null, messages: [] });
	mocks.git.set({ projectId: 'repo', worktreeId: null, sessionId: null, hydrated: true, status: { is_git_repo: true } });
	activeSession.setDeviceIntent(null);
	activeSession.setWorktreeIntent('worktree');
	activeSession.clearWorktreeSelection();
	activeSession.setWorktreeBaseBranch('main');
	mocks.worktrees.set([]);
	mocks.branches.set([
		{ name: 'main', current: true, remote: false },
		{ name: 'develop', current: false, remote: false }
	]);
});


describe('stale worktree visibility', () => {
	it('requires confirmation to forget stale rows and keeps refusal visible', async () => {
		mocks.forgetWorktree.mockReset().mockRejectedValueOnce({ detail: 'Forget refused: worktree is locked' });
		mocks.worktrees.set([{ id: 'wt-stale', branch_name: 'fix/stale', exists: false }]);
		render(ExecutionModeBar);
		await fireEvent.click(screen.getByRole('button', { name: /worktree branch:/i }));
		expect(mocks.forgetWorktree).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Forget fix/stale' }));
		expect(screen.getByText(/removes Git's record of this worktree and its entry in AI Shuffle/)).toBeTruthy();
		expect(screen.getByText(/Checkout files, branches and chat history are kept/)).toBeTruthy();
		// Cancel is the default action: a stray Enter backs out (#2072).
		expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Cancel' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		expect(mocks.forgetWorktree).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Forget fix/stale' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Confirm forget' }));
		expect(mocks.forgetWorktree).toHaveBeenCalledWith('wt-stale');
		expect(screen.getByRole('alert').textContent).toContain('worktree is locked');
		mocks.forgetWorktree.mockImplementationOnce(async () => { mocks.worktrees.set([]); });
		await fireEvent.click(screen.getByRole('button', { name: 'Confirm forget' }));
		expect(screen.queryByRole('button', { name: 'Forget fix/stale' })).toBeNull();
	});

	it('keeps missing and switched bindings visible but unavailable for selection', async () => {
		mocks.worktrees.set([
			{ id: 'wt-missing', branch_name: 'fix/missing', exists: false, status_error: 'directory missing; needs cleanup' },
			{ id: 'wt-switched', branch_name: 'fix/bound', exists: true,
				binding_warning: 'Checkout is on fix/actual, not the bound branch fix/bound',
				git_status: { current_branch: 'fix/actual', is_clean: false, modified_count: 1, staged_count: 0, untracked_count: 2 } }
		]);
		render(ExecutionModeBar);
		await fireEvent.click(screen.getByRole('button', { name: /worktree branch:/i }));
		expect((screen.getByRole('button', { name: /fix\/missing.*missing/i }) as HTMLButtonElement).disabled).toBe(true);
		const switched = screen.getByRole('button', { name: /fix\/bound.*branch changed/i });
		expect((switched as HTMLButtonElement).disabled).toBe(true);
		expect(screen.getByText('On fix/actual · 3 changed')).toBeTruthy();
		expect(screen.getByText('directory missing; needs cleanup')).toBeTruthy();
	});

	it('does not reclaim registry rows or branches just by rendering the picker', async () => {
		mocks.syncWorktrees.mockClear();
		render(ExecutionModeBar);
		await tick();
		expect(mocks.syncWorktrees).not.toHaveBeenCalled();
	});
});

describe('automatic non-git fallback', () => {
	it('refreshes a canonical-only branch picker without GitHub credentials', async () => {
		Object.assign(activeSession.current, { project: null, projectV2Id: 'pv2-shared' });
		render(ExecutionModeBar);
		mocks.loadWorktrees.mockClear();
		mocks.loadBranches.mockClear();
		await fireEvent.click(screen.getByRole('button', { name: /worktree branch:/i }));
		expect(mocks.loadWorktrees).toHaveBeenCalledOnce();
		expect(mocks.loadBranches).toHaveBeenCalledOnce();
	});

	it('uses canonical eligibility for an adopted session and never syncs its foreign legacy project', async () => {
		Object.assign(activeSession.current, { sessionId: 'adopted', project: 'foreign-legacy', projectV2Id: 'pv2-shared' });
		mocks.syncWorktrees.mockClear();
		vi.mocked(getClusterGitState).mockClear();
		render(ExecutionModeBar);
		await fireEvent.click(screen.getByRole('button', { name: /runner: local/i }));
		expect(getClusterGitState).toHaveBeenCalledWith(null, 'pv2-shared');
		expect(mocks.syncWorktrees).not.toHaveBeenCalled();
	});

	const nonGit = { projectId: 'repo', worktreeId: null, sessionId: null,
		hydrated: true, status: { is_git_repo: false } };

	it('runs a non-git draft locally without changing the remembered toggle', async () => {
		render(ExecutionModeBar);
		mocks.git.set(nonGit);
		await tick();
		expect(activeSession.current.worktreeIntent).toBe('local');
		expect(composerPreferences.current.worktreeIntent).toBe('worktree');
		expect(getPersistedWorktreeIntent()).toBe('worktree');
		expect(JSON.parse(localStorage.getItem('ai-shuffle-composer:worktreeIntent')!)).toBe('worktree');
		expect(screen.queryByRole('checkbox')).toBeNull();

		activeSession.setProject('another-repo', null);
		await tick();
		expect(activeSession.current.worktreeIntent).toBe('worktree');
		expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(true);
	});

	it.each([
		{ projectId: 'previous-repo' },
		{ hydrated: false },
		{ worktreeId: 'previous-checkout' },
		{ sessionId: 'previous-session' },
	])('ignores non-git status from a different or unhydrated checkout: %o', async stale => {
		render(ExecutionModeBar);
		mocks.git.set({ ...nonGit, ...stale });
		await tick();
		expect(activeSession.current.worktreeIntent).toBe('worktree');
		expect(getPersistedWorktreeIntent()).toBe('worktree');
		expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(true);
	});

	it('does not change an existing session binding', async () => {
		Object.assign(activeSession.current, { sessionId: 'existing' });
		mocks.git.set({ ...nonGit, sessionId: 'existing' });
		render(ExecutionModeBar);
		await tick();
		expect(activeSession.current.worktreeIntent).toBe('worktree');
		expect(getPersistedWorktreeIntent()).toBe('worktree');
	});

	it('still persists explicit checkbox changes in both directions', async () => {
		render(ExecutionModeBar);
		await fireEvent.click(screen.getByRole('checkbox'));
		expect(activeSession.current.worktreeIntent).toBe('local');
		expect(composerPreferences.current.worktreeIntent).toBe('local');
		expect(getPersistedWorktreeIntent()).toBe('local');
		await fireEvent.click(screen.getByRole('checkbox'));
		expect(activeSession.current.worktreeIntent).toBe('worktree');
		expect(composerPreferences.current.worktreeIntent).toBe('worktree');
		expect(getPersistedWorktreeIntent()).toBe('worktree');
	});
});

describe('worker picker persistence', () => {
	it('selects the local owner when picking a legacy checkout after a remote worker', async () => {
		activeSession.setDeviceIntent({ id: 'runner-1', name: 'Unraid' });
		mocks.worktrees.set([{ id: 'legacy-local', branch_name: 'feature/local', exists: true }]);
		render(ExecutionModeBar);
		await fireEvent.click(screen.getByRole('button', { name: /worktree branch:/i }));
		await fireEvent.click(screen.getByText('feature/local'));
		expect(activeSession.current.deviceIntent).toBeNull();
		expect(activeSession.current.worktreeId).toBe('legacy-local');
		expect(activeSession.current.selectedWorktreeBranch).toBe('feature/local');
	});

	it('converts a legacy checkout into a logical branch when changing workers', async () => {
		vi.mocked(listInstalls).mockResolvedValue({ self_instance_id: 'local', installs: [{
			id: 'runner-1', device_name: 'Unraid', online: true, base_url: null,
			platform: 'linux', version: null, capabilities: {}, last_seen: null,
		}] });
		mocks.worktrees.set([{ id: 'legacy-worktree', branch_name: 'feature/shared', exists: true }]);
		render(ExecutionModeBar);
		await fireEvent.click(screen.getByRole('button', { name: /worktree branch:/i }));
		await fireEvent.click(screen.getByText('feature/shared'));
		expect(activeSession.current.worktreeId).toBe('legacy-worktree');
		await fireEvent.click(screen.getByRole('button', { name: 'Runner: Local' }));
		await fireEvent.click(screen.getByRole('menuitemradio', { name: /Unraid/ }));
		expect(activeSession.current.deviceIntent?.id).toBe('runner-1');
		expect(activeSession.current.worktreeId).toBeNull();
		expect(activeSession.current.selectedWorktreeBranch).toBeNull();
		expect(activeSession.current.worktreeBranch).toBe('feature/shared');
		expect(activeSession.current.worktreeMode).toBe('new');
		expect(activeSession.current.worktreeCreateNewBranch).toBe(false);
		await fireEvent.click(screen.getByRole('button', { name: 'Runner: Unraid' }));
		await fireEvent.click(screen.getByRole('menuitemradio', { name: 'Local' }));
		expect(activeSession.current.worktreeId).toBeNull();
		expect(activeSession.current.worktreeBranch).toBe('feature/shared');
		expect(activeSession.current.deviceIntent).toBeNull();
	});

	it('persists actual picker clicks and retains the worker on project changes', async () => {
		vi.mocked(listInstalls).mockResolvedValueOnce({ self_instance_id: 'local', installs: [{
			id: 'runner-1', device_name: 'Unraid', online: true, base_url: null,
			platform: 'linux', version: null, capabilities: {}, last_seen: null,
		}] });
		render(ExecutionModeBar);
		await fireEvent.click(screen.getByRole('button', { name: 'Runner: Local' }));
		await fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Unraid' }));
		expect(getPersistedDeviceIntent()).toEqual({ id: 'runner-1', name: 'Unraid' });
		activeSession.setProject('another-project', null);
		expect(activeSession.current.deviceIntent).toEqual(getPersistedDeviceIntent());
		await fireEvent.click(screen.getByRole('button', { name: 'Runner: Unraid' }));
		await fireEvent.click(screen.getByRole('menuitemradio', { name: 'Local' }));
		expect(getPersistedDeviceIntent()).toBeNull();
	});
});

describe('worker presence dots', () => {
	const dotOf = (el: Element | null) => el?.querySelector('.worker-status-dot')?.getAttribute('data-status') ?? null;

	it('marks Local and online workers green, offline workers red, and the picked worker on the chip', async () => {
		vi.mocked(listInstalls).mockResolvedValue({ self_instance_id: 'local', installs: [
			{ id: 'runner-1', device_name: 'Unraid', online: true, base_url: null,
				platform: 'linux', version: null, capabilities: {}, last_seen: null },
			{ id: 'runner-2', device_name: 'linux-worker', online: false, base_url: null,
				platform: 'linux', version: null, capabilities: {}, last_seen: null }
		] });
		try {
			render(ExecutionModeBar);
			expect(dotOf(screen.getByRole('button', { name: 'Runner: Local' }))).toBe('online');
			await fireEvent.click(screen.getByRole('button', { name: 'Runner: Local' }));
			expect(dotOf(screen.getByRole('menuitemradio', { name: 'Local' }))).toBe('online');
			const unraid = await screen.findByRole('menuitemradio', { name: 'Unraid' });
			expect(dotOf(unraid)).toBe('online');
			// The offline row keeps its existing greyed, blocked state.
			const popOs = screen.getByRole('menuitemradio', { name: /linux-worker/ });
			expect(dotOf(popOs)).toBe('offline');
			expect(popOs.getAttribute('aria-disabled')).toBe('true');
			await fireEvent.click(unraid);
			expect(dotOf(screen.getByRole('button', { name: 'Runner: Unraid' }))).toBe('online');
		} finally {
			vi.mocked(listInstalls).mockResolvedValue({ self_instance_id: 'local', installs: [] });
		}
	});

	it('shows no dot on the chip while a picked worker\'s presence is unknown', () => {
		activeSession.setDeviceIntent({ id: 'runner-9', name: 'Elsewhere' });
		render(ExecutionModeBar);
		expect(dotOf(screen.getByRole('button', { name: 'Runner: Elsewhere' }))).toBeNull();
	});

	const registry = (online: boolean) => ({ self_instance_id: 'local', installs: [
		{ id: 'runner-1', device_name: 'Unraid', online, base_url: null,
			platform: 'linux', version: null, capabilities: {}, last_seen: null }
	] });

	it('greys out and blocks a worker that goes offline while the menu is open', async () => {
		vi.mocked(listInstalls).mockResolvedValue(registry(true));
		try {
			render(ExecutionModeBar);
			await fireEvent.click(screen.getByRole('button', { name: 'Runner: Local' }));
			const before = await screen.findByRole('menuitemradio', { name: 'Unraid' });
			expect(dotOf(before)).toBe('online');
			expect(before.getAttribute('aria-disabled')).toBe('false');

			// The next presence refresh (the open menu's poll) reports it offline.
			vi.mocked(listInstalls).mockResolvedValue(registry(false));
			await refreshWorkerPresence();
			await tick();
			const after = screen.getByRole('menuitemradio', { name: /Unraid/ });
			expect(dotOf(after)).toBe('offline');
			expect(after.getAttribute('aria-disabled')).toBe('true');
			await fireEvent.click(after);
			expect(activeSession.current.deviceIntent).toBeNull();
		} finally {
			vi.mocked(listInstalls).mockResolvedValue({ self_instance_id: 'local', installs: [] });
		}
	});

	it('turns the picked worker\'s chip red when it goes offline with the menu closed', async () => {
		vi.mocked(listInstalls).mockResolvedValue(registry(true));
		try {
			activeSession.setDeviceIntent({ id: 'runner-1', name: 'Unraid' });
			render(ExecutionModeBar);
			// A picked worker is watched with the menu closed, so its presence loads.
			await vi.waitFor(() => expect(dotOf(screen.getByRole('button', { name: 'Runner: Unraid' }))).toBe('online'));

			vi.mocked(listInstalls).mockResolvedValue(registry(false));
			await refreshWorkerPresence();
			await tick();
			expect(dotOf(screen.getByRole('button', { name: 'Runner: Unraid (offline)' }))).toBe('offline');
		} finally {
			vi.mocked(listInstalls).mockResolvedValue({ self_instance_id: 'local', installs: [] });
		}
	});
});

describe('ExecutionModeBar merged worktree branch chip', () => {
	it('selects canonical checkout branches without storing a legacy worktree ID', async () => {
		mocks.worktrees.set([{ id: 'ck-local', resource_kind: 'checkout', runner_id: 'local-runner', branch_name: 'feature/native', exists: true }]);
		activeSession.setDeviceIntent({ id: 'other-worker', name: 'Other worker' });
		activeSession.selectWorktreeBranch('old-branch', 'legacy-worktree');
		render(ExecutionModeBar);
		await fireEvent.click(screen.getByRole('button', { name: /worktree branch:/i }));
		await fireEvent.click(screen.getByText('feature/native'));
		expect(activeSession.current.worktreeId).toBeNull();
		expect(activeSession.current.deviceIntent).toBeNull();
		expect(activeSession.current.selectedWorktreeBranch).toBeNull();
		expect(activeSession.current.worktreeBranch).toBe('feature/native');
		expect(activeSession.current.worktreeMode).toBe('new');
		expect(activeSession.current.worktreeCreateNewBranch).toBe(false);
	});

	it('stages a branch made with New branch as an existing branch, like a listed pick (#1528)', async () => {
		render(ExecutionModeBar);
		await fireEvent.click(screen.getByRole('button', { name: /worktree branch:/i }));
		await fireEvent.click(screen.getByText('New branch...'));
		await fireEvent.input(screen.getByLabelText('Branch name'), { target: { value: 'live/1528-explicit' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Continue' }));
		await vi.waitFor(() => expect(mocks.createBranch).toHaveBeenCalledWith('live/1528-explicit'));
		await vi.waitFor(() => expect(activeSession.current.worktreeBranch).toBe('live/1528-explicit'));
		expect(activeSession.current.worktreeCreateNewBranch).toBe(false);
		expect(composerPreferences.current.checkout).toEqual({ kind: 'branch', projectId: 'repo', projectV2Id: null,
			branch: 'live/1528-explicit', baseBranch: 'main', createNew: false });

		await fireEvent.click(screen.getByRole('button', { name: /worktree branch:/i }));
		await fireEvent.click(screen.getByText('develop'));
		expect(activeSession.current.worktreeBranch).toBe('develop');
		expect(composerPreferences.current.checkout).toEqual({ kind: 'branch', projectId: 'repo', projectV2Id: null,
			branch: 'develop', baseBranch: 'main', createNew: false });
	});

	it('does not render the new-worktree folder chip', () => {
		const { container } = render(ExecutionModeBar);

		expect(container.querySelector('.new-wt-chip')).toBeNull();
		expect(screen.queryByRole('button', { name: /new worktree from/i })).toBeNull();
		expect(screen.getByRole('checkbox')).toBeTruthy();
	});

	it('merges Auto and the base branch into one chip', () => {
		const { container } = render(ExecutionModeBar);

		expect(container.querySelectorAll('.branch-selector')).toHaveLength(1);
		expect(screen.getByRole('button', { name: /worktree branch: auto from main/i })).toBeTruthy();
		expect(screen.queryByRole('button', { name: /^base branch:/i })).toBeNull();
		expect(screen.queryByRole('button', { name: /^from main$/i })).toBeNull();
	});

	it('opens one dropdown with Auto, New branch, and the base-branch picker without destructive sync', async () => {
		mocks.syncWorktrees.mockClear();
		render(ExecutionModeBar);
		await tick();
		expect(mocks.syncWorktrees).not.toHaveBeenCalled();

		await fireEvent.click(screen.getByRole('button', { name: /worktree branch:/i }));

		expect(screen.getByText('Auto')).toBeTruthy();
		expect(screen.getByText('New branch...')).toBeTruthy();
		expect(screen.getByText('Base branch')).toBeTruthy();
		expect(screen.getAllByText('develop').length).toBeGreaterThan(0);
		activeSession.setProject('another-project', null);
		await tick();
		expect(mocks.syncWorktrees).not.toHaveBeenCalled();
	});

	it('shows only the selected branch when Auto is not the current choice', async () => {
		activeSession.setWorktreeBranch('feat-x');
		const { container } = render(ExecutionModeBar);

		expect(container.querySelectorAll('.branch-selector')).toHaveLength(1);
		expect(screen.getByRole('button', { name: /worktree branch: feat-x/i })).toBeTruthy();
		expect(screen.queryByRole('button', { name: /auto from/i })).toBeNull();

		await fireEvent.click(screen.getByRole('button', { name: /worktree branch: feat-x/i }));

		expect(screen.getByText('Auto')).toBeTruthy();
		expect(screen.getByText('New branch...')).toBeTruthy();
		expect(screen.queryByText('Base branch')).toBeNull();
	});

	it('keeps the merge in sheetMode instead of stacking Auto and from-main rows', () => {
		const { container } = render(ExecutionModeBar, { props: { sheetMode: true } });

		expect(container.querySelectorAll('.branch-selector')).toHaveLength(1);
		expect(screen.getByRole('button', { name: /worktree branch: auto from main/i })).toBeTruthy();
		expect(screen.queryByRole('button', { name: /^base branch:/i })).toBeNull();
		expect(container.querySelector('.new-wt-chip')).toBeNull();
	});
});

describe('flat worker list (matches the active session runner menu)', () => {
	const peer = (id: string, device_name: string, online = true) => ({
		id, device_name, online, base_url: null, platform: 'linux', version: null, capabilities: {}, last_seen: null
	});
	const fixture = { self_instance_id: 'local', installs: [
		{ ...peer('local', 'This worker'), is_self: true },
		peer('runner-1', 'Unraid'),
		peer('runner-2', 'linux-worker', false),
		peer('runner-3', 'Laptop')
	] };

	afterEach(() => {
		vi.mocked(listInstalls).mockResolvedValue({ self_instance_id: 'local', installs: [] });
		vi.mocked(getClusterGitState).mockResolvedValue(null as never);
	});

	it('lists this worker as Local, then every peer, with no Other workers section', async () => {
		vi.mocked(listInstalls).mockResolvedValue(fixture);
		render(ExecutionModeBar);
		await fireEvent.click(screen.getByRole('button', { name: 'Runner: Local' }));
		await screen.findByRole('menuitemradio', { name: 'Unraid' });
		const menu = screen.getByRole('menu', { name: 'Runner selection' });
		const rows = [...menu.querySelectorAll('[role="menuitemradio"]')];
		expect(rows.map(r => r.querySelector('.worker-row-name')?.textContent)).toEqual(['Local', 'Unraid', 'linux-worker', 'Laptop']);
		expect(screen.queryByRole('button', { name: 'Other workers' })).toBeNull();
		expect(menu.querySelector('[aria-expanded]')).toBeNull();
		expect(screen.getByRole('menuitemradio', { name: 'Local' }).getAttribute('aria-checked')).toBe('true');
		expect(screen.getByRole('menuitemradio', { name: 'Unraid' }).getAttribute('aria-checked')).toBe('false');
		expect(screen.getByRole('menuitemradio', { name: 'linux-worker offline' }).getAttribute('aria-disabled')).toBe('true');
	});

	it('keeps a peer without a checkout disabled in worktree mode with its reason', async () => {
		vi.mocked(listInstalls).mockResolvedValue(fixture);
		vi.mocked(getClusterGitState).mockResolvedValue({ project_v2_id: 'pv2', installs: [
			{ id: 'runner-1', online: true, eligible: true },
			{ id: 'runner-3', online: true, eligible: false }
		] } as never);
		render(ExecutionModeBar);
		await fireEvent.click(screen.getByRole('button', { name: 'Runner: Local' }));
		const laptop = await screen.findByRole('menuitemradio', { name: 'Laptop no checkout' });
		expect(laptop.getAttribute('aria-disabled')).toBe('true');
		expect(laptop.getAttribute('title')).toBe("No checkout of this project on Laptop — a worktree can't be created there");
		await fireEvent.click(laptop);
		expect(activeSession.current.deviceIntent).toBeNull();
	});

	it('stamps the picked peer as the session runner and checks its row', async () => {
		vi.mocked(listInstalls).mockResolvedValue(fixture);
		render(ExecutionModeBar);
		await fireEvent.click(screen.getByRole('button', { name: 'Runner: Local' }));
		await fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Unraid' }));
		expect(activeSession.current.deviceIntent).toEqual({ id: 'runner-1', name: 'Unraid' });
		expect(screen.queryByRole('menu', { name: 'Runner selection' })).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Runner: Unraid' }));
		expect(screen.getByRole('menuitemradio', { name: 'Unraid' }).getAttribute('aria-checked')).toBe('true');
		expect(screen.getByRole('menuitemradio', { name: 'Local' }).getAttribute('aria-checked')).toBe('false');
	});

	it('says so when there are no other workers, keeping Local pickable', async () => {
		vi.mocked(listInstalls).mockResolvedValue({ self_instance_id: 'local', installs: [] });
		render(ExecutionModeBar);
		await fireEvent.click(screen.getByRole('button', { name: 'Runner: Local' }));
		expect(await screen.findByText('No other workers available.')).toBeTruthy();
		expect(screen.getByRole('menuitemradio', { name: 'Local' })).toBeTruthy();
	});
});
