import { afterEach, describe, expect, it, vi } from 'vitest';
import { beforeTranscriptCovered, exposedReadingViewport, obscuresTranscript } from './transcript-reading-area';
import { createTranscriptWindow, dominantReadingIndex } from './transcript-window';

const viewport = { top: 56, bottom: 697, left: 0, right: 390 };
const cleanups: Array<() => void> = [];
afterEach(() => { for (const cleanup of cleanups.splice(0).reverse()) cleanup(); });
function overlay(top: number, left = 8, right = 382, enabled = true) {
	const node = document.createElement('div'); document.body.append(node);
	const measure = vi.spyOn(node, 'getBoundingClientRect').mockReturnValue({ top, bottom: 844, left, right } as DOMRect);
	const action = obscuresTranscript(node, enabled);
	cleanups.push(() => { action.destroy(); node.remove(); });
	return { action, measure, node };
}

describe('exposed transcript reading area', () => {
	it('keeps the exposed prefix rather than the larger overlay-covered reply', () => {
		const rows = [{ index: 0, getBoundingClientRect: () => ({ top: 56, bottom: 300 }) },
			{ index: 1, getBoundingClientRect: () => ({ top: 300, bottom: 3000 }) }];
		expect(dominantReadingIndex(rows, viewport, row => row.index)).toBe(1);
		overlay(400);
		const reading = () => dominantReadingIndex(rows, exposedReadingViewport(viewport), row => row.index);
		expect(reading()).toBe(0);
		let costs = [6000, 2000];
		const window = createTranscriptWindow({ cost: index => costs[index], budget: 12000, getReadingIndex: reading });
		window.setItemCount(2); costs = [6000, 13000];
		expect(window.setItemCount(2, { followTail: false })).toEqual({ start: 0, end: 1 });
	});
	it('captures the previous exposed area before a full cover is published', () => {
		overlay(400);
		let captured: unknown;
		cleanups.push(beforeTranscriptCovered(() => { captured = exposedReadingViewport(viewport); }));
		const cover = overlay(0);
		expect(captured).toEqual({ top: 56, bottom: 400 });
		expect(exposedReadingViewport(viewport)).toEqual({ top: 56, bottom: 56 });
		cover.action.destroy();
		expect(exposedReadingViewport(viewport)).toEqual({ top: 56, bottom: 400 });
	});
	it('publishes only on open/resize, captures once per open and releases a closed publisher', () => {
		const capture = vi.fn(); cleanups.push(beforeTranscriptCovered(capture));
		const sheet = overlay(400, 8, 382, false);
		for (let i = 0; i < 20; i++) sheet.action.update(false);
		expect(capture).not.toHaveBeenCalled(); expect(sheet.measure).not.toHaveBeenCalled();
		sheet.action.update(true);
		for (let i = 0; i < 20; i++) sheet.action.update(true);
		expect(capture).toHaveBeenCalledTimes(1); expect(sheet.measure).toHaveBeenCalledTimes(1);
		for (let i = 0; i < 20; i++) expect(exposedReadingViewport(viewport).bottom).toBe(400);
		expect(sheet.measure).toHaveBeenCalledTimes(1);
		sheet.measure.mockReturnValue({ top: 450, bottom: 844, left: 8, right: 382 } as DOMRect);
		window.dispatchEvent(new Event('resize'));
		expect(exposedReadingViewport(viewport).bottom).toBe(450);
		expect(sheet.measure).toHaveBeenCalledTimes(2);
		sheet.action.update(false);
		window.dispatchEvent(new Event('resize'));
		expect(exposedReadingViewport(viewport).bottom).toBe(697);
		expect(sheet.measure).toHaveBeenCalledTimes(2);
	});
	it('cannot retain coverage after unmount or route removal, including delayed events', () => {
		const sheet = overlay(400);
		expect(exposedReadingViewport(viewport).bottom).toBe(400);
		sheet.action.destroy();
		window.dispatchEvent(new Event('resize'));
		sheet.node.dispatchEvent(new Event('animationend'));
		expect(exposedReadingViewport(viewport)).toEqual({ top: 56, bottom: 697 });
		expect(sheet.measure).toHaveBeenCalledTimes(1);
		const routeCover = overlay(0);
		expect(exposedReadingViewport(viewport).bottom).toBe(56);
		routeCover.node.remove();
		window.dispatchEvent(new Event('resize'));
		routeCover.node.dispatchEvent(new Event('transitionend'));
		expect(exposedReadingViewport(viewport)).toEqual({ top: 56, bottom: 697 });
		expect(routeCover.measure).toHaveBeenCalledTimes(1);
	});
	it('disconnects the resize publisher and rejects a queued callback after unmount', () => {
		let notify = () => {};
		const disconnect = vi.fn(), observe = vi.fn();
		vi.stubGlobal('ResizeObserver', class {
			constructor(callback: () => void) { notify = callback; }
			observe = observe;
			disconnect = disconnect;
		});
		try {
			const sheet = overlay(400);
			expect(observe).toHaveBeenCalledWith(sheet.node);
			notify(); expect(sheet.measure).toHaveBeenCalledTimes(2);
			sheet.action.destroy();
			expect(disconnect).toHaveBeenCalledTimes(1);
			notify();
			expect(sheet.measure).toHaveBeenCalledTimes(2);
			expect(exposedReadingViewport(viewport).bottom).toBe(697);
		} finally { vi.unstubAllGlobals(); }
	});
	it('uses the highest overlapping sheet and ignores disconnected/other-column publishers', () => {
		const first = overlay(500); overlay(400); overlay(0, 500, 800);
		overlay(0).node.remove();
		expect(exposedReadingViewport(viewport).bottom).toBe(400);
		first.action.destroy();
		expect(exposedReadingViewport(viewport).bottom).toBe(400);
	});
});
