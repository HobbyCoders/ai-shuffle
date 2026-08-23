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
import { getPreference, setPreference } from '$lib/api/client';

const STORAGE_KEY = 'aihub.pinnedModels';
const SERVER_PREFERENCE_KEY = 'model_favorites';
const SERVER_SYNC_DEBOUNCE_MS = 500;

let serverSyncTimer: ReturnType<typeof setTimeout> | null = null;
let hasSyncedFromServer = false;
let pendingState: string[] | null = null;

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
	if (!browser || !hasSyncedFromServer) return;
	pendingState = value;
	if (serverSyncTimer) clearTimeout(serverSyncTimer);
	serverSyncTimer = setTimeout(async () => {
		try {
			await setPreference(SERVER_PREFERENCE_KEY, pendingState);
			pendingState = null;
		} catch {
			// Keep pendingState so the unload flush can retry via sendBeacon.
		}
	}, SERVER_SYNC_DEBOUNCE_MS);
}

function flushPendingSave(): void {
	if (!browser || !pendingState || !hasSyncedFromServer) return;
	if (serverSyncTimer) {
		clearTimeout(serverSyncTimer);
		serverSyncTimer = null;
	}
	const data = JSON.stringify({ key: SERVER_PREFERENCE_KEY, value: pendingState });
	navigator.sendBeacon(
		'/api/v1/preferences/' + SERVER_PREFERENCE_KEY,
		new Blob([data], { type: 'application/json' })
	);
	pendingState = null;
}

// Guard against HMR duplicate registration
if (browser && !(window as any).__favModels_listeners) {
	(window as any).__favModels_listeners = true;
	window.addEventListener('beforeunload', flushPendingSave);
	document.addEventListener('visibilitychange', () => {
		if (document.visibilityState === 'hidden') flushPendingSave();
	});
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
	if (!browser || hasSyncedFromServer) return;
	let serverValue: unknown = null;
	try {
		const response = await getPreference<unknown>(SERVER_PREFERENCE_KEY);
		serverValue = response?.value ?? null;
	} catch {
		return; // Offline / auth not ready yet — layout retries on next launch.
	}
	hasSyncedFromServer = true;
	if (Array.isArray(serverValue)) {
		const serverList = serverValue.filter((v): v is string => typeof v === 'string');
		favorites = serverList;
		persist();
	} else {
		const local = load();
		if (local.length > 0) saveToServer(local);
	}
}
