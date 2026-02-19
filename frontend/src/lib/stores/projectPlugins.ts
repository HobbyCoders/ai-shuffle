/**
 * Project Plugins Management Store -- Re-export shim.
 * Implementation lives in projectPlugins.svelte.ts (Svelte 5 runes).
 */
export {
	projectPlugins,
	installedProjectPlugins,
	localPlugins,
	projectScopePlugins,
	enabledProjectPlugins,
	availableProjectPlugins,
	projectPluginsLoading,
	projectPluginsError,
	projectPluginsInstalling,
	projectPluginStats,
} from './projectPlugins.svelte';

export type { ProjectPluginsState } from './projectPlugins.svelte';
