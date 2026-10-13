/**
 * Behavior tests for RowActionMenu — the shared collapsed-⋮ + slide-out rail
 * used by session rows and file rows.
 *
 * The animation itself is pure CSS (not asserted here). What matters is the
 * contract the parents rely on:
 *  - the caller's action buttons render inside the tray
 *  - a single ⋮ toggle renders with the given accessible name + aria-expanded
 *  - tapping the toggle calls onToggle AND stops propagation, so the row-level
 *    click (open the item) never also fires
 *  - an optional `persistent` action renders in its own slot OUTSIDE the
 *    collapsing tray (so it stays visible on touch/narrow) — asserted here by
 *    DOM placement, since jsdom doesn't evaluate the media query that does the
 *    collapsing; the placement is what guarantees the behavior in both modes.
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import RowActionMenu from './RowActionMenu.svelte';

// A snippet with the two action buttons a caller would pass in.
const actions = createRawSnippet(() => ({
	render: () => `<span><button title="Rename">R</button><button title="Delete">D</button></span>`
}));

// A snippet for the always-visible persistent slot.
const persistentAction = createRawSnippet(() => ({
	render: () => `<button title="Complete">C</button>`
}));

afterEach(() => cleanup());

describe('RowActionMenu', () => {
	it('renders the caller action buttons plus one labelled ⋮ toggle', () => {
		render(RowActionMenu, {
			props: { open: false, onToggle: vi.fn(), label: 'File actions', children: actions }
		});
		expect(screen.getByTitle('Rename')).toBeTruthy();
		expect(screen.getByTitle('Delete')).toBeTruthy();
		const toggle = screen.getByRole('button', { name: 'File actions' });
		expect(toggle).toBeTruthy();
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
	});

	it('reflects the open prop on the toggle aria-expanded', () => {
		render(RowActionMenu, {
			props: { open: true, onToggle: vi.fn(), label: 'File actions', children: actions }
		});
		expect(
			screen.getByRole('button', { name: 'File actions' }).getAttribute('aria-expanded')
		).toBe('true');
	});

	it('calls onToggle and stops the click from bubbling to the row', async () => {
		const onToggle = vi.fn();
		// A row-level click that must NOT fire when the toggle is tapped.
		const rowClick = vi.fn();
		document.body.addEventListener('click', rowClick);
		try {
			render(RowActionMenu, {
				props: { open: false, onToggle, label: 'Session actions', children: actions }
			});
			await fireEvent.click(screen.getByRole('button', { name: 'Session actions' }));
			expect(onToggle).toHaveBeenCalledTimes(1);
			expect(rowClick).not.toHaveBeenCalled();
		} finally {
			document.body.removeEventListener('click', rowClick);
		}
	});

	it('omits the persistent slot entirely when no persistent snippet is passed', () => {
		const { container } = render(RowActionMenu, {
			props: { open: false, onToggle: vi.fn(), label: 'File actions', children: actions }
		});
		// Existing consumers pass no `persistent` — the extra wrapper must not appear.
		expect(container.querySelector('.ram-persistent')).toBeNull();
	});

	it('renders persistent content in its own slot OUTSIDE the collapsing tray', () => {
		const { container } = render(RowActionMenu, {
			props: {
				open: false,
				onToggle: vi.fn(),
				label: 'Session actions',
				children: actions,
				persistent: persistentAction
			}
		});
		const complete = screen.getByTitle('Complete');
		expect(complete).toBeTruthy();
		// It lives in .ram-persistent, which is a sibling of the tray, NOT inside it —
		// that placement is what keeps it visible while .ram-tray collapses on mobile.
		expect(complete.closest('.ram-persistent')).toBeTruthy();
		expect(complete.closest('.ram-tray')).toBeNull();
		// The tray still holds only the caller's children.
		expect(container.querySelector('.ram-tray [title="Rename"]')).toBeTruthy();
		expect(container.querySelector('.ram-persistent [title="Rename"]')).toBeNull();
	});

	it('keeps the persistent slot present in both tray states (open and closed)', () => {
		// The persistent action must render regardless of open/closed — it is never
		// gated on the tray being expanded (that's the whole point on mobile).
		for (const open of [false, true]) {
			const { container } = render(RowActionMenu, {
				props: {
					open,
					onToggle: vi.fn(),
					label: 'Session actions',
					children: actions,
					persistent: persistentAction
				}
			});
			expect(container.querySelector('.ram-persistent [title="Complete"]')).toBeTruthy();
			cleanup();
		}
	});
});
