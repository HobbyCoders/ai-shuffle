/**
 * MCP manager (Build page): list states, add/edit form payloads, validation,
 * inline delete confirmation, and load failures surfacing in the banner.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import type { McpServerInfo } from '$lib/api/mcp';

const api = vi.hoisted(() => ({
	getGlobalMcpServers: vi.fn(),
	addGlobalMcpServer: vi.fn(),
	updateGlobalMcpServer: vi.fn(),
	removeGlobalMcpServer: vi.fn()
}));
vi.mock('$lib/api/mcp', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/mcp')>()),
	...api
}));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
vi.mock('$lib/stores/layout.svelte', () => ({ layout: { clearFeatureItemId: vi.fn() } }));

import { mcp } from '$lib/stores/mcp.svelte';
import McpManagerCard from './McpManagerCard.svelte';

const server = (over: Partial<McpServerInfo> = {}): McpServerInfo => ({
	name: 'files', type: 'stdio', command: 'npx', args: ['-y', 'fs-server'], scope: 'user', enabled: true, ...over
});
const field = (placeholder: string) => screen.getByPlaceholderText(placeholder) as HTMLInputElement;

beforeEach(() => {
	vi.clearAllMocks();
	mcp.reset();
	api.getGlobalMcpServers.mockResolvedValue([
		server(),
		server({ name: 'remote', type: 'http', command: undefined, args: undefined, url: 'https://mcp.example.com' }),
		server({ name: 'builtin-sdk', type: 'sdk' as McpServerInfo['type'], command: undefined, args: undefined, builtin: true })
	]);
	api.addGlobalMcpServer.mockImplementation(async (req) => ({ ...req, scope: 'user', enabled: true }));
	api.updateGlobalMcpServer.mockImplementation(async (name, req) => ({ ...req, name, scope: 'user', enabled: true }));
	api.removeGlobalMcpServer.mockResolvedValue(undefined);
});
afterEach(() => cleanup());

describe('McpManagerCard list', () => {
	it('lists servers with their command or URL and filters by search', async () => {
		render(McpManagerCard, { props: { embedded: true } });
		expect(await screen.findByText('npx -y fs-server')).toBeTruthy();
		expect(screen.getByText('https://mcp.example.com')).toBeTruthy();
		// Built-in rows are not editable and carry no delete action.
		expect(screen.getAllByRole('button', { name: /Delete/ })).toHaveLength(2);

		await fireEvent.input(field('Search servers…'), { target: { value: 'zzz' } });
		expect(screen.getByText('No servers match “zzz”.')).toBeTruthy();
		await fireEvent.input(field('Search servers…'), { target: { value: 'rem' } });
		expect(screen.queryByText('npx -y fs-server')).toBeNull();
		expect(screen.getByText('https://mcp.example.com')).toBeTruthy();
	});

	it('shows a load failure in the banner without an unhandled rejection, and dismisses it', async () => {
		api.getGlobalMcpServers.mockRejectedValue({ detail: 'worker offline' });
		render(McpManagerCard, { props: { embedded: true } });
		expect(await screen.findByText('worker offline')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
		expect(screen.queryByText('worker offline')).toBeNull();
	});

	it('asks before deleting and removes the server only on confirm', async () => {
		render(McpManagerCard, { props: { embedded: true } });
		await screen.findByText('npx -y fs-server');
		await fireEvent.click(screen.getByRole('button', { name: 'Delete files' }));
		expect(screen.getByText(/can't be undone/)).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		expect(api.removeGlobalMcpServer).not.toHaveBeenCalled();

		await fireEvent.click(screen.getByRole('button', { name: 'Delete files' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
		await waitFor(() => expect(api.removeGlobalMcpServer).toHaveBeenCalledWith('files'));
		await waitFor(() => expect(api.getGlobalMcpServers).toHaveBeenCalledTimes(2));
	});

	it('keeps the confirmation and shows the error when delete fails', async () => {
		api.removeGlobalMcpServer.mockRejectedValue({ detail: 'server busy' });
		const err = vi.spyOn(console, 'error').mockImplementation(() => {});
		render(McpManagerCard, { props: { embedded: true } });
		await screen.findByText('npx -y fs-server');
		await fireEvent.click(screen.getByRole('button', { name: 'Delete files' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
		expect(await screen.findByText('server busy')).toBeTruthy();
		expect(screen.getByText(/can't be undone/)).toBeTruthy();
		err.mockRestore();
	});
});

describe('McpManagerCard form', () => {
	it('validates required fields before calling the API', async () => {
		render(McpManagerCard, { props: { embedded: true } });
		await screen.findByText('npx -y fs-server');
		await fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Add Server' }));
		expect(screen.getByText('Server name is required')).toBeTruthy();
		expect(screen.getByText('Command is required for stdio servers')).toBeTruthy();
		await fireEvent.input(field('my-mcp-server'), { target: { value: 'bad name' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Add Server' }));
		expect(screen.getByText('Server name must be alphanumeric with hyphens/underscores only')).toBeTruthy();
		expect(api.addGlobalMcpServer).not.toHaveBeenCalled();
	});

	it('adds a remote server with trimmed URL and headers, then returns to the list', async () => {
		const onDetailChange = vi.fn();
		render(McpManagerCard, { props: { embedded: true, onDetailChange } });
		await screen.findByText('npx -y fs-server');
		await fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
		expect(onDetailChange).toHaveBeenLastCalledWith(true);

		await fireEvent.input(field('my-mcp-server'), { target: { value: ' search ' } });
		await fireEvent.change(screen.getByRole('combobox', { name: 'Transport type' }), { target: { value: 'sse' } });
		await fireEvent.input(field('https://api.example.com/mcp'), { target: { value: ' https://s.example.com/sse ' } });
		await fireEvent.click(screen.getByRole('button', { name: '+ Add header' }));
		await fireEvent.input(field('Header name'), { target: { value: ' Authorization ' } });
		await fireEvent.input(screen.getAllByPlaceholderText('Value')[0], { target: { value: 'Bearer x' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Add Server' }));

		await waitFor(() => expect(api.addGlobalMcpServer).toHaveBeenCalledOnce());
		expect(api.addGlobalMcpServer).toHaveBeenCalledWith({
			name: 'search', type: 'sse', command: undefined, args: undefined,
			url: 'https://s.example.com/sse', env: undefined,
			headers: { Authorization: 'Bearer x' }, description: undefined
		});
		expect(await screen.findByText('https://s.example.com/sse')).toBeTruthy();
		expect(onDetailChange).toHaveBeenLastCalledWith(false);
	});

	it('edits an existing server by name (keyboard activation) with split args and env', async () => {
		render(McpManagerCard, { props: { embedded: true } });
		await screen.findByText('npx -y fs-server');
		const row = screen.getByText('npx -y fs-server').closest('[role="button"]') as HTMLElement;
		await fireEvent.keyDown(row, { key: 'Enter' });

		expect(field('my-mcp-server').value).toBe('files');
		expect(field('my-mcp-server').disabled).toBe(true);
		await fireEvent.input(field('./server.js --port 3000'), { target: { value: '  -y   fs-server  --root /tmp ' } });
		await fireEvent.click(screen.getByRole('button', { name: '+ Add variable' }));
		await fireEvent.input(field('VARIABLE_NAME'), { target: { value: 'TOKEN' } });
		await fireEvent.input(field('Value'), { target: { value: 'abc' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Save' }));

		await waitFor(() => expect(api.updateGlobalMcpServer).toHaveBeenCalledOnce());
		expect(api.updateGlobalMcpServer).toHaveBeenCalledWith('files', expect.objectContaining({
			name: 'files', type: 'stdio', command: 'npx', args: ['-y', 'fs-server', '--root', '/tmp'], env: { TOKEN: 'abc' }, headers: undefined
		}));
		expect(await screen.findByText('npx -y fs-server --root /tmp')).toBeTruthy();
	});

	it('stays on the form with the worker error when saving fails', async () => {
		api.addGlobalMcpServer.mockRejectedValue({ detail: 'name already exists' });
		const err = vi.spyOn(console, 'error').mockImplementation(() => {});
		render(McpManagerCard, { props: { embedded: true } });
		await screen.findByText('npx -y fs-server');
		await fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
		await fireEvent.input(field('my-mcp-server'), { target: { value: 'files' } });
		await fireEvent.input(field('node, python, npx, etc.'), { target: { value: 'node' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Add Server' }));
		expect(await screen.findByText('name already exists')).toBeTruthy();
		expect(field('my-mcp-server').value).toBe('files');
		err.mockRestore();
	});

	it('discards the draft on Back', async () => {
		render(McpManagerCard, { props: { embedded: true } });
		await screen.findByText('npx -y fs-server');
		await fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
		await fireEvent.input(field('my-mcp-server'), { target: { value: 'draft' } });
		await fireEvent.click(screen.getByRole('button', { name: 'MCP Servers' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
		expect(field('my-mcp-server').value).toBe('');
	});
});
