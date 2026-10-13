import { describe, it, expect, beforeEach } from 'vitest';
import { browserPanel, flushWidthPersist } from './browser-panel.svelte';
let sequence = 0;
let a: string;
let b: string;
beforeEach(() => { a = `a-${++sequence}`; b = `b-${sequence}`; localStorage.clear(); browserPanel.setAutoOpen(true); browserPanel.setWidth(480); flushWidthPersist(); });
describe('session browser visibility', () => {
 it('starts closed and never borrows another session visibility', () => {
  browserPanel.openPanel(a);
  expect(browserPanel.forSession(b).open).toBe(false);
  expect(browserPanel.forSession(a).open).toBe(true);
 });
 it('keeps dismissal after activity in a background session', () => {
  browserPanel.openPanel(a); browserPanel.closePanel(a); browserPanel.autoOpenFor(b); browserPanel.autoOpenFor(a);
  expect(browserPanel.forSession(a).open).toBe(false); expect(browserPanel.forSession(b).open).toBe(true);
  browserPanel.openPanel(a); expect(browserPanel.forSession(a).userClosedForSession).toBeNull();
 });
 it('toggles only the supplied session', () => {
  browserPanel.toggle(a); browserPanel.toggle(b); browserPanel.toggle(a);
  expect(browserPanel.forSession(a).open).toBe(false); expect(browserPanel.forSession(b).open).toBe(true);
 });
 it('keeps expansion per session and restores the global width', () => {
  browserPanel.setWidth(640); browserPanel.openPanel(a); browserPanel.setExpanded(a, true);
  expect(browserPanel.forSession(b).expanded).toBe(false);
  browserPanel.setExpanded(a, false); expect(browserPanel.current.width).toBe(640);
 });
 it('does not auto-open without a session or when disabled', () => {
  browserPanel.autoOpenFor(null); browserPanel.setAutoOpen(false); browserPanel.autoOpenFor(a);
  expect(browserPanel.forSession(a).open).toBe(false); browserPanel.openPanel(a); expect(browserPanel.forSession(a).open).toBe(true);
 });
 it('isolates page metadata and inspection and clears attachment state', () => {
  browserPanel.setStatus('live', 'https://a.test', a); browserPanel.setPage('https://tracked.test', true, a);
  browserPanel.setStatus('connecting', 'https://old.test', a);
  expect(browserPanel.forSession(a).url).toBe('https://tracked.test'); expect(browserPanel.forSession(b).url).toBeNull();
  browserPanel.setInspectMode(true, a); expect(browserPanel.forSession(b).inspectMode).toBe(false);
  browserPanel.resetPageTracking(a); expect(browserPanel.forSession(a).inspectMode).toBe(false); expect(browserPanel.forSession(a).url).toBeNull();
 });
 it('persists preferences but no runtime map', () => {
  browserPanel.openPanel(a); browserPanel.setExpanded(a, true); browserPanel.setWidth(700); flushWidthPersist();
  expect(JSON.parse(localStorage.getItem('ai-shuffle-browser-panel')!)).toEqual({ width: 700 });
  browserPanel.setDefaultViewport({ mode: 'fixed', width: 390, height: 844 });
  expect(browserPanel.forSession(a).defaultViewport).toEqual(browserPanel.forSession(b).defaultViewport);
  browserPanel.setWidth(-1); expect(browserPanel.current.width).toBe(320);
  browserPanel.setWidth(Infinity); expect(browserPanel.current.width).toBe(480);
 });
});

const sampleElement = {
	selector: 'main > button#cta',
	tag: 'button',
	id: 'cta',
	classes: ['btn'],
	rect: { x: 1, y: 2, width: 3, height: 4 },
};

describe('browserPanel width clamp + persistence', () => {
	it('clamps below the minimum to 320', () => {
		browserPanel.setWidth(100);
		expect(browserPanel.forSession(a).width).toBe(320);
	});

	it('does not cap widths above the old 960px maximum', () => {
		browserPanel.setWidth(1400);
		expect(browserPanel.forSession(a).width).toBe(1400);
	});

	it('keeps an in-range width (rounded)', () => {
		browserPanel.setWidth(512.6);
		expect(browserPanel.forSession(a).width).toBe(513);
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
		expect(browserPanel.forSession(a).width).toBe(560);
		expect(localStorage.getItem('ai-shuffle-browser-panel')).toBeNull();

		flushWidthPersist();
		const raw = localStorage.getItem('ai-shuffle-browser-panel');
		expect(JSON.parse(raw!).width).toBe(560);
	});
});

