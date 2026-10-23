/**
 * Settings > Model tuning: what each model in the picker starts a new chat
 * with, and where that can be changed.
 *
 * The per-model records are the composer's own account preferences
 * (`composer.modelEffort.*`, `composer.modelFast.*`,
 * `composer.modelContextCap.*`), keyed by the canonical bare model id. One
 * record therefore covers every route that serves that model (ChatGPT and an
 * OpenAI key both serving gpt-6.1-sol share one effort). This page groups the
 * picker's options by that key, so a value set here is exactly the value the
 * composer reads.
 *
 * Each route keeps its own capabilities: a level one route does not offer is
 * fitted there the way the composer fits it (`reconcileModelEffort`).
 * Local sampling and thinking (`local_settings`) stay per provider row; they
 * show only for a route on a custom OpenAI-dialect gateway.
 */
import { buildModelPickerCatalog } from '$lib/constants/model-picker';
import {
	decodeProviderModel,
	getProviders,
	isFastModeCapable,
	isRegistryHydrated
} from '$lib/constants/models.svelte';
import { EFFORT_OPTIONS, reconcileModelEffort, resolveModelEffort } from '$lib/constants/model-effort';
import { formatContextTokens, getModelContextControl, standardCapPresets, type ModelContextControl } from '$lib/constants/model-context';
import { composerPreferences, effortModelKey } from '$lib/stores/composer-preferences';
import { providerGlyphKind, type ProviderGlyphKind } from '../ProviderGlyph.svelte';
import type { ModelProvider } from '$lib/api/model-providers';
import { supportsLocalTuning } from '../providers/local-tuning';

/** Item id prefix: model keys can be anything a gateway lists, so keep them apart from other ids. */
const ITEM_PREFIX = 'model:';
export const tuningItem = (key: string): string => ITEM_PREFIX + key;
export const parseTuningItem = (item: string | null | undefined): string | null =>
	item?.startsWith(ITEM_PREFIX) ? item.slice(ITEM_PREFIX.length) : null;

export type EffortCapability = ReturnType<typeof resolveModelEffort>;

/** One way to run a model: a picker option under one provider group. */
export interface TuningRoute {
	/** The picker value: a native alias or a `prov:<row>:<model>` composite. */
	value: string;
	/** Provider row id; null for native Claude. */
	providerId: string | null;
	group: string;
	groupSubtext: string | null;
	glyph: ProviderGlyphKind;
	/** The id this route sends. */
	modelId: string;
	effort: EffortCapability;
	fast: boolean;
	context: ModelContextControl | null;
	/** False when the worker says this route cannot run now. */
	usable: boolean;
}

/** One canonical model: one set of saved records, one or more routes. */
export interface TuningModel {
	key: string;
	item: string;
	label: string;
	/** Group of the first route; the list groups by it. */
	group: string;
	glyph: ProviderGlyphKind;
	routes: TuningRoute[];
}

/** Every model in the picker, one entry per saved-preference key, in picker order. */
export function collectTuningModels(): TuningModel[] {
	const catalog = buildModelPickerCatalog();
	const groups = new Map(catalog.groups.map((g) => [g.key, g]));
	const providers = getProviders();
	const byKey = new Map<string, TuningModel>();
	for (const option of catalog.options) {
		const group = groups.get(option.groupKey);
		const composite = decodeProviderModel(option.value);
		const row = composite ? providers.find((p) => p.id === composite.providerId) : undefined;
		const route: TuningRoute = {
			value: option.value,
			providerId: composite?.providerId ?? null,
			group: option.group,
			groupSubtext: option.groupSubtext,
			glyph: group?.glyph ?? providerGlyphKind(row?.provider_type),
			modelId: composite?.modelId ?? option.value,
			effort: resolveModelEffort(option.value),
			fast: isFastModeCapable(option.value),
			context: getModelContextControl(option.value),
			usable: row ? row.usable !== false : true
		};
		const key = effortModelKey(option.value);
		const hit = byKey.get(key);
		if (hit) {
			if (!hit.routes.some((r) => r.value === route.value)) hit.routes.push(route);
			continue;
		}
		byKey.set(key, { key, item: tuningItem(key), label: option.label, group: route.group, glyph: route.glyph, routes: [route] });
	}
	return [...byKey.values()];
}

export const tuningReady = (): boolean => isRegistryHydrated();

// ---------------------------------------------------------------------------
// Effort
// ---------------------------------------------------------------------------

export interface EffortChoice {
	value: string;
	label: string;
}

/** The value `null` stands for in a select: Auto (no effort sent). */
export const AUTO = '';

export type EffortState =
	| { kind: 'unknown'; reason: string }
	| { kind: 'fixed'; reason: string }
	| { kind: 'adjustable'; options: EffortChoice[]; vendorDefault: string | null; initial: string | null };

const labelOf = (value: string | null): string =>
	value === null ? 'Auto' : (EFFORT_OPTIONS.find((o) => o.value === value)?.label ?? value);
export const effortLabel = labelOf;

