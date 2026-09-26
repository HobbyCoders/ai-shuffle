/**
 * Plugin Management Store
 *
 * Manages state for Claude Code plugins:
 * - Marketplaces list and management
 * - Available plugins from all marketplaces
 * - Installed plugins
 * - Plugin enable/disable state
 * - File-based agents from plugins
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import type {
	MarketplaceInfo,
	PluginInfo,
	AvailablePlugin,
	PluginDetails,
	FileBasedAgent,
	PluginUpdateInfo,
	AutoUpdateSettings
} from '$lib/api/plugins';
import * as pluginsApi from '$lib/api/plugins';
import { getErrorMessage } from '$lib/api/client';
import { createSubscribers, withLoading } from './store-utils';

// ============================================================================
// Types
// ============================================================================

interface PluginsState {
	marketplaces: MarketplaceInfo[];
	available: AvailablePlugin[];
	installed: PluginInfo[];
	fileBasedAgents: FileBasedAgent[];
	selectedPlugin: PluginDetails | null;
	loading: boolean;
	syncing: Record<string, boolean>; // marketplace id -> syncing state
	installing: Record<string, boolean>; // plugin id -> installing state
	updating: Record<string, boolean>; // plugin id -> updating state
	updatesAvailable: PluginUpdateInfo[];
	autoUpdateSettings: AutoUpdateSettings | null;
	checkingUpdates: boolean;
	error: string | null;
}

/**
 * Merge freshly installed plugins into the installed list. The backend keeps one
 * install record per plugin id (a reinstall, e.g. at another scope, replaces
 * it), so a returned id replaces its old entry instead of adding a duplicate.
 */
function upsertInstalled(list: PluginInfo[], incoming: PluginInfo[]): PluginInfo[] {
	const ids = new Set(incoming.map((p) => p.id));
	return [...list.filter((p) => !ids.has(p.id)), ...incoming];
}

// ============================================================================
// Initial State
// ============================================================================

const initialState: PluginsState = {
	marketplaces: [],
	available: [],
	installed: [],
	fileBasedAgents: [],
	selectedPlugin: null,
	loading: false,
	syncing: {},
	installing: {},
	updating: {},
	updatesAvailable: [],
	autoUpdateSettings: null,
	checkingUpdates: false,
	error: null
};

// ============================================================================
// Reactive State
// ============================================================================

let _state = $state<PluginsState>(initialState);

// ============================================================================
// Subscribe shim (centralized via store-utils)
// ============================================================================

const { subscribe, update: _update, notifySubscribers: _notifySubscribers, createDerived } =
	createSubscribers<PluginsState>(
		() => _state,
		(v) => { _state = v; }
	);

// ============================================================================
// Exported Store
// ============================================================================

