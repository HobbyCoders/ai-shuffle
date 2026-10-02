import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import Switch from './Switch.svelte';
import Segmented from './Segmented.svelte';
import SelectField from './SelectField.svelte';
import SettingRowHarness from '$lib/test-stubs/SettingRowHarness.svelte';

afterEach(cleanup);

describe('Switch', () => {
	it('is a labelled role=switch that toggles aria-checked and reports the new value', async () => {
		const onchange = vi.fn();
		render(Switch, { checked: false, onchange, label: 'Sound cues' });
		const sw = screen.getByRole('switch', { name: 'Sound cues' });
		expect(sw.getAttribute('aria-checked')).toBe('false');
		await fireEvent.click(sw);
		expect(sw.getAttribute('aria-checked')).toBe('true');
		expect(onchange).toHaveBeenLastCalledWith(true);
		await fireEvent.click(sw);
		expect(onchange).toHaveBeenLastCalledWith(false);
	});

	it('does nothing while disabled', async () => {
		const onchange = vi.fn();
		render(Switch, { checked: true, onchange, disabled: true, label: 'Cues' });
		const sw = screen.getByRole('switch', { name: 'Cues' }) as HTMLButtonElement;
		expect(sw.disabled).toBe(true);
		await fireEvent.click(sw);
		expect(onchange).not.toHaveBeenCalled();
		expect(sw.getAttribute('aria-checked')).toBe('true');
	});

	it('has a small size', () => {
		render(Switch, { checked: false, size: 'sm', label: 'On' });
		expect(screen.getByRole('switch').classList.contains('sm')).toBe(true);
	});

	it('takes its name and description from the row it sits in', () => {
		render(SettingRowHarness, { label: 'Group tool calls', hint: 'Fewer lines' });
		const sw = screen.getByRole('switch', { name: 'Group tool calls' });
		const described = sw.getAttribute('aria-describedby');
		expect(described).toBeTruthy();
		expect(document.getElementById(described!)?.textContent).toBe('Fewer lines');
	});

	it('is disabled when Settings is read-only', () => {
		render(SettingRowHarness, { label: 'Group tool calls', readOnly: true });
		expect((screen.getByRole('switch') as HTMLButtonElement).disabled).toBe(true);
	});
});

describe('a failed save puts the control back', () => {
	it('Switch flips back when onchange resolves false', async () => {
		render(Switch, { checked: false, onchange: () => Promise.resolve(false), label: 'Cues' });
		const sw = screen.getByRole('switch', { name: 'Cues' });
		await fireEvent.click(sw);
		await waitFor(() => expect(sw.getAttribute('aria-checked')).toBe('false'));
	});

	it('Switch flips back when onchange rejects, and keeps a plain return value', async () => {
		render(Switch, { checked: false, onchange: () => Promise.reject(new Error('no')), label: 'A' });
		const a = screen.getByRole('switch', { name: 'A' });
		await fireEvent.click(a);
		await waitFor(() => expect(a.getAttribute('aria-checked')).toBe('false'));

		let on = true;
		render(Switch, { checked: on, onchange: (v: boolean) => (on = v), label: 'B' });
		const b = screen.getByRole('switch', { name: 'B' });
		await fireEvent.click(b);
		await Promise.resolve();
		expect(b.getAttribute('aria-checked')).toBe('false');
		expect(on).toBe(false);
	});

	it('Segmented restores the previous choice', async () => {
		render(Segmented, { options: ['Light', 'Dark'], value: 'Light', onchange: () => Promise.resolve(false), label: 'Theme' });
		await fireEvent.click(screen.getByRole('button', { name: 'Dark' }));
		await waitFor(() => expect(screen.getByRole('button', { name: 'Light' }).getAttribute('aria-pressed')).toBe('true'));
	});

	it('SelectField restores the previous value', async () => {
		render(SelectField, { options: ['a', 'b'], value: 'a', onchange: () => Promise.resolve(false), label: 'Pick' });
		const select = screen.getByRole('combobox', { name: 'Pick' }) as HTMLSelectElement;
		await fireEvent.change(select, { target: { value: 'b' } });
		await waitFor(() => expect(select.value).toBe('a'));
	});
});
