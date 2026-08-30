import { describe, it, expect, beforeEach } from 'vitest';
import { browserPanel, flushWidthPersist } from './browser-panel.svelte';

// The store is a module singleton; openPanel() gives every test a known baseline
// (open=true, userClosedForSession=null) without needing a private reset.
beforeEach(() => {
	localStorage.clear();
	browserPanel.openPanel();
	browserPanel.setWidth(480);
	// Settle the debounced write so no timer from one test lands in another.
	flushWidthPersist();
	// Reset per-attachment page tracking so url assertions start clean.
	browserPanel.resetPageTracking();
	browserPanel.setStatus('disconnected', null);
	browserPanel.setInspectMode(false);
});

const sampleElement = {
	selector: 'main > button#cta',
	tag: 'button',
	id: 'cta',
	classes: ['btn'],
	rect: { x: 1, y: 2, width: 3, height: 4 },
};

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

	it('does not cap widths above the old 960px maximum', () => {
		browserPanel.setWidth(1400);
		expect(browserPanel.current.width).toBe(1400);
	});

	it('keeps an in-range width (rounded)', () => {
		browserPanel.setWidth(512.6);
		expect(browserPanel.current.width).toBe(513);
	});

	it('persists the clamped width to localStorage once the debounce settles', () => {
		browserPanel.setWidth(720);
		flushWidthPersist();
		const raw = localStorage.getItem('ai-shuffle-browser-panel');
		expect(raw).not.toBeNull();
		expect(JSON.parse(raw!).width).toBe(720);
	});

	// A drag-resize calls setWidth on every pointermove; a synchronous write per
	// move stalls the main thread and stutters the live canvas.
	it('debounces the write during a drag, keeping the in-memory width live', () => {
		flushWidthPersist();
		localStorage.clear();

		for (let w = 500; w <= 560; w += 10) browserPanel.setWidth(w);
		expect(browserPanel.current.width).toBe(560);
		expect(localStorage.getItem('ai-shuffle-browser-panel')).toBeNull();

		flushWidthPersist();
		const raw = localStorage.getItem('ai-shuffle-browser-panel');
		expect(JSON.parse(raw!).width).toBe(560);
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

describe('browserPanel page tracking', () => {
	it('setPage records url + loading and marks tracking live', () => {
		browserPanel.setPage('https://tracked.example/', true);
		expect(browserPanel.current.url).toBe('https://tracked.example/');
		expect(browserPanel.current.pageLoading).toBe(true);
		expect(browserPanel.current.pageTracked).toBe(true);
	});

	it('once tracked, coarse status frames no longer overwrite the CDP url', () => {
		browserPanel.setPage('https://tracked.example/page2', false);
		browserPanel.setStatus('live', 'https://stale-registry.example/');
		expect(browserPanel.current.url).toBe('https://tracked.example/page2');

		// …but before any page frame, registry metadata still fills the gap.
		browserPanel.resetPageTracking();
		browserPanel.setStatus('connecting', 'https://registry.example/');
		expect(browserPanel.current.url).toBe('https://registry.example/');
	});

	it('resetPageTracking clears loading + tracked but keeps the last url', () => {
		browserPanel.setPage('https://kept.example/', true);
		browserPanel.resetPageTracking();
		expect(browserPanel.current.pageLoading).toBe(false);
		expect(browserPanel.current.pageTracked).toBe(false);
		expect(browserPanel.current.url).toBe('https://kept.example/');
	});
});

describe('browserPanel inspect mode + selection', () => {
	it('starts with inspect off and nothing selected', () => {
		expect(browserPanel.current.inspectMode).toBe(false);
		expect(browserPanel.current.selectedElement).toBeNull();
	});

	it('setInspectMode(true) keeps a selection; setInspectMode(false) drops it', () => {
		browserPanel.setInspectMode(true);
		browserPanel.setSelectedElement(sampleElement);
		expect(browserPanel.current.selectedElement).toEqual(sampleElement);

		browserPanel.setInspectMode(true); // re-arm is a no-op for the selection
		expect(browserPanel.current.selectedElement).toEqual(sampleElement);

		browserPanel.setInspectMode(false);
		expect(browserPanel.current.inspectMode).toBe(false);
		expect(browserPanel.current.selectedElement).toBeNull();
	});

	it('closePanel and toggle-close reset inspect state', () => {
		browserPanel.setInspectMode(true);
		browserPanel.setSelectedElement(sampleElement);
		browserPanel.closePanel('s1');
		expect(browserPanel.current.inspectMode).toBe(false);
		expect(browserPanel.current.selectedElement).toBeNull();

		browserPanel.openPanel();
		browserPanel.setInspectMode(true);
		browserPanel.setSelectedElement(sampleElement);
		browserPanel.toggle('s1'); // closes
		expect(browserPanel.current.inspectMode).toBe(false);
		expect(browserPanel.current.selectedElement).toBeNull();
	});

	it('resetPageTracking (session switch) drops the selection', () => {
		browserPanel.setInspectMode(true);
		browserPanel.setSelectedElement(sampleElement);
		browserPanel.resetPageTracking();
		expect(browserPanel.current.selectedElement).toBeNull();
		// The mode itself survives — the user re-picks on the new page.
		expect(browserPanel.current.inspectMode).toBe(true);
	});
});
