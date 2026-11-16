import { afterEach, describe, expect, it, vi } from 'vitest';
import { FLICK_STALE_MS, FlickTracker, flickVelocity, gestureClock, swipeArmDistance, SWIPE_ARM_MAX_PX } from './swipe-arm';

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

/** A pointer event stamped `t` at `x`, optionally carrying coalesced moves. */
function pointer(type: string, x: number, t: number, coalesced?: { x: number; t: number }[]): PointerEvent {
	const event = new Event(type) as PointerEvent;
	Object.defineProperty(event, 'clientX', { value: x });
	Object.defineProperty(event, 'clientY', { value: 0 });
	Object.defineProperty(event, 'timeStamp', { value: t });
	if (coalesced) {
		Object.defineProperty(event, 'getCoalescedEvents', { value: () => coalesced.map((c) => pointer(type, c.x, c.t)) });
	}
	return event;
}

describe('gesture clock', () => {
	afterEach(() => vi.restoreAllMocks());

	it("reads the events' own stamps when the press has one, and never mixes in now", () => {
		vi.spyOn(performance, 'now').mockReturnValue(99_999);
		const clock = gestureClock(pointer('pointerdown', 0, 1000));
		expect(clock(pointer('pointermove', 0, 1016))).toBe(1016);
		// An unstamped event counts as the latest stamped one.
		expect(clock(pointer('pointermove', 0, 0))).toBe(1016);
		expect(clock()).toBe(1016);
	});

	it('reads now throughout when the press has no usable stamp (jsdom stamps 1970 ms)', () => {
		const now = vi.spyOn(performance, 'now').mockReturnValue(500);
		const clock = gestureClock(pointer('pointerdown', 0, Date.now()));
		now.mockReturnValue(520);
		expect(clock(pointer('pointermove', 0, 1016))).toBe(520);
		expect(gestureClock()()).toBe(520);
	});
});

describe('flick tracker', () => {
	afterEach(() => vi.restoreAllMocks());

	it('reads a busy-page flick from the moves the browser coalesced into one event', () => {
		// Press at 1000, rest 100ms, then 80px in 64ms handed over as one pointermove.
		const flick = new FlickTracker();
		flick.start(50, pointer('pointerdown', 50, 1000));
		const moves = Array.from({ length: 8 }, (_, i) => ({ x: 60 + i * 10, t: 1108 + i * 8 }));
		flick.addPointer(pointer('pointermove', 130, 1164, moves));
		expect(flick.velocity(pointer('pointerup', 130, 1166))).toBeCloseTo(1.25);
		// Without the coalesced moves, the same event alone reads the slow average.
		const bare = new FlickTracker();
		bare.start(50, pointer('pointerdown', 50, 1000));
		bare.addPointer(pointer('pointermove', 130, 1164));
		expect(bare.velocity()).toBeLessThan(0.9);
	});

	it('reads zero, never Infinity, for one sample or for moves at the same instant', () => {
		const one = new FlickTracker();
		one.start(50, pointer('pointerdown', 50, 1000));
		expect(one.velocity(pointer('pointerup', 50, 1001))).toBe(0);
		const same = new FlickTracker();
		same.start(50, pointer('pointerdown', 50, 1000));
		same.addPointer(pointer('pointermove', 130, 1000, [{ x: 90, t: 1000 }, { x: 130, t: 1000 }]));
		expect(same.velocity(pointer('pointerup', 130, 1000))).toBe(0);
		expect(new FlickTracker().velocity()).toBe(0);
	});

	it('reads zero when the finger rested before letting go', () => {
		const flick = new FlickTracker();
		flick.start(50, pointer('pointerdown', 50, 1000));
		for (let i = 1; i <= 8; i++) flick.add(50 + i * 10, pointer('pointermove', 0, 1000 + i * 8));
		expect(flick.velocity(pointer('pointerup', 130, 1064 + FLICK_STALE_MS))).toBeCloseTo(1.25);
		expect(flick.velocity(pointer('pointerup', 130, 1064 + FLICK_STALE_MS + 1))).toBe(0);
		expect(flick.velocity(pointer('pointerup', 130, 1564))).toBe(0);
	});

	it('drops a move stamped before the latest one', () => {
		const flick = new FlickTracker();
		flick.addAt(0, 0);
		flick.addAt(40, 40);
		flick.addAt(500, 20);
		expect(flick.velocityAt(41)).toBeCloseTo(1);
	});
});
