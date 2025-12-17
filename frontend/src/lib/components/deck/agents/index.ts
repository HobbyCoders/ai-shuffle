/**
 * Agent Mode Components for "The Deck"
 *
 * Mission Control center for managing background autonomous agents.
 */

// Main views
export { default as AgentsView } from './AgentsView.svelte';
export { default as AgentLauncher } from './AgentLauncher.svelte';
export { default as AgentDetails } from './AgentDetails.svelte';

// List components
export { default as AgentListItem } from './AgentListItem.svelte';

// Visualization components
export { default as TaskTree } from './TaskTree.svelte';
export { default as AgentLogs } from './AgentLogs.svelte';
export { default as AgentStats } from './AgentStats.svelte';

// Re-export types from store for convenience
export type {
	BackgroundAgent,
	AgentTask,
	AgentStatus,
	TaskStatus,
	LaunchAgentOptions
} from '$lib/stores/agents';

// Export launcher config type
export type { LaunchConfig } from './AgentLauncher.svelte';
