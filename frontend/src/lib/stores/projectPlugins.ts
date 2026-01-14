/**
 * Project Plugins Management Store
 *
 * Manages state for project-scoped plugins:
 * - LOCAL scope: <project>/.claude/plugins/ (gitignored)
 * - PROJECT scope: <project>/plugins/ (committed to git)
 */

import { writable, derived, get } from 'svelte/store';
import type { ProjectPluginInfo, AvailablePluginInfo } from '$lib/api/projectPlugins';
import * as projectPluginsApi from '$lib/api/projectPlugins';

// ============================================================================
// Types
// ============================================================================

export interface ProjectPluginsState {
	plugins: ProjectPluginInfo[];
	availablePlugins: AvailablePluginInfo[];
	currentProjectId: string | null;
	loading: boolean;
	loadingAvailable: boolean;
	installing: Record<string, boolean>; // plugin id -> installing state
	error: string | null;
}

// ============================================================================
// Initial State
// ============================================================================

const initialState: ProjectPluginsState = {
	plugins: [],
	availablePlugins: [],
	currentProjectId: null,
	loading: false,
	loadingAvailable: false,
	installing: {},
	error: null
};

// ============================================================================
// Store Creation
// ============================================================================

function createProjectPluginsStore() {
	const { subscribe, set, update } = writable<ProjectPluginsState>(initialState);

	return {
		subscribe,

		// ========================================================================
		// Plugin Loading
		// ========================================================================

		/**
		 * Load installed plugins for a project
		 */
		async loadPlugins(projectId: string): Promise<void> {
			update((state) => ({ ...state, loading: true, error: null, currentProjectId: projectId }));

			try {
				const plugins = await projectPluginsApi.getProjectPlugins(projectId);
				update((state) => ({ ...state, plugins, loading: false }));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load project plugins';
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Load available plugins from marketplaces
		 */
		async loadAvailablePlugins(projectId: string, marketplace?: string): Promise<void> {
			update((state) => ({ ...state, loadingAvailable: true, error: null }));

			try {
				const availablePlugins = await projectPluginsApi.getAvailablePlugins(projectId, marketplace);
				update((state) => ({ ...state, availablePlugins, loadingAvailable: false }));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load available plugins';
				update((state) => ({ ...state, error, loadingAvailable: false }));
				throw e;
			}
		},

		/**
		 * Load both installed and available plugins
		 */
		async loadAll(projectId: string): Promise<void> {
			update((state) => ({
				...state,
				loading: true,
				loadingAvailable: true,
				error: null,
				currentProjectId: projectId
			}));

			try {
				const [plugins, availablePlugins] = await Promise.all([
					projectPluginsApi.getProjectPlugins(projectId),
					projectPluginsApi.getAvailablePlugins(projectId)
				]);

				update((state) => ({
					...state,
					plugins,
					availablePlugins,
					loading: false,
					loadingAvailable: false
				}));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load plugins';
				update((state) => ({ ...state, error, loading: false, loadingAvailable: false }));
				throw e;
			}
		},

		// ========================================================================
		// Plugin Installation
		// ========================================================================

		/**
		 * Install a plugin to the project
		 */
		async installPlugin(
			projectId: string,
			pluginId: string,
			scope: 'local' | 'project' = 'local'
		): Promise<ProjectPluginInfo> {
			update((state) => ({
				...state,
				installing: { ...state.installing, [pluginId]: true },
				error: null
			}));

			try {
				const plugin = await projectPluginsApi.installProjectPlugin(projectId, pluginId, scope);
				update((state) => ({
					...state,
					plugins: [...state.plugins, plugin],
					availablePlugins: state.availablePlugins.map((p) =>
						p.id === pluginId ? { ...p, installed: true } : p
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
		 * Uninstall a plugin from the project
		 */
		async uninstallPlugin(projectId: string, pluginId: string): Promise<void> {
			update((state) => ({
				...state,
				installing: { ...state.installing, [pluginId]: true },
				error: null
			}));

			try {
				await projectPluginsApi.uninstallProjectPlugin(projectId, pluginId);

				// Get the plugin name to update available status
				const plugin = get({ subscribe }).plugins.find((p) => p.id === pluginId);
				const pluginName = plugin?.name;

				update((state) => ({
					...state,
					plugins: state.plugins.filter((p) => p.id !== pluginId),
					availablePlugins: state.availablePlugins.map((p) =>
						p.name === pluginName ? { ...p, installed: false } : p
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
		 * Enable a plugin
		 */
		async enablePlugin(projectId: string, pluginId: string): Promise<void> {
			try {
				await projectPluginsApi.enableProjectPlugin(projectId, pluginId);
				update((state) => ({
					...state,
					plugins: state.plugins.map((p) =>
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
		 * Disable a plugin
		 */
		async disablePlugin(projectId: string, pluginId: string): Promise<void> {
			try {
				await projectPluginsApi.disableProjectPlugin(projectId, pluginId);
				update((state) => ({
					...state,
					plugins: state.plugins.map((p) =>
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
		async togglePlugin(projectId: string, pluginId: string): Promise<void> {
			const state = get({ subscribe });
			const plugin = state.plugins.find((p) => p.id === pluginId);
			if (plugin) {
				if (plugin.enabled) {
					await this.disablePlugin(projectId, pluginId);
				} else {
					await this.enablePlugin(projectId, pluginId);
				}
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

export const projectPlugins = createProjectPluginsStore();

// All installed plugins
export const installedProjectPlugins = derived(projectPlugins, ($state) => $state.plugins);

// Local scope plugins
export const localPlugins = derived(projectPlugins, ($state) =>
	$state.plugins.filter((p) => p.scope === 'local')
);

// Project scope plugins
export const projectScopePlugins = derived(projectPlugins, ($state) =>
	$state.plugins.filter((p) => p.scope === 'project')
);

// Enabled plugins
export const enabledProjectPlugins = derived(projectPlugins, ($state) =>
	$state.plugins.filter((p) => p.enabled)
);

// Available plugins (not installed)
export const availableProjectPlugins = derived(projectPlugins, ($state) =>
	$state.availablePlugins.filter((p) => !p.installed)
);

// Loading state
export const projectPluginsLoading = derived(projectPlugins, ($state) => $state.loading);

// Error state
export const projectPluginsError = derived(projectPlugins, ($state) => $state.error);

// Installing state
export const projectPluginsInstalling = derived(projectPlugins, ($state) => $state.installing);

// Stats
export const projectPluginStats = derived(projectPlugins, ($state) => ({
	totalInstalled: $state.plugins.length,
	localCount: $state.plugins.filter((p) => p.scope === 'local').length,
	projectCount: $state.plugins.filter((p) => p.scope === 'project').length,
	enabledCount: $state.plugins.filter((p) => p.enabled).length,
	availableCount: $state.availablePlugins.filter((p) => !p.installed).length
}));
