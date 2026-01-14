/**
 * Project Plugin Management API Client
 *
 * Provides functions for managing project-scoped plugins:
 * - LOCAL scope: <project>/.claude/plugins/ (gitignored)
 * - PROJECT scope: <project>/plugins/ (committed to git)
 */

import { api } from './client';

// ============================================================================
// Types
// ============================================================================

export interface ProjectPluginInfo {
	id: string;
	name: string;
	marketplace: string;
	description: string;
	version: string;
	scope: 'local' | 'project';
	install_path: string;
	installed_at: string | null;
	enabled: boolean;
	has_commands: boolean;
	has_agents: boolean;
	has_skills: boolean;
	has_hooks: boolean;
}

export interface AvailablePluginInfo {
	id: string;
	name: string;
	marketplace: string;
	marketplace_path: string;
	description: string;
	has_commands: boolean;
	has_agents: boolean;
	has_skills: boolean;
	has_hooks: boolean;
	installed: boolean;
	enabled: boolean;
}

export interface InstallProjectPluginRequest {
	plugin_id: string;
	scope: 'local' | 'project';
}

// ============================================================================
// API Functions
// ============================================================================

/**
 * Get all plugins installed in a project
 */
export async function getProjectPlugins(projectId: string): Promise<ProjectPluginInfo[]> {
	return api.get(`/projects/${projectId}/plugins/installed`);
}

/**
 * Get available plugins from marketplaces for installation
 */
export async function getAvailablePlugins(
	projectId: string,
	marketplace?: string
): Promise<AvailablePluginInfo[]> {
	const params = marketplace ? `?marketplace=${encodeURIComponent(marketplace)}` : '';
	return api.get(`/projects/${projectId}/plugins/available${params}`);
}

/**
 * Install a plugin to a project
 */
export async function installProjectPlugin(
	projectId: string,
	pluginId: string,
	scope: 'local' | 'project' = 'local'
): Promise<ProjectPluginInfo> {
	return api.post(`/projects/${projectId}/plugins/install`, {
		plugin_id: pluginId,
		scope
	});
}

/**
 * Uninstall a plugin from a project
 */
export async function uninstallProjectPlugin(projectId: string, pluginId: string): Promise<void> {
	return api.delete(`/projects/${projectId}/plugins/${encodeURIComponent(pluginId)}`);
}

/**
 * Enable a plugin in a project
 */
export async function enableProjectPlugin(projectId: string, pluginId: string): Promise<void> {
	return api.post(`/projects/${projectId}/plugins/${encodeURIComponent(pluginId)}/enable`);
}

/**
 * Disable a plugin in a project
 */
export async function disableProjectPlugin(projectId: string, pluginId: string): Promise<void> {
	return api.post(`/projects/${projectId}/plugins/${encodeURIComponent(pluginId)}/disable`);
}
