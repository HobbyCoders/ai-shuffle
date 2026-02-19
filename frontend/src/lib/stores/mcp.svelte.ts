/**
 * MCP Server Management Store
 *
 * Manages state for MCP (Model Context Protocol) servers:
 * - Global (USER scope) servers
 * - Project-scoped servers (LOCAL and PROJECT scopes)
 * - MCP tools discovery
 *
 * Migrated to Svelte 5 runes. Provides subscribe() shim for backward
 * compatibility with $storeName auto-subscription in .svelte files.
 */

import type {
	McpServerInfo,
	McpServersResponse,
	McpTool,
	McpStats,
	CreateMcpServerRequest,
	CreateProjectMcpServerRequest,
	UpdateMcpServerRequest,
	McpScope
} from '$lib/api/mcp';
import * as mcpApi from '$lib/api/mcp';

// ============================================================================
// Types
// ============================================================================

export interface McpState {
	// Global servers (USER scope)
	globalServers: McpServerInfo[];
	// Project servers by scope
	projectServers: {
		user: McpServerInfo[];
		local: McpServerInfo[];
		project: McpServerInfo[];
	};
	// Discovered MCP tools
	tools: McpTool[];
	// Stats
	stats: McpStats | null;
	// UI state
	loading: boolean;
	error: string | null;
	// Currently selected project for project-scoped servers
	currentProjectId: string | null;
}

// ============================================================================
// Initial State
// ============================================================================

const initialState: McpState = {
	globalServers: [],
	projectServers: {
		user: [],
		local: [],
		project: []
	},
	tools: [],
	stats: null,
	loading: false,
	error: null,
	currentProjectId: null
};

// ============================================================================
// Helpers
// ============================================================================

/** Extract a human-readable error message from ApiError or Error objects */
function extractError(e: unknown, fallback: string): string {
	if (e && typeof e === 'object' && 'detail' in e) return (e as any).detail;
	if (e instanceof Error) return e.message;
	return fallback;
}

// ============================================================================
// Reactive State
// ============================================================================

let _state = $state<McpState>({ ...initialState, projectServers: { user: [], local: [], project: [] } });

// ============================================================================
// Subscribe Shim
// ============================================================================

type Subscriber<T> = (value: T) => void;

const _subscribers = new Set<Subscriber<McpState>>();

function _notifySubscribers() {
	for (const fn of _subscribers) {
		fn(_state);
	}
}

function _update(fn: (s: McpState) => McpState) {
	_state = fn(_state);
	_notifySubscribers();
}

// ============================================================================
// Exported Store
// ============================================================================

