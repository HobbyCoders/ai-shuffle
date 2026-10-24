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
	GenerationPrice
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

	/** Record a pick (always the last choice; remembered when `remember`). */
	async save(kind: GenerationKind, choice: GenerationChoice, remember: boolean, origin: 'card' | 'settings' = 'card'): Promise<GenerationChoice> {
		const { origin: _drop, ...body } = choice;
		void _drop;
		const response = await api.put<{ choice: GenerationChoice }>(`/generation/choices/${encodeURIComponent(kind)}`, {
			choice: body,
			remember,
			origin
		});
		this.last = { ...this.last, [kind]: response.choice };
		if (remember) this.remembered = { ...this.remembered, [kind]: response.choice };
		return response.choice;
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

	/** Priced options across accounts, for picking from Settings. */
	async options(kind: GenerationKind, query?: string): Promise<GenerationOption[]> {
		const q = query?.trim() ? `?query=${encodeURIComponent(query.trim())}` : '';
		const data = await api.get<{ options: GenerationOption[] }>(`/generation/options/${encodeURIComponent(kind)}${q}`);
		return (data.options ?? []).map((o) => ({ ...o, kind, options: o.options ?? {}, available: o.available !== false }));
	}
}

export const generationChoices = new GenerationChoices();
