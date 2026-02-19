/**
 * App Data Store — Re-export shim.
 * Implementation lives in app-data.svelte.ts (Svelte 5 runes).
 */
export {
	appData,
	profiles,
	projects,
	sessions,
	selectedSessionIds,
	selectionMode,
	sessionsLoading,
	sessionsTagFilter,
	apiUserSessions,
} from './app-data.svelte';

export type {
	Project,
} from './app-data.svelte';
