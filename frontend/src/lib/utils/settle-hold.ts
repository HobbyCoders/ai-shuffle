/**
 * Hold the latest turn "unsettled" briefly after its live signals drop.
 *
 * The transcript folds the latest turn into its Worked group the moment the
 * turn looks settled, and unfolds it the moment it looks live again. Several
 * ordinary sequences drop the live signals for a moment without the turn
 * really finishing:
 *
 *  - the backend emits `turn.completed` before the park gate's `turn.parked`,
 *    and the completed event speculatively settles running background work;
 *  - a parked turn's last background task finishes, and the wake turn that
 *    reports it starts a moment later, appending to the same turn.
 *
 * Each of those folded the whole turn and then popped it back open. The hold
 * keeps the turn as it is for a short grace window; if the turn goes live
 * again inside it, nothing on screen moves. If it does not, the turn folds
 * once, at the end of the window.
 */

/** Grace after a turn ends, covering `turn.completed` → `turn.parked`. */
export const SETTLE_HOLD_MS = 1_200;
/** Grace for a parked turn whose background work just finished: the wake. */
export const PARKED_SETTLE_HOLD_MS = 10_000;

export interface SettleHoldInput {
	sessionId: string | null | undefined;
	/** The raw lifecycle predicate: streaming, live turn, running task, pending decision. */
	unsettled: boolean;
	/** The turn is parked on background work (reducer `parked`). */
	parked: boolean;
}

export interface SettleHoldTimers {
	setTimeout: (fn: () => void, ms: number) => unknown;
	clearTimeout: (handle: unknown) => void;
}

export interface SettleHold {
	/**
	 * The effective value. Idempotent for the same input, so it is safe to call
	 * from a derived that re-evaluates.
	 */
	update(input: SettleHoldInput): boolean;
	dispose(): void;
}

/**
 * @param onExpire called when a hold runs out, so a reactive caller can
 *   re-evaluate (the hold has no reactive state of its own).
 */
export function createSettleHold(
	onExpire: () => void,
	timers: SettleHoldTimers = {
		setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
		clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>)
	}
): SettleHold {
	let sessionId: string | null | undefined;
	let started = false;
	let effective = false;
	let timer: unknown = null;
	let timerParked = false;

	function clear() {
		if (timer !== null) timers.clearTimeout(timer);
		timer = null;
	}

	function arm(parked: boolean) {
		clear();
		timerParked = parked;
		timer = timers.setTimeout(() => {
			timer = null;
			effective = false;
			onExpire();
		}, parked ? PARKED_SETTLE_HOLD_MS : SETTLE_HOLD_MS);
	}

	return {
		update({ sessionId: id, unsettled, parked }) {
			// A different session (or the first read) shows its real state at once:
			// there is nothing on screen to keep steady yet.
			if (!started || id !== sessionId) {
				started = true;
				sessionId = id;
				clear();
				effective = unsettled;
				return effective;
			}
			if (unsettled) {
				clear();
				effective = true;
				return true;
			}
			if (!effective) return false;
			// Live signals dropped while the turn was shown live: hold it. A parked
			// hold that is no longer parked (the turn ended or was stopped) shortens
			// to the ordinary grace; a park landing during the short grace extends it.
			if (timer === null || timerParked !== parked) arm(parked);
			return true;
		},
		dispose() {
			clear();
		}
	};
}
