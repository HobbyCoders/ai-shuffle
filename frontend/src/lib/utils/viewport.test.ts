/**
 * Tests for viewport helpers.
 *
 * Locks in the fullscreen-panel breakpoint, which must treat unfolded
 * foldables (Galaxy Z Fold inner screen ≈ 884px) as handheld/fullscreen even
 * though their width exceeds the 640px `sm` breakpoint.
 */

import { describe, it, expect } from 'vitest';
import { isPanelFullscreenWidth, PANEL_FULLSCREEN_MAX_WIDTH } from './viewport';

describe('isPanelFullscreenWidth', () => {
	it('is fullscreen on narrow phones (< 640px)', () => {
		expect(isPanelFullscreenWidth(390)).toBe(true); // iPhone-class
		expect(isPanelFullscreenWidth(639)).toBe(true);
		expect(isPanelFullscreenWidth(640)).toBe(true);
	});

	it('is fullscreen on the Galaxy Z Fold inner screen (~884px)', () => {
		// The reported bug: the Fold inner screen exceeds 640px, so the panel
		// used to open as a partial-width drawer instead of fullscreen.
		expect(isPanelFullscreenWidth(884)).toBe(true);
	});

	it('is fullscreen up to and including the 1080px cutoff', () => {
		expect(isPanelFullscreenWidth(1080)).toBe(true);
		expect(isPanelFullscreenWidth(PANEL_FULLSCREEN_MAX_WIDTH)).toBe(true);
	});

	it('is a side drawer above the cutoff (tablets landscape / desktop)', () => {
		expect(isPanelFullscreenWidth(1081)).toBe(false);
		expect(isPanelFullscreenWidth(1280)).toBe(false);
		expect(isPanelFullscreenWidth(1920)).toBe(false);
	});
});
