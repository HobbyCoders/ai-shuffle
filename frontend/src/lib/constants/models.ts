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
	supportsAutoPermission,
	CUSTOM_VALUE,
	NATIVE_PROVIDER_KEY,
	VOICE_PROVIDER_KEY,
	getProviderDisplay,
	getClaudeDisplay,
	formatProviderDisplay,
	PROVIDER_MODEL_PREFIX,
	encodeProviderModel,
	decodeProviderModel,
	isProviderModel,
	subagentPickerValue,
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
	isFastModeCapable,
	getProviderContextWindow,
	getModelLabel,
	getModelFamily,
	getModelBadgeClass,
	hydrateModelRegistry,
	ensureModelRegistryHydrated
} from './models.svelte';
