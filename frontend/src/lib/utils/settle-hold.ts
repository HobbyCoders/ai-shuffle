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

export interface TurnHandoffInput {
	sessionId: string | null | undefined;
	/** Identity of the latest turn (`latestTurnKey`). */
	latestTurnKey: string | null;
	/** The effective (held) unsettled value for the latest turn. */
	unsettled: boolean;
	/** The turn is parked on background work (reducer `parked`). */
	parked: boolean;
}

export interface TurnHandoffHold {
	/**
	 * Keys of earlier turns still inside their hold. Returns the same set
	 * instance until it changes, so it is cheap to compare.
	 */
	update(input: TurnHandoffInput): ReadonlySet<string>;
	dispose(): void;
}

/**
 * The settle hold above only covers the LATEST turn. When a send (or wake)
 * opens a newer turn while the latest one is still shown live — mid-hold, or
 * parked — that turn stops being the latest and would fold at once. This keeps
 * it flat for the rest of an ordinary hold window instead (the parked window
 * when it was parked), so a send never short-circuits the hold.
 *
 * @param onExpire called when a hand-off runs out, so a reactive caller can
 *   re-evaluate.
 */
export function createTurnHandoffHold(
	onExpire: () => void,
	timers: SettleHoldTimers = {
		setTimeout: (fn, ms) => globalThis.setTimeout(fn, ms),
		clearTimeout: (handle) => globalThis.clearTimeout(handle as ReturnType<typeof setTimeout>)
	}
): TurnHandoffHold {
	let sessionId: string | null | undefined;
	let started = false;
	let previousKey: string | null = null;
	let previousUnsettled = false;
	let previousParked = false;
	const held = new Map<string, unknown>();
	let snapshot: ReadonlySet<string> = new Set();

	function publish() {
		snapshot = new Set(held.keys());
	}

	function clearAll() {
		for (const handle of held.values()) timers.clearTimeout(handle);
		held.clear();
		publish();
	}

	return {
		update({ sessionId: id, latestTurnKey, unsettled, parked }) {
			if (!started || id !== sessionId) {
				started = true;
				sessionId = id;
				clearAll();
			} else if (latestTurnKey !== previousKey && previousKey !== null && previousUnsettled) {
				const key = previousKey;
				if (held.has(key)) timers.clearTimeout(held.get(key));
				held.set(key, timers.setTimeout(() => {
					held.delete(key);
					publish();
					onExpire();
				}, previousParked ? PARKED_SETTLE_HOLD_MS : SETTLE_HOLD_MS));
				publish();
			}
			// The turn that is latest again (a rewind) is governed by the main hold.
			if (latestTurnKey !== null && held.has(latestTurnKey)) {
				timers.clearTimeout(held.get(latestTurnKey));
				held.delete(latestTurnKey);
				publish();
			}
			previousKey = latestTurnKey;
			previousUnsettled = unsettled;
			previousParked = parked;
			return snapshot;
		},
		dispose() {
			clearAll();
		}
	};
}
