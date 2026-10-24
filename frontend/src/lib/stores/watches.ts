/**
 * The owner's watches, shared by every running-tasks panel (#371 step 3).
 *
 * Watches belong to the account, not to one chat, so one list serves the
 * desktop inline panel, the sheet and the phone controls. The server sends no
 * live event for them: while at least one view tracks the list it is fetched
 * on mount, every POLL_MS while the page is visible, when the page becomes
 * visible again, and after each Pause, Resume, Stop or Move.
 *
 * Moves (#565) are kept here rather than in a panel, so the inline list, the
 * sheet and a remount all show the same pending, uncertain or failed move.
 * A move is never reported as done unless the server answered with the row.
 */
import { get, writable } from 'svelte/store';
import {
	listWatches,
	moveWatch,
	setWatchPreference,
	setWatchStatus,
	type WatchRow,
	type WatchStatus
} from '$lib/api/watches';

export const WATCH_POLL_MS = 30_000;

/**
 * A move this browser asked for. `pending` while the request runs; `uncertain`
 * when no answer says whether it landed (the worker was unreachable or did not
 * answer); `failed` when it was refused, with the reason; `moved` briefly after
 * the server confirmed it.
 */
export interface WatchMove {
	target: string;
	phase: 'pending' | 'uncertain' | 'failed' | 'moved';
	message: string | null;
	/**
	 * This attempt is the automatic re-send of an unconfirmed move, made when
	 * the list showed the watch on its target (see `reconcileMove`). It is made
	 * at most once per attempt the owner started.
	 */
	confirming?: boolean;
}

export interface WatchesState {
	list: WatchRow[];
	/** Where new watches run; `null` is automatic. */
	preferredWorkerId: string | null;
	/** Moves by watch id. */
	moves: Record<string, WatchMove>;
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

const EMPTY: WatchesState = { list: [], preferredWorkerId: null, moves: {}, loaded: false, fetched: false, error: false };
/** How long a confirmed move says "Moved to …" before the row speaks for itself. */
export const MOVED_NOTE_MS = 6_000;
const state = writable<WatchesState>({ ...EMPTY });

let inflight: Promise<void> | null = null;
let users = 0;
let timer: ReturnType<typeof setInterval> | null = null;

function refresh(): Promise<void> {
	if (inflight) return inflight;
	inflight = (async () => {
		try {
			const listing = await listWatches();
			state.update((s) => ({ ...s, list: listing.watches, preferredWorkerId: listing.preferredWorkerId, loaded: true, fetched: true, error: false }));
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

function setMove(id: string, move: WatchMove | null) {
	state.update((s) => {
		const moves = { ...s.moves };
		if (move) moves[id] = move;
		else delete moves[id];
		return { ...s, moves };
	});
}

const movedTimers = new Map<string, ReturnType<typeof setTimeout>>();

/** What a failed move means: refused (409, a reason) or unknown (anything else). */
export function moveFailure(error: unknown): Pick<WatchMove, 'phase' | 'message'> {
	const e = error as { status?: unknown; detail?: unknown } | null;
	const detail = typeof e?.detail === 'string' && e.detail.trim() ? e.detail.trim() : null;
	if (e && e.status === 409) return { phase: 'failed', message: detail ?? 'The worker refused the move.' };
	// 503 (unreachable or no answer), a dropped connection or a server error:
	// the move may have started, so it must not read as done or as refused.
	return { phase: 'uncertain', message: null };
}

/**
 * Move one watch to `target`. Resolves with the outcome; never throws. The
 * list is refetched either way: a refused or unanswered move may still have
 * left the watch fenced on its old worker, which only the server can say.
 */
async function move(id: string, target: string, confirming = false): Promise<WatchMove['phase']> {
	const current = get(state).moves[id];
	if (current?.phase === 'pending') return 'pending';
	clearTimeout(movedTimers.get(id));
	movedTimers.delete(id);
	setMove(id, { target, phase: 'pending', message: null, confirming });
	let phase: WatchMove['phase'];
	try {
		await moveWatch(id, target);
		phase = 'moved';
		markMoved(id, target);
	} catch (error) {
		const failure = moveFailure(error);
		phase = failure.phase;
		setMove(id, { target, ...failure, confirming });
	}
	await (inflight ?? Promise.resolve()).then(refresh);
	return phase;
}

function markMoved(id: string, target: string) {
	clearTimeout(movedTimers.get(id));
	setMove(id, { target, phase: 'moved', message: null });
	movedTimers.set(
		id,
		setTimeout(() => {
			movedTimers.delete(id);
			if (get(state).moves[id]?.phase === 'moved') setMove(id, null);
		}, MOVED_NOTE_MS)
	);
}

/**
 * The list shows the watch on the target of a move whose answer was lost or
 * refused. The import may have committed while the old worker's transfer
 * receipt is still open; a later move back would then be refused. Seeing the
 * row there is not enough: send the same move once more. The server
 * reconciles the old worker's receipt on that idempotent retry and answers
 * with the row, and only that answer reads "Moved". If it fails again the
 * note stays (with Retry), and no further automatic attempt is made.
 */
function reconcileMove(id: string): Promise<WatchMove['phase']> | null {
	const current = get(state).moves[id];
	if (!current || current.confirming || (current.phase !== 'uncertain' && current.phase !== 'failed')) return null;
	return move(id, current.target, true);
}

/** Forget a failed or uncertain move note (the row's own state remains). */
function dismissMove(id: string) {
	if (get(state).moves[id]?.phase !== 'pending') setMove(id, null);
}

/** Where new watches run. Rejects with the server's reason when refused. */
async function setPreference(workerId: string | null): Promise<void> {
	const preferredWorkerId = await setWatchPreference(workerId);
	state.update((s) => ({ ...s, preferredWorkerId }));
}

export const watches = {
	subscribe: state.subscribe,
	refresh,
	track,
	setStatus,
	move,
	reconcileMove,
	dismissMove,
	setPreference
};

/** Test hook: forget everything between cases. */
export function __resetWatches() {
	if (timer !== null) clearInterval(timer);
	timer = null;
	users = 0;
	inflight = null;
	for (const t of movedTimers.values()) clearTimeout(t);
	movedTimers.clear();
	if (typeof document !== 'undefined') document.removeEventListener('visibilitychange', onVisibility);
	state.set({ ...EMPTY, moves: {} });
}
