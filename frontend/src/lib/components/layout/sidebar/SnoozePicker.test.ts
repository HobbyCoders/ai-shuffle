import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import SnoozePicker from './SnoozePicker.svelte';

const local = (y: number, mo: number, d: number, h = 0, mi = 0) => new Date(y, mo - 1, d, h, mi, 0, 0);

afterEach(() => {
	cleanup();
	vi.useRealTimers();
});

describe('SnoozePicker', () => {
	it('lists the presets with the time each one means and picks on click', async () => {
		const onPick = vi.fn();
		render(SnoozePicker, { props: { now: local(2026, 11, 13, 10).getTime(), onPick } });
		const names = screen.getAllByRole('button').map((b) => b.querySelector('.name')?.textContent).filter(Boolean);
		expect(names).toEqual(['Later today', 'This evening', 'Tomorrow', 'Next Monday']);
		await fireEvent.click(screen.getByRole('button', { name: /^Tomorrow, / }));
		expect(onPick).toHaveBeenCalledWith(local(2026, 11, 14, 9));
	});

	it('refuses a past custom time in words and accepts a future one', async () => {
		vi.useFakeTimers({ toFake: ['Date'], now: local(2026, 11, 13, 10) });
		const onPick = vi.fn();
		render(SnoozePicker, { props: { now: Date.now(), count: 2, onPick } });
		expect(screen.getByText('Snooze 2 chats until')).toBeTruthy();
		const field = screen.getByLabelText('Pick a date and time') as HTMLInputElement;
		expect(field.min).toBe('2026-11-13T10:01');

		await fireEvent.input(field, { target: { value: '2026-11-13T09:00' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Snooze' }));
		expect(onPick).not.toHaveBeenCalled();
		expect(screen.getByText('Pick a time in the future.')).toBeTruthy();
		expect(field.getAttribute('aria-invalid')).toBe('true');
		expect(field.getAttribute('aria-describedby')).toBeTruthy();

		await fireEvent.input(field, { target: { value: '2026-11-20T08:15' } });
		await fireEvent.click(screen.getByRole('button', { name: 'Snooze' }));
		expect(onPick).toHaveBeenCalledWith(local(2026, 11, 20, 8, 15));
	});
});
