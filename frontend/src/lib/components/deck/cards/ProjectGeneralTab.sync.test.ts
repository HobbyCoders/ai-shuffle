import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { tick } from 'svelte';
import ProjectGeneralTab from './ProjectGeneralTab.svelte';
import { appData, projects } from '$lib/stores/app-data.svelte';
import type { Writable } from 'svelte/store';
import { groups as groupsStore } from '$lib/stores/groups.svelte';
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { projects: writable([]), appData: { updateProject: vi.fn() } };
});
vi.mock('$lib/stores/groups.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { groups: Object.assign(writable({ projects: { groups: [], assignments: {} } }), { createGroup: vi.fn(), assignToGroup: vi.fn() }) };
});
vi.mock('$lib/api/git', () => ({ getGitStatus: vi.fn().mockResolvedValue({ is_git_repo: true }) }));
vi.mock('$lib/stores/git.svelte', () => ({ git: { refreshStatus: vi.fn() } }));
afterEach(cleanup);
const list = projects as unknown as Writable<any[]>;
describe('project general remote fields', () => {
	it('focuses the New group field and preserves Escape and Enter', async () => {
		list.set([{id:'project',name:'Original',path:'/old'}]);
		render(ProjectGeneralTab, {projectId:'project'});
		await fireEvent.click(screen.getByRole('button', {name:'New group'}));
		const input = screen.getByRole('textbox', {name:'Group name'});
		await waitFor(() => expect(document.activeElement).toBe(input));
		await fireEvent.keyDown(input, {key:'Escape'});
		expect(screen.queryByRole('textbox', {name:'Group name'})).toBeNull();
		await fireEvent.click(screen.getByRole('button', {name:'New group'}));
		const second = screen.getByRole('textbox', {name:'Group name'});
		await fireEvent.input(second, {target:{value:'Tools'}});
		await fireEvent.keyDown(second, {key:'Enter'});
		expect(groupsStore.createGroup).toHaveBeenCalledWith('projects','Tools');
		expect(groupsStore.assignToGroup).toHaveBeenCalledWith('projects','project','Tools');
	});
	it('preserves Enter-to-save through the Settings field wrapper', async () => {
		list.set([{id:'project',name:'Original',path:'/old'}]);
		render(ProjectGeneralTab, {projectId:'project'});
		const name = screen.getByPlaceholderText('Project name');
		await fireEvent.input(name, {target:{value:'Renamed'}});
		await fireEvent.keyDown(name, {key:'Enter'});
		expect(appData.updateProject).toHaveBeenCalledWith('project', {name:'Renamed'});
		const path = screen.getByPlaceholderText('Project path');
		await fireEvent.input(path, {target:{value:'/new'}});
		await fireEvent.keyDown(path, {key:'Enter'});
		expect(appData.updateProject).toHaveBeenCalledWith('project', {path:'/new'});
	});
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
