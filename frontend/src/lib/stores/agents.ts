/**
 * Agents Store - Background agent management for "The Deck" UI
 *
 * Manages background agent execution, task tracking, logging,
 * and WebSocket integration for real-time status updates.
 */

import { writable, derived, get } from 'svelte/store';
import { deck } from './deck';

// =============================================================================
// Types
// =============================================================================

export type TaskStatus = 'pending' | 'in_progress' | 'completed' | 'failed';

export type AgentStatus = 'queued' | 'running' | 'paused' | 'completed' | 'failed';

export interface AgentTask {
	id: string;
	name: string;
	status: TaskStatus;
	description?: string;
	children?: AgentTask[];
	startedAt?: Date;
	completedAt?: Date;
}

export interface BackgroundAgent {
	id: string;
	name: string;
	prompt: string;
	status: AgentStatus;
	progress: number; // 0-100
	branch?: string; // Git branch for this agent's work
	tasks: AgentTask[];
	logs: string[];
	startedAt: Date;
	completedAt?: Date;
	error?: string;
	// Agent configuration
	profileId?: string;
	projectId?: string;
	// Connection tracking
	sessionId?: string; // Backend session ID
}

export interface LaunchAgentOptions {
	branch?: string;
	profileId?: string;
	projectId?: string;
	autoCreateBranch?: boolean;
}

interface AgentsState {
	agents: BackgroundAgent[];
	wsConnected: boolean;
	wsError: string | null;
	// Connection management
	reconnectAttempts: number;
}

// =============================================================================
// WebSocket Connection
// =============================================================================

let ws: WebSocket | null = null;
let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
let pingTimer: ReturnType<typeof setInterval> | null = null;
const MAX_RECONNECT_DELAY = 30000;
const INITIAL_RECONNECT_DELAY = 1000;

/**
 * Get WebSocket URL for agent status updates
 */
function getWsUrl(): string {
	if (typeof window === 'undefined') return '';
	const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
	const host = window.location.host;
	return `${protocol}//${host}/api/v1/ws/agents`;
}

/**
 * Get auth token from cookie
 */
function getAuthToken(): string | null {
	if (typeof window === 'undefined') return null;
	const cookies = document.cookie.split(';');
	for (const cookie of cookies) {
		const [name, value] = cookie.trim().split('=');
		if (name === 'session') {
			return value;
		}
	}
	return null;
}

// =============================================================================
// Store Creation
// =============================================================================

