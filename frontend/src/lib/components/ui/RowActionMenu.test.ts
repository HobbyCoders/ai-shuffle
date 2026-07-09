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
 */
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent, cleanup } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import RowActionMenu from './RowActionMenu.svelte';

// A snippet with the two action buttons a caller would pass in.
const actions = createRawSnippet(() => ({
	render: () => `<span><button title="Rename">R</button><button title="Delete">D</button></span>`
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
});
