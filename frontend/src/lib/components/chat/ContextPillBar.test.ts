import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { writable } from 'svelte/store';
import ContextPillBar from './ContextPillBar.svelte';
import { adoptSession } from '$lib/api/sessions';

const { session, execution, appSessions, listInstallsMock, getClusterGitStateMock } = vi.hoisted(() => ({
	session: { store: null as any },
	execution: vi.fn(),
	appSessions: { store: null as any },
	listInstallsMock: vi.fn(),
	getClusterGitStateMock: vi.fn()
}));
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	session.store = writable({});
	return { activeSession: session.store };
});
vi.mock('$lib/stores/app-data.svelte', () => {
	const sessions = writable([]);
	appSessions.store = sessions;
	return {
		projects: writable([{ id: 'project', name: 'Project' }]),
		clusterProjects: writable([]),
		sessions,
		appData: { loadSessions: vi.fn() }
	};
});
vi.mock('$lib/stores/auth.svelte', () => ({ githubAuthenticated: writable(false) }));
vi.mock('$lib/stores/git.svelte', () => ({
	gitStatus: writable({ current_branch: 'main' }), git: {}
}));
vi.mock('./ProfileSelector.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('./WorkspaceFolderSelector.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('./BranchSelector.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/layout/ExecutionModeBar.svelte', async () => {
	const { default: component } = await import('$lib/test-stubs/EmptyRouteDependency.svelte');
	return { default: (...args: Parameters<typeof component>) => { execution(); return component(...args); } };
});
vi.mock('$lib/components/git/CreateBranchModal.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/api/installs', () => ({
	listInstalls: listInstallsMock,
	getClusterGitState: getClusterGitStateMock
}));
vi.mock('$lib/api/sessions', () => ({ adoptSession: vi.fn(), moveSession: vi.fn() }));

afterEach(() => {
	cleanup();
	vi.mocked(adoptSession).mockClear();
	execution.mockClear();
	appSessions.store?.set([]);
	listInstallsMock.mockReset();
	listInstallsMock.mockResolvedValue({ installs: [] });
	getClusterGitStateMock.mockReset();
	getClusterGitStateMock.mockResolvedValue(null);
});

const base = {
	sessionId: 'saved-session', project: 'project', mode: 'code', messages: [{ role: 'user', content: 'Fix bug' }],
	worktreeIntent: 'worktree', worktreeId: null, worktreeBranch: null,
	selectedWorktreeBranch: null, isCreatingWorktree: false
};

describe.each([false, true])('worktree indicator (sheetMode=%s)', (sheetMode) => {
	it.each(['project', 'canonical'])('offers worker and branch selection without GitHub auth for %s', kind => {
		execution.mockClear();
		session.store.set({ ...base, sessionId: null, messages: [], project: kind === 'project' ? 'project' : null, projectV2Id: kind === 'canonical' ? 'pv2-project' : null });
		render(ContextPillBar, { sheetMode });
		expect(execution).toHaveBeenCalledOnce();
	});
	it('keeps a persisted zero-message remote session in the runner view', async () => {
		session.store.set({
			...base,
			sessionId: 'remote-empty',
			messages: [],
			projectV2Id: 'pv2-project'
		});
		appSessions.store.set([
			{
				id: 'remote-empty',
				project_id: null,
				project_v2_id: 'pv2-project',
				instance_id: 'owner',
				device_name: 'Other worker',
				device_online: true,
				is_remote: true,
				controllable: true
			}
		]);
		const { container } = render(ContextPillBar, { sheetMode });
		expect(execution).not.toHaveBeenCalled();
		const runner = container.querySelector<HTMLButtonElement>('.ctx-chip.runner');
		expect(runner).not.toBeNull();
		expect(runner?.textContent).toContain('Other worker');

		listInstallsMock.mockResolvedValue({ installs: [] });
		getClusterGitStateMock.mockResolvedValue(null);
		await fireEvent.click(runner!);
		await waitFor(() => expect(container.querySelector('.runner-item')?.textContent).toContain('Continue here'));
	});
	it.each(['cancel', 'confirm', 'navigate'])('requires confirmation before Git publication: %s', async (action) => {
		session.store.set({ ...base, sessionId: 'remote', projectV2Id: 'pv2-project' });
		appSessions.store.set([{ id: 'remote', project_v2_id: 'pv2-project', instance_id: 'owner',
			device_name: 'Other worker', device_online: true, is_remote: true, controllable: true }]);
		listInstallsMock.mockResolvedValue({ installs: [] });
		getClusterGitStateMock.mockResolvedValue(null);
		const view = render(ContextPillBar, { sheetMode });
		await fireEvent.click(view.container.querySelector<HTMLButtonElement>('.ctx-chip.runner')!);
		await fireEvent.click(await view.findByRole('menuitem', { name: 'Continue here' }));
		expect(view.getByRole('dialog')).toBeTruthy();
		expect(view.getByText(/commit and push are not undone/)).toBeTruthy();
		expect(adoptSession).not.toHaveBeenCalled();
		if (action === 'cancel') {
			await fireEvent.click(view.getAllByRole('button', { name: 'Cancel' }).at(-1)!);
		} else {
			if (action === 'navigate') {
				session.store.set({ ...base, sessionId: 'different' });
				await tick();
			}
			await fireEvent.click(view.getByRole('button', { name: 'Commit, push and continue' }));
		}
		if (action === 'confirm') expect(adoptSession).toHaveBeenCalledExactlyOnceWith('remote');
		else expect(adoptSession).not.toHaveBeenCalled();
		expect(view.queryByRole('dialog')).toBeNull();
	});

	it('moves a plain chat without the Git publication dialog', async () => {
		session.store.set({ ...base, sessionId: 'remote' });
		appSessions.store.set([{ id: 'remote', instance_id: 'owner',
			device_name: 'Other worker', device_online: true, is_remote: true, controllable: true }]);
		listInstallsMock.mockResolvedValue({ installs: [] });
		getClusterGitStateMock.mockResolvedValue(null);
		const view = render(ContextPillBar, { sheetMode });
		await fireEvent.click(view.container.querySelector<HTMLButtonElement>('.ctx-chip.runner')!);
		await fireEvent.click(await view.findByRole('menuitem', { name: 'Continue here' }));
		expect(view.queryByRole('dialog')).toBeNull();
		await waitFor(() => expect(adoptSession).toHaveBeenCalledExactlyOnceWith('remote'));
	});

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
