/**
 * A popover opened from a row sits beside that row: under a row in the upper
 * half of the panel, above a row in the lower half (the Snoozed lane), and
 * never off the panel's edge.
 */
import { describe, expect, it } from 'vitest';
import { anchoredPopover } from './sheet-anchor';

describe('anchoredPopover', () => {
	it('opens under a row near the top', () => {
		expect(anchoredPopover({ top: 120, bottom: 160, height: 900 })).toEqual({
			side: 'below',
			top: 164,
			bottom: null,
			maxHeight: 728
		});
	});

	it('opens above a snoozed row resting at the bottom', () => {
		expect(anchoredPopover({ top: 840, bottom: 880, height: 900 })).toEqual({
			side: 'above',
			top: null,
			bottom: 64,
			maxHeight: 828
		});
	});

	it('keeps a usable height when the row is at the very edge', () => {
		const below = anchoredPopover({ top: 100, bottom: 440, height: 600 });
		expect(below.side).toBe('below');
		expect(below.top).toBe(444);
		expect(below.maxHeight).toBe(148);
		// 300 - 8 - 120 = 172 caps the top, so the picker keeps 120px.
		expect(anchoredPopover({ top: 0, bottom: 260, height: 300 })).toEqual({
			side: 'below',
			top: 172,
			bottom: null,
			maxHeight: 120
		});
	});

	it('anchors a row half scrolled out of view to the part that shows', () => {
		const out = anchoredPopover({ top: 880, bottom: 940, height: 900 });
		expect(out.side).toBe('above');
		expect(out.bottom).toBe(24);
	});
});
