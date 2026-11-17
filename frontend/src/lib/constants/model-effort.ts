/** Effective effort capabilities shared by every composer surface. */
import {
	decodeProviderModel, encodeProviderModel, getDefaultModel, getModelLabel, getNativeModel, getProviders,
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
	liveProvider: string | null,
	liveRerouted: boolean | null = null
): string {
	const selected = selection || getDefaultModel();
	const composite = decodeProviderModel(selected);
	const requested = decodeProviderModel(liveRequestedModel);
	const sameRequest = !!liveRequestedModel && (selected === liveRequestedModel ||
		(!composite && !requested && normalizeModel(selected) === normalizeModel(liveRequestedModel)));
	if (!sameRequest) return selected;
	if (liveProvider && !composite) return encodeProviderModel(liveProvider, liveModel || selected);
	if (isReroute(liveModel, liveRequestedModel, liveProvider, liveRerouted)) return liveModel!;
	return selected;
}

/**
 * Whether the composer's selection is still the model a turn REQUESTED (#1635).
 * A reroute note describes that turn only: once another model is picked, the
 * chip names the pick. The note names the bare provider model id, so a
 * `prov:` pick matches on its model id; native aliases match through the
 * registry, and two values the picker labels the same are the same pick.
 */
export function selectionIsRequestedModel(
	selection: string | null,
	requested: string | null,
	liveProvider: string | null = null
): boolean {
	if (!requested) return false;
	const selected = selection || getDefaultModel();
	if (selected === requested) return true;
	const pick = decodeProviderModel(selected);
	const asked = decodeProviderModel(requested);
	if (pick && !asked) {
		return pick.modelId === requested && (!liveProvider || liveProvider === pick.providerId);
	}
	if (!pick && !asked && normalizeModel(selected) === normalizeModel(requested)) return true;
	return getModelLabel(selected) === getModelLabel(requested);
}

export function resolveModelEffort(value: string | null | undefined) {
	const selected = value || getDefaultModel();
	let composite = decodeProviderModel(selected);
	const nativeModel = composite ? undefined : getNativeModel(selected);
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
	const initialValue = options.find(option => option.value === model?.initial_effort)?.value ?? null;
	return {
		options, defaultValue, initialValue, known, ready: isRegistryHydrated(),
		explanation: !known ? UNKNOWN_EFFORT : options.length ? undefined : NO_ADJUSTABLE_EFFORT
	};
}

/**
 * What effort reads while no level is pinned: "Auto", naming the level the
 * model runs at when it declares one, the level the controls mark (#1526).
 */
export function autoEffortLabel(options: readonly { value: string; label: string }[], defaultValue: string | null | undefined) {
	const level = defaultValue ? options.find(option => option.value === defaultValue)?.label : undefined;
	return level ? `Auto (${level})` : 'Auto';
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
	const rank = EFFORT_OPTIONS.findIndex(option => option.value === value);
	if (rank < 0 || !capability.options.length) return capability.defaultValue;
	return capability.options.reduce((best, option) =>
		Math.abs(EFFORT_OPTIONS.findIndex(level => level.value === option.value) - rank)
		< Math.abs(EFFORT_OPTIONS.findIndex(level => level.value === best.value) - rank) ? option : best
	).value;
}

/**
 * What a model switch did to a pinned effort level (#1641): the composer fits
 * the level to the new model's own levels, and says so.
 */
export function effortFitNotice(previous: string, fitted: string | null, model: string): string {
	const label = (value: string) => EFFORT_OPTIONS.find((option) => option.value === value)?.label ?? value;
	const name = getModelLabel(model);
	return fitted
		? `Effort set to ${label(fitted)}: ${name} has no ${label(previous)}.`
		: `Effort set to Auto: ${name} has no ${label(previous)}.`;
}
