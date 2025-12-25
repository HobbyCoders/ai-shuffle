/**
 * Background Agents Store
 *
 * Manages background AI agents that run autonomously:
 * - Agent lifecycle (launch, pause, resume, cancel)
 * - Task tracking and progress
 * - Log streaming via WebSocket
 * - Real-time updates from backend
 */

import { writable, derived, get } from 'svelte/store';
import { browser } from '$app/environment';

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
	prUrl?: string;
	tasks: AgentTask[];
	logs: AgentLogEntry[];
	startedAt: Date;
	completedAt?: Date;
	error?: string;
	resultSummary?: string;
	profileId?: string;
	projectId?: string;
	worktreeId?: string;
	autoBranch: boolean;
	autoPr: boolean;
	autoReview: boolean;
	maxDurationMinutes: number;
}

export interface AgentLogEntry {
	timestamp: Date;
	level: 'debug' | 'info' | 'warning' | 'error';
	message: string;
	metadata?: Record<string, unknown>;
}

export interface AgentStats {
	total: number;
	running: number;
	queued: number;
	completed: number;
	failed: number;
	successRate: number;
	avgDurationMinutes: number;
	byDay: Record<string, number>;
}

interface AgentsState {
	agents: BackgroundAgent[];
	stats: AgentStats | null;
	loading: boolean;
	error: string | null;
	wsConnected: boolean;
}

// ============================================================================
// API Client
// ============================================================================

const API_BASE = '/api/v1/agents';

async function getAuthHeaders(): Promise<HeadersInit> {
	// Get auth token from cookie or localStorage
	const headers: HeadersInit = {
		'Content-Type': 'application/json'
	};
	return headers;
}

async function apiRequest<T>(
	endpoint: string,
	options: RequestInit = {}
): Promise<T> {
	const headers = await getAuthHeaders();
	const response = await fetch(`${API_BASE}${endpoint}`, {
		...options,
		headers: { ...headers, ...options.headers },
		credentials: 'include'
	});

	if (!response.ok) {
		const error = await response.json().catch(() => ({ detail: 'Unknown error' }));
		throw new Error(error.detail || `API error: ${response.status}`);
	}

	// Handle 204 No Content
	if (response.status === 204) {
		return undefined as T;
	}

	return response.json();
}

// ============================================================================
// Response Type Converters
// ============================================================================

interface ApiAgentResponse {
	id: string;
	name: string;
	prompt: string;
	status: AgentStatus;
	progress: number;
	branch?: string;
	pr_url?: string;
	tasks: AgentTask[];
	started_at: string;
	completed_at?: string;
	error?: string;
	result_summary?: string;
	profile_id?: string;
	project_id?: string;
	worktree_id?: string;
	auto_branch: boolean;
	auto_pr: boolean;
	auto_review: boolean;
	max_duration_minutes: number;
}

interface ApiLogEntry {
	timestamp: string;
	level: 'debug' | 'info' | 'warning' | 'error';
	message: string;
	metadata?: Record<string, unknown>;
}

function convertApiAgent(api: ApiAgentResponse): BackgroundAgent {
	return {
		id: api.id,
		name: api.name,
		prompt: api.prompt,
		status: api.status,
		progress: api.progress,
		branch: api.branch,
		prUrl: api.pr_url,
		tasks: api.tasks,
		logs: [], // Logs are fetched separately
		startedAt: new Date(api.started_at),
		completedAt: api.completed_at ? new Date(api.completed_at) : undefined,
		error: api.error,
		resultSummary: api.result_summary,
		profileId: api.profile_id,
		projectId: api.project_id,
		worktreeId: api.worktree_id,
		autoBranch: api.auto_branch,
		autoPr: api.auto_pr,
		autoReview: api.auto_review,
		maxDurationMinutes: api.max_duration_minutes
	};
}

function convertApiLog(api: ApiLogEntry): AgentLogEntry {
	return {
		timestamp: new Date(api.timestamp),
		level: api.level,
		message: api.message,
		metadata: api.metadata
	};
}

// ============================================================================
// WebSocket Manager
// ============================================================================

class AgentWebSocket {
	private ws: WebSocket | null = null;
	private reconnectTimeout: ReturnType<typeof setTimeout> | null = null;
	private reconnectAttempts = 0;
	private maxReconnectAttempts = 10;
	private baseReconnectDelay = 1000;
	private onMessage: (event: MessageEvent) => void;
	private onConnected: () => void;
	private onDisconnected: () => void;

	constructor(
		onMessage: (event: MessageEvent) => void,
		onConnected: () => void,
		onDisconnected: () => void
	) {
		this.onMessage = onMessage;
		this.onConnected = onConnected;
		this.onDisconnected = onDisconnected;
	}

