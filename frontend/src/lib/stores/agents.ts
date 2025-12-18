/**
 * Background Agents Store
 *
 * Manages background AI agents that run autonomously:
 * - Agent lifecycle (launch, pause, resume, cancel)
 * - Task tracking and progress
 * - Log streaming
 * - Branch management for git operations
 */

import { writable, derived, get } from 'svelte/store';

// ============================================================================
// Types
// ============================================================================

export type AgentStatus = 'queued' | 'running' | 'paused' | 'completed' | 'failed';
export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'failed';

export interface AgentTask {
	id: string;
	name: string;
	status: TaskStatus;
	children?: AgentTask[];
}

export interface BackgroundAgent {
	id: string;
	name: string;
	prompt: string;
	status: AgentStatus;
	progress: number; // 0-100
	branch?: string;
	tasks: AgentTask[];
	logs: string[];
	startedAt: Date;
	completedAt?: Date;
	error?: string;
}

interface AgentsState {
	agents: BackgroundAgent[];
	maxConcurrent: number;
}

// ============================================================================
// Constants
// ============================================================================

const STORAGE_KEY = 'background_agents';
const MAX_LOGS_PER_AGENT = 1000;

// ============================================================================
// Persistence Helpers
// ============================================================================

interface PersistedAgent {
	id: string;
	name: string;
	prompt: string;
	status: AgentStatus;
	progress: number;
	branch?: string;
	tasks: AgentTask[];
	logs: string[];
	startedAt: string;
	completedAt?: string;
	error?: string;
}

function loadFromStorage(): BackgroundAgent[] {
	if (typeof window === 'undefined') return [];

	try {
		const stored = localStorage.getItem(STORAGE_KEY);
		if (stored) {
			const parsed: PersistedAgent[] = JSON.parse(stored);
			return parsed.map((a) => ({
				...a,
				startedAt: new Date(a.startedAt),
				completedAt: a.completedAt ? new Date(a.completedAt) : undefined
			}));
		}
	} catch (e) {
		console.error('[Agents] Failed to load from localStorage:', e);
	}
	return [];
}

function saveToStorage(agents: BackgroundAgent[]) {
	if (typeof window === 'undefined') return;

	try {
		const persisted: PersistedAgent[] = agents.map((a) => ({
			...a,
			startedAt: a.startedAt.toISOString(),
			completedAt: a.completedAt?.toISOString()
		}));
		localStorage.setItem(STORAGE_KEY, JSON.stringify(persisted));
	} catch (e) {
		console.error('[Agents] Failed to save to localStorage:', e);
	}
}

// ============================================================================
// Helpers
// ============================================================================

