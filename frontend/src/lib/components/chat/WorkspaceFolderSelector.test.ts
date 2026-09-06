import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { get } from 'svelte/store';
import WorkspaceFolderSelector from './WorkspaceFolderSelector.svelte';
import { activeSession } from '$lib/stores/active-session';
import { openDropdown, openHeaderDropdown } from '$lib/stores/header-dropdowns';

vi.mock('$lib/stores/active-session', async () => {
	const { writable, get } = await import('svelte/store');
	const state = writable({ sessionId: null, project: 'main', projectV2Id: null, workspaceProjectIds: [] });
	return { activeSession: { ...state, setWorkspaceProjectIds: vi.fn(async (ids: string[]) => state.set({ ...get(state), workspaceProjectIds: ids } as any)) } };
});
vi.mock('$lib/stores/app-data.svelte', async () => {
	const { writable } = await import('svelte/store');
	return { projects: writable([
		{ id: 'main', name: 'Main project', path: '/work/main' },
		{ id: 'docs', name: 'Docs', path: '/work/docs' },
		{ id: 'api', name: 'API', path: '/work/api' }
	]), appData: {} };
});
beforeEach(() => {
	(activeSession as any).set({ sessionId: null, project: 'main', projectV2Id: null, workspaceProjectIds: [] });
	openHeaderDropdown.set(null);
	vi.mocked(activeSession.setWorkspaceProjectIds).mockClear();
});
afterEach(cleanup);

it('adds and removes folders while keeping the main project in the chip', async () => {
	render(WorkspaceFolderSelector, { projectName: 'Main project' });
	const trigger = screen.getByRole('button', { name: 'Workspace folders: Main project' });
	await fireEvent.click(trigger);
	expect(screen.getByText('Main')).toBeTruthy();
	expect(screen.queryByRole('button', { name: /Remove Main project/ })).toBeNull();
	await fireEvent.click(screen.getByRole('button', { name: 'Add project' }));
	await fireEvent.input(screen.getByRole('textbox', { name: 'Search projects' }), { target: { value: 'docs' } });
	expect(screen.queryByRole('button', { name: /API/ })).toBeNull();
	await fireEvent.click(screen.getByRole('button', { name: /Docs/ }));
	await waitFor(() => expect(screen.getByRole('button', { name: 'Remove Docs from workspace' })).toBeTruthy());
	expect(trigger.textContent).toContain('Main project');
	expect(trigger.textContent).toContain('+1');
	await fireEvent.click(screen.getByRole('button', { name: 'Add project' }));
	expect(screen.queryByRole('button', { name: /^Docs/ })).toBeNull();
	await fireEvent.click(screen.getByRole('button', { name: 'Remove Docs from workspace' }));
	await waitFor(() => expect(get(activeSession).workspaceProjectIds).toEqual([]));
	expect(trigger.textContent).not.toContain('+1');
});

it('retains folders and shows the error if saving fails', async () => {
	(activeSession as any).set({ sessionId: 'chat-a', project: 'main', workspaceProjectIds: ['docs'] });
	vi.mocked(activeSession.setWorkspaceProjectIds).mockRejectedValueOnce({ detail: 'Could not save folders' });
	render(WorkspaceFolderSelector, { projectName: 'Main project' });
	await fireEvent.click(screen.getByRole('button', { name: 'Workspace folders: Main project' }));
	await fireEvent.click(screen.getByRole('button', { name: 'Remove Docs from workspace' }));
	expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Could not save folders');
	expect(get(activeSession).workspaceProjectIds).toEqual(['docs']);
});

it('coordinates menus, closes on Escape and outside click, and resets on chat switch', async () => {
	render(WorkspaceFolderSelector, { projectName: 'Main project' });
	const trigger = screen.getByRole('button', { name: 'Workspace folders: Main project' });
	await fireEvent.click(trigger);
	await fireEvent.keyDown(window, { key: 'Escape' });
	expect(screen.queryByRole('dialog')).toBeNull();
	expect(document.activeElement).toBe(trigger);
	await fireEvent.click(trigger);
	openDropdown('profile');
	await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
	await fireEvent.click(trigger);
	await fireEvent.click(document.body);
	expect(screen.queryByRole('dialog')).toBeNull();
	await fireEvent.click(trigger);
	(activeSession as any).set({ sessionId: 'chat-b', project: 'main', workspaceProjectIds: [] });
	await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
});

it('opens only the mobile menu when both composer surfaces are mounted', async () => {
	render(WorkspaceFolderSelector, { projectName: 'Main project' });
	render(WorkspaceFolderSelector, { projectName: 'Main project', sheetMode: true });
	await fireEvent.click(screen.getAllByRole('button', { name: 'Workspace folders: Main project' })[1]);
	expect(screen.getAllByRole('dialog')).toHaveLength(1);
	expect(get(openHeaderDropdown)).toBe('sheet-workspace-folders');
});
