/**
 * Remembered generation choices (account preferences `generation.choice.<kind>`
 * and `generation.lastChoice.<kind>`), shared by the in-chat choice card,
 * Settings → Default models and the fal.ai catalog.
 *
 * The server validates and owns the records (app/api/generation_choices.py);
 * this store mirrors them and refreshes when another device changes them.
 */
import { api } from '$lib/api/client';
import type {
	CatalogEntry,
	GenerationChoice,
	GenerationKind,
	GenerationOption,
	GenerationPrice,
	ModelPage,
	ModelRow,
	ProvidersResponse,
	SaveQuote
} from '$lib/utils/generation-choice';

export const GENERATION_KINDS: readonly GenerationKind[] = ['image', 'video', 'audio', '3d'];

interface ChoicesResponse {
	remembered: Partial<Record<GenerationKind, GenerationChoice>>;
	last: Partial<Record<GenerationKind, GenerationChoice>>;
	migrated: boolean;
	worker: string;
}

interface CatalogResponse {
	entries: CatalogEntry[];
	favourites: Partial<Record<GenerationKind, GenerationChoice>>;
	kinds: GenerationKind[];
	limit: number;
}

export type { SaveQuote, UnattendedSkipReason } from '$lib/utils/generation-choice';

/** `PUT /generation/choices/{kind}`: the stored pick plus the save-time quote. */
export interface SavedChoice extends SaveQuote {
	kind: GenerationKind;
	choice: GenerationChoice;
	remembered: boolean;
}

export interface Quote {
	ok: boolean;
	price?: GenerationPrice;
	options?: Record<string, unknown>;
	priced?: boolean;
	error?: string;
}

class GenerationChoices {
	remembered = $state<Partial<Record<GenerationKind, GenerationChoice>>>({});
	last = $state<Partial<Record<GenerationKind, GenerationChoice>>>({});
	/** Catalog bookmarks (`generation.favourite.<kind>`); they never run anything. */
	favourites = $state<Partial<Record<GenerationKind, GenerationChoice>>>({});
	/** This worker's device id: picks made in Settings run here. */
	worker = $state<string>('');
	status = $state<'idle' | 'loading' | 'ok' | 'error'>('idle');
	#inflight: Promise<void> | null = null;

