import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { PluginDetails, PluginInfo } from '$lib/api/plugins';
import responses from '$lib/api/__fixtures__/plugin-responses.json';

const m = vi.hoisted(() => ({
	getInstalledPlugins: vi.fn(),
	getAvailablePlugins: vi.fn(),
	getPluginDetails: vi.fn(),
	installPlugin: vi.fn(),
	uninstallPlugin: vi.fn(),
	enablePlugin: vi.fn(),
	disablePlugin: vi.fn(),
	getMarketplaces: vi.fn(),
	addMarketplace: vi.fn(),
	removeMarketplace: vi.fn(),
	syncMarketplace: vi.fn(),
	checkForUpdates: vi.fn(),
	updatePlugin: vi.fn(),
	updateAllPlugins: vi.fn(),
	getAutoUpdateSettings: vi.fn(),
	updateAutoUpdateSettings: vi.fn(),
	runAutoUpdate: vi.fn(),
	getConnectors: vi.fn(),
	getConnectorTypes: vi.fn(),
	get: vi.fn(),
	post: vi.fn()
}));
vi.mock('$lib/api/plugins', () => ({
	getInstalledPlugins: m.getInstalledPlugins,
	getAvailablePlugins: m.getAvailablePlugins,
	getPluginDetails: m.getPluginDetails,
	installPlugin: m.installPlugin,
	uninstallPlugin: m.uninstallPlugin,
	enablePlugin: m.enablePlugin,
	disablePlugin: m.disablePlugin,
	getMarketplaces: m.getMarketplaces,
	addMarketplace: m.addMarketplace,
	removeMarketplace: m.removeMarketplace,
	syncMarketplace: m.syncMarketplace,
	checkForUpdates: m.checkForUpdates,
	updatePlugin: m.updatePlugin,
	updateAllPlugins: m.updateAllPlugins,
	getAutoUpdateSettings: m.getAutoUpdateSettings,
	updateAutoUpdateSettings: m.updateAutoUpdateSettings,
	runAutoUpdate: m.runAutoUpdate
}));
vi.mock('$lib/api/connectors', () => ({ getConnectors: m.getConnectors, getConnectorTypes: m.getConnectorTypes }));
vi.mock('$lib/api/client', () => ({ api: { get: m.get, post: m.post } }));

import { PluginsModel, parseItem, connectionItem, pluginItem } from './plugins-model.svelte';
import { pluginsLive, resetPluginsLive } from '$lib/settings/entries/plugins-live.svelte';
import { searchSource, attention } from '$lib/settings/entries/plugins';

const toolkit = responses.installed[0] as PluginInfo;
const draft: PluginInfo = {
	...toolkit,
	id: 'release-notes@authored',
	name: 'release-notes',
	marketplace: 'authored',
	version: '0.1.0',
	enabled: false,
	trust: 'authored',
	approval: 'pending',
	content_hash: 'sha256:abc'
};
const gmailType = {
	type: 'gmail',
	name: 'Gmail',
	description: 'Email',
	plugin_id: 'gmail@shuffle-connectors',
	plugin_installed: true,
	plugin_enabled: true,
	config_fields: [],
	triggers: [],
	actions: [],
	tools: []
};
const discordType = {
	...gmailType,
	type: 'discord',
	name: 'Discord',
	plugin_id: 'discord@shuffle-connectors',
	plugin_installed: false,
	plugin_enabled: false
};

function forbidden() {
	return Object.assign(new Error('Forbidden'), { status: 403 });
}

beforeEach(() => {
	vi.clearAllMocks();
	resetPluginsLive();
	m.getInstalledPlugins.mockResolvedValue([toolkit, draft]);
	m.getAvailablePlugins.mockResolvedValue([
		{ ...toolkit, description: 'Duplicate', installed: true },
		{ ...responses.available[1] },
		{ ...responses.available[1], id: 'gmail@shuffle-connectors', name: 'gmail', connector_type: 'gmail' }
	]);
	m.getConnectorTypes.mockResolvedValue([gmailType, discordType]);
	m.getConnectors.mockResolvedValue([
		{ id: 'work', type: 'gmail', name: 'Work', is_active: true, status: 'connected' },
		{ id: 'home', type: 'gmail', name: 'Home', is_active: true, status: 'error' }
	]);
	m.getMarketplaces.mockResolvedValue([{ id: 'mp', url: 'https://x/mp.git', last_updated: null }]);
	m.checkForUpdates.mockResolvedValue([
		{ plugin_id: 'toolkit@mp', name: 'toolkit', installed_version: '1.2.0', available_version: '1.3.0' }
	]);
	m.getAutoUpdateSettings.mockResolvedValue({
		enabled: true,
		interval_hours: 6,
		auto_update_plugins: false,
		last_check: null
	});
	m.getPluginDetails.mockImplementation(async (id: string) =>
		id === draft.id ? ({ ...responses.details_installed, ...draft } as PluginDetails) : responses.details_installed
	);
	m.get.mockResolvedValue([]);
});