function generateAgentId(): string {
	return `agent-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
}

function generateTaskId(): string {
	return `task-${Date.now()}-${Math.random().toString(36).substr(2, 6)}`;
}

// ============================================================================
// Store Creation
// ============================================================================

function createAgentsStore() {
	const initialAgents = loadFromStorage();

	const { subscribe, set, update } = writable<AgentsState>({
		agents: initialAgents,
		maxConcurrent: 3
	});

	/**
	 * Helper to update state and persist
	 */
	function updateAndPersist(updater: (state: AgentsState) => AgentsState): void {
		update((state) => {
			const newState = updater(state);
			saveToStorage(newState.agents);
			return newState;
		});
	}

	return {
		subscribe,

		// ========================================================================
		// Agent Lifecycle
		// ========================================================================

		/**
		 * Launch a new background agent
		 */
		launchAgent(
			name: string,
			prompt: string,
			options?: {
				branch?: string;
				tasks?: Array<{ name: string; children?: Array<{ name: string }> }>;
			}
		): string {
			const agentId = generateAgentId();

			// Convert task definitions to AgentTask objects
			const tasks: AgentTask[] = (options?.tasks || []).map((t) => ({
				id: generateTaskId(),
				name: t.name,
				status: 'pending' as TaskStatus,
				children: t.children?.map((c) => ({
					id: generateTaskId(),
					name: c.name,
					status: 'pending' as TaskStatus
				}))
			}));

			const newAgent: BackgroundAgent = {
				id: agentId,
				name,
				prompt,
				status: 'queued',
				progress: 0,
				branch: options?.branch,
				tasks,
				logs: [],
				startedAt: new Date()
			};

			updateAndPersist((state) => {
				// Check if we can start immediately
				const runningCount = state.agents.filter((a) => a.status === 'running').length;
				if (runningCount < state.maxConcurrent) {
					newAgent.status = 'running';
				}

				return {
					...state,
					agents: [...state.agents, newAgent]
				};
			});

			return agentId;
		},

		/**
		 * Pause a running agent
		 */
		pauseAgent(id: string): void {
			updateAndPersist((state) => ({
				...state,
				agents: state.agents.map((a) =>
					a.id === id && a.status === 'running' ? { ...a, status: 'paused' as AgentStatus } : a
				)
			}));
		},

		/**
		 * Resume a paused agent
		 */
		resumeAgent(id: string): void {
			updateAndPersist((state) => {
				const runningCount = state.agents.filter((a) => a.status === 'running').length;
				return {
					...state,
					agents: state.agents.map((a) =>
						a.id === id && a.status === 'paused'
							? {
									...a,
									status:
										runningCount < state.maxConcurrent
											? ('running' as AgentStatus)
											: ('queued' as AgentStatus)
								}
							: a
					)
				};
			});
		},

		/**
		 * Cancel an agent (stop and mark as failed)
		 */
		cancelAgent(id: string): void {
			updateAndPersist((state) => ({
				...state,
				agents: state.agents.map((a) =>
					a.id === id && (a.status === 'running' || a.status === 'paused' || a.status === 'queued')
						? {
								...a,
								status: 'failed' as AgentStatus,
								error: 'Cancelled by user',
								completedAt: new Date()
							}
						: a
				)
			}));

			// Start next queued agent if any
			this.processQueue();
		},

		/**
		 * Intervene in a running agent (opens intervention modal/card)
		 * This is a signal - actual intervention logic happens in the UI
		 */
		interveneAgent(id: string): void {
			// Pause the agent first
			this.pauseAgent(id);

			// The UI will handle showing the intervention interface
			// by listening to the paused state
		},

		// ========================================================================
		// Progress and Logs
		// ========================================================================

		/**
		 * Update agent progress
		 */
		setProgress(id: string, progress: number): void {
			updateAndPersist((state) => ({
				...state,
				agents: state.agents.map((a) =>
					a.id === id ? { ...a, progress: Math.max(0, Math.min(100, progress)) } : a
				)
			}));
		},

		/**
		 * Append a log line to an agent
		 */
		appendLog(id: string, line: string): void {
			updateAndPersist((state) => ({
				...state,
				agents: state.agents.map((a) => {
					if (a.id !== id) return a;

					const logs = [...a.logs, line];
					// Trim if over limit
					if (logs.length > MAX_LOGS_PER_AGENT) {
						logs.splice(0, logs.length - MAX_LOGS_PER_AGENT);
					}

					return { ...a, logs };
				})
			}));
		},

		/**
		 * Clear logs for an agent
		 */
		clearLogs(id: string): void {
			updateAndPersist((state) => ({
				...state,
				agents: state.agents.map((a) => (a.id === id ? { ...a, logs: [] } : a))
			}));
		},

		// ========================================================================
		// Task Management
		// ========================================================================

		/**
		 * Update a task's status
		 */
		updateTask(
			agentId: string,
			taskId: string,
			updates: Partial<Pick<AgentTask, 'status' | 'name'>>
		): void {
			updateAndPersist((state) => ({
				...state,
				agents: state.agents.map((agent) => {
					if (agent.id !== agentId) return agent;

					const updateTaskRecursive = (tasks: AgentTask[]): AgentTask[] =>
						tasks.map((task) => {
							if (task.id === taskId) {
								return { ...task, ...updates };
							}
							if (task.children) {
								return { ...task, children: updateTaskRecursive(task.children) };
							}
							return task;
						});

					return {
						...agent,
						tasks: updateTaskRecursive(agent.tasks)
					};
				})
			}));
		},

		/**
		 * Add a task to an agent
		 */
		addTask(
			agentId: string,
			task: { name: string; parentTaskId?: string }
		): string {
			const taskId = generateTaskId();
			const newTask: AgentTask = {
				id: taskId,
				name: task.name,
				status: 'pending'
			};

			updateAndPersist((state) => ({
				...state,
				agents: state.agents.map((agent) => {
					if (agent.id !== agentId) return agent;

					if (!task.parentTaskId) {
						return { ...agent, tasks: [...agent.tasks, newTask] };
					}

					const addToParent = (tasks: AgentTask[]): AgentTask[] =>
						tasks.map((t) => {
							if (t.id === task.parentTaskId) {
								return {
									...t,
									children: [...(t.children || []), newTask]
								};
							}
							if (t.children) {
								return { ...t, children: addToParent(t.children) };
							}
							return t;
						});

					return { ...agent, tasks: addToParent(agent.tasks) };
				})
			}));

			return taskId;
		},

		// ========================================================================
		// Completion
		// ========================================================================

		/**
		 * Mark an agent as completed
		 */
		completeAgent(id: string): void {
			updateAndPersist((state) => ({
				...state,
				agents: state.agents.map((a) =>
					a.id === id
						? {
								...a,
								status: 'completed' as AgentStatus,
								progress: 100,
								completedAt: new Date()
							}
						: a
				)
			}));

			// Start next queued agent
			this.processQueue();
		},

		/**
		 * Mark an agent as failed
		 */
		failAgent(id: string, error: string): void {
			updateAndPersist((state) => ({
				...state,
				agents: state.agents.map((a) =>
					a.id === id
						? {
								...a,
								status: 'failed' as AgentStatus,
								error,
								completedAt: new Date()
							}
						: a
				)
			}));

			// Start next queued agent
			this.processQueue();
		},

		// ========================================================================
		// Queue Management
		// ========================================================================

		/**
		 * Process the queue - start queued agents if slots available
		 */
		processQueue(): void {
			update((state) => {
				const runningCount = state.agents.filter((a) => a.status === 'running').length;
				const availableSlots = state.maxConcurrent - runningCount;

				if (availableSlots <= 0) return state;

				let slotsToFill = availableSlots;
				const updatedAgents = state.agents.map((agent) => {
					if (agent.status === 'queued' && slotsToFill > 0) {
						slotsToFill--;
						return { ...agent, status: 'running' as AgentStatus };
					}
					return agent;
				});

				saveToStorage(updatedAgents);
				return { ...state, agents: updatedAgents };
			});
		},

		/**
		 * Set maximum concurrent agents
		 */
		setMaxConcurrent(max: number): void {
			update((state) => ({
				...state,
				maxConcurrent: Math.max(1, max)
			}));

			// Process queue in case we can start more agents
			this.processQueue();
		},

		// ========================================================================
		// Cleanup
		// ========================================================================

		/**
		 * Remove a completed/failed agent from the list
		 */
		removeAgent(id: string): void {
			updateAndPersist((state) => ({
				...state,
				agents: state.agents.filter((a) => a.id !== id)
			}));
		},

		/**
		 * Clear all completed agents
		 */
		clearCompleted(): void {
			updateAndPersist((state) => ({
				...state,
				agents: state.agents.filter((a) => a.status !== 'completed')
			}));
		},

		/**
		 * Clear all failed agents
		 */
		clearFailed(): void {
			updateAndPersist((state) => ({
				...state,
				agents: state.agents.filter((a) => a.status !== 'failed')
			}));
		},

		// ========================================================================
		// Utility
		// ========================================================================

		/**
		 * Get an agent by ID
		 */
		getAgent(id: string): BackgroundAgent | undefined {
			const state = get({ subscribe });
			return state.agents.find((a) => a.id === id);
		},

		/**
		 * Reset store
		 */
		reset(): void {
			const freshState: AgentsState = {
				agents: [],
				maxConcurrent: 3
			};
			set(freshState);
			saveToStorage([]);
		}
	};
}

// ============================================================================
// Export Store & Derived Stores
// ============================================================================

export const agents = createAgentsStore();

// All agents
export const allAgents = derived(agents, ($agents) => $agents.agents);

// Running agents
export const runningAgents = derived(agents, ($agents) =>
	$agents.agents.filter((a) => a.status === 'running')
);

// Queued agents
export const queuedAgents = derived(agents, ($agents) =>
	$agents.agents.filter((a) => a.status === 'queued')
);

// Paused agents
export const pausedAgents = derived(agents, ($agents) =>
	$agents.agents.filter((a) => a.status === 'paused')
);

// Completed agents
export const completedAgents = derived(agents, ($agents) =>
	$agents.agents.filter((a) => a.status === 'completed')
);

// Failed agents
export const failedAgents = derived(agents, ($agents) =>
	$agents.agents.filter((a) => a.status === 'failed')
);

// Active agents (running or paused)
export const activeAgents = derived(agents, ($agents) =>
	$agents.agents.filter((a) => a.status === 'running' || a.status === 'paused')
);

// Count of running agents
export const runningCount = derived(runningAgents, ($running) => $running.length);

// Count of queued agents
export const queuedCount = derived(queuedAgents, ($queued) => $queued.length);

// Max concurrent setting
export const maxConcurrent = derived(agents, ($agents) => $agents.maxConcurrent);