	load(force = false): Promise<void> {
		if (this.#inflight && !force) return this.#inflight;
		if (this.status === 'idle' || this.status === 'error') this.status = 'loading';
		this.#inflight = api
			.get<ChoicesResponse>('/generation/choices')
			.then((data) => {
				this.remembered = data.remembered ?? {};
				this.last = data.last ?? {};
				this.worker = data.worker ?? '';
				this.status = 'ok';
			})
			.catch(() => {
				if (this.status !== 'ok') this.status = 'error';
			})
			.finally(() => {
				this.#inflight = null;
			});
		return this.#inflight;
	}

	/**
	 * Record a pick (always the last choice; remembered when `remember`). The
	 * response carries the server's fresh quote and, when unattended runs will
	 * skip the pick, why. It never submits or approves anything.
	 */
	async save(kind: GenerationKind, choice: GenerationChoice, remember: boolean, origin: 'card' | 'settings' = 'card'): Promise<SavedChoice> {
		const { origin: _drop, ...body } = choice;
		void _drop;
		const response = await api.put<SavedChoice>(`/generation/choices/${encodeURIComponent(kind)}`, {
			choice: body,
			remember,
			origin
		});
		this.last = { ...this.last, [kind]: response.choice };
		// The server remembers a pick only once it priced it.
		if (remember && response.remembered !== false) this.remembered = { ...this.remembered, [kind]: response.choice };
		return response;
	}

	/** Ask again next time for this kind. */
	async forget(kind: GenerationKind): Promise<void> {
		await api.delete(`/generation/choices/${encodeURIComponent(kind)}`);
		const next = { ...this.remembered };
		delete next[kind];
		this.remembered = next;
	}

	/** Validate options against the model's declared schema and price them. */
	quote(kind: GenerationKind, choice: GenerationChoice, prompt?: string): Promise<Quote> {
		const { origin: _drop, ...body } = choice;
		void _drop;
		return api.post<Quote>('/generation/quote', { kind, choice: body, prompt: prompt || undefined });
	}

	/**
	 * Every connected account on every worker for one kind, including routes
	 * that cannot run (with the reason), plus the saved favourites. Prices are
	 * live estimates for default options; nothing is reserved or submitted.
	 */
	async catalog(kind: GenerationKind, query?: string, limit = 15): Promise<CatalogEntry[]> {
		const params = new URLSearchParams({ kind, limit: String(limit) });
		if (query?.trim()) params.set('query', query.trim());
		const data = await api.get<CatalogResponse>(`/generation/catalog?${params}`);
		this.favourites = data.favourites ?? {};
		return (data.entries ?? []).map((e) => ({
			...e,
			kind: e.kind ?? kind,
			options: e.options ?? {},
			available: e.available !== false,
			status: e.status ?? (e.available === false ? 'unavailable' : 'available')
		}));
	}

	/** Bookmark one account/model per kind. Never a remembered or job choice. */
	async setFavourite(kind: GenerationKind, choice: GenerationChoice): Promise<GenerationChoice | null> {
		const { origin: _drop, ...body } = choice;
		void _drop;
		const response = await api.put<{ favourite: GenerationChoice | null }>(`/generation/favourites/${encodeURIComponent(kind)}`, body);
		const next = { ...this.favourites };
		if (response.favourite) next[kind] = response.favourite;
		else delete next[kind];
		this.favourites = next;
		return response.favourite;
	}

	async clearFavourite(kind: GenerationKind): Promise<void> {
		await api.delete(`/generation/favourites/${encodeURIComponent(kind)}`);
		const next = { ...this.favourites };
		delete next[kind];
		this.favourites = next;
	}

	/**
	 * The providers/accounts on every worker that can perform `operation`
	 * (default: the kind's text operation), unavailable ones with their reason.
	 */
	providers(kind: GenerationKind, operation?: string): Promise<ProvidersResponse> {
		const q = operation ? `?operation=${encodeURIComponent(operation)}` : '';
		return api.get<ProvidersResponse>(`/generation/providers/${encodeURIComponent(kind)}${q}`);
	}

	/** One page of one provider's models, searched server-side. Rows from fal may be unpriced until picked. */
	async models(
		kind: GenerationKind,
		params: { source: string; operation?: string; query?: string; offset?: number; limit?: number }
	): Promise<ModelPage> {
		const q = new URLSearchParams({ source: params.source, offset: String(params.offset ?? 0), limit: String(params.limit ?? 20) });
		if (params.operation) q.set('operation', params.operation);
		if (params.query?.trim()) q.set('query', params.query.trim());
		const page = await api.get<ModelPage>(`/generation/models/${encodeURIComponent(kind)}?${q}`);
		return {
			...page,
			models: (page.models ?? []).map((m) => ({ ...m, kind, options: m.options ?? {}, available: m.available !== false }))
		};
	}

	/** One named model with its declared options and default-options price, from the worker holding it. */
	async model(kind: GenerationKind, source: string, model: string, operation?: string): Promise<CatalogEntry | null> {
		const params = new URLSearchParams({ kind, source, model, limit: '1' });
		if (operation) params.set('operation', operation);
		const data = await api.get<CatalogResponse>(`/generation/catalog?${params}`);
		const entries = (data.entries ?? []).filter((e) => e.model === model);
		const entry = entries.find((e) => e.available !== false) ?? entries[0];
		return entry ? { ...entry, kind, options: entry.options ?? {}, available: entry.available !== false } : null;
	}

	/**
	 * A browsed model row as a pickable option. A fal row is listed without
	 * its schema, so its declared options and default price are read first.
	 * Throws with the reason when it cannot be picked.
	 */
	async optionFor(kind: GenerationKind, row: ModelRow, operation?: string): Promise<GenerationOption> {
		let option: GenerationOption = row;
		if (row.source === 'fal') {
			const full = await this.model(kind, 'fal', row.model, operation);
			if (!full) throw new Error('This model is no longer listed.');
			option = full;
		}
		if (!option.available) throw new Error(option.reason || 'This model is not available right now.');
		return {
			source: option.source, account: option.account, worker: option.worker, model: option.model, kind,
			options: { ...(option.options ?? {}) }, verified: option.verified, price: option.price,
			quick_settings: option.quick_settings, schema: option.schema, available: true, reason: null
		};
	}

	/** Priced options across accounts, for picking from Settings. */
	async options(kind: GenerationKind, query?: string): Promise<GenerationOption[]> {
		const q = query?.trim() ? `?query=${encodeURIComponent(query.trim())}` : '';
		const data = await api.get<{ options: GenerationOption[] }>(`/generation/options/${encodeURIComponent(kind)}${q}`);
		return (data.options ?? []).map((o) => ({ ...o, kind, options: o.options ?? {}, available: o.available !== false }));
	}
}

export const generationChoices = new GenerationChoices();
