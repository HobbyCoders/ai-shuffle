import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { tick } from 'svelte';
import ProjectGeneralTab from './ProjectGeneralTab.svelte';
import { projects } from '$lib/stores/app-data.svelte';
import type { Writable } from 'svelte/store';
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { projects: writable([]), appData: { updateProject: vi.fn() } };
});
vi.mock('$lib/stores/groups.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { groups: writable({ projects: { groups: [], assignments: {} } }) };
});
afterEach(cleanup);
const list = projects as unknown as Writable<any[]>;
describe('project general remote fields', () => {
	it('updates clean fields but preserves dirty fields and makes deleted-project text recoverable', async () => {
		list.set([{id:'project',name:'Original',path:'/old'}]);
		render(ProjectGeneralTab, {projectId:'project'});
		const name = screen.getByPlaceholderText('Project name') as HTMLInputElement;
		const path = screen.getByPlaceholderText('Project path') as HTMLInputElement;
		await fireEvent.input(name,{target:{value:'Unsaved name'}});
		list.set([{id:'project',name:'Remote name',path:'/new'}]);
		await tick();
		expect(name.value).toBe('Unsaved name');
		expect(path.value).toBe('/new');
		expect(screen.getByRole('status').textContent).toContain('changed elsewhere');
		list.set([]);
		await tick();
		expect(screen.getByDisplayValue('Unsaved name')).toBeTruthy();
		expect(screen.getByRole('status').textContent).toContain('deleted elsewhere');
	});
});
