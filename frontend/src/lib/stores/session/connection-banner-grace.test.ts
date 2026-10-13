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
import { createConnectionBannerGrace, DEFAULT_CONNECTION_BANNER_GRACE_MS } from './connection-banner-grace';

beforeEach(() => {
	vi.useFakeTimers();
});

afterEach(() => {
	vi.useRealTimers();
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
