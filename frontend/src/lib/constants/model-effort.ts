/** Effective effort capabilities shared by every composer surface. */
import {
	decodeProviderModel, encodeProviderModel, getDefaultModel, getModels, getProviders,
	isRegistryHydrated, isReroute, normalizeModel
} from './models.svelte';

export const EFFORT_OPTIONS = [
	{ value: 'none', label: 'None', description: 'No reasoning' },
	{ value: 'minimal', label: 'Minimal', description: 'Minimal reasoning' },
	{ value: 'low', label: 'Low', description: 'Quick, minimal reasoning' },
	{ value: 'medium', label: 'Medium', description: 'Balanced reasoning' },
	{ value: 'high', label: 'High', description: 'Thorough reasoning' },
	{ value: 'xhigh', label: 'XHigh', description: 'Extreme reasoning' },
	{ value: 'max', label: 'Max', description: 'Maximum reasoning depth' },
	{ value: 'ultra', label: 'Ultra', description: 'Reasoning beyond Max' }
];
export const UNKNOWN_EFFORT = 'Reasoning effort capabilities are not known yet. Refresh models in provider settings to try again.';
export const NO_ADJUSTABLE_EFFORT = 'Adjustable reasoning effort is not available for this model in AI Shuffle.';

/** Only apply live routing information while it still describes the selected turn. */
export function resolveEffortModel(
	selection: string | null,
	liveModel: string | null,
	liveRequestedModel: string | null,
	liveProvider: string | null
): string {
	const selected = selection || getDefaultModel();
	const composite = decodeProviderModel(selected);
	const requested = decodeProviderModel(liveRequestedModel);
	const sameRequest = !!liveRequestedModel && (selected === liveRequestedModel ||
		(!composite && !requested && normalizeModel(selected) === normalizeModel(liveRequestedModel)));
	if (!sameRequest) return selected;
	if (liveProvider && !composite) return encodeProviderModel(liveProvider, liveModel || selected);
	if (isReroute(liveModel, liveRequestedModel, liveProvider)) return liveModel!;
	return selected;
}

export function resolveModelEffort(value: string | null | undefined) {
	const selected = value || getDefaultModel();
	let composite = decodeProviderModel(selected);
	const nativeModel = composite ? undefined : getModels().find(m => m.value === normalizeModel(selected));
	// Older sessions may store a bare provider model. Resolve it only when the
	// catalog is unambiguous; never borrow a different provider's capabilities.
	if (!composite && !nativeModel) {
		const matches = getProviders().filter(p => p.models.some(m => m.id === selected));
		if (matches.length === 1) composite = { providerId: matches[0].id, modelId: selected };
	}
	const provider = composite ? getProviders().find(p => p.id === composite.providerId) : undefined;
	const model = composite ? provider?.models.find(m => m.id === composite.modelId) : nativeModel;
	const levels = model?.effort_levels;
	const known = Array.isArray(levels);
	const options = EFFORT_OPTIONS.filter(option => levels?.includes(option.value));
	const defaultValue = options.find(option => option.value === model?.default_effort)?.value ?? null;
	return {
		options, defaultValue, known, ready: isRegistryHydrated(),
		explanation: !known ? UNKNOWN_EFFORT : options.length ? undefined : NO_ADJUSTABLE_EFFORT
	};
}

/** Settings keep their existing inherit option outside this list. */
export function modelEffortOptions(model: string | null, pinned: string | null) {
	const capability = resolveModelEffort(model);
	if (!capability.known && pinned && !capability.options.some(option => option.value === pinned)) {
		return [{ value: pinned, label: pinned, description: 'Saved effort; capabilities unknown' }];
	}
	return capability.options;
}

/** Preserve unknown pins and omitted defaults; reconcile only declared capabilities. */
export function reconcileModelEffort(value: string | null, capability: ReturnType<typeof resolveModelEffort>) {
	if (!capability.ready || !capability.known || value === null || capability.options.some(option => option.value === value)) return value;
	return capability.defaultValue;
}
