/**
 * "Runs on" in the phone's Chat controls (#1816) moves this chat with the
 * busy rule from #1819: an idle chat moves now; a running or parked one
 * queues "Move when this turn ends", which can be cancelled. The tile is the
 * shared ContextPillBar selector, so the rule is the one the desktop uses.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';
import { getQueuedMove, queueMove, cancelQueuedMove, moveSession } from '$lib/api/sessions';

const { session, appSessions, listInstallsMock } = vi.hoisted(() => ({
	session: { store: null as any },
	appSessions: { store: null as any },
	listInstallsMock: vi.fn()
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
		projects: writable([{ id: 'project', name: 'ai-shuffle' }]),
		clusterProjects: writable([]),
		sessions,
		appData: { loadSessions: vi.fn() }
	};
});
vi.mock('$lib/stores/auth.svelte', () => ({ githubAuthenticated: writable(false) }));
vi.mock('$lib/stores/git.svelte', () => ({ gitStatus: writable({ current_branch: 'main' }), git: {} }));
vi.mock('$lib/components/chat/ProfileSelector.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/WorkspaceFolderSelector.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/BranchSelector.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/layout/ExecutionModeBar.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/git/CreateBranchModal.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/RunningTasksList.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/ContextUsageBar.svelte', () => import('./test-fixtures/ContextUsageBarStub.svelte'));
vi.mock('$lib/api/installs', () => ({
	listInstalls: listInstallsMock,
	getClusterGitState: vi.fn().mockResolvedValue({ project_v2_id: 'pv2', installs: [{ id: 'unraid', online: true, eligible: true }] })
}));
vi.mock('$lib/api/sessions', () => ({
	adoptSession: vi.fn(), moveSession: vi.fn(),
	getQueuedMove: vi.fn(), queueMove: vi.fn(), cancelQueuedMove: vi.fn()
}));

afterEach(() => {
	cleanup();
	vi.mocked(getQueuedMove).mockReset();
	vi.mocked(queueMove).mockReset();
	vi.mocked(cancelQueuedMove).mockReset();
	vi.mocked(moveSession).mockReset();
	document.querySelectorAll('.mcp-portal').forEach((n) => n.remove());
});

function props() {
	return {
		open: true, onClose: vi.fn(), onOpenChange: vi.fn(),
		modelOptions: [], modelValue: 'opus', modelLabel: 'Opus', onModelSelect: vi.fn(),
		contextChoice: 'standard' as const, onContextChoiceSelect: vi.fn(),
		effortOptions: [], effortValue: null, onEffortSelect: vi.fn(),
		permissionOptions: [{ value: 'default', label: 'Ask', description: '' }], permissionValue: 'default', onPermissionSelect: vi.fn(),
		showSetup: true
	};
}

async function openRunsOn(busy: { running: boolean; tasks: string[] }) {
	session.store.set({
		sessionId: 'chat-1', title: 'Linux install guide', mode: 'code', project: 'project', projectV2Id: 'pv2',
		messages: [{ role: 'user', content: 'How do I install on Linux?' }], activeTasks: [],
		worktreeIntent: 'worktree', worktreeId: null, worktreeBranch: null, selectedWorktreeBranch: null, isCreatingWorktree: false
	});
	appSessions.store.set([{ id: 'chat-1', project_v2_id: 'pv2', instance_id: 'desktopworker', is_remote: false, controllable: true }]);
	listInstallsMock.mockResolvedValue({ installs: [
		{ id: 'desktopworker', is_self: true, online: true, device_name: 'DesktopWorker' },
		{ id: 'unraid', is_self: false, online: true, device_name: 'Unraid', base_url: 'https://unraid.test' }
	] });
	vi.mocked(getQueuedMove).mockResolvedValue({ busy, pending: null });
	render(MobileComposerControlsSheet, { props: props() });
	const panel = document.querySelector<HTMLElement>('.mcp')!;
	const tile = await within(panel).findByRole('button', { name: /^Runner:/ });
	// The tile reads as "Runs on" over the worker, a label the panel draws.
	expect(panel.querySelector('.mcp-setup .ctx-chip.runner')).toBe(tile);
	await fireEvent.click(tile);
	await fireEvent.click(await within(panel).findByRole('menuitem', { name: 'Unraid' }));
	return panel;
}

describe('Runs on in Chat controls', () => {
	it('queues the move while the turn runs, then shows it and lets it be cancelled', async () => {
		vi.mocked(queueMove).mockResolvedValue({ queued: true, worker: 'Unraid' });
		vi.mocked(cancelQueuedMove).mockResolvedValue(undefined);
		const panel = await openRunsOn({ running: true, tasks: ['pytest -q'] });
		expect(await within(panel).findByText('This chat is running')).toBeTruthy();
		expect(within(panel).getByText('pytest -q')).toBeTruthy();
		await fireEvent.click(within(panel).getByRole('menuitem', { name: 'Move when this turn ends' }));
		expect(queueMove).toHaveBeenCalledExactlyOnceWith('chat-1', 'unraid', false);
		expect(moveSession).not.toHaveBeenCalled();
		const tile = panel.querySelector<HTMLButtonElement>('.ctx-chip.runner')!;
		await waitFor(() => expect(tile.textContent).toContain('Moving to Unraid after this turn'));
		expect(tile.disabled).toBe(true);
		await fireEvent.click(within(panel).getByRole('button', { name: 'Cancel queued move' }));
		expect(cancelQueuedMove).toHaveBeenCalledExactlyOnceWith('chat-1');
	});

	it('offers to stop parked work and move now', async () => {
		vi.mocked(queueMove).mockResolvedValue({ queued: true, worker: 'Unraid' });
		const panel = await openRunsOn({ running: false, tasks: ['Watch CI'] });
		expect(await within(panel).findByText('Background work is running')).toBeTruthy();
		await fireEvent.click(within(panel).getByRole('menuitem', { name: 'Stop them and move now' }));
		expect(queueMove).toHaveBeenCalledExactlyOnceWith('chat-1', 'unraid', true);
	});

	it('moves an idle chat now', async () => {
		await openRunsOn({ running: false, tasks: [] });
		await fireEvent.click(await screen.findByRole('button', { name: 'Commit, push and move' }));
		expect(moveSession).toHaveBeenCalledExactlyOnceWith('chat-1', 'unraid');
		expect(queueMove).not.toHaveBeenCalled();
	});
});
