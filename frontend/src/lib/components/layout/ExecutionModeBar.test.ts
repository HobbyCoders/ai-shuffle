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
		gitStatus: store({ is_git_repo: true }),
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
	gitStatus: mocks.gitStatus,
	currentBranch: mocks.currentBranch,
	git: {
		loadWorktrees: mocks.loadWorktrees,
		loadBranches: mocks.loadBranches
	}
}));

import ExecutionModeBar from './ExecutionModeBar.svelte';
import { activeSession } from '$lib/stores/active-session';
import { listInstalls } from '$lib/api/installs';
import { getPersistedDeviceIntent } from '$lib/stores/worker-selection';

afterEach(() => cleanup());
afterAll(() => vi.unstubAllGlobals());

beforeEach(() => {
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

describe('worker picker persistence', () => {
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
