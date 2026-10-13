import { preferenceSync } from '$lib/services/preferences';
/**
 * Favorite ("pinned") models — shared across every model picker surface
 * (profile/agent editor, chat composer dock, mobile controls sheet) and
 * synced across devices via the per-user preferences API.
 *
 * Values are the composite selection strings the pickers emit (a native Claude
 * alias such as `sonnet`, or `prov:<providerId>:<modelId>`).
 *
 * Persistence follows the sibling preference stores: localStorage is the
 * instant-load cache (and offline fallback), the server preference is the
 * source of truth. On sync, server wins when it has data; otherwise a
 * device's local pins are pushed up so the first device that set favorites
 * seeds every other one.
 */

import { browser } from '$app/environment';

const STORAGE_KEY = 'aihub.pinnedModels';
const SERVER_PREFERENCE_KEY = 'model_favorites';


function load(): string[] {
	if (!browser) return [];
	try {
		const raw = localStorage.getItem(STORAGE_KEY);
		const parsed = raw ? JSON.parse(raw) : [];
		return Array.isArray(parsed) ? parsed.filter((v) => typeof v === 'string') : [];
	} catch {
		return [];
	}
}

let favorites = $state<string[]>(load());

function persist() {
	if (!browser) return;
	try {
		localStorage.setItem(STORAGE_KEY, JSON.stringify(favorites));
	} catch {
		// Best-effort — a full/blocked store just means pins don't persist.
	}
}

function saveToServer(value: string[]): void {
 preferenceSync.save(SERVER_PREFERENCE_KEY, value);
}

// Guard against HMR duplicate registration
// Persistence lifecycle is owned by live-sync, not module-level unload listeners.

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
	saveToServer(favorites);
}

/** Test helper — reset to whatever localStorage currently holds. */
export function reloadFavoriteModels(): void {
	favorites = load();
}

/**
 * Pull the server list into local state. Server wins when it has a list; a
 * fresh account with none pushes the device's existing pins up instead, so
 * pre-sync-era pins aren't lost. Safe to call repeatedly (idempotent after
 * the first successful sync).
 */
export async function syncFavoriteModels(): Promise<void> {
 await preferenceSync.refresh(SERVER_PREFERENCE_KEY);
}
preferenceSync.register(SERVER_PREFERENCE_KEY, {
 read: () => favorites,
 apply(value) { favorites = Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []; persist(); }
});
