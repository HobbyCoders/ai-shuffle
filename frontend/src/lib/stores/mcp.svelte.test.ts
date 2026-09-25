/**
 * MCP store — global/project server lists, scoped mutations, error surfacing
 * and the stale-response guard for project loads.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { McpServerInfo, McpServersResponse } from '$lib/api/mcp';

const api = vi.hoisted(() => ({
	getGlobalMcpServers: vi.fn(),
	addGlobalMcpServer: vi.fn(),
	updateGlobalMcpServer: vi.fn(),
	removeGlobalMcpServer: vi.fn(),
	getProjectMcpServers: vi.fn(),
	toggleProjectMcpServer: vi.fn(),
	addProjectMcpServer: vi.fn(),
	updateProjectMcpServer: vi.fn(),
	removeProjectMcpServer: vi.fn()
}));

vi.mock('$lib/api/mcp', () => api);

import { mcp, mcpDisplayStats, mcpError, mcpLoading, projectMcpServers } from './mcp.svelte';

function server(name: string, scope: McpServerInfo['scope'] = 'user', enabled = true): McpServerInfo {
	return { name, type: 'stdio', command: 'npx', args: [name], scope, enabled };
}

function response(parts: Partial<McpServersResponse> = {}): McpServersResponse {
	return { user: [], installed: [], local: [], project: [], total_count: 0, ...parts };
}

function deferred<T>() {
	let resolve!: (v: T) => void;
	const promise = new Promise<T>((res) => { resolve = res; });
	return { promise, resolve };
}

beforeEach(() => {
	for (const fn of Object.values(api)) fn.mockReset();
	mcp.reset();
});

describe('mcp store — global servers', () => {
	it('loads, adds, updates and removes global servers', async () => {
		api.getGlobalMcpServers.mockResolvedValue([server('a')]);
		await mcp.loadGlobalServers();
		api.addGlobalMcpServer.mockResolvedValue(server('b'));
		await mcp.addGlobalServer({ name: 'b' } as never);
		api.updateGlobalMcpServer.mockResolvedValue(server('a', 'user', false));
		await mcp.updateGlobalServer('a', { enabled: false } as never);
		expect(mcp.current.globalServers.map((s) => [s.name, s.enabled])).toEqual([['a', false], ['b', true]]);
		api.removeGlobalMcpServer.mockResolvedValue(undefined);
		await mcp.removeGlobalServer('a');
		expect(mcp.current.globalServers.map((s) => s.name)).toEqual(['b']);
		expect(mcpDisplayStats.current.globalCount).toBe(1);
		expect(mcpLoading.current).toBe(false);
	});

	it("shows the backend's reason on failure and rethrows", async () => {
		const refusal = { status: 409, detail: "MCP server 'a' already exists" };
		api.addGlobalMcpServer.mockRejectedValue(refusal);
		await expect(mcp.addGlobalServer({ name: 'a' } as never)).rejects.toBe(refusal);
		expect(mcpError.current).toBe("MCP server 'a' already exists");
		expect(mcpLoading.current).toBe(false);
		mcp.clearError();
		expect(mcpError.current).toBeNull();
	});
});

describe('mcp store — project servers', () => {
	it('loads every scope and defaults a missing installed list', async () => {
		api.getProjectMcpServers.mockResolvedValue({ ...response({ local: [server('l', 'local')] }), installed: undefined });
		await mcp.loadProjectServers('P1');
		expect(mcp.current.currentProjectId).toBe('P1');
		expect(projectMcpServers.current).toEqual({ user: [], installed: [], local: [server('l', 'local')], project: [] });
	});

	it("keeps the newer project's servers when an older load resolves last", async () => {
		const slow = deferred<McpServersResponse>();
		api.getProjectMcpServers.mockReturnValueOnce(slow.promise)
			.mockResolvedValueOnce(response({ project: [server('new', 'project')] }));
		const first = mcp.loadProjectServers('OLD');
		await mcp.loadProjectServers('NEW');
		slow.resolve(response({ project: [server('old', 'project')] }));
		await first;
		expect(mcp.current.currentProjectId).toBe('NEW');
		expect(mcp.current.projectServers.project.map((s) => s.name)).toEqual(['new']);
	});

	it('adds and updates within the given scope only', async () => {
		api.getProjectMcpServers.mockResolvedValue(response({ local: [server('l', 'local')] }));
		await mcp.loadProjectServers('P1');
		api.addProjectMcpServer.mockResolvedValue(server('p', 'project'));
		await mcp.addProjectServer('P1', { name: 'p', scope: 'project' } as never);
		api.updateProjectMcpServer.mockResolvedValue(server('l', 'local', false));
		await mcp.updateProjectServer('P1', 'l', { enabled: false } as never, 'local');
		expect(api.updateProjectMcpServer).toHaveBeenCalledWith('P1', 'l', { enabled: false }, 'local');
		expect(mcp.current.projectServers.project.map((s) => s.name)).toEqual(['p']);
		expect(mcp.current.projectServers.local[0].enabled).toBe(false);
	});

	it('removing a user-scope server drops it globally and reloads the project', async () => {
		api.getGlobalMcpServers.mockResolvedValue([server('g')]);
		await mcp.loadGlobalServers();
		api.getProjectMcpServers.mockResolvedValue(response());
		api.removeProjectMcpServer.mockResolvedValue(undefined);
		await mcp.removeProjectServer('P1', 'g', 'user');
		expect(mcp.current.globalServers).toEqual([]);
		expect(api.getProjectMcpServers).toHaveBeenCalledWith('P1');
		expect(mcpLoading.current).toBe(false);
	});

	it('removing a scoped server filters only that scope; failures keep the list', async () => {
		api.getProjectMcpServers.mockResolvedValue(response({ local: [server('x', 'local')], project: [server('x', 'project')] }));
		await mcp.loadProjectServers('P1');
		api.removeProjectMcpServer.mockResolvedValueOnce(undefined);
		await mcp.removeProjectServer('P1', 'x', 'local');
		expect(mcp.current.projectServers.local).toEqual([]);
		expect(mcp.current.projectServers.project.map((s) => s.name)).toEqual(['x']);

		api.removeProjectMcpServer.mockRejectedValueOnce({ status: 403, detail: 'Project config is read-only' });
		await expect(mcp.removeProjectServer('P1', 'x', 'project')).rejects.toMatchObject({ status: 403 });
		expect(mcp.current.projectServers.project.map((s) => s.name)).toEqual(['x']);
		expect(mcpError.current).toBe('Project config is read-only');
		expect(mcpLoading.current).toBe(false);
	});

	it('toggle reloads the project; a failed toggle records the error and rethrows', async () => {
		api.toggleProjectMcpServer.mockResolvedValueOnce(undefined);
		api.getProjectMcpServers.mockResolvedValue(response({ local: [server('l', 'local', false)] }));
		await mcp.toggleProjectServer('P1', 'l', false);
		expect(api.toggleProjectMcpServer).toHaveBeenCalledWith('P1', 'l', false);
		expect(mcp.current.projectServers.local[0].enabled).toBe(false);

		api.toggleProjectMcpServer.mockRejectedValueOnce({ status: 404, detail: 'Unknown server' });
		await expect(mcp.toggleProjectServer('P1', 'nope', true)).rejects.toMatchObject({ status: 404 });
		expect(mcpError.current).toBe('Unknown server');
	});
});
