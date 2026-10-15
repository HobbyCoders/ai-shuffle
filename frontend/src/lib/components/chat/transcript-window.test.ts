import { describe, it, expect, vi, afterEach } from 'vitest';
import {
	TRANSCRIPT_WINDOW_SIZE,
	TRANSCRIPT_WINDOW_CAP,
	initialWindowRange,
	olderWindowRange,
	newerWindowRange,
	windowRangeForItem,
	clampWindowRange,
	clampWindowStart,
	createWindowBackfill,
	createTranscriptWindow,
	classifyTurnEntry,
	expandedWindowStart,
	initialWindowStart,
	isLiveTailIndex,
	scrollTopPreservingBottom,
	settleSkippedTurns,
	shouldExpandOnScroll,
	turnTopsFromVisibility,
	windowStartForItem,
	type TurnVisibility
} from './transcript-window';
import { resolveCurrentTurnIndex } from '$lib/utils/transcript-navigation';

afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });


describe('pure bounded ranges for reactive views', () => {
	it('shifts, jumps, follows and clamps without mutating input', () => {
		const tail = Object.freeze(initialWindowRange(2000));
		expect(TRANSCRIPT_WINDOW_CAP).toBe(120);
		expect(tail).toEqual({ start: 1880, end: 2000 });
		const older = olderWindowRange(tail, 2000);
		expect(older).toEqual({ start: 1850, end: 1970 });
		expect(newerWindowRange(older, 2000)).toEqual(tail);
		expect(clampWindowRange(older, 2100)).toBe(older);
		expect(clampWindowRange(older, 2100, { followTail: true })).toEqual({ start: 1980, end: 2100 });
		expect(windowRangeForItem(tail, 900, 2000)).toEqual({ start: 840, end: 960 });
		expect(windowRangeForItem(tail, -1, 2000)).toBe(tail);
		expect(windowRangeForItem(tail, 2000, 2000)).toBe(tail);
		expect(clampWindowRange(tail, 20)).toEqual({ start: 0, end: 20 });
		expect(clampWindowRange(tail, 0)).toEqual({ start: 0, end: 0 });
	});

	it('also bounds the rows of one giant group through repeated navigation', () => {
		let range = initialWindowRange(2000, 30);
		for (let i = 0; i < 200; i++) {
			range = olderWindowRange(range, 2000, 10, 30);
			expect(range.end - range.start).toBe(30);
		}
		expect(range).toEqual({ start: 0, end: 30 });
		for (let i = 0; i < 200; i++) range = newerWindowRange(range, 2000, 10, 30);
		expect(range).toEqual({ start: 1970, end: 2000 });
	});
});

describe('bounded transcript range controller', () => {
	it('caps mounting after appends, streaming changes, idle time and edge navigation', async () => {
		vi.useFakeTimers();
		const window = createTranscriptWindow({ cap: 120, step: 30 });
		expect(window.setItemCount(2000)).toEqual({ start: 1880, end: 2000 });
		window.onStreamingChange(true);
		expect(window.setItemCount(2100)).toEqual({ start: 1980, end: 2100 });
		window.onStreamingChange(false);
		await vi.advanceTimersByTimeAsync(600_000);
		expect(window.visibleRange).toEqual({ start: 1980, end: 2100 });
		expect(vi.getTimerCount()).toBe(0);
		for (let i = 0; i < 100; i++) {
			const range = window.loadOlder();
			expect(range.end - range.start).toBe(120);
		}
		expect(window.visibleRange).toEqual({ start: 0, end: 120 });
		for (let i = 0; i < 100; i++) window.extendNewer();
		expect(window.visibleRange).toEqual({ start: 1980, end: 2100 });
		expect(window.followsTail).toBe(true);
	});

	it('retains overlap for a keyed anchor when shifting either edge', () => {
		const window = createTranscriptWindow({ cap: 120, step: 30 });
		window.setItemCount(2000);
		const before = window.visibleRange;
		const older = window.extendOlder();
		expect(older).toEqual({ start: 1850, end: 1970 });
		// DOM-side code can retain any key in this 90-item overlap, then restore
		// its top offset rather than using the now-invalid bottom distance.
		expect(Math.min(before.end, older.end) - Math.max(before.start, older.start)).toBe(90);
		expect(window.extendNewer()).toEqual(before);
	});

	it('centres jumps, keeps historical readers in place on append, and returns to latest', () => {
		const window = createTranscriptWindow();
		window.setItemCount(2000);
		expect(window.windowAround(900)).toEqual({ start: 840, end: 960 });
		const anchorRange = window.visibleRange;
		expect(window.setItemCount(2300)).toBe(anchorRange);
		expect(window.onStreamingChange(true)).toBe(anchorRange);
		expect(window.isStreaming).toBe(true);
		expect(window.windowAround(-1)).toBe(anchorRange);
		expect(window.windowAround(2300)).toBe(anchorRange);
		expect(window.toTail()).toEqual({ start: 2180, end: 2300 });
		expect(window.setItemCount(2400, { followTail: false })).toEqual({ start: 2180, end: 2300 });
	});

	it('freezes hidden appends and resumes once without disturbing historical readers', () => {
		const window = createTranscriptWindow();
		window.setItemCount(2000);
		const before = window.pauseWhenHidden(true);
		for (let count = 2001; count <= 2100; count++) expect(window.setItemCount(count)).toBe(before);
		expect(window.pauseWhenHidden(false)).toEqual({ start: 1980, end: 2100 });
		window.windowAround(900);
		const history = window.pauseWhenHidden(true);
		window.setItemCount(2400);
		expect(window.pauseWhenHidden(false)).toBe(history);
	});

	it('clamps rewinds and empty histories even while hidden', () => {
		const window = createTranscriptWindow();
		window.setItemCount(2000);
		window.pauseWhenHidden(true);
		expect(window.setItemCount(20)).toEqual({ start: 0, end: 20 });
		expect(window.setItemCount(0)).toEqual({ start: 0, end: 0 });
		window.pauseWhenHidden(false);
		expect(window.setItemCount(25)).toEqual({ start: 0, end: 25 });
	});

	it('validates bounds and keeps oversized steps overlapping when possible', () => {
		expect(() => createTranscriptWindow({ cap: 0 })).toThrow(RangeError);
		expect(() => createTranscriptWindow({ cap: Infinity })).toThrow(RangeError);
		expect(() => createTranscriptWindow({ step: 0.5 })).toThrow(RangeError);
		const window = createTranscriptWindow({ cap: 10, step: 50 });
		expect(() => window.setItemCount(-1)).toThrow(RangeError);
		window.setItemCount(100);
		expect(window.loadOlder()).toEqual({ start: 81, end: 91 });
		const single = createTranscriptWindow({ cap: 1 });
		single.setItemCount(3);
		expect(single.loadOlder()).toEqual({ start: 1, end: 2 });
	});
});

