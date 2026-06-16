/**
 * Claude model registry — Re-export shim.
 * Implementation lives in models.svelte.ts (Svelte 5 runes).
 */
export type { ModelFamily, ModelOption } from './models.svelte';
export {
	INHERIT_VALUE,
	CUSTOM_VALUE,
	CONTEXT_1M_SUFFIX,
	getModels,
	getDefaultModel,
	isRegistryHydrated,
	normalizeModel,
	isPresetModel,
	has1MContext,
	getBaseModel,
	withContextSuffix,
	modelSupports1M,
	getModelLabel,
	getModelFamily,
	getModelBadgeClass,
	hydrateModelRegistry
} from './models.svelte';
