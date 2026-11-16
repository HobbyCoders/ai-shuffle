import { describe, expect, it } from 'vitest';
import { isQuickSwipe, QUICK_SWIPE_MS, swipeArmDistance, SWIPE_ARM_MAX_PX } from './swipe-arm';

describe('swipe arm distance', () => {
	it('is a share of a phone row and capped on a wide one', () => {
		expect(swipeArmDistance(320, 0.4)).toBeCloseTo(128);
		expect(swipeArmDistance(856, 0.4)).toBe(SWIPE_ARM_MAX_PX);
	});
});

describe('quick swipe (#1637)', () => {
	const quick = (durationMs: number, dx: number, peakDx = dx) => isQuickSwipe({ durationMs, dx, peakDx, minDx: 60 });

	it('takes a short swipe from its press, lift and farthest point', () => {
		// Press to lift 166ms, 80px: no move in between is needed.
		expect(quick(166, 80)).toBe(true);
		expect(quick(166, -80)).toBe(true);
		expect(quick(QUICK_SWIPE_MS, 60)).toBe(true);
	});

	it('refuses a long gesture, a short distance or no reading at all', () => {
		expect(quick(QUICK_SWIPE_MS + 1, 80)).toBe(false);
		expect(quick(600, 80)).toBe(false);
		expect(quick(166, 59)).toBe(false);
		// Identical or missing timestamps are no reading, not an instant swipe.
		expect(quick(0, 80)).toBe(false);
		expect(quick(-5, 80)).toBe(false);
		expect(quick(Number.NaN, 80)).toBe(false);
	});

	it('cancels when the finger came back more than a quarter of the way', () => {
		expect(quick(130, 85, 100)).toBe(true);
		expect(quick(130, 75, 100)).toBe(true);
		expect(quick(130, 74, 100)).toBe(false);
		expect(quick(130, -70, -140)).toBe(false);
		// Ended on the other side of the press: not the farthest point's way.
		expect(quick(130, -70, 80)).toBe(false);
	});
});
