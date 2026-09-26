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
import { createSubscribers, withLoading } from './store-utils';

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

/** Latest loadProjectServers call; an older (slower) response is discarded so
 *  a project the user already left cannot overwrite the current lists. */
let _projectLoadSeq = 0;

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
		return withLoading(_update, async () => {
			const servers = await mcpApi.getGlobalMcpServers();
			_update((state) => ({ ...state, globalServers: servers }));
		}, (e) => extractError(e, 'Failed to load global MCP servers'));
	},

	/**
	 * Add a new global MCP server
	 */
	async addGlobalServer(config: CreateMcpServerRequest): Promise<McpServerInfo> {
		return withLoading(_update, async () => {
			const server = await mcpApi.addGlobalMcpServer(config);
			_update((state) => ({ ...state, globalServers: [...state.globalServers, server] }));
			return server;
		}, (e) => extractError(e, 'Failed to add MCP server'));
	},

	/**
	 * Update a global MCP server
	 */
	async updateGlobalServer(
		serverName: string,
		config: UpdateMcpServerRequest
	): Promise<McpServerInfo> {
		return withLoading(_update, async () => {
			const server = await mcpApi.updateGlobalMcpServer(serverName, config);
			_update((state) => ({
				...state,
				globalServers: state.globalServers.map((s) => (s.name === serverName ? server : s))
			}));
			return server;
		}, (e) => extractError(e, 'Failed to update MCP server'));
	},

	/**
	 * Remove a global MCP server
	 */
	async removeGlobalServer(serverName: string): Promise<void> {
		return withLoading(_update, async () => {
			await mcpApi.removeGlobalMcpServer(serverName);
			_update((state) => ({
				...state,
				globalServers: state.globalServers.filter((s) => s.name !== serverName)
			}));
		}, (e) => extractError(e, 'Failed to remove MCP server'));
	},

	// ========================================================================
	// Project Server Operations
	// ========================================================================

	/**
	 * Load all MCP servers for a project
	 */
	async loadProjectServers(projectId: string): Promise<void> {
		// Not withLoading: a load for a project already left must write
		// nothing, neither its lists nor its error nor the loading flag (the
		// newer load still owns that).
		const seq = ++_projectLoadSeq;
		_update((state) => ({ ...state, loading: true, error: null, currentProjectId: projectId }));
		try {
			const response = await mcpApi.getProjectMcpServers(projectId);
			if (seq !== _projectLoadSeq) return;
			_update((state) => ({
				...state,
				loading: false,
				projectServers: {
					user: response.user,
					installed: response.installed || [],
					local: response.local,
					project: response.project
				}
			}));
		} catch (e) {
			if (seq !== _projectLoadSeq) return;
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
		return withLoading(_update, async () => {
			const server = await mcpApi.addProjectMcpServer(projectId, config);
			_update((state) => {
				const scope = config.scope as 'installed' | 'local' | 'project';
				return {
					...state,
					projectServers: {
						...state.projectServers,
						[scope]: [...state.projectServers[scope], server]
					}
				};
			});
			return server;
		}, (e) => extractError(e, 'Failed to add MCP server'));
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
		return withLoading(_update, async () => {
			const server = await mcpApi.updateProjectMcpServer(projectId, serverName, config, scope);
			_update((state) => ({
				...state,
				projectServers: {
					...state.projectServers,
					[scope]: state.projectServers[scope].map((s) =>
						s.name === serverName ? server : s
					)
				}
			}));
			return server;
		}, (e) => extractError(e, 'Failed to update MCP server'));
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
			const error = extractError(e, 'Failed to remove MCP server');
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
		_projectLoadSeq++;
		_state = { ...initialState, projectServers: { user: [], installed: [], local: [], project: [] } };
		_notifySubscribers();
	},

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
