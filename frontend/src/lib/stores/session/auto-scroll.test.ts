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
	createQueuedPromptTracker,
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
	readingAnchorOf,
	resolveAnchoredRevealDelta,
	resolveAnchoredTailReserve,
	resolveCurrentTurnAnchorScrollTop,
	resolveReadingAnchorDelta,
	shouldAutoScrollOnChange,
	shouldFollowLatestTurnOnSettle,
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
	it('keeps following when a content collapse clamps the viewport to the new bottom', () => {
		expect(nextStickToBottom(true, metrics(BOTTOM), metrics(BOTTOM - 300, 1700))).toBe(true);
	});

	it('keeps following when the composer shrinks and the viewport grows', () => {
		expect(nextStickToBottom(true, metrics(BOTTOM), metrics(BOTTOM - 100, 2000, 700))).toBe(true);
	});

	it('does not treat upward scroll anchoring during collapse as reader intent', () => {
		expect(nextStickToBottom(true, metrics(BOTTOM - 100), metrics(BOTTOM - 400, 1700))).toBe(true);
	});

	it('does not resume a paused reader when collapse clamps to the bottom', () => {
		expect(nextStickToBottom(false, metrics(BOTTOM - 100), metrics(BOTTOM - 300, 1700))).toBe(false);
	});

	it('resumes on a downward return to the new bottom when the old gap was zero', () => {
		// Upward input released following, but its scroll event was swallowed by
		// the programmatic guard. Growth then left the cached metrics at the old bottom.
		expect(nextStickToBottom(false, metrics(BOTTOM), metrics(BOTTOM + 300, 2300))).toBe(true);
	});

	it('does not release when reflow moves upward while increasing the bottom gap', () => {
		expect(nextStickToBottom(true, metrics(BOTTOM), metrics(BOTTOM - 25, 3000, 812))).toBe(true);
	});

	it('preserves a wheel/touch release during the same reflow', () => {
		expect(nextStickToBottom(false, metrics(BOTTOM), metrics(BOTTOM - 25, 3000, 812))).toBe(false);
	});

	it('releases on a single upward notch inside the near-bottom band', () => {
		expect(nextStickToBottom(true, metrics(BOTTOM), metrics(BOTTOM - 40))).toBe(false);
	});

	it('does not undo wheel/touch release during tiny upward or stationary events', () => {
		let prev = metrics(BOTTOM);
		for (const distance of [0, 2, 4, 8, 16, 32, 64, 100]) {
			const next = metrics(BOTTOM - distance);
			expect(nextStickToBottom(false, prev, next)).toBe(false);
			prev = next;
		}
	});

	it('does not resume when content collapse brings a stationary reader near bottom', () => {
		expect(nextStickToBottom(false, metrics(BOTTOM - 300), metrics(BOTTOM - 300, 1700))).toBe(false);
	});

	it('does not resume when scroll anchoring moves down without approaching the bottom', () => {
		expect(nextStickToBottom(false, metrics(BOTTOM - 40), metrics(BOTTOM - 20, 2020))).toBe(false);
	});

	it('resumes on a downward return after content grew while the reader was paused', () => {
		// No scroll event accompanies growth, so prev still has the old height.
		expect(nextStickToBottom(false, metrics(BOTTOM - 20), metrics(BOTTOM + 35, 2035))).toBe(true);
	});

	it('resumes after the reader reverses direction and returns toward the bottom', () => {
		const start = metrics(BOTTOM);
		const reading = metrics(BOTTOM - 40);
		const released = nextStickToBottom(true, start, reading);
		expect(released).toBe(false);
		expect(nextStickToBottom(released, reading, metrics(BOTTOM - 20))).toBe(true);
	});

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
		// Upward intent takes priority even with a generous near-bottom threshold.
		expect(nextStickToBottom(true, prev, next, { nearBottomThreshold: 80 })).toBe(false);
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

	it('continues live-edge following on settle, without navigating to the prompt', () => {
		expect(shouldFollowLatestTurnOnSettle(true, false, true)).toBe(true);
		expect(shouldFollowLatestTurnOnSettle(true, false, false)).toBe(false);
		expect(shouldFollowLatestTurnOnSettle(true, true, true)).toBe(false);
		expect(shouldFollowLatestTurnOnSettle(false, false, true)).toBe(false);
	});
});

