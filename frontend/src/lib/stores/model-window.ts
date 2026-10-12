/**
 * The context window the model picker shows for ONE selectable model, and the
 * owner's way to set it by hand when the model has no Standard/Extended choice.
 *
 * Three shapes:
 *   - `choice`: a Claude/GPT model with two windows. The picker's
 *     Standard/Extended radio applies; the chat pins its window through
 *     `PUT /sessions/{id}/run-settings` (see `active-session.ts`).
 *   - `single`: one known window (an OpenRouter, Grok or gateway model, or a
 *     native model without a 1M option). The model compacts against that
 *     window, so it IS the compaction point.
 *   - `unknown`: nothing advertises a window (#2274/#2280). Never guessed.
 *
 * For `single` and `unknown` the window is the provider row's per-model
 * `context_window` in `models_json` — the value
 * `model_registry.resolve_provider_context_window` reads first, the registry
 * shows and the turn runs with. Saving it is the existing
 * `PATCH /model-providers/{id}` with the row's stored models sent back as
 * they are, apart from that one entry's window.
 */
import { listModelProviders, updateModelProvider } from '$lib/api/model-providers';
import { getContextMaxForModel } from '$lib/constants/chat';
import { getModelContextControl } from '$lib/constants/model-context';
import {
	decodeProviderModel,
	getProviders,
	hydrateModelRegistry,
	isPresetModel,
	normalizeModel
} from '$lib/constants/models';

export type ModelWindowKind = 'choice' | 'single' | 'unknown';

export interface ModelWindowInfo {
	kind: ModelWindowKind;
	/** The model's window in tokens; null when unknown. Unused for `choice`. */
	window: number | null;
	/** The provider row that owns the model, or null for a native model. */
	providerId: string | null;
	/** The bare model id inside that row. */
	modelId: string;
	/** Whether the owner can set the window by hand from the picker. */
	editable: boolean;
}

/** Smallest and largest window the picker accepts by hand. */
export const MODEL_WINDOW_MIN = 8_000;
export const MODEL_WINDOW_MAX = 10_000_000;

/** Bare model id and owning provider row for a picker value. */
function locate(value: string): { providerId: string | null; modelId: string } {
	const composite = decodeProviderModel(value);
	if (composite) return composite;
	if (isPresetModel(value)) return { providerId: null, modelId: normalizeModel(value) };
	// A bare provider model id: the first hydrated row that lists it.
	const row = getProviders().find((p) => p.models.some((m) => m.id === value));
	return { providerId: row?.id ?? null, modelId: value };
}

export function getModelWindowInfo(value: string | null | undefined): ModelWindowInfo | null {
	if (!value) return null;
	const { providerId, modelId } = locate(value);
	if (getModelContextControl(value)) {
		return { kind: 'choice', window: null, providerId, modelId, editable: false };
	}
	const window = getContextMaxForModel(modelId, providerId);
	// Only a model stored on its row can carry its own window: an uncurated
	// row would have to be curated (and every other model dropped) to save one.
	const editable = providerId != null
		&& !!getProviders().find((p) => p.id === providerId)?.models.some((m) => m.id === modelId);
	return { kind: window ? 'single' : 'unknown', window, providerId, modelId, editable };
}

/**
 * Save `tokens` as `modelId`'s own window on its provider row, then refresh the
 * registry so every picker, the usage meter and the next turn read it. Throws
 * with a plain message when the row or the stored entry cannot be found.
 */
export async function saveModelWindow(providerId: string, modelId: string, tokens: number): Promise<void> {
	const { providers } = await listModelProviders();
	const row = (providers ?? []).find((p) => p.id === providerId);
	if (!row) throw new Error('This provider is no longer set up.');
	const stored = row.models ?? [];
	if (!stored.some((m) => m?.id === modelId)) {
		throw new Error('This model is not saved on its provider. Add it in Settings → Providers first.');
	}
	// The worker replaces `models_json` with this array: every stored entry
	// goes back exactly as stored (aliases, flags, pricing, anything unknown),
	// and only the target's `context_window` changes.
	const models = stored.map((m) =>
		m?.id === modelId ? { ...m, context_window: Math.round(tokens) } : m
	);
	await updateModelProvider(providerId, { models });
	await hydrateModelRegistry();
}
