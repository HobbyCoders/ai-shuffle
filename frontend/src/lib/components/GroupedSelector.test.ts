/**
 * The grouped menu slides between its main list and a group's list. The panel
 * that is off screen must be inert, or Tab can reach it and the browser scrolls
 * the clipped track sideways to show it (the phone Chat controls Project page
 * went blank this way). Keyboard focus follows the slide instead, without
 * scrolling.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

import GroupedSelector from './GroupedSelector.svelte';

afterEach(cleanup);

// Svelte may set the property rather than the attribute; jsdom reflects neither way.
const isInert = (el: HTMLElement) => el.hasAttribute('inert') || (el as { inert?: boolean }).inert === true;

function setup() {
	const { container } = render(GroupedSelector, {
		items: [{ id: 'p1', name: 'Alpha' }],
		entityType: 'projects',
		selectedId: null,
		onSelect: vi.fn(),
		extraGroups: [
			{ name: 'Folders', items: [{ id: 'f1', name: 'Beta' }] },
			{ name: 'Archive', items: [{ id: 'a1', name: 'Gamma' }] }
		]
	});
	const main = container.querySelector<HTMLElement>('.main-panel')!;
	const sub = container.querySelector<HTMLElement>('.sub-panel')!;
	return { main, sub };
}

describe('GroupedSelector panels', () => {
	it('keeps only the visible panel reachable', async () => {
		const { sub } = setup();
		expect(isInert(main)).toBe(false);
		expect(isInert(sub)).toBe(true);

		await fireEvent.click(screen.getByRole('button', { name: /Folders/ }));
		expect(isInert(main)).toBe(true);
		expect(isInert(sub)).toBe(false);
	});

	it('moves keyboard focus with the slide without scrolling', async () => {
		const { sub } = setup();
		const focus = vi.spyOn(HTMLElement.prototype, 'focus');
		const group = screen.getByRole('button', { name: /Archive/ });
		group.focus();
		focus.mockClear();

		await fireEvent.click(group);
		await waitFor(() => expect(document.activeElement).toBe(sub.querySelector('.back-button')));
		expect(focus).toHaveBeenLastCalledWith({ preventScroll: true });

		focus.mockClear();
		await new Promise((r) => setTimeout(r, 300)); // let the slide finish
		await fireEvent.click(sub.querySelector<HTMLElement>('.back-button')!);
		// Back to the row that opened it, not the first row: in a scrolled list
		// the first row can be out of view.
		await waitFor(() => expect(document.activeElement).toBe(group));
		expect(focus).toHaveBeenLastCalledWith({ preventScroll: true });
		focus.mockRestore();
	});
});
