/**
 * State for Settings → Default models → Catalog, "All accounts" tab: the
 * all-account generation catalog (`GET /generation/catalog`) for one kind,
 * grouped by the worker that holds each account.
 *
 * The server owns verification, status and price; this only loads, filters
 * by a debounced search and groups. Nothing here can start a generation:
 * a favourite is a bookmark, separate from the remembered choice.
 */
import { api, getErrorMessage } from '$lib/api/client';
import { refreshWorkerPresence } from '$lib/stores/worker-presence';
import { generationChoices } from '$lib/stores/generation-choices.svelte';
import type { CatalogEntry, CatalogStatus, GenerationKind } from '$lib/utils/generation-choice';

export const SEARCH_DEBOUNCE_MS = 300;

const STATUS_RANK: Record<CatalogStatus, number> = { available: 0, unpriced: 1, unavailable: 2 };

export interface WorkerGroup {
	worker: string;
	local: boolean;
	entries: CatalogEntry[];
}

/** Local worker first, then by id; inside a worker, runnable rows first. */
export function groupByWorker(entries: readonly CatalogEntry[], self: string): WorkerGroup[] {
	const groups = new Map<string, CatalogEntry[]>();
	for (const entry of entries) {
		const list = groups.get(entry.worker) ?? [];
		list.push(entry);
		groups.set(entry.worker, list);
	}
	return [...groups.entries()]
		.map(([worker, list]) => ({
			worker,
			local: worker === self || worker === 'local',
			entries: [...list].sort(
				(a, b) =>
					(STATUS_RANK[a.status] ?? 3) - (STATUS_RANK[b.status] ?? 3) ||
					a.source.localeCompare(b.source) ||
					a.model.localeCompare(b.model)
			)
		}))
		.sort((a, b) => Number(b.local) - Number(a.local) || a.worker.localeCompare(b.worker));
}

/** Distinct accounts (one account on two workers counts twice: two routes). */
export function accountCount(entries: readonly CatalogEntry[]): number {
	return new Set(entries.map((e) => `${e.source}\u0000${e.account}\u0000${e.worker}`)).size;
}

export class AccountCatalog {
	kind = $state<GenerationKind>('image');
	query = $state('');
	entries = $state<CatalogEntry[] | null>(null);
	loading = $state(false);
	error = $state('');
	/** Local model-provider id → its name, to label subscription accounts. */
	accountNames = $state<Record<string, string>>({});
	#seq = 0;
	#timer: ReturnType<typeof setTimeout> | null = null;

	start(): () => void {
		void this.load();
		void this.loadAccountNames();
		refreshWorkerPresence().catch(() => {
			// Names fall back to a clipped worker id.
		});
		return () => {
			if (this.#timer) clearTimeout(this.#timer);
			this.#timer = null;
			this.#seq++;
		};
	}

	async load(): Promise<void> {
		if (this.#timer) clearTimeout(this.#timer);
		this.#timer = null;
		const mine = ++this.#seq;
		this.loading = true;
		this.error = '';
		try {
			const list = await generationChoices.catalog(this.kind, this.query);
			if (mine === this.#seq) this.entries = list;
		} catch (e) {
			if (mine === this.#seq) this.error = getErrorMessage(e, 'Could not load the catalog.');
		} finally {
			if (mine === this.#seq) this.loading = false;
		}
	}

	setKind(kind: GenerationKind): void {
		if (kind === this.kind) return;
		this.kind = kind;
		this.entries = null;
		void this.load();
	}

	setQuery(query: string): void {
		if (query === this.query) return;
		this.query = query;
		if (this.#timer) clearTimeout(this.#timer);
		this.#timer = setTimeout(() => void this.load(), SEARCH_DEBOUNCE_MS);
	}

	async loadAccountNames(): Promise<void> {
		try {
			const data = await api.get<{ providers?: { id: string; name: string }[] }>('/model-providers');
			this.accountNames = Object.fromEntries((data.providers ?? []).map((p) => [String(p.id), p.name]));
		} catch {
			// Optional: rows still show the source.
		}
	}

	/** `ChatGPT plan · Work` when the local registry names the account. */
	accountName(entry: Pick<CatalogEntry, 'account' | 'source'>): string | null {
		if (entry.source === 'fal') return null;
		const name = this.accountNames[entry.account];
		return name && name.trim() ? name.trim() : null;
	}
}