describe('reading anchor across a width change (#865)', () => {
	it('keeps a row that starts inside the view at the same offset', () => {
		const anchor = readingAnchorOf('row-a', 140, 300, 100);
		expect(anchor).toEqual({ key: 'row-a', offset: 40, past: 0 });
		// After the reflow the row sits 900px further down: scroll down by that.
		expect(resolveReadingAnchorDelta(anchor, 1040, 180, 60)).toBe(940);
	});

	it('keeps the same fraction of a long row the reader is partway through', () => {
		const anchor = readingAnchorOf('reply', -400, 1600, 0);
		expect(anchor.past).toBe(0.25);
		// Landscape: the reply is half as tall; a quarter in is 200px past its top.
		expect(resolveReadingAnchorDelta(anchor, -1500, 800, 0)).toBe(-1300);
	});
});

describe('queued prompt tracker', () => {
	const entry = (id: string, content: string) => ({ id, clientMessageId: `client-${id}`, content });

	it('marks a prompt queued before Stop as queued, so it does not pull a reader', () => {
		const tracker = createQueuedPromptTracker();
		tracker.observe([entry('a', 'Continue')], false);
		tracker.observe([entry('a', 'Continue')], true);
		expect(tracker.classify({ content: 'Continue', commandId: 'client-a' })).toEqual({ fromQueue: true, followsStop: false });
	});

	it('follows a send made while Stop is pending', () => {
		const tracker = createQueuedPromptTracker();
		tracker.observe([entry('b', 'Continue')], true);
		expect(tracker.classify({ content: 'Continue', commandId: 'client-b' })).toEqual({ fromQueue: true, followsStop: true });
	});

	it('follows a resend of the same words after the earlier prompt was withdrawn', () => {
		const tracker = createQueuedPromptTracker();
		tracker.observe([entry('a', 'Continue')], false);
		tracker.observe([], false);
		tracker.observe([entry('b', 'Continue')], true);
		expect(tracker.classify({ content: 'Continue', commandId: 'client-b' })).toEqual({ fromQueue: true, followsStop: true });
	});

	it('treats a direct resend of a withdrawn prompt as new, by command id', () => {
		const tracker = createQueuedPromptTracker();
		tracker.observe([entry('a', 'Continue')], false);
		tracker.observe([], false);
		expect(tracker.classify({ content: 'Continue', commandId: 'client-b', turnId: 'turn-b' })).toEqual({ fromQueue: false, followsStop: false });
		expect(tracker.classify({ content: 'Continue', commandId: 'client-a' })).toEqual({ fromQueue: true, followsStop: false });
	});

	it('does not let a withdrawn entry with no client id claim a composer send', () => {
		const tracker = createQueuedPromptTracker();
		tracker.observe([{ id: 'row-1', content: 'Continue' }], false);
		tracker.observe([], true);
		expect(tracker.classify({ content: 'Continue', commandId: 'client-b', turnId: 'turn-b' })).toEqual({ fromQueue: false, followsStop: false });
	});

	it('treats a composer send as new even when it joins the turn an entry ran as', () => {
		const tracker = createQueuedPromptTracker();
		// The opener landed in a batch with its reply, so it was never the newest row.
		tracker.observe([{ id: 'turn-1', content: 'Opener' }], false);
		expect(tracker.classify({ content: 'Follow-up', commandId: 'client-b', turnId: 'turn-1' })).toEqual({ fromQueue: false, followsStop: false });
	});

	it('matches an entry with no client id by the queue row its row names', () => {
		const tracker = createQueuedPromptTracker();
		tracker.observe([{ id: 'row-1', content: 'Same' }, { id: 'row-2', content: 'Same' }], false);
		tracker.observe([], false);
		expect(tracker.classify({ content: 'Same', turnId: 'row-2' }).fromQueue).toBe(true);
		expect(tracker.classify({ content: 'Same', turnId: 'live-turn', queuedTurnId: 'row-1' }).fromQueue).toBe(true);
		expect(tracker.classify({ content: 'Same', turnId: 'row-1' }).fromQueue).toBe(false);
	});

	it('recognizes an absorbed row that names only the turn it joined', () => {
		const tracker = createQueuedPromptTracker();
		tracker.observe([{ id: 'row-1', content: 'Absorbed' }, { id: 'row-2', content: 'Resumed' }], false);
		// Ordinary mid-turn delivery: the live turn id, no command id, no queue id.
		expect(tracker.classify({ content: 'Absorbed', turnId: 'live-turn' })).toEqual({ fromQueue: true, followsStop: false });
		// A parked turn resuming: its queue row has already left the snapshot.
		tracker.observe([], false);
		expect(tracker.classify({ content: 'Resumed', commandId: 'continuation-1', turnId: 'parked-turn' }).fromQueue).toBe(true);
	});

	it('leaves other rows new when no queue entry shares their identity or words', () => {
		const tracker = createQueuedPromptTracker();
		tracker.observe([{ id: 'row-1', content: 'Listed' }], false);
		expect(tracker.classify({ content: 'Other', turnId: 'turn-9' }).fromQueue).toBe(false);
		expect(tracker.classify({ content: 'Other' }).fromQueue).toBe(false);
	});

	it('never matches a voice or brief row to a withdrawn prompt', () => {
		const tracker = createQueuedPromptTracker();
		tracker.observe([{ id: 'row-1', content: 'Continue' }], true);
		tracker.observe([], false);
		expect(tracker.classify({ content: 'Continue', turnId: 'voice-abc' })).toEqual({ fromQueue: false, followsStop: false });
		expect(tracker.classify({ content: 'Continue', turnId: 'brief-abc' })).toEqual({ fromQueue: false, followsStop: false });
		expect(tracker.classify({ content: 'Continue', turnId: 'row-1' }).fromQueue).toBe(true);
	});

	it('bounds its history but keeps queued prompts and ones that just left the queue', () => {
		const rows = (from: number, count: number) => Array.from({ length: count }, (_, i) => ({ id: `row-${from + i}`, content: `prompt ${from + i}` }));

		const longQueue = createQueuedPromptTracker();
		longQueue.observe(rows(0, 201), false);
		expect(longQueue.classify({ content: 'prompt 0', turnId: 'row-0' }).fromQueue).toBe(true);

		// The oldest prompt starts: the queue drops it before its row lands.
		const draining = createQueuedPromptTracker();
		draining.observe(rows(0, 201), false);
		draining.observe(rows(1, 200), false);
		expect(draining.classify({ content: 'prompt 0', turnId: 'row-0' }).fromQueue).toBe(true);

		const bounded = createQueuedPromptTracker();
		bounded.observe(rows(0, 201), false);
		bounded.observe([], false);
		expect(bounded.classify({ content: 'prompt 0', turnId: 'row-0' }).fromQueue).toBe(false);
		expect(bounded.classify({ content: 'prompt 1', turnId: 'row-1' }).fromQueue).toBe(true);
	});

	it('does not count a retained delivered row again, or re-arm its follow', () => {
		const tracker = createQueuedPromptTracker();
		tracker.observe([{ id: 'row-1', content: 'Continue' }], true);
		tracker.releaseStopFollow();
		expect(tracker.classify({ content: 'Continue', turnId: 'row-1' })).toEqual({ fromQueue: true, followsStop: false });
		// The queue keeps showing the delivered row while a cancel is settled.
		tracker.observe([{ id: 'row-1', content: 'Continue' }], true);
		expect(tracker.classify({ content: 'Continue', turnId: 'row-1' })).toEqual({ fromQueue: false, followsStop: false });
		expect(tracker.classify({ content: 'Continue' })).toEqual({ fromQueue: false, followsStop: false });
		expect(tracker.classify({ content: 'Continue', commandId: 'client-b' })).toEqual({ fromQueue: false, followsStop: false });
	});

	it('keeps one entry the same when the server snapshot replaces its local id', () => {
		const tracker = createQueuedPromptTracker();
		tracker.observe([{ id: 'local', clientMessageId: 'client-a', content: 'Continue' }], false);
		tracker.observe([{ id: 'server', clientMessageId: 'client-a', content: 'Continue' }], true);
		expect(tracker.classify({ content: 'Continue', turnId: 'server' })).toEqual({ fromQueue: true, followsStop: false });
	});

	it('drops the follow once the reader scrolls away, and forgets a landed row', () => {
		const tracker = createQueuedPromptTracker();
		tracker.observe([entry('b', 'Continue')], true);
		tracker.releaseStopFollow();
		expect(tracker.classify({ content: 'Continue', commandId: 'client-b' })).toEqual({ fromQueue: true, followsStop: false });
		expect(tracker.classify({ content: 'Continue', commandId: 'client-b' })).toEqual({ fromQueue: false, followsStop: false });
	});

	it('forgets everything on reset', () => {
		const tracker = createQueuedPromptTracker();
		tracker.observe([entry('a', 'Continue')], false);
		tracker.reset();
		expect(tracker.classify({ content: 'Continue', commandId: 'client-a' })).toEqual({ fromQueue: false, followsStop: false });
	});
});
