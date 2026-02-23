/**
 * MCP Server Management API Client
 *
 * Provides functions for managing MCP (Model Context Protocol) servers:
 * - Global (USER scope) MCP servers
 * - Project-scoped MCP servers (LOCAL and PROJECT scopes)
 * - MCP tool discovery
 */

import { api } from './client';

// ============================================================================
// Types
// ============================================================================

export type McpServerType = 'stdio' | 'sse' | 'http';
export type McpScope = 'user' | 'local' | 'project' | 'installed';

export interface McpServerInfo {
	name: string;
	type: McpServerType;
	command?: string;
	args?: string[];
	url?: string;
	env?: Record<string, string>;
	headers?: Record<string, string>;
	scope: McpScope;
	enabled: boolean;
	description?: string;
	builtin?: boolean;
}

export interface McpServersResponse {
	user: McpServerInfo[];
	installed: McpServerInfo[];
	local: McpServerInfo[];
	project: McpServerInfo[];
	total_count: number;
}

export interface McpTool {
	name: string;
	server: string;
	description?: string;
}

export interface McpStats {
	global_server_count: number;
}

export interface CreateMcpServerRequest {
	name: string;
	type: McpServerType;
	command?: string;
	args?: string[];
	url?: string;
	env?: Record<string, string>;
	headers?: Record<string, string>;
	description?: string;
}

export interface CreateProjectMcpServerRequest extends CreateMcpServerRequest {
	scope: 'installed' | 'local' | 'project';
}

export interface UpdateMcpServerRequest {
	type?: McpServerType;
	command?: string;
	args?: string[];
	url?: string;
	env?: Record<string, string>;
	headers?: Record<string, string>;
	description?: string;
}

// ============================================================================
// Global (USER scope) MCP API
// ============================================================================

/**
 * Get all global (USER scope) MCP servers
 */
export async function getGlobalMcpServers(): Promise<McpServerInfo[]> {
	return api.get<McpServerInfo[]>('/mcp/servers');
}

/**
 * Add a new global (USER scope) MCP server
 */
export async function addGlobalMcpServer(request: CreateMcpServerRequest): Promise<McpServerInfo> {
	return api.post<McpServerInfo>('/mcp/servers', request);
}

/**
 * Update a global (USER scope) MCP server
 */
export async function updateGlobalMcpServer(
	serverName: string,
	request: UpdateMcpServerRequest
): Promise<McpServerInfo> {
	return api.put<McpServerInfo>(`/mcp/servers/${encodeURIComponent(serverName)}`, request);
}

/**
 * Remove a global (USER scope) MCP server
 */
export async function removeGlobalMcpServer(serverName: string): Promise<void> {
	await api.delete(`/mcp/servers/${encodeURIComponent(serverName)}`);
}

/**
 * Toggle a global MCP server enabled/disabled
 */
export async function toggleGlobalMcpServer(
	serverName: string,
	enabled: boolean
): Promise<McpServerInfo> {
	return api.patch<McpServerInfo>(
		`/mcp/servers/${encodeURIComponent(serverName)}/toggle`,
		{ enabled }
	);
}

// ============================================================================
// Project-scoped MCP API
// ============================================================================

/**
 * Get all MCP servers for a project (all scopes)
 */
export async function getProjectMcpServers(projectId: string): Promise<McpServersResponse> {
	return api.get<McpServersResponse>(`/projects/${projectId}/mcp/servers`);
}

/**
 * Toggle an MCP server enabled/disabled for a project
 * Writes to ~/.claude.json projects[path].disabledMcpServers
 */
export async function toggleProjectMcpServer(
	projectId: string,
	serverName: string,
	enabled: boolean
): Promise<McpServerInfo> {
	return api.patch<McpServerInfo>(
		`/projects/${projectId}/mcp/servers/${encodeURIComponent(serverName)}/toggle`,
		{ enabled }
	);
}

/**
 * Add an MCP server to a project (LOCAL or PROJECT scope)
 */
export async function addProjectMcpServer(
	projectId: string,
	request: CreateProjectMcpServerRequest
): Promise<McpServerInfo> {
	return api.post<McpServerInfo>(`/projects/${projectId}/mcp/servers`, request);
}

/**
 * Update a project-scoped MCP server
 */
export async function updateProjectMcpServer(
	projectId: string,
	serverName: string,
	request: UpdateMcpServerRequest,
	scope: 'installed' | 'local' | 'project' = 'local'
): Promise<McpServerInfo> {
	return api.put<McpServerInfo>(
		`/projects/${projectId}/mcp/servers/${encodeURIComponent(serverName)}?scope=${scope}`,
		request
	);
}

/**
 * Remove a project-scoped MCP server
 */
export async function removeProjectMcpServer(
	projectId: string,
	serverName: string,
	scope: 'user' | 'installed' | 'local' | 'project' = 'local'
): Promise<void> {
	await api.delete(
		`/projects/${projectId}/mcp/servers/${encodeURIComponent(serverName)}?scope=${scope}`
	);
}

// ============================================================================
// MCP Tools Discovery
// ============================================================================

/**
 * Get all discovered MCP tools from the CLI cache
 */
export async function getMcpTools(refresh: boolean = false): Promise<McpTool[]> {
	return api.get<McpTool[]>(`/mcp/tools${refresh ? '?refresh=true' : ''}`);
}

/**
 * Get MCP server statistics
 */
export async function getMcpStats(): Promise<McpStats> {
	return api.get<McpStats>('/mcp/stats');
}

// ============================================================================
// Helper Functions
// ============================================================================

/**
 * Get a human-readable type label for an MCP server type
 */
export function getServerTypeLabel(type: McpServerType): string {
	switch (type) {
		case 'stdio':
			return 'Local Process';
		case 'sse':
			return 'SSE (Remote)';
		case 'http':
			return 'HTTP (Remote)';
		default:
			return type;
	}
}

/**
 * Get a human-readable scope label
 */
export function getScopeLabel(scope: McpScope): string {
	switch (scope) {
		case 'user':
			return 'Global';
		case 'local':
			return 'Project Local';
		case 'project':
			return 'Project';
		default:
			return scope;
	}
}

/**
 * Validate an MCP server configuration
 */
export function validateMcpServerConfig(
	config: CreateMcpServerRequest
): { valid: boolean; errors: string[] } {
	const errors: string[] = [];

	if (!config.name || config.name.trim().length === 0) {
		errors.push('Server name is required');
	} else if (!/^[a-zA-Z0-9_-]+$/.test(config.name)) {
		errors.push('Server name must be alphanumeric with hyphens/underscores only');
	}

	if (config.type === 'stdio') {
		if (!config.command) {
			errors.push('Command is required for stdio servers');
		}
	} else if (config.type === 'sse' || config.type === 'http') {
		if (!config.url) {
			errors.push('URL is required for SSE/HTTP servers');
		}
	}

	return {
		valid: errors.length === 0,
		errors
	};
}
