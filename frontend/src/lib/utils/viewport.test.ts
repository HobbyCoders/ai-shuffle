/**
 * Tests for the desktop shell's narrow-window helper. This is deliberately
 * NOT a mobile-UI switch: the mobile shell is chosen by platform (see
 * platform.test.ts). It only decides when the desktop shell tightens up.
 */

import { describe, it, expect } from 'vitest';
import { isNarrowViewportWidth, NARROW_VIEWPORT_MAX_WIDTH } from './viewport';

describe('isNarrowViewportWidth', () => {
	it('is narrow for phone-sized and tablet-sized windows', () => {
		expect(isNarrowViewportWidth(390)).toBe(true);
		expect(isNarrowViewportWidth(884)).toBe(true);
	});

	it('is narrow up to and including the cutoff', () => {
		expect(isNarrowViewportWidth(1080)).toBe(true);
		expect(isNarrowViewportWidth(NARROW_VIEWPORT_MAX_WIDTH)).toBe(true);
	});

	it('is wide above the cutoff', () => {
		expect(isNarrowViewportWidth(1081)).toBe(false);
		expect(isNarrowViewportWidth(1920)).toBe(false);
	});
});
