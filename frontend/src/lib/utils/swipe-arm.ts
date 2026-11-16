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

/**
 * A quick swipe commits on its own (#1637). A busy page may hand over a whole
 * flick as one move, so no speed can be read from the moves; the press, the
 * lift and the farthest point always arrive. A swipe commits when the press
 * to lift took at most QUICK_SWIPE_MS by the events' own timestamps, the
 * finger ended at least `minDx` from the press, and it came back no more than
 * QUICK_SWIPE_RETREAT of the way from its farthest point.
 */
export const QUICK_SWIPE_MS = 250;
export const QUICK_SWIPE_RETREAT = 0.25;

export interface QuickSwipe {
	/** Press to lift, in the events' own milliseconds. */
	durationMs: number;
	/** Where the finger ended, from the press. */
	dx: number;
	/** The farthest it got from the press, signed. */
	peakDx: number;
	minDx: number;
}

export function isQuickSwipe({ durationMs, dx, peakDx, minDx }: QuickSwipe): boolean {
	// No duration (an unstamped event) is no reading, not an instant swipe.
	if (!(durationMs > 0) || durationMs > QUICK_SWIPE_MS) return false;
	if (Math.abs(dx) < minDx || Math.sign(dx) !== Math.sign(peakDx)) return false;
	return Math.abs(dx) >= Math.abs(peakDx) * (1 - QUICK_SWIPE_RETREAT);
}

/** A speed read from the moves counts only if the last one came this soon
 *  before the lift: a fast move then a hold is not a flick. */
export const FLICK_STALE_MS = 90;