	connect(): void {
		if (!browser) return;
		if (this.ws?.readyState === WebSocket.OPEN) return;

		const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
		const wsUrl = `${protocol}//${window.location.host}${API_BASE}/ws`;

		try {
			this.ws = new WebSocket(wsUrl);

			this.ws.onopen = () => {
				console.log('[Agents WS] Connected');
				this.reconnectAttempts = 0;
				this.onConnected();
			};

			this.ws.onmessage = this.onMessage;

			this.ws.onclose = (event) => {
				console.log('[Agents WS] Disconnected:', event.code, event.reason);
				this.onDisconnected();
				this.scheduleReconnect();
			};

			this.ws.onerror = (error) => {
				console.error('[Agents WS] Error:', error);
			};
		} catch (error) {
			console.error('[Agents WS] Failed to connect:', error);
			this.scheduleReconnect();
		}
	}

	private scheduleReconnect(): void {
		if (this.reconnectAttempts >= this.maxReconnectAttempts) {
			console.error('[Agents WS] Max reconnect attempts reached');
			return;
		}

		const delay = this.baseReconnectDelay * Math.pow(2, this.reconnectAttempts);
		this.reconnectAttempts++;

		console.log(`[Agents WS] Reconnecting in ${delay}ms (attempt ${this.reconnectAttempts})`);
		this.reconnectTimeout = setTimeout(() => this.connect(), delay);
	}

	disconnect(): void {
		if (this.reconnectTimeout) {
			clearTimeout(this.reconnectTimeout);
			this.reconnectTimeout = null;
		}
		if (this.ws) {
			this.ws.close();
			this.ws = null;
		}
	}

	send(message: object): void {
		if (this.ws?.readyState === WebSocket.OPEN) {
			this.ws.send(JSON.stringify(message));
		}
	}
}

// ============================================================================
// Store Creation
// ============================================================================

