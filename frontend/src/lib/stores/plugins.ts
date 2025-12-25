/**
 * Plugin Management Store
 *
 * Manages state for Claude Code plugins:
 * - Marketplaces list and management
 * - Available plugins from all marketplaces
 * - Installed plugins
 * - Plugin enable/disable state
 * - File-based agents from plugins
 */

import { writable, derived, get } from 'svelte/store';
import type {
	MarketplaceInfo,
	PluginInfo,
	AvailablePlugin,
	PluginDetails,
	FileBasedAgent
} from '$lib/api/plugins';
import * as pluginsApi from '$lib/api/plugins';

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
	error: null
};

// ============================================================================
// Store Creation
// ============================================================================

function createPluginsStore() {
	const { subscribe, set, update } = writable<PluginsState>(initialState);

	return {
		subscribe,

		// ========================================================================
		// Marketplace Operations
		// ========================================================================

		/**
		 * Load all marketplaces
		 */
		async loadMarketplaces(): Promise<void> {
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const marketplaces = await pluginsApi.getMarketplaces();
				update((state) => ({ ...state, marketplaces, loading: false }));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load marketplaces';
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Add a new marketplace
		 */
		async addMarketplace(url: string, name: string): Promise<MarketplaceInfo> {
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const marketplace = await pluginsApi.addMarketplace(url, name);
				update((state) => ({
					...state,
					marketplaces: [...state.marketplaces, marketplace],
					loading: false
				}));
				return marketplace;
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to add marketplace';
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Remove a marketplace
		 */
		async removeMarketplace(marketplaceId: string): Promise<void> {
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				await pluginsApi.removeMarketplace(marketplaceId);
				update((state) => ({
					...state,
					marketplaces: state.marketplaces.filter((m) => m.id !== marketplaceId),
					available: state.available.filter((p) => p.marketplace !== marketplaceId),
					loading: false
				}));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to remove marketplace';
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Sync a marketplace (git pull)
		 */
		async syncMarketplace(marketplaceId: string): Promise<void> {
			update((state) => ({
				...state,
				syncing: { ...state.syncing, [marketplaceId]: true },
				error: null
			}));

			try {
				await pluginsApi.syncMarketplace(marketplaceId);
				// Reload available plugins after sync
				const available = await pluginsApi.getAvailablePlugins();
				update((state) => ({
					...state,
					available,
					syncing: { ...state.syncing, [marketplaceId]: false }
				}));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to sync marketplace';
				update((state) => ({
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
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const available = await pluginsApi.getAvailablePlugins(marketplace);
				update((state) => ({ ...state, available, loading: false }));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load available plugins';
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Load all installed plugins
		 */
		async loadInstalled(): Promise<void> {
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const installed = await pluginsApi.getInstalledPlugins();
				update((state) => ({ ...state, installed, loading: false }));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load installed plugins';
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Load all data (marketplaces, available, installed)
		 */
		async loadAll(): Promise<void> {
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const [marketplaces, available, installed, fileBasedAgents] = await Promise.all([
					pluginsApi.getMarketplaces(),
					pluginsApi.getAvailablePlugins(),
					pluginsApi.getInstalledPlugins(),
					pluginsApi.getFileBasedAgents()
				]);

				update((state) => ({
					...state,
					marketplaces,
					available,
					installed,
					fileBasedAgents,
					loading: false
				}));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load plugins data';
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Get plugin details
		 */
		async selectPlugin(pluginId: string): Promise<void> {
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const selectedPlugin = await pluginsApi.getPluginDetails(pluginId);
				update((state) => ({ ...state, selectedPlugin, loading: false }));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load plugin details';
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Clear selected plugin
		 */
		clearSelectedPlugin(): void {
			update((state) => ({ ...state, selectedPlugin: null }));
		},

		// ========================================================================
		// Plugin Installation
		// ========================================================================

		/**
		 * Install a single plugin
		 */
		async installPlugin(pluginId: string, scope: string = 'user'): Promise<PluginInfo> {
			update((state) => ({
				...state,
				installing: { ...state.installing, [pluginId]: true },
				error: null
			}));

			try {
				const plugin = await pluginsApi.installPlugin(pluginId, scope);
				update((state) => ({
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
				update((state) => ({
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
			update((state) => ({
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

				update((state) => ({
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
				update((state) => ({
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
			update((state) => ({
				...state,
				installing: { ...state.installing, [pluginId]: true },
				error: null
			}));

			try {
				await pluginsApi.uninstallPlugin(pluginId);
				update((state) => ({
					...state,
					installed: state.installed.filter((p) => p.id !== pluginId),
					available: state.available.map((p) =>
						p.id === pluginId ? { ...p, installed: false, installed_version: null } : p
					),
					installing: { ...state.installing, [pluginId]: false }
				}));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to uninstall plugin';
				update((state) => ({
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
				update((state) => ({
					...state,
					installed: state.installed.map((p) =>
						p.id === pluginId ? { ...p, enabled: true } : p
					)
				}));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to enable plugin';
				update((state) => ({ ...state, error }));
				throw e;
			}
		},

		/**
		 * Disable a plugin globally
		 */
		async disablePlugin(pluginId: string): Promise<void> {
			try {
				await pluginsApi.disablePlugin(pluginId);
				update((state) => ({
					...state,
					installed: state.installed.map((p) =>
						p.id === pluginId ? { ...p, enabled: false } : p
					)
				}));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to disable plugin';
				update((state) => ({ ...state, error }));
				throw e;
			}
		},

		/**
		 * Toggle plugin enabled state
		 */
		async togglePlugin(pluginId: string): Promise<void> {
			const state = get({ subscribe });
			const plugin = state.installed.find((p) => p.id === pluginId);
			if (plugin) {
				if (plugin.enabled) {
					await this.disablePlugin(pluginId);
				} else {
					await this.enablePlugin(pluginId);
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
				update((state) => ({ ...state, fileBasedAgents }));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load file-based agents';
				update((state) => ({ ...state, error }));
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
			update((state) => ({ ...state, error: null }));
		},

		/**
		 * Reset store to initial state
		 */
		reset(): void {
			set(initialState);
		}
	};
}

// ============================================================================
// Export Store & Derived Stores
// ============================================================================

export const plugins = createPluginsStore();

// All marketplaces
export const marketplaces = derived(plugins, ($plugins) => $plugins.marketplaces);

// All available plugins
export const availablePlugins = derived(plugins, ($plugins) => $plugins.available);

// All installed plugins
export const installedPlugins = derived(plugins, ($plugins) => $plugins.installed);

// Enabled plugins only
export const enabledPlugins = derived(plugins, ($plugins) =>
	$plugins.installed.filter((p) => p.enabled)
);

// Disabled plugins only
export const disabledPlugins = derived(plugins, ($plugins) =>
	$plugins.installed.filter((p) => !p.enabled)
);

// Plugins with commands
export const pluginsWithCommands = derived(plugins, ($plugins) =>
	$plugins.installed.filter((p) => p.has_commands)
);

// Plugins with agents
export const pluginsWithAgents = derived(plugins, ($plugins) =>
	$plugins.installed.filter((p) => p.has_agents)
);

// Plugins with skills
export const pluginsWithSkills = derived(plugins, ($plugins) =>
	$plugins.installed.filter((p) => p.has_skills)
);

// Plugins with hooks
export const pluginsWithHooks = derived(plugins, ($plugins) =>
	$plugins.installed.filter((p) => p.has_hooks)
);

// File-based agents
export const fileBasedAgents = derived(plugins, ($plugins) => $plugins.fileBasedAgents);

// Selected plugin details
export const selectedPlugin = derived(plugins, ($plugins) => $plugins.selectedPlugin);

// Loading state
export const pluginsLoading = derived(plugins, ($plugins) => $plugins.loading);

// Error state
export const pluginsError = derived(plugins, ($plugins) => $plugins.error);

// Syncing state for marketplaces
export const marketplaceSyncing = derived(plugins, ($plugins) => $plugins.syncing);

// Installing state for plugins
export const pluginInstalling = derived(plugins, ($plugins) => $plugins.installing);

// Grouped by marketplace
export const pluginsByMarketplace = derived(plugins, ($plugins) => {
	const groups: Record<string, AvailablePlugin[]> = {};
	for (const plugin of $plugins.available) {
		if (!groups[plugin.marketplace]) {
			groups[plugin.marketplace] = [];
		}
		groups[plugin.marketplace].push(plugin);
	}
	return groups;
});

// Stats
export const pluginStats = derived(plugins, ($plugins) => ({
	totalMarketplaces: $plugins.marketplaces.length,
	totalAvailable: $plugins.available.length,
	totalInstalled: $plugins.installed.length,
	totalEnabled: $plugins.installed.filter((p) => p.enabled).length,
	totalFileBasedAgents: $plugins.fileBasedAgents.length
}));
