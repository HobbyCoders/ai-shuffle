/**
 * Hooks Management Store -- Re-export shim.
 * Implementation lives in hooks.svelte.ts (Svelte 5 runes).
 */
export {
	hooks,
	projectHooks,
	enabledHooks,
	disabledHooks,
	hooksByEvent,
	pluginHooks,
	pluginHooksByPlugin,
	currentProjectId,
	hooksLoading,
	hooksError,
	hookTesting,
	hookSaving,
	hookStats,
} from './hooks.svelte';

export type { HooksState } from './hooks.svelte';
