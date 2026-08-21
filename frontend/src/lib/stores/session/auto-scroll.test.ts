/**
 * Tests for the auto-scroll ("stick to bottom") decision logic.
 *
 * The bug: auto-scroll was too aggressive — scrolling up to read earlier
 * messages got interrupted by the stream yanking the user back to the bottom.
 * These tests pin the canonical stick-to-bottom contract:
 *   - at/near bottom            → stick (auto-scroll)
 *   - deliberate scroll-up      → release (suspend auto-scroll)
 *   - new user message          → always scroll, regardless of scroll position
 *   - threshold boundary        → exact edge behaviour
 *   - programmatic / jitter     → not misclassified as a user scroll-up
 */

import { describe, expect, it } from 'vitest';
import {
	CURRENT_TURN_ANCHOR_OFFSET,
	MIN_TRANSCRIPT_TAIL_RESERVE,
	NEAR_BOTTOM_THRESHOLD,
	USER_SCROLL_UP_MIN_DELTA,
	USER_TOUCH_DRAG_MIN,
	USER_WHEEL_UP_MIN_DELTA,
	type ScrollMetrics,
	distanceFromBottom,
	isNearBottom,
	isUserScrollUpGesture,
	nextStickToBottom,
	resolveAnchoredRevealDelta,
	resolveAnchoredTailReserve,
	resolveCurrentTurnAnchorScrollTop,
	shouldAutoScrollOnChange,
	shouldShowJumpToLatest
} from './auto-scroll';

/** Build metrics for a 600px-tall viewport over a 2000px-tall scroll area. */
function metrics(scrollTop: number, scrollHeight = 2000, clientHeight = 600): ScrollMetrics {
	return { scrollTop, scrollHeight, clientHeight };
}

// scrollTop at the very bottom of the default 2000/600 container.
const BOTTOM = 2000 - 600; // 1400

describe('distanceFromBottom', () => {
	it('is 0 at the bottom', () => {
		expect(distanceFromBottom(metrics(BOTTOM))).toBe(0);
	});

	it('grows as the user scrolls up', () => {
		expect(distanceFromBottom(metrics(BOTTOM - 100))).toBe(100);
		expect(distanceFromBottom(metrics(0))).toBe(1400);
	});

	it('clamps elastic over-scroll to 0 (never negative)', () => {
		expect(distanceFromBottom(metrics(BOTTOM + 50))).toBe(0);
	});
});

describe('isNearBottom', () => {
	it('is true exactly at the bottom', () => {
		expect(isNearBottom(metrics(BOTTOM))).toBe(true);
	});

	it('is true within the threshold', () => {
		expect(isNearBottom(metrics(BOTTOM - (NEAR_BOTTOM_THRESHOLD - 1)))).toBe(true);
	});

	it('is true exactly at the threshold boundary (inclusive)', () => {
		expect(isNearBottom(metrics(BOTTOM - NEAR_BOTTOM_THRESHOLD))).toBe(true);
	});

	it('is false just past the threshold', () => {
		expect(isNearBottom(metrics(BOTTOM - (NEAR_BOTTOM_THRESHOLD + 1)))).toBe(false);
	});

	it('is false when scrolled well up', () => {
		expect(isNearBottom(metrics(0))).toBe(false);
		expect(isNearBottom(metrics(BOTTOM - 500))).toBe(false);
	});

	it('honours a custom threshold', () => {
		const m = metrics(BOTTOM - 120);
		expect(isNearBottom(m, 100)).toBe(false);
		expect(isNearBottom(m, 200)).toBe(true);
	});
});

describe('nextStickToBottom', () => {
	it('keeps sticking while the user stays at the bottom', () => {
		const prev = metrics(BOTTOM);
		const next = metrics(BOTTOM);
		expect(nextStickToBottom(true, prev, next)).toBe(true);
	});

	it('RELEASES when the user deliberately scrolls up off the bottom', () => {
		// Scrolled up by 300px, far above the near-bottom threshold.
		const prev = metrics(BOTTOM);
		const next = metrics(BOTTOM - 300);
		expect(nextStickToBottom(true, prev, next)).toBe(false);
	});

	it('RE-ENGAGES when the user scrolls back down to the bottom', () => {
		// Was released (current=false), user scrolls back to the bottom.
		const prev = metrics(BOTTOM - 300);
		const next = metrics(BOTTOM);
		expect(nextStickToBottom(false, prev, next)).toBe(true);
	});

	it('re-engages when scrolling back to within the near-bottom threshold (not just exact bottom)', () => {
		const prev = metrics(BOTTOM - 300);
		const next = metrics(BOTTOM - (NEAR_BOTTOM_THRESHOLD - 1));
		expect(nextStickToBottom(false, prev, next)).toBe(true);
	});

	it('does NOT release on sub-threshold upward jitter (layout-reflow drift)', () => {
		// Up by only a few px, below USER_SCROLL_UP_MIN_DELTA, while not near bottom.
		const prev = metrics(BOTTOM - 500);
		const next = metrics(BOTTOM - 500 - (USER_SCROLL_UP_MIN_DELTA - 1));
		expect(nextStickToBottom(true, prev, next)).toBe(true);
	});

	it('releases at exactly the user-scroll-up min delta (inclusive)', () => {
		const prev = metrics(BOTTOM - 500);
		const next = metrics(BOTTOM - 500 - USER_SCROLL_UP_MIN_DELTA);
		expect(nextStickToBottom(true, prev, next)).toBe(false);
	});

	it('does NOT re-enable just because content grew below the viewport (downward delta, still not near bottom)', () => {
		// User had scrolled up and stayed put; content streaming in below them
		// makes scrollHeight grow, which can register as scrollTop moving "down"
		// relative to bottom. The flag must stay released.
		const prev = metrics(BOTTOM - 800, 2000);
		const next = metrics(BOTTOM - 800, 2600); // taller content, same scrollTop
		expect(nextStickToBottom(false, prev, next)).toBe(false);
	});

	it('treats a programmatic scroll-to-bottom as a return to bottom (re-engages), not a user action', () => {
		// If a programmatic scroll somehow reaches this function, landing at the
		// bottom should still mean "stuck" — it must never be read as a scroll-up.
		const prev = metrics(BOTTOM - 1000);
		const next = metrics(BOTTOM);
		expect(nextStickToBottom(false, prev, next)).toBe(true);
	});

	it('honours custom thresholds', () => {
		const prev = metrics(BOTTOM);
		const next = metrics(BOTTOM - 40);
		// With a generous near-bottom threshold, 40px up is still "near bottom".
		expect(nextStickToBottom(true, prev, next, { nearBottomThreshold: 80 })).toBe(true);
		// With a tight threshold + tiny min-delta, the same move releases.
		expect(
			nextStickToBottom(true, prev, next, { nearBottomThreshold: 10, userScrollUpMinDelta: 10 })
		).toBe(false);
	});
});

