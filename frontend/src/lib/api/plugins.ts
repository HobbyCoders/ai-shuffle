/**
 * Plugin Management API Client
 *
 * Provides functions for managing Claude Code plugins:
 * - Marketplaces (add, remove, sync)
 * - Plugin installation/uninstallation
 * - Plugin enable/disable
 * - File-based agents discovery
 */

import { api } from './client';

// ============================================================================
// Types
// ============================================================================

export interface MarketplaceInfo {
	id: string;
	source: string;
	url: string;
	install_location: string;
	last_updated: string | null;
}

export interface PluginInfo {
	id: string;
	name: string;
	marketplace: string;
	description: string;
	version: string;
	scope: string;
	install_path: string;
	installed_at: string;
	enabled: boolean;
	has_commands: boolean;
	has_agents: boolean;
	has_skills: boolean;
	has_hooks: boolean;
}

export interface PluginDetails extends PluginInfo {
	commands: string[];
	agents: string[];
	skills: string[];
	hooks: string[];
	readme: string | null;
}

export interface AvailablePlugin {
	id: string;
	name: string;
	marketplace: string;
	description: string;
	path: string;
	installed: boolean;
	installed_version: string | null;
	has_commands: boolean;
	has_agents: boolean;
	has_skills: boolean;
	has_hooks: boolean;
}

export interface FileBasedAgent {
	id: string;
	name: string;
	description: string;
	plugin_id: string;
	plugin_name: string;
	file_path: string;
	tools: string[] | null;
	model: string | null;
}

export interface InstallPluginRequest {
	plugin_id: string;
	scope?: string;
}

export interface InstallPluginsBatchRequest {
	plugin_ids: string[];
	scope?: string;
}

export interface AddMarketplaceRequest {
	url: string;
	name: string;
}

// ============================================================================
// Marketplace API
// ============================================================================

/**
 * Get all registered marketplaces
 */
export async function getMarketplaces(): Promise<MarketplaceInfo[]> {
	return api.get<MarketplaceInfo[]>('/plugins/marketplaces');
}

/**
 * Add a new marketplace from a git URL
 */
export async function addMarketplace(url: string, name: string): Promise<MarketplaceInfo> {
	return api.post<MarketplaceInfo>('/plugins/marketplaces', { url, name });
}

/**
 * Remove a marketplace
 */
export async function removeMarketplace(marketplaceId: string): Promise<void> {
	await api.delete(`/plugins/marketplaces/${marketplaceId}`);
}

/**
 * Sync a marketplace (git pull)
 */
export async function syncMarketplace(marketplaceId: string): Promise<{ success: boolean; message: string }> {
	return api.post<{ success: boolean; message: string }>(`/plugins/marketplaces/${marketplaceId}/sync`);
}

// ============================================================================
// Plugin Discovery API
// ============================================================================

/**
 * Get all available plugins from all marketplaces
 */
export async function getAvailablePlugins(marketplace?: string): Promise<AvailablePlugin[]> {
	const params = marketplace ? `?marketplace=${encodeURIComponent(marketplace)}` : '';
	return api.get<AvailablePlugin[]>(`/plugins/available${params}`);
}

/**
 * Get all installed plugins
 */
export async function getInstalledPlugins(): Promise<PluginInfo[]> {
	return api.get<PluginInfo[]>('/plugins/installed');
}

/**
 * Get detailed information about a specific plugin
 */
export async function getPluginDetails(pluginId: string): Promise<PluginDetails> {
	return api.get<PluginDetails>(`/plugins/${encodeURIComponent(pluginId)}`);
}

// ============================================================================
// Plugin Installation API
// ============================================================================

/**
 * Install a single plugin
 */
export async function installPlugin(pluginId: string, scope: string = 'user'): Promise<PluginInfo> {
	return api.post<PluginInfo>('/plugins/install', { plugin_id: pluginId, scope });
}

/**
 * Install multiple plugins at once
 */
export async function installPluginsBatch(pluginIds: string[], scope: string = 'user'): Promise<PluginInfo[]> {
	return api.post<PluginInfo[]>('/plugins/install/batch', { plugin_ids: pluginIds, scope });
}

/**
 * Uninstall a plugin
 */
export async function uninstallPlugin(pluginId: string): Promise<void> {
	await api.delete(`/plugins/${encodeURIComponent(pluginId)}`);
}

// ============================================================================
// Plugin Enable/Disable API
// ============================================================================

/**
 * Enable a plugin globally
 */
export async function enablePlugin(pluginId: string): Promise<{ success: boolean }> {
	return api.post<{ success: boolean }>(`/plugins/${encodeURIComponent(pluginId)}/enable`);
}

/**
 * Disable a plugin globally
 */
export async function disablePlugin(pluginId: string): Promise<{ success: boolean }> {
	return api.post<{ success: boolean }>(`/plugins/${encodeURIComponent(pluginId)}/disable`);
}

// ============================================================================
// File-Based Agents API
// ============================================================================

/**
 * Get all file-based agents from enabled plugins
 */
export async function getFileBasedAgents(): Promise<FileBasedAgent[]> {
	return api.get<FileBasedAgent[]>('/plugins/agents/file-based');
}

// ============================================================================
// Convenience Types for UI
// ============================================================================

export type PluginScope = 'user' | 'project' | 'local';

export interface PluginFilter {
	marketplace?: string;
	hasCommands?: boolean;
	hasAgents?: boolean;
	hasSkills?: boolean;
	hasHooks?: boolean;
	installed?: boolean;
	enabled?: boolean;
}

/**
 * Filter available plugins based on criteria
 */
export function filterPlugins(plugins: AvailablePlugin[], filter: PluginFilter): AvailablePlugin[] {
	return plugins.filter(plugin => {
		if (filter.marketplace && plugin.marketplace !== filter.marketplace) return false;
		if (filter.hasCommands !== undefined && plugin.has_commands !== filter.hasCommands) return false;
		if (filter.hasAgents !== undefined && plugin.has_agents !== filter.hasAgents) return false;
		if (filter.hasSkills !== undefined && plugin.has_skills !== filter.hasSkills) return false;
		if (filter.hasHooks !== undefined && plugin.has_hooks !== filter.hasHooks) return false;
		if (filter.installed !== undefined && plugin.installed !== filter.installed) return false;
		return true;
	});
}

/**
 * Group plugins by marketplace
 */
export function groupPluginsByMarketplace(plugins: AvailablePlugin[]): Record<string, AvailablePlugin[]> {
	return plugins.reduce((groups, plugin) => {
		const key = plugin.marketplace;
		if (!groups[key]) {
			groups[key] = [];
		}
		groups[key].push(plugin);
		return groups;
	}, {} as Record<string, AvailablePlugin[]>);
}
