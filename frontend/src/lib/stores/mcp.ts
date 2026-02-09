/**
 * MCP Server Management Store
 *
 * Manages state for MCP (Model Context Protocol) servers:
 * - Global (USER scope) servers
 * - Project-scoped servers (LOCAL and PROJECT scopes)
 * - MCP tools discovery
 */

import { writable, derived, get } from 'svelte/store';
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
// Store Creation
// ============================================================================

function createMcpStore() {
	const { subscribe, set, update } = writable<McpState>(initialState);

	return {
		subscribe,

		// ========================================================================
		// Global Server Operations
		// ========================================================================

		/**
		 * Load all global MCP servers
		 */
		async loadGlobalServers(): Promise<void> {
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const servers = await mcpApi.getGlobalMcpServers();
				update((state) => ({ ...state, globalServers: servers, loading: false }));
			} catch (e) {
				const error = extractError(e, 'Failed to load global MCP servers');
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Add a new global MCP server
		 */
		async addGlobalServer(config: CreateMcpServerRequest): Promise<McpServerInfo> {
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const server = await mcpApi.addGlobalMcpServer(config);
				update((state) => ({
					...state,
					globalServers: [...state.globalServers, server],
					loading: false
				}));
				return server;
			} catch (e) {
				const error = extractError(e, 'Failed to add MCP server');
				update((state) => ({ ...state, error, loading: false }));
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
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const server = await mcpApi.updateGlobalMcpServer(serverName, config);
				update((state) => ({
					...state,
					globalServers: state.globalServers.map((s) => (s.name === serverName ? server : s)),
					loading: false
				}));
				return server;
			} catch (e) {
				const error = extractError(e, 'Failed to update MCP server');
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Remove a global MCP server
		 */
		async removeGlobalServer(serverName: string): Promise<void> {
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				await mcpApi.removeGlobalMcpServer(serverName);
				update((state) => ({
					...state,
					globalServers: state.globalServers.filter((s) => s.name !== serverName),
					loading: false
				}));
			} catch (e) {
				const error = extractError(e, 'Failed to remove MCP server');
				update((state) => ({ ...state, error, loading: false }));
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
			update((state) => ({ ...state, loading: true, error: null, currentProjectId: projectId }));

			try {
				const response = await mcpApi.getProjectMcpServers(projectId);
				update((state) => ({
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
				update((state) => ({ ...state, error, loading: false }));
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
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const server = await mcpApi.addProjectMcpServer(projectId, config);
				update((state) => {
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
				update((state) => ({ ...state, error, loading: false }));
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
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const server = await mcpApi.updateProjectMcpServer(projectId, serverName, config, scope);
				update((state) => ({
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
				update((state) => ({ ...state, error, loading: false }));
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
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				await mcpApi.removeProjectMcpServer(projectId, serverName, scope);
				update((state) => ({
					...state,
					projectServers: {
						...state.projectServers,
						[scope]: state.projectServers[scope].filter((s) => s.name !== serverName)
					},
					loading: false
				}));
			} catch (e) {
				const error = e instanceof Error ? e.message : 'Failed to remove MCP server';
				update((state) => ({ ...state, error, loading: false }));
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
			update((state) => ({ ...state, loading: true, error: null }));

			try {
				const tools = await mcpApi.getMcpTools(refresh);
				update((state) => ({ ...state, tools, loading: false }));
			} catch (e) {
				const error = extractError(e, 'Failed to load MCP tools');
				update((state) => ({ ...state, error, loading: false }));
				throw e;
			}
		},

		/**
		 * Load MCP stats
		 */
		async loadStats(): Promise<void> {
			try {
				const stats = await mcpApi.getMcpStats();
				update((state) => ({ ...state, stats }));
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
			update((state) => ({ ...state, error: null }));
		},

		/**
		 * Reset store to initial state
		 */
		reset(): void {
			set(initialState);
		},

		/**
		 * Get all servers combined (for stats)
		 */
		getAllServers(): McpServerInfo[] {
			const state = get({ subscribe });
			return [
				...state.globalServers,
				...state.projectServers.local,
				...state.projectServers.project
			];
		}
	};
}

// ============================================================================
// Export Store & Derived Stores
// ============================================================================

export const mcp = createMcpStore();

// Global servers
export const globalMcpServers = derived(mcp, ($mcp) => $mcp.globalServers);

// Project servers by scope
export const projectMcpServers = derived(mcp, ($mcp) => $mcp.projectServers);

// All project servers (local + project scope, excludes user since that's shown separately)
export const allProjectMcpServers = derived(mcp, ($mcp) => [
	...($mcp.projectServers.local || []),
	...($mcp.projectServers.project || [])
]);

// MCP tools
export const mcpTools = derived(mcp, ($mcp) => $mcp.tools);

// Tools grouped by server
export const mcpToolsByServer = derived(mcp, ($mcp) => {
	const groups: Record<string, McpTool[]> = {};
	for (const tool of $mcp.tools) {
		if (!groups[tool.server]) {
			groups[tool.server] = [];
		}
		groups[tool.server].push(tool);
	}
	return groups;
});

// MCP stats
export const mcpStats = derived(mcp, ($mcp) => $mcp.stats);

// Loading state
export const mcpLoading = derived(mcp, ($mcp) => $mcp.loading);

// Error state
export const mcpError = derived(mcp, ($mcp) => $mcp.error);

// Total server count (global + project)
export const totalMcpServerCount = derived(mcp, ($mcp) => {
	return (
		$mcp.globalServers.length +
		($mcp.projectServers.local?.length || 0) +
		($mcp.projectServers.project?.length || 0)
	);
});

// Stats for display
export const mcpDisplayStats = derived(mcp, ($mcp) => ({
	globalCount: $mcp.globalServers.length,
	localCount: $mcp.projectServers.local?.length || 0,
	projectCount: $mcp.projectServers.project?.length || 0,
	toolCount: $mcp.tools.length
}));
