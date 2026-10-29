import { describe, expect, it } from 'vitest';
import { readLeavingScrollTop } from './scroll-top-persist';

/**
 * Opening Settings (or any full page) and coming back put a reader who had
 * scrolled up back at the bottom. By the time MessageArea's onDestroy ran, its
 * container was already out of the layout (scrollHeight and clientHeight 0),
 * so scrollTop read 0 and overwrote the position captured from the reader's
 * last scroll frame; the remount then had "no position" and pinned the bottom.
 * Found by the side-panels journey (settings-and-back).
 */
describe('readLeavingScrollTop', () => {
	const box = (scrollTop: number, clientHeight: number, isConnected = true) =>
		({ scrollTop, clientHeight, isConnected }) as unknown as HTMLElement;

	it('reads the container while it is still laid out', () => {
		expect(readLeavingScrollTop(box(4200, 693), 1000)).toBe(4200);
		expect(readLeavingScrollTop(box(0, 693), 1000)).toBe(0);
	});

	it('keeps the last captured position once the container left the layout', () => {
		expect(readLeavingScrollTop(box(0, 0), 10226)).toBe(10226);
		expect(readLeavingScrollTop(box(0, 693, false), 10226)).toBe(10226);
	});

	it('has nothing to save when it was never captured and cannot be read', () => {
		expect(readLeavingScrollTop(box(0, 0), undefined)).toBeUndefined();
	});
});
