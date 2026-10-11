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
import { noteHandoffTarget, type HandoffPendingView } from '$lib/utils/handoff-episodes';

const store = writable<Record<string, HandoffPendingView>>({});

export const handoffPending: Readable<Record<string, HandoffPendingView>> = { subscribe: store.subscribe };

/** Record a pending state read elsewhere (the move menu reads the same endpoint). */
export function recordHandoffPending(sessionId: string, pending: HandoffPendingView['pending']): void {
	if (pending?.handoff_id) noteHandoffTarget(pending.handoff_id, pending.target_id);
	store.update((all) => ({ ...all, [sessionId]: { known: true, pending: pending ?? null } }));
}

const inFlight = new Map<string, Promise<void>>();

/**
 * Read the pending hand-off of `sessionId` now. `reason` identifies what
 * changed (open move, phase, owner): the same reason is not read twice at
 * once. A failed read leaves the previous view, so nothing settles on it.
 */
export function refreshHandoffPending(sessionId: string, reason = ''): Promise<void> {
	const key = `${sessionId}\0${reason}`;
	const running = inFlight.get(key);
	if (running) return running;
	const read = getQueuedMove(sessionId)
		.then((state) => recordHandoffPending(sessionId, state?.pending ?? null))
		.catch(() => {})
		.finally(() => inFlight.delete(key));
	inFlight.set(key, read);
	return read;
}

/** Test hook. */
export function resetHandoffPending(): void {
	store.set({});
	inFlight.clear();
}