export const plugins = {
	get current() {
		return _state;
	},

	subscribe,

	// ========================================================================
	// Marketplace Operations
	// ========================================================================

	/**
	 * Add a new marketplace
	 */
	async addMarketplace(url: string, name: string): Promise<MarketplaceInfo> {
		return withLoading(_update, async () => {
			const marketplace = await pluginsApi.addMarketplace(url, name);
			_update((state) => ({ ...state, marketplaces: [...state.marketplaces, marketplace] }));
			return marketplace;
		}, (e) => getErrorMessage(e, 'Failed to add marketplace'));
	},

	/**
	 * Remove a marketplace
	 */
	async removeMarketplace(marketplaceId: string): Promise<void> {
		return withLoading(_update, async () => {
			await pluginsApi.removeMarketplace(marketplaceId);
			_update((state) => ({
				...state,
				marketplaces: state.marketplaces.filter((m) => m.id !== marketplaceId),
				available: state.available.filter((p) => p.marketplace !== marketplaceId)
			}));
		}, (e) => getErrorMessage(e, 'Failed to remove marketplace'));
	},

	/**
	 * Sync a marketplace (git pull)
	 */
	async syncMarketplace(marketplaceId: string): Promise<void> {
		_update((state) => ({
			...state,
			syncing: { ...state.syncing, [marketplaceId]: true },
			error: null
		}));

		try {
			await pluginsApi.syncMarketplace(marketplaceId);
			// Reload available plugins after sync
			const available = await pluginsApi.getAvailablePlugins();
			_update((state) => ({
				...state,
				available,
				syncing: { ...state.syncing, [marketplaceId]: false }
			}));
		} catch (e) {
			const error = getErrorMessage(e, 'Failed to sync marketplace');
			_update((state) => ({
				...state,
				syncing: { ...state.syncing, [marketplaceId]: false },
				error
			}));
			throw e;
		}
	},

	// ========================================================================
	// Plugin Discovery
	// ========================================================================

	/**
	 * Load all available plugins
	 */
	async loadAvailable(marketplace?: string): Promise<void> {
		return withLoading(_update, async () => {
			const available = await pluginsApi.getAvailablePlugins(marketplace);
			_update((state) => ({ ...state, available }));
		}, (e) => getErrorMessage(e, 'Failed to load available plugins'));
	},

	/**
	 * Load all installed plugins
	 */
	async loadInstalled(): Promise<void> {
		return withLoading(_update, async () => {
			const installed = await pluginsApi.getInstalledPlugins();
			_update((state) => ({ ...state, installed }));
		}, (e) => getErrorMessage(e, 'Failed to load installed plugins'));
	},

	/**
	 * Load all data (marketplaces, available, installed)
	 */
	async loadAll(): Promise<void> {
		return withLoading(_update, async () => {
			const [marketplaces, available, installed, fileBasedAgents, updatesAvailable, autoUpdateSettings] = await Promise.all([
				pluginsApi.getMarketplaces(),
				pluginsApi.getAvailablePlugins(),
				pluginsApi.getInstalledPlugins(),
				pluginsApi.getFileBasedAgents(),
				pluginsApi.checkForUpdates().catch(() => [] as PluginUpdateInfo[]),
				pluginsApi.getAutoUpdateSettings().catch(() => null as AutoUpdateSettings | null)
			]);

			_update((state) => ({
				...state,
				marketplaces,
				available,
				installed,
				fileBasedAgents,
				updatesAvailable,
				autoUpdateSettings
			}));
		}, (e) => getErrorMessage(e, 'Failed to load plugins data'));
	},

	/**
	 * Get plugin details
	 */
	async selectPlugin(pluginId: string): Promise<void> {
		return withLoading(_update, async () => {
			const selectedPlugin = await pluginsApi.getPluginDetails(pluginId);
			_update((state) => ({ ...state, selectedPlugin }));
		}, (e) => getErrorMessage(e, 'Failed to load plugin details'));
	},

	/**
	 * Clear selected plugin
	 */
	clearSelectedPlugin(): void {
		_update((state) => ({ ...state, selectedPlugin: null }));
	},

	// ========================================================================
	// Plugin Installation
	// ========================================================================

	/**
	 * Install a single plugin
	 */
	async installPlugin(pluginId: string, scope: string = 'user'): Promise<PluginInfo> {
		_update((state) => ({
			...state,
			installing: { ...state.installing, [pluginId]: true },
			error: null
		}));

		try {
			const plugin = await pluginsApi.installPlugin(pluginId, scope);
			_update((state) => ({
				...state,
				installed: upsertInstalled(state.installed, [plugin]),
				available: state.available.map((p) =>
					p.id === pluginId ? { ...p, installed: true, enabled: plugin.enabled } : p
				),
				installing: { ...state.installing, [pluginId]: false }
			}));
			return plugin;
		} catch (e) {
			const error = getErrorMessage(e, 'Failed to install plugin');
			_update((state) => ({
				...state,
				installing: { ...state.installing, [pluginId]: false },
				error
			}));
			throw e;
		}
	},

	/**
	 * Install multiple plugins at once
	 */
	async installPluginsBatch(pluginIds: string[], scope: string = 'user'): Promise<PluginInfo[]> {
		// Mark all as installing
		_update((state) => ({
			...state,
			installing: {
				...state.installing,
				...pluginIds.reduce((acc, id) => ({ ...acc, [id]: true }), {})
			},
			error: null
		}));

		try {
			const plugins = await pluginsApi.installPluginsBatch(pluginIds, scope);
			const installedById = new Map(plugins.map((p) => [p.id, p]));

			_update((state) => ({
				...state,
				installed: upsertInstalled(state.installed, plugins),
				available: state.available.map((p) => {
					const installed = installedById.get(p.id);
					return installed ? { ...p, installed: true, enabled: installed.enabled } : p;
				}),
				installing: {
					...state.installing,
					...pluginIds.reduce((acc, id) => ({ ...acc, [id]: false }), {})
				}
			}));
			return plugins;
		} catch (e) {
			const error = getErrorMessage(e, 'Failed to install plugins');
			_update((state) => ({
				...state,
				installing: {
					...state.installing,
					...pluginIds.reduce((acc, id) => ({ ...acc, [id]: false }), {})
				},
				error
			}));
			throw e;
		}
	},

	/**
	 * Uninstall a plugin
	 */
	async uninstallPlugin(pluginId: string): Promise<void> {
		_update((state) => ({
			...state,
			installing: { ...state.installing, [pluginId]: true },
			error: null
		}));

		try {
			await pluginsApi.uninstallPlugin(pluginId);
			_update((state) => ({
				...state,
				installed: state.installed.filter((p) => p.id !== pluginId),
				available: state.available.map((p) =>
					p.id === pluginId ? { ...p, installed: false, enabled: false } : p
				),
				installing: { ...state.installing, [pluginId]: false }
			}));
		} catch (e) {
			const error = getErrorMessage(e, 'Failed to uninstall plugin');
			_update((state) => ({
				...state,
				installing: { ...state.installing, [pluginId]: false },
				error
			}));
			throw e;
		}
	},

	// ========================================================================
	// Plugin Enable/Disable
	// ========================================================================

	/**
	 * Enable a plugin globally
	 */
	async enablePlugin(pluginId: string): Promise<void> {
		try {
			await pluginsApi.enablePlugin(pluginId);
			_update((state) => ({
				...state,
				installed: state.installed.map((p) =>
					p.id === pluginId ? { ...p, enabled: true } : p
				)
			}));
		} catch (e) {
			const error = getErrorMessage(e, 'Failed to enable plugin');
			_update((state) => ({ ...state, error }));
			throw e;
		}
	},

	/**
	 * Disable a plugin globally
	 */
	async disablePlugin(pluginId: string): Promise<void> {
		try {
			await pluginsApi.disablePlugin(pluginId);
			_update((state) => ({
				...state,
				installed: state.installed.map((p) =>
					p.id === pluginId ? { ...p, enabled: false } : p
				)
			}));
		} catch (e) {
			const error = getErrorMessage(e, 'Failed to disable plugin');
			_update((state) => ({ ...state, error }));
			throw e;
		}
	},

	/**
	 * Toggle plugin enabled state
	 */
	async togglePlugin(pluginId: string): Promise<void> {
		const plugin = _state.installed.find((p) => p.id === pluginId);
		if (plugin) {
			if (plugin.enabled) {
				await plugins.disablePlugin(pluginId);
			} else {
				await plugins.enablePlugin(pluginId);
			}
		}
	},

	// ========================================================================
	// File-Based Agents
	// ========================================================================

	// ========================================================================
	// Plugin Updates
	// ========================================================================

	/**
	 * Check for available plugin updates
	 */
	async checkForUpdates(): Promise<PluginUpdateInfo[]> {
		_update((state) => ({ ...state, checkingUpdates: true, error: null }));

		try {
			const updatesAvailable = await pluginsApi.checkForUpdates();
			_update((state) => ({ ...state, updatesAvailable, checkingUpdates: false }));
			return updatesAvailable;
		} catch (e) {
			const error = getErrorMessage(e, 'Failed to check for updates');
			_update((state) => ({ ...state, checkingUpdates: false, error }));
			throw e;
		}
	},

	/**
	 * Update a specific plugin
	 */
	async updatePlugin(pluginId: string): Promise<PluginInfo> {
		_update((state) => ({
			...state,
			updating: { ...state.updating, [pluginId]: true },
			error: null
		}));

		try {
			const updated = await pluginsApi.updatePlugin(pluginId);

			// Re-check from the backend to get authoritative update state
			const freshUpdates = await pluginsApi.checkForUpdates();

			_update((state) => ({
				...state,
				installed: state.installed.map((p) =>
					p.id === pluginId ? updated : p
				),
				updatesAvailable: freshUpdates,
				updating: { ...state.updating, [pluginId]: false }
			}));
			return updated;
		} catch (e) {
			const error = getErrorMessage(e, 'Failed to update plugin');
			_update((state) => ({
				...state,
				updating: { ...state.updating, [pluginId]: false },
				error
			}));
			throw e;
		}
	},

	/**
	 * Update all plugins with available updates
	 */
	async updateAllPlugins(): Promise<PluginInfo[]> {
		const updateIds = _state.updatesAvailable.map((u) => u.plugin_id);
		_update((state) => ({
			...state,
			updating: {
				...state.updating,
				...updateIds.reduce((acc, id) => ({ ...acc, [id]: true }), {})
			},
			error: null
		}));

		try {
			const updatedPlugins = await pluginsApi.updateAllPlugins();

			// Re-check from the backend to get authoritative update state
			// rather than relying on optimistic filtering which can drift
			const [freshInstalled, freshUpdates] = await Promise.all([
				pluginsApi.getInstalledPlugins(),
				pluginsApi.checkForUpdates()
			]);

			_update((state) => ({
				...state,
				installed: freshInstalled,
				updatesAvailable: freshUpdates,
				updating: {
					...state.updating,
					...updateIds.reduce((acc, id) => ({ ...acc, [id]: false }), {})
				}
			}));
			return updatedPlugins;
		} catch (e) {
			const error = getErrorMessage(e, 'Failed to update plugins');
			_update((state) => ({
				...state,
				updating: {
					...state.updating,
					...updateIds.reduce((acc, id) => ({ ...acc, [id]: false }), {})
				},
				error
			}));
			throw e;
		}
	},

	// ========================================================================
	// Auto-Update Settings
	// ========================================================================

	/**
	 * Update auto-update settings
	 */
	async updateAutoUpdateSettings(settings: Partial<AutoUpdateSettings>): Promise<void> {
		try {
			const autoUpdateSettings = await pluginsApi.updateAutoUpdateSettings(settings);
			_update((state) => ({ ...state, autoUpdateSettings }));
		} catch (e) {
			const error = getErrorMessage(e, 'Failed to update auto-update settings');
			_update((state) => ({ ...state, error }));
			throw e;
		}
	},

	/**
	 * Manually trigger auto-update cycle
	 */
	async runAutoUpdate(): Promise<void> {
		_update((state) => ({ ...state, checkingUpdates: true, error: null }));

		try {
			const result = await pluginsApi.runAutoUpdate();
			// Reload installed plugins to reflect any updates
			const [installed, updatesAvailable] = await Promise.all([
				pluginsApi.getInstalledPlugins(),
				pluginsApi.checkForUpdates()
			]);
			_update((state) => ({
				...state,
				installed,
				updatesAvailable,
				checkingUpdates: false
			}));
		} catch (e) {
			const error = getErrorMessage(e, 'Failed to run auto-update');
			_update((state) => ({ ...state, checkingUpdates: false, error }));
			throw e;
		}
	},

	// ========================================================================
	// Utility
	// ========================================================================

	/**
	 * Clear error state
	 */
	clearError(): void {
		_update((state) => ({ ...state, error: null }));
	},

	/**
	 * Reset store to initial state
	 */
	reset(): void {
		_state = {
			marketplaces: [],
			available: [],
			installed: [],
			fileBasedAgents: [],
			selectedPlugin: null,
			loading: false,
			syncing: {},
			installing: {},
			updating: {},
			updatesAvailable: [],
			autoUpdateSettings: null,
			checkingUpdates: false,
			error: null
		};
		_notifySubscribers();
	}
};

