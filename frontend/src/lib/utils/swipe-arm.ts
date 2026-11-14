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
