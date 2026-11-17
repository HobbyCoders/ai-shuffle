import { describe, expect, it } from 'vitest';
import { canRecordSplitScroll, isScrollClamped } from './split-scroll-place';

describe('split scroll place', () => {
	it('records the reader before resize observation so a layout round trip restores the new place', () => {
		let saved = 80;
		const scroller = { scrollTop: 160, scrollHeight: 650, clientHeight: 300 };
		// Width changed from the last measurement, but the same split scroller is still shown.
		if (canRecordSplitScroll(1100, true, false) && !isScrollClamped(scroller, saved)) saved = scroller.scrollTop;
		expect(saved).toBe(160);
		// A later shrink clamps the position, but does not supersede the reader’s scroll.
		const clamped = { ...scroller, scrollTop: 100, clientHeight: 550 };
		if (canRecordSplitScroll(1080, true, false) && !isScrollClamped(clamped, saved)) saved = clamped.scrollTop;
		expect(saved).toBe(160);
	});

	it('does not record a hidden or incoming split pane before restoration', () => {
		expect(canRecordSplitScroll(0, true, false)).toBe(false);
		expect(canRecordSplitScroll(1100, false, false)).toBe(false);
		expect(canRecordSplitScroll(1100, true, true)).toBe(false);
	});
});
