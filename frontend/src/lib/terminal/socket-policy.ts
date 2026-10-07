/**
 * What a terminal tab does when its socket closes (contract:
 * docs/internal/terminals.md, "Close codes"). Pure, so every code is tested.
 */

export type CloseAction =
	/** The terminal is gone (closed, not found): remove the tab. */
	| { kind: 'drop' }
	/** Signed out or not allowed: stop, show the reason, no retry. */
	| { kind: 'auth' }
	/** The worker is stopping: its terminals are gone. */
	| { kind: 'gone' }
	/** A client bug: show it, never retry in a loop. */
	| { kind: 'fatal'; message: string }
	/** Try again after `delay` ms. */
	| { kind: 'retry'; delay: number };

const MAX_DELAY = 15_000;
/** Give up after this many failed reconnects in a row and check the list. */
export const MAX_RECONNECTS = 8;
/** First wait after "too far behind" (4408). */
export const MIN_BEHIND_DELAY = 1_000;
/** A socket must stay live this long before the retry count starts over. */
export const STABLE_AFTER = 5_000;

/** 0.5 s, 1 s, 2 s, ... capped at 15 s. */
export function backoff(attempt: number, floor = 500): number {
	return Math.min(MAX_DELAY, Math.max(floor, 500 * 2 ** Math.max(0, attempt)));
}

export function closeAction(code: number, attempt: number): CloseAction {
	switch (code) {
		case 4004:
		case 4410:
			return { kind: 'drop' };
		case 4001:
			return { kind: 'auth' };
		case 1001:
			return { kind: 'gone' };
		case 4400:
			return { kind: 'fatal', message: 'The terminal connection hit a protocol error. Reload the page.' };
		case 4408:
			// Too far behind: the replay covers the gap. Wait at least a second
			// so a viewer that keeps falling behind backs off instead of
			// downloading the scrollback again in a tight loop.
			return { kind: 'retry', delay: backoff(attempt, MIN_BEHIND_DELAY) };
		case 4429:
			// Input too fast: come back, but not at once.
			return { kind: 'retry', delay: backoff(attempt, 2_000) };
		default:
			// 1006 (dropped), 1011 (send failed), 1013 (auth check unavailable)
			// and anything unknown: back off and retry.
			return { kind: 'retry', delay: backoff(attempt) };
	}
}
