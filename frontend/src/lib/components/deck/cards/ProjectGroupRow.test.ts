/** The Group row in a project's Settings: pick, create, leave and (separately, confirmed) delete a group. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { Writable } from 'svelte/store';
import ProjectGroupRow from './ProjectGroupRow.svelte';
import { groups as groupsStore } from '$lib/stores/groups.svelte';
vi.mock('$lib/stores/groups.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { groups: Object.assign(writable({ projects: { groups: [], assignments: {} } }), { createGroup: vi.fn(), assignToGroup: vi.fn(), deleteGroup: vi.fn(), removeFromGroup: vi.fn() }) };
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const store = groupsStore as unknown as Writable<any>;
const select = () => screen.getByRole('combobox', { name: 'Group for Demo' }) as HTMLSelectElement;

describe('Project group row', () => {
	it('says "No group" and creates a group in the row, with Escape and Enter', async () => {
		store.set({ projects: { groups: [], assignments: {} } });
		render(ProjectGroupRow, { projectId: 'project', projectName: 'Demo' });
		expect(select().value).toBe('');
		expect(select().selectedOptions[0].textContent).toBe('No group');

		await fireEvent.click(screen.getByRole('button', { name: 'New group' }));
		const input = screen.getByRole('textbox', { name: 'Group name' });
		await waitFor(() => expect(document.activeElement).toBe(input));
		expect((screen.getByRole('button', { name: 'Create group' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.keyDown(input, { key: 'Escape' });
		expect(screen.queryByRole('textbox', { name: 'Group name' })).toBeNull();
		await waitFor(() => expect(document.activeElement).toBe(screen.getByRole('button', { name: 'New group' })));

		await fireEvent.click(screen.getByRole('button', { name: 'New group' }));
		const second = screen.getByRole('textbox', { name: 'Group name' });
		await fireEvent.input(second, { target: { value: 'Tools' } });
		await fireEvent.keyDown(second, { key: 'Enter' });
		expect(groupsStore.createGroup).toHaveBeenCalledWith('projects', 'Tools');
		expect(groupsStore.assignToGroup).toHaveBeenCalledWith('projects', 'project', 'Tools');
		expect(screen.queryByRole('textbox', { name: 'Group name' })).toBeNull();
	});

	it('joins an existing group by name without creating it again', async () => {
		store.set({ projects: { groups: [{ name: 'Work', collapsed: false }], assignments: {} } });
		render(ProjectGroupRow, { projectId: 'project', projectName: 'Demo' });

		await fireEvent.change(select(), { target: { value: 'Work' } });
		expect(groupsStore.assignToGroup).toHaveBeenCalledWith('projects', 'project', 'Work');

		await fireEvent.click(screen.getByRole('button', { name: 'New group' }));
		await fireEvent.input(screen.getByRole('textbox', { name: 'Group name' }), { target: { value: ' Work ' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Create group' }));
		expect(groupsStore.createGroup).not.toHaveBeenCalled();
		expect(groupsStore.assignToGroup).toHaveBeenLastCalledWith('projects', 'project', 'Work');
	});

	it('takes only this project out of its group with "No group", never deleting the group', async () => {
		store.set({ projects: { groups: [{ name: 'Work', collapsed: false }], assignments: { project: 'Work', other: 'Work' } } });
		render(ProjectGroupRow, { projectId: 'project', projectName: 'Demo' });
		expect(select().value).toBe('Work');

		await fireEvent.change(select(), { target: { value: '' } });
		expect(groupsStore.removeFromGroup).toHaveBeenCalledWith('projects', 'project');
		expect(groupsStore.deleteGroup).not.toHaveBeenCalled();
	});

	it('asks before deleting a group, naming it and how many projects become ungrouped', async () => {
		// The assignments are synced: 'on-peer' is listed only on another worker,
		// and deleting the group clears it too, so it counts.
		store.set({ projects: { groups: [{ name: 'Work', collapsed: true }], assignments: { project: 'Work', other: 'Work', third: 'Home', 'on-peer': 'Work' } } });
		render(ProjectGroupRow, { projectId: 'project', projectName: 'Demo' });

		await fireEvent.click(screen.getByRole('button', { name: 'Delete group Work' }));
		expect(groupsStore.deleteGroup).not.toHaveBeenCalled();
		const dialog = await screen.findByRole('dialog');
		expect(dialog.textContent).toContain('Delete the group "Work"? 3 projects become ungrouped.');
		expect(dialog.textContent).toContain('To take only Demo out, choose No group instead.');
		await fireEvent.click(within(dialog).getAllByRole('button', { name: 'Cancel' }).at(-1)!);
		await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
		expect(groupsStore.deleteGroup).not.toHaveBeenCalled();

		await fireEvent.click(screen.getByRole('button', { name: 'Delete group Work' }));
		await fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Delete group' }));
		expect(groupsStore.deleteGroup).toHaveBeenCalledWith('projects', 'Work');
		expect(groupsStore.removeFromGroup).not.toHaveBeenCalled();
	});

	it('offers no delete while the project has no group', () => {
		store.set({ projects: { groups: [{ name: 'Work', collapsed: false }], assignments: {} } });
		render(ProjectGroupRow, { projectId: 'project', projectName: 'Demo' });
		expect(screen.queryByRole('button', { name: /Delete group/ })).toBeNull();
	});
});
