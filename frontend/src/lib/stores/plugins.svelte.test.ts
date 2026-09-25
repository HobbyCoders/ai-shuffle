/**
 * Plugins store — install / uninstall / update flows and their failure paths.
 *
 * The API module is mocked; errors are thrown in the shape ApiClient really
 * throws (`{ status, detail }` plain objects, not `Error`s), because the store
 * has to show the backend's reason for a refused install.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import type { PluginInfo, AvailablePlugin } from '$lib/api/plugins';

const api = vi.hoisted(() => ({
	getMarketplaces: vi.fn(),
	getAvailablePlugins: vi.fn(),
	getInstalledPlugins: vi.fn(),
	getFileBasedAgents: vi.fn(),
	checkForUpdates: vi.fn(),
	getAutoUpdateSettings: vi.fn(),
	installPlugin: vi.fn(),
	installPluginsBatch: vi.fn(),
	uninstallPlugin: vi.fn(),
	syncMarketplace: vi.fn(),
	updatePlugin: vi.fn(),
	updateAllPlugins: vi.fn()
}));

vi.mock('$lib/api/plugins', () => api);

import { plugins, pluginStats } from './plugins.svelte';

function installed(id: string, extra: Partial<PluginInfo> = {}): PluginInfo {
	const [name, marketplace] = id.split('@');
	return {
		id,
		name,
		marketplace,
		description: '',
		version: '1.0.0',
		scope: 'user',
		install_path: `/cache/${marketplace}/${name}`,
		installed_at: null,
		enabled: true,
		has_commands: false,
		has_agents: false,
		has_skills: true,
		has_hooks: false,
		...extra
	} as PluginInfo;
}

function available(id: string, isInstalled = false): AvailablePlugin {
	const [name, marketplace] = id.split('@');
	return { id, name, marketplace, description: '', installed: isInstalled, installed_version: null } as AvailablePlugin;
}

async function seed(inst: PluginInfo[], avail: AvailablePlugin[]) {
	api.getMarketplaces.mockResolvedValue([{ id: 'mp', name: 'mp' }]);
	api.getAvailablePlugins.mockResolvedValue(avail);
	api.getInstalledPlugins.mockResolvedValue(inst);
	api.getFileBasedAgents.mockResolvedValue([]);
	api.checkForUpdates.mockResolvedValue([]);
	api.getAutoUpdateSettings.mockResolvedValue(null);
	await plugins.loadAll();
}

beforeEach(() => {
	for (const fn of Object.values(api)) fn.mockReset();
	plugins.reset();
});

describe('plugins store — install', () => {
	it('shows the backend reason when an install is refused and keeps the installed list', async () => {
		await seed([installed('a@mp')], [available('a@mp', true), available('evil@mp')]);

		const refusal = { status: 400, detail: 'Plugin source escapes the marketplace directory' };
		api.installPlugin.mockRejectedValue(refusal);

		await expect(plugins.installPlugin('evil@mp')).rejects.toBe(refusal);
		expect(plugins.current.error).toBe('Plugin source escapes the marketplace directory');
		expect(plugins.current.installing['evil@mp']).toBe(false);
		expect(plugins.current.installed.map((p) => p.id)).toEqual(['a@mp']);
		expect(plugins.current.available.find((p) => p.id === 'evil@mp')?.installed).toBe(false);
	});

	it('replaces the entry when a plugin is reinstalled at another scope (no duplicate ids)', async () => {
		await seed([installed('a@mp', { scope: 'user' }), installed('b@mp')], [available('a@mp', true)]);

		api.installPlugin.mockResolvedValue(installed('a@mp', { scope: 'project', version: '1.1.0' }));
		await plugins.installPlugin('a@mp', 'project');

		expect(api.installPlugin).toHaveBeenCalledWith('a@mp', 'project');
		const ids = plugins.current.installed.map((p) => p.id);
		expect(ids.filter((id) => id === 'a@mp')).toHaveLength(1);
		expect(ids).toContain('b@mp');
		expect(plugins.current.installed.find((p) => p.id === 'a@mp')?.scope).toBe('project');

		// The batch path merges the same way.
		api.installPluginsBatch.mockResolvedValue([installed('b@mp', { version: '2.0.0' }), installed('c@mp')]);
		await plugins.installPluginsBatch(['b@mp', 'c@mp']);
		expect(plugins.current.installed.map((p) => p.id).sort()).toEqual(['a@mp', 'b@mp', 'c@mp']);
		expect(plugins.current.installed.find((p) => p.id === 'b@mp')?.version).toBe('2.0.0');
		expect(plugins.current.installing).toEqual({ 'a@mp': false, 'b@mp': false, 'c@mp': false });
	});

	it('uninstall removes the plugin and returns it to the available list', async () => {
		await seed([installed('a@mp'), installed('b@mp')], [available('a@mp', true)]);
		api.uninstallPlugin.mockResolvedValue(undefined);

		let stats: { totalInstalled: number; totalAvailable: number } | undefined;
		const unsub = pluginStats.subscribe((s) => (stats = s));
		await plugins.uninstallPlugin('a@mp');
		unsub();

		expect(plugins.current.installed.map((p) => p.id)).toEqual(['b@mp']);
		expect(plugins.current.available[0]).toMatchObject({ id: 'a@mp', installed: false, installed_version: null });
		expect(stats).toMatchObject({ totalInstalled: 1, totalAvailable: 1 });
	});
});

describe('plugins store — marketplace and update failures', () => {
	it('a failing marketplace load or sync never clears what is already installed', async () => {
		await seed([installed('a@mp')], [available('a@mp', true)]);

		api.getMarketplaces.mockRejectedValue({ status: 502, detail: 'Could not reach the marketplace' });
		await expect(plugins.loadAll()).rejects.toMatchObject({ status: 502 });
		expect(plugins.current.error).toBe('Could not reach the marketplace');
		expect(plugins.current.loading).toBe(false);
		expect(plugins.current.installed.map((p) => p.id)).toEqual(['a@mp']);

		api.syncMarketplace.mockRejectedValue({ status: 500, detail: 'git pull failed' });
		await expect(plugins.syncMarketplace('mp')).rejects.toMatchObject({ status: 500 });
		expect(plugins.current.syncing.mp).toBe(false);
		expect(plugins.current.error).toBe('git pull failed');
		expect(plugins.current.installed.map((p) => p.id)).toEqual(['a@mp']);
		expect(plugins.current.available.map((p) => p.id)).toEqual(['a@mp']);
	});

	it('update applies the new version, then re-reads updates; a failed update-all resets its flags', async () => {
		api.checkForUpdates.mockResolvedValue([]);
		await seed([installed('a@mp'), installed('b@mp')], []);
		api.checkForUpdates.mockResolvedValue([
			{ plugin_id: 'a@mp', current_version: '1.0.0', latest_version: '1.2.0' },
			{ plugin_id: 'b@mp', current_version: '1.0.0', latest_version: '1.1.0' }
		]);
		await plugins.checkForUpdates();

		api.updatePlugin.mockResolvedValue(installed('a@mp', { version: '1.2.0' }));
		api.checkForUpdates.mockResolvedValue([{ plugin_id: 'b@mp', current_version: '1.0.0', latest_version: '1.1.0' }]);
		await plugins.updatePlugin('a@mp');
		expect(plugins.current.installed.find((p) => p.id === 'a@mp')?.version).toBe('1.2.0');
		expect(plugins.current.updatesAvailable.map((u) => u.plugin_id)).toEqual(['b@mp']);
		expect(plugins.current.updating['a@mp']).toBe(false);

		api.updateAllPlugins.mockRejectedValue({ status: 500, detail: 'b@mp: clone failed' });
		await expect(plugins.updateAllPlugins()).rejects.toMatchObject({ status: 500 });
		expect(plugins.current.updating['b@mp']).toBe(false);
		expect(plugins.current.error).toBe('b@mp: clone failed');
		expect(plugins.current.installed.map((p) => p.id)).toEqual(['a@mp', 'b@mp']);
	});
});
