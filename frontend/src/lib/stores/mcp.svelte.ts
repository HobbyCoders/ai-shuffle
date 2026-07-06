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
import { createSubscribers } from './store-utils';

// ============================================================================
// Types
// ============================================================================

interface McpState {
	// Global servers (USER scope)
	globalServers: McpServerInfo[];
	// Project servers by scope
	projectServers: {
		user: McpServerInfo[];
		installed: McpServerInfo[];
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
		installed: [],
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
	if (e && typeof e === 'object' && 'detail' in e) return (e as { detail: string }).detail;
	if (e instanceof Error) return e.message;
	return fallback;
}

// ============================================================================
// Reactive State
// ============================================================================

let _state = $state<McpState>({ ...initialState, projectServers: { user: [], installed: [], local: [], project: [] } });

// ============================================================================
// Subscribe Shim (centralized via store-utils)
// ============================================================================

const { subscribe, update: _update, notifySubscribers: _notifySubscribers, createDerived } =
	createSubscribers<McpState>(
		() => _state,
		(v) => { _state = v; }
	);

// ============================================================================
// Exported Store
// ============================================================================

export const mcp = {
	get current() {
		return _state;
	},

	subscribe,

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

	/**
	 * Toggle a global MCP server enabled/disabled
	 */
	async toggleGlobalServer(serverName: string, enabled: boolean): Promise<void> {
		try {
			const updated = await mcpApi.toggleGlobalMcpServer(serverName, enabled);
			_update((state) => ({
				...state,
				globalServers: state.globalServers.map((s) =>
					s.name === serverName ? updated : s
				)
			}));
		} catch (e) {
			const error = extractError(e, 'Failed to toggle MCP server');
			_update((state) => ({ ...state, error }));
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
					installed: response.installed || [],
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
				const scope = config.scope as 'installed' | 'local' | 'project';
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
		scope: 'installed' | 'local' | 'project'
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
		scope: 'user' | 'installed' | 'local' | 'project'
	): Promise<void> {
		_update((state) => ({ ...state, loading: true, error: null }));

		try {
			await mcpApi.removeProjectMcpServer(projectId, serverName, scope);
			if (scope === 'user') {
				// Global server removed — also remove from globalServers and reload project servers
				_update((state) => ({
					...state,
					globalServers: state.globalServers.filter((s) => s.name !== serverName),
					loading: false
				}));
				await mcp.loadProjectServers(projectId);
			} else {
				_update((state) => ({
					...state,
					projectServers: {
						...state.projectServers,
						[scope]: state.projectServers[scope].filter((s) => s.name !== serverName)
					},
					loading: false
				}));
			}
		} catch (e) {
			const error = e instanceof Error ? e.message : 'Failed to remove MCP server';
			_update((state) => ({ ...state, error, loading: false }));
			throw e;
		}
	},

	/**
	 * Toggle an MCP server enabled/disabled for a project.
	 * Writes to ~/.claude.json projects[path].disabledMcpServers.
	 */
	async toggleProjectServer(
		projectId: string,
		serverName: string,
		enabled: boolean
	): Promise<void> {
		try {
			await mcpApi.toggleProjectMcpServer(projectId, serverName, enabled);
			// Reload project servers to get updated enabled state
			await mcp.loadProjectServers(projectId);
		} catch (e) {
			const error = extractError(e, 'Failed to toggle MCP server');
			_update((state) => ({ ...state, error }));
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
		_state = { ...initialState, projectServers: { user: [], installed: [], local: [], project: [] } };
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
// Derived Stores (via store-utils createDerived)
// ============================================================================

// Global servers
export const globalMcpServers = createDerived(() => _state.globalServers);

// Project servers by scope
export const projectMcpServers = createDerived(() => _state.projectServers);

// Loading state
export const mcpLoading = createDerived(() => _state.loading);

// Error state
export const mcpError = createDerived(() => _state.error);

// Stats for display
export const mcpDisplayStats = createDerived(() => ({
	globalCount: _state.globalServers.length,
	installedCount: _state.projectServers.installed?.length || 0,
	localCount: _state.projectServers.local?.length || 0,
	projectCount: _state.projectServers.project?.length || 0,
	toolCount: _state.tools.length
}));
