/** The group chip beside the project's name: pick, create, leave and (separately, confirmed) delete a group. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { Writable } from 'svelte/store';
import ProjectGroupChip from './ProjectGroupChip.svelte';
import { groups as groupsStore } from '$lib/stores/groups.svelte';
vi.mock('$lib/stores/groups.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { groups: Object.assign(writable({ projects: { groups: [], assignments: {} } }), { createGroup: vi.fn(), assignToGroup: vi.fn(), deleteGroup: vi.fn(), removeFromGroup: vi.fn() }) };
});
afterEach(() => { cleanup(); vi.clearAllMocks(); });
const store = groupsStore as unknown as Writable<any>;
const chip = () => screen.getByRole('button', { name: /^Group: / });

describe('Project group chip', () => {
	it('says "No group" and creates a group from the popover, with Escape and Enter', async () => {
		store.set({ projects: { groups: [], assignments: {} } });
		render(ProjectGroupChip, { projectId: 'project', projectName: 'Demo' });
		expect(chip().textContent).toContain('No group');

		await fireEvent.click(chip());
		await fireEvent.click(screen.getByRole('menuitem', { name: 'New group' }));
		const input = screen.getByRole('textbox', { name: 'Group name' });
		await waitFor(() => expect(document.activeElement).toBe(input));
		await fireEvent.keyDown(input, { key: 'Escape' });
		expect(screen.queryByRole('textbox', { name: 'Group name' })).toBeNull();

		await fireEvent.click(screen.getByRole('menuitem', { name: 'New group' }));
		const second = screen.getByRole('textbox', { name: 'Group name' });
		await fireEvent.input(second, { target: { value: 'Tools' } });
		await fireEvent.submit(second.closest('form')!);
		expect(groupsStore.createGroup).toHaveBeenCalledWith('projects', 'Tools');
		expect(groupsStore.assignToGroup).toHaveBeenCalledWith('projects', 'project', 'Tools');
	});

	it('takes only this project out of its group with "No group", never deleting the group', async () => {
		store.set({ projects: { groups: [{ name: 'Work', collapsed: false }], assignments: { project: 'Work', other: 'Work' } } });
		render(ProjectGroupChip, { projectId: 'project', projectName: 'Demo' });
		expect(chip().textContent).toContain('Work');

		await fireEvent.click(chip());
		expect(screen.getByRole('menuitemradio', { name: /Work/ }).getAttribute('aria-checked')).toBe('true');
		await fireEvent.click(screen.getByRole('menuitemradio', { name: 'No group' }));
		expect(groupsStore.removeFromGroup).toHaveBeenCalledWith('projects', 'project');
		expect(groupsStore.deleteGroup).not.toHaveBeenCalled();
	});

	it('asks before deleting a group, naming it and how many projects become ungrouped', async () => {
		// The assignments are synced: 'on-peer' is listed only on another worker,
		// and deleting the group clears it too, so it counts.
		store.set({ projects: { groups: [{ name: 'Work', collapsed: true }], assignments: { project: 'Work', other: 'Work', third: 'Home', 'on-peer': 'Work' } } });
		render(ProjectGroupChip, { projectId: 'project', projectName: 'Demo' });

		await fireEvent.click(chip());
		await fireEvent.click(screen.getByRole('menuitem', { name: 'Delete group "Work"…' }));
		expect(groupsStore.deleteGroup).not.toHaveBeenCalled();
		const dialog = await screen.findByRole('dialog');
		expect(dialog.textContent).toContain('Delete the group "Work"? 3 projects become ungrouped.');
		expect(dialog.textContent).toContain('To take only Demo out, choose No group instead.');
		await fireEvent.click(within(dialog).getAllByRole('button', { name: 'Cancel' }).at(-1)!);
		await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
		expect(groupsStore.deleteGroup).not.toHaveBeenCalled();

		await fireEvent.click(chip());
		await fireEvent.click(screen.getByRole('menuitem', { name: 'Delete group "Work"…' }));
		await fireEvent.click(within(await screen.findByRole('dialog')).getByRole('button', { name: 'Delete group' }));
		expect(groupsStore.deleteGroup).toHaveBeenCalledWith('projects', 'Work');
		expect(groupsStore.removeFromGroup).not.toHaveBeenCalled();
	});

	it('offers no delete while the project has no group', async () => {
		store.set({ projects: { groups: [{ name: 'Work', collapsed: false }], assignments: {} } });
		render(ProjectGroupChip, { projectId: 'project', projectName: 'Demo' });
		await fireEvent.click(chip());
		expect(screen.queryByRole('menuitem', { name: /Delete group/ })).toBeNull();
	});
});
