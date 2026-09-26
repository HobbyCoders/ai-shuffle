/**
 * Project Agents Management Store
 *
 * Manages state for file-based agents in project directories:
 * - Agents stored as .md files in <project>/.fluid/agents/
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
import { getErrorMessage } from '$lib/api/client';
import { createSubscribers } from './store-utils';

// ============================================================================
// Types
// ============================================================================

interface ProjectAgentsState {
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

/** Latest loadAgents call; an older (slower) response is discarded so a
 *  project the user already left cannot overwrite the current list. */
let _loadSeq = 0;

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
		const seq = ++_loadSeq;
		_update((state) => ({ ...state, loading: true, error: null, currentProjectId: projectId }));

		try {
			const agents = await projectAgentsApi.getProjectAgents(projectId);
			if (seq !== _loadSeq) return;
			_update((state) => ({ ...state, agents, loading: false }));
		} catch (e) {
			if (seq !== _loadSeq) return;
			const error = getErrorMessage(e, 'Failed to load project agents');
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
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
			const error = getErrorMessage(e, 'Failed to create agent');
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
			const error = getErrorMessage(e, 'Failed to delete agent');
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
		_loadSeq++;
		_state = { ...initialState };
		_notifySubscribers();
	}
};

// ============================================================================
// Derived stores (via store-utils createDerived)
// ============================================================================

// All agents
export const allProjectAgents = createDerived(() => _state.agents);

// Loading state
export const projectAgentsLoading = createDerived(() => _state.loading);

// Error state
export const projectAgentsError = createDerived(() => _state.error);

// Agent count
export const projectAgentCount = createDerived(() => _state.agents.length);
