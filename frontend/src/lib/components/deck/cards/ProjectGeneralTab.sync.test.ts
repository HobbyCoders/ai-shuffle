import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import ProjectGeneralTab from './ProjectGeneralTab.svelte';
import { appData, projects } from '$lib/stores/app-data.svelte';
import type { Writable } from 'svelte/store';
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { projects: writable([]), appData: { updateProject: vi.fn() } };
});
vi.mock('$lib/api/git', () => ({ getGitStatus: vi.fn().mockResolvedValue({ is_git_repo: true }) }));
vi.mock('$lib/stores/git.svelte', () => ({ git: { refreshStatus: vi.fn() } }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const list = projects as unknown as Writable<any[]>;
describe('project identity fields', () => {
	it('preserves Enter-to-save through the Settings field wrapper', async () => {
		list.set([{ id: 'project', name: 'Original', path: '/workspace/example/old' }]);
		render(ProjectGeneralTab, { projectId: 'project' });
		const name = screen.getByPlaceholderText('Project name');
		await fireEvent.input(name, { target: { value: 'Renamed' } });
		await fireEvent.keyDown(name, { key: 'Enter' });
		expect(appData.updateProject).toHaveBeenCalledWith('project', { name: 'Renamed' });
	});
	it('shows the folder read-only, or sends folder changes to the worker row', async () => {
		list.set([{ id: 'project', name: 'Original', path: '/workspace/example/old' }]);
		const first = render(ProjectGeneralTab, { projectId: 'project', selfName: 'Home server' });
		expect(screen.queryByPlaceholderText('Project path')).toBeNull();
		expect(screen.getByText('/workspace/example/old')).toBeTruthy();
		first.unmount();

		const onOpenFolders = vi.fn();
		render(ProjectGeneralTab, { projectId: 'project', selfName: 'Home server', onOpenFolders });
		expect(screen.queryByText('/workspace/example/old')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Open Home server' }));
		expect(onOpenFolders).toHaveBeenCalled();
	});
	it('updates a clean name but preserves a dirty one and makes deleted-project text recoverable', async () => {
		list.set([{ id: 'project', name: 'Original', path: '/workspace/example/old' }]);
		render(ProjectGeneralTab, { projectId: 'project' });
		const name = screen.getByPlaceholderText('Project name') as HTMLInputElement;
		list.set([{ id: 'project', name: 'Remote name', path: '/workspace/example/old' }]);
		await tick();
		expect(name.value).toBe('Remote name');
		await fireEvent.input(name, { target: { value: 'Unsaved name' } });
		list.set([{ id: 'project', name: 'Remote again', path: '/workspace/example/old' }]);
		await tick();
		expect(name.value).toBe('Unsaved name');
		expect(screen.getByText(/changed elsewhere/)).toBeTruthy();
		list.set([]);
		await tick();
		expect(screen.getByDisplayValue('Unsaved name')).toBeTruthy();
		expect(screen.getByText(/deleted elsewhere/)).toBeTruthy();
	});
});
