/**
 * Project → MCP tab: servers grouped by scope, per-project enable/disable,
 * confirm-before-remove, add/edit payloads through the server form, failures
 * shown in a banner, and per-project state reset when the project changes.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { McpServerInfo } from '$lib/api/mcp';

const api = vi.hoisted(() => ({
	getProjectMcpServers: vi.fn(), toggleProjectMcpServer: vi.fn(), addProjectMcpServer: vi.fn(),
	updateProjectMcpServer: vi.fn(), removeProjectMcpServer: vi.fn()
}));
vi.mock('$lib/api/mcp', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/mcp')>()),
	...api
}));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));

import { mcp } from '$lib/stores/mcp.svelte';
import ProjectMcpTab from './ProjectMcpTab.svelte';

const s = (name: string, scope: McpServerInfo['scope'], over: Partial<McpServerInfo> = {}): McpServerInfo => ({
	name, scope, type: 'stdio', command: 'npx', args: [name], enabled: true, ...over
});
const servers = () => ({
	user: [s('github', 'user'), s('sdk', 'user', { builtin: true, command: undefined, args: undefined })],
	installed: [s('sentry', 'installed', { enabled: false })],
	local: [s('scratch', 'local')],
	project: [s('db', 'project', { type: 'http', command: undefined, args: undefined, url: 'https://db.example.com/mcp' })],
	total_count: 5
});

beforeEach(() => {
	vi.clearAllMocks();
	mcp.reset();
	api.getProjectMcpServers.mockResolvedValue(servers());
	api.toggleProjectMcpServer.mockResolvedValue(undefined);
	api.removeProjectMcpServer.mockResolvedValue(undefined);
	api.addProjectMcpServer.mockImplementation(async (_p, req) => ({ ...req, enabled: true }));
	api.updateProjectMcpServer.mockImplementation(async (_p, name, req, scope) => ({ ...req, name, scope, enabled: true }));
});
afterEach(() => cleanup());

async function mount(projectId = 'p1') {
	const view = render(ProjectMcpTab, { props: { projectId } });
	await screen.findByText('Global MCP Servers');
	return view;
}

describe('ProjectMcpTab', () => {
	it('groups servers by scope and filters by search', async () => {
		await mount();
		expect(api.getProjectMcpServers).toHaveBeenCalledWith('p1');
		for (const h of ['Installed MCP Servers', 'Local MCP Servers', 'Project MCP Servers']) expect(screen.getByText(h)).toBeTruthy();
		expect(screen.getByText('https://db.example.com/mcp')).toBeTruthy();
		// Built-in global servers can be toggled but not removed.
		expect(screen.getByRole('switch', { name: 'Disable sdk' })).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Remove sdk' })).toBeNull();

		await fireEvent.input(screen.getByPlaceholderText('Search MCP servers…'), { target: { value: 'db.example' } });
		expect(screen.queryByText('Global MCP Servers')).toBeNull();
		expect(screen.getByText('Project MCP Servers')).toBeTruthy();
	});

	it('shows the empty state when the project has no servers', async () => {
		api.getProjectMcpServers.mockResolvedValue({ user: [], installed: [], local: [], project: [], total_count: 0 });
		render(ProjectMcpTab, { props: { projectId: 'p1' } });
		expect(await screen.findByText('No MCP servers available')).toBeTruthy();
	});

	it('shows a load failure instead of the empty state', async () => {
		api.getProjectMcpServers.mockRejectedValue({ detail: 'project path missing' });
		render(ProjectMcpTab, { props: { projectId: 'p1' } });
		expect(await screen.findByText('project path missing')).toBeTruthy();
		expect(screen.queryByText('No MCP servers available')).toBeNull();
	});

	it('toggles a server for this project and reloads', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('switch', { name: 'Enable sentry' }));
		await waitFor(() => expect(api.toggleProjectMcpServer).toHaveBeenCalledWith('p1', 'sentry', true));
		await waitFor(() => expect(api.getProjectMcpServers).toHaveBeenCalledTimes(2));
	});

	it('shows a failed toggle', async () => {
		api.toggleProjectMcpServer.mockRejectedValue({ detail: 'config file read-only' });
		const err = vi.spyOn(console, 'error').mockImplementation(() => {});
		await mount();
		await fireEvent.click(screen.getByRole('switch', { name: 'Disable github' }));
		expect(await screen.findByText('config file read-only')).toBeTruthy();
		err.mockRestore();
	});

	it('asks before removing, warning that a global server goes everywhere', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Remove github' }));
		const dialog = screen.getByRole('dialog');
		expect(within(dialog).getByText(/global server, so every project loses it/)).toBeTruthy();
		await fireEvent.click(within(dialog).getAllByRole('button', { name: 'Cancel' })[0]);
		expect(api.removeProjectMcpServer).not.toHaveBeenCalled();

		await fireEvent.click(screen.getByRole('button', { name: 'Remove sentry' }));
		expect(within(screen.getByRole('dialog')).getByText('Remove "sentry" from this project?')).toBeTruthy();
		await fireEvent.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Remove' }));
		await waitFor(() => expect(api.removeProjectMcpServer).toHaveBeenCalledWith('p1', 'sentry', 'installed'));
		await waitFor(() => expect(screen.queryByText('Installed MCP Servers')).toBeNull());
	});

	it('adds a project-scoped server through the form', async () => {
		const view = await mount();
		(view.component as unknown as { openAddForm: () => void }).openAddForm();
		const form = await screen.findByRole('dialog');
		await fireEvent.input(within(form).getByPlaceholderText('my-mcp-server'), { target: { value: 'notes' } });
		await fireEvent.input(within(form).getByPlaceholderText('node, python, npx, etc.'), { target: { value: 'node' } });
		await fireEvent.click(within(form).getByRole('button', { name: 'Project (committed)' }));
		await fireEvent.click(within(form).getByRole('button', { name: 'Add Server' }));
		await waitFor(() => expect(api.addProjectMcpServer).toHaveBeenCalledWith('p1', expect.objectContaining({ name: 'notes', command: 'node', scope: 'project' })));
		await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
	});

	it('edits a local server in its own scope', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Edit scratch' }));
		const form = await screen.findByRole('dialog');
		await fireEvent.input(within(form).getByPlaceholderText('./server.js --port 3000'), { target: { value: 'scratch --verbose' } });
		await fireEvent.click(within(form).getByRole('button', { name: 'Save Changes' }));
		await waitFor(() => expect(api.updateProjectMcpServer).toHaveBeenCalledWith(
			'p1', 'scratch', expect.objectContaining({ args: ['scratch', '--verbose'] }), 'local'
		));
	});

	it('closes an open editor and clears the search when the project changes', async () => {
		const view = await mount();
		await fireEvent.input(screen.getByPlaceholderText('Search MCP servers…'), { target: { value: 'scr' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Edit scratch' }));
		expect(await screen.findByRole('dialog')).toBeTruthy();

		await view.rerender({ projectId: 'p2' });
		await waitFor(() => expect(api.getProjectMcpServers).toHaveBeenLastCalledWith('p2'));
		expect(screen.queryByRole('dialog')).toBeNull();
		expect((screen.getByPlaceholderText('Search MCP servers…') as HTMLInputElement).value).toBe('');
		expect(api.updateProjectMcpServer).not.toHaveBeenCalled();
	});

	// R13-5: the list itself belongs to the project too.
	function deferred<T>() {
		let resolve!: (v: T) => void;
		let reject!: (e: unknown) => void;
		const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
		return { promise, resolve, reject };
	}
	const p2Servers = () => ({
		user: [s('github', 'user')], installed: [], local: [s('p2-only', 'local')], project: [], total_count: 2
	});

	it('hides the previous project\'s servers while the new project loads', async () => {
		const view = await mount();
		const p2 = deferred<ReturnType<typeof p2Servers>>();
		api.getProjectMcpServers.mockReturnValueOnce(p2.promise);
		await view.rerender({ projectId: 'p2' });
		await waitFor(() => expect(api.getProjectMcpServers).toHaveBeenLastCalledWith('p2'));
		// p1's rows are gone, so nothing can act on p2 with p1's names.
		expect(screen.queryByRole('switch', { name: 'Enable sentry' })).toBeNull();
		expect(screen.queryByRole('button', { name: 'Edit scratch' })).toBeNull();
		expect(screen.queryByText('No MCP servers available')).toBeNull();
		p2.resolve(p2Servers());
		expect(await screen.findByRole('button', { name: 'Edit p2-only' })).toBeTruthy();
		expect(screen.queryByText('Installed MCP Servers')).toBeNull();
	});

	it('does not keep showing the previous project when the new load fails', async () => {
		const view = await mount();
		api.getProjectMcpServers.mockRejectedValueOnce({ detail: 'project path missing' });
		await view.rerender({ projectId: 'p2' });
		expect(await screen.findByText('project path missing')).toBeTruthy();
		expect(screen.queryByText('Global MCP Servers')).toBeNull();
		expect(screen.queryByRole('switch', { name: 'Enable sentry' })).toBeNull();
	});

	it('ignores a late response for a project the user already left', async () => {
		const p1 = deferred<ReturnType<typeof servers>>();
		api.getProjectMcpServers.mockReturnValueOnce(p1.promise).mockResolvedValueOnce(p2Servers());
		const view = render(ProjectMcpTab, { props: { projectId: 'p1' } });
		await view.rerender({ projectId: 'p2' });
		expect(await screen.findByRole('button', { name: 'Edit p2-only' })).toBeTruthy();
		p1.resolve(servers());
		await new Promise((r) => setTimeout(r, 0));
		expect(screen.getByRole('button', { name: 'Edit p2-only' })).toBeTruthy();
		expect(screen.queryByRole('switch', { name: 'Enable sentry' })).toBeNull();
	});

	it('shows the current project again after a toggle for the previous one lands', async () => {
		const view = await mount();
		const toggle = deferred<undefined>();
		api.toggleProjectMcpServer.mockReturnValueOnce(toggle.promise);
		await fireEvent.click(screen.getByRole('switch', { name: 'Enable sentry' }));
		api.getProjectMcpServers.mockImplementation(async (id: string) => (id === 'p2' ? p2Servers() : servers()));
		await view.rerender({ projectId: 'p2' });
		expect(await screen.findByRole('button', { name: 'Edit p2-only' })).toBeTruthy();
		const before = api.getProjectMcpServers.mock.calls.length;
		toggle.resolve(undefined); // the store then reloads p1, and the tab p2
		await waitFor(() => expect(api.getProjectMcpServers.mock.calls.slice(before)).toEqual([['p1'], ['p2']]));
		expect(api.toggleProjectMcpServer).toHaveBeenCalledWith('p1', 'sentry', true);
		expect(await screen.findByRole('button', { name: 'Edit p2-only' })).toBeTruthy();
		expect(screen.queryByText('Installed MCP Servers')).toBeNull();
	});
});