describe('parseItem', () => {
	it('reads every item shape', () => {
		expect(parseItem('plugin:toolkit@mp')).toEqual({ kind: 'plugin', id: 'toolkit@mp' });
		expect(parseItem('connection:gmail')).toEqual({ kind: 'connection', type: 'gmail' });
		expect(parseItem('explore')).toEqual({ kind: 'explore' });
		expect(parseItem('sources')).toEqual({ kind: 'sources' });
		expect(parseItem('toolkit@mp')).toBeNull();
		expect(parseItem(null)).toBeNull();
		expect(pluginItem('a@b')).toBe('plugin:a@b');
		expect(connectionItem('gmail')).toBe('connection:gmail');
	});
});

describe('PluginsModel', () => {
	it('merges installed and available packages without duplicates and keeps runtimes out of packages', async () => {
		const model = new PluginsModel();
		await model.load();
		expect(model.packageEntries.map((e) => e.id)).toEqual(['toolkit@mp', 'release-notes@authored', 'extra@mp']);
		expect(model.packageEntries[0]).toMatchObject({
			installed: true,
			enabled: true,
			version: '1.2.0',
			update: { available_version: '1.3.0' }
		});
		expect(model.review.map((e) => e.id)).toEqual(['release-notes@authored']);
		expect(model.installedPackages.map((e) => e.id)).toEqual(['toolkit@mp']);
		const gmail = model.entryByItem('connection:gmail');
		expect(gmail).toMatchObject({ count: 2, failing: true, installed: true, enabled: true });
		expect(model.apps.map((e) => e.id)).toEqual(['gmail']);
		expect(model.availableCount).toBe(2); // discord and extra
		expect(model.marketplaces).toHaveLength(1);
		expect(model.autoUpdate?.interval_hours).toBe(6);
	});

	it('publishes search items and the review count for the index badge', async () => {
		const model = new PluginsModel();
		await model.load();
		expect(pluginsLive.pending).toBe(1);
		expect(attention()).toEqual({ count: 1, tone: 'warn' });
		const items = searchSource();
		expect(items.find((i) => i.title === 'release-notes')?.target).toEqual({
			category: 'plugins',
			item: 'plugin:release-notes@authored'
		});
		expect(items.find((i) => i.title === 'Gmail')?.where).toBe('Plugins, Apps');
		// Known connector types are searchable before a visit, once each.
		expect(items.filter((i) => i.target.item === 'connection:gmail')).toHaveLength(1);
		expect(items.find((i) => i.title === 'Telegram')?.target).toEqual({
			category: 'plugins',
			item: 'connection:telegram'
		});
		expect(items.find((i) => i.title === 'Explore plugins')?.target.item).toBe('explore');
	});

	it('leaves owner-only data alone on a read-only device', async () => {
		const model = new PluginsModel({ canManage: () => false });
		await model.load();
		expect(m.getConnectorTypes).not.toHaveBeenCalled();
		expect(m.getConnectors).not.toHaveBeenCalled();
		expect(m.getMarketplaces).not.toHaveBeenCalled();
		expect(model.connectionEntries).toEqual([]);
		expect(model.packageEntries).toHaveLength(3);
	});

	it('treats a 403 as forbidden and other failures as an error', async () => {
		m.getInstalledPlugins.mockRejectedValueOnce(forbidden());
		const model = new PluginsModel();
		await model.load();
		expect(model.forbidden).toBe(true);
		expect(model.error).toBe('');
		m.getInstalledPlugins.mockRejectedValueOnce({ detail: 'Plugin store offline' });
		await model.load();
		expect(model.error).toBe('Plugin store offline');
		expect(model.loading).toBe(false);
	});

	it('installs with the package id and loads its details', async () => {
		const model = new PluginsModel();
		await model.load();
		expect(await model.install('extra@mp')).toBe(true);
		expect(m.installPlugin).toHaveBeenCalledWith('extra@mp');
		expect(m.getPluginDetails).toHaveBeenCalledWith('extra@mp');
		m.installPlugin.mockRejectedValueOnce({ detail: 'No space left' });
		expect(await model.install('extra@mp')).toBe(false);
		expect(model.actionError).toBe('No space left');
		expect(model.busy).toBe('');
	});

	it('enables and disables a package, reporting failure so the switch flips back', async () => {
		const model = new PluginsModel();
		await model.load();
		expect(await model.setEnabled('toolkit@mp', false)).toBe(true);
		expect(m.disablePlugin).toHaveBeenCalledWith('toolkit@mp');
		expect(model.entryByItem('plugin:toolkit@mp')?.enabled).toBe(false);
		m.enablePlugin.mockRejectedValueOnce(new Error('nope'));
		expect(await model.setEnabled('toolkit@mp', true)).toBe(false);
		expect(model.actionError).toBe('nope');
	});

	it('approves a draft by enabling it pinned to the reviewed content hash', async () => {
		const model = new PluginsModel();
		await model.load();
		expect(await model.approve(draft.id)).toBe(false); // nothing reviewed yet
		expect(m.enablePlugin).not.toHaveBeenCalled();
		await model.loadDetails(draft.id);
		m.getInstalledPlugins.mockResolvedValue([toolkit, { ...draft, enabled: true, approval: 'approved' }]);
		expect(await model.approve(draft.id)).toBe(true);
		expect(m.enablePlugin).toHaveBeenCalledWith(draft.id, 'sha256:abc');
		expect(model.review).toHaveLength(0);
		expect(pluginsLive.pending).toBe(0);
	});

	it('changes a connector runtime by its plugin id and refreshes what uses it', async () => {
		const refs = [
			{ id: 'work', name: 'Work', active: true, automations: [{ id: 'a', name: 'Digest', is_enabled: true }] }
		];
		m.get.mockResolvedValue(refs);
		const model = new PluginsModel();
		await model.load();
		expect(await model.changeRuntime('discord', 'install')).toBe(true);
		expect(m.installPlugin).toHaveBeenCalledWith('discord@shuffle-connectors');
		expect(m.get).toHaveBeenCalledWith('/plugins/runtime/discord%40shuffle-connectors/connections');
		await model.changeRuntime('gmail', 'disable');
		expect(m.disablePlugin).toHaveBeenCalledWith('gmail@shuffle-connectors');
		await model.changeRuntime('gmail', 'rollback');
		expect(m.post).toHaveBeenCalledWith('/plugins/runtime/gmail%40shuffle-connectors/rollback');
		await model.changeRuntime('gmail', 'uninstall');
		expect(m.uninstallPlugin).toHaveBeenCalledWith('gmail@shuffle-connectors');
		expect(model.refs.gmail).toEqual(refs);
		expect(await model.changeRuntime('nope', 'install')).toBe(false);
	});

	it('reports a failed dependency check without keeping stale references', async () => {
		const model = new PluginsModel();
		await model.load();
		m.get.mockRejectedValueOnce({ detail: 'Could not reach worker' });
		await model.loadReferences('gmail');
		expect(model.refs.gmail).toEqual([]);
		expect(model.actionError).toBe('Could not reach worker');
	});

	it('maps deep-link ids from main (plugin ids, types, account ids) onto items', async () => {
		const model = new PluginsModel();
		await model.load();
		expect(model.resolveItem('connection:gmail')).toBe('connection:gmail');
		expect(model.resolveItem('toolkit@mp')).toBe('plugin:toolkit@mp');
		expect(model.resolveItem('discord')).toBe('connection:discord');
		expect(model.resolveItem('work')).toBe('connection:gmail');
		expect(model.resolveItem('ghost')).toBeNull();
	});

	it('updates one or all plugins, checks for updates and manages sources and auto-update', async () => {
		const model = new PluginsModel();
		await model.load();
		await model.update('toolkit@mp');
		expect(m.updatePlugin).toHaveBeenCalledWith('toolkit@mp');
		expect(model.updating['toolkit@mp']).toBe(false);
		await model.updateAll();
		expect(m.updateAllPlugins).toHaveBeenCalled();
		m.checkForUpdates.mockResolvedValueOnce([]);
		await model.checkUpdates();
		expect(model.updates).toEqual([]);

		m.addMarketplace.mockResolvedValueOnce({ id: 'more', url: 'https://x/more.git', last_updated: null });
		expect(await model.addSource(' https://x/more.git ', ' more ')).toBe(true);
		expect(m.addMarketplace).toHaveBeenCalledWith('https://x/more.git', 'more');
		expect(await model.addSource('', 'x')).toBe(false);
		await model.syncSource('mp');
		expect(m.syncMarketplace).toHaveBeenCalledWith('mp');
		await model.removeSource('mp');
		expect(m.removeMarketplace).toHaveBeenCalledWith('mp');

		m.updateAutoUpdateSettings.mockResolvedValueOnce({ enabled: false, interval_hours: 6 });
		expect(await model.setAutoUpdate({ enabled: false })).toBe(true);
		expect(model.autoUpdate?.enabled).toBe(false);
		m.updateAutoUpdateSettings.mockRejectedValueOnce(new Error('x'));
		expect(await model.setAutoUpdate({ interval_hours: 3 })).toBe(false);
		await model.runAutoUpdate();
		expect(m.runAutoUpdate).toHaveBeenCalled();
		expect(model.checking).toBe(false);
	});
});