function createAgentsStore() {
	const { subscribe, set, update } = writable<AgentsState>({
		agents: [],
		wsConnected: false,
		wsError: null,
		reconnectAttempts: 0
	});

	/**
	 * Calculate reconnect delay with exponential backoff
	 */
	function getReconnectDelay(attempts: number): number {
		const delay = Math.min(
			INITIAL_RECONNECT_DELAY * Math.pow(2, attempts),
			MAX_RECONNECT_DELAY
		);
		// Add jitter
		return delay + Math.random() * 1000;
	}

	/**
	 * Handle incoming WebSocket message
	 */
	function handleMessage(data: Record<string, unknown>) {
		const msgType = data.type as string;
		const agentId = data.agent_id as string;

		switch (msgType) {
			case 'agent_started': {
				// Agent has started running
				update((state) => ({
					...state,
					agents: state.agents.map((agent) =>
						agent.id === agentId
							? {
									...agent,
									status: 'running' as AgentStatus,
									sessionId: data.session_id as string
								}
							: agent
					)
				}));

				// Sync to deck store
				deck.updateAgent(agentId, { status: 'running' });
				break;
			}

			case 'agent_progress': {
				// Agent progress update
				const progress = data.progress as number;
				update((state) => ({
					...state,
					agents: state.agents.map((agent) =>
						agent.id === agentId ? { ...agent, progress } : agent
					)
				}));

				// Sync to deck store
				deck.updateAgent(agentId, { progress });
				break;
			}

			case 'agent_task_update': {
				// Task status changed
				const taskId = data.task_id as string;
				const taskStatus = data.status as TaskStatus;

				update((state) => ({
					...state,
					agents: state.agents.map((agent) => {
						if (agent.id !== agentId) return agent;

						return {
							...agent,
							tasks: updateTaskInList(agent.tasks, taskId, { status: taskStatus })
						};
					})
				}));
				break;
			}

			case 'agent_log': {
				// Log line from agent
				const logLine = data.line as string;

				update((state) => ({
					...state,
					agents: state.agents.map((agent) =>
						agent.id === agentId
							? { ...agent, logs: [...agent.logs, logLine] }
							: agent
					)
				}));
				break;
			}

			case 'agent_completed': {
				// Agent finished successfully
				update((state) => ({
					...state,
					agents: state.agents.map((agent) =>
						agent.id === agentId
							? {
									...agent,
									status: 'completed' as AgentStatus,
									progress: 100,
									completedAt: new Date()
								}
							: agent
					)
				}));

				// Sync to deck store
				deck.updateAgent(agentId, { status: 'completed', progress: 100, completedAt: new Date() });
				break;
			}

			case 'agent_failed': {
				// Agent encountered an error
				const error = data.error as string;

				update((state) => ({
					...state,
					agents: state.agents.map((agent) =>
						agent.id === agentId
							? {
									...agent,
									status: 'failed' as AgentStatus,
									error,
									completedAt: new Date()
								}
							: agent
					)
				}));

				// Sync to deck store
				deck.updateAgent(agentId, { status: 'failed', completedAt: new Date() });
				break;
			}

			case 'agent_paused': {
				// Agent was paused
				update((state) => ({
					...state,
					agents: state.agents.map((agent) =>
						agent.id === agentId
							? { ...agent, status: 'paused' as AgentStatus }
							: agent
					)
				}));

				// Sync to deck store
				deck.updateAgent(agentId, { status: 'paused' });
				break;
			}

			case 'agents_list': {
				// Initial list of agents from backend
				const agentsList = data.agents as Array<Record<string, unknown>>;

				const agents: BackgroundAgent[] = agentsList.map((a) => ({
					id: a.id as string,
					name: a.name as string,
					prompt: a.prompt as string,
					status: a.status as AgentStatus,
					progress: (a.progress as number) || 0,
					branch: a.branch as string | undefined,
					tasks: (a.tasks as AgentTask[]) || [],
					logs: (a.logs as string[]) || [],
					startedAt: new Date(a.started_at as string),
					completedAt: a.completed_at ? new Date(a.completed_at as string) : undefined,
					error: a.error as string | undefined,
					profileId: a.profile_id as string | undefined,
					projectId: a.project_id as string | undefined,
					sessionId: a.session_id as string | undefined
				}));

				update((state) => ({ ...state, agents }));

				// Sync running agents to deck store
				agents
					.filter((a) => a.status === 'running' || a.status === 'queued')
					.forEach((a) => {
						deck.addAgent({
							id: a.id,
							name: a.name,
							status: a.status,
							progress: a.progress,
							branch: a.branch,
							startedAt: a.startedAt,
							completedAt: a.completedAt
						});
					});
				break;
			}

			case 'ping': {
				// Respond to ping
				if (ws?.readyState === WebSocket.OPEN) {
					ws.send(JSON.stringify({ type: 'pong' }));
				}
				break;
			}

			case 'error': {
				// Error from backend
				console.error('[Agents] Backend error:', data.message);
				update((state) => ({ ...state, wsError: data.message as string }));
				break;
			}
		}
	}

	/**
	 * Helper to recursively update a task in a task tree
	 */
	function updateTaskInList(
		tasks: AgentTask[],
		taskId: string,
		updates: Partial<AgentTask>
	): AgentTask[] {
		return tasks.map((task) => {
			if (task.id === taskId) {
				return { ...task, ...updates };
			}
			if (task.children && task.children.length > 0) {
				return {
					...task,
					children: updateTaskInList(task.children, taskId, updates)
				};
			}
			return task;
		});
	}

	/**
	 * Connect to agents WebSocket
	 */
	function connect(isReconnect = false) {
		if (typeof window === 'undefined') return;
		if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
			return;
		}

		const token = getAuthToken();
		let url = getWsUrl();
		if (token) {
			url = `${url}?token=${encodeURIComponent(token)}`;
		}

		console.log(`[Agents] ${isReconnect ? 'Reconnecting' : 'Connecting'} to WebSocket...`);

		ws = new WebSocket(url);

		ws.onopen = () => {
			console.log('[Agents] WebSocket connected');
			update((state) => ({
				...state,
				wsConnected: true,
				wsError: null,
				reconnectAttempts: 0
			}));

			// Request current agents list
			ws?.send(JSON.stringify({ type: 'list_agents' }));

			// Start ping timer
			if (pingTimer) clearInterval(pingTimer);
			pingTimer = setInterval(() => {
				if (ws?.readyState === WebSocket.OPEN) {
					ws.send(JSON.stringify({ type: 'pong' }));
				}
			}, 25000);
		};

		ws.onclose = (event) => {
			console.log('[Agents] WebSocket closed:', event.code, event.reason);
			update((state) => ({ ...state, wsConnected: false }));

			if (pingTimer) {
				clearInterval(pingTimer);
				pingTimer = null;
			}

			// Reconnect unless intentionally closed
			if (event.code !== 1000) {
				update((state) => {
					const attempts = state.reconnectAttempts + 1;
					const delay = getReconnectDelay(attempts);

					console.log(`[Agents] Scheduling reconnect in ${Math.round(delay / 1000)}s...`);

					if (reconnectTimer) clearTimeout(reconnectTimer);
					reconnectTimer = setTimeout(() => connect(true), delay);

					return { ...state, reconnectAttempts: attempts };
				});
			}
		};

		ws.onerror = (error) => {
			console.error('[Agents] WebSocket error:', error);
		};

		ws.onmessage = (event) => {
			try {
				const data = JSON.parse(event.data);
				handleMessage(data);
			} catch (e) {
				console.error('[Agents] Failed to parse message:', e);
			}
		};
	}

	/**
	 * Disconnect from WebSocket
	 */
	function disconnect() {
		if (reconnectTimer) {
			clearTimeout(reconnectTimer);
			reconnectTimer = null;
		}
		if (pingTimer) {
			clearInterval(pingTimer);
			pingTimer = null;
		}
		if (ws) {
			ws.close(1000);
			ws = null;
		}
		update((state) => ({ ...state, wsConnected: false }));
	}

	return {
		subscribe,

		// ==========================================================================
		// Connection Management
		// ==========================================================================

		/**
		 * Initialize the store and connect WebSocket
		 */
		init() {
			connect();
		},

		/**
		 * Cleanup on destroy
		 */
		destroy() {
			disconnect();
		},

		/**
		 * Reconnect WebSocket
		 */
		reconnect() {
			disconnect();
			connect();
		},

		// ==========================================================================
		// Agent Actions
		// ==========================================================================

		/**
		 * Launch a new background agent
		 */
		async launchAgent(
			name: string,
			prompt: string,
			options: LaunchAgentOptions = {}
		): Promise<string> {
			const id = `agent-${Date.now()}-${Math.random().toString(36).substr(2, 9)}`;
			const now = new Date();

			const agent: BackgroundAgent = {
				id,
				name,
				prompt,
				status: 'queued',
				progress: 0,
				branch: options.branch,
				tasks: [],
				logs: [],
				startedAt: now,
				profileId: options.profileId,
				projectId: options.projectId
			};

			// Add to local state
			update((state) => ({
				...state,
				agents: [...state.agents, agent]
			}));

			// Sync to deck store
			deck.addAgent({
				id,
				name,
				status: 'queued',
				progress: 0,
				branch: options.branch,
				startedAt: now
			});

			// Send launch request via WebSocket
			if (ws?.readyState === WebSocket.OPEN) {
				ws.send(
					JSON.stringify({
						type: 'launch_agent',
						agent_id: id,
						name,
						prompt,
						branch: options.branch,
						profile_id: options.profileId,
						project_id: options.projectId,
						auto_create_branch: options.autoCreateBranch
					})
				);
			} else {
				// Fallback to REST API if WebSocket not connected
				try {
					const response = await fetch('/api/v1/agents/launch', {
						method: 'POST',
						headers: { 'Content-Type': 'application/json' },
						credentials: 'include',
						body: JSON.stringify({
							agent_id: id,
							name,
							prompt,
							branch: options.branch,
							profile_id: options.profileId,
							project_id: options.projectId,
							auto_create_branch: options.autoCreateBranch
						})
					});

					if (!response.ok) {
						throw new Error('Failed to launch agent');
					}
				} catch (e) {
					console.error('[Agents] Failed to launch agent:', e);
					// Mark as failed
					update((state) => ({
						...state,
						agents: state.agents.map((a) =>
							a.id === id
								? { ...a, status: 'failed' as AgentStatus, error: 'Failed to launch' }
								: a
						)
					}));
					deck.updateAgent(id, { status: 'failed' });
					throw e;
				}
			}

			return id;
		},

		/**
		 * Pause a running agent
		 */
		pauseAgent(id: string) {
			if (ws?.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({ type: 'pause_agent', agent_id: id }));
			}

			// Optimistic update
			update((state) => ({
				...state,
				agents: state.agents.map((agent) =>
					agent.id === id ? { ...agent, status: 'paused' as AgentStatus } : agent
				)
			}));

			deck.updateAgent(id, { status: 'paused' });
		},

		/**
		 * Resume a paused agent
		 */
		resumeAgent(id: string) {
			if (ws?.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({ type: 'resume_agent', agent_id: id }));
			}

			// Optimistic update
			update((state) => ({
				...state,
				agents: state.agents.map((agent) =>
					agent.id === id ? { ...agent, status: 'running' as AgentStatus } : agent
				)
			}));

			deck.updateAgent(id, { status: 'running' });
		},

		/**
		 * Cancel an agent
		 */
		cancelAgent(id: string) {
			if (ws?.readyState === WebSocket.OPEN) {
				ws.send(JSON.stringify({ type: 'cancel_agent', agent_id: id }));
			}

			// Optimistic update
			update((state) => ({
				...state,
				agents: state.agents.map((agent) =>
					agent.id === id
						? { ...agent, status: 'failed' as AgentStatus, error: 'Cancelled', completedAt: new Date() }
						: agent
				)
			}));

			deck.updateAgent(id, { status: 'failed', completedAt: new Date() });
		},

		/**
		 * Intervene on an agent - opens it in a chat card for direct interaction
		 */
		interveneAgent(id: string): string | null {
			const state = get({ subscribe });
			const agent = state.agents.find((a) => a.id === id);

			if (!agent || !agent.sessionId) {
				console.error('[Agents] Cannot intervene - agent not found or no session');
				return null;
			}

			// Create a chat card for the agent's session
			const cardId = deck.addCard({
				type: 'chat',
				title: `Intervene: ${agent.name}`,
				subtitle: 'Agent intervention',
				sessionId: agent.sessionId,
				agentId: id
			});

			return cardId;
		},

		/**
		 * Append a log line to an agent
		 */
		appendLog(id: string, line: string) {
			update((state) => ({
				...state,
				agents: state.agents.map((agent) =>
					agent.id === id ? { ...agent, logs: [...agent.logs, line] } : agent
				)
			}));
		},

		/**
		 * Update a task's status
		 */
		updateTask(agentId: string, taskId: string, updates: Partial<AgentTask>) {
			update((state) => ({
				...state,
				agents: state.agents.map((agent) => {
					if (agent.id !== agentId) return agent;

					return {
						...agent,
						tasks: updateTaskInList(agent.tasks, taskId, updates)
					};
				})
			}));
		},

		/**
		 * Remove an agent from the list
		 */
		removeAgent(id: string) {
			update((state) => ({
				...state,
				agents: state.agents.filter((a) => a.id !== id)
			}));

			deck.removeAgent(id);
		},

		/**
		 * Clear completed/failed agents
		 */
		clearCompletedAgents() {
			update((state) => {
				const completedIds = state.agents
					.filter((a) => a.status === 'completed' || a.status === 'failed')
					.map((a) => a.id);

				completedIds.forEach((id) => deck.removeAgent(id));

				return {
					...state,
					agents: state.agents.filter(
						(a) => a.status !== 'completed' && a.status !== 'failed'
					)
				};
			});
		},

		// ==========================================================================
		// Utility
		// ==========================================================================

		/**
		 * Get agent by ID
		 */
		getAgent(id: string): BackgroundAgent | undefined {
			const state = get({ subscribe });
			return state.agents.find((a) => a.id === id);
		},

		/**
		 * Clear WebSocket error
		 */
		clearError() {
			update((state) => ({ ...state, wsError: null }));
		}
	};
}