function createAgentsStore() {
	const initialState: AgentsState = {
		agents: [],
		stats: null,
		loading: false,
		error: null,
		wsConnected: false
	};

	const { subscribe, set, update } = writable<AgentsState>(initialState);

	let wsManager: AgentWebSocket | null = null;
	let initialized = false;

	// WebSocket message handler
	function handleWsMessage(event: MessageEvent): void {
		try {
			const message = JSON.parse(event.data);

			switch (message.type) {
				case 'ping':
					wsManager?.send({ type: 'pong' });
					break;

				case 'agent_launched':
				case 'agent_started':
				case 'agent_progress':
				case 'agent_paused':
				case 'agent_resumed':
				case 'agent_completed':
				case 'agent_failed':
				case 'agent_cancelled':
					// Update agent in store
					if (message.data) {
						update((state) => ({
							...state,
							agents: state.agents.map((a) =>
								a.id === message.agent_id ? convertApiAgent(message.data) : a
							)
						}));
					}
					// If it's a new agent (launched), add it
					if (message.type === 'agent_launched' && message.data) {
						update((state) => {
							const exists = state.agents.some((a) => a.id === message.agent_id);
							if (!exists) {
								return {
									...state,
									agents: [...state.agents, convertApiAgent(message.data)]
								};
							}
							return state;
						});
					}
					break;

				case 'agent_task_update':
					// Update task within agent
					update((state) => ({
						...state,
						agents: state.agents.map((a) => {
							if (a.id !== message.agent_id) return a;
							return {
								...a,
								tasks: message.data.tasks || a.tasks
							};
						})
					}));
					break;

				case 'agent_log':
					// Append log to agent
					if (message.data) {
						update((state) => ({
							...state,
							agents: state.agents.map((a) => {
								if (a.id !== message.agent_id) return a;
								const newLog = convertApiLog(message.data);
								return {
									...a,
									logs: [...a.logs.slice(-999), newLog] // Keep last 1000 logs
								};
							})
						}));
					}
					break;

				case 'agent_deleted':
					// Remove agent from store
					update((state) => ({
						...state,
						agents: state.agents.filter((a) => a.id !== message.agent_id)
					}));
					break;

				case 'subscribed':
				case 'unsubscribed':
					// Acknowledgment messages, no action needed
					break;

				default:
					console.log('[Agents WS] Unknown message type:', message.type);
			}
		} catch (error) {
			console.error('[Agents WS] Failed to parse message:', error);
		}
	}

	return {
		subscribe,

		// ========================================================================
		// Initialization
		// ========================================================================

		/**
		 * Initialize store - fetch agents and connect WebSocket
		 * This is idempotent - safe to call multiple times
		 */
		async init(): Promise<void> {
			if (!browser) return;

			// If already initialized and WebSocket is connected, just refresh data
			if (initialized && wsManager) {
				await this.refresh();
				return;
			}

			update((s) => ({ ...s, loading: true, error: null }));

			try {
				// Fetch initial agents list
				const response = await apiRequest<{ agents: ApiAgentResponse[]; total: number }>('');

				update((s) => ({
					...s,
					agents: response.agents.map(convertApiAgent),
					loading: false
				}));

				// Connect WebSocket for real-time updates
				this.connectWebSocket();
				initialized = true;
			} catch (error) {
				console.error('[Agents] Failed to initialize:', error);
				update((s) => ({
					...s,
					loading: false,
					error: error instanceof Error ? error.message : 'Failed to load agents'
				}));
			}
		},

		/**
		 * Connect to WebSocket for real-time updates
		 */
		connectWebSocket(): void {
			if (wsManager) {
				wsManager.disconnect();
			}

			wsManager = new AgentWebSocket(
				handleWsMessage,
				() => update((s) => ({ ...s, wsConnected: true })),
				() => update((s) => ({ ...s, wsConnected: false }))
			);
			wsManager.connect();
		},

		/**
		 * Disconnect WebSocket
		 */
		disconnectWebSocket(): void {
			wsManager?.disconnect();
			wsManager = null;
			update((s) => ({ ...s, wsConnected: false }));
		},

		// ========================================================================
		// Agent Lifecycle
		// ========================================================================

		/**
		 * Launch a new background agent
		 *
		 * Simplified workflow:
		 * - Always creates a new feature branch (autoBranch=true)
		 * - Always creates a PR on completion (autoPr=true)
		 * - Runs until completion (maxDurationMinutes=0 = unlimited)
		 * - No auto-review (autoReview=false)
		 */
		async launchAgent(options: {
			name: string;
			prompt: string;
			profileId?: string;
			projectId?: string;
			autoBranch?: boolean;
			autoPr?: boolean;
			autoMerge?: boolean;
			autoReview?: boolean;
			maxDurationMinutes?: number;
			baseBranch?: string;
		}): Promise<BackgroundAgent> {
			const response = await apiRequest<ApiAgentResponse>('/launch', {
				method: 'POST',
				body: JSON.stringify({
					name: options.name,
					prompt: options.prompt,
					profile_id: options.profileId,
					project_id: options.projectId,
					auto_branch: options.autoBranch ?? true,
					auto_pr: options.autoPr ?? true,
					auto_merge: options.autoMerge ?? false,
					auto_review: options.autoReview ?? false,
					max_duration_minutes: options.maxDurationMinutes ?? 0,
					base_branch: options.baseBranch
				})
			});

			const agent = convertApiAgent(response);

			// Add to local state (WebSocket will also notify, but this is faster)
			update((state) => {
				const exists = state.agents.some((a) => a.id === agent.id);
				if (!exists) {
					return { ...state, agents: [...state.agents, agent] };
				}
				return state;
			});

			return agent;
		},

		/**
		 * Pause a running agent
		 */
		async pauseAgent(id: string): Promise<void> {
			await apiRequest(`/${id}/pause`, { method: 'POST' });

			// Optimistic update
			update((state) => ({
				...state,
				agents: state.agents.map((a) =>
					a.id === id ? { ...a, status: 'paused' as AgentStatus } : a
				)
			}));
		},

		/**
		 * Resume a paused agent
		 */
		async resumeAgent(id: string): Promise<void> {
			await apiRequest(`/${id}/resume`, { method: 'POST' });

			// Optimistic update
			update((state) => ({
				...state,
				agents: state.agents.map((a) =>
					a.id === id ? { ...a, status: 'running' as AgentStatus } : a
				)
			}));
		},

		/**
		 * Cancel an agent
		 */
		async cancelAgent(id: string): Promise<void> {
			await apiRequest(`/${id}/cancel`, { method: 'POST' });

			// Optimistic update
			update((state) => ({
				...state,
				agents: state.agents.map((a) =>
					a.id === id
						? { ...a, status: 'failed' as AgentStatus, error: 'Cancelled by user' }
						: a
				)
			}));
		},

		/**
		 * Delete an agent record
		 */
		async deleteAgent(id: string): Promise<void> {
			await apiRequest(`/${id}`, { method: 'DELETE' });

			// Remove from local state
			update((state) => ({
				...state,
				agents: state.agents.filter((a) => a.id !== id)
			}));
		},

		// ========================================================================
		// Data Fetching
		// ========================================================================

		/**
		 * Refresh agents list from server
		 */
		async refresh(): Promise<void> {
			update((s) => ({ ...s, loading: true }));

			try {
				const response = await apiRequest<{ agents: ApiAgentResponse[]; total: number }>('');

				update((s) => ({
					...s,
					agents: response.agents.map(convertApiAgent),
					loading: false,
					error: null
				}));
			} catch (error) {
				update((s) => ({
					...s,
					loading: false,
					error: error instanceof Error ? error.message : 'Failed to refresh agents'
				}));
			}
		},

		/**
		 * Fetch a single agent's details
		 */
		async fetchAgent(id: string): Promise<BackgroundAgent | null> {
			try {
				const response = await apiRequest<ApiAgentResponse>(`/${id}`);
				const agent = convertApiAgent(response);

				// Update in store
				update((state) => ({
					...state,
					agents: state.agents.map((a) => (a.id === id ? agent : a))
				}));

				return agent;
			} catch (error) {
				console.error(`[Agents] Failed to fetch agent ${id}:`, error);
				return null;
			}
		},

		/**
		 * Fetch logs for an agent
		 */
		async fetchLogs(
			id: string,
			options?: { limit?: number; offset?: number; level?: string }
		): Promise<AgentLogEntry[]> {
			const params = new URLSearchParams();
			if (options?.limit) params.set('limit', String(options.limit));
			if (options?.offset) params.set('offset', String(options.offset));
			if (options?.level) params.set('level', options.level);

			const response = await apiRequest<{
				agent_id: string;
				logs: ApiLogEntry[];
				total: number;
				offset: number;
				limit: number;
			}>(`/${id}/logs?${params}`);

			const logs = response.logs.map(convertApiLog);

			// Update agent's logs in store
			update((state) => ({
				...state,
				agents: state.agents.map((a) => (a.id === id ? { ...a, logs } : a))
			}));

			return logs;
		},

		/**
		 * Fetch agent statistics
		 */
		async fetchStats(days: number = 7, projectId?: string): Promise<AgentStats> {
			const params = new URLSearchParams({ days: String(days) });
			if (projectId) params.set('project_id', projectId);

			const response = await apiRequest<{
				total: number;
				running: number;
				queued: number;
				completed: number;
				failed: number;
				success_rate: number;
				avg_duration_minutes: number;
				by_day: Record<string, number>;
			}>(`/stats?${params}`);

			const stats: AgentStats = {
				total: response.total,
				running: response.running,
				queued: response.queued,
				completed: response.completed,
				failed: response.failed,
				successRate: response.success_rate,
				avgDurationMinutes: response.avg_duration_minutes,
				byDay: response.by_day
			};

			update((s) => ({ ...s, stats }));
			return stats;
		},

		// ========================================================================
		// Bulk Operations
		// ========================================================================

		/**
		 * Clear all completed agents
		 */
		async clearCompleted(): Promise<number> {
			const response = await apiRequest<{ status: string; deleted: number }>(
				'/clear-completed',
				{ method: 'POST' }
			);

			// Remove completed from local state
			update((state) => ({
				...state,
				agents: state.agents.filter((a) => a.status !== 'completed')
			}));

			return response.deleted;
		},

		/**
		 * Clear all failed agents
		 */
		async clearFailed(): Promise<number> {
			const response = await apiRequest<{ status: string; deleted: number }>(
				'/clear-failed',
				{ method: 'POST' }
			);

			// Remove failed from local state
			update((state) => ({
				...state,
				agents: state.agents.filter((a) => a.status !== 'failed')
			}));

			return response.deleted;
		},

		// ========================================================================
		// Utility
		// ========================================================================

		/**
		 * Get an agent by ID from local state
		 */
		getAgent(id: string): BackgroundAgent | undefined {
			const state = get({ subscribe });
			return state.agents.find((a) => a.id === id);
		},

		/**
		 * Subscribe to a specific agent for updates
		 */
		subscribeToAgent(id: string): void {
			wsManager?.send({ type: 'subscribe', agent_id: id });
		},

		/**
		 * Unsubscribe from a specific agent
		 */
		unsubscribeFromAgent(id: string): void {
			wsManager?.send({ type: 'unsubscribe', agent_id: id });
		},

		/**
		 * Reset store to initial state
		 */
		reset(): void {
			this.disconnectWebSocket();
			initialized = false;
			set(initialState);
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

// Loading state
export const agentsLoading = derived(agents, ($agents) => $agents.loading);

// Error state
export const agentsError = derived(agents, ($agents) => $agents.error);

// WebSocket connected state
export const agentsWsConnected = derived(agents, ($agents) => $agents.wsConnected);

// Agent statistics
export const agentStats = derived(agents, ($agents) => $agents.stats);
