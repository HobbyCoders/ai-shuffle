/**
 * Memory: the data and form state behind Settings > Memory.
 *
 * - The list is everything the worker has saved for the owner, active and
 *   archived. Memory changes publish no resource event, so it loads when the
 *   page opens, and each write updates its own row from the worker's answer.
 * - Each opened memory keeps its own form; saving is explicit. A form remembers
 *   the version it was opened from, so a save or delete can never overwrite a
 *   newer version it has not seen: the worker answers 409 and the draft stays.
 * - A row just saved stays listed until the filter or search changes, even if
 *   it no longer matches, so saving never throws the owner out of the row.
 * - The worker collapses whitespace when it saves, so "changed" and the
 *   character counts compare the collapsed text, as the worker will.
 */
import { api } from '$lib/api/client';
import { isForbidden } from '$lib/settings/access.svelte';
import { relativeAgo, shortDate } from '../integrations/api-users.svelte';

export type MemoryStatus = 'active' | 'archived';
export type MemoryTier = 'user' | 'agent' | 'learned';
export type MemoryFilter = 'active' | 'archived' | 'review' | 'all';

export interface MemoryItem {
	id: string;
	revision: number;
	/** Moves with what `revision` misses: the when-line, sync conflicts, replacement links. */
	metadata_version: string;
	text: string;
	when: string | null;
	status: MemoryStatus;
	tier: MemoryTier;
	source: string;
	scope: string;
	scope_label: string;
	always: boolean;
	needs_review: boolean;
	this_worker_only: boolean;
	created_at: number | null;
	updated_at: number | null;
	last_recalled_at: number | null;
	expires_at: number | null;
	uses: number;
}

export interface MemoryLimits {
	text_chars: number;
	when_chars: number;
}

interface MemoryList {
	items: MemoryItem[];
	limits: MemoryLimits;
}

export interface MemoryForm {
	text: string;
	when: string;
	/** What the form was opened from; a later list refresh does not move it. */
	base: { text: string; when: string; revision: number; metadataVersion: string };
}

export interface MemoryProblems {
	text?: string;
	when?: string;
}

export interface MemoryError {
	message: string;
	/** The saved version moved on: offer to load it. */
	conflict: boolean;
}

export type RemoteChange = 'changed' | 'deleted';

/** Rows drawn at once; "Show more" adds this many. Stores hold 500 or more. */
export const PAGE_SIZE = 100;

export const FILTER_OPTIONS: { value: MemoryFilter; label: string }[] = [
	{ value: 'active', label: 'Active' },
	{ value: 'archived', label: 'Archived' },
	{ value: 'review', label: 'Needs review' },
	{ value: 'all', label: 'All' }
];

const SAVED_BY: Record<MemoryTier, string> = {
	user: 'You',
	agent: 'An agent',
	learned: 'Learned automatically'
};

const SAVED_LINE: Record<MemoryTier, string> = {
	user: 'You saved',
	agent: 'An agent saved',
	learned: 'Learned automatically'
};

export const compact = (value: string): string => value.split(/\s+/).filter(Boolean).join(' ');

const iso = (seconds: number): string => new Date(seconds * 1000).toISOString();

export const agoText = (seconds: number | null): string => (seconds ? relativeAgo(iso(seconds)) : '');
export const dateText = (seconds: number | null): string => (seconds ? shortDate(iso(seconds)) : '');
export const savedByText = (m: Pick<MemoryItem, 'tier'>): string => SAVED_BY[m.tier];

/** The quiet line under a row: "You saved · All projects · 3d ago". */
export function memoryMeta(m: MemoryItem): string {
	return [
		m.needs_review ? 'Needs review' : '',
		m.status === 'archived' ? 'Archived' : '',
		SAVED_LINE[m.tier],
		m.scope_label,
		agoText(m.updated_at ?? m.created_at)
	]
		.filter(Boolean)
		.join(' · ');
}

export function matchesFilter(m: MemoryItem, filter: MemoryFilter): boolean {
	if (filter === 'all') return true;
	if (filter === 'review') return m.needs_review;
	return m.status === filter;
}

/** Lower-cased once per row, so a keystroke only runs `includes`. */
const searchable = new WeakMap<MemoryItem, string>();

function haystack(m: MemoryItem): string {
	let text = searchable.get(m);
	if (text === undefined) {
		text = `${m.text}\n${m.when ?? ''}`.toLowerCase();
		searchable.set(m, text);
	}
	return text;
}

export function filterMemories(
	items: readonly MemoryItem[],
	view: { filter: MemoryFilter; scope: string; query: string },
	keep: ReadonlySet<string> = new Set()
): MemoryItem[] {
	const q = view.query.trim().toLowerCase();
	return items.filter(
		(m) =>
			keep.has(m.id) ||
			(matchesFilter(m, view.filter) && (!view.scope || m.scope_label === view.scope) && (!q || haystack(m).includes(q)))
	);
}

