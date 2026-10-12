/**
 * What the owning worker says about a chat's pending hand-off
 * (`GET /sessions/{id}/handoff` → `pending`), per session.
 *
 * The hand-off row settles a sending move on the source worker from the
 * authoritative pair "the session is owned by the move's target device, and
 * that move is no longer pending" (handoff-episodes.ts `settleByOwner`). This
 * store is where the second half comes from. It is read again whenever the
 * open move or the session's owner changes, so a reload after a finished move
 * reads `pending: null` from the new owner.
 */
import { writable, type Readable } from 'svelte/store';
import { getQueuedMove } from '$lib/api/sessions';
import { beginHandoffPendingRead, noteHandoffTarget, type HandoffPendingView } from '$lib/utils/handoff-episodes';

export { beginHandoffPendingRead };

const store = writable<Record<string, HandoffPendingView>>({});

export const handoffPending: Readable<Record<string, HandoffPendingView>> = { subscribe: store.subscribe };

/** Latest read recorded per session, so an older answer never replaces a newer one. */
const committed = new Map<string, number>();

/**
 * Record a pending state read elsewhere (the move menu reads the same
 * endpoint). `seq` is `beginHandoffPendingRead()` taken before the request
 * was sent: it orders answers, and an answer from before a move was seen does
 * not count for it. Returns false when a newer answer is already recorded.
 */
export function recordHandoffPending(sessionId: string, pending: HandoffPendingView['pending'], seq: number): boolean {
	if ((committed.get(sessionId) ?? 0) >= seq) return false;
	committed.set(sessionId, seq);
	if (pending?.handoff_id) noteHandoffTarget(pending.handoff_id, pending.target_id);
	store.update((all) => ({ ...all, [sessionId]: { known: true, pending: pending ?? null, seq } }));
	return true;
}

const inFlight = new Map<string, Promise<void>>();
const retries = new Map<string, ReturnType<typeof setTimeout>>();

/** A failed read is tried once more after this long (a worker restarting, a dropped request). */
export const HANDOFF_PENDING_RETRY_MS = 15_000;

/**
 * Read the pending hand-off of `sessionId` now. `reason` identifies what
 * changed (open move, phase, owner): the same reason is not read twice at
 * once. A failed read leaves the previous view, so nothing settles on it, and
 * is tried once more after HANDOFF_PENDING_RETRY_MS unless a newer answer
 * arrived meanwhile. A later change of reason or a remount reads again too.
 */
export function refreshHandoffPending(sessionId: string, reason = '', retry = true): Promise<void> {
	const key = `${sessionId}\0${reason}`;
	const running = inFlight.get(key);
	if (running) return running;
	const pendingRetry = retries.get(key);
	if (pendingRetry) {
		clearTimeout(pendingRetry);
		retries.delete(key);
	}
	const seq = beginHandoffPendingRead();
	const read = getQueuedMove(sessionId)
		.then((state) => void recordHandoffPending(sessionId, state?.pending ?? null, seq))
		.catch(() => {
			if (!retry) return;
			retries.set(key, setTimeout(() => {
				retries.delete(key);
				if ((committed.get(sessionId) ?? 0) > seq) return;
				void refreshHandoffPending(sessionId, reason, false);
			}, HANDOFF_PENDING_RETRY_MS));
		})
		.finally(() => inFlight.delete(key));
	inFlight.set(key, read);
	return read;
}

/** Test hook. */
export function resetHandoffPending(): void {
	store.set({});
	inFlight.clear();
	committed.clear();
	for (const timer of retries.values()) clearTimeout(timer);
	retries.clear();
}
