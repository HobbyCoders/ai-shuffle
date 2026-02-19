/**
 * Groups Store — Re-export shim.
 * Implementation lives in groups.svelte.ts (Svelte 5 runes).
 */
export {
	groups,
	organizeByGroups,
	projectGroups,
	profileGroups,
	subagentGroups,
} from './groups.svelte';

export type {
	EntityType,
	GroupDefinition,
	EntityGroups,
	GroupsState,
} from './groups.svelte';