export function formFromMemory(m: MemoryItem): MemoryForm {
	const when = m.when ?? '';
	return { text: m.text, when, base: { text: m.text, when, revision: m.revision, metadataVersion: m.metadata_version } };
}

export function validateMemoryForm(f: MemoryForm, limits: MemoryLimits): MemoryProblems {
	const problems: MemoryProblems = {};
	const text = compact(f.text);
	const when = compact(f.when);
	if (!text) problems.text = 'A memory needs some text.';
	else if (text.length > limits.text_chars) problems.text = `Keep this under ${limits.text_chars.toLocaleString()} characters.`;
	if (when.length > limits.when_chars) problems.when = `Keep this under ${limits.when_chars.toLocaleString()} characters.`;
	// The worker keeps an existing line when none is sent, so blank cannot remove one.
	else if (!when && compact(f.base.when)) problems.when = 'You can reword this line, but it can’t be left empty.';
	return problems;
}

function message(e: unknown, fallback: string): string {
	if (e && typeof e === 'object') {
		const detail = (e as { detail?: unknown; message?: unknown }).detail ?? (e as { message?: unknown }).message;
		if (typeof detail === 'string' && detail) return detail;
	}
	return fallback;
}

const statusOf = (e: unknown): number | undefined =>
	e && typeof e === 'object' ? ((e as { status?: number }).status ?? undefined) : undefined;

const path = (id: string) => `/memories/${encodeURIComponent(id)}`;

/** A dropped connection can't say whether the write landed, so don't claim either way. */
const UNREACHABLE = {
	change: 'Could not reach this worker, so this may not have saved. Your edits are still here. Try again.',
	delete: 'Could not reach this worker, so this may not have been deleted. Try again.'
} as const;

export class MemoryModel {
	/** Replaced, never mutated: rows are read many times and edited rarely. */
	items = $state.raw<MemoryItem[]>([]);
	limits = $state<MemoryLimits>({ text_chars: 2000, when_chars: 140 });
	loading = $state(false);
	loaded = $state(false);
	forbidden = $state(false);
	loadError = $state('');
	forms = $state<Record<string, MemoryForm>>({});
	errors = $state<Record<string, MemoryError>>({});
	remote = $state<Record<string, RemoteChange>>({});
	saving = $state<string | null>(null);

	#filter = $state<MemoryFilter>('active');
	#scope = $state('');
	#query = $state('');
	#limit = $state(PAGE_SIZE);
	#keep = $state.raw<ReadonlySet<string>>(new Set());
	/** Only the newest request may land; a write makes older reads stale. */
	#requests = 0;
	#pending = 0;