export const mcp = {
	get current() {
		return _state;
	},

	subscribe(fn: Subscriber<McpState>) {
		_subscribers.add(fn);
		fn(_state);
		return () => {
			_subscribers.delete(fn);
		};
	},

	// ========================================================================
	// Global Server Operations
	// ========================================================================

	/**
	 * Load all global MCP servers
	 */
	async loadGlobalServers(): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const servers = await mcpApi.getGlobalMcpServers();
			_update((state) => ({ ...state, globalServers: servers, loading: false }));
		} catch (e) {
			const error = extractError(e, 'Failed to load global MCP servers');
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Add a new global MCP server
	 */
	async addGlobalServer(config: CreateMcpServerRequest): Promise<McpServerInfo> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const server = await mcpApi.addGlobalMcpServer(config);
			_update((state) => ({
				...state,
				globalServers: [...state.globalServers, server],
				loading: false
			}));
			return server;
		} catch (e) {
			const error = extractError(e, 'Failed to add MCP server');
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Update a global MCP server
	 */
	async updateGlobalServer(
		serverName: string,
		config: UpdateMcpServerRequest
	): Promise<McpServerInfo> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const server = await mcpApi.updateGlobalMcpServer(serverName, config);
			_update((state) => ({
				...state,
				globalServers: state.globalServers.map((s) => (s.name === serverName ? server : s)),
				loading: false
			}));
			return server;
		} catch (e) {
			const error = extractError(e, 'Failed to update MCP server');
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Remove a global MCP server
	 */
	async removeGlobalServer(serverName: string): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			await mcpApi.removeGlobalMcpServer(serverName);
			_update((state) => ({
				...state,
				globalServers: state.globalServers.filter((s) => s.name !== serverName),
				loading: false
			}));
		} catch (e) {
			const error = extractError(e, 'Failed to remove MCP server');
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	// ========================================================================
	// Project Server Operations
	// ========================================================================

	/**
	 * Load all MCP servers for a project
	 */
	async loadProjectServers(projectId: string): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null, currentProjectId: projectId }));

		try {
			const response = await mcpApi.getProjectMcpServers(projectId);
			_update((state) => ({
				...state,
				projectServers: {
					user: response.user,
					local: response.local,
					project: response.project
				},
				loading: false
			}));
		} catch (e) {
			const error = extractError(e, 'Failed to load project MCP servers');
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Add an MCP server to a project
	 */
	async addProjectServer(
		projectId: string,
		config: CreateProjectMcpServerRequest
	): Promise<McpServerInfo> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const server = await mcpApi.addProjectMcpServer(projectId, config);
			_update((state) => {
				const scope = config.scope as 'local' | 'project';
				return {
					...state,
					projectServers: {
						...state.projectServers,
						[scope]: [...state.projectServers[scope], server]
					},
					loading: false
				};
			});
			return server;
		} catch (e) {
			const error = extractError(e, 'Failed to add MCP server');
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Update a project-scoped MCP server
	 */
	async updateProjectServer(
		projectId: string,
		serverName: string,
		config: UpdateMcpServerRequest,
		scope: 'local' | 'project'
	): Promise<McpServerInfo> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const server = await mcpApi.updateProjectMcpServer(projectId, serverName, config, scope);
			_update((state) => ({
				...state,
				projectServers: {
					...state.projectServers,
					[scope]: state.projectServers[scope].map((s) =>
						s.name === serverName ? server : s
					)
				},
				loading: false
			}));
			return server;
		} catch (e) {
			const error = extractError(e, 'Failed to update MCP server');
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Remove a project-scoped MCP server
	 */
	async removeProjectServer(
		projectId: string,
		serverName: string,
		scope: 'local' | 'project'
	): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			await mcpApi.removeProjectMcpServer(projectId, serverName, scope);
			_update((state) => ({
				...state,
				projectServers: {
					...state.projectServers,
					[scope]: state.projectServers[scope].filter((s) => s.name !== serverName)
				},
				loading: false
			}));
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to remove MCP server';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	// ========================================================================
	// MCP Tools Discovery
	// ========================================================================

	/**
	 * Load discovered MCP tools
	 */
	async loadTools(refresh: boolean = false): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			const tools = await mcpApi.getMcpTools(refresh);
			_update((state) => ({ ...state, tools, loading: false }));
		} catch (e) {
			const error = extractError(e, 'Failed to load MCP tools');
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Load MCP stats
	 */
	async loadStats(): Promise<void> {
		try {
			const stats = await mcpApi.getMcpStats();
			_update((state) => ({ ...state, stats }));
		} catch (e) {
			// Non-critical, don't throw
			console.error('Failed to load MCP stats:', e);
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
		_state = { ...initialState, projectServers: { user: [], local: [], project: [] } };
		_notifySubscribers();
	},

	/**
	 * Get all servers combined (for stats)
	 */
	getAllServers(): McpServerInfo[] {
		return [
			..._state.globalServers,
			..._state.projectServers.local,
			..._state.projectServers.project
		];
	}
};

// ============================================================================
// Derived Stores
// ============================================================================

function createDerived<T>(getter: () => T) {
	return {
		get current() {
			return getter();
		},
		subscribe(fn: Subscriber<T>) {
			const handler: Subscriber<McpState> = () => fn(getter());
			_subscribers.add(handler);
			fn(getter());
			return () => {
				_subscribers.delete(handler);
			};
		}
	};
}

// Global servers
export const globalMcpServers = createDerived(() => _state.globalServers);

// Project servers by scope
export const projectMcpServers = createDerived(() => _state.projectServers);

// All project servers (local + project scope, excludes user since that's shown separately)
export const allProjectMcpServers = createDerived(() => [
	...(_state.projectServers.local || []),
	...(_state.projectServers.project || [])
]);

// MCP tools
export const mcpTools = createDerived(() => _state.tools);

// Tools grouped by server
export const mcpToolsByServer = createDerived(() => {
	const groups: Record<string, McpTool[]> = {};
	for (const tool of _state.tools) {
		if (!groups[tool.server]) {
			groups[tool.server] = [];
		}
		groups[tool.server].push(tool);
	}
	return groups;
});

// MCP stats
export const mcpStats = createDerived(() => _state.stats);

// Loading state
export const mcpLoading = createDerived(() => _state.loading);

// Error state
export const mcpError = createDerived(() => _state.error);

// Total server count (global + project)
export const totalMcpServerCount = createDerived(() => {
	return (
		_state.globalServers.length +
		(_state.projectServers.local?.length || 0) +
		(_state.projectServers.project?.length || 0)
	);
});

// Stats for display
export const mcpDisplayStats = createDerived(() => ({
	globalCount: _state.globalServers.length,
	localCount: _state.projectServers.local?.length || 0,
	projectCount: _state.projectServers.project?.length || 0,
	toolCount: _state.tools.length
}));
