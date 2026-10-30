/**
 * Questions the user has answered (or skipped) whose answer frame is on its
 * way. The question tray and the phone prompt dock leave them out at once,
 * so they close on the tap instead of on the server's ack, which arrives only
 * after the answer's follow-up work (#891: 865 ms on a slow phone, and the
 * composer jumped with no tap behind it).
 *
 * The request itself stays in `pendingQuestions` until the ack removes it. A
 * retryable delivery failure (`relay_offline`) un-marks it, so the tray comes
 * back with the user's draft. A (re)connect clears every mark: a frame lost
 * with the old socket must not hide a question the server still waits on, and
 * the reconnect's `pending_questions` resync says what is still open.
 */
import { writable } from 'svelte/store';

export const sentQuestions = writable<ReadonlySet<string>>(new Set());

export function markQuestionSent(requestId: string): void {
	sentQuestions.update((ids) => (ids.has(requestId) ? ids : new Set(ids).add(requestId)));
}

export function unmarkQuestionSent(requestId: string): void {
	sentQuestions.update((ids) => {
		if (!ids.has(requestId)) return ids;
		const next = new Set(ids);
		next.delete(requestId);
		return next;
	});
}

export function clearSentQuestions(): void {
	sentQuestions.update((ids) => (ids.size === 0 ? ids : new Set()));
}
