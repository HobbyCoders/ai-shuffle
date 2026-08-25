import { render, fireEvent } from '@testing-library/svelte';
import { describe, expect, it } from 'vitest';
import NotesList from './NotesList.svelte';

describe('NotesList', () => {
	it('renders every note as a current thought, not a completed todo', () => {
		const { container, getByText } = render(NotesList, {
			props: { notes: ['first thought', 'second thought', 'newest'] }
		});

		expect(getByText('first thought')).toBeTruthy();
		expect(getByText('second thought')).toBeTruthy();
		expect(getByText('newest')).toBeTruthy();
		expect(container.querySelector('.todo')).toBeNull();
		expect(container.querySelector('.wlbar')).toBeNull();
		expect(container.querySelector('.is-done')).toBeNull();
		expect(container.textContent).not.toMatch(/\d+\/\d+/);
		expect(container.querySelector('.notes-count')?.textContent).toBe('3');
	});

	it('collapses to a preview of the newest note', async () => {
		const { container, getByText, queryByText } = render(NotesList, {
			props: { notes: ['older', 'newest'], initialExpanded: true }
		});

		const header = container.querySelector<HTMLButtonElement>('.notes-row');
		expect(header?.getAttribute('aria-expanded')).toBe('true');
		await fireEvent.click(header!);

		expect(header?.getAttribute('aria-expanded')).toBe('false');
		expect(queryByText('older')).toBeNull();
		expect(getByText('newest')).toBeTruthy();
	});
});