describe('browserPanel setStatus', () => {
	it('updates status and url, and preserves url when omitted', () => {
		browserPanel.setStatus('live', 'https://example.com', a);
		expect(browserPanel.forSession(a).status).toBe('live');
		expect(browserPanel.forSession(a).url).toBe('https://example.com');

		browserPanel.setStatus('connecting', undefined, a);
		expect(browserPanel.forSession(a).status).toBe('connecting');
		expect(browserPanel.forSession(a).url).toBe('https://example.com'); // preserved
	});
});

describe('browserPanel page tracking', () => {
	it('setPage records url + loading and marks tracking live', () => {
		browserPanel.setPage('https://tracked.example/', true, a);
		expect(browserPanel.forSession(a).url).toBe('https://tracked.example/');
		expect(browserPanel.forSession(a).pageLoading).toBe(true);
		expect(browserPanel.forSession(a).pageTracked).toBe(true);
	});

	it('once tracked, coarse status frames no longer overwrite the CDP url', () => {
		browserPanel.setPage('https://tracked.example/page2', false, a);
		browserPanel.setStatus('live', 'https://stale-registry.example/', a);
		expect(browserPanel.forSession(a).url).toBe('https://tracked.example/page2');

		// …but before any page frame, registry metadata still fills the gap.
		browserPanel.resetPageTracking(a);
		browserPanel.setStatus('connecting', 'https://registry.example/', a);
		expect(browserPanel.forSession(a).url).toBe('https://registry.example/');
	});

	it('resetPageTracking clears loading, tracking and the previous attachment URL', () => {
		browserPanel.setPage('https://kept.example/', true, a);
		browserPanel.resetPageTracking(a);
		expect(browserPanel.forSession(a).pageLoading).toBe(false);
		expect(browserPanel.forSession(a).pageTracked).toBe(false);
		expect(browserPanel.forSession(a).url).toBeNull();
	});
});

describe('browserPanel inspect mode + selection', () => {
	it('starts with inspect off and nothing selected', () => {
		expect(browserPanel.forSession(a).inspectMode).toBe(false);
		expect(browserPanel.forSession(a).selectedElement).toBeNull();
	});

	it('setInspectMode(true) keeps a selection; setInspectMode(false) drops it', () => {
		browserPanel.setInspectMode(true, a);
		browserPanel.setSelectedElement(sampleElement, a);
		expect(browserPanel.forSession(a).selectedElement).toEqual(sampleElement);

		browserPanel.setInspectMode(true, a); // re-arm is a no-op for the selection
		expect(browserPanel.forSession(a).selectedElement).toEqual(sampleElement);

		browserPanel.setInspectMode(false, a);
		expect(browserPanel.forSession(a).inspectMode).toBe(false);
		expect(browserPanel.forSession(a).selectedElement).toBeNull();
	});

	it('closePanel and toggle-close reset inspect state', () => {
		browserPanel.setInspectMode(true, a);
		browserPanel.setSelectedElement(sampleElement, a);
		browserPanel.closePanel(a);
		expect(browserPanel.forSession(a).inspectMode).toBe(false);
		expect(browserPanel.forSession(a).selectedElement).toBeNull();

		browserPanel.openPanel(a);
		browserPanel.setInspectMode(true, a);
		browserPanel.setSelectedElement(sampleElement, a);
		browserPanel.toggle(a); // closes
		expect(browserPanel.forSession(a).inspectMode).toBe(false);
		expect(browserPanel.forSession(a).selectedElement).toBeNull();
	});

	it('resetPageTracking (session switch) drops the selection', () => {
		browserPanel.setInspectMode(true, a);
		browserPanel.setSelectedElement(sampleElement, a);
		browserPanel.resetPageTracking(a);
		expect(browserPanel.forSession(a).selectedElement).toBeNull();
		// Inspection never carries over to a different attachment.
		expect(browserPanel.forSession(a).inspectMode).toBe(false);
	});
});