/**
 * Ticked task-list items in chat messages (#1813), kept as account data so a
 * tick survives a reload and shows on the owner's other devices.
 *
 * The message text is never edited: a tick is an override on top of what the
 * markdown says (`- [ ]` or `- [x]`), keyed by the session and a hash of the
 * message's text, so it needs no message id and stays put when the transcript
 * reloads. The record keeps the most recently ticked messages only.
 */
import { preferenceSync } from '$lib/services/preferences';
import { createPersistedStore, type PersistedSync } from './persisted';

/** Messages whose ticks are remembered; the oldest drop off past this. */
export const MAX_TASK_MESSAGES = 300;

/** message key -> item index -> checked */
export type TaskChecks = Record<string, Record<string, boolean>>;

function normalize(raw: unknown): TaskChecks | undefined {
	if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return undefined;
	const out: TaskChecks = {};
	for (const [key, items] of Object.entries(raw)) {
		if (!items || typeof items !== 'object' || Array.isArray(items)) continue;
		const kept: Record<string, boolean> = {};
		for (const [index, checked] of Object.entries(items)) {
			if (/^\d+$/.test(index) && typeof checked === 'boolean') kept[index] = checked;
		}
		if (Object.keys(kept).length) out[key] = kept;
	}
	return out;
}

/** FNV-1a over the text: short, stable, and the same on every device. */
function hashText(text: string): string {
	let hash = 0x811c9dc5;
	for (let i = 0; i < text.length; i++) {
		hash ^= text.charCodeAt(i);
		hash = Math.imul(hash, 0x01000193);
	}
	return (hash >>> 0).toString(36) + text.length.toString(36);
}

/** The record key for one message's task list. */
export function taskMessageKey(sessionId: string, content: string): string {
	return `${sessionId}:${hashText(content)}`;
}

export function createTaskChecks(sync: PersistedSync, storage?: Storage) {
	const store = createPersistedStore<TaskChecks>({ key: 'chat_task_checks', initial: {}, normalize }, sync, storage);

	/** The stored override for item `index`, or undefined when it was never ticked. */
	function checked(messageKey: string, index: number): boolean | undefined {
		return store.current[messageKey]?.[String(index)];
	}

	function setChecked(messageKey: string, index: number, value: boolean): void {
		store.update((current) => {
			const { [messageKey]: items, ...rest } = current;
			// Re-inserted last, so the newest edits survive the cap.
			const next: TaskChecks = { ...rest, [messageKey]: { ...items, [String(index)]: value } };
			const keys = Object.keys(next);
			for (const old of keys.slice(0, Math.max(0, keys.length - MAX_TASK_MESSAGES))) delete next[old];
			return next;
		});
	}

	return { store, checked, setChecked };
}

let storage: Storage | undefined;
try { storage = globalThis.localStorage; } catch { /* SSR or blocked storage. */ }
export const taskChecks = createTaskChecks(preferenceSync, storage);
