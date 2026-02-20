/**
 * Project Plugins Management Store
 *
 * Manages state for project-scoped plugins:
 * - LOCAL scope: <project>/.claude/plugins/ (gitignored)
 * - PROJECT scope: <project>/plugins/ (committed to git)
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import type { ProjectPluginInfo, AvailablePluginInfo } from '$lib/api/projectPlugins';
import * as projectPluginsApi from '$lib/api/projectPlugins';
import { hooks } from './hooks';

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
// Reactive State
// ============================================================================

let _state = $state<ProjectPluginsState>({ ...initialState });

// ============================================================================
// Subscribe Shim
// ============================================================================

type Subscriber<T> = (value: T) => void;

const _subscribers = new Set<Subscriber<ProjectPluginsState>>();

function _notifySubscribers() {
	for (const fn of _subscribers) {
		fn(_state);
	}
}

function _update(fn: (s: ProjectPluginsState) => ProjectPluginsState) {
	_state = fn(_state);
	_notifySubscribers();
}

// ============================================================================
// Exported Store
// ============================================================================

export const projectPlugins = {
	get current() {
		return _state;
	},

	subscribe(fn: Subscriber<ProjectPluginsState>) {
		_subscribers.add(fn);
		fn(_state);
		return () => {
			_subscribers.delete(fn);
		};
	},

	// ========================================================================
	// Plugin Loading
	// ========================================================================

	/**
	 * Load installed plugins for a project
	 */
	async loadPlugins(projectId: string): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null, currentProjectId: projectId }));

		try {
			const plugins = await projectPluginsApi.getProjectPlugins(projectId);
			_update((state) => ({ ...state, plugins, loading: false }));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load project plugins';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Load available plugins from marketplaces
	 */
	async loadAvailablePlugins(projectId: string, marketplace?: string): Promise<void> {
		_update((state) => ({ ...state, loadingAvailable: true, error: null }));

		try {
			const availablePlugins = await projectPluginsApi.getAvailablePlugins(projectId, marketplace);
			_update((state) => ({ ...state, availablePlugins, loadingAvailable: false }));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load available plugins';
			_update((state) => ({ ...state, error, loadingAvailable: false }));
			throw e;
		}
	},

	/**
	 * Load both installed and available plugins
	 */
	async loadAll(projectId: string): Promise<void> {
		_update((state) => ({
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

			_update((state) => ({
				...state,
				plugins,
				availablePlugins,
				loading: false,
				loadingAvailable: false
			}));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load plugins';
			_update((state) => ({ ...state, error, loading: false, loadingAvailable: false }));
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
		_update((state) => ({
			...state,
			installing: { ...state.installing, [pluginId]: true },
			error: null
		}));

		try {
			const plugin = await projectPluginsApi.installProjectPlugin(projectId, pluginId, scope);
			_update((state) => ({
				...state,
				plugins: [...state.plugins, plugin],
				availablePlugins: state.availablePlugins.map((p) =>
					p.id === pluginId ? { ...p, installed: true } : p
				),
				installing: { ...state.installing, [pluginId]: false }
			}));

			// Refresh hooks since plugin may have hooks
			hooks.loadAll(projectId);

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
	 * Uninstall a plugin from the project
	 */
	async uninstallPlugin(projectId: string, pluginId: string): Promise<void> {
		_update((state) => ({
			...state,
			installing: { ...state.installing, [pluginId]: true },
			error: null
		}));

		try {
			await projectPluginsApi.uninstallProjectPlugin(projectId, pluginId);

			// Get the plugin name to update available status
			const plugin = _state.plugins.find((p) => p.id === pluginId);
			const pluginName = plugin?.name;

			_update((state) => ({
				...state,
				plugins: state.plugins.filter((p) => p.id !== pluginId),
				availablePlugins: state.availablePlugins.map((p) =>
					p.name === pluginName ? { ...p, installed: false } : p
				),
				installing: { ...state.installing, [pluginId]: false }
			}));

			// Refresh hooks since plugin hooks may have changed
			hooks.loadAll(projectId);
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
	 * Enable a plugin
	 */
	async enablePlugin(projectId: string, pluginId: string): Promise<void> {
		try {
			await projectPluginsApi.enableProjectPlugin(projectId, pluginId);
			_update((state) => ({
				...state,
				plugins: state.plugins.map((p) =>
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
	 * Disable a plugin
	 */
	async disablePlugin(projectId: string, pluginId: string): Promise<void> {
		try {
			await projectPluginsApi.disableProjectPlugin(projectId, pluginId);
			_update((state) => ({
				...state,
				plugins: state.plugins.map((p) =>
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
	async togglePlugin(projectId: string, pluginId: string): Promise<void> {
		const plugin = _state.plugins.find((p) => p.id === pluginId);
		if (plugin) {
			if (plugin.enabled) {
				await projectPlugins.disablePlugin(projectId, pluginId);
			} else {
				await projectPlugins.enablePlugin(projectId, pluginId);
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
		_update((state) => ({ ...state, error: null }));
	},

	/**
	 * Reset store to initial state
	 */
	reset(): void {
		_state = { ...initialState };
		_notifySubscribers();
	}
};

// ============================================================================
// Derived Stores
// ============================================================================

function createDerived<T>(getter: () => T) {
	return {
		get current() {
			return getter();
		},
		subscribe(fn: Subscriber<T>) {
			const handler: Subscriber<ProjectPluginsState> = () => fn(getter());
			_subscribers.add(handler);
			fn(getter());
			return () => {
				_subscribers.delete(handler);
			};
		}
	};
}

// All installed plugins
export const installedProjectPlugins = createDerived(() => _state.plugins);

// Local scope plugins
export const localPlugins = createDerived(() =>
	_state.plugins.filter((p) => p.scope === 'local')
);

// Project scope plugins
export const projectScopePlugins = createDerived(() =>
	_state.plugins.filter((p) => p.scope === 'project')
);

// Enabled plugins
export const enabledProjectPlugins = createDerived(() =>
	_state.plugins.filter((p) => p.enabled)
);

// Available plugins (not installed)
export const availableProjectPlugins = createDerived(() =>
	_state.availablePlugins.filter((p) => !p.installed)
);

// Loading state
export const projectPluginsLoading = createDerived(() => _state.loading);

// Error state
export const projectPluginsError = createDerived(() => _state.error);

// Installing state
export const projectPluginsInstalling = createDerived(() => _state.installing);

// Stats
export const projectPluginStats = createDerived(() => ({
	totalInstalled: _state.plugins.length,
	localCount: _state.plugins.filter((p) => p.scope === 'local').length,
	projectCount: _state.plugins.filter((p) => p.scope === 'project').length,
	enabledCount: _state.plugins.filter((p) => p.enabled).length,
	availableCount: _state.availablePlugins.filter((p) => !p.installed).length
}));
