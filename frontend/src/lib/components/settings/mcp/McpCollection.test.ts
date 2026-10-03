/**
 * Settings > MCP servers against a mocked MCP API: the list (custom vs
 * built-in, search), the add flow and its payload, explicit Save of an edit,
 * read-only built-ins, delete confirmation, load failure, and the model's
 * payload and "changed elsewhere" rules.
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

import { mcp } from '$lib/stores/mcp.svelte';
import { settingsNav } from '$lib/settings/navigation.svelte';
import McpCollection from './McpCollection.svelte';
import { DRAFT_ID, McpModel, configFromForm, emptyForm, formFromServer } from './mcp-model.svelte';

const server = (over: Partial<McpServerInfo> = {}): McpServerInfo => ({
	name: 'files',
	type: 'stdio',
	command: 'npx',
	args: ['-y', 'fs-server'],
	scope: 'user',
	enabled: true,
	...over
});

function servers() {
	return [
		server(),
		server({ name: 'remote', type: 'http', command: undefined, args: undefined, url: 'https://mcp.example.com' }),
		server({ name: 'builtin-sdk', type: 'sdk' as McpServerInfo['type'], command: undefined, args: undefined, builtin: true })
	];
}

async function show(item?: string) {
	settingsNav.reset();
	settingsNav.selectCategory('mcp');
	if (item) settingsNav.go({ category: 'mcp', item });
	const page = render(McpCollection);
	await screen.findByText('npx -y fs-server');
	return page;
}

const field = (placeholder: string) => screen.getByPlaceholderText(placeholder) as HTMLInputElement;
async function type(input: HTMLInputElement, value: string) {
	await fireEvent.input(input, { target: { value } });
	await fireEvent.change(input, { target: { value } });
}

beforeEach(() => {
	vi.clearAllMocks();
	mcp.reset();
	api.getGlobalMcpServers.mockResolvedValue(servers());
	api.addGlobalMcpServer.mockImplementation(async (req) => ({ ...req, scope: 'user', enabled: true }));
	api.updateGlobalMcpServer.mockImplementation(async (name, req) => ({ ...req, name, scope: 'user', enabled: true }));
	api.removeGlobalMcpServer.mockResolvedValue(undefined);
});
afterEach(() => cleanup());

describe('Settings > MCP servers list', () => {
	it('lists custom servers before built-ins and filters by search', async () => {
		await show();
		expect(screen.getByText('https://mcp.example.com')).toBeTruthy();
		expect(screen.getByText('Custom')).toBeTruthy();
		expect(screen.getByText('Built-in')).toBeTruthy();

		await fireEvent.input(screen.getByRole('searchbox'), { target: { value: 'rem' } });
		expect(screen.queryByText('npx -y fs-server')).toBeNull();
		expect(screen.getByText('https://mcp.example.com')).toBeTruthy();
		await fireEvent.input(screen.getByRole('searchbox'), { target: { value: 'zzz' } });
		expect(screen.getByText('Nothing matches.')).toBeTruthy();
	});

	it('reports a load failure and retries', async () => {
		api.getGlobalMcpServers.mockRejectedValueOnce({ detail: 'worker offline' });
		settingsNav.reset();
		settingsNav.selectCategory('mcp');
		render(McpCollection);
		expect((await screen.findByRole('alert')).textContent).toContain('Could not load MCP servers');
		await fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
		expect(await screen.findByText('npx -y fs-server')).toBeTruthy();
	});
});

describe('Settings > MCP servers editing', () => {
	it('adds a local server with the typed command and arguments', async () => {
		await show();
		await fireEvent.click(screen.getByRole('button', { name: 'Add server' }));
		await type(field('github'), 'github');
		await type(field('npx'), 'npx');
		await type(field('-y @modelcontextprotocol/server-github'), '-y @modelcontextprotocol/server-github');
		await fireEvent.click(screen.getAllByRole('button', { name: 'Add server' }).at(-1)!);
		await waitFor(() =>
			expect(api.addGlobalMcpServer).toHaveBeenCalledWith(
				expect.objectContaining({
					name: 'github',
					type: 'stdio',
					command: 'npx',
					args: ['-y', '@modelcontextprotocol/server-github']
				})
			)
		);
		await waitFor(() => expect(settingsNav.selectedItem('mcp')).toBe('github'));
	});

	it('saves an edit only when Save is pressed', async () => {
		await show('files');
		const save = () => screen.getByRole('button', { name: 'Save' }) as HTMLButtonElement;
		expect(save().disabled).toBe(true);
		await type(field('npx'), 'uvx');
		expect(api.updateGlobalMcpServer).not.toHaveBeenCalled();
		await waitFor(() => expect(save().disabled).toBe(false));
		await fireEvent.click(save());
		await waitFor(() =>
			expect(api.updateGlobalMcpServer).toHaveBeenCalledWith('files', expect.objectContaining({ command: 'uvx' }))
		);
		expect(api.updateGlobalMcpServer.mock.calls[0][1]).not.toHaveProperty('name');
	});

	it('shows built-in servers read-only', async () => {
		await show('builtin-sdk');
		expect(screen.getByText('Read-only')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Save' })).toBeNull();
	});

	it('deletes a server only after confirming', async () => {
		await show('files');
		await fireEvent.click(screen.getByRole('button', { name: 'Delete' }));
		expect(api.removeGlobalMcpServer).not.toHaveBeenCalled();
		expect(await screen.findByText('Delete files?')).toBeTruthy();
		await fireEvent.click(screen.getAllByRole('button', { name: 'Delete' }).at(-1)!);
		await waitFor(() => expect(api.removeGlobalMcpServer).toHaveBeenCalledWith('files'));
	});
});

describe('mcp-model', () => {
	it('sends only the fields of the chosen transport', () => {
		const remote = { ...emptyForm(), name: ' web ', type: 'http' as const, url: ' https://x.test ', command: 'npx', args: '-y a' };
		remote.headers = [{ key: 'Authorization', value: 'Bearer t' }, { key: ' ', value: 'dropped' }];
		expect(configFromForm(remote)).toEqual({
			name: 'web',
			type: 'http',
			command: undefined,
			args: undefined,
			url: 'https://x.test',
			env: undefined,
			headers: { Authorization: 'Bearer t' },
			description: undefined
		});
		const local = { ...emptyForm(), name: 'fs', command: 'npx', args: '', headers: [{ key: 'X', value: '1' }] };
		expect(configFromForm(local)).toMatchObject({ command: 'npx', args: undefined, url: undefined, headers: undefined });
	});

	it('round-trips a server through its form', () => {
		const s = server({ env: { TOKEN: 'abc' } });
		expect(configFromForm(formFromServer(s))).toMatchObject({ name: 'files', command: 'npx', args: ['-y', 'fs-server'], env: { TOKEN: 'abc' } });
	});

	it('keeps one draft and flags dirty forms changed or deleted elsewhere', async () => {
		const model = new McpModel();
		await model.load();
		expect(model.addDraft()).toBe(DRAFT_ID);
		model.draft!.name = 'kept';
		model.addDraft();
		expect(model.draft!.name).toBe('kept');

		model.ensure('files')!.command = 'edited';
		model.ensure('remote');
		expect(model.dirty('files')).toBe(true);
		expect(model.dirty('remote')).toBe(false);

		api.getGlobalMcpServers.mockResolvedValue([server({ command: 'changed' })]);
		await model.load();
		expect(model.remote.files).toBe('changed');
		expect(model.form('files')!.command).toBe('edited');
		// A clean form whose server vanished is simply dropped.
		expect(model.form('remote')).toBeUndefined();

		api.getGlobalMcpServers.mockResolvedValue([]);
		await model.load();
		expect(model.remote.files).toBe('deleted');
	});

	it('rejects a draft whose name is taken', async () => {
		const model = new McpModel();
		await model.load();
		model.addDraft();
		Object.assign(model.draft!, { name: 'files', command: 'npx' });
		expect(await model.save(DRAFT_ID)).toBeNull();
		expect(model.errors[DRAFT_ID]).toEqual(['A server named files already exists']);
		expect(api.addGlobalMcpServer).not.toHaveBeenCalled();
	});
});
