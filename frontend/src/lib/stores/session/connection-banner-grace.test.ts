/**
 * Tests for connection-banner-grace.ts — the 3s grace period before the
 * "connection lost" banner appears in ChatView.
 *
 * Pins the contract: transient blips (tab switches, brief WS hiccups) that
 * resolve within the grace period never flash the banner; a disconnect that
 * persists past the grace period shows it; reconnecting hides it immediately
 * with no debounce; and a pending timer never fires after destroy().
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import {
	createConnectionBannerGrace,
	DEFAULT_CONNECTION_BANNER_GRACE_MS,
	OVERDUE_TIMER_SLACK_MS
} from './connection-banner-grace';

const GRACE = DEFAULT_CONNECTION_BANNER_GRACE_MS;

// jsdom (pretendToBeVisual) reports a visible page; tests flip it per case.
function setHidden(hidden: boolean, { dispatch = true } = {}) {
	Object.defineProperty(document, 'hidden', { configurable: true, get: () => hidden });
	if (dispatch) document.dispatchEvent(new Event('visibilitychange'));
}

beforeEach(() => {
	vi.useFakeTimers();
});

afterEach(() => {
	vi.useRealTimers();
	// Drop the override so jsdom's own getter applies again.
	delete (document as { hidden?: boolean }).hidden;
});

describe('createConnectionBannerGrace', () => {
	it('does not show the banner if disconnected for less than the grace period then reconnects', () => {
		const onShow = vi.fn();
		const onHide = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide });

		gate.setConnected(false);
		vi.advanceTimersByTime(2999);
		expect(onShow).not.toHaveBeenCalled();

		gate.setConnected(true);
		vi.advanceTimersByTime(10_000);

		expect(onShow).not.toHaveBeenCalled();
		// Reconnecting before the banner ever showed shouldn't fire onHide either
		// (nothing to hide).
		expect(onHide).not.toHaveBeenCalled();
	});

	it('shows the banner once disconnected continuously for >= the grace period', () => {
		const onShow = vi.fn();
		const onHide = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide });

		gate.setConnected(false);
		vi.advanceTimersByTime(DEFAULT_CONNECTION_BANNER_GRACE_MS);

		expect(onShow).toHaveBeenCalledTimes(1);
		expect(onHide).not.toHaveBeenCalled();
	});

	it('hides the banner immediately on reconnect once it was shown, with no debounce', () => {
		const onShow = vi.fn();
		const onHide = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide });

		gate.setConnected(false);
		vi.advanceTimersByTime(DEFAULT_CONNECTION_BANNER_GRACE_MS);
		expect(onShow).toHaveBeenCalledTimes(1);

		gate.setConnected(true);
		expect(onHide).toHaveBeenCalledTimes(1);

		// No further timer fires late.
		vi.advanceTimersByTime(10_000);
		expect(onShow).toHaveBeenCalledTimes(1);
		expect(onHide).toHaveBeenCalledTimes(1);
	});

	it('does not leak or late-fire a pending timer after destroy()', () => {
		const onShow = vi.fn();
		const onHide = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide });

		gate.setConnected(false);
		vi.advanceTimersByTime(1000);
		gate.destroy();

		vi.advanceTimersByTime(10_000);

		expect(onShow).not.toHaveBeenCalled();
		expect(onHide).not.toHaveBeenCalled();
		expect(vi.getTimerCount()).toBe(0);
	});

	it('setConnected(false) is idempotent while a timer is already armed (does not reset the grace window)', () => {
		const onShow = vi.fn();
		const onHide = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide });

		gate.setConnected(false);
		vi.advanceTimersByTime(2000);
		// Re-reporting "still disconnected" shouldn't restart the timer.
		gate.setConnected(false);
		vi.advanceTimersByTime(1000);

		expect(onShow).toHaveBeenCalledTimes(1);
	});

	it('respects a custom graceMs', () => {
		const onShow = vi.fn();
		const onHide = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide, graceMs: 500 });

		gate.setConnected(false);
		vi.advanceTimersByTime(499);
		expect(onShow).not.toHaveBeenCalled();

		vi.advanceTimersByTime(1);
		expect(onShow).toHaveBeenCalledTimes(1);
	});

	it('ignores setConnected calls after destroy()', () => {
		const onShow = vi.fn();
		const onHide = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide });

		gate.destroy();
		gate.setConnected(false);
		vi.advanceTimersByTime(10_000);

		expect(onShow).not.toHaveBeenCalled();
	});
});

describe('createConnectionBannerGrace — only visible time counts', () => {
	it('never shows while hidden, then shows only after a fresh grace once visible', () => {
		const onShow = vi.fn();
		const onHide = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide });

		setHidden(true);
		gate.setConnected(false);
		vi.advanceTimersByTime(60_000);
		expect(onShow).not.toHaveBeenCalled();
		expect(vi.getTimerCount()).toBe(0);

		setHidden(false);
		vi.advanceTimersByTime(GRACE - 1);
		expect(onShow).not.toHaveBeenCalled();
		vi.advanceTimersByTime(1);
		expect(onShow).toHaveBeenCalledTimes(1);
		expect(onHide).not.toHaveBeenCalled();
		gate.destroy();
	});

	it('a quick resume reconnect never flashes the notice', () => {
		const onShow = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide: vi.fn() });

		gate.setConnected(false);
		vi.advanceTimersByTime(1000);
		setHidden(true);
		vi.advanceTimersByTime(5 * 60_000);
		setHidden(false);
		vi.advanceTimersByTime(800); // resume reconnect handshake
		gate.setConnected(true);
		vi.advanceTimersByTime(10_000);

		expect(onShow).not.toHaveBeenCalled();
		gate.destroy();
	});

	it('a timer that fires while the page is hidden (no visibilitychange seen) does not show', () => {
		const onShow = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide: vi.fn() });

		gate.setConnected(false);
		// The page froze without dispatching visibilitychange.
		setHidden(true, { dispatch: false });
		vi.advanceTimersByTime(GRACE * 4);
		expect(onShow).not.toHaveBeenCalled();

		// Back on screen: a fresh window, not an instant show.
		setHidden(false);
		vi.advanceTimersByTime(GRACE - 1);
		expect(onShow).not.toHaveBeenCalled();
		vi.advanceTimersByTime(1);
		expect(onShow).toHaveBeenCalledTimes(1);
		gate.destroy();
	});

	it('an overdue timer that runs on wake before visibilitychange re-arms a fresh window', () => {
		const onShow = vi.fn();
		let skew = 0;
		const gate = createConnectionBannerGrace({ onShow, onHide: vi.fn(), now: () => Date.now() + skew });

		gate.setConnected(false);
		vi.advanceTimersByTime(GRACE - 1);
		// The page sat frozen for a minute; the timer runs the moment it wakes.
		skew += 60_000;
		vi.advanceTimersByTime(1);
		expect(onShow).not.toHaveBeenCalled();

		// The resume reconnect lands inside the fresh window.
		vi.advanceTimersByTime(GRACE - 1);
		gate.setConnected(true);
		vi.advanceTimersByTime(10_000);
		expect(onShow).not.toHaveBeenCalled();
		gate.destroy();
	});

	it('a timer a little late (within the slack) still shows', () => {
		const onShow = vi.fn();
		let skew = 0;
		const gate = createConnectionBannerGrace({ onShow, onHide: vi.fn(), now: () => Date.now() + skew });

		gate.setConnected(false);
		skew += OVERDUE_TIMER_SLACK_MS;
		vi.advanceTimersByTime(GRACE);
		expect(onShow).toHaveBeenCalledTimes(1);
		gate.destroy();
	});

	it('reconnecting inside the visible window never shows', () => {
		const onShow = vi.fn();
		const onHide = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide });

		setHidden(true);
		gate.setConnected(false);
		setHidden(false);
		vi.advanceTimersByTime(GRACE - 1);
		gate.setConnected(true);
		vi.advanceTimersByTime(10_000);

		expect(onShow).not.toHaveBeenCalled();
		expect(onHide).not.toHaveBeenCalled();
		gate.destroy();
	});

	it('a notice shown before hiding hides with the page and needs a fresh window after', () => {
		const onShow = vi.fn();
		const onHide = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide });

		gate.setConnected(false);
		vi.advanceTimersByTime(GRACE);
		expect(onShow).toHaveBeenCalledTimes(1);

		setHidden(true);
		expect(onHide).toHaveBeenCalledTimes(1);
		vi.advanceTimersByTime(60_000);

		setHidden(false);
		vi.advanceTimersByTime(GRACE - 1);
		expect(onShow).toHaveBeenCalledTimes(1);
		vi.advanceTimersByTime(1);
		expect(onShow).toHaveBeenCalledTimes(2);

		// Reconnect hides it once; nothing fires late.
		gate.setConnected(true);
		expect(onHide).toHaveBeenCalledTimes(2);
		vi.advanceTimersByTime(10_000);
		expect(onShow).toHaveBeenCalledTimes(2);
		gate.destroy();
	});

	it('reconnecting while hidden shows nothing on return', () => {
		const onShow = vi.fn();
		const onHide = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide });

		gate.setConnected(false);
		vi.advanceTimersByTime(GRACE);
		setHidden(true);
		gate.setConnected(true);
		setHidden(false);
		vi.advanceTimersByTime(10_000);

		expect(onShow).toHaveBeenCalledTimes(1);
		expect(onHide).toHaveBeenCalledTimes(1);
		gate.destroy();
	});

	it('a duplicate visible event does not restart a running window', () => {
		const onShow = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide: vi.fn() });

		gate.setConnected(false);
		vi.advanceTimersByTime(2000);
		setHidden(false);
		vi.advanceTimersByTime(1000);
		expect(onShow).toHaveBeenCalledTimes(1);
		gate.destroy();
	});

	it('pageshow (BFCache restore) starts a fresh window like visibilitychange', () => {
		const onShow = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide: vi.fn() });

		setHidden(true);
		gate.setConnected(false);
		setHidden(false, { dispatch: false });
		window.dispatchEvent(new Event('pageshow'));
		vi.advanceTimersByTime(GRACE);
		expect(onShow).toHaveBeenCalledTimes(1);
		gate.destroy();
	});

	it('visibleTimeOnly: false keeps the plain timer', () => {
		const onShow = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide: vi.fn(), visibleTimeOnly: false });

		setHidden(true);
		gate.setConnected(false);
		vi.advanceTimersByTime(GRACE);
		expect(onShow).toHaveBeenCalledTimes(1);
		gate.destroy();
	});

	it('destroy() stops listening for visibility changes', () => {
		const onShow = vi.fn();
		const gate = createConnectionBannerGrace({ onShow, onHide: vi.fn() });

		setHidden(true);
		gate.setConnected(false);
		gate.destroy();
		setHidden(false);
		vi.advanceTimersByTime(10_000);

		expect(onShow).not.toHaveBeenCalled();
		expect(vi.getTimerCount()).toBe(0);
	});
});
