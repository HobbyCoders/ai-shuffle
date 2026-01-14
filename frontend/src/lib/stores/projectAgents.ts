/**
 * Project Agents Management Store
 *
 * Manages state for file-based agents in project directories:
 * - Agents stored as .md files in <project>/.claude/agents/
 * - YAML frontmatter for metadata (description, model, tools)
 */

import { writable, derived, get } from 'svelte/store';
import type {
	ProjectAgent,
	ProjectAgentDetail,
	CreateProjectAgentRequest,
	UpdateProjectAgentRequest
} from '$lib/api/projectAgents';
import * as projectAgentsApi from '$lib/api/projectAgents';

// ============================================================================
// Types
// ============================================================================

export interface ProjectAgentsState {
	agents: ProjectAgent[];
	selectedAgent: ProjectAgentDetail | null;
	currentProjectId: string | null;
	loading: boolean;
	saving: boolean;
	error: string | null;
}

// ============================================================================
// Initial State
// ============================================================================

const initialState: ProjectAgentsState = {
	agents: [],
	selectedAgent: null,
	currentProjectId: null,
	loading: false,
	saving: false,
	error: null
};

// ============================================================================
// Store Creation
// ============================================================================

function createProjectAgentsStore() {
	const { subscribe, set, update } = writable<ProjectAgentsState>(initialState);

	return {
		subscribe,

		// ========================================================================
		// Agent Loading
		// ========================================================================

		/**
		 * Load all agents for a project
		 */
		async loadAgents(projectId: string): Promise<void> {
			update((state) => ({ ...state, loading: true, error: null, currentProjectId: projectId }));

			try {
				const agents = await projectAgentsApi.getProjectAgents(projectId);
				update((state) => ({ ...state, agents, loading: false }));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load project agents';
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Load a specific agent with full details
		 */
		async selectAgent(projectId: string, agentName: string): Promise<void> {
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const selectedAgent = await projectAgentsApi.getProjectAgent(projectId, agentName);
				update((state) => ({ ...state, selectedAgent, loading: false }));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to load agent details';
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Clear selected agent
		 */
		clearSelectedAgent(): void {
			update((state) => ({ ...state, selectedAgent: null }));
		},

		// ========================================================================
		// Agent CRUD Operations
		// ========================================================================

		/**
		 * Create a new agent
		 */
		async createAgent(projectId: string, data: CreateProjectAgentRequest): Promise<ProjectAgent> {
			update((state) => ({ ...state, saving: true, error: null }));

			try {
				const agent = await projectAgentsApi.createProjectAgent(projectId, data);
				update((state) => ({
					...state,
					agents: [...state.agents, agent].sort((a, b) =>
						a.name.toLowerCase().localeCompare(b.name.toLowerCase())
					),
					saving: false
				}));
				return agent;
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to create agent';
				update((state) => ({ ...state, error, saving: false }));
				throw e;
			}
		},

		/**
		 * Update an existing agent
		 */
		async updateAgent(
			projectId: string,
			agentName: string,
			data: UpdateProjectAgentRequest
		): Promise<ProjectAgent> {
			update((state) => ({ ...state, saving: true, error: null }));

			try {
				const agent = await projectAgentsApi.updateProjectAgent(projectId, agentName, data);
				update((state) => ({
					...state,
					agents: state.agents.map((a) => (a.name === agentName ? agent : a)),
					selectedAgent:
						state.selectedAgent?.name === agentName
							? { ...state.selectedAgent, ...agent, ...data }
							: state.selectedAgent,
					saving: false
				}));
				return agent;
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to update agent';
				update((state) => ({ ...state, error, saving: false }));
				throw e;
			}
		},

		/**
		 * Rename an agent
		 */
		async renameAgent(
			projectId: string,
			oldName: string,
			newName: string
		): Promise<ProjectAgent> {
			update((state) => ({ ...state, saving: true, error: null }));

			try {
				const agent = await projectAgentsApi.renameProjectAgent(projectId, oldName, newName);
				update((state) => ({
					...state,
					agents: state.agents
						.map((a) => (a.name === oldName ? agent : a))
						.sort((a, b) => a.name.toLowerCase().localeCompare(b.name.toLowerCase())),
					selectedAgent:
						state.selectedAgent?.name === oldName
							? { ...state.selectedAgent, name: newName, file_path: agent.file_path }
							: state.selectedAgent,
					saving: false
				}));
				return agent;
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to rename agent';
				update((state) => ({ ...state, error, saving: false }));
				throw e;
			}
		},

		/**
		 * Delete an agent
		 */
		async deleteAgent(projectId: string, agentName: string): Promise<void> {
			update((state) => ({ ...state, saving: true, error: null }));

			try {
				await projectAgentsApi.deleteProjectAgent(projectId, agentName);
				update((state) => ({
					...state,
					agents: state.agents.filter((a) => a.name !== agentName),
					selectedAgent:
						state.selectedAgent?.name === agentName ? null : state.selectedAgent,
					saving: false
				}));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to delete agent';
				update((state) => ({ ...state, error, saving: false }));
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

export const projectAgents = createProjectAgentsStore();

// All agents
export const allProjectAgents = derived(projectAgents, ($state) => $state.agents);

// Selected agent detail
export const selectedProjectAgent = derived(projectAgents, ($state) => $state.selectedAgent);

// Loading state
export const projectAgentsLoading = derived(projectAgents, ($state) => $state.loading);

// Saving state
export const projectAgentsSaving = derived(projectAgents, ($state) => $state.saving);

// Error state
export const projectAgentsError = derived(projectAgents, ($state) => $state.error);

// Agent count
export const projectAgentCount = derived(projectAgents, ($state) => $state.agents.length);

// Agents grouped by model
export const projectAgentsByModel = derived(projectAgents, ($state) => {
	const groups: Record<string, ProjectAgent[]> = { 'No model': [] };
	for (const agent of $state.agents) {
		const model = agent.model || 'No model';
		if (!groups[model]) {
			groups[model] = [];
		}
		groups[model].push(agent);
	}
	return groups;
});
