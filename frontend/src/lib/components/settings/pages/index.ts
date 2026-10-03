/**
 * Which component renders each category. Preference pages are small and
 * imported statically; collections load on first visit.
 */
import type { Component } from 'svelte';
import type { CategoryId } from '$lib/settings/types';
import GeneralPage from './GeneralPage.svelte';
import ArchivedChatsPage from './ArchivedChatsPage.svelte';
import VoicePage from './VoicePage.svelte';
import SecurityPage from './SecurityPage.svelte';
import SystemPage from './SystemPage.svelte';

export type PreferenceCategory = 'general' | 'archived' | 'voice' | 'security' | 'system';
export type CollectionCategory = Exclude<CategoryId, PreferenceCategory>;

export const PREFERENCE_PAGES: Record<PreferenceCategory, Component> = {
	general: GeneralPage,
	archived: ArchivedChatsPage,
	voice: VoicePage,
	security: SecurityPage,
	system: SystemPage
};

type Loader = () => Promise<{ default: Component }>;

export const COLLECTION_LOADERS: Record<CollectionCategory, Loader> = {
	providers: () => import('../providers/ProvidersCollection.svelte'),
	models: () => import('../models/ModelsCollection.svelte'),
	integrations: () => import('../integrations/IntegrationsCollection.svelte'),
	plugins: () => import('../plugins/PluginsCollection.svelte'),
	mcp: () => import('../mcp/McpCollection.svelte'),
	hooks: () => import('../hooks/HooksCollection.svelte'),
	devices: () => import('../devices/DevicesCollection.svelte'),
	logs: () => import('../logs/LogsCollection.svelte')
};

const loaded = new Map<CollectionCategory, Component>();

/** The collection component if it already loaded (renders without a flash). */
export function loadedCollection(category: CollectionCategory): Component | undefined {
	return loaded.get(category);
}

export async function loadCollection(category: CollectionCategory): Promise<Component> {
	const hit = loaded.get(category);
	if (hit) return hit;
	const mod = await COLLECTION_LOADERS[category]();
	loaded.set(category, mod.default);
	return mod.default;
}

export function isPreferenceCategory(category: CategoryId): category is PreferenceCategory {
	return category in PREFERENCE_PAGES;
}
