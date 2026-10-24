/**
 * The owner's watches, shared by every running-tasks panel (#371 step 3).
 *
 * Watches belong to the account, not to one chat, so one list serves the
 * desktop inline panel, the sheet and the phone controls. The server sends no
 * live event for them: while at least one view tracks the list it is fetched
 * on mount, every POLL_MS while the page is visible, when the page becomes
 * visible again, and after each Pause, Resume or Stop.
 */
import { get, writable } from 'svelte/store';
import { listWatches, setWatchStatus, type WatchRow, type WatchStatus } from '$lib/api/watches';

export const WATCH_POLL_MS = 30_000;

export interface WatchesState {
	list: WatchRow[];
	/** A request has finished, whether or not it worked. */
	loaded: boolean;
	/**
	 * A request has succeeded at least once. With `error`, it tells a list that
	 * could not be refreshed (the rows shown are the last good ones, maybe
	 * none) from one that never loaded (nothing is known about the watches).
	 */
	fetched: boolean;
	/** The last request failed. */
	error: boolean;
}

const EMPTY: WatchesState = { list: [], loaded: false, fetched: false, error: false };
const state = writable<WatchesState>({ ...EMPTY });

let inflight: Promise<void> | null = null;
let users = 0;
let timer: ReturnType<typeof setInterval> | null = null;

function refresh(): Promise<void> {
	if (inflight) return inflight;
	inflight = (async () => {
		try {
			const list = await listWatches();
			state.set({ list, loaded: true, fetched: true, error: false });
		} catch {
			state.update((s) => ({ ...s, loaded: true, error: true }));
		} finally {
			inflight = null;
		}
	})();
	return inflight;
}

function visible(): boolean {
	return typeof document === 'undefined' || document.visibilityState !== 'hidden';
}

function onVisibility() {
	if (visible()) void refresh();
}

/** Start keeping the list fresh; the returned function stops this view's share. */
function track(): () => void {
	users += 1;
	if (users === 1) {
		void refresh();
		timer = setInterval(() => {
			if (visible()) void refresh();
		}, WATCH_POLL_MS);
		if (typeof document !== 'undefined') document.addEventListener('visibilitychange', onVisibility);
	}
	let released = false;
	return () => {
		if (released) return;
		released = true;
		users -= 1;
		if (users === 0) {
			if (timer !== null) clearInterval(timer);
			timer = null;
			if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibility);
		}
	};
}

function patchLocal(id: string, status: WatchStatus) {
	state.update((s) => ({
		...s,
		list: s.list.map((w) =>
			w.id === id ? { ...w, status, can_pause: status === 'active', can_stop: status !== 'stopped' } : w
		)
	}));
}

/**
 * Pause, resume or stop one watch. The row changes at once; a failure puts it
 * back and rethrows so the caller can say so. The list is refetched either way
 * because the server owns the render flags.
 */
async function setStatus(id: string, status: WatchStatus): Promise<void> {
	const before = get(state).list.find((w) => w.id === id);
	patchLocal(id, status);
	try {
		await setWatchStatus(id, status);
	} catch (error) {
		if (before) patchLocal(id, before.status);
		throw error;
	} finally {
		// A read already in flight began before this change; refetch after it.
		void (inflight ?? Promise.resolve()).then(refresh);
	}
}

export const watches = { subscribe: state.subscribe, refresh, track, setStatus };

/** Test hook: forget everything between cases. */
export function __resetWatches() {
	if (timer !== null) clearInterval(timer);
	timer = null;
	users = 0;
	inflight = null;
	if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibility);
	state.set({ ...EMPTY });
}
