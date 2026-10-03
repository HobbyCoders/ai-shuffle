/**
 * Settings > Plugins rendered against mocked plugin and connector APIs.
 * Ports the assertions of main's PluginsHub.test.ts (list, filters, install,
 * connector runtime guidance) and PluginDetails.test.ts (backend payloads,
 * components not loaded here, agent-draft review), plus runtime references,
 * accounts, sources, read-only access, deep links and registry coverage.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';
import type { PluginDetails, PluginInfo } from '$lib/api/plugins';
import responses from '$lib/api/__fixtures__/plugin-responses.json';

const m = vi.hoisted(() => ({
	getInstalledPlugins: vi.fn(),
	getAvailablePlugins: vi.fn(),
	getPluginDetails: vi.fn(),
	installPlugin: vi.fn(),
	installPluginsBatch: vi.fn(),
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
	getProjectPlugins: vi.fn(),
	removeProjectPlugin: vi.fn(),
	getConnectors: vi.fn(),
	getConnectorTypes: vi.fn(),
	get: vi.fn(),
	post: vi.fn()
}));
vi.mock('$lib/api/plugins', () => ({ ...m }));
vi.mock('$lib/api/connectors', () => ({ getConnectors: m.getConnectors, getConnectorTypes: m.getConnectorTypes }));
vi.mock('$lib/api/client', () => ({ api: { get: m.get, post: m.post } }));
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));
// Account editing has its own tests (ConnectorManager, GoogleConnection,
// AccountConnections); here it only has to mount for the right type.
vi.mock('$lib/components/ConnectorManager.svelte', async () => ({
	default: (await import('$lib/test-stubs/ConnectorManagerStub.svelte')).default
}));

import { settingsNav } from '$lib/settings/navigation.svelte';
import { SETTINGS_ACCESS_CONTEXT } from '$lib/settings/access.svelte';
import { assertCategoryCoverage } from '$lib/settings/testing/coverage';
import { resetPluginsLive } from '$lib/settings/entries/plugins-live.svelte';
import PluginsCollection from './PluginsCollection.svelte';

const toolkit = responses.installed[0] as PluginInfo;
const installedDetails = responses.details_installed as PluginDetails;
const marketplaceDetails = responses.details_available as PluginDetails;
const gmail = {
	type: 'gmail',
	name: 'Gmail',
	description: 'Email',
	plugin_id: 'gmail@shuffle-connectors',
	config_fields: [],
	triggers: [],
	actions: [],
	tools: []
};

let details: Record<string, PluginDetails>;

function hub() {
	// The data main's PluginsHub tests used.
	m.getConnectorTypes.mockResolvedValue([gmail]);
	m.getConnectors.mockResolvedValue([{ id: 'work', type: 'gmail', name: 'Work', is_active: true }]);
	m.getInstalledPlugins.mockResolvedValue([
		{ id: 'writer@local', name: 'Writer', description: 'Writing skills', enabled: true }
	]);
	m.getAvailablePlugins.mockResolvedValue([
		{ id: 'writer@local', name: 'Writer', description: 'Duplicate' },
		{ id: 'review@local', name: 'Review', description: 'Code review' }
	]);
}

function backend() {
	// Real payloads from tests/api/test_plugins_frontend_contract.py.
	m.getConnectorTypes.mockResolvedValue([]);
	m.getConnectors.mockResolvedValue([]);
	m.getInstalledPlugins.mockResolvedValue(responses.installed);
	m.getAvailablePlugins.mockResolvedValue(responses.available);
}

async function show(item?: string, options: { tab?: string; readOnly?: boolean } = {}) {
	settingsNav.reset();
	settingsNav.selectCategory('plugins');
	if (item) settingsNav.go({ category: 'plugins', item, tab: options.tab });
	const context = new Map<unknown, unknown>();
	if (options.readOnly) context.set(SETTINGS_ACCESS_CONTEXT, { readOnly: true });
	const page = render(PluginsCollection, { context });
	await waitFor(() => expect(screen.queryByText('Loading plugins…')).toBeNull());
	return page;
}

function detail(): HTMLElement {
	return screen.getByRole('region', { name: 'Details' });
}

beforeEach(() => {
	vi.clearAllMocks();
	resetPluginsLive();
	details = {
		[installedDetails.id]: installedDetails,
		[marketplaceDetails.id]: marketplaceDetails
	};
	backend();
	m.getPluginDetails.mockImplementation(async (id: string) => {
		if (details[id]) return details[id];
		throw { detail: 'Not found' };
	});
	m.getMarketplaces.mockResolvedValue([]);
	m.checkForUpdates.mockResolvedValue([]);
	m.getAutoUpdateSettings.mockResolvedValue(null);
	m.get.mockResolvedValue([]);
});
afterEach(cleanup);

describe('the list (from PluginsHub)', () => {
	it('groups apps with accounts and installed plugins, then the catalog', async () => {
		hub();
		await show();
		const list = screen.getByRole('heading', { level: 1, name: 'Plugins' }).closest('.st-lcol') as HTMLElement;
		expect(within(list).getByText('Connected apps')).toBeTruthy();
		expect(within(list).getByText('1 account')).toBeTruthy();
		expect(within(list).getByText('Installed')).toBeTruthy();
		expect(within(list).getByRole('button', { name: /Writer/ })).toBeTruthy();
		// Review is only in the catalog.
		expect(within(list).queryByRole('button', { name: /^Review/ })).toBeNull();
		expect(within(list).getByRole('button', { name: /Explore plugins/ }).textContent).toContain('1 to add');
		await fireEvent.input(within(list).getByRole('searchbox'), { target: { value: 'gmail' } });
		expect(within(list).queryByRole('button', { name: /Writer/ })).toBeNull();
		expect(within(list).getByRole('button', { name: /Gmail/ })).toBeTruthy();
	});

	it('merges installed apps and packages in Explore without duplicate catalog entries', async () => {
		hub();
		await show('explore');
		const pane = detail();
		const rows = () => pane.querySelectorAll('[data-settings-nav-item]');
		expect(rows()).toHaveLength(3);
		expect(within(pane).getByText('1 account')).toBeTruthy();
		const search = within(pane).getByRole('searchbox', { name: 'Search plugins' });
		await fireEvent.input(search, { target: { value: 'gmail' } });
		expect(rows()).toHaveLength(1);
		await fireEvent.input(search, { target: { value: 'no-such-plugin' } });
		expect(within(pane).getByText('No plugins match your search.')).toBeTruthy();
	});

	it('filters apps independently of agent plugins', async () => {
		hub();
		await show('explore');
		const pane = detail();
		const rows = () => pane.querySelectorAll('[data-settings-nav-item]');
		expect(rows()).toHaveLength(3);
		await fireEvent.click(within(pane).getByRole('button', { name: 'Apps' }));
		expect(rows()).toHaveLength(1);
		await fireEvent.click(within(pane).getByRole('button', { name: 'Agent plugins' }));
		expect(rows()).toHaveLength(2);
	});

	it('installs using the existing package id, not a connection id', async () => {
		hub();
		await show('explore');
		await fireEvent.click(within(detail()).getByRole('button', { name: 'Install Review' }));
		await waitFor(() => expect(m.installPlugin).toHaveBeenCalledWith('review@local', 'user', false));
	});

	it('opens an item from Explore', async () => {
		hub();
		await show('explore');
		await fireEvent.click(within(detail()).getByRole('button', { name: /Writer/ }));
		expect(settingsNav.selectedItem('plugins')).toBe('plugin:writer@local');
	});
});

describe('a connected app (from PluginsHub)', () => {
	it('explains bundled source and PyPI dependencies without requiring GitHub access', async () => {
		hub();
		m.getConnectors.mockResolvedValue([{ id: 'work', type: 'gmail', name: 'Work', is_active: false }]);
		await show('connection:gmail');
		const pane = detail();
		expect(within(pane).getByText('Bundled with AI Shuffle')).toBeTruthy();
		expect(within(pane).getByText('GitHub access').closest('.st-row')?.textContent).toContain('Not needed');
		expect(within(pane).getByText('Downloads from PyPI')).toBeTruthy();
		expect(within(pane).queryByText(/sign in to GitHub in Settings/i)).toBeNull();
		// Existing accounts keep working on the bundled runtime and stay manageable.
		expect(within(pane).getByText('Keep their IDs')).toBeTruthy();
		expect(within(pane).getByRole('region', { name: 'Accounts for gmail' })).toBeTruthy();
		await fireEvent.click(within(pane).getByRole('button', { name: 'Install Gmail' }));
		await waitFor(() => expect(m.installPlugin).toHaveBeenCalledWith('gmail@shuffle-connectors'));
	});

	it('manages accounts of an installed runtime and warns before removing one that is in use', async () => {
		hub();
		m.getConnectorTypes.mockResolvedValue([
			{ ...gmail, plugin_installed: true, plugin_enabled: true, plugin_version: '1.4.0' }
		]);
		m.get.mockImplementation(async (url: string) =>
			url.includes('/connections')
				? [{ id: 'work', name: 'Work', active: true, automations: [{ id: 'a1', name: 'Digest', is_enabled: true }] }]
				: []
		);
		await show('connection:gmail');
		const pane = detail();
		expect(within(pane).getByRole('region', { name: 'Accounts for gmail' }).getAttribute('data-type-filter')).toBe(
			'gmail'
		);
		await waitFor(() => expect(within(pane).getByText('Used by 1 connection, 1 automation')).toBeTruthy());

		// Turning the runtime off asks first while connections use it.
		await fireEvent.click(within(pane).getByRole('switch', { name: 'Runtime' }));
		expect(within(pane).getByText('Disable? 1 connection, 1 automation use it.')).toBeTruthy();
		expect(m.disablePlugin).not.toHaveBeenCalled();
		await fireEvent.click(within(pane).getByRole('button', { name: 'Disable' }));
		await waitFor(() => expect(m.disablePlugin).toHaveBeenCalledWith('gmail@shuffle-connectors'));

		// So does uninstalling.
		const uninstall = within(pane).getByRole('button', { name: 'Uninstall' }) as HTMLButtonElement;
		await waitFor(() => expect(uninstall.disabled).toBe(false));
		await fireEvent.click(uninstall);
		expect(within(pane).getByText('Uninstall? 1 connection, 1 automation use it.')).toBeTruthy();
		const confirm = within(pane).getAllByRole('button', { name: 'Uninstall' }).at(-1)!;
		await fireEvent.click(confirm);
		await waitFor(() => expect(m.uninstallPlugin).toHaveBeenCalledWith('gmail@shuffle-connectors'));
	});

	it('lists what uses the runtime and offers update and roll back', async () => {
		hub();
		m.getConnectorTypes.mockResolvedValue([
			{
				...gmail,
				plugin_installed: true,
				plugin_enabled: true,
				plugin_version: '1.4.0',
				plugin_update_available: true,
				plugin_rollback_available: true
			}
		]);
		m.get.mockResolvedValue([
			{ id: 'work', name: 'Work', active: false, automations: [{ id: 'a1', name: 'Digest', is_enabled: false }] }
		]);
		await show('connection:gmail', { tab: 'Runtime' });
		const pane = detail();
		await waitFor(() => expect(within(pane).getByText('↳ Digest')).toBeTruthy());
		expect(within(pane).getByText('Inactive')).toBeTruthy();
		expect(within(pane).getByText('Paused')).toBeTruthy();
		await fireEvent.click(within(pane).getByRole('button', { name: 'Roll back runtime' }));
		await fireEvent.click(within(pane).getByRole('button', { name: 'Roll back' }));
		await waitFor(() => expect(m.post).toHaveBeenCalledWith('/plugins/runtime/gmail%40shuffle-connectors/rollback'));
		await fireEvent.click(within(pane).getAllByRole('button', { name: 'Update runtime' })[0]);
		await waitFor(() => expect(m.installPlugin).toHaveBeenCalledWith('gmail@shuffle-connectors'));
	});
});

describe('a plugin package (from PluginDetails)', () => {
	it('shows an installed plugin: version, install date, what it includes and its contents', async () => {
		await show('plugin:toolkit@mp');
		const pane = detail();
		await waitFor(() => expect(within(pane).getByText('Review helpers for pull requests.')).toBeTruthy());
		expect(within(pane).getByRole('heading', { name: 'toolkit' })).toBeTruthy();
		expect(within(pane).getByText('v1.2.0')).toBeTruthy();
		expect(within(pane).getByText('On')).toBeTruthy();
		expect(within(pane).getByRole('switch', { name: 'Enabled' }).getAttribute('aria-checked')).toBe('true');
		const installedOn = new Date(installedDetails.installed_at!).toLocaleDateString(undefined, {
			year: 'numeric',
			month: 'short',
			day: 'numeric'
		});
		expect(within(pane).getByText(installedOn)).toBeTruthy();
		const row = (label: string) => within(pane).getByText(label).closest('.st-row')?.textContent;
		expect(row('Commands')).toContain('1');
		expect(row('Agents')).toContain('1');
		expect(row('Skills')).toContain('1');
		// has_hooks drives the row; there is no hook list, so no count and no tab.
		expect(row('Hooks')).toContain('Yes');
		expect(within(pane).queryByRole('tab', { name: 'Hooks' })).toBeNull();
		expect(within(pane).getByText(installedDetails.install_path)).toBeTruthy();

		await fireEvent.click(within(pane).getByRole('tab', { name: 'Contents' }));
		expect(within(pane).getByText('/review')).toBeTruthy();
		expect(within(pane).getByText('reviewer')).toBeTruthy();
		expect(within(pane).getByText('lint')).toBeTruthy();
	});

	it('enables and disables from the header switch and uninstalls from the danger zone', async () => {
		await show('plugin:toolkit@mp');
		const pane = detail();
		await fireEvent.click(within(pane).getByRole('switch', { name: 'Enabled' }));
		await waitFor(() => expect(m.disablePlugin).toHaveBeenCalledWith('toolkit@mp'));
		await fireEvent.click(within(pane).getByRole('button', { name: 'Uninstall' }));
		expect(within(pane).getByText('Uninstall toolkit?')).toBeTruthy();
		await fireEvent.click(within(pane).getAllByRole('button', { name: 'Uninstall' }).at(-1)!);
		await waitFor(() => expect(m.uninstallPlugin).toHaveBeenCalledWith('toolkit@mp'));
	});

	it('flips the switch back when enabling fails', async () => {
		m.disablePlugin.mockRejectedValueOnce({ detail: 'Plugin is busy' });
		await show('plugin:toolkit@mp');
		const pane = detail();
		const sw = within(pane).getByRole('switch', { name: 'Enabled' });
		await fireEvent.click(sw);
		await waitFor(() => expect(within(pane).getByRole('alert').textContent).toBe('Plugin is busy'));
		expect(sw.getAttribute('aria-checked')).toBe('true');
	});

	it('lists components this app does not run, with the reason, and nothing when all load', async () => {
		await show('plugin:toolkit@mp');
		await waitFor(() => expect(within(detail()).getByText('Review helpers for pull requests.')).toBeTruthy());
		expect(screen.queryByTestId('plugin-not-loaded')).toBeNull();
		cleanup();

		details['toolkit@mp'] = {
			...installedDetails,
			format: 'agent-plugins',
			components: [
				{ kind: 'skill', name: 'lint', support: 'loaded', reason: '' },
				{ kind: 'app', name: 'drive', support: 'unsupported', reason: 'hosted by ChatGPT; it cannot run outside it' },
				{
					kind: 'user_config',
					name: 'api_token',
					support: 'needs_config',
					reason: 'asks for settings this app does not collect yet'
				}
			]
		};
		await show('plugin:toolkit@mp');
		const block = await screen.findByTestId('plugin-not-loaded');
		const text = (label: string) => within(block).getByText(label).closest('li')?.textContent;
		expect(text('ChatGPT app drive')).toContain('hosted by ChatGPT; it cannot run outside it');
		expect(text('Plugin settings api_token')).toContain('asks for settings this app does not collect yet');
		expect(block.textContent).not.toContain('lint');
		expect(within(detail()).getByText('Agent plugin')).toBeTruthy();
	});

	it('shows a marketplace plugin with an install action and no install date', async () => {
		await show('plugin:extra@mp');
		const pane = detail();
		await waitFor(() => expect(within(pane).getByText('No README')).toBeTruthy());
		expect(within(pane).queryByText('Installed')).toBeNull();
		expect(within(pane).queryByText(/1970/)).toBeNull();
		expect(within(pane).queryByText('Hooks')).toBeNull();
		expect(within(pane).getByText('Not installed')).toBeTruthy();
		expect(within(pane).queryByRole('switch')).toBeNull();
		await fireEvent.click(within(pane).getByRole('button', { name: 'Install' }));
		await waitFor(() => expect(m.installPlugin).toHaveBeenCalledWith('extra@mp', 'user', false));
	});

	it('omits the install date for an installed record without one', async () => {
		details['toolkit@mp'] = { ...installedDetails, installed_at: null };
		await show('plugin:toolkit@mp');
		const pane = detail();
		await waitFor(() => expect(within(pane).getByText('Scope')).toBeTruthy());
		expect(within(pane).queryByText('Installed')).toBeNull();
		expect(within(pane).queryByText(/1970/)).toBeNull();
		expect(within(pane).getByText('Scope').nextElementSibling?.textContent).toBe('user');
	});

	it('offers an update when one is available', async () => {
		m.checkForUpdates.mockResolvedValue([
			{
				plugin_id: 'toolkit@mp',
				name: 'toolkit',
				marketplace: 'mp',
				installed_version: '1.2.0',
				available_version: '1.3.0',
				is_local: false
			}
		]);
		await show('plugin:toolkit@mp');
		const pane = detail();
		await waitFor(() => expect(within(pane).getByText('Update to v1.3.0')).toBeTruthy());
		await fireEvent.click(within(pane).getByRole('button', { name: 'Update' }));
		await waitFor(() => expect(m.updatePlugin).toHaveBeenCalledWith('toolkit@mp'));
	});
});

describe('an agent-authored draft (review)', () => {
	const draftInfo: PluginInfo = {
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
	const draft: PluginDetails = {
		...installedDetails,
		...draftInfo,
		components: [
			{ kind: 'skill', name: 'draft-notes', support: 'loaded', reason: '', detail: '' },
			{ kind: 'mcp_server', name: 'changelog', support: 'loaded', reason: '', detail: 'python srv.py' }
		]
	};

	it('needs review in the list and on the index badge', async () => {
		m.getInstalledPlugins.mockResolvedValue([toolkit, draftInfo]);
		await show();
		const list = screen.getByRole('heading', { level: 1, name: 'Plugins' }).closest('.st-lcol') as HTMLElement;
		expect(within(list).getByText('Needs your review')).toBeTruthy();
		expect(within(list).getByRole('button', { name: /release-notes/ }).textContent).toContain('Agent draft, v0.1.0');
		const { attention } = await import('$lib/settings/entries/plugins');
		expect(attention()).toEqual({ count: 1, tone: 'warn' });
	});

	it('opens on Review, lists every component and its command line, and approves the reviewed hash', async () => {
		m.getInstalledPlugins.mockResolvedValue([toolkit, draftInfo]);
		details[draft.id] = draft;
		await show(`plugin:${draft.id}`);
		const pane = detail();
		const review = await screen.findByTestId('plugin-review');
		expect(within(pane).getByRole('tab', { name: 'Review' }).getAttribute('aria-selected')).toBe('true');
		expect(within(pane).getByText('Written by an agent. Review first.')).toBeTruthy();
		expect(within(review).getByText('Version').closest('.st-row')?.textContent).toContain('0.1.0');
		expect(within(review).getByText('Skill draft-notes')).toBeTruthy();
		expect(within(review).getByText('MCP server changelog')).toBeTruthy();
		expect(within(review).getByText('python srv.py')).toBeTruthy();
		// No enable switch: a draft is enabled only by approving it.
		expect(within(pane).queryByRole('switch')).toBeNull();

		await fireEvent.click(within(review).getByRole('button', { name: 'Approve and enable' }));
		expect(within(review).getByText('Approve v0.1.0?')).toBeTruthy();
		expect(m.enablePlugin).not.toHaveBeenCalled();
		await fireEvent.click(within(review).getByRole('button', { name: 'Approve' }));
		await waitFor(() => expect(m.enablePlugin).toHaveBeenCalledWith(draft.id, 'sha256:abc'));
	});

	it('shows no review once approved, nor for a marketplace plugin', async () => {
		m.getInstalledPlugins.mockResolvedValue([toolkit, { ...draftInfo, approval: 'approved', enabled: true }]);
		details[draft.id] = { ...draft, approval: 'approved', enabled: true };
		await show(`plugin:${draft.id}`);
		await waitFor(() => expect(within(detail()).getByRole('switch', { name: 'Enabled' })).toBeTruthy());
		expect(screen.queryByTestId('plugin-review')).toBeNull();
		cleanup();

		await show('plugin:toolkit@mp');
		await waitFor(() => expect(within(detail()).getByText('Review helpers for pull requests.')).toBeTruthy());
		expect(screen.queryByTestId('plugin-review')).toBeNull();
	});
});

describe('sources and updates', () => {
	it('adds, syncs and removes sources and updates plugins', async () => {
		m.getMarketplaces.mockResolvedValue([
			{ id: 'mp', name: 'mp', url: 'https://x/mp.git', source: 'git', path: '', last_updated: null }
		]);
		m.checkForUpdates.mockResolvedValue([
			{
				plugin_id: 'toolkit@mp',
				name: 'toolkit',
				marketplace: 'mp',
				installed_version: '1.2.0',
				available_version: '1.3.0',
				is_local: false
			}
		]);
		m.getAutoUpdateSettings.mockResolvedValue({
			enabled: true,
			interval_hours: 6,
			auto_sync_marketplaces: true,
			auto_update_plugins: false,
			last_check: null
		});
		m.addMarketplace.mockResolvedValue({
			id: 'more',
			name: 'more',
			url: 'https://x/more.git',
			source: 'git',
			path: '',
			last_updated: null
		});
		m.updateAutoUpdateSettings.mockResolvedValue({
			enabled: true,
			interval_hours: 12,
			auto_sync_marketplaces: true,
			auto_update_plugins: false,
			last_check: null
		});
		await show('sources');
		const pane = detail();
		await waitFor(() => expect(within(pane).getByText('https://x/mp.git')).toBeTruthy());

		await fireEvent.click(within(pane).getByRole('button', { name: 'Update all (1)' }));
		await waitFor(() => expect(m.updateAllPlugins).toHaveBeenCalled());
		await fireEvent.click(within(pane).getByRole('button', { name: 'Update toolkit' }));
		await waitFor(() => expect(m.updatePlugin).toHaveBeenCalledWith('toolkit@mp'));

		await fireEvent.change(within(pane).getByRole('combobox', { name: 'Check every' }), { target: { value: '12' } });
		await waitFor(() => expect(m.updateAutoUpdateSettings).toHaveBeenCalledWith({ interval_hours: 12 }));

		await fireEvent.click(within(pane).getByRole('button', { name: 'Sync mp' }));
		await waitFor(() => expect(m.syncMarketplace).toHaveBeenCalledWith('mp'));
		await fireEvent.click(within(pane).getByRole('button', { name: 'Remove' }));
		await fireEvent.click(within(pane).getAllByRole('button', { name: 'Remove' }).at(-1)!);
		await waitFor(() => expect(m.removeMarketplace).toHaveBeenCalledWith('mp'));

		await fireEvent.click(within(pane).getByRole('button', { name: 'Add source' }));
		await fireEvent.input(within(pane).getByRole('textbox', { name: 'Name' }), { target: { value: 'more' } });
		await fireEvent.input(within(pane).getByRole('textbox', { name: 'Git URL' }), {
			target: { value: 'https://x/more.git' }
		});
		await fireEvent.click(within(pane).getByRole('button', { name: 'Add' }));
		await waitFor(() => expect(m.addMarketplace).toHaveBeenCalledWith('https://x/more.git', 'more'));
	});
});

// Ported from BuildPluginList.test.ts (main's hub mounted it as its Manage view).
describe('ported from the hub manage view', () => {
	it('batch-installs the selected plugins from Explore', async () => {
		m.getAvailablePlugins.mockResolvedValue([
			...responses.available,
			{ id: 'docs@mp', name: 'docs', description: 'Docs helpers' }
		]);
		m.installPluginsBatch.mockResolvedValue([]);
		await show('explore');
		const pane = detail();
		expect(within(pane).queryByRole('button', { name: /^Install \(/ })).toBeNull();
		const pick = within(pane).getByRole('button', { name: 'Select extra for batch install' });
		await fireEvent.click(pick);
		expect(pick.getAttribute('aria-pressed')).toBe('true');
		await fireEvent.click(within(pane).getByRole('button', { name: 'Select docs for batch install' }));
		await fireEvent.click(within(pane).getByRole('button', { name: 'Install (2)' }));
		await waitFor(() => expect(m.installPluginsBatch).toHaveBeenCalledWith(['extra@mp', 'docs@mp'], 'user', false));
		await waitFor(() => expect(within(pane).queryByRole('button', { name: /^Install \(/ })).toBeNull());
		expect(m.getInstalledPlugins.mock.calls.length).toBeGreaterThan(1);
	});

	it('shows a failed toggle and puts the switch back', async () => {
		m.disablePlugin.mockRejectedValue({ detail: 'Plugin is busy' });
		await show('plugin:toolkit@mp');
		const pane = detail();
		const sw = within(pane).getByRole('switch', { name: 'Enabled' });
		await fireEvent.click(sw);
		await waitFor(() => expect(screen.getByText('Plugin is busy')).toBeTruthy());
		await waitFor(() => expect(sw.getAttribute('aria-checked')).toBe('true'));
	});

	it('keeps the add-source form filled when the worker refuses it', async () => {
		m.addMarketplace.mockRejectedValue({ detail: 'not a git repository' });
		await show('sources');
		const pane = detail();
		await fireEvent.click(within(pane).getByRole('button', { name: 'Add source' }));
		await fireEvent.input(within(pane).getByRole('textbox', { name: 'Name' }), { target: { value: 'team' } });
		await fireEvent.input(within(pane).getByRole('textbox', { name: 'Git URL' }), { target: { value: 'https://x' } });
		await fireEvent.click(within(pane).getByRole('button', { name: 'Add' }));
		await waitFor(() => expect(m.addMarketplace).toHaveBeenCalledOnce());
		await waitFor(() => expect(screen.getByText('not a git repository')).toBeTruthy());
		expect((within(pane).getByRole('textbox', { name: 'Git URL' }) as HTMLInputElement).value).toBe('https://x');
	});

	it('says so when plugin details fail to load, and the list stays', async () => {
		m.getInstalledPlugins.mockResolvedValue([
			...responses.installed,
			{ id: 'broken@mp', name: 'broken', description: 'x', enabled: true }
		]);
		await show('plugin:broken@mp');
		await waitFor(() => expect(within(detail()).getByRole('alert').textContent).toContain('Not found'));
		expect(screen.getAllByText('toolkit').length).toBeGreaterThan(0);
	});
});

describe('access and deep links', () => {
	it('keeps apps, sources and installs off a read-only device', async () => {
		hub();
		await show('explore', { readOnly: true });
		expect(m.getConnectorTypes).not.toHaveBeenCalled();
		expect(screen.queryByRole('button', { name: /Sources and updates/ })).toBeNull();
		expect(within(detail()).queryByRole('button', { name: 'Install Review' })).toBeNull();
		cleanup();
		await show('connection:gmail', { readOnly: true });
		expect(within(detail()).getByText('Available after approval')).toBeTruthy();
	});

	it('shows Available after approval when the plugin list is forbidden', async () => {
		m.getInstalledPlugins.mockRejectedValue(Object.assign(new Error('Forbidden'), { status: 403 }));
		await show();
		expect(screen.getByText('Available after approval')).toBeTruthy();
	});

	it('lands bare plugin ids, connector types and account ids from older links on their item', async () => {
		hub();
		await show('writer@local');
		await waitFor(() => expect(settingsNav.selectedItem('plugins')).toBe('plugin:writer@local'));
		cleanup();
		await show('work');
		await waitFor(() => expect(settingsNav.selectedItem('plugins')).toBe('connection:gmail'));
	});
});

describe('telling the owner what a plugin does (plugin review 2026-10-03)', () => {
	const hooked: PluginDetails = {
		...installedDetails,
		enabled: false,
		components: [
			{ kind: 'skill', name: 'lint', support: 'loaded', reason: '' },
			{ kind: 'hook', name: 'PreToolUse', support: 'loaded', reason: '', detail: 'Bash: ./guard.sh' },
			{ kind: 'mcp_server', name: 'github', support: 'loaded', reason: '', detail: 'npx -y gh-mcp' },
			{ kind: 'mcp_server', name: 'remote', support: 'unsupported', reason: 'SSE transport is not supported', detail: '' }
		]
	};

	function installedOff(info: Partial<PluginInfo> = {}) {
		m.getInstalledPlugins.mockResolvedValue([{ ...toolkit, enabled: false, ...info }]);
	}

	it('switching on a plugin that runs code shows what runs first, with one confirm', async () => {
		installedOff({ components: hooked.components });
		details['toolkit@mp'] = hooked;
		await show('plugin:toolkit@mp');
		const pane = detail();
		const sw = within(pane).getByRole('switch', { name: 'Enabled' });
		await fireEvent.click(sw);
		const card = await screen.findByTestId('plugin-enable-review');
		expect(m.enablePlugin).not.toHaveBeenCalled();
		await waitFor(() => expect(sw.getAttribute('aria-checked')).toBe('false'));
		expect(within(card).getByText('Before you turn it on')).toBeTruthy();
		expect(within(card).getByText('Before a tool runs')).toBeTruthy();
		expect(within(card).getByText('Bash: ./guard.sh')).toBeTruthy();
		expect(within(card).getByText('Runs a program')).toBeTruthy();
		expect(within(card).getByText('npx -y gh-mcp')).toBeTruthy();
		// A server that does not load here runs nothing, so it is not listed.
		expect(within(card).queryByText('remote')).toBeNull();

		await fireEvent.click(within(card).getByRole('button', { name: 'Cancel' }));
		expect(screen.queryByTestId('plugin-enable-review')).toBeNull();
		expect(m.enablePlugin).not.toHaveBeenCalled();

		await fireEvent.click(sw);
		const again = await screen.findByTestId('plugin-enable-review');
		await fireEvent.click(within(again).getByRole('button', { name: 'Turn on' }));
		await waitFor(() => expect(m.enablePlugin).toHaveBeenCalledWith('toolkit@mp'));
		await waitFor(() => expect(screen.queryByTestId('plugin-enable-review')).toBeNull());
	});

	it('a plugin that runs no code switches on at once', async () => {
		installedOff();
		details['toolkit@mp'] = { ...installedDetails, enabled: false };
		await show('plugin:toolkit@mp');
		await fireEvent.click(within(detail()).getByRole('switch', { name: 'Enabled' }));
		await waitFor(() => expect(m.enablePlugin).toHaveBeenCalledWith('toolkit@mp'));
		expect(screen.queryByTestId('plugin-enable-review')).toBeNull();
	});

	it('an agent install waiting for the owner says so, unlike one the owner switched off', async () => {
		const agentInstall = { ...toolkit, id: 'gh@mp', name: 'gh', enabled: false, approval: 'pending' };
		m.getInstalledPlugins.mockResolvedValue([{ ...toolkit, enabled: false }, agentInstall]);
		details['gh@mp'] = { ...hooked, ...agentInstall, components: hooked.components };
		await show();
		const list = screen.getByRole('heading', { level: 1, name: 'Plugins' }).closest('.st-lcol') as HTMLElement;
		expect(within(list).getByText('Needs your review')).toBeTruthy();
		expect(within(list).getByRole('button', { name: /^gh/ }).textContent).toContain('Added by an agent');
		const off = within(list).getByRole('button', { name: /^toolkit/ }).textContent ?? '';
		expect(off).toContain('Off');
		expect(off).not.toContain('agent');
		expect(off.trim().endsWith('Off')).toBe(true);
		cleanup();

		await show('plugin:gh@mp');
		const pane = detail();
		const review = await screen.findByTestId('plugin-review');
		expect(within(pane).getByText('Added by an agent. Review first.')).toBeTruthy();
		expect(within(pane).getByText('Agent install')).toBeTruthy();
		expect(within(review).getByText('npx -y gh-mcp')).toBeTruthy();
		expect(within(pane).queryByRole('switch')).toBeNull();
		await fireEvent.click(within(review).getByRole('button', { name: 'Approve and enable' }));
		await fireEvent.click(within(review).getByRole('button', { name: 'Approve' }));
		await waitFor(() => expect(m.enablePlugin).toHaveBeenCalledWith('gh@mp', null));
	});

	it('shows what was adapted on import, and nothing when no note came back', async () => {
		details['toolkit@mp'] = {
			...installedDetails,
			components: [
				{ kind: 'skill', name: 'lint', support: 'loaded', reason: '' },
				{
					kind: 'agent',
					name: 'reviewer',
					support: 'loaded',
					reason: 'model pin removed: the model is chosen per chat here; MultiEdit became Edit'
				},
				{ kind: 'agent', name: 'shell-only', support: 'unsupported', reason: 'none of the tools it lists exist here' },
				{ kind: 'executables', name: 'bin/', support: 'unsupported', reason: 'files in bin/ are not added to the PATH' },
				{ kind: 'brand_new_kind', name: 'x', support: 'unsupported', reason: '' }
			]
		};
		await show('plugin:toolkit@mp');
		const adapted = await screen.findByTestId('plugin-adapted');
		expect(within(adapted).getByText('Agent reviewer')).toBeTruthy();
		expect(adapted.textContent).toContain('model pin removed');
		expect(adapted.textContent).not.toContain('lint');
		const left = screen.getByTestId('plugin-not-loaded');
		expect(within(left).getByText('Agent shell-only')).toBeTruthy();
		expect(within(left).getByText('Programs bin/')).toBeTruthy();
		// An unknown kind is spelled out, and a missing reason still says something.
		expect(within(left).getByText('Brand new kind x').closest('li')?.textContent).toContain('Not supported here');
		expect(left.textContent).not.toContain('brand_new_kind');
		cleanup();

		// The shared fixture's agent really was adapted (its model pin); clear
		// the notes to check the empty case.
		details['toolkit@mp'] = {
			...installedDetails,
			components: (installedDetails.components ?? []).map((c) => ({ ...c, reason: '' }))
		};
		await show('plugin:toolkit@mp');
		await waitFor(() => expect(within(detail()).getByText('Review helpers for pull requests.')).toBeTruthy());
		expect(screen.queryByTestId('plugin-adapted')).toBeNull();
	});

	it('lists the outside services a plugin declares, by name', async () => {
		details['toolkit@mp'] = {
			...installedDetails,
			components: [
				...(installedDetails.components ?? []).map((c) => ({ ...c, reason: '' })),
				{ kind: 'service', name: 'cloudflare', support: 'loaded', reason: '', detail: '' }
			]
		};
		// No sign-in declared: the installed plugin's Connections list the name only.
		m.get.mockRejectedValueOnce({ status: 404, detail: 'not declared' });
		await show('plugin:toolkit@mp');
		const block = await screen.findByTestId('plugin-connections');
		await waitFor(() => expect(within(block).getByText('cloudflare')).toBeTruthy());
		expect(within(block).getByText('Asks you for access')).toBeTruthy();
		// A declaration is not a part that loads or was adapted.
		expect(screen.queryByTestId('plugin-adapted')).toBeNull();
		expect(screen.queryByTestId('plugin-not-loaded')).toBeNull();
	});

	it('shows a declared sign-in in the plugin’s Connections with Sign out', async () => {
		details['toolkit@mp'] = {
			...installedDetails,
			components: [{ kind: 'service', name: 'cloudflare', support: 'loaded', reason: '', detail: '' }]
		};
		m.get.mockImplementation(async (path: string) =>
			path.startsWith('/plugins/oauth/status')
				? {
						plugin_id: 'toolkit@mp',
						enabled: true,
						redirect_uri: 'https://w.example/api/v1/plugins/oauth/callback',
						services: [{ plugin_id: 'toolkit@mp', service: 'cloudflare', state: 'signed_in', reason: '', client_configured: true, client_id: 'cid', access: 'read', authorize_host: 'dash.cloudflare.com' }]
					}
				: undefined
		);
		await show('plugin:toolkit@mp');
		const row = await screen.findByTestId('plugin-service-cloudflare');
		expect(m.get).toHaveBeenCalledWith('/plugins/oauth/status?plugin=toolkit%40mp');
		expect(within(detail()).getByText('Signed in · dash.cloudflare.com')).toBeTruthy();
		expect(within(detail()).getByRole('button', { name: 'Sign out' })).toBeTruthy();
		expect(row.dataset.step).toBe('signed_in');
		m.get.mockReset();
	});

	it('reports catalog entries that cannot install here, with the reason', async () => {
		m.getMarketplaces.mockResolvedValue([
			{
				id: 'openai-mp',
				source: 'git',
				url: 'https://x/mp.git',
				install_location: '',
				last_updated: null,
				skipped: [{ name: 'npm-only', reason: "packages from npm can't be installed here" }]
			}
		]);
		await show('explore');
		const block = await screen.findByTestId('plugins-skipped');
		expect(within(block).getByText("Can't install here")).toBeTruthy();
		expect(within(block).getByText('npm-only')).toBeTruthy();
		expect(within(block).getByText("packages from npm can't be installed here")).toBeTruthy();
		expect(within(block).getByText('openai-mp')).toBeTruthy();
	});

	it('fills the source name from the URL and lets the owner change it', async () => {
		m.addMarketplace.mockResolvedValue({ id: 'team', url: 'https://x/team.git', last_updated: null });
		await show('sources');
		const pane = detail();
		await fireEvent.click(within(pane).getByRole('button', { name: 'Add source' }));
		const name = within(pane).getByRole('textbox', { name: 'Name' }) as HTMLInputElement;
		const url = within(pane).getByRole('textbox', { name: 'Git URL' }) as HTMLInputElement;
		const add = within(pane).getByRole('button', { name: 'Add' }) as HTMLButtonElement;
		expect(add.disabled).toBe(true);
		await fireEvent.input(url, { target: { value: 'https://github.com/acme/team-plugins.git' } });
		expect(name.value).toBe('team-plugins');
		expect(add.disabled).toBe(false);
		await fireEvent.input(name, { target: { value: 'team' } });
		await fireEvent.input(url, { target: { value: 'https://github.com/acme/other.git' } });
		expect(name.value).toBe('team');
		await fireEvent.click(add);
		await waitFor(() => expect(m.addMarketplace).toHaveBeenCalledWith('https://github.com/acme/other.git', 'team'));
	});

	it('shows plugins found in project folders, says they do not load yet, and removes one', async () => {
		m.get.mockImplementation(async (path: string) =>
			path === '/projects'
				? [
						{ id: 'p1', name: 'Website' },
						{ id: 'p2', name: 'Empty repo' }
					]
				: []
		);
		m.getProjectPlugins.mockImplementation(async (id: string) =>
			id === 'p1' ? [{ ...toolkit, id: 'lint@project-local', name: 'lint', scope: 'local', marketplace: 'project' }] : []
		);
		await show('projects');
		const pane = detail();
		await waitFor(() => expect(within(pane).getByText('Website')).toBeTruthy());
		expect(within(pane).queryByText('Empty repo')).toBeNull();
		expect(within(pane).getByText("Chats don't load these yet.")).toBeTruthy();
		expect(within(pane).getByText('lint').closest('.st-row')?.textContent).toContain('Not loaded');
		expect(within(pane).queryByRole('switch')).toBeNull();
		await fireEvent.click(within(pane).getByRole('button', { name: 'Remove' }));
		await fireEvent.click(within(pane).getByRole('button', { name: 'Delete' }));
		await waitFor(() => expect(m.removeProjectPlugin).toHaveBeenCalledWith('p1', 'lint@project-local'));
		await waitFor(() => expect(within(pane).getByText('No project has plugins.')).toBeTruthy());
	});
});

describe('registry', () => {
	it('renders every Plugins entry where the registry says', async () => {
		await assertCategoryCoverage('plugins', async (where) => {
			settingsNav.reset();
			settingsNav.go({ category: 'plugins', ...where });
			return render(PluginsCollection).container;
		});
	});
});
