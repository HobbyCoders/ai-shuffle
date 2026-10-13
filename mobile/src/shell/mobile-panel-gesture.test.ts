import { describe, expect, it } from 'vitest';
import { createPanelGesture, finishPanelGesture, updatePanelGesture } from './mobile-panel-gesture';

const start = { x: 100, y: 100, time: 0 };
const point = (x: number, y: number, time = 500) => ({ x, y, time });

describe('panel dismiss gesture ownership', () => {
	it('preserves taps and small movement without starting a dismiss', () => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		expect(updatePanelGesture(gesture, point(102, 104, 10))).toEqual({ mode: 'pending', offset: 0 });
		expect(finishPanelGesture(gesture, point(102, 104, 20), 240)).toBe(false);
	});

	it.each([
		['upward', 100, 80],
		['horizontal', 125, 105],
		['diagonal tie', 120, 120]
	])('locks an initially %s gesture to scrolling even if it later turns downward', (_, x, y) => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		expect(updatePanelGesture(gesture, point(Number(x), Number(y))).mode).toBe('scroll');
		expect(updatePanelGesture(gesture, point(100, 240))).toEqual({ mode: 'scroll', offset: 0 });
		expect(finishPanelGesture(gesture, point(100, 240), 240)).toBe(false);
	});

	it('dismisses a deliberate downward pull that starts with every container at top', () => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		expect(updatePanelGesture(gesture, point(103, 145))).toEqual({ mode: 'dismiss', offset: 45 });
		expect(finishPanelGesture(gesture, point(103, 180, 900), 240)).toBe(true);
	});

	it('settles an incomplete slow pull without closing', () => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		updatePanelGesture(gesture, point(100, 135));
		expect(finishPanelGesture(gesture, point(100, 135, 900), 240)).toBe(false);
	});

	it('requires a meaningful travel distance even for a fast flick', () => {
		const tiny = createPanelGesture({ ...start, eligible: true });
		updatePanelGesture(tiny, point(100, 114, 5));
		expect(finishPanelGesture(tiny, point(100, 114, 10), 240)).toBe(false);
		const flick = createPanelGesture({ ...start, eligible: true });
		updatePanelGesture(flick, point(100, 126, 20));
		expect(finishPanelGesture(flick, point(100, 126, 30), 240)).toBe(true);
	});

	it('does not collapse an extremely short panel from a tiny slow pull', () => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		updatePanelGesture(gesture, point(100, 120));
		expect(finishPanelGesture(gesture, point(100, 120, 900), 30)).toBe(false);
	});

	it('does not close when a downward pull is retracted before release', () => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		updatePanelGesture(gesture, point(100, 200));
		updatePanelGesture(gesture, point(100, 104, 900));
		expect(finishPanelGesture(gesture, point(100, 104, 1000), 240)).toBe(false);
	});


	it('finishes at most once when duplicate release events arrive', () => {
		const gesture = createPanelGesture({ ...start, eligible: true });
		updatePanelGesture(gesture, point(100, 200));
		expect(finishPanelGesture(gesture, point(100, 200), 240)).toBe(true);
		expect(finishPanelGesture(gesture, point(100, 200), 240)).toBe(false);
	});
});