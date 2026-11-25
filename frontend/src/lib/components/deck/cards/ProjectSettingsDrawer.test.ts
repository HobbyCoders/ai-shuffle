/** Project settings drawer: identity, and the project's one danger zone. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
vi.mock('$lib/stores/auth.svelte', async () => ({ isAdmin: (await import('svelte/store')).writable(true) }));
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { projects: writable([{ id: 'demo', name: 'Demo', path: '/workspace/example/demo' }]), appData: { updateProject: vi.fn(), deleteProject: vi.fn() } };
});
vi.mock('$lib/api/git', () => ({ getGitStatus: vi.fn().mockResolvedValue({ is_git_repo: true }) }));
vi.mock('$lib/stores/git.svelte', () => ({ git: { refreshStatus: vi.fn() } }));
vi.mock('$lib/components/git/InitRepoModal.svelte', () => import('$lib/test-stubs/EmptyRouteDependency.svelte'));
vi.mock('$lib/api/project-workers', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/project-workers')>()),
	forgetProject: vi.fn(),
	getProjectRemovalPreview: vi.fn()
}));
import type { Writable } from 'svelte/store';
import ProjectSettingsDrawer from './ProjectSettingsDrawer.svelte';
import { forgetProject } from '$lib/api/project-workers';
import { appData } from '$lib/stores/app-data.svelte';

const ref = { project_v2_id: 'pv2-demo' };
const base = { projectName: 'Demo', projectRef: ref, kind: 'git' as const, selfName: 'Home server', onClose: vi.fn() };
afterEach(() => { cleanup(); vi.clearAllMocks(); });

describe('Project settings drawer', () => {
	it('has one danger zone with this worker’s removal, and no raw path box', async () => {
		render(ProjectSettingsDrawer, { ...base, localProjectId: 'demo', orphaned: false, onRemoved: vi.fn() });
		const drawer = screen.getByRole('dialog', { name: 'Project settings' });
		expect(within(drawer).getAllByRole('heading', { name: 'Danger zone' })).toHaveLength(1);
		expect(within(drawer).getByRole('button', { name: 'Remove project and chats' })).toBeTruthy();
		expect(within(drawer).queryByPlaceholderText('Project path')).toBeNull();
		expect((within(drawer).getByRole('button', { name: 'Forget project' }) as HTMLButtonElement).disabled).toBe(true);
	});

	it('forgets a project no worker holds, then hands it back', async () => {
		vi.mocked(forgetProject).mockResolvedValue({ ok: true, project_v2_id: 'pv2-demo' });
		const onRemoved = vi.fn();
		render(ProjectSettingsDrawer, { ...base, localProjectId: null, orphaned: true, onRemoved });

		const drawer = screen.getByRole('dialog', { name: 'Project settings' });
		expect(within(drawer).queryByRole('button', { name: 'Remove project and chats' })).toBeNull();
		await fireEvent.click(within(drawer).getByRole('button', { name: 'Forget project' }));
		expect(forgetProject).toHaveBeenCalledWith(ref);
		await waitFor(() => expect(onRemoved).toHaveBeenCalledOnce());
	});

	it('sends folder changes to this worker’s row', async () => {
		const onOpenSelf = vi.fn();
		const onClose = vi.fn();
		render(ProjectSettingsDrawer, { ...base, onClose, localProjectId: null, orphaned: false, workerTable: true, onOpenSelf, onRemoved: vi.fn() });
		await fireEvent.click(screen.getByRole('button', { name: 'Open Home server' }));
		expect(onClose).toHaveBeenCalled();
		expect(onOpenSelf).toHaveBeenCalled();
	});

	it('sends this worker’s removal to its row when the worker table has one, instead of a second removal', async () => {
		const onRemoveSelf = vi.fn();
		const onClose = vi.fn();
		render(ProjectSettingsDrawer, { ...base, onClose, localProjectId: 'demo', orphaned: false, workerTable: true, onOpenSelf: vi.fn(), onRemoveSelf, onRemoved: vi.fn() });
		const drawer = screen.getByRole('dialog', { name: 'Project settings' });
		expect(within(drawer).getAllByRole('heading', { name: 'Danger zone' })).toHaveLength(1);
		expect(within(drawer).queryByRole('button', { name: 'Remove project and chats' })).toBeNull();
		await fireEvent.click(within(drawer).getByRole('button', { name: 'Remove from Home server…' }));
		expect(onClose).toHaveBeenCalled();
		expect(onRemoveSelf).toHaveBeenCalledOnce();
	});

	describe('without a worker table', () => {
		const projectsStore = async () => (await import('$lib/stores/app-data.svelte')).projects as unknown as Writable<{ id: string; name: string; path: string }[]>;
		afterEach(async () => { (await projectsStore()).set([{ id: 'demo', name: 'Demo', path: '/workspace/example/demo' }]); });

		it('changes this worker’s folder here, through the guarded local update', async () => {
			vi.mocked(appData.updateProject).mockResolvedValue(undefined as never);
			render(ProjectSettingsDrawer, { ...base, localProjectId: 'demo', orphaned: false, onRemoved: vi.fn() });
			const drawer = screen.getByRole('dialog', { name: 'Project settings' });
			expect(within(drawer).queryByRole('button', { name: 'Open Home server' })).toBeNull();
			await fireEvent.click(within(drawer).getByRole('button', { name: 'Change folder' }));
			const field = within(drawer).getByLabelText('New folder on Home server') as HTMLInputElement;
			expect(field.value).toBe('/workspace/example/demo');
			await fireEvent.input(field, { target: { value: '/workspace/example/demo-moved' } });
			await fireEvent.click(within(drawer).getByRole('button', { name: 'Save folder' }));
			expect(appData.updateProject).toHaveBeenCalledWith('demo', { path: '/workspace/example/demo-moved' });
		});

		it('refuses to save over a folder that changed elsewhere meanwhile', async () => {
			render(ProjectSettingsDrawer, { ...base, localProjectId: 'demo', orphaned: false, onRemoved: vi.fn() });
			const drawer = screen.getByRole('dialog', { name: 'Project settings' });
			await fireEvent.click(within(drawer).getByRole('button', { name: 'Change folder' }));
			await fireEvent.input(within(drawer).getByLabelText('New folder on Home server'), { target: { value: '/workspace/example/demo-moved' } });
			(await projectsStore()).set([{ id: 'demo', name: 'Demo', path: '/workspace/example/demo-elsewhere' }]);
			expect(await within(drawer).findByText(/The folder changed elsewhere/)).toBeTruthy();
			expect((within(drawer).getByRole('button', { name: 'Save folder' }) as HTMLButtonElement).disabled).toBe(true);
			expect(appData.updateProject).not.toHaveBeenCalled();
		});

		it('puts the keyboard back on the folder field after a rejected save', async () => {
			vi.mocked(appData.updateProject).mockRejectedValue({ detail: 'Folder not found.' });
			render(ProjectSettingsDrawer, { ...base, localProjectId: 'demo', orphaned: false, onRemoved: vi.fn() });
			const drawer = screen.getByRole('dialog', { name: 'Project settings' });
			await fireEvent.click(within(drawer).getByRole('button', { name: 'Change folder' }));
			const field = within(drawer).getByLabelText('New folder on Home server') as HTMLInputElement;
			await fireEvent.input(field, { target: { value: '/workspace/example/missing' } });
			const save = within(drawer).getByRole('button', { name: 'Save folder' });
			save.focus();
			await fireEvent.click(save);
			expect(await within(drawer).findByText('Folder not found.')).toBeTruthy();
			await waitFor(() => expect(document.activeElement).toBe(within(drawer).getByLabelText('New folder on Home server')));
			expect(drawer.contains(document.activeElement)).toBe(true);
		});
	});
});
