/**
 * A name on the composer's context line opens Chat controls straight on that
 * name's selector: the Agent name shows the agent list at once, with no Back to
 * a list it never showed, and a pick closes the panel back to the composer.
 * A sheet opened without a row (the Controls cell) still starts on its list.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, waitFor, within } from '@testing-library/svelte';
import { writable } from 'svelte/store';
import { page } from '$app/state';
import MobileComposerControlsSheet from '$lib/components/chat/phone/MobileComposerControlsSheet.svelte';

const { session, setProfile } = vi.hoisted(() => ({ session: { store: null as any }, setProfile: vi.fn() }));
// The panel's history entry lands in page.state, as SvelteKit's shallow routing does.
vi.mock('$app/navigation', async () => {
	const { page } = await import('$app/state');
	return { pushState: (_url: string, state: Record<string, unknown>) => { page.state = state; } };
});
vi.mock('$lib/stores/active-session', async () => {
	const { writable } = await import('svelte/store');
	session.store = writable({});
	return { activeSession: { subscribe: session.store.subscribe, setProfile }, streamingSessions: writable(new Set<string>()) };
});
vi.mock('$lib/stores/app-data.svelte', () => ({
	projects: writable([]),
	clusterProjects: writable([]),
	sessions: writable([]),
	profiles: writable([
		{ id: 'overseer', name: 'Overseer', description: 'Coordinates work and delegates' },
		{ id: 'reviewer', name: 'Reviewer', description: 'Read-only code review' }
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
	page.state = {};
	document.querySelectorAll('.mcp-portal').forEach((n) => n.remove());
});

function props(setupRow: 'agent' | null) {
	return {
		open: true, setupRow, onClose: vi.fn(), onOpenChange: vi.fn(),
		modelOptions: [], modelValue: 'opus', modelLabel: 'Opus', onModelSelect: vi.fn(),
		contextChoice: 'standard' as const, onContextChoiceSelect: vi.fn(),
		effortOptions: [], effortValue: null, onEffortSelect: vi.fn(),
		permissionOptions: [{ value: 'default', label: 'Ask', description: '' }], permissionValue: 'default', onPermissionSelect: vi.fn(),
		showSetup: true
	};
}

describe('Chat controls opened from the context line', () => {
	it('opens straight on the agent list, and a pick closes the panel', async () => {
		session.store.set({ sessionId: null, mode: 'chat', profile: 'overseer', messages: [], activeTasks: [] });
		const p = props('agent');
		render(MobileComposerControlsSheet, { props: p });
		const panel = document.querySelector<HTMLElement>('.mcp')!;
		const agentPage = panel.querySelector<HTMLElement>('[data-page="agent"]')!;
		const menu = await within(agentPage).findByRole('menu', { name: 'Agent selection' });
		await waitFor(() => expect(panel.getAttribute('aria-label')).toBe('Agent'));
		// No way back to a list it never showed: only out.
		expect(panel.querySelector('.mcp-back')!.getAttribute('aria-hidden')).toBe('true');

		await fireEvent.click(within(menu).getByText('Reviewer').closest('button')!);
		expect(setProfile).toHaveBeenCalledExactlyOnceWith('reviewer');
		await waitFor(() => expect(p.onOpenChange).toHaveBeenCalledWith(false));
	});

	it('starts on the list when opened without a row', async () => {
		session.store.set({ sessionId: null, mode: 'chat', profile: 'overseer', messages: [], activeTasks: [] });
		const p = props(null);
		render(MobileComposerControlsSheet, { props: p });
		const panel = document.querySelector<HTMLElement>('.mcp')!;
		await new Promise((resolve) => setTimeout(resolve, 50));
		expect(panel.getAttribute('aria-label')).toBe('Chat controls');
		expect(document.querySelector('[role="menu"][aria-label="Agent selection"]')).toBeNull();
		expect(p.onOpenChange).not.toHaveBeenCalled();
	});
});
