import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import WorkerMenuList from './WorkerMenuList.svelte';

afterEach(cleanup);

const rows = [
	{ id: 'local', label: 'Local', online: true, disabled: false, title: 'Run here', checked: true },
	{ id: 'a', label: 'ServerWorker', online: true, disabled: false, title: 'Run on ServerWorker', note: null },
	{ id: 'b', label: 'linux-worker', online: false, disabled: true, title: 'linux-worker is offline', note: 'offline' }
];

describe('WorkerMenuList', () => {
	it('renders one flat row per worker with dot, name and tag', () => {
		const { container } = render(WorkerMenuList, { items: rows, role: 'menuitemradio', onSelect: vi.fn() });
		const items = screen.getAllByRole('menuitemradio');
		expect(items).toHaveLength(3);
		expect(items.map(i => i.querySelector('.worker-status-dot')?.getAttribute('data-status')))
			.toEqual(['online', 'online', 'offline']);
		expect(items[2].querySelector('.worker-row-note')?.textContent).toBe('offline');
		expect(container.querySelector('.worker-row-check')).toBeTruthy();
		expect(items.map(i => i.getAttribute('aria-checked'))).toEqual(['true', 'false', 'false']);
	});

	it('keeps blocked rows focusable, announces their reason and never activates them', async () => {
		const onSelect = vi.fn();
		render(WorkerMenuList, { items: rows, onSelect });
		const blocked = screen.getByRole('menuitem', { name: 'linux-worker offline' }) as HTMLButtonElement;
		// Not natively disabled, so keyboard users can reach it and hear why.
		expect(blocked.disabled).toBe(false);
		blocked.focus();
		expect(document.activeElement).toBe(blocked);
		expect(blocked.getAttribute('aria-disabled')).toBe('true');
		const reason = document.getElementById(blocked.getAttribute('aria-describedby')!);
		expect(reason?.textContent).toBe('linux-worker is offline');
		expect(reason?.hidden).toBe(true);
		expect(blocked.title).toBe('linux-worker is offline');
		expect(blocked.hasAttribute('aria-checked')).toBe(false);
		await fireEvent.click(blocked);
		await fireEvent.keyDown(blocked, { key: 'Enter' });
		await fireEvent.keyDown(blocked, { key: ' ' });
		expect(onSelect).not.toHaveBeenCalled();
		// Rows that are not blocked carry no reason.
		const unraid = screen.getByRole('menuitem', { name: 'ServerWorker' });
		expect(unraid.hasAttribute('aria-describedby')).toBe(false);
		await fireEvent.click(unraid);
		expect(onSelect).toHaveBeenCalledExactlyOnceWith(rows[1]);
	});

	it('locks every row while a move is running', () => {
		render(WorkerMenuList, { items: rows, locked: true, onSelect: vi.fn() });
		for (const item of screen.getAllByRole('menuitem')) expect((item as HTMLButtonElement).disabled).toBe(true);
	});

	it('shows loading, error with retry, and empty status rows', async () => {
		const onRetry = vi.fn();
		const view = render(WorkerMenuList, { items: [], loading: true, onSelect: vi.fn() });
		expect(screen.getByRole('status').textContent).toContain('Loading');
		await view.rerender({ items: [], loading: false, error: 'Could not load workers. Try again.', onRetry, onSelect: vi.fn() });
		expect(screen.getByRole('alert').textContent).toBe('Could not load workers. Try again.');
		await fireEvent.click(screen.getByRole('menuitem', { name: 'Retry' }));
		expect(onRetry).toHaveBeenCalledOnce();
		await view.rerender({ items: [], error: null, emptyText: 'No other workers available.', onSelect: vi.fn() });
		expect(screen.getByRole('status').textContent).toBe('No other workers available.');
	});
});
