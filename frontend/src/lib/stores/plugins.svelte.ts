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
import { untrack } from 'svelte';

// ============================================================================
// Types
// ============================================================================

export interface PluginsState {
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
// Subscribe shim
// ============================================================================

type Subscriber<T> = (value: T) => void;

const _subscribers = new Set<Subscriber<PluginsState>>();

function _notifySubscribers() {
	untrack(() => { for (const fn of _subscribers) fn(_state); });
}

function _update(fn: (s: PluginsState) => PluginsState) {
	_state = fn(untrack(() => _state));
	_notifySubscribers();
}

// ============================================================================
// Exported Store
// ============================================================================

export const plugins = {
	get current() {
		return _state;
	},

	subscribe(fn: Subscriber<PluginsState>) {
		_subscribers.add(fn);
		untrack(() => fn(_state));
		return () => {
			_subscribers.delete(fn);
		};
	},

	// ========================================================================
	// Marketplace Operations
	// ========================================================================

	/**
	 * Load all marketplaces
	 */
	async loadMarketplaces(): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const marketplaces = await pluginsApi.getMarketplaces();
			_update((state) => ({ ...state, marketplaces, loading: false }));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load marketplaces';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Add a new marketplace
	 */
	async addMarketplace(url: string, name: string): Promise<MarketplaceInfo> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const marketplace = await pluginsApi.addMarketplace(url, name);
			_update((state) => ({
				...state,
				marketplaces: [...state.marketplaces, marketplace],
				loading: false
			}));
			return marketplace;
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to add marketplace';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Remove a marketplace
	 */
	async removeMarketplace(marketplaceId: string): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			await pluginsApi.removeMarketplace(marketplaceId);
			_update((state) => ({
				...state,
				marketplaces: state.marketplaces.filter((m) => m.id !== marketplaceId),
				available: state.available.filter((p) => p.marketplace !== marketplaceId),
				loading: false
			}));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to remove marketplace';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
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
			const error = e instanceof Error ? e.message : 'Failed to sync marketplace';
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
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const available = await pluginsApi.getAvailablePlugins(marketplace);
			_update((state) => ({ ...state, available, loading: false }));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load available plugins';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Load all installed plugins
	 */
	async loadInstalled(): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const installed = await pluginsApi.getInstalledPlugins();
			_update((state) => ({ ...state, installed, loading: false }));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load installed plugins';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Load all data (marketplaces, available, installed)
	 */
	async loadAll(): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
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
				autoUpdateSettings,
				loading: false
			}));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load plugins data';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Get plugin details
	 */
	async selectPlugin(pluginId: string): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const selectedPlugin = await pluginsApi.getPluginDetails(pluginId);
			_update((state) => ({ ...state, selectedPlugin, loading: false }));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load plugin details';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
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
				installed: [...state.installed, plugin],
				available: state.available.map((p) =>
					p.id === pluginId ? { ...p, installed: true, installed_version: plugin.version } : p
				),
				installing: { ...state.installing, [pluginId]: false }
			}));
			return plugin;
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to install plugin';
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
			const installedIds = new Set(plugins.map((p) => p.id));

			_update((state) => ({
				...state,
				installed: [...state.installed, ...plugins],
				available: state.available.map((p) =>
					installedIds.has(p.id)
						? { ...p, installed: true, installed_version: plugins.find((ip) => ip.id === p.id)?.version || null }
						: p
				),
				installing: {
					...state.installing,
					...pluginIds.reduce((acc, id) => ({ ...acc, [id]: false }), {})
				}
			}));
			return plugins;
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to install plugins';
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
					p.id === pluginId ? { ...p, installed: false, installed_version: null } : p
				),
				installing: { ...state.installing, [pluginId]: false }
			}));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to uninstall plugin';
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
			const error = e instanceof Error ? e.message : 'Failed to enable plugin';
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
			const error = e instanceof Error ? e.message : 'Failed to disable plugin';
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

	/**
	 * Load file-based agents from enabled plugins
	 */
	async loadFileBasedAgents(): Promise<void> {
		try {
			const fileBasedAgents = await pluginsApi.getFileBasedAgents();
			_update((state) => ({ ...state, fileBasedAgents }));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load file-based agents';
			_update((state) => ({ ...state, error }));
			throw e;
		}
	},

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
			const error = e instanceof Error ? e.message : 'Failed to check for updates';
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
			_update((state) => ({
				...state,
				installed: state.installed.map((p) =>
					p.id === pluginId ? updated : p
				),
				updatesAvailable: state.updatesAvailable.filter((u) => u.plugin_id !== pluginId),
				updating: { ...state.updating, [pluginId]: false }
			}));
			return updated;
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to update plugin';
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
			const updatedIds = new Set(updatedPlugins.map((p) => p.id));

			_update((state) => ({
				...state,
				installed: state.installed.map((p) => {
					const updated = updatedPlugins.find((up) => up.id === p.id);
					return updated || p;
				}),
				updatesAvailable: state.updatesAvailable.filter((u) => !updatedIds.has(u.plugin_id)),
				updating: {
					...state.updating,
					...updateIds.reduce((acc, id) => ({ ...acc, [id]: false }), {})
				}
			}));
			return updatedPlugins;
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to update plugins';
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
	 * Load auto-update settings
	 */
	async loadAutoUpdateSettings(): Promise<void> {
		try {
			const autoUpdateSettings = await pluginsApi.getAutoUpdateSettings();
			_update((state) => ({ ...state, autoUpdateSettings }));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load auto-update settings';
			_update((state) => ({ ...state, error }));
			throw e;
		}
	},

	/**
	 * Update auto-update settings
	 */
	async updateAutoUpdateSettings(settings: Partial<AutoUpdateSettings>): Promise<void> {
		try {
			const autoUpdateSettings = await pluginsApi.updateAutoUpdateSettings(settings);
			_update((state) => ({ ...state, autoUpdateSettings }));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to update auto-update settings';
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
			const error = e instanceof Error ? e.message : 'Failed to run auto-update';
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
// Derived stores with subscribe shim
// ============================================================================

function createDerived<T>(getter: () => T) {
	return {
		get current() {
			return getter();
		},
		subscribe(fn: Subscriber<T>) {
			const handler: Subscriber<PluginsState> = () => fn(getter());
			_subscribers.add(handler);
			untrack(() => fn(getter()));
			return () => {
				_subscribers.delete(handler);
			};
		}
	};
}

// All marketplaces
export const marketplaces = createDerived(() => _state.marketplaces);

// All available plugins
export const availablePlugins = createDerived(() => _state.available);

// All installed plugins
export const installedPlugins = createDerived(() => _state.installed);

// Enabled plugins only
export const enabledPlugins = createDerived(() =>
	_state.installed.filter((p) => p.enabled)
);

// Disabled plugins only
export const disabledPlugins = createDerived(() =>
	_state.installed.filter((p) => !p.enabled)
);

// Plugins with commands
export const pluginsWithCommands = createDerived(() =>
	_state.installed.filter((p) => p.has_commands)
);

// Plugins with agents
export const pluginsWithAgents = createDerived(() =>
	_state.installed.filter((p) => p.has_agents)
);

// Plugins with skills
export const pluginsWithSkills = createDerived(() =>
	_state.installed.filter((p) => p.has_skills)
);

// Plugins with hooks
export const pluginsWithHooks = createDerived(() =>
	_state.installed.filter((p) => p.has_hooks)
);

// File-based agents
export const fileBasedAgents = createDerived(() => _state.fileBasedAgents);

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

// Grouped by marketplace (all available)
export const pluginsByMarketplace = createDerived(() => {
	const groups: Record<string, AvailablePlugin[]> = {};
	for (const plugin of _state.available) {
		if (!groups[plugin.marketplace]) {
			groups[plugin.marketplace] = [];
		}
		groups[plugin.marketplace].push(plugin);
	}
	return groups;
});

// Uninstalled available plugins (for Available tab - hides already installed)
export const uninstalledAvailablePlugins = createDerived(() =>
	_state.available.filter((p) => !p.installed)
);

// Grouped by marketplace (uninstalled only - for Available tab)
export const uninstalledPluginsByMarketplace = createDerived(() => {
	const groups: Record<string, AvailablePlugin[]> = {};
	for (const plugin of _state.available) {
		if (plugin.installed) continue; // Skip installed plugins
		if (!groups[plugin.marketplace]) {
			groups[plugin.marketplace] = [];
		}
		groups[plugin.marketplace].push(plugin);
	}
	return groups;
});

// Stats
export const pluginStats = createDerived(() => ({
	totalMarketplaces: _state.marketplaces.length,
	totalAvailable: _state.available.filter((p) => !p.installed).length, // Only count uninstalled
	totalInstalled: _state.installed.length,
	totalEnabled: _state.installed.filter((p) => p.enabled).length,
	totalFileBasedAgents: _state.fileBasedAgents.length
}));
