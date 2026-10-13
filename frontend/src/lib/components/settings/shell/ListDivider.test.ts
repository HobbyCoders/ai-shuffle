import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ListDivider from './ListDivider.svelte';
import {
	clampListWidth,
	readListWidth,
	LIST_WIDTH_DEFAULT,
	LIST_WIDTH_MIN,
	LIST_WIDTH_STORAGE_KEY
} from '$lib/settings/list-width';

beforeEach(() => localStorage.clear());
afterEach(() => {
	cleanup();
	vi.restoreAllMocks();
});

function setup(width = 384, containerWidth = 1200) {
	const onchange = vi.fn();
	render(ListDivider, { width, containerWidth, onchange });
	const sep = screen.getByRole('separator', { name: 'Resize list' });
	const now = () => Number(sep.getAttribute('aria-valuenow'));
	return { sep, now, onchange };
}

describe('list width helpers', () => {
	it('clamps the list to 300px and leaves the detail 440px', () => {
		expect(clampListWidth(100, 1200)).toBe(300);
		expect(clampListWidth(900, 1200)).toBe(760);
		expect(clampListWidth(500, 1200)).toBe(500);
		expect(clampListWidth(500, 600)).toBe(300);
		expect(clampListWidth(Number.NaN, 1200)).toBe(LIST_WIDTH_DEFAULT);
	});

	it('reads a remembered width and ignores junk', () => {
		expect(readListWidth()).toBe(LIST_WIDTH_DEFAULT);
		localStorage.setItem(LIST_WIDTH_STORAGE_KEY, '420');
		expect(readListWidth()).toBe(420);
		localStorage.setItem(LIST_WIDTH_STORAGE_KEY, 'wide');
		expect(readListWidth()).toBe(LIST_WIDTH_DEFAULT);
		localStorage.setItem(LIST_WIDTH_STORAGE_KEY, '120');
		expect(readListWidth()).toBe(LIST_WIDTH_DEFAULT);
	});

	it('survives blocked storage', () => {
		vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
			throw new Error('blocked');
		});
		expect(readListWidth()).toBe(LIST_WIDTH_DEFAULT);
	});
});

describe('ListDivider', () => {
	it('is a focusable vertical separator with its value and bounds', () => {
		const { sep } = setup();
		expect(sep.getAttribute('aria-orientation')).toBe('vertical');
		expect(sep.getAttribute('aria-valuenow')).toBe('384');
		expect(sep.getAttribute('aria-valuemin')).toBe(String(LIST_WIDTH_MIN));
		expect(sep.getAttribute('aria-valuemax')).toBe('760');
		expect(sep.tabIndex).toBe(0);
	});

	it('moves 16px per arrow, 64px with Shift, and jumps with Home and End', async () => {
		const { sep, now, onchange } = setup();
		await fireEvent.keyDown(sep, { key: 'ArrowRight' });
		expect(now()).toBe(400);
		await fireEvent.keyDown(sep, { key: 'ArrowLeft', shiftKey: true });
		expect(now()).toBe(336);
		await fireEvent.keyDown(sep, { key: 'Home' });
		expect(now()).toBe(300);
		await fireEvent.keyDown(sep, { key: 'End' });
		expect(now()).toBe(760);
		expect(onchange).toHaveBeenLastCalledWith(760);
		expect(localStorage.getItem(LIST_WIDTH_STORAGE_KEY)).toBe('760');
	});

	it('resets to 384 with Enter or a double-click', async () => {
		const { sep, now } = setup(500);
		await fireEvent.keyDown(sep, { key: 'Enter' });
		expect(now()).toBe(384);
		await fireEvent.keyDown(sep, { key: 'End' });
		await fireEvent.dblClick(sep);
		expect(now()).toBe(384);
		expect(localStorage.getItem(LIST_WIDTH_STORAGE_KEY)).toBe('384');
	});

	it('follows a pointer drag, clamped, and saves when it ends', async () => {
		const { sep, now } = setup();
		await fireEvent.pointerDown(sep, { button: 0, pointerId: 1, clientX: 384 });
		await fireEvent.pointerMove(sep, { pointerId: 1, clientX: 520 });
		expect(now()).toBe(520);
		expect(localStorage.getItem(LIST_WIDTH_STORAGE_KEY)).toBeNull();
		await fireEvent.pointerMove(sep, { pointerId: 1, clientX: 5000 });
		expect(now()).toBe(760);
		await fireEvent.pointerUp(sep, { pointerId: 1, clientX: 5000 });
		expect(localStorage.getItem(LIST_WIDTH_STORAGE_KEY)).toBe('760');
		await fireEvent.pointerMove(sep, { pointerId: 1, clientX: 400 });
		expect(now()).toBe(760);
	});
});
