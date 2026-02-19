/**
 * Plugin Management Store -- Re-export shim.
 * Implementation lives in plugins.svelte.ts (Svelte 5 runes).
 */
export {
	plugins,
	marketplaces,
	availablePlugins,
	installedPlugins,
	enabledPlugins,
	disabledPlugins,
	pluginsWithCommands,
	pluginsWithAgents,
	pluginsWithSkills,
	pluginsWithHooks,
	fileBasedAgents,
	selectedPlugin,
	pluginsLoading,
	pluginsError,
	marketplaceSyncing,
	pluginInstalling,
	pluginsByMarketplace,
	uninstalledAvailablePlugins,
	uninstalledPluginsByMarketplace,
	pluginStats,
} from './plugins.svelte';

export type { PluginsState } from './plugins.svelte';
