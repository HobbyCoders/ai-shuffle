// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import BrowserPicker from './BrowserPicker.svelte';
import type { BrowserEditor } from '$lib/browser-panel/workspace';
import type { BrowserChoices } from '$lib/browser-panel/pickers';

afterEach(cleanup);

const select: BrowserEditor = { id: 'sel-1', type: 'select', label: 'Country', value: 'Belgium', x: 10, y: 20, width: 160, height: 28 };
const choices: BrowserChoices = { selected: 1, groups: [], options: [{ label: 'Austria', disabled: false }, { label: 'Belgium', disabled: false }] };

function setup(field: BrowserEditor, loadChoices?: (f: BrowserEditor) => Promise<BrowserChoices>, view = { width: 800, height: 600 }) {
	const boundary = document.createElement('div');
	document.body.append(boundary);
	const onApply = vi.fn();
	const onCancel = vi.fn();
	render(BrowserPicker, {
		target: boundary,
		props: { field, anchor: { left: 10, top: 20, width: 160, height: 28 }, viewWidth: view.width, viewHeight: view.height, boundary, loadChoices, onApply, onCancel },
	});
	return { boundary, onApply, onCancel };
}

describe('BrowserPicker', () => {
	it('loads a select’s options (skeleton meanwhile) and applies the chosen index', async () => {
		let resolve!: (c: BrowserChoices) => void;
		const load = vi.fn(() => new Promise<BrowserChoices>((r) => (resolve = r)));
		const { onApply } = setup(select, load);
		expect(load).toHaveBeenCalledWith(select);
		expect(screen.getByRole('dialog', { name: 'List: Country' })).toBeTruthy();
		expect(screen.getByRole('status', { name: 'Loading options' })).toBeTruthy();
		resolve(choices);
		await fireEvent.click(await screen.findByText('Austria'));
		// The label it showed goes along: a list the page refilled since is refused.
		expect(onApply).toHaveBeenCalledWith({ index: 0, label: 'Austria' });
	});

	it('shows a refused choice in place, with the list closed until it is read again', async () => {
		const load = vi.fn().mockResolvedValue(choices);
		const { onApply } = setup(select, load);
		onApply.mockRejectedValueOnce(new Error('This list changed. Open it again.'));
		await fireEvent.click(await screen.findByText('Austria'));
		expect((await screen.findByRole('alert')).textContent).toContain('This list changed');
		expect(screen.queryByRole('listbox')).toBeNull(); // the stale list is not offered
		await fireEvent.click(screen.getByRole('button', { name: /Open the list again/ }));
		expect(load).toHaveBeenCalledTimes(2);
		await fireEvent.click(await screen.findByText('Belgium'));
		expect(onApply).toHaveBeenLastCalledWith({ index: 1, label: 'Belgium' });
		expect(screen.queryByRole('alert')).toBeNull();
	});

	it('waits for the page to take a date, one choice at a time, and keeps the picker on a refusal', async () => {
		let reject!: (e: Error) => void;
		const { onApply } = setup({ ...select, type: 'date', label: 'Arrival', value: '2026-10-21' });
		onApply.mockImplementationOnce(() => new Promise<void>((_, r) => (reject = r)));
		await fireEvent.click(screen.getByRole('gridcell', { name: /October 24, 2026/ }));
		const body = document.querySelector('.bp-body')!;
		expect(body.getAttribute('aria-busy')).toBe('true');
		await fireEvent.click(screen.getByRole('gridcell', { name: /October 25, 2026/ }));
		expect(onApply).toHaveBeenCalledTimes(1);
		reject(new Error("The page didn't accept that value."));
		expect((await screen.findByRole('alert')).textContent).toContain("didn't accept");
		expect(body.getAttribute('aria-busy')).toBe('false');
		await fireEvent.click(screen.getByRole('gridcell', { name: /October 25, 2026/ }));
		expect(onApply).toHaveBeenLastCalledWith({ value: '2026-10-25' });
	});

	it('offers only on-step days counted from the value attribute', () => {
		setup({ ...select, type: 'date', label: 'Weekly', value: '2026-10-21', step: '7', base: '2026-10-21' });
		expect(screen.getByRole('gridcell', { name: /October 28, 2026/ }).getAttribute('aria-disabled')).toBe('false');
		expect(screen.getByRole('gridcell', { name: /October 22, 2026/ }).getAttribute('aria-disabled')).toBe('true');
	});

	it('marks today by the page’s clock and says whose clock it is', () => {
		vi.useFakeTimers({ toFake: ['Date'] });
		try {
			vi.setSystemTime(new Date(Date.UTC(2026, 9, 21, 20, 0)));
			setup({ ...select, type: 'date', label: 'Arrival', value: '', zone: 'Pacific/Kiritimati' });
			expect(screen.getByRole('gridcell', { name: /October 22, 2026/ }).getAttribute('aria-current')).toBe('date');
			expect(screen.getByText('Page time zone: Pacific/Kiritimati')).toBeTruthy();
		} finally {
			vi.useRealTimers();
		}
	});

	it('shows a failed lookup with a retry', async () => {
		const load = vi.fn().mockRejectedValueOnce(new Error('This list changed. Tap it again.')).mockResolvedValueOnce(choices);
		setup(select, load);
		expect((await screen.findByRole('alert')).textContent).toContain('This list changed');
		await fireEvent.click(screen.getByRole('button', { name: /Try again/ }));
		expect(await screen.findByRole('listbox', { name: 'Country' })).toBeTruthy();
		expect(load).toHaveBeenCalledTimes(2);
	});

	it('opens a calendar for a date field and applies the value', async () => {
		const { onApply } = setup({ ...select, type: 'date', label: 'Arrival', value: '2026-10-21', max: '2026-10-25' });
		expect(screen.getByRole('dialog', { name: 'Date: Arrival' })).toBeTruthy();
		expect(screen.getByRole('gridcell', { name: /October 26, 2026/ }).getAttribute('aria-disabled')).toBe('true');
		await fireEvent.click(screen.getByRole('gridcell', { name: /October 24, 2026/ }));
		expect(onApply).toHaveBeenCalledWith({ value: '2026-10-24' });
	});

	it('cancels on Escape, the close button, a press on the screencast and a press outside the view', async () => {
		const { boundary, onCancel } = setup({ ...select, type: 'time', label: 'At', value: '10:00' });
		await fireEvent.keyDown(screen.getByRole('listbox', { name: 'Hours' }), { key: 'Escape' });
		expect(onCancel).toHaveBeenCalledTimes(1);
		await fireEvent.click(screen.getByRole('button', { name: 'Close without changing' }));
		expect(onCancel).toHaveBeenCalledTimes(2);
		const scrim = boundary.querySelector('.bp-scrim')!;
		const press = new PointerEvent('pointerdown', { bubbles: true, cancelable: true });
		scrim.dispatchEvent(press);
		expect(press.defaultPrevented).toBe(true); // never reaches the page
		expect(onCancel).toHaveBeenCalledTimes(3);
		await fireEvent.pointerDown(document.body);
		expect(onCancel).toHaveBeenCalledTimes(4);
		// A press inside the popup itself is not a cancel.
		await fireEvent.pointerDown(screen.getByRole('dialog'));
		expect(onCancel).toHaveBeenCalledTimes(4);
	});

	it('becomes a bottom sheet in a narrow view', async () => {
		const { boundary } = setup({ ...select, type: 'month', label: 'Card expiry', value: '' }, undefined, { width: 390, height: 700 });
		const dialog = screen.getByRole('dialog', { name: 'Month: Card expiry' });
		expect(dialog.classList.contains('sheet')).toBe(true);
		expect(dialog.getAttribute('style') ?? '').toBe('');
		expect(boundary.querySelector('.bp-scrim.sheet')).toBeTruthy();
		await waitFor(() => expect(document.activeElement?.getAttribute('data-month')).not.toBeNull());
	});
});
