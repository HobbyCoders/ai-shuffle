/**
 * The model a subagent task runs on, as the rest of the UI names it (#1827).
 *
 * The backend stamps `task.updated` with the registry's "<provider> · <model>"
 * label: the provider row's display name ("agent" for the native Claude leg),
 * then the model id or "inherit", plus " (note)" when the user asked for that
 * model. This turns it into the same label the model pickers show.
 */
import {
	encodeProviderModel,
	getModelLabel,
	getProviders,
	type ProviderEntry
} from '$lib/constants/models';

export interface TaskModel {
	/** The model's display label, e.g. "Sonnet" or "GPT-5.6". */
	label: string;
	/** The full backend label, for a tooltip. */
	raw: string;
}

const SEPARATOR = ' · ';
const NOTE = /\s*\([^)]*\)\s*$/;

export function taskModel(
	raw: string | null | undefined,
	providers: readonly ProviderEntry[] = getProviders(),
	label: (value: string) => string = getModelLabel
): TaskModel | null {
	const text = (raw ?? '').replace(/\s+/g, ' ').trim();
	if (!text) return null;
	const cut = text.indexOf(SEPARATOR);
	const providerName = cut >= 0 ? text.slice(0, cut).trim() : '';
	const modelId = (cut >= 0 ? text.slice(cut + SEPARATOR.length) : text).replace(NOTE, '').trim();
	const provider = providerName
		? providers.find((p) => p.name === providerName || p.id === providerName)
		: undefined;
	// "inherit": the worker runs on its provider's default model.
	const id = !modelId || modelId === 'inherit' ? provider?.default_model ?? '' : modelId;
	if (!id) return null;
	return { label: label(provider ? encodeProviderModel(provider.id, id) : id), raw: text };
}
