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
		expect(onApply).toHaveBeenCalledWith({ index: 0 });
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
