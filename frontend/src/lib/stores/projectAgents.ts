/**
 * Project Agents Management Store -- Re-export shim.
 * Implementation lives in projectAgents.svelte.ts (Svelte 5 runes).
 */
export {
	projectAgents,
	allProjectAgents,
	selectedProjectAgent,
	projectAgentsLoading,
	projectAgentsSaving,
	projectAgentsError,
	projectAgentCount,
	projectAgentsByModel,
} from './projectAgents.svelte';

export type { ProjectAgentsState } from './projectAgents.svelte';
