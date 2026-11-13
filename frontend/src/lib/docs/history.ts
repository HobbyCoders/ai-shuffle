/**
 * Docs browser history (#1477). Every entry Docs stands on says what it showed:
 * `docsDoc` is a document (`category/slug`), or '' for the list. `docsStep`
 * marks an entry Docs pushed, so the entry below it is a Docs entry too.
 * Like a chat link's entry, a Back or Forward step onto a Docs entry from
 * another destination reopens Docs there, so no step is ever a dead press.
 */
import { replaceState } from '$app/navigation';
import { layout } from '$lib/stores/layout.svelte';

export type DocsEntry = { docsDoc?: string; docsStep?: boolean };

/** The tab's record of the entry Docs last stood on: `{ id, doc, step }`. */
export const ENTRY_KEY = 'docs.entry';

/** SvelteKit's own number for a history entry. Unlike its state, it survives a reload (as in Settings). */
export function currentEntryId(): number | null {
	if (typeof history === 'undefined') return null;
	const id = (history.state as Record<string, unknown> | null)?.['sveltekit:history'];
	return typeof id === 'number' ? id : null;
}

/**
 * What the current entry said before a reload emptied its state, when Docs
 * stood on it: SvelteKit keeps the entry's number but not its state.
 */
export function keptEntry(): Required<DocsEntry> | null {
	try {
		const kept = JSON.parse(sessionStorage.getItem(ENTRY_KEY) || 'null');
		const id = currentEntryId();
		return id !== null && kept?.id === id && typeof kept.doc === 'string' ? { docsDoc: kept.doc, docsStep: !!kept.step } : null;
	} catch {
		return null;
	}
}

let followed: string | undefined;

/** A reloaded Docs entry says what it said again, even while another destination is shown. */
function restoreReloaded(state: unknown): string | undefined {
	if (state && Object.keys(state).length) return undefined;
	const kept = keptEntry();
	if (!kept) return undefined;
	try {
		replaceState('', { ...(state as object), ...kept } as App.PageState);
	} catch {
		return undefined; // Router not ready: Docs restores the entry when it opens.
	}
	return kept.docsDoc;
}

/**
 * Called by AppPage whenever `page.state` changes (`first` for the page's
 * initial state): a step onto a Docs entry while another destination is shown
 * opens Docs, which then shows what the entry says. Most features copy
 * `page.state` into their entries, so only a change of `docsDoc` is a step;
 * like a chat link, an entry that names no document (Settings' own steps) is
 * not, so Back from it to the entry below stays in that destination.
 */
export function followDocsHistory(state: unknown, first = false): void {
	const doc = (state as DocsEntry | undefined)?.docsDoc;
	if (first) {
		followed = doc ?? restoreReloaded(state);
		return;
	}
	if (doc === undefined || doc === followed) return;
	followed = doc;
	if (layout.current.activeView?.type === 'docs') return;
	layout.openFeaturePage('docs');
}
