/**
 * Plugin Management API Client
 *
 * Provides functions for managing Fluid plugins (~/.fluid/plugins):
 * - Marketplaces (add, remove, sync)
 * - Plugin installation/uninstallation
 * - Plugin enable/disable
 * - File-based agents discovery
 */

import { api } from './client';

// ============================================================================
// Types
//
// These mirror the Pydantic response models in app/api/plugins.py. Real payloads
// live in ./__fixtures__/plugin-responses.json (regenerated and checked by
// tests/api/test_plugins_frontend_contract.py); plugins.contract.test.ts fails
// when these interfaces and that fixture disagree on a key.
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
	/** ISO timestamp; null for marketplace (not installed) details or an unreadable record. */
	installed_at: string | null;
	enabled: boolean;
	has_commands: boolean;
	has_agents: boolean;
	has_skills: boolean;
	has_hooks: boolean;
	has_mcp: boolean;
	connector_type: string | null;
}

/**
 * GET /plugins/{id}. Commands, agents and skills are listed by name; hooks are
 * reported only as `has_hooks` (the backend does not enumerate them).
 * For a not-yet-installed plugin `scope` is "marketplace", `version` is
 * "available" and `installed_at` is null.
 */
export interface PluginDetails extends PluginInfo {
	commands: string[];
	agents: string[];
	skills: string[];
	readme: string | null;
}

export interface AvailablePlugin {
	id: string;
	name: string;
	marketplace: string;
	/** Local directory of the plugin in its marketplace, or the JSON-encoded external source. */
	marketplace_path: string;
	description: string;
	has_commands: boolean;
	has_agents: boolean;
	has_skills: boolean;
	has_hooks: boolean;
	has_mcp: boolean;
	connector_type: string | null;
	installed: boolean;
	enabled: boolean;
}

export interface FileBasedAgent {
	/** `<plugin id>:<agent file stem>` */
	id: string;
	name: string;
	description: string;
	model: string | null;
	tools: string[] | null;
	prompt: string;
	/** Id of the plugin that provides the agent (`name@marketplace`). */
	source_plugin: string;
	file_path: string;
}

export interface MarketplaceSyncResult {
	status: 'synced';
	marketplace_id: string;
}

export interface PluginUpdateInfo {
	plugin_id: string;
	name: string;
	marketplace: string;
	installed_version: string;
	available_version: string;
	is_local: boolean;
}

export interface AutoUpdateSettings {
	enabled: boolean;
	interval_hours: number;
	auto_sync_marketplaces: boolean;
	auto_update_plugins: boolean;
	last_check: string | null;
}

export interface AutoUpdateRunResult {
	synced_marketplaces: Record<string, boolean>;
	updates_available: PluginUpdateInfo[];
	updated_plugins: PluginInfo[];
	errors: string[];
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
export async function syncMarketplace(marketplaceId: string): Promise<MarketplaceSyncResult> {
	return api.post<MarketplaceSyncResult>(`/plugins/marketplaces/${marketplaceId}/sync`);
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
export async function enablePlugin(pluginId: string): Promise<void> {
	await api.post<void>(`/plugins/${encodeURIComponent(pluginId)}/enable`);
}

/**
 * Disable a plugin globally
 */
export async function disablePlugin(pluginId: string): Promise<void> {
	await api.post<void>(`/plugins/${encodeURIComponent(pluginId)}/disable`);
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
// Plugin Update API
// ============================================================================

/**
 * Check all installed plugins for available updates
 */
export async function checkForUpdates(): Promise<PluginUpdateInfo[]> {
	return api.get<PluginUpdateInfo[]>('/plugins/updates');
}

/**
 * Update a specific plugin to the latest version
 */
export async function updatePlugin(pluginId: string): Promise<PluginInfo> {
	return api.post<PluginInfo>('/plugins/update', { plugin_id: pluginId });
}

/**
 * Update all plugins that have available updates
 */
export async function updateAllPlugins(): Promise<PluginInfo[]> {
	return api.post<PluginInfo[]>('/plugins/update/all');
}

// ============================================================================
// Auto-Update Settings API
// ============================================================================

/**
 * Get auto-update settings
 */
export async function getAutoUpdateSettings(): Promise<AutoUpdateSettings> {
	return api.get<AutoUpdateSettings>('/plugins/auto-update/settings');
}

/**
 * Update auto-update settings
 */
export async function updateAutoUpdateSettings(
	settings: Partial<AutoUpdateSettings>
): Promise<AutoUpdateSettings> {
	return api.put<AutoUpdateSettings>('/plugins/auto-update/settings', settings);
}

/**
 * Manually trigger an auto-update cycle
 */
export async function runAutoUpdate(): Promise<AutoUpdateRunResult> {
	return api.post<AutoUpdateRunResult>('/plugins/auto-update/run');
}
