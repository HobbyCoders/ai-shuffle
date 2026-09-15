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
		syncWorktrees: vi.fn().mockResolvedValue({})
	};
});

vi.hoisted(() => {
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
		loadBranches: mocks.loadBranches
	}
}));

import ExecutionModeBar from './ExecutionModeBar.svelte';
import { activeSession } from '$lib/stores/active-session';
import { getClusterGitState, listInstalls } from '$lib/api/installs';
import { getPersistedDeviceIntent } from '$lib/stores/worker-selection';
import { composerPreferences } from '$lib/stores/composer-preferences';
import { getPersistedWorktreeIntent } from '$lib/stores/session/session-switcher';
import { tick } from 'svelte';

afterEach(() => cleanup());
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
		await fireEvent.click(screen.getByRole('button', { name: 'Other workers' }));
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
		await fireEvent.click(screen.getByRole('button', { name: 'Other workers' }));
		await fireEvent.click(await screen.findByRole('menuitemradio', { name: 'Unraid' }));
		expect(getPersistedDeviceIntent()).toEqual({ id: 'runner-1', name: 'Unraid' });
		activeSession.setProject('another-project', null);
		expect(activeSession.current.deviceIntent).toEqual(getPersistedDeviceIntent());
		await fireEvent.click(screen.getByRole('button', { name: 'Runner: Unraid' }));
		await fireEvent.click(screen.getByRole('menuitemradio', { name: 'Local' }));
		expect(getPersistedDeviceIntent()).toBeNull();
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

	it('opens one dropdown with Auto, New branch, and the base-branch picker', async () => {
		render(ExecutionModeBar);

		await fireEvent.click(screen.getByRole('button', { name: /worktree branch:/i }));

		expect(screen.getByText('Auto')).toBeTruthy();
		expect(screen.getByText('New branch...')).toBeTruthy();
		expect(screen.getByText('Base branch')).toBeTruthy();
		expect(screen.getAllByText('develop').length).toBeGreaterThan(0);
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
