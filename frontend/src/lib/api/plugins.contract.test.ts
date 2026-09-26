/**
 * Plugin API types vs. what the backend really returns.
 *
 * __fixtures__/plugin-responses.json is produced from the real FastAPI routes
 * by tests/api/test_plugins_frontend_contract.py (which fails when the backend
 * drifts from it). Here each key list is checked at compile time to name every
 * key of its interface and nothing else, and at run time to equal the keys of
 * the fixture payload — so a renamed, added or removed field on either side
 * fails a test instead of silently reading `undefined` in the UI.
 */

import { describe, expect, it, vi } from 'vitest';
import fixture from './__fixtures__/plugin-responses.json';
import type {
	AvailablePlugin,
	FileBasedAgent,
	MarketplaceSyncResult,
	PluginDetails,
	PluginInfo
} from './plugins';

const post = vi.hoisted(() => vi.fn());
vi.mock('./client', () => ({ api: { post, get: vi.fn(), put: vi.fn(), delete: vi.fn() } }));

import { syncMarketplace } from './plugins';

type Missing<T, K extends readonly PropertyKey[]> = Exclude<keyof T, K[number]>;

/** Accepts only a tuple that lists every key of T (unknown keys are rejected by the constraint). */
function keysOf<T>() {
	return <const K extends readonly (keyof T)[]>(
		keys: K & ([Missing<T, K>] extends [never] ? unknown : { missingKeys: Missing<T, K> })
	): string[] => [...keys].map(String).sort();
}

const PLUGIN_INFO_KEYS = keysOf<PluginInfo>()([
	'id', 'name', 'marketplace', 'description', 'version', 'scope', 'install_path', 'installed_at',
	'enabled', 'has_commands', 'has_agents', 'has_skills', 'has_hooks', 'has_mcp', 'connector_type'
]);
const DETAILS_KEYS = keysOf<PluginDetails>()([
	'id', 'name', 'marketplace', 'description', 'version', 'scope', 'install_path', 'installed_at',
	'enabled', 'has_commands', 'has_agents', 'has_skills', 'has_hooks', 'has_mcp', 'connector_type',
	'commands', 'agents', 'skills', 'readme'
]);
const AVAILABLE_KEYS = keysOf<AvailablePlugin>()([
	'id', 'name', 'marketplace', 'marketplace_path', 'description', 'has_commands', 'has_agents',
	'has_skills', 'has_hooks', 'has_mcp', 'connector_type', 'installed', 'enabled'
]);
const AGENT_KEYS = keysOf<FileBasedAgent>()([
	'id', 'name', 'description', 'model', 'tools', 'prompt', 'source_plugin', 'file_path'
]);
const SYNC_KEYS = keysOf<MarketplaceSyncResult>()(['status', 'marketplace_id']);

// Compile-time: the real payloads are assignable to the declared types.
const available: AvailablePlugin[] = fixture.available;
const installed: PluginInfo[] = fixture.installed;
const detailsInstalled: PluginDetails = fixture.details_installed;
const detailsAvailable: PluginDetails = fixture.details_available;
const agents: FileBasedAgent[] = fixture.file_based_agents;

const sortedKeys = (o: object) => Object.keys(o).sort();

describe('plugin API types match backend payloads', () => {
	it('marketplace sync returns { status, marketplace_id }', async () => {
		expect(sortedKeys(fixture.marketplace_sync)).toEqual(SYNC_KEYS);
		post.mockResolvedValueOnce(fixture.marketplace_sync);
		const result: MarketplaceSyncResult = await syncMarketplace('mp');
		expect(post).toHaveBeenCalledWith('/plugins/marketplaces/mp/sync');
		expect(result).toEqual({ status: 'synced', marketplace_id: 'mp' });
	});

	it('available plugins carry marketplace_path and enabled, not path / installed_version', () => {
		expect(available.length).toBeGreaterThan(0);
		for (const plugin of available) expect(sortedKeys(plugin)).toEqual(AVAILABLE_KEYS);
	});

	it('installed plugins and plugin details (installed and marketplace) have the declared keys', () => {
		for (const plugin of installed) expect(sortedKeys(plugin)).toEqual(PLUGIN_INFO_KEYS);
		expect(sortedKeys(detailsInstalled)).toEqual(DETAILS_KEYS);
		expect(sortedKeys(detailsAvailable)).toEqual(DETAILS_KEYS);
		// Hooks are a flag only; there is no list of hook names to render.
		expect(detailsInstalled).not.toHaveProperty('hooks');
		expect(detailsInstalled.has_hooks).toBe(true);
		// A marketplace (not installed) plugin has no install time.
		expect(detailsAvailable).toMatchObject({ scope: 'marketplace', installed_at: null });
	});

	it('file-based agents name their plugin via source_plugin and include the prompt', () => {
		expect(agents.length).toBeGreaterThan(0);
		for (const agent of agents) expect(sortedKeys(agent)).toEqual(AGENT_KEYS);
		expect(agents[0].id.startsWith(`${agents[0].source_plugin}:`)).toBe(true);
	});
});
