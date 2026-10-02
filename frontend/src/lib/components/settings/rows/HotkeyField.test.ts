import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import HotkeyField from './HotkeyField.svelte';

afterEach(cleanup);

const value = { key: 's', cmdOrCtrl: true, shift: true, alt: false };

describe('HotkeyField', () => {
	it('shows the shortcut as key caps', () => {
		const { container } = render(HotkeyField, { value, onchange: vi.fn(), label: 'Voice input hotkey' });
		const caps = [...container.querySelectorAll('kbd span')].map((s) => s.textContent);
		expect(caps).toEqual(['Ctrl', 'Shift', 'S']);
	});

	it('captures a combination with a modifier and ignores bare letters', async () => {
		const onchange = vi.fn();
		render(HotkeyField, { value, onchange, label: 'Hotkey' });
		const button = screen.getByRole('button');
		await fireEvent.click(button);
		await fireEvent.keyDown(button, { key: 'k' });
		expect(onchange).not.toHaveBeenCalled();
		expect(button.textContent).toContain('Add Ctrl, Alt or Cmd');
		await fireEvent.keyDown(button, { key: 'Control', ctrlKey: true });
		expect(onchange).not.toHaveBeenCalled();
		await fireEvent.keyDown(button, { key: 'k', ctrlKey: true });
		expect(onchange).toHaveBeenCalledWith({ key: 'k', cmdOrCtrl: true, shift: false, alt: false });
	});

	it('takes function keys alone and cancels on Escape', async () => {
		const onchange = vi.fn();
		render(HotkeyField, { value, onchange, label: 'Hotkey' });
		const button = screen.getByRole('button');
		await fireEvent.click(button);
		await fireEvent.keyDown(button, { key: 'Escape' });
		expect(button.querySelector('kbd')).toBeTruthy();
		await fireEvent.click(button);
		await fireEvent.keyDown(button, { key: 'F9' });
		expect(onchange).toHaveBeenCalledWith({ key: 'f9', cmdOrCtrl: false, shift: false, alt: false });
	});

	it('is disabled when asked', () => {
		render(HotkeyField, { value, onchange: vi.fn(), label: 'Hotkey', disabled: true });
		expect((screen.getByRole('button') as HTMLButtonElement).disabled).toBe(true);
	});
});
