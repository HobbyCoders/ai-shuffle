/**
 * Prompt stash: Ctrl/Cmd+S in the composer sets the current draft aside so the
 * composer is free for something else, and the stash list puts it back later.
 *
 * Entries live in localStorage under the signed-in account, so they survive a
 * reload and are never shown to another account on the same browser. The
 * stored draft is the composer's Tiptap JSON, so file chips come back too.
 */
import { writable } from 'svelte/store';
import { hasComposerDraft } from './composer-suggestion';

export interface StashedPrompt {
	id: string;
	/** Composer draft: Tiptap JSON, or plain text. */
	draft: string;
	/** One-line preview for the list. */
	preview: string;
	savedAt: number;
}

const PREFIX = 'ai-shuffle-prompt-stash:';
export const MAX_STASHED_PROMPTS = 50;

export function stashStorageKey(owner: string): string {
	return `${PREFIX}${owner}`;
}

/** A short single-line preview of a draft, chips shown as @name. */
export function draftPreview(draft: string, max = 120): string {
	let text = draft;
	try {
		const doc = JSON.parse(draft);
		if (doc?.type === 'doc') {
			const parts: string[] = [];
			const walk = (node: { type?: string; text?: string; attrs?: Record<string, unknown>; content?: unknown[] }) => {
				if (node.type === 'text') parts.push(node.text ?? '');
				else if (node.type === 'fileChip') parts.push(`@${String(node.attrs?.filename ?? node.attrs?.path ?? 'file')}`);
				else if (node.type === 'hardBreak' || node.type === 'paragraph') parts.push(' ');
				for (const child of node.content ?? []) walk(child as Parameters<typeof walk>[0]);
			};
			walk(doc);
			text = parts.join('');
		}
	} catch { /* plain text */ }
	const line = text.replace(/\s+/g, ' ').trim();
	return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

function isEntry(value: unknown): value is StashedPrompt {
	const v = value as StashedPrompt;
	return !!v && typeof v.id === 'string' && typeof v.draft === 'string' &&
		typeof v.preview === 'string' && typeof v.savedAt === 'number';
}

function read(owner: string | null): StashedPrompt[] {
	if (!owner) return [];
	try {
		const parsed: unknown = JSON.parse(localStorage.getItem(stashStorageKey(owner)) ?? '[]');
		return Array.isArray(parsed) ? parsed.filter(isEntry) : [];
	} catch {
		return [];
	}
}

function write(owner: string | null, entries: StashedPrompt[]): void {
	if (!owner) return;
	try {
		if (entries.length) localStorage.setItem(stashStorageKey(owner), JSON.stringify(entries));
		else localStorage.removeItem(stashStorageKey(owner));
	} catch { /* quota or disabled storage: the in-memory list still works this page */ }
}

export function createPromptStash(now: () => number = () => Date.now()) {
	let owner: string | null = null;
	const entries = writable<StashedPrompt[]>([]);
	let current: StashedPrompt[] = [];
	const set = (next: StashedPrompt[]) => {
		current = next;
		entries.set(next);
		write(owner, next);
	};
	return {
		subscribe: entries.subscribe,
		/** Switch to the signed-in account's list (null when signed out). */
		setOwner(next: string | null) {
			if (next === owner) return;
			owner = next;
			current = read(owner);
			entries.set(current);
		},
		/** Stash a draft, newest first. Returns the entry, or null for an empty draft. */
		push(draft: string): StashedPrompt | null {
			if (!hasComposerDraft(draft)) return null;
			const savedAt = now();
			const entry: StashedPrompt = {
				id: `${savedAt.toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
				draft,
				preview: draftPreview(draft),
				savedAt
			};
			set([entry, ...current].slice(0, MAX_STASHED_PROMPTS));
			return entry;
		},
		/** Remove and return an entry (restore), or just remove it (delete). */
		take(id: string): StashedPrompt | null {
			const entry = current.find(e => e.id === id) ?? null;
			if (entry) set(current.filter(e => e.id !== id));
			return entry;
		},
		/** Put a taken entry back where it was (Undo). */
		putBack(entry: StashedPrompt) {
			if (current.some(e => e.id === entry.id)) return;
			set([...current, entry].sort((a, b) => b.savedAt - a.savedAt).slice(0, MAX_STASHED_PROMPTS));
		}
	};
}

export const promptStash = createPromptStash();

/** "just now", "5 min ago", "3 h ago", or a date. */
export function stashAge(savedAt: number, now: number = Date.now()): string {
	const minutes = Math.floor((now - savedAt) / 60_000);
	if (minutes < 1) return 'just now';
	if (minutes < 60) return `${minutes} min ago`;
	const hours = Math.floor(minutes / 60);
	if (hours < 24) return `${hours} h ago`;
	return new Date(savedAt).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}
