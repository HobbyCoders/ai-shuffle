/**
 * The Agent row in the phone's Chat controls opens the shared agent menu
 * (ProfileSelector) as the sheet's own "Agent" page: one row per agent with
 * its description, a check on the current one, and a pick that saves and
 * slides back to the main list.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import MobileComposerControlsSheet from './MobileComposerControlsSheet.svelte';

const { session, setProfile } = vi.hoisted(() => ({ session: { store: null as any }, setProfile: vi.fn() }));
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	session.store = writable({});
	return { activeSession: { subscribe: session.store.subscribe, setProfile } };
});
vi.mock('$lib/stores/app-data.svelte', () => ({
	projects: writable([]),
	clusterProjects: writable([]),
	sessions: writable([]),
	profiles: writable([
		{ id: 'overseer', name: 'Overseer', description: 'Coordinates work and delegates' },
		{ id: 'reviewer', name: 'Reviewer', description: 'Read-only code review' },
		{ id: 'plain', name: 'Plain', description: null }
	]),
	appData: { loadSessions: vi.fn() }
}));
vi.mock('$lib/stores/auth.svelte', () => ({ githubAuthenticated: writable(false), isAuthenticated: writable(true), isAdmin: writable(true) }));
vi.mock('$lib/stores/git.svelte', () => ({
	gitStatus: writable(null),
	git: writable({ projectId: null, sessionId: 'chat-1', worktreeId: null, hydrated: true, status: null })
}));
vi.mock('$lib/components/chat/WorkspaceFolderSelector.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/BranchSelector.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/layout/ExecutionModeBar.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/git/CreateBranchModal.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/RunningTasksList.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/components/chat/ContextUsageBar.svelte', () => import('./test-fixtures/ContextUsageBarStub.svelte'));
vi.mock('$lib/api/installs', () => ({ listInstalls: vi.fn().mockResolvedValue({ installs: [] }), getClusterGitState: vi.fn() }));

afterEach(() => {
	cleanup();
	setProfile.mockReset();
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

describe('Agent in Chat controls', () => {
	it('opens the agent list as a page and slides back after a pick', async () => {
		session.store.set({ sessionId: null, mode: 'chat', profile: 'overseer', messages: [], activeTasks: [] });
		render(MobileComposerControlsSheet, { props: props() });
		const panel = document.querySelector<HTMLElement>('.mcp')!;
		const row = await within(panel).findByRole('button', { name: 'Agent: Overseer' });
		expect(row.closest('.mcp-setup')).toBeTruthy();
		await fireEvent.click(row);

		// The shared menu now sits on the Agent page, not under the row.
		const agentPage = panel.querySelector<HTMLElement>('[data-page="agent"]')!;
		const menu = await within(agentPage).findByRole('menu', { name: 'Agent selection' });
		await waitFor(() => expect(panel.getAttribute('aria-label')).toBe('Agent'));
		expect(agentPage.classList.contains('active')).toBe(true);
		expect(panel.querySelector('.mcp-setup [role="menu"]')).toBeNull();
		// A description line where the agent has one; the current agent is checked.
		expect(within(menu).getByText('Read-only code review')).toBeTruthy();
		const current = within(menu).getByText('Overseer').closest('button')!;
		expect(current.classList.contains('selected')).toBe(true);
		expect(current.querySelector('.check-icon')).toBeTruthy();

		await fireEvent.click(within(menu).getByText('Reviewer').closest('button')!);
		expect(setProfile).toHaveBeenCalledExactlyOnceWith('reviewer');
		await waitFor(() => expect(panel.getAttribute('aria-label')).toBe('Chat controls'));
		// The menu closed; an inert copy keeps the page from going blank as it
		// slides out (#705), then goes.
		expect(agentPage.querySelector('[role="menu"]:not([data-mcp-ghost])')).toBeNull();
		expect(agentPage.querySelector<HTMLElement>('[role="menu"][data-mcp-ghost]')?.inert).toBe(true);
		await waitFor(() => expect(agentPage.querySelector('[role="menu"]')).toBeNull());
	});

	it('Back closes the hosted menu and returns to the list', async () => {
		session.store.set({ sessionId: null, mode: 'chat', profile: 'overseer', messages: [], activeTasks: [] });
		render(MobileComposerControlsSheet, { props: props() });
		const panel = document.querySelector<HTMLElement>('.mcp')!;
		await fireEvent.click(await within(panel).findByRole('button', { name: 'Agent: Overseer' }));
		await waitFor(() => expect(panel.getAttribute('aria-label')).toBe('Agent'));
		await fireEvent.click(within(panel).getByRole('button', { name: 'Back to chat controls' }));
		await waitFor(() => expect(panel.querySelector('[data-page="agent"] [role="menu"]')).toBeNull());
		expect(panel.getAttribute('aria-label')).toBe('Chat controls');
		expect(setProfile).not.toHaveBeenCalled();
	});
});
