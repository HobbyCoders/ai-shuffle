import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, render } from '@testing-library/svelte';
import { tick } from 'svelte';
import { writable } from 'svelte/store';
import ContextPillBar from './ContextPillBar.svelte';

const { session } = vi.hoisted(() => ({ session: { store: null as any } }));
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	session.store = writable({});
	return { activeSession: session.store };
});
vi.mock('$lib/stores/app-data.svelte', () => ({
	projects: writable([{ id: 'project', name: 'Project' }]),
	clusterProjects: writable([]), sessions: writable([]), appData: {}
}));
vi.mock('$lib/stores/auth.svelte', () => ({ githubAuthenticated: writable(true) }));
vi.mock('$lib/stores/git.svelte', () => ({
	gitStatus: writable({ current_branch: 'main' }), git: {}
}));
vi.mock('./ProfileSelector.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('./WorkspaceFolderSelector.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('./BranchSelector.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/layout/ExecutionModeBar.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/git/CreateBranchModal.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));

afterEach(cleanup);

const base = {
	project: 'project', mode: 'code', messages: [{ role: 'user', content: 'Fix bug' }],
	worktreeIntent: 'worktree', worktreeId: null, worktreeBranch: null,
	selectedWorktreeBranch: null, isCreatingWorktree: false
};

describe.each([false, true])('worktree indicator (sheetMode=%s)', (sheetMode) => {
	it('stays checked from auto naming through completed setup', async () => {
		session.store.set({ ...base, isCreatingWorktree: true });
		const { container } = render(ContextPillBar, { sheetMode });
		const checked = () => container.querySelector('.wt-indicator .ctx-chip-cb')!.classList.contains('checked');
		expect(checked()).toBe(true);
		expect(container.querySelector('.wt-indicator')!.getAttribute('title')).toBe('Runs in an isolated git worktree');
		session.store.set({ ...base, worktreeId: 'checkout', worktreeBranch: 'fix/bug' });
		await tick();
		expect(checked()).toBe(true);
	});

	it('does not treat a saved worktree preference as an existing worktree', () => {
		session.store.set(base);
		const { container } = render(ContextPillBar, { sheetMode });
		expect(container.querySelector('.wt-indicator .checked')).toBeNull();
		expect(container.querySelector('.wt-indicator')!.getAttribute('title')).toBe('Runs in the main checkout');
	});

	it('clears pending selection on failure or navigation to a local session', async () => {
		session.store.set({ ...base, isCreatingWorktree: true });
		const { container } = render(ContextPillBar, { sheetMode });
		expect(container.querySelector('.wt-indicator .checked')).not.toBeNull();
		session.store.set({ ...base, worktreeError: 'Setup failed' });
		await tick();
		expect(container.querySelector('.wt-indicator .checked')).toBeNull();
	});

	it('keeps cleaned-up worktree sessions checked', () => {
		session.store.set({ ...base, worktreeBranch: 'fix/bug', worktreeDeleted: true });
		const { container } = render(ContextPillBar, { sheetMode });
		expect(container.querySelector('.wt-indicator .checked')).not.toBeNull();
	});
});
