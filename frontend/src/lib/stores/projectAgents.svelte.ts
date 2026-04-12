/**
 * Project Agents Management Store
 *
 * Manages state for file-based agents in project directories:
 * - Agents stored as .md files in <project>/.claude/agents/
 * - YAML frontmatter for metadata (description, model, tools)
 *
 * Migrated to Svelte 5 runes.
 */

import type {
	ProjectAgent,
	ProjectAgentDetail,
	CreateProjectAgentRequest,
	UpdateProjectAgentRequest
} from '$lib/api/projectAgents';
import * as projectAgentsApi from '$lib/api/projectAgents';
import { createSubscribers } from './store-utils';

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
// Reactive State
// ============================================================================

let _state = $state<ProjectAgentsState>({ ...initialState });

// ============================================================================
// Subscribe shim (centralized via store-utils)
// ============================================================================

const { subscribe, update: _update, notifySubscribers: _notifySubscribers, createDerived } =
	createSubscribers<ProjectAgentsState>(
		() => _state,
		(v) => { _state = v; }
	);

// ============================================================================
// Exported Store
// ============================================================================

export const projectAgents = {
	get current() {
		return _state;
	},

	subscribe,

	// ========================================================================
	// Agent Loading
	// ========================================================================

	/**
	 * Load all agents for a project
	 */
	async loadAgents(projectId: string): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null, currentProjectId: projectId }));

		try {
			const agents = await projectAgentsApi.getProjectAgents(projectId);
			_update((state) => ({ ...state, agents, loading: false }));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load project agents';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Load a specific agent with full details
	 */
	async selectAgent(projectId: string, agentName: string): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const selectedAgent = await projectAgentsApi.getProjectAgent(projectId, agentName);
			_update((state) => ({ ...state, selectedAgent, loading: false }));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to load agent details';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Clear selected agent
	 */
	clearSelectedAgent(): void {
		_update((state) => ({ ...state, selectedAgent: null }));
	},

	// ========================================================================
	// Agent CRUD Operations
	// ========================================================================

	/**
	 * Create a new agent
	 */
	async createAgent(projectId: string, data: CreateProjectAgentRequest): Promise<ProjectAgent> {
		_update((state) => ({ ...state, saving: true, error: null }));

		try {
			const agent = await projectAgentsApi.createProjectAgent(projectId, data);
			_update((state) => ({
				...state,
				agents: [...state.agents, agent].sort((a, b) =>
					a.name.toLowerCase().localeCompare(b.name.toLowerCase())
				),
				saving: false
			}));
			return agent;
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to create agent';
			_update((state) => ({ ...state, error, saving: false }));
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
		_update((state) => ({ ...state, saving: true, error: null }));

		try {
			const agent = await projectAgentsApi.updateProjectAgent(projectId, agentName, data);
			_update((state) => ({
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
			_update((state) => ({ ...state, error, saving: false }));
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
		_update((state) => ({ ...state, saving: true, error: null }));

		try {
			const agent = await projectAgentsApi.renameProjectAgent(projectId, oldName, newName);
			_update((state) => ({
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
			_update((state) => ({ ...state, error, saving: false }));
			throw e;
		}
	},

	/**
	 * Delete an agent
	 */
	async deleteAgent(projectId: string, agentName: string): Promise<void> {
		_update((state) => ({ ...state, saving: true, error: null }));

		try {
			await projectAgentsApi.deleteProjectAgent(projectId, agentName);
			_update((state) => ({
				...state,
				agents: state.agents.filter((a) => a.name !== agentName),
				selectedAgent:
					state.selectedAgent?.name === agentName ? null : state.selectedAgent,
				saving: false
			}));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to delete agent';
			_update((state) => ({ ...state, error, saving: false }));
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
// Derived stores (via store-utils createDerived)
// ============================================================================

// All agents
export const allProjectAgents = createDerived(() => _state.agents);

// Selected agent detail
export const selectedProjectAgent = createDerived(() => _state.selectedAgent);

// Loading state
export const projectAgentsLoading = createDerived(() => _state.loading);

// Saving state
export const projectAgentsSaving = createDerived(() => _state.saving);

// Error state
export const projectAgentsError = createDerived(() => _state.error);

// Agent count
export const projectAgentCount = createDerived(() => _state.agents.length);

// Agents grouped by model
export const projectAgentsByModel = createDerived(() => {
	const groups: Record<string, ProjectAgent[]> = { 'No model': [] };
	for (const agent of _state.agents) {
		const model = agent.model || 'No model';
		if (!groups[model]) {
			groups[model] = [];
		}
		groups[model].push(agent);
	}
	return groups;
});
