import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import { writable } from 'svelte/store';
import ContextPillBar from './ContextPillBar.svelte';
import { adoptSession, moveSession } from '$lib/api/sessions';
import { notify } from '$lib/stores/notifications';

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
	vi.mocked(moveSession).mockReset();
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

	it.each(['cancel', 'confirm', 'navigate'])('offers a confirmed move for a persisted local Git session: %s', async action => {
		session.store.set({ ...base, messages: [], projectV2Id: 'pv2-project' });
		appSessions.store.set([{ id: base.sessionId, project_v2_id: 'pv2-project',
			instance_id: 'local', is_remote: false, controllable: true }]);
		listInstallsMock.mockResolvedValue({ installs: [
			{ id: 'local', is_self: true, online: true, device_name: 'This worker' },
			{ id: 'peer', is_self: false, online: true, device_name: 'Other worker' },
			{ id: 'offline', is_self: false, online: false, device_name: 'Offline worker' },
			{ id: 'missing', is_self: false, online: true, device_name: 'Missing checkout' }
		] });
		getClusterGitStateMock.mockResolvedValue({ project_v2_id: 'pv2-project', installs: [
			{ id: 'peer', online: true, eligible: true }, { id: 'missing', online: true, eligible: false }
		] });
		const view = render(ContextPillBar, { sheetMode });
		await fireEvent.click(view.getByRole('button', { name: 'Runner: Local' }));
		const destination = await view.findByRole('menuitem', { name: 'Other worker' });
		expect(view.queryByRole('menuitem', { name: 'Continue here' })).toBeNull();
		expect((view.getByRole('menuitem', { name: 'Offline worker' }) as HTMLButtonElement).disabled).toBe(true);
		expect((view.getByRole('menuitem', { name: 'Missing checkout' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.click(destination);
		expect(view.getByRole('dialog')).toBeTruthy();
		expect(moveSession).not.toHaveBeenCalled();
		if (action === 'cancel') await fireEvent.click(view.getAllByRole('button', { name: 'Cancel' }).at(-1)!);
		else {
			if (action === 'navigate') { session.store.set({ ...base, sessionId: 'different' }); await tick(); }
			await fireEvent.click(view.getByRole('button', { name: 'Commit, push and move' }));
		}
		if (action === 'confirm') expect(moveSession).toHaveBeenCalledExactlyOnceWith(base.sessionId, 'peer');
		else expect(moveSession).not.toHaveBeenCalled();
	});

	it('shows why a confirmed move was refused inside the reopened worker menu', async () => {
		const reason = 'Another session is working in this checkout or on this branch. Wait for it to finish before moving this session.';
		session.store.set({ ...base, messages: [], projectV2Id: 'pv2-project' });
		appSessions.store.set([{ id: base.sessionId, project_v2_id: 'pv2-project',
			instance_id: 'local', is_remote: false, controllable: true }]);
		listInstallsMock.mockResolvedValue({ installs: [
			{ id: 'local', is_self: true, online: true, device_name: 'This worker' },
			{ id: 'peer', is_self: false, online: true, device_name: 'Other worker' }
		] });
		getClusterGitStateMock.mockResolvedValue({ project_v2_id: 'pv2-project', installs: [
			{ id: 'peer', online: true, eligible: true }
		] });
		vi.mocked(moveSession).mockRejectedValueOnce({ status: 409, detail: reason });
		const view = render(ContextPillBar, { sheetMode });
		await fireEvent.click(view.getByRole('button', { name: 'Runner: Local' }));
		await fireEvent.click(await view.findByRole('menuitem', { name: 'Other worker' }));
		await fireEvent.click(view.getByRole('button', { name: 'Commit, push and move' }));
		const alert = await view.findByRole('alert');
		expect(alert.textContent).toBe(reason);
		expect(view.getByRole('menu', { name: 'Move this session' })).toBeTruthy();
		expect(await view.findByRole('menuitem', { name: 'Other worker' })).toBeTruthy();
		// Closing the menu clears the reason; reopening starts clean.
		await fireEvent.click(view.getByRole('button', { name: 'Runner: Local' }));
		await fireEvent.click(view.getByRole('button', { name: 'Runner: Local' }));
		await view.findByRole('menuitem', { name: 'Other worker' });
		expect(view.queryByRole('alert')).toBeNull();
	});

	async function confirmContinueHere() {
		session.store.set({ ...base, sessionId: 'remote', projectV2Id: 'pv2-project' });
		appSessions.store.set([{ id: 'remote', project_v2_id: 'pv2-project', instance_id: 'owner',
			device_name: 'Other worker', device_online: true, is_remote: true, controllable: true }]);
		const view = render(ContextPillBar, { sheetMode });
		await fireEvent.click(view.container.querySelector<HTMLButtonElement>('.ctx-chip.runner')!);
		await fireEvent.click(await view.findByRole('menuitem', { name: 'Continue here' }));
		await fireEvent.click(view.getByRole('button', { name: 'Commit, push and continue' }));
		return view;
	}

	it('shows Continue here as busy and blocks a second submit until it settles', async () => {
		let finish!: (value: unknown) => void;
		vi.mocked(adoptSession).mockReturnValueOnce(new Promise((resolve) => (finish = resolve)) as never);
		const success = vi.spyOn(notify, 'success');
		const view = await confirmContinueHere();
		const chip = view.container.querySelector<HTMLButtonElement>('.ctx-chip.runner')!;
		await waitFor(() => expect(chip.textContent).toContain('Continuing here…'));
		expect(chip.disabled).toBe(true);
		expect(chip.getAttribute('aria-busy')).toBe('true');
		expect(chip.querySelector('.spin')).not.toBeNull();
		expect(view.queryByRole('menu')).toBeNull();
		await fireEvent.click(chip);
		expect(view.queryByRole('menu')).toBeNull();
		expect(adoptSession).toHaveBeenCalledOnce();

		finish({ session_id: 'remote', adopted: true, status: 'adopted' });
		await waitFor(() => expect(chip.textContent).toContain('Now runs here'));
		expect(chip.classList.contains('done')).toBe(true);
		expect(chip.disabled).toBe(false);
		expect(chip.getAttribute('aria-busy')).toBeNull();
		expect(success).toHaveBeenCalledWith('This session now runs on this worker.');
		success.mockRestore();
	});

	it('shows why Continue here failed and restores the chip', async () => {
		const reason = "Branch main in the receiving worker's checkout C:/repo has 1 commit (newest: abc1234 fix) that is not on origin. Push them from that worker, or move them to another branch there, then try again. Nothing was changed.";
		vi.mocked(adoptSession).mockRejectedValueOnce({ status: 409, detail: reason });
		const error = vi.spyOn(notify, 'error');
		const view = await confirmContinueHere();
		const alert = await view.findByRole('alert');
		expect(alert.textContent).toBe(reason);
		expect(error).toHaveBeenCalledWith(reason);
		const chip = view.container.querySelector<HTMLButtonElement>('.ctx-chip.runner')!;
		expect(chip.disabled).toBe(false);
		expect(chip.getAttribute('aria-busy')).toBeNull();
		expect(chip.textContent).toContain('Other worker');
		expect(chip.classList.contains('done')).toBe(false);
		error.mockRestore();
	});

	it('explains empty and failed local worker discovery and supports retry', async () => {
		session.store.set(base);
		appSessions.store.set([{ id: base.sessionId, is_remote: false }]);
		listInstallsMock.mockRejectedValueOnce(new Error('offline')).mockResolvedValue({ installs: [] });
		const view = render(ContextPillBar, { sheetMode });
		await fireEvent.click(view.getByRole('button', { name: 'Runner: Local' }));
		expect(await view.findByRole('alert')).toBeTruthy();
		await fireEvent.click(view.getByRole('menuitem', { name: 'Retry' }));
		expect(await view.findByRole('status')).toBeTruthy();
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
