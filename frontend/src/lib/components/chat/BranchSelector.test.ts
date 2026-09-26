/**
 * Branch pill above the composer: branch listing (local + remote-only),
 * checkout via the toast helper, locks (worktree / streaming / checkout in
 * flight), filtering, and closing (Escape, outside click, New branch).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { writable } from 'svelte/store';

const state = vi.hoisted(() => ({
	session: null as unknown as ReturnType<typeof writable>,
	status: null as unknown as ReturnType<typeof writable>,
	branches: null as unknown as ReturnType<typeof writable>,
	mode: null as unknown as ReturnType<typeof writable>
}));
const git = vi.hoisted(() => ({ loadBranches: vi.fn(), refreshDiffStats: vi.fn() }));
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	state.session = writable({});
	return { activeSession: { subscribe: (fn: (v: unknown) => void) => state.session.subscribe(fn) } };
});
vi.mock('$lib/stores/git.svelte', async () => {
	const { writable } = await import('svelte/store');
	state.status = writable(null);
	state.branches = writable([]);
	state.mode = writable('local');
	return {
		git,
		gitStatus: { subscribe: (fn: (v: unknown) => void) => state.status.subscribe(fn) },
		branches: { subscribe: (fn: (v: unknown) => void) => state.branches.subscribe(fn) },
		gitMode: { subscribe: (fn: (v: unknown) => void) => state.mode.subscribe(fn) }
	};
});
const checkoutBranchWithToast = vi.hoisted(() => vi.fn());
vi.mock('$lib/stores/gitCheckoutAction', () => ({ checkoutBranchWithToast }));

import { openHeaderDropdown } from '$lib/stores/header-dropdowns';
import BranchSelector from './BranchSelector.svelte';

const b = (name: string, remote = false) => ({ name, remote, current: false });

beforeEach(() => {
	vi.clearAllMocks();
	openHeaderDropdown.set(null);
	state.session.set({ worktreeId: null, worktreeBranch: null, selectedWorktreeBranch: null, worktreeDeleted: false, isStreaming: false });
	state.status.set({ current_branch: 'main' });
	state.branches.set([b('main'), b('dev'), b('origin/main', true), b('origin/feature-x', true)]);
	state.mode.set('local');
	checkoutBranchWithToast.mockResolvedValue(undefined);
});
afterEach(() => cleanup());

const pill = () => screen.getByRole('button', { name: /^Branch: |^Select branch$/ }) as HTMLButtonElement;
async function open() {
	await fireEvent.click(pill());
	return screen.getByRole('menu', { name: 'Branch selection' });
}

describe('BranchSelector', () => {
	it('shows the project and current branch, and lists local plus remote-only branches', async () => {
		render(BranchSelector, { props: { projectName: 'Website' } });
		expect(pill().getAttribute('aria-label')).toBe('Branch: main');
		expect(screen.getByText('Website')).toBeTruthy();
		expect(pill().getAttribute('aria-expanded')).toBe('false');

		await open();
		expect(pill().getAttribute('aria-expanded')).toBe('true');
		expect(git.loadBranches).toHaveBeenCalledOnce();
		expect(screen.getByRole('menuitemradio', { name: 'main' }).getAttribute('aria-checked')).toBe('true');
		expect(screen.getByRole('menuitemradio', { name: 'dev' }).getAttribute('aria-checked')).toBe('false');
		// origin/main has a local counterpart; origin/feature-x does not.
		expect(screen.getAllByRole('menuitem').map((i) => i.textContent?.trim())).toEqual(['feature-x origin', 'New branch...']);
		expect(screen.queryByLabelText('Filter branches')).toBeNull();
	});

	it('checks out another branch through the toast helper and locks the pill meanwhile', async () => {
		let resolve!: () => void;
		checkoutBranchWithToast.mockReturnValue(new Promise<void>((r) => (resolve = r)));
		render(BranchSelector);
		await open();
		await fireEvent.click(screen.getByRole('menuitemradio', { name: 'dev' }));
		expect(checkoutBranchWithToast).toHaveBeenCalledWith('dev', expect.any(Function));
		expect(screen.queryByRole('menu')).toBeNull();
		expect(pill().disabled).toBe(true);
		checkoutBranchWithToast.mock.calls[0][1]();
		expect(git.refreshDiffStats).toHaveBeenCalledOnce();
		resolve();
		await waitFor(() => expect(pill().disabled).toBe(false));
	});

	it('checks out a remote-only branch by its short name', async () => {
		render(BranchSelector);
		await open();
		await fireEvent.click(screen.getByRole('menuitem', { name: /feature-x/ }));
		expect(checkoutBranchWithToast).toHaveBeenCalledWith('feature-x', expect.any(Function));
	});

	it('just closes when the current branch is picked', async () => {
		render(BranchSelector);
		await open();
		await fireEvent.click(screen.getByRole('menuitemradio', { name: 'main' }));
		expect(checkoutBranchWithToast).not.toHaveBeenCalled();
		expect(screen.queryByRole('menu')).toBeNull();
	});

	it('filters long branch lists and keeps Enter out of the chat form', async () => {
		state.branches.set(['main', 'dev', 'fix/a', 'fix/b', 'docs', 'chore'].map((n) => b(n)));
		render(BranchSelector);
		await open();
		const filter = screen.getByLabelText('Filter branches');
		await fireEvent.input(filter, { target: { value: 'FIX' } });
		expect(screen.getAllByRole('menuitemradio').map((i) => i.textContent?.trim())).toEqual(['fix/a', 'fix/b']);
		await fireEvent.input(filter, { target: { value: 'zzz' } });
		expect(screen.getByText('No matching branches')).toBeTruthy();
		expect(await fireEvent.keyDown(filter, { key: 'Enter' })).toBe(false);
		// Escape in a non-empty filter clears it without closing the menu.
		await fireEvent.keyDown(filter, { key: 'Escape' });
		expect(screen.getByRole('menu')).toBeTruthy();
	});

	it('is a read-only worktree branch in worktree mode', async () => {
		state.session.set({ worktreeId: 'w1', worktreeBranch: 'agent/task', isStreaming: false });
		render(BranchSelector);
		expect(pill().getAttribute('aria-label')).toBe('Branch: agent/task');
		expect(pill().disabled).toBe(true);
		expect(pill().title).toBe('Worktree branch: agent/task');
		await fireEvent.click(pill());
		expect(screen.queryByRole('menu')).toBeNull();
		expect(git.loadBranches).not.toHaveBeenCalled();
	});

	it('never shows the main repo branch for a deleted worktree', () => {
		state.mode.set('worktree');
		state.session.set({ worktreeId: null, worktreeBranch: null, worktreeDeleted: true, isStreaming: false });
		render(BranchSelector);
		expect(pill().getAttribute('aria-label')).toBe('Select branch');
	});

	it('locks while the agent is streaming', () => {
		state.session.set({ isStreaming: true });
		render(BranchSelector);
		expect(pill().disabled).toBe(true);
		expect(pill().title).toBe('Branch locked during streaming');
	});

	it('closes on Escape and outside click, and hands New branch to the parent', async () => {
		const onClickNewBranch = vi.fn();
		render(BranchSelector, { props: { onClickNewBranch } });
		await open();
		await fireEvent.keyDown(window, { key: 'Escape' });
		expect(screen.queryByRole('menu')).toBeNull();
		await open();
		await fireEvent.click(document.body);
		expect(screen.queryByRole('menu')).toBeNull();
		await open();
		await fireEvent.click(screen.getByRole('menuitem', { name: 'New branch...' }));
		expect(onClickNewBranch).toHaveBeenCalledOnce();
		expect(screen.queryByRole('menu')).toBeNull();
	});

	it('closes when another header dropdown opens', async () => {
		render(BranchSelector);
		await open();
		openHeaderDropdown.set('profile');
		await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());
	});
});
