/**
 * Persisted stores — any UI choice becomes account data, with localStorage as a cache.
 *
 * One pattern for every store built here:
 * - the cache is read synchronously at creation, so the first paint shows the user's choice;
 * - an adapter is registered with the sync engine, so authoritative server values apply
 *   without echoing a save, and an account reset restores `initial`;
 * - every edit writes through: memory, then cache, then `sync.save`. Nothing is debounced —
 *   PreferenceSync already coalesces edits per key.
 *
 * Server keys are used verbatim, unless `ui: true` prefixes them with `ui:` — the namespace
 * for plain UI state (which tab is open, which tool was last picked). Existing unprefixed keys
 * keep their data.
 *
 * Every storage access is wrapped: private mode, a full quota or SSR only cost the cache.
 * Plain .ts on purpose (no runes), so it is safe to import from anywhere — see store-utils.ts.
 */
import { preferenceSync } from '$lib/services/preferences';
import type { PreferenceSync } from '$lib/services/preference-sync';
import { compatible } from '$lib/services/preference-values';
import { createStore } from './store-utils';

const UI_KEY_PREFIX = 'ui:';
const STORAGE_KEY_PREFIX = 'ai-shuffle:';

export type PersistedSync = Pick<PreferenceSync, 'register' | 'save'>;

/** A cache that predates the one-JSON-key convention (several keys, '0'/'1' flags). */
export interface CacheLayout<T> {
	/** Return undefined when nothing is cached. */
	read(storage: Storage): unknown;
	/** Replace everything the layout held: a reset must not leave another account's cache behind. */
	write(storage: Storage, value: T): void;
}

export interface PersistedOptions<T> {
	/** Server preference key, used verbatim unless `ui` is set. */
	key: string;
	/** Prefix the server key with `ui:` — `{ key: 'runner_pick', ui: true }` syncs as `ui:runner_pick`. */
	ui?: boolean;
	/** Value before any cache or server value is known, and what a reset restores. */
	initial: T;
	/**
	 * Validate or migrate a parsed value from the cache, a legacy key or the server; return
	 * undefined to fall back to `initial`. Without it a value is accepted when it has the same
	 * type as `initial`, and a plain object is deep-merged over `initial` (see `compatible`).
	 */
	normalize?: (raw: unknown) => T | undefined;
	/** Older localStorage keys holding this value (JSON or a raw string): imported once, first found wins, then removed. */
	legacyKeys?: string[];
	/** localStorage key; defaults to `ai-shuffle:<server key>`. */
	storageKey?: string;
	/** Custom cache layout; replaces `storageKey`. */
	cache?: CacheLayout<T>;
	/** Device-local: cached, never registered with the sync engine (device id, platform override, debug flags). */
	local?: boolean;
}

export interface PersistedStore<T> {
	/** Resolved server key — what `preferenceSync.refresh()` and the inventory see. */
	readonly key: string;
	readonly current: T;
	subscribe(fn: (value: T) => void): () => void;
	set(value: T): void;
	update(fn: (value: T) => T): void;
	/** Back to `initial`, as an edit: the account copy resets too. */
	reset(): void;
}

const isPlainObject = (value: unknown): value is Record<string, unknown> =>
	!!value && typeof value === 'object' && !Array.isArray(value);
/** Values are JSON by construction (they travel to the server); a fresh copy keeps `initial` pristine. */
const clone = <T>(value: T): T => value && typeof value === 'object' ? JSON.parse(JSON.stringify(value)) : value;
/** Caches written before the JSON convention hold raw strings. */
function parseStored(stored: string): unknown {
	try { return JSON.parse(stored); } catch { return stored; }
}

/** Keys are verbatim unless `ui`, which namespaces them under `ui:` (never twice). */
export function resolvePreferenceKey(key: string, ui = false): string {
	return ui && !key.startsWith(UI_KEY_PREFIX) ? UI_KEY_PREFIX + key : key;
}

export function createPersistedStore<T>(options: PersistedOptions<T>, sync: PersistedSync, storage?: Storage): PersistedStore<T> {
	const { initial, normalize, legacyKeys = [], local = false } = options;
	const key = resolvePreferenceKey(options.key, options.ui);
	const storageKey = options.storageKey ?? STORAGE_KEY_PREFIX + key;
	const cache: CacheLayout<T> = options.cache ?? {
		read: s => { const stored = s.getItem(storageKey); return stored === null ? undefined : parseStored(stored); },
		write: (s, value) => {
			const serialized = JSON.stringify(value);
			if (serialized === undefined) s.removeItem(storageKey); else s.setItem(storageKey, serialized);
		}
	};

	function coerce(raw: unknown): T {
		if (raw === undefined) return clone(initial);
		if (normalize) { const value = normalize(raw); return value === undefined ? clone(initial) : value; }
		if (isPlainObject(initial)) return compatible(initial, raw);
		if (Array.isArray(initial)) return (Array.isArray(raw) ? raw : clone(initial)) as T;
		return (typeof raw === typeof initial && (raw === null) === (initial === null) ? raw : initial) as T;
	}
	function write(value: T) {
		if (!storage) return;
		try { cache.write(storage, value); } catch { /* A blocked cache does not prevent account sync. */ }
	}
	function load(): T {
		let cached: unknown, imported: unknown;
		if (storage) {
			try {
				cached = cache.read(storage);
				for (const legacy of legacyKeys) {
					const stored = storage.getItem(legacy);
					if (stored === null) continue;
					if (cached === undefined && imported === undefined) imported = parseStored(stored);
					storage.removeItem(legacy);
				}
			} catch { /* Private mode or SSR: start from the defaults; the account copy applies on connect. */ }
		}
		if (cached !== undefined) return coerce(cached);
		const value = coerce(imported);
		if (imported !== undefined) write(value);
		return value;
	}

	const state = createStore<T>(load());
	function set(value: T) {
		state.set(value);
		write(value);
		if (!local) sync.save(key, value);
	}
	if (!local) sync.register(key, {
		read: () => state.current,
		// Applied without echo: the server (or an account reset) is the source of truth.
		apply(raw) { state.set(coerce(raw)); write(state.current); }
	});
	return {
		key,
		subscribe: state.subscribe,
		get current() { return state.current; },
		set,
		update: fn => set(fn(state.current)),
		reset: () => set(clone(initial))
	};
}

function browserStorage(): Storage | undefined {
	try { return globalThis.localStorage; } catch { return undefined; }
}

/** `createPersistedStore` wired to the account sync engine and this browser's localStorage. */
export function persisted<T>(options: PersistedOptions<T>): PersistedStore<T> {
	return createPersistedStore(options, preferenceSync, browserStorage());
}