describe('isUserScrollUpGesture', () => {
	it('releases on an upward wheel (negative deltaY)', () => {
		expect(isUserScrollUpGesture(-100, 0)).toBe(true);
	});

	it('releases at exactly the min upward wheel delta (inclusive)', () => {
		expect(isUserScrollUpGesture(-USER_WHEEL_UP_MIN_DELTA, 0)).toBe(true);
	});

	it('does NOT release on a downward wheel (scrolling toward the bottom)', () => {
		expect(isUserScrollUpGesture(120, 0)).toBe(false);
	});

	it('does NOT release on a zero / horizontal wheel event', () => {
		expect(isUserScrollUpGesture(0, 0)).toBe(false);
	});

	it('releases on a downward finger drag (touch scroll-up) past the threshold', () => {
		expect(isUserScrollUpGesture(0, USER_TOUCH_DRAG_MIN)).toBe(true);
		expect(isUserScrollUpGesture(0, 200)).toBe(true);
	});

	it('does NOT release on sub-threshold touch jitter from a stationary press', () => {
		expect(isUserScrollUpGesture(0, USER_TOUCH_DRAG_MIN - 1)).toBe(false);
	});

	it('does NOT release on an upward finger drag (touch scroll-down toward bottom)', () => {
		expect(isUserScrollUpGesture(0, -200)).toBe(false);
	});
});

describe('shouldAutoScrollOnChange', () => {
	it('scrolls when stuck to bottom', () => {
		expect(shouldAutoScrollOnChange(true, false)).toBe(true);
	});

	it('does NOT scroll when released and the change is not a user message', () => {
		// This is the core fix: streaming deltas while the user reads up = no scroll.
		expect(shouldAutoScrollOnChange(false, false)).toBe(false);
	});

	it('ALWAYS scrolls on a new user message, even when released', () => {
		expect(shouldAutoScrollOnChange(false, true)).toBe(true);
	});

	it('scrolls on a new user message while already stuck', () => {
		expect(shouldAutoScrollOnChange(true, true)).toBe(true);
	});
});

describe('jump to latest visibility', () => {
	it('hides at the live edge and shows just beyond the threshold', () => {
		expect(shouldShowJumpToLatest(metrics(BOTTOM))).toBe(false);
		expect(shouldShowJumpToLatest(metrics(BOTTOM - NEAR_BOTTOM_THRESHOLD - 1))).toBe(true);
	});

	it('does not flash during a programmatic scroll or controlled turn anchor', () => {
		expect(shouldShowJumpToLatest(metrics(BOTTOM - 500), true)).toBe(false);
		expect(shouldShowJumpToLatest(metrics(BOTTOM - 500), false, true)).toBe(false);
	});
});

describe('current-turn anchor geometry', () => {
	it('positions the sent user row at the stable top offset', () => {
		expect(resolveCurrentTurnAnchorScrollTop(640)).toBe(640 - CURRENT_TURN_ANCHOR_OFFSET);
		expect(resolveCurrentTurnAnchorScrollTop(8)).toBe(0);
	});

	it('reserves the remaining viewport and shrinks as the response grows', () => {
		const initial = resolveAnchoredTailReserve({ clientHeight: 700, contentAfterAnchor: 80 });
		const grown = resolveAnchoredTailReserve({ clientHeight: 700, contentAfterAnchor: 420 });
		expect(initial).toBe(700 - CURRENT_TURN_ANCHOR_OFFSET - 80);
		expect(grown).toBe(700 - CURRENT_TURN_ANCHOR_OFFSET - 420);
		expect(resolveAnchoredTailReserve({ clientHeight: 700, contentAfterAnchor: 900 }))
			.toBe(MIN_TRANSCRIPT_TAIL_RESERVE);
	});

	it('advances only the overflow needed to reveal an outgrown response', () => {
		expect(resolveAnchoredRevealDelta(400, 600, 980)).toBe(4);
		expect(resolveAnchoredRevealDelta(400, 600, 1200)).toBe(224);
		expect(resolveAnchoredRevealDelta(400, 600, 900)).toBe(0);
	});
});