describe('background transcript mounting', () => {
	it('checks visibility, streaming and idle budget before producing each chunk', () => {
		let callback!: IdleRequestCallback;
		const request = vi.fn((cb: IdleRequestCallback) => { callback = cb; return 1; });
		vi.stubGlobal('requestIdleCallback', request);
		vi.stubGlobal('cancelIdleCallback', vi.fn());
		let hidden = true, streaming = false, start = initialWindowStart(500);
		const backfill = createWindowBackfill(() => !hidden && !streaming && start > 0,
			() => { start = expandedWindowStart(start); });
		backfill.schedule();
		expect(request).not.toHaveBeenCalled();
		hidden = false; backfill.schedule();
		callback({ didTimeout: true, timeRemaining: () => 0 });
		expect(start).toBe(440);
		callback({ didTimeout: false, timeRemaining: () => 2 });
		expect(start).toBe(440);
		streaming = true;
		callback({ didTimeout: false, timeRemaining: () => 20 });
		expect(start).toBe(440);
		streaming = false; backfill.schedule();
		callback({ didTimeout: false, timeRemaining: () => 20 });
		expect(start).toBe(410);
		hidden = true;
		callback({ didTimeout: false, timeRemaining: () => 20 });
		expect(start).toBe(410);
		hidden = false; backfill.schedule();
		while (start > 0) callback({ didTimeout: false, timeRemaining: () => 20 });
		expect(start).toBe(0);
		backfill.cancel();
	});

	it('cancels fallback work and rechecks eligibility when the timer fires', async () => {
		vi.useFakeTimers();
		vi.stubGlobal('requestIdleCallback', undefined);
		let allowed = true;
		const expand = vi.fn(() => { allowed = false; });
		const backfill = createWindowBackfill(() => allowed, expand);
		backfill.schedule(); backfill.cancel();
		await vi.advanceTimersByTimeAsync(300);
		expect(expand).not.toHaveBeenCalled();
		backfill.schedule(); allowed = false;
		await vi.advanceTimersByTimeAsync(300);
		expect(expand).not.toHaveBeenCalled();
		allowed = true; backfill.schedule();
		await vi.advanceTimersByTimeAsync(300);
		expect(expand).toHaveBeenCalledOnce();
	});
});

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

describe('settleSkippedTurns', () => {
	const ids = ['a', 'b', 'c', 'd', 'e'];

	function settle(states: Record<string, TurnVisibility>, reported: string[]) {
		const map = new Map(Object.entries(states));
		settleSkippedTurns(ids, map, new Set(reported));
		return Object.fromEntries(map);
	}

	it('moves turns a long downward jump skipped above the band', () => {
		// a..d jumped from below the band to above it in one frame; only e reported.
		expect(settle({ a: 'above', b: 'below', c: 'below', d: 'below', e: 'inside' }, ['e'])).toEqual({
			a: 'above', b: 'above', c: 'above', d: 'above', e: 'inside'
		});
	});

	it('moves turns a long upward jump skipped below the band', () => {
		expect(settle({ a: 'inside', b: 'above', c: 'above', d: 'below', e: 'above' }, ['a'])).toEqual({
			a: 'inside', b: 'below', c: 'below', d: 'below', e: 'below'
		});
	});

	it('changes nothing without a turn reported inside the band', () => {
		const states: Record<string, TurnVisibility> = { a: 'above', b: 'below', c: 'below' };
		expect(settle(states, [])).toEqual(states);
	});

	it('only trusts a reported turn whose final state in the batch is inside', () => {
		// c was reported inside then above in the same batch (last entry wins);
		// a was inside before this batch and was not reported.
		const states: Record<string, TurnVisibility> = { a: 'inside', b: 'below', c: 'above', d: 'above' };
		expect(settle(states, ['c'])).toEqual(states);
	});

	it('leaves unobserved turns and turns between inside ones alone', () => {
		expect(settle({ b: 'inside', c: 'below', d: 'inside' }, ['b', 'd'])).toEqual({
			b: 'inside', c: 'below', d: 'inside'
		});
	});

	it('lets the rail resolve the reported turn after a jump', () => {
		const map = new Map<string, TurnVisibility>(
			Object.entries({ a: 'above', b: 'below', c: 'below', d: 'inside', e: 'below' })
		);
		settleSkippedTurns(ids, map, new Set(['d']));
		const tops = turnTopsFromVisibility(ids, map, 140, () => true);
		expect(resolveCurrentTurnIndex(tops, 140)).toBe(3);
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
