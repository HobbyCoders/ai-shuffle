import { describe, it, expect } from 'vitest';
import {
	TRANSCRIPT_WINDOW_SIZE,
	clampWindowStart,
	classifyTurnEntry,
	expandedWindowStart,
	initialWindowStart,
	isLiveTailIndex,
	scrollTopPreservingBottom,
	shouldExpandOnScroll,
	turnTopsFromVisibility,
	windowStartForItem,
	type TurnVisibility
} from './transcript-window';
import { resolveCurrentTurnIndex } from '$lib/utils/transcript-navigation';

describe('transcript window', () => {
	it('mounts only a trailing window of a long transcript', () => {
		expect(initialWindowStart(500, 60)).toBe(440);
		expect(initialWindowStart(500)).toBe(500 - TRANSCRIPT_WINDOW_SIZE);
	});

	it('mounts everything for a transcript shorter than the window', () => {
		expect(initialWindowStart(12, 60)).toBe(0);
		expect(initialWindowStart(0, 60)).toBe(0);
	});

	it('mounts everything when windowing is disabled', () => {
		expect(initialWindowStart(500, 0)).toBe(0);
	});

	it('never un-mounts rows as new messages arrive', () => {
		// Reader expanded back to item 100; appending must not push the window
		// forward and destroy what they are reading.
		expect(clampWindowStart(100, 500, 60)).toBe(100);
		expect(clampWindowStart(100, 900, 60)).toBe(100);
		expect(clampWindowStart(0, 900, 60)).toBe(0);
	});

	it('clamps back into range when the transcript shrinks (rewind/clear)', () => {
		expect(clampWindowStart(440, 100, 60)).toBe(40);
		expect(clampWindowStart(440, 10, 60)).toBe(0);
	});

	it('expands backwards a chunk at a time, stopping at the top', () => {
		expect(expandedWindowStart(100, 30)).toBe(70);
		expect(expandedWindowStart(10, 30)).toBe(0);
		expect(expandedWindowStart(0, 30)).toBe(0);
	});

	it('mounts far enough back to reach a jump target, with lead-in', () => {
		expect(windowStartForItem(440, 120, 8)).toBe(112);
		expect(windowStartForItem(440, 3, 8)).toBe(0);
		// Already mounted: the window never moves forward.
		expect(windowStartForItem(100, 300, 8)).toBe(100);
	});

	it('expands on scroll-up only while history is still unmounted', () => {
		expect(shouldExpandOnScroll(120, 440, 900)).toBe(true);
		expect(shouldExpandOnScroll(4000, 440, 900)).toBe(false);
		expect(shouldExpandOnScroll(0, 0, 900)).toBe(false);
	});

	it('keeps the reader in place when prepending (distance from bottom held)', () => {
		// Before: 5000px tall, scrolled to 3000 → 2000px below the fold.
		const distanceFromBottom = 5000 - 3000;
		// Prepending 1200px of history grows the content to 6200px.
		expect(scrollTopPreservingBottom(6200, distanceFromBottom)).toBe(4200);
		// Bottom-pinned readers stay pinned.
		expect(scrollTopPreservingBottom(6200, 0)).toBe(0 + 6200);
	});

	it('marks the live tail by GLOBAL index, not window-relative index', () => {
		// 500 items, window starts at 440, LIVE_TAIL 8 → items 492..499.
		expect(isLiveTailIndex(440 + 51, 500, 8)).toBe(false);
		expect(isLiveTailIndex(440 + 52, 500, 8)).toBe(true);
		expect(isLiveTailIndex(499, 500, 8)).toBe(true);
	});
});

describe('turn visibility → rail index', () => {
	const ids = ['a', 'b', 'c', 'd'];
	const readingLine = 140;
	const allMounted = (): boolean => true;

	function indexFor(
		states: Record<string, TurnVisibility>,
		mounted: (turnId: string) => boolean = allMounted
	) {
		const tops = turnTopsFromVisibility(ids, new Map(Object.entries(states)), readingLine, mounted);
		return resolveCurrentTurnIndex(tops, readingLine);
	}

	it('picks the last turn that crossed the reading line', () => {
		expect(indexFor({ a: 'above', b: 'above', c: 'inside', d: 'below' })).toBe(2);
		expect(indexFor({ a: 'above', b: 'below', c: 'below', d: 'below' })).toBe(0);
	});

	it('treats everything below the line as not-yet-reached', () => {
		expect(indexFor({ a: 'below', b: 'below', c: 'below', d: 'below' })).toBe(0);
	});

	it('counts unmounted older turns as already passed', () => {
		// 'a' and 'b' are above the mounted window: they are older, hence above.
		const mounted = (id: string): boolean => id === 'c' || id === 'd';
		expect(indexFor({ c: 'inside', d: 'below' }, mounted)).toBe(2);
	});

	it('skips mounted-but-unobserved turns instead of freezing on turn 0', () => {
		// 'd' has just mounted and the observer has not reported it yet.
		expect(indexFor({ a: 'above', b: 'above', c: 'above' })).toBe(2);
	});
});

describe('classifyTurnEntry', () => {
	it('reports an anchor inside the reading band', () => {
		expect(classifyTurnEntry(true, 40, 0, undefined)).toBe('inside');
	});

	it('reports anchors scrolled past the band as above', () => {
		expect(classifyTurnEntry(false, -320, 0, 'inside')).toBe('above');
	});

	it('reports anchors below the band as below', () => {
		expect(classifyTurnEntry(false, 900, 0, 'inside')).toBe('below');
	});

	it('keeps the previous state when the root bounds are unknown', () => {
		expect(classifyTurnEntry(false, 900, null, 'above')).toBe('above');
		expect(classifyTurnEntry(false, 900, undefined, undefined)).toBeUndefined();
	});
});
