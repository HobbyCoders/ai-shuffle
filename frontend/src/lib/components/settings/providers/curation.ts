/**
 * Model curation: which of a credential's models the pickers offer.
 *
 * The saved entries carry more than an id and a label: a per-model
 * `context_window` (dropping it flattens every model onto the row's one shared
 * window), OpenRouter `pricing` (server-side spend projection), and the raw and
 * normalized reasoning metadata (`copyReasoningMetadata`). Every rebuild here
 * keeps them; computed registry fields are never saved.
 */
import {
	copyReasoningMetadata,
	mergeReasoningMetadata,
	type FetchModelsResult,
	type ProviderModelEntry
} from '$lib/api/model-providers';

export interface CurationRow extends ProviderModelEntry {
	checked: boolean;
}

/**
 * Rows for a stored credential after a catalog fetch:
 * - stored models missing from the catalog stay, checked, so a save never drops them;
 * - a curated row checks exactly its saved ids; an uncurated one checks the
 *   preset roster and nothing from an upstream fetch;
 * - a stored per-model window and pricing win over the fetched ones; reasoning
 *   evidence merges without downgrading a normalized saved catalog.
 */
export function rowsFromFetch(stored: ProviderModelEntry[], res: FetchModelsResult): CurationRow[] {
	const catalog = res.models ?? [];
	const catalogIds = new Set(catalog.map((m) => m.id));
	const curatedIds = new Set(stored.map((m) => m.id));
	const hasCurated = stored.length > 0;
	const extras = stored
		.filter((m) => !catalogIds.has(m.id))
		.map((m) => ({
			id: m.id,
			label: m.label,
			checked: true,
			context_window: m.context_window,
			pricing: m.pricing,
			...copyReasoningMetadata(m)
		}));
	const storedById = new Map(stored.map((m) => [m.id, m]));
	const catalogRows = catalog.map((m) => {
		const saved = storedById.get(m.id);
		return {
			id: m.id,
			label: m.label,
			checked: hasCurated ? curatedIds.has(m.id) : res.source === 'preset',
			context_window: saved?.context_window ?? m.context_window,
			pricing: saved?.pricing ?? m.pricing,
			...mergeReasoningMetadata(saved, m)
		};
	});
	return [...extras, ...catalogRows];
}

/** When the fetch fails: the stored curation, checked, so nothing gets wiped. */
export function rowsFromStored(stored: ProviderModelEntry[]): CurationRow[] {
	return stored.map((m) => ({
		id: m.id,
		label: m.label,
		checked: true,
		context_window: m.context_window,
		pricing: m.pricing,
		...copyReasoningMetadata(m)
	}));
}

/** Rows for a new credential: nothing checked except what was checked before a refresh. */
export function rowsForSetup(res: FetchModelsResult, selected: Set<string>): CurationRow[] {
	return (res.models ?? []).map((m) => ({
		id: m.id,
		label: m.label,
		checked: selected.has(m.id),
		context_window: m.context_window,
		...copyReasoningMetadata(m)
	}));
}

/** The entries to save: checked rows with their window, metadata and pricing. */
export function curatedEntries(rows: CurationRow[]): ProviderModelEntry[] {
	return rows
		.filter((r) => r.checked)
		.map((r) => ({
			id: r.id.trim(),
			label: (r.label || r.id).trim(),
			...(r.context_window != null ? { context_window: r.context_window } : {}),
			...copyReasoningMetadata(r),
			...(r.pricing ? { pricing: r.pricing } : {})
		}))
		.filter((m) => m.id && m.label);
}

/** A stored row's models passed back unchanged when other fields are saved. */
export function passthroughModels(models: ProviderModelEntry[] | undefined): ProviderModelEntry[] {
	return (models ?? []).map((m) => ({
		id: m.id,
		label: m.label,
		...(m.context_window != null ? { context_window: m.context_window } : {}),
		...copyReasoningMetadata(m),
		...(m.pricing ? { pricing: m.pricing } : {})
	}));
}

export function filterRows(rows: CurationRow[], query: string): CurationRow[] {
	const q = query.trim().toLowerCase();
	if (!q) return rows;
	return rows.filter((r) => r.id.toLowerCase().includes(q) || r.label.toLowerCase().includes(q));
}

export type HiddenMedia = { id: string; label?: string; media_kind?: string };

/** "voice", "image", "video", or "speech". */
export function mediaKindLabel(kind: string | undefined): string {
	if (kind === 'voice' || kind === 'image' || kind === 'video') return kind;
	return 'speech';
}
