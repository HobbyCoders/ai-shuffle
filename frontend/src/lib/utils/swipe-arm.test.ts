import { afterEach, describe, expect, it, vi } from 'vitest';
import { flickVelocity, sampleTime, swipeArmDistance, SWIPE_ARM_MAX_PX } from './swipe-arm';

describe('swipe arm distance', () => {
	it('is a share of a phone row and capped on a wide one', () => {
		expect(swipeArmDistance(320, 0.4)).toBeCloseTo(128);
		expect(swipeArmDistance(856, 0.4)).toBe(SWIPE_ARM_MAX_PX);
	});
});

describe('flick velocity', () => {
	it('averages the last moves, so one late-handled move does not decide it', () => {
		// 80px in 64ms (1.25px/ms) as eight moves 8ms apart.
		const even = Array.from({ length: 9 }, (_, i) => ({ t: 1000 + i * 8, x: 50 + i * 10 }));
		expect(flickVelocity(even)).toBeCloseTo(1.25);
		// The last two moves 15ms apart: the last move alone reads 0.67px/ms,
		// the window still reads the finger's speed.
		const late = [...even.slice(0, 8), { t: 1071, x: 130 }];
		expect(flickVelocity(late)).toBeGreaterThan(0.9);
	});

	it('reads a slow drag as slow, reaching back past the window', () => {
		expect(flickVelocity([{ t: 1000, x: 300 }, { t: 1400, x: 220 }])).toBeCloseTo(-0.2);
		expect(flickVelocity([{ t: 1000, x: 300 }])).toBe(0);
	});

	it('looks only at the last window of a drag that sped up', () => {
		const samples = [{ t: 0, x: 0 }, { t: 400, x: 40 }, { t: 420, x: 80 }, { t: 440, x: 120 }];
		expect(flickVelocity(samples)).toBeCloseTo(80 / 40);
	});
});

describe('sample time', () => {
	afterEach(() => vi.restoreAllMocks());

	it("uses the event's own time, and now when that is missing or in the future", () => {
		vi.spyOn(performance, 'now').mockReturnValue(5000);
		const at = (timeStamp: number) => {
			const event = new Event('pointermove');
			Object.defineProperty(event, 'timeStamp', { value: timeStamp });
			return sampleTime(event);
		};
		expect(at(4980)).toBe(4980);
		expect(at(0)).toBe(5000);
		expect(at(9000)).toBe(5000);
	});
});
