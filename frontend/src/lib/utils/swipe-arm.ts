/**
 * How far a finger drags a list row sideways before letting go commits.
 *
 * A share of the row's width suits a phone, but a row as wide as a foldable's
 * inner screen or a spanned dual-screen device would arm 300px or more in,
 * across most of the screen. The cap keeps the travel what a phone row asks
 * for: 40% of a 400px row, so phones up to about 428px wide (the phone
 * app's chat rows are the screen less 28px) keep the arm point they had.
 */
export const SWIPE_ARM_MAX_PX = 160;

/** The arm distance for a row `rowWidth` wide that arms at `ratio` of it. */
export function swipeArmDistance(rowWidth: number, ratio: number): number {
	return Math.min(rowWidth * ratio, SWIPE_ARM_MAX_PX);
}

/** A finger position and when it was there, on the performance.now() clock. */
export interface SwipeSample {
	t: number;
	x: number;
}

/** How far back the release speed looks: about three frames, so a burst of
 *  bunched moves averages out but the slow start of a flick does not count. */
export const FLICK_WINDOW_MS = 50;

/**
 * When a pointer event happened, not when it was handled: a busy page handles
 * moves late and in bunches, so handler time makes one sample's speed swing
 * from a fraction of the finger's to several times it (#1637). The event's
 * stamp is on the performance.now() clock; a missing or impossible one
 * counts as now.
 */
export function sampleTime(event: Event): number {
	const now = performance.now();
	return event.timeStamp > 0 && event.timeStamp <= now ? event.timeStamp : now;
}

/**
 * The finger's speed (px/ms, signed) over the last FLICK_WINDOW_MS of
 * `samples` (oldest first), not over the last move alone. With one sample in
 * the window it reaches back to the one before, so a slow drag reads slow.
 */
export function flickVelocity(samples: readonly SwipeSample[], windowMs = FLICK_WINDOW_MS): number {
	if (samples.length < 2) return 0;
	const last = samples[samples.length - 1];
	let first = samples.length - 2;
	while (first > 0 && last.t - samples[first - 1].t <= windowMs) first--;
	const from = samples[first];
	const elapsed = last.t - from.t;
	return elapsed > 0 ? (last.x - from.x) / elapsed : 0;
}