// ============================================================================
// Derived stores (via store-utils createDerived)
// ============================================================================

// All marketplaces
export const marketplaces = createDerived(() => _state.marketplaces);

// All installed plugins
export const installedPlugins = createDerived(() => _state.installed);

// Selected plugin details
export const selectedPlugin = createDerived(() => _state.selectedPlugin);

// Loading state
export const pluginsLoading = createDerived(() => _state.loading);

// Error state
export const pluginsError = createDerived(() => _state.error);

// Syncing state for marketplaces
export const marketplaceSyncing = createDerived(() => _state.syncing);

// Installing state for plugins
export const pluginInstalling = createDerived(() => _state.installing);

// Updating state for plugins
export const pluginUpdating = createDerived(() => _state.updating);

// Available updates
export const updatesAvailable = createDerived(() => _state.updatesAvailable);

// Number of available updates (for badges)
export const updatesCount = createDerived(() => _state.updatesAvailable.length);

// Auto-update settings
export const autoUpdateSettings = createDerived(() => _state.autoUpdateSettings);

// Checking for updates state
export const checkingUpdates = createDerived(() => _state.checkingUpdates);

// Uninstalled available plugins (for Available tab - hides already installed)
export const uninstalledAvailablePlugins = createDerived(() =>
	_state.available.filter((p) => !p.installed)
);

// Stats
export const pluginStats = createDerived(() => ({
	totalMarketplaces: _state.marketplaces.length,
	totalAvailable: _state.available.filter((p) => !p.installed).length, // Only count uninstalled
	totalInstalled: _state.installed.length,
	totalEnabled: _state.installed.filter((p) => p.enabled).length,
	totalFileBasedAgents: _state.fileBasedAgents.length
}));
