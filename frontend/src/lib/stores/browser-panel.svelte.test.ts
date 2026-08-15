import { describe, it, expect, beforeEach } from 'vitest';
import { browserPanel } from './browser-panel.svelte';

// The store is a module singleton; openPanel() gives every test a known baseline
// (open=true, userClosedForSession=null) without needing a private reset.
beforeEach(() => {
	localStorage.clear();
	browserPanel.openPanel();
	browserPanel.setWidth(480);
});

describe('browserPanel open/close/toggle', () => {
	it('openPanel opens and clears close-suppression', () => {
		browserPanel.closePanel('s1');
		browserPanel.openPanel();
		expect(browserPanel.current.open).toBe(true);
		expect(browserPanel.current.userClosedForSession).toBeNull();
	});

	it('closePanel closes and records the session it was closed for', () => {
		browserPanel.closePanel('s1');
		expect(browserPanel.current.open).toBe(false);
		expect(browserPanel.current.userClosedForSession).toBe('s1');
	});

	it('toggle flips open→closed (recording the session) and closed→open', () => {
		// starts open (baseline)
		browserPanel.toggle('s1');
		expect(browserPanel.current.open).toBe(false);
		expect(browserPanel.current.userClosedForSession).toBe('s1');

		browserPanel.toggle('s1');
		expect(browserPanel.current.open).toBe(true);
		expect(browserPanel.current.userClosedForSession).toBeNull();
	});
});

describe('browserPanel autoOpenFor suppression', () => {
	it('opens when nothing was closed', () => {
		browserPanel.closePanel('other'); // suppression is for a different session
		browserPanel.autoOpenFor('s1');
		expect(browserPanel.current.open).toBe(true);
	});

	it('is suppressed for the exact session the user closed', () => {
		browserPanel.closePanel('s1');
		browserPanel.autoOpenFor('s1');
		expect(browserPanel.current.open).toBe(false);
	});

	it('still opens for a different session after a close', () => {
		browserPanel.closePanel('s1');
		browserPanel.autoOpenFor('s2');
		expect(browserPanel.current.open).toBe(true);
	});

	it('is a no-op (leaves suppression intact) when already open', () => {
		// open baseline; autoOpen must not clear an unrelated suppression record
		browserPanel.openPanel();
		browserPanel.autoOpenFor('s3');
		expect(browserPanel.current.open).toBe(true);
	});
});

describe('browserPanel width clamp + persistence', () => {
	it('clamps below the minimum to 320', () => {
		browserPanel.setWidth(100);
		expect(browserPanel.current.width).toBe(320);
	});

	it('clamps above the maximum to 960', () => {
		browserPanel.setWidth(5000);
		expect(browserPanel.current.width).toBe(960);
	});

	it('keeps an in-range width (rounded)', () => {
		browserPanel.setWidth(512.6);
		expect(browserPanel.current.width).toBe(513);
	});

	it('persists the clamped width to localStorage', () => {
		browserPanel.setWidth(720);
		const raw = localStorage.getItem('ai-shuffle-browser-panel');
		expect(raw).not.toBeNull();
		expect(JSON.parse(raw!).width).toBe(720);
	});
});

describe('browserPanel setStatus', () => {
	it('updates status and url, and preserves url when omitted', () => {
		browserPanel.setStatus('live', 'https://example.com');
		expect(browserPanel.current.status).toBe('live');
		expect(browserPanel.current.url).toBe('https://example.com');

		browserPanel.setStatus('connecting');
		expect(browserPanel.current.status).toBe('connecting');
		expect(browserPanel.current.url).toBe('https://example.com'); // preserved
	});
});