/** A level as the page shows it: Auto names the provider default it stands for, when known. */
export function effortText(state: EffortState, value: string | null): string {
	if (value === null && state.kind === 'adjustable' && state.vendorDefault) return `Auto (${labelOf(state.vendorDefault)})`;
	return labelOf(value);
}

/**
 * What the Effort control offers: every level any route offers, in the
 * vocabulary's order. With routes that disagree, the composer fits the
 * saved level per route; `routeEffort` says what each one runs.
 */
export function effortState(model: TuningModel): EffortState {
	const caps = model.routes.map((r) => r.effort);
	const known = caps.filter((c) => c.known);
	if (!known.length) return { kind: 'unknown', reason: caps[0]?.explanation ?? 'Capabilities are not known yet.' };
	const offered = new Set(known.flatMap((c) => c.options.map((o) => o.value)));
	if (!offered.size) return { kind: 'fixed', reason: known[0].explanation ?? 'This model has no effort setting.' };
	const options = EFFORT_OPTIONS.filter((o) => offered.has(o.value)).map((o) => ({ value: o.value, label: o.label }));
	const first = known.find((c) => c.options.length) ?? known[0];
	return { kind: 'adjustable', options, vendorDefault: first.defaultValue, initial: first.initialValue };
}

/** True when this account has its own effort record for the model (Auto counts). */
export function effortSaved(model: TuningModel): boolean {
	return Object.prototype.hasOwnProperty.call(composerPreferences.current.modelEfforts, model.key);
}

/**
 * The level the Effort control shows: the saved record as stored, else what a
 * never-picked model starts with (`composerPreferences.effortForModel`: an
 * older family record, the measured initial pick, or Auto).
 */
export function selectedEffort(model: TuningModel): string | null {
	if (effortSaved(model)) return composerPreferences.current.modelEfforts[model.key] ?? null;
	return composerPreferences.effortForModel(model.routes[0].value);
}

/** What a new chat on one route starts with, fitted to that route's levels like the composer does. */
export function routeEffort(route: TuningRoute): string | null {
	return reconcileModelEffort(composerPreferences.effortForModel(route.value), route.effort);
}

// ---------------------------------------------------------------------------
// Fast mode and context
// ---------------------------------------------------------------------------

export const fastRoutes = (model: TuningModel): TuningRoute[] => model.routes.filter((r) => r.fast);

/** The first route's adjustable Standard window, if any route has one. */
export function contextControl(model: TuningModel): ModelContextControl | null {
	return model.routes.find((r) => r.context)?.context ?? null;
}

/** Quick picks for the Standard cap, plus a saved value that is not one of them. */
export function contextChoices(control: ModelContextControl, saved: number | null): EffortChoice[] {
	const values = standardCapPresets(control);
	if (saved != null && !values.includes(saved)) values.push(saved);
	values.sort((a, b) => a - b);
	return values.map((v) => ({
		value: String(v),
		label: v === control.standardTokens ? `${formatContextTokens(v)} (default)` : formatContextTokens(v)
	}));
}

// ---------------------------------------------------------------------------
// Local server routes and the list summary
// ---------------------------------------------------------------------------

/** Rows (from Settings > Providers) that can carry local tuning for this model, with that model listed. */
export function localRows(model: TuningModel, rows: readonly ModelProvider[]): { row: ModelProvider; modelId: string }[] {
	const result: { row: ModelProvider; modelId: string }[] = [];
	for (const route of model.routes) {
		const row = route.providerId ? rows.find((r) => r.id === route.providerId) : undefined;
		if (row && supportsLocalTuning(row) && row.models.some((m) => m.id === route.modelId)) {
			result.push({ row, modelId: route.modelId });
		}
	}
	return result;
}

/** One-line list summary: what differs from a fresh model. */
export function tuningSummary(model: TuningModel, rows: readonly ModelProvider[]): string {
	const parts: string[] = [];
	const effort = effortState(model);
	if (effort.kind === 'adjustable') parts.push(`Effort ${effortText(effort, selectedEffort(model))}`);
	else if (effort.kind === 'unknown') parts.push('Effort not known yet');
	if (fastRoutes(model).length && composerPreferences.fastForModel(model.routes[0].value) === true) parts.push('Fast');
	const cap = composerPreferences.contextCapForModel(model.routes[0].value);
	if (cap != null && contextControl(model)) parts.push(formatContextTokens(cap));
	if (localRows(model, rows).some(({ row, modelId }) => row.models.find((m) => m.id === modelId)?.local_settings)) {
		parts.push('Local tuning');
	}
	return parts.join(' · ') || 'No adjustable settings';
}

/** True when any record differs from a never-touched model. */
export function hasSavedTuning(model: TuningModel, rows: readonly ModelProvider[]): boolean {
	const value = model.routes[0].value;
	const effort = effortState(model);
	const savedEffort = effort.kind === 'adjustable' && effortSaved(model) && selectedEffort(model) !== effort.initial;
	return (
		savedEffort ||
		composerPreferences.fastForModel(value) != null ||
		composerPreferences.contextCapForModel(value) != null ||
		localRows(model, rows).some(({ row, modelId }) => !!row.models.find((m) => m.id === modelId)?.local_settings)
	);
}
