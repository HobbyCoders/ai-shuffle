import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import BoundedTranscriptRows from './BoundedTranscriptRows.svelte';

const items = Array.from({ length: 2000 }, (_, index) => ({ id: `row-${index}`, content: `Entry ${index}` }));
const keyOf = (item: typeof items[number]) => item.id;
const children = createRawSnippet<[typeof items[number]]>((item) => ({
	render: () => `<p data-testid="row">${item().content}</p>`
}));
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks(); });

describe('bounded nested transcript mounting', () => {
	it('does not page on follow-bottom scroll, but uses its baseline for the next reader scroll', async () => {
		vi.useFakeTimers();
		const scroller = document.createElement('div');
		scroller.style.overflowY = 'auto';
		document.body.append(scroller);
		vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockReturnValue({
			top: 0, bottom: 400, left: 0, right: 400, width: 400, height: 400, x: 0, y: 0, toJSON() {}
		});
		const view = render(BoundedTranscriptRows<typeof items[number]>, {
			target: scroller, props: { items, keyOf, children, startAtTail: true, pagingEnabled: false }
		});
		scroller.scrollTop = 1000;
		await fireEvent.scroll(scroller);
		await vi.advanceTimersByTimeAsync(32);
		expect(screen.getByText('Entry 1999')).toBeTruthy();
		await view.rerender({ pagingEnabled: true });
		scroller.scrollTop = 0;
		await fireEvent.scroll(scroller);
		await vi.advanceTimersByTimeAsync(32);
		expect(screen.getByText('Entry 1920')).toBeTruthy();
		expect(screen.queryByText('Entry 1999')).toBeNull();
		expect(screen.getAllByTestId('row')).toHaveLength(60);
		scroller.remove();
	});

	it('caps mounted rows through repeated older/newer navigation', async () => {
		render(BoundedTranscriptRows<typeof items[number]>, { items, keyOf, children, startAtTail: true });
		expect(screen.getAllByTestId('row')).toHaveLength(60);
		expect(screen.getByText('Entry 1999')).toBeTruthy();
		for (let i = 0; i < 10; i++) {
			await fireEvent.click(screen.getByRole('button', { name: 'Show earlier log entries' }));
			expect(screen.getAllByTestId('row')).toHaveLength(60);
		}
		expect(screen.queryByText('Entry 1999')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: 'Show newer log entries' }));
		expect(screen.getAllByTestId('row')).toHaveLength(60);
	});
	it('reveals a distant target without expanding the mounted budget or re-centering subsequent deltas', async () => {
		const view = render(BoundedTranscriptRows<typeof items[number]>, { items, keyOf, children });
		await view.rerender({ revealKey: 'row-1500' });
		expect(screen.getByText('Entry 1500')).toBeTruthy();
		expect(screen.queryByText('Entry 0')).toBeNull();
		expect(screen.getAllByTestId('row')).toHaveLength(60);
		for (let i = 0; i < 4; i++) await fireEvent.click(screen.getByRole('button', { name: 'Show earlier log entries' }));
		const before = screen.getAllByTestId('row').map(row => row.textContent);
		await view.rerender({ items: [...items] });
		expect(screen.getAllByTestId('row').map(row => row.textContent)).toEqual(before);
	});
	it('keeps a reading window stable when rows arrive at the live tail', async () => {
		const view = render(BoundedTranscriptRows<typeof items[number]>, { items, keyOf, children, startAtTail: true });
		await fireEvent.click(screen.getByRole('button', { name: 'Show earlier log entries' }));
		const before = screen.getAllByTestId('row').map(row => row.textContent);
		await view.rerender({ items: [...items, { id: 'new', content: 'New streaming entry' }] });
		expect(screen.getAllByTestId('row').map(row => row.textContent)).toEqual(before);
	});
});
