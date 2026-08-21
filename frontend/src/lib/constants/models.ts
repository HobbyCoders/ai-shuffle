/**
 * Claude model registry — Re-export shim.
 * Implementation lives in models.svelte.ts (Svelte 5 runes).
 */
export type {
	ModelFamily,
	ModelOption,
	ProviderModel,
	ProviderEntry,
	ProviderAuthKind,
	ClaudeAuthInfo,
	ProviderDisplay,
	ProviderDisplayInput,
	DecodedProviderModel
} from './models.svelte';
export {
	INHERIT_VALUE,
	CUSTOM_VALUE,
	NATIVE_PROVIDER_KEY,
	CLAUDE_BRAND,
	API_SUBTEXT,
	SUBSCRIPTION_SUBTEXT,
	getProviderDisplay,
	getClaudeDisplay,
	formatProviderDisplay,
	PROVIDER_MODEL_PREFIX,
	encodeProviderModel,
	decodeProviderModel,
	isProviderModel,
	getModels,
	getProviders,
	getClaudeAuth,
	getProviderModels,
	isReroute,
	getDefaultModel,
	isRegistryHydrated,
	normalizeModel,
	isPresetModel,
	getClaudeContextWindow,
	getProviderContextWindow,
	getModelLabel,
	getModelFamily,
	inferModelFamily,
	getModelBadgeClass,
	hydrateModelRegistry,
	ensureModelRegistryHydrated
} from './models.svelte';
