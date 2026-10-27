/**
 * A new chat's "Runs on" row in the phone's Chat controls (#705): the real
 * ExecutionModeBar's runner picker, opened as the sheet's own page. The other
 * sheet tests stub ExecutionModeBar; this one renders it, so the `.mode-trigger`
 * lookup, the hosted menu and its shared dropdown id are checked end to end.
 */
import { afterAll, afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';

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
			get current() { return value; },
			set(next: T) {
				value = next;
				for (const run of subscribers) run(value);
			}
		};
	}
	return {
		worktrees: store([] as Array<Record<string, unknown>>),
		branches: store([{ name: 'main', current: true, remote: false }]),
		git: store({ projectId: 'repo', worktreeId: null as string | null, sessionId: null as string | null,
			hydrated: true, status: { is_git_repo: true } }),
		gitStatus: store({ current_branch: 'main', is_git_repo: true }),
		currentBranch: store({ name: 'main' }),
		listInstalls: vi.fn()
	};
});

vi.hoisted(() => {
	vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} });
	vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 404, json: async () => ({}) }));
});

vi.mock('$lib/api/installs', () => ({
	listInstalls: mocks.listInstalls,
	getClusterGitState: vi.fn().mockResolvedValue(null)
}));
vi.mock('$lib/stores/git.svelte', () => ({
	worktrees: mocks.worktrees,
	branches: mocks.branches,
	currentBranch: mocks.currentBranch,
	gitStatus: mocks.gitStatus,
	git: {
		...mocks.git,
		get current() { return mocks.git.current; },
		loadWorktrees: vi.fn().mockResolvedValue(undefined),
		loadBranches: vi.fn().mockResolvedValue(undefined),
		refreshStatus: vi.fn()
	}
}));
vi.mock('$lib/components/chat/ProfileSelector.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/WorkspaceFolderSelector.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/BranchSelector.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/git/CreateBranchModal.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/RunningTasksList.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/ContextUsageBar.svelte', () => import('./test-fixtures/ContextUsageBarStub.svelte'));

import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';
import { activeSession } from '$lib/stores/active-session';
import { resetWorkerPresence } from '$lib/stores/worker-presence';
import { get } from 'svelte/store';
import { openHeaderDropdown } from '$lib/stores/header-dropdowns';

afterAll(() => vi.unstubAllGlobals());
beforeEach(() => {
	Object.assign(activeSession.current, { sessionId: null, mode: 'code', project: 'repo', projectV2Id: null, messages: [], activeTasks: [] });
	activeSession.setDeviceIntent(null);
	activeSession.setWorktreeIntent('local');
	mocks.listInstalls.mockResolvedValue({ installs: [
		{ id: 'self', is_self: true, online: true, device_name: 'DesktopWorker' },
		{ id: 'unraid', is_self: false, online: true, device_name: 'ServerWorker', base_url: 'https://unraid.test' }
	] });
});
afterEach(() => {
	cleanup();
	resetWorkerPresence();
	activeSession.setDeviceIntent(null);
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

async function openRunsOn() {
	render(MobileComposerControlsSheet, { props: props() });
	const panel = document.querySelector<HTMLElement>('.mcp')!;
	const row = await within(panel).findByRole('button', { name: 'Runner: Local' });
	expect(row.classList.contains('mode-trigger')).toBe(true);
	expect(row.closest('.mcp-setup')).toBeTruthy();
	await fireEvent.click(row);
	const runsOn = panel.querySelector<HTMLElement>('[data-page="runner"]')!;
	await waitFor(() => expect(panel.getAttribute('aria-label')).toBe('Runs on'));
	expect(runsOn.classList.contains('active')).toBe(true);
	// The picker sits on the page, not under its row.
	const menu = within(runsOn).getByRole('menu', { name: 'Runner selection' });
	expect(panel.querySelector('.mcp-setup [role="menu"]')).toBeNull();
	return { panel, row, runsOn, menu };
}

describe('Runs on for a new chat', () => {
	it('opens the real runner picker as a page, and a pick sets the worker and slides back', async () => {
		const { panel, runsOn, menu } = await openRunsOn();
		const worker = await within(menu).findByRole('menuitemradio', { name: 'ServerWorker' });
		await fireEvent.click(worker);
		expect(activeSession.current.deviceIntent).toEqual({ id: 'unraid', name: 'ServerWorker' });
		await waitFor(() => expect(panel.getAttribute('aria-label')).toBe('Chat controls'));
		// The list stays on the page while it slides out, then goes.
		expect(runsOn.querySelector('[data-mcp-ghost]')?.textContent).toContain('ServerWorker');
		await waitFor(() => expect(runsOn.querySelector('[role="menu"]')).toBeNull(), { timeout: 1500 });
		expect(within(panel).getByRole('button', { name: 'Runner: ServerWorker' })).toBeTruthy();
	});

	it('Back closes the picker through its shared id, with no synthetic click', async () => {
		const { panel, row } = await openRunsOn();
		expect(get(openHeaderDropdown)).toBe('sheet-mode-runner');
		const dispatch = vi.spyOn(document.body, 'dispatchEvent');
		await fireEvent.click(within(panel).getByRole('button', { name: 'Back to chat controls' }));
		expect(dispatch.mock.calls.some(([e]) => e.type === 'click')).toBe(false);
		dispatch.mockRestore();
		expect(panel.getAttribute('aria-label')).toBe('Chat controls');
		expect(get(openHeaderDropdown)).toBeNull();
		await waitFor(() => expect(row.getAttribute('aria-expanded')).toBe('false'));
		expect(activeSession.current.deviceIntent ?? null).toBeNull();
	});
});