// =============================================================================
// Export Store Instance
// =============================================================================

export const agents = createAgentsStore();

// =============================================================================
// Derived Stores
// =============================================================================

/**
 * All agents list
 */
export const allAgents = derived(agents, ($agents) => $agents.agents);

/**
 * Running agents
 */
export const runningAgents = derived(agents, ($agents) =>
	$agents.agents.filter((a) => a.status === 'running')
);

/**
 * Queued agents
 */
export const queuedAgents = derived(agents, ($agents) =>
	$agents.agents.filter((a) => a.status === 'queued')
);

/**
 * Paused agents
 */
export const pausedAgents = derived(agents, ($agents) =>
	$agents.agents.filter((a) => a.status === 'paused')
);

/**
 * Completed agents
 */
export const completedAgents = derived(agents, ($agents) =>
	$agents.agents.filter((a) => a.status === 'completed')
);

/**
 * Failed agents
 */
export const failedAgents = derived(agents, ($agents) =>
	$agents.agents.filter((a) => a.status === 'failed')
);

/**
 * Active agents count (running + queued)
 */
export const activeAgentsCount = derived(
	agents,
	($agents) =>
		$agents.agents.filter((a) => a.status === 'running' || a.status === 'queued').length
);

/**
 * WebSocket connection status
 */
export const agentsWsConnected = derived(agents, ($agents) => $agents.wsConnected);

/**
 * WebSocket error
 */
export const agentsWsError = derived(agents, ($agents) => $agents.wsError);
