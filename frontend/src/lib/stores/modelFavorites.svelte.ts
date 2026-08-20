/**
 * Favorite ("pinned") models — shared across every model picker surface
 * (profile/agent editor, chat composer dock, mobile controls sheet).
 *
 * Values are the composite selection strings the pickers emit (a native Claude
 * alias such as `sonnet`, or `prov:<providerId>:<modelId>`). Frontend-only and
 * persisted to localStorage under the key the original ModelPicker used, so
 * pins made before this store existed carry over.
 */

const STORAGE_KEY = 'aihub.pinnedModels';

function load(): string[] {
	if (typeof window === 'undefined') return [];
	try {
		const raw = window.localStorage.getItem(STORAGE_KEY);
		const parsed = raw ? JSON.parse(raw) : [];
		return Array.isArray(parsed) ? parsed.filter((v) => typeof v === 'string') : [];
	} catch {
		return [];
	}
}

let favorites = $state<string[]>(load());

function persist() {
	if (typeof window === 'undefined') return;
	try {
		window.localStorage.setItem(STORAGE_KEY, JSON.stringify(favorites));
	} catch {
		// Best-effort — a full/blocked store just means pins don't persist.
	}
}

/** Current favorites in pin order (reactive). */
export function getFavoriteModels(): readonly string[] {
	return favorites;
}

export function isFavoriteModel(value: string): boolean {
	return favorites.includes(value);
}

export function toggleFavoriteModel(value: string): void {
	favorites = favorites.includes(value)
		? favorites.filter((v) => v !== value)
		: [...favorites, value];
	persist();
}

/** Test helper — reset to whatever localStorage currently holds. */
export function reloadFavoriteModels(): void {
	favorites = load();
}
