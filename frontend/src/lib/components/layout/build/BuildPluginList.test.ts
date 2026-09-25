/**
 * Build → Plugins: tab states, search, per-row actions (toggle / update /
 * confirm-before-uninstall / install / batch install), details navigation, sources with
 * confirm-before-remove and add-source, and failures surfacing in the error
 * strip without unhandled rejections.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';

const api = vi.hoisted(() => ({
	getMarketplaces: vi.fn(), getAvailablePlugins: vi.fn(), getInstalledPlugins: vi.fn(), getFileBasedAgents: vi.fn(),
	checkForUpdates: vi.fn(), getAutoUpdateSettings: vi.fn(), getPluginDetails: vi.fn(), installPlugin: vi.fn(),
	installPluginsBatch: vi.fn(), uninstallPlugin: vi.fn(), enablePlugin: vi.fn(), disablePlugin: vi.fn(),
	updatePlugin: vi.fn(), updateAllPlugins: vi.fn(), updateAutoUpdateSettings: vi.fn(), runAutoUpdate: vi.fn(),
	addMarketplace: vi.fn(), removeMarketplace: vi.fn(), syncMarketplace: vi.fn()
}));
vi.mock('$lib/api/plugins', () => api);
vi.mock('$lib/services/watch-resources', () => ({ watchResources: vi.fn() }));

import { plugins } from '$lib/stores/plugins.svelte';
import BuildPluginList from './BuildPluginList.svelte';

const caps = { has_commands: true, has_agents: false, has_skills: true, has_hooks: false, has_mcp: false, connector_type: null };
const installed = [
	{ id: 'writer@local', name: 'Writer', marketplace: 'local', description: 'Writing skills', version: '1.0.0', scope: 'user', install_path: '/p', installed_at: null, enabled: true, ...caps },
	{ id: 'lint@local', name: 'Linter', marketplace: 'local', description: 'Lint rules', version: '0.1.0', scope: 'user', install_path: '/p', installed_at: null, enabled: false, ...caps }
];
const available = [
	{ id: 'writer@local', name: 'Writer', marketplace: 'local', marketplace_path: '', description: '', installed: true, enabled: true, ...caps },
	{ id: 'review@local', name: 'Review', marketplace: 'local', marketplace_path: '', description: 'Code review', installed: false, enabled: false, ...caps },
	{ id: 'docs@local', name: 'Docs', marketplace: 'local', marketplace_path: '', description: 'Doc helpers', installed: false, enabled: false, ...caps }
];
const marketplace = { id: 'local', source: 'git', url: 'https://example.com/plugins.git', install_location: '/m', last_updated: null };

beforeEach(() => {
	vi.clearAllMocks();
	plugins.reset();
	api.getMarketplaces.mockResolvedValue([marketplace]);
	api.getAvailablePlugins.mockResolvedValue(available);
	api.getInstalledPlugins.mockResolvedValue(installed);
	api.getFileBasedAgents.mockResolvedValue([]);
	api.checkForUpdates.mockResolvedValue([{ plugin_id: 'writer@local', name: 'Writer', marketplace: 'local', installed_version: '1.0.0', available_version: '1.1.0', is_local: false }]);
	api.getAutoUpdateSettings.mockResolvedValue({ enabled: true, interval_hours: 6, auto_sync_marketplaces: true, auto_update_plugins: false, last_check: null });
	api.getPluginDetails.mockResolvedValue({ ...installed[0], commands: [], agents: [], skills: [] });
	api.enablePlugin.mockResolvedValue(undefined);
	api.disablePlugin.mockResolvedValue(undefined);
	api.uninstallPlugin.mockResolvedValue(undefined);
	api.installPlugin.mockImplementation(async (id: string) => ({ ...installed[0], id, name: id }));
	api.installPluginsBatch.mockImplementation(async (ids: string[]) => ids.map((id) => ({ ...installed[0], id })));
	api.removeMarketplace.mockResolvedValue(undefined);
	api.addMarketplace.mockResolvedValue({ ...marketplace, id: 'team' });
	api.updateAutoUpdateSettings.mockImplementation(async (s) => ({ enabled: true, interval_hours: 6, auto_sync_marketplaces: true, auto_update_plugins: false, last_check: null, ...s }));
});
afterEach(() => cleanup());

async function mount() {
	render(BuildPluginList);
	await screen.findByText('Writing skills');
}
const tab = (name: RegExp) => screen.getByRole('button', { name });

describe('installed tab', () => {
	it('shows loading, then rows with their state, update badge and capabilities', async () => {
		let resolve!: (v: unknown) => void;
		api.getInstalledPlugins.mockReturnValue(new Promise((r) => (resolve = r)));
		render(BuildPluginList);
		expect(screen.getByText('Loading plugins…')).toBeTruthy();
		resolve(installed);
		expect(await screen.findByText('Writing skills')).toBeTruthy();
		expect(screen.getByText('Enabled')).toBeTruthy();
		expect(screen.getByText('Disabled')).toBeTruthy();
		expect(screen.getByText('Update')).toBeTruthy();
		expect(screen.getByRole('button', { name: 'Update all (1)' })).toBeTruthy();
		expect(tab(/^Installed/).textContent).toMatch(/Installed\s*2/);
	});

	it('shows the empty state and links to Available', async () => {
		api.getInstalledPlugins.mockResolvedValue([]);
		render(BuildPluginList);
		expect(await screen.findByText('No plugins installed')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Browse available plugins' }));
		expect(await screen.findByText('Code review')).toBeTruthy();
	});

	it('shows a load failure in the error strip and dismisses it', async () => {
		api.getMarketplaces.mockRejectedValue(new Error('marketplace dir unreadable'));
		render(BuildPluginList);
		expect(await screen.findByText('marketplace dir unreadable')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
		expect(screen.queryByText('marketplace dir unreadable')).toBeNull();
	});

	it('filters by search', async () => {
		await mount();
		await fireEvent.input(screen.getByPlaceholderText('Search plugins…'), { target: { value: 'lint' } });
		expect(screen.queryByText('Writing skills')).toBeNull();
		expect(screen.getByText('Lint rules')).toBeTruthy();
		await fireEvent.input(screen.getByPlaceholderText('Search plugins…'), { target: { value: 'zzz' } });
		expect(screen.getByText('No plugins match your search')).toBeTruthy();
	});

	it('disables an enabled plugin and enables a disabled one', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Disable Writer' }));
		await waitFor(() => expect(api.disablePlugin).toHaveBeenCalledWith('writer@local'));
		await fireEvent.click(screen.getByRole('button', { name: 'Enable Linter' }));
		await waitFor(() => expect(api.enablePlugin).toHaveBeenCalledWith('lint@local'));
	});

	it('shows a failed toggle and uninstall in the error strip', async () => {
		api.disablePlugin.mockRejectedValue(new Error('plugin locked'));
		api.uninstallPlugin.mockRejectedValue(new Error('files in use'));
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Disable Writer' }));
		expect(await screen.findByText('plugin locked')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Uninstall Linter' }));
		await fireEvent.click(within(screen.getByRole('dialog', { name: 'Uninstall plugin' })).getByRole('button', { name: 'Uninstall' }));
		expect(await screen.findByText('files in use')).toBeTruthy();
		expect(screen.getByText('Lint rules')).toBeTruthy();
	});

	it('asks before uninstalling: cancel keeps the plugin, confirm uninstalls it by id', async () => {
		await mount();
		await fireEvent.click(screen.getByRole('button', { name: 'Uninstall Linter' }));
		const dialog = screen.getByRole('dialog', { name: 'Uninstall plugin' });
		expect(dialog.textContent).toContain('Linter');
		// (the backdrop is also named Cancel; press the visible button)
		await fireEvent.click(within(dialog).getByText('Cancel', { selector: 'button' }));
		expect(screen.queryByRole('dialog', { name: 'Uninstall plugin' })).toBeNull();
		expect(api.uninstallPlugin).not.toHaveBeenCalled();
		expect(screen.getByText('Lint rules')).toBeTruthy();

		await fireEvent.click(screen.getByRole('button', { name: 'Uninstall Linter' }));
		await fireEvent.click(within(screen.getByRole('dialog', { name: 'Uninstall plugin' })).getByRole('button', { name: 'Uninstall' }));
		await waitFor(() => expect(api.uninstallPlugin).toHaveBeenCalledWith('lint@local'));
		expect(api.uninstallPlugin).toHaveBeenCalledTimes(1);
		expect(screen.queryByRole('dialog', { name: 'Uninstall plugin' })).toBeNull();
		await waitFor(() => expect(screen.queryByText('Lint rules')).toBeNull());
	});

	it('opens details by keyboard and returns to the list', async () => {
		await mount();
		const row = screen.getByText('Writing skills').closest('[role="button"]') as HTMLElement;
		await fireEvent.keyDown(row, { key: 'Enter' });
		expect(await screen.findByRole('button', { name: 'Back to plugin list' })).toBeTruthy();
		expect(api.getPluginDetails).toHaveBeenCalledWith('writer@local');
		await fireEvent.click(screen.getByRole('button', { name: 'Back to plugin list' }));
		expect(await screen.findByText('Lint rules')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Details' })).toBeNull();
	});

	it('stays on the list when details fail to load', async () => {
		api.getPluginDetails.mockRejectedValue(new Error('manifest missing'));
		await mount();
		await fireEvent.click(screen.getByText('Writing skills'));
		expect(await screen.findByText('manifest missing')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Back to plugin list' })).toBeNull();
	});
});

describe('available tab', () => {
	it('lists only uninstalled plugins and installs one', async () => {
		await mount();
		await fireEvent.click(tab(/^Available/));
		expect(await screen.findByText('Code review')).toBeTruthy();
		expect(screen.queryByRole('button', { name: 'Install Writer' })).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Install Review' }));
		await waitFor(() => expect(api.installPlugin).toHaveBeenCalledWith('review@local', 'user'));
	});

	it('batch-installs the selected plugins', async () => {
		await mount();
		await fireEvent.click(tab(/^Available/));
		const pick = await screen.findByRole('button', { name: 'Select Review for batch install' });
		await fireEvent.click(pick);
		expect(pick.getAttribute('aria-pressed')).toBe('true');
		await fireEvent.click(screen.getByRole('button', { name: 'Select Docs for batch install' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Install (2)' }));
		await waitFor(() => expect(api.installPluginsBatch).toHaveBeenCalledWith(['review@local', 'docs@local'], 'user'));
		await waitFor(() => expect(screen.queryByRole('button', { name: /^Install \(/ })).toBeNull());
	});
});

describe('sources tab', () => {
	it('removes a source only on the second, confirming click', async () => {
		await mount();
		await fireEvent.click(tab(/^Sources/));
		await fireEvent.click(screen.getByRole('button', { name: 'Remove source local' }));
		expect(api.removeMarketplace).not.toHaveBeenCalled();
		await fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Remove source local' }));
		await fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
		await waitFor(() => expect(api.removeMarketplace).toHaveBeenCalledWith('local'));
	});

	it('adds a source with trimmed values and reloads available plugins', async () => {
		await mount();
		await fireEvent.click(tab(/^Sources/));
		await fireEvent.click(screen.getByRole('button', { name: 'Add source' }));
		const dialog = screen.getByRole('dialog', { name: 'Add plugin source' });
		const submit = [...dialog.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Add source') as HTMLButtonElement;
		expect(submit.disabled).toBe(true);
		await fireEvent.input(screen.getByLabelText('Name'), { target: { value: ' team ' } });
		await fireEvent.input(screen.getByLabelText('Git URL'), { target: { value: ' https://example.com/team.git ' } });
		await fireEvent.click(submit);
		await waitFor(() => expect(api.addMarketplace).toHaveBeenCalledWith('https://example.com/team.git', 'team'));
		await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
		expect(api.getAvailablePlugins).toHaveBeenCalledTimes(2);
	});

	it('keeps the add-source dialog open when the worker refuses it', async () => {
		api.addMarketplace.mockRejectedValue(new Error('not a git repository'));
		const err = vi.spyOn(console, 'error').mockImplementation(() => {});
		await mount();
		await fireEvent.click(tab(/^Sources/));
		await fireEvent.click(screen.getByRole('button', { name: 'Add source' }));
		await fireEvent.input(screen.getByLabelText('Name'), { target: { value: 'team' } });
		await fireEvent.input(screen.getByLabelText('Git URL'), { target: { value: 'https://x' } });
		const dialog = screen.getByRole('dialog', { name: 'Add plugin source' });
		await fireEvent.click([...dialog.querySelectorAll('button')].find((b) => b.textContent?.trim() === 'Add source')!);
		await waitFor(() => expect(api.addMarketplace).toHaveBeenCalledOnce());
		expect(screen.getByRole('dialog', { name: 'Add plugin source' })).toBeTruthy();
		expect((screen.getByLabelText('Git URL') as HTMLInputElement).value).toBe('https://x');
		err.mockRestore();
	});

	it('toggles auto-updates and changes the interval', async () => {
		await mount();
		await fireEvent.click(tab(/^Sources/));
		await fireEvent.click(screen.getByRole('button', { name: /Auto-update settings/ }));
		await fireEvent.change(screen.getByDisplayValue('6 hours'), { target: { value: '12' } });
		await waitFor(() => expect(api.updateAutoUpdateSettings).toHaveBeenCalledWith({ interval_hours: 12 }));
		await fireEvent.click(screen.getByRole('button', { name: 'Toggle auto-updates' }));
		await waitFor(() => expect(api.updateAutoUpdateSettings).toHaveBeenCalledWith({ enabled: false }));
	});
});