	readonly visible = $derived(
		filterMemories(this.items, { filter: this.#filter, scope: this.#scope, query: this.#query }, this.#keep)
	);
	readonly shown = $derived(this.visible.slice(0, this.#limit));
	readonly hiddenCount = $derived(Math.max(0, this.visible.length - this.#limit));
	readonly scopes = $derived([...new Set(this.items.map((m) => m.scope_label))].sort((a, b) => a.localeCompare(b)));
	readonly anyDirty = $derived(Object.keys(this.forms).some((id) => this.dirty(id)));

	get filter(): MemoryFilter {
		return this.#filter;
	}
	set filter(value: MemoryFilter) {
		if (value !== this.#filter) this.#setView(() => (this.#filter = value));
	}

	get scope(): string {
		return this.#scope;
	}
	set scope(value: string) {
		if (value !== this.#scope) this.#setView(() => (this.#scope = value));
	}

	get query(): string {
		return this.#query;
	}
	set query(value: string) {
		if (value !== this.#query) this.#setView(() => (this.#query = value));
	}

	get narrowed(): boolean {
		return !!this.#query.trim() || !!this.#scope;
	}

	#setView(change: () => void): void {
		change();
		this.#limit = PAGE_SIZE;
		this.#keep = new Set();
	}

	clearSearch(): void {
		this.#setView(() => {
			this.#query = '';
			this.#scope = '';
		});
	}

	showMore(): void {
		this.#limit += PAGE_SIZE;
	}

	item(id: string): MemoryItem | undefined {
		return this.items.find((m) => m.id === id);
	}

	/** True when the load succeeded and is still the newest word on the list. */
	async load(): Promise<boolean> {
		const request = ++this.#requests;
		this.#pending += 1;
		this.loading = true;
		try {
			const data = await api.get<MemoryList>('/memories');
			if (request !== this.#requests) return false;
			this.items = data.items ?? [];
			if (data.limits) this.limits = data.limits;
			this.forbidden = false;
			this.loadError = '';
			this.loaded = true;
			this.#reconcile();
			return true;
		} catch (e) {
			if (request === this.#requests) {
				if (isForbidden(e)) this.forbidden = true;
				else this.loadError = 'Could not load your memories.';
			}
			return false;
		} finally {
			this.#pending -= 1;
			this.loading = this.#pending > 0;
		}
	}

	/** The form for a memory. Call outside rendering (component init). */
	ensure(id: string): MemoryForm | undefined {
		if (this.forms[id]) return this.forms[id];
		const m = this.item(id);
		if (!m) return undefined;
		this.forms[id] = formFromMemory(m);
		return this.forms[id];
	}

	dirty(id: string): boolean {
		const f = this.forms[id];
		return !!f && (compact(f.text) !== compact(f.base.text) || compact(f.when) !== compact(f.base.when));
	}

	problems(id: string): MemoryProblems {
		const f = this.forms[id];
		return f ? validateMemoryForm(f, this.limits) : {};
	}

	canSave(id: string): boolean {
		const problems = this.problems(id);
		return this.dirty(id) && !problems.text && !problems.when && !this.saving && this.remote[id] !== 'deleted';
	}

	discard(id: string): void {
		delete this.errors[id];
		delete this.remote[id];
		const m = this.item(id);
		if (m) this.forms[id] = formFromMemory(m);
		else delete this.forms[id];
	}

	/** The owner asked for the saved version: only this replaces a draft. */
	async loadLatest(id: string): Promise<void> {
		if (await this.load()) {
			this.discard(id);
			return;
		}
		this.errors[id] = { message: 'Could not load the latest version. Your edits are still here.', conflict: true };
	}

	async save(id: string): Promise<boolean> {
		const f = this.forms[id];
		if (!f || !this.canSave(id)) return false;
		const sent = { text: f.text, when: f.when };
		delete this.errors[id];
		this.saving = id;
		try {
			const saved = await api.put<MemoryItem>(path(id), {
				text: compact(sent.text),
				when: compact(sent.when) || null,
				expected_revision: f.base.revision,
				expected_metadata_version: f.base.metadataVersion
			});
			this.#requests += 1;
			this.items = this.items.map((m) => (m.id === id ? saved : m));
			this.#keep = new Set([...this.#keep, id]);
			const next = formFromMemory(saved);
			// Typing that happened while the save ran is still the owner's draft.
			const current = this.forms[id];
			if (current && (current.text !== sent.text || current.when !== sent.when)) {
				next.text = current.text;
				next.when = current.when;
			}
			this.forms[id] = next;
			delete this.remote[id];
			return true;
		} catch (e) {
			this.#fail(id, e, 'change', 'Could not save this memory.');
			return false;
		} finally {
			this.saving = null;
		}
	}

	async remove(id: string): Promise<boolean> {
		const f = this.forms[id];
		if (!f) return false;
		delete this.errors[id];
		try {
			await api.delete(
				`${path(id)}?expected_revision=${f.base.revision}&expected_metadata_version=${encodeURIComponent(f.base.metadataVersion)}`
			);
		} catch (e) {
			// Already gone is what the owner asked for.
			if (statusOf(e) !== 404) {
				this.#fail(id, e, 'delete', 'Could not delete this memory.');
				return false;
			}
		}
		this.#requests += 1;
		this.items = this.items.filter((m) => m.id !== id);
		delete this.forms[id];
		delete this.errors[id];
		delete this.remote[id];
		return true;
	}

	#fail(id: string, e: unknown, verb: 'change' | 'delete', fallback: string): void {
		const status = statusOf(e);
		if (status === 404) this.remote[id] = 'deleted';
		this.errors[id] = {
			message:
				status === undefined
					? UNREACHABLE[verb]
					: isForbidden(e)
						? `You need owner access to ${verb} memories.`
						: message(e, fallback),
			conflict: status === 409
		};
	}

	#reconcile(): void {
		for (const id of Object.keys(this.forms)) {
			const m = this.item(id);
			if (!this.dirty(id)) {
				if (m) this.forms[id] = formFromMemory(m);
				else delete this.forms[id];
				delete this.remote[id];
			} else if (!m) {
				this.remote[id] = 'deleted';
			} else {
				const base = this.forms[id].base;
				if (m.revision !== base.revision || m.metadata_version !== base.metadataVersion || m.text !== base.text || (m.when ?? '') !== base.when) this.remote[id] = 'changed';
				else delete this.remote[id];
			}
		}
	}
}

/**
 * Unsaved edits outlive the page: Settings unmounts a collection when another
 * category opens, so a model with drafts is kept for the next visit. A clean
 * model is dropped, so saved text does not linger in memory.
 */
let retained: MemoryModel | null = null;

/** The page's own unload warning ends with it, but a kept draft is still only in memory. */
function warnAboutRetainedDraft(event: BeforeUnloadEvent): void {
	if (!retained?.anyDirty) return;
	event.preventDefault();
	event.returnValue = '';
}

export function acquireMemoryModel(): MemoryModel {
	const model = retained ?? new MemoryModel();
	retained = null;
	window.removeEventListener('beforeunload', warnAboutRetainedDraft);
	return model;
}

export function releaseMemoryModel(model: MemoryModel): void {
	retained = model.anyDirty ? model : null;
	if (retained) window.addEventListener('beforeunload', warnAboutRetainedDraft);
	else window.removeEventListener('beforeunload', warnAboutRetainedDraft);
}
