/**
 * Hooks store — app/project list loading, CRUD, per-hook saving/testing flags,
 * error surfacing, and the stale-response guard across list reloads.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { Hook, PluginHookInfo } from '$lib/api/client';

const api = vi.hoisted(() => ({
	getAppHooks: vi.fn(),
	getPluginHooks: vi.fn(),
	getProjectHooks: vi.fn(),
	getProjectPluginHooks: vi.fn(),
	createHook: vi.fn(),
	createAppHook: vi.fn(),
	updateHook: vi.fn(),
	deleteHook: vi.fn(),
	testHook: vi.fn()
}));

vi.mock('$lib/api/client', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/api/client')>()),
	...api
}));

import { hooks, hookStats, hookSaving, hookTesting, hooksError } from './hooks.svelte';

function hook(id: string, extra: Partial<Hook> = {}): Hook {
	return {
		id, project_id: null, name: id, event: 'PreToolUse' as Hook['event'], hook_type: 'bash',
		command: 'true', timeout: 30, url: null, secret: null, enabled: true,
		tool_matcher: null, created_at: '2026-09-25T00:00:00Z', updated_at: '2026-09-25T00:00:00Z',
		last_triggered_at: null, failure_count: 0, ...extra
	};
}

function pluginHook(plugin_id: string): PluginHookInfo {
	return { plugin_id, plugin_name: plugin_id, event: 'Stop', hook_type: 'command', description: null, tool_matcher: null };
}

function deferred<T>() {
	let resolve!: (v: T) => void;
	let reject!: (e: unknown) => void;
	const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
	return { promise, resolve, reject };
}

beforeEach(() => {
	for (const fn of Object.values(api)) fn.mockReset();
	hooks.reset();
});

describe('hooks store — loading', () => {
	it('loads app hooks with the global plugin hooks', async () => {
		api.getAppHooks.mockResolvedValue([hook('a')]);
		api.getPluginHooks.mockResolvedValue([pluginHook('p')]);
		await hooks.loadAppHooks();
		expect(hooks.current.hooks.map((h) => h.id)).toEqual(['a']);
		expect(hooks.current.pluginHooks.map((h) => h.plugin_id)).toEqual(['p']);
		expect(hooks.current.loading).toBe(false);
	});

	it('loadAll combines global and project plugin hooks', async () => {
		api.getProjectHooks.mockResolvedValue([hook('a'), hook('b', { project_id: 'P1' })]);
		api.getPluginHooks.mockResolvedValue([pluginHook('g')]);
		api.getProjectPluginHooks.mockResolvedValue([pluginHook('local')]);
		await hooks.loadAll('P1');
		expect(api.getProjectHooks).toHaveBeenCalledWith('P1');
		expect(api.getProjectPluginHooks).toHaveBeenCalledWith('P1');
		expect(hooks.current.currentProjectId).toBe('P1');
		expect(hooks.current.pluginHooks.map((h) => h.plugin_id)).toEqual(['g', 'local']);
		expect(hookStats.current).toMatchObject({ totalCustom: 2, totalApp: 1, totalProject: 1, totalPlugin: 2, totalEnabled: 2 });
	});

	it('an older project load that resolves last does not replace the newer list', async () => {
		const slow = deferred<Hook[]>();
		api.getProjectHooks.mockReturnValueOnce(slow.promise).mockResolvedValueOnce([hook('new', { project_id: 'NEW' })]);
		api.getPluginHooks.mockResolvedValue([]);
		api.getProjectPluginHooks.mockResolvedValue([]);
		const first = hooks.loadAll('OLD');
		await hooks.loadAll('NEW');
		slow.resolve([hook('old', { project_id: 'OLD' })]);
		await first;
		expect(hooks.current.currentProjectId).toBe('NEW');
		expect(hooks.current.hooks.map((h) => h.id)).toEqual(['new']);
	});

	it('a failure for a project already left sets no error and leaves the newer load in charge (R14-3)', async () => {
		const old = deferred<Hook[]>();
		const cur = deferred<Hook[]>();
		api.getProjectHooks.mockReturnValueOnce(old.promise).mockReturnValueOnce(cur.promise);
		api.getPluginHooks.mockResolvedValue([]);
		api.getProjectPluginHooks.mockResolvedValue([]);
		const first = hooks.loadAll('OLD');
		const second = hooks.loadAll('NEW');
		old.reject({ status: 500, detail: 'OLD exploded' });
		await first;
		expect(hooksError.current).toBeNull();
		expect(hooks.current.loading).toBe(true);
		cur.resolve([hook('new', { project_id: 'NEW' })]);
		await second;
		expect(hooksError.current).toBeNull();
		expect(hooks.current.loading).toBe(false);
		expect(hooks.current.hooks.map((h) => h.id)).toEqual(['new']);
	});

	it("a stale app-hooks success does not clear a newer project load's loading flag", async () => {
		const app = deferred<Hook[]>();
		const cur = deferred<Hook[]>();
		api.getAppHooks.mockReturnValueOnce(app.promise);
		api.getProjectHooks.mockReturnValueOnce(cur.promise);
		api.getPluginHooks.mockResolvedValue([]);
		api.getProjectPluginHooks.mockResolvedValue([]);
		const first = hooks.loadAppHooks();
		const second = hooks.loadAll('NEW');
		app.resolve([hook('app')]);
		await first;
		expect(hooks.current.loading).toBe(true);
		expect(hooks.current.hooks).toEqual([]);
		cur.resolve([hook('new', { project_id: 'NEW' })]);
		await second;
		expect(hooks.current.loading).toBe(false);
		expect(hooks.current.hooks.map((h) => h.id)).toEqual(['new']);
	});

	it("shows the backend's reason when a load fails", async () => {
		const failure = { status: 403, detail: 'Admin only' };
		api.getAppHooks.mockRejectedValue(failure);
		api.getPluginHooks.mockResolvedValue([]);
		await expect(hooks.loadAppHooks()).rejects.toBe(failure);
		expect(hooksError.current).toBe('Admin only');
		expect(hooks.current.loading).toBe(false);
	});
});

describe('hooks store — mutations', () => {
	beforeEach(async () => {
		api.getAppHooks.mockResolvedValue([hook('a'), hook('b', { enabled: false })]);
		api.getPluginHooks.mockResolvedValue([]);
		await hooks.loadAppHooks();
	});

	it('prepends created hooks', async () => {
		api.createAppHook.mockResolvedValue(hook('c'));
		api.createHook.mockResolvedValue(hook('d', { project_id: 'P1' }));
		await hooks.createAppHook({ name: 'c' } as never);
		await hooks.createHook('P1', { name: 'd' } as never);
		expect(api.createHook).toHaveBeenCalledWith('P1', { name: 'd' });
		expect(hooks.current.hooks.map((h) => h.id)).toEqual(['d', 'c', 'a', 'b']);
	});

	it('shows a validation detail when a create is refused', async () => {
		const refusal = { status: 422, detail: 'Webhook URL must be https' };
		api.createAppHook.mockRejectedValue(refusal);
		await expect(hooks.createAppHook({ name: 'x' } as never)).rejects.toBe(refusal);
		expect(hooksError.current).toBe('Webhook URL must be https');
	});

	it('toggleHook flips enabled through updateHook and tracks the saving flag', async () => {
		const pending = deferred<Hook>();
		api.updateHook.mockReturnValue(pending.promise);
		const toggling = hooks.toggleHook('b');
		expect(api.updateHook).toHaveBeenCalledWith('b', { enabled: true });
		expect(hookSaving.current).toEqual({ b: true });
		pending.resolve(hook('b', { enabled: true }));
		await toggling;
		expect(hookSaving.current).toEqual({ b: false });
		expect(hooks.current.hooks.find((h) => h.id === 'b')?.enabled).toBe(true);
	});

	it('toggleHook ignores an unknown id', async () => {
		await hooks.toggleHook('missing');
		expect(api.updateHook).not.toHaveBeenCalled();
	});

	it('a failed update clears the saving flag and keeps the hook', async () => {
		api.updateHook.mockRejectedValue({ status: 409, detail: 'Hook changed elsewhere' });
		await expect(hooks.updateHook('a', { enabled: false })).rejects.toMatchObject({ status: 409 });
		expect(hookSaving.current).toEqual({ a: false });
		expect(hooks.current.hooks.find((h) => h.id === 'a')?.enabled).toBe(true);
		expect(hooksError.current).toBe('Hook changed elsewhere');
	});

	it('deletes a hook; a failed delete keeps it', async () => {
		api.deleteHook.mockResolvedValueOnce(undefined).mockRejectedValueOnce(new Error('locked'));
		await hooks.deleteHook('a');
		expect(hooks.current.hooks.map((h) => h.id)).toEqual(['b']);
		await expect(hooks.deleteHook('b')).rejects.toThrow('locked');
		expect(hooks.current.hooks.map((h) => h.id)).toEqual(['b']);
		expect(hookSaving.current).toEqual({ a: false, b: false });
	});

	it('testHook returns the result and clears the testing flag either way', async () => {
		api.testHook.mockResolvedValueOnce({ success: true, output: 'ok' });
		await expect(hooks.testHook('a')).resolves.toMatchObject({ success: true });
		expect(hookTesting.current).toEqual({ a: false });
		api.testHook.mockRejectedValueOnce({ status: 504, detail: 'Hook timed out' });
		await expect(hooks.testHook('a')).rejects.toMatchObject({ status: 504 });
		expect(hookTesting.current).toEqual({ a: false });
		expect(hooksError.current).toBe('Hook timed out');
		hooks.clearError();
		expect(hooksError.current).toBeNull();
	});
});
