/**
 * Auto-scroll ("stick to bottom") decision logic for the chat message area.
 *
 * Pure + framework-free so the decisions can be unit-tested in isolation
 * (mirrors gap-indicator.ts). MessageArea.svelte owns the DOM/lifecycle wiring
 * (refs, RAF throttling, ResizeObserver) and defers every *decision* to the
 * helpers here.
 *
 * THE BUG THIS FIXES — "auto-scroll is too aggressive":
 * When the user scrolls up to read earlier messages, the chat kept yanking them
 * back to the bottom on every streaming delta. The canonical "stick to bottom"
 * pattern is: only auto-scroll while the user is at/near the bottom. The instant
 * they scroll up away from the bottom, suspend auto-scroll until they either
 * scroll back to the bottom OR send a new message (their own message always
 * scrolls into view).
 *
 * Two things have to be measured, and both are easy to get subtly wrong:
 *
 *  1. "Is the viewport near the bottom?" — a simple geometry check, but the
 *     threshold has to be small enough that a deliberate scroll-up of a notch or
 *     two actually counts as "not at the bottom" (the old 150px threshold was so
 *     generous a single wheel notch still read as near-bottom, so auto-scroll
 *     never disengaged).
 *
 *  2. "Did the USER scroll up, or did our own programmatic scroll / a layout
 *     reflow move scrollTop?" — programmatic scrolls and content-growth reflows
 *     must NOT be misread as a user scroll-up, or auto-scroll disables itself
 *     mid-stream. The component guards programmatic scrolls with a depth counter;
 *     this module only has to decide, given a real user scroll event, whether the
 *     upward delta is intentional.
 */

/** Geometry of a scroll container at a moment in time. */
export interface ScrollMetrics {
	scrollTop: number;
	scrollHeight: number;
	clientHeight: number;
}

/**
 * Distance in CSS px from the bottom of the scroll container, i.e. how far the
 * user would still have to scroll down to be pinned to the very bottom. 0 means
 * fully scrolled to the bottom; larger means scrolled up. Never negative
 * (over-scroll / elastic bounce is clamped to 0).
 */
export function distanceFromBottom(m: ScrollMetrics): number {
	return Math.max(0, m.scrollHeight - m.scrollTop - m.clientHeight);
}

/**
 * Default "near bottom" threshold in CSS px. Small on purpose: the user is only
 * considered "at the bottom" (and therefore stuck to it) when they are within a
 * couple of lines of the end. A large threshold is what made the old behaviour
 * feel aggressive — a deliberate scroll-up still counted as near-bottom, so the
 * stream kept dragging the user back down.
 */
export const NEAR_BOTTOM_THRESHOLD = 64;

/**
 * Minimum upward movement (px) that counts as a deliberate user scroll-up.
 *
 * Browser scroll-anchoring and input-area reflow can nudge scrollTop upward by a
 * handful of px when late-rendering content (tables, code blocks) or the chat
 * input shifts the container. Requiring a small-but-real upward delta keeps that
 * jitter from being misread as the user intentionally scrolling away. Kept well
 * below a single mouse-wheel notch (~40–120px) so genuine scroll-ups still
 * register immediately.
 */
export const USER_SCROLL_UP_MIN_DELTA = 24;

/**
 * Whether the viewport is at/near the bottom of the container (within
 * `threshold` px). When true the chat should stick to the bottom.
 */
export function isNearBottom(m: ScrollMetrics, threshold: number = NEAR_BOTTOM_THRESHOLD): boolean {
	return distanceFromBottom(m) <= threshold;
}

/**
 * Decide the next `stickToBottom` (auto-scroll) flag in response to a *user*
 * scroll event. The caller must only invoke this for real user scrolls — events
 * fired by our own programmatic scrollToBottom() (or while one is in flight) are
 * filtered out upstream by the component's programmatic-scroll guard.
 *
 * Rules (the canonical stick-to-bottom contract):
 *   - At/near the bottom              → enable  (re-stick when the user returns).
 *   - Deliberate scroll-up off bottom → disable (suspend until they come back).
 *   - Anything else (content grew below the fold, sub-threshold jitter, a
 *     downward scroll that isn't yet near the bottom) → keep the current flag.
 *
 * @param current   The current stickToBottom flag.
 * @param prev      Metrics captured at the previous scroll event (for direction).
 * @param next      Metrics captured at this scroll event.
 * @param opts      Optional threshold overrides (defaults match the constants).
 */
export function nextStickToBottom(
	current: boolean,
	prev: ScrollMetrics,
	next: ScrollMetrics,
	opts: { nearBottomThreshold?: number; userScrollUpMinDelta?: number } = {}
): boolean {
	const nearThreshold = opts.nearBottomThreshold ?? NEAR_BOTTOM_THRESHOLD;
	const upDelta = opts.userScrollUpMinDelta ?? USER_SCROLL_UP_MIN_DELTA;

	// Returning to the bottom always re-engages stick-to-bottom.
	if (isNearBottom(next, nearThreshold)) return true;

	// Not near the bottom: only disable on a deliberate upward movement. This
	// guards against layout-reflow jitter that nudges scrollTop up by a few px.
	const scrolledUpBy = prev.scrollTop - next.scrollTop;
	if (scrolledUpBy >= upDelta) return false;

	// Downward / sideways / sub-threshold movement away from the bottom: don't
	// change the flag. (e.g. content streamed in below the viewport while the
	// user was already reading higher up.)
	return current;
}

/**
 * Whether a content/message change should trigger an auto-scroll to the bottom.
 *
 * Auto-scroll fires when EITHER:
 *   - stick-to-bottom is currently engaged (the user is parked at the bottom), OR
 *   - the change is the user's own newly-sent message — their message always
 *     scrolls into view and re-engages stick-to-bottom, regardless of where they
 *     had scrolled to. (Sending resets the "I scrolled up to read" state.)
 *
 * A pending session-switch scroll is treated like an engaged stick-to-bottom by
 * the caller (it sets `stickToBottom` before invoking), so it is not a separate
 * input here.
 *
 * @param stickToBottom    Current auto-scroll flag.
 * @param isNewUserMessage Whether the change appended a message authored by the user.
 */
export function shouldAutoScrollOnChange(stickToBottom: boolean, isNewUserMessage: boolean): boolean {
	return stickToBottom || isNewUserMessage;
}
