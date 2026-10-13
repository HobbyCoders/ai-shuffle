/**
 * Live worker presence for the runner pickers.
 *
 * The server derives each install's `online` flag from its heartbeat
 * (`last_seen` within 90 seconds) and pushes no event when it flips, so
 * presence is pulled. One shared list backs every picker: a menu that opens
 * asks for a refresh, and while anything that shows presence is on screen
 * (an open menu, a chip for a picked or owning worker) the list is refreshed
 * on a short interval. When nothing watches, nothing is requested.
 */

import { get, writable } from 'svelte/store';
import { listInstalls, type Install, type InstallsResponse } from '$lib/api/installs';

/** Refresh period while a watcher is active; well inside the 90 s online window. */
export const PRESENCE_REFRESH_MS = 15_000;

/** `null` until the first successful load. */
const store = writable<Install[] | null>(null);

export const workerInstalls = { subscribe: store.subscribe };

let inflight: Promise<Install[]> | null = null;
let watchers = 0;
let timer: ReturnType<typeof setInterval> | null = null;
/** Bumped by the test reset so stale requests and releases are ignored. */
let generation = 0;

/**
 * Reload the install registry. Concurrent callers share one request. A
 * failure rejects and leaves the last known list in place; callers decide
 * how to surface it.
 */
export function refreshWorkerPresence(): Promise<Install[]> {
	if (inflight) return inflight;
	const gen = generation;
	let pending: Promise<InstallsResponse>;
	try {
		pending = Promise.resolve(listInstalls());
	} catch (e) {
		pending = Promise.reject(e);
	}
	const request = pending.then((res) => {
		if (gen === generation) store.set(res.installs);
		return res.installs;
	});
	inflight = request;
	void request.then(
		() => { if (inflight === request) inflight = null; },
		() => { if (inflight === request) inflight = null; }
	);
	return request;
}

function tick() {
	// A hidden tab cannot show the dots; skip the request until it is visible.
	if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
	refreshWorkerPresence().catch(() => {
		// Keep the last known presence; the next tick tries again.
	});
}

/**
 * Keep presence fresh while the caller shows it. Loads the list at once if
 * it was never loaded. Returns the release function; the interval stops when
 * the last watcher releases.
 */
export function watchWorkerPresence(): () => void {
	watchers += 1;
	if (!timer) timer = setInterval(tick, PRESENCE_REFRESH_MS);
	if (get(store) === null) tick();
	const gen = generation;
	let released = false;
	return () => {
		if (released || gen !== generation) return;
		released = true;
		watchers -= 1;
		if (watchers <= 0 && timer) {
			clearInterval(timer);
			timer = null;
			watchers = 0;
		}
	};
}

/** Whether an install row stands for the given owner id (install, device or legacy instance id). */
function matches(install: Install, id: string): boolean {
	return install.id === id || install.device_id === id || install.legacy_instance_id === id;
}

/**
 * Presence of one worker, looked up by any of its ids, else by its display
 * name. `null` means unknown: not loaded yet, or not in the registry.
 */
export function presenceOf(
	installs: Install[] | null,
	ids: Array<string | null | undefined>,
	name?: string | null
): boolean | null {
	if (!installs) return null;
	for (const id of ids) {
		if (!id) continue;
		const hit = installs.find((i) => matches(i, id));
		if (hit) return hit.online;
	}
	if (name) {
		const named = installs.filter((i) => i.device_name === name);
		// A name shared by two workers cannot say which one owns the chat.
		if (named.length === 1) return named[0].online;
	}
	return null;
}

/** Test hook: forget the cached list and stop any interval. */
export function resetWorkerPresence(): void {
	generation += 1;
	store.set(null);
	inflight = null;
	watchers = 0;
	if (timer) {
		clearInterval(timer);
		timer = null;
	}
}
