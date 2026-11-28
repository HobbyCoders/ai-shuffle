/**
 * The Settings tab with the real "Set up Git" dialog on top of it: Escape
 * closes Set up Git only, focus goes back to "Initialize Git repository" and
 * the tab keeps its name draft.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
vi.mock('$lib/stores/auth.svelte', async () => ({ isAdmin: (await import('svelte/store')).writable(true) }));
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { projects: writable([{ id: 'demo', name: 'Demo', path: '/workspace/example/demo' }]), appData: { updateProject: vi.fn(), deleteProject: vi.fn() } };
});
vi.mock('$lib/api/git', () => ({ getGitStatus: vi.fn().mockResolvedValue({ is_git_repo: false }), initRepository: vi.fn(), cloneRepository: vi.fn() }));
vi.mock('$lib/api/github', () => ({ listGitHubRepos: vi.fn().mockResolvedValue({ connected: false, items: [], total: 0, page: 1, per_page: 30, has_more: false, truncated: false }) }));
vi.mock('$lib/stores/git.svelte', () => ({ git: { refreshStatus: vi.fn() } }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { openFeaturePage: vi.fn() } }));
vi.mock('$lib/api/project-workers', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/project-workers')>()),
	forgetProject: vi.fn(),
	getProjectRemovalPreview: vi.fn()
}));
import ProjectSettingsTab from './ProjectSettingsTab.svelte';

afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('Project settings with Set up Git open', () => {
	it('closes only Set up Git on Escape and keeps the tab and its name draft', async () => {
		render(ProjectSettingsTab, {
			projectName: 'Demo', localProjectId: 'demo', projectRef: { project_v2_id: 'pv2-demo' }, kind: 'folder' as const, isGit: false, remote: null,
			selfName: 'Home server', orphaned: false, onRemoved: vi.fn()
		});
		const tab = document.querySelector('[data-ui-project-settings]') as HTMLElement;
		const name = within(tab).getByPlaceholderText('Project name') as HTMLInputElement;
		await fireEvent.input(name, { target: { value: 'Unsaved name' } });

		const init = await within(tab).findByRole('button', { name: 'Initialize Git repository' });
		init.focus();
		await fireEvent.click(init);
		const gitDialog = await screen.findByRole('dialog', { name: 'Set up Git' });
		const inside = (gitDialog.querySelector('input, button') as HTMLElement);
		inside.focus();

		await fireEvent.keyDown(inside, { key: 'Escape' });

		await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Set up Git' })).toBeNull());
		expect((screen.getByPlaceholderText('Project name') as HTMLInputElement).value).toBe('Unsaved name');
		await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Initialize Git repository' })));
	});
});
