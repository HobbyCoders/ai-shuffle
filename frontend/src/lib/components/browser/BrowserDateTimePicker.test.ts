// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { tick } from 'svelte';
import BrowserDateTimePicker from './BrowserDateTimePicker.svelte';
import { limitsFor, type DateKind } from '$lib/browser-panel/pickers';

afterEach(cleanup);

// Wednesday 21 October 2026, 09:07 local time.
const now = new Date(2026, 9, 21, 9, 7, 12);

function setup(kind: DateKind, value = '', min?: string, max?: string, step?: string) {
	const onChoose = vi.fn();
	const onCancel = vi.fn();
	render(BrowserDateTimePicker, { kind, value, limits: limitsFor(kind, min, max, step), label: 'When', locale: 'en-US', now, onChoose, onCancel });
	return { onChoose, onCancel };
}
const day = (label: string) => screen.getByRole('gridcell', { name: label });

describe('BrowserDateTimePicker — date', () => {
	it('opens on the value, disables days outside min/max/step and applies a click', async () => {
		const { onChoose } = setup('date', '2026-10-12', '2026-10-05', '2026-10-26', '7');
		await tick();
		expect(screen.getByRole('button', { name: /October 2026/ })).toBeTruthy();
		const selected = day('Monday, October 12, 2026');
		expect(selected.getAttribute('aria-selected')).toBe('true');
		expect(document.activeElement).toBe(selected);
		expect(day('Tuesday, October 13, 2026').getAttribute('aria-disabled')).toBe('true');
		expect(day('Sunday, October 4, 2026').getAttribute('aria-disabled')).toBe('true');
		expect(day('Monday, October 26, 2026').getAttribute('aria-disabled')).toBe('false');
		await fireEvent.click(day('Tuesday, October 13, 2026'));
		expect(onChoose).not.toHaveBeenCalled();
		await fireEvent.click(day('Monday, October 19, 2026'));
		expect(onChoose).toHaveBeenCalledWith('2026-10-19');
	});

	it('moves by keyboard across months and chooses with Enter; Escape cancels', async () => {
		const { onChoose, onCancel } = setup('date', '2026-10-31');
		await fireEvent.keyDown(day('Saturday, October 31, 2026'), { key: 'ArrowRight' });
		await tick();
		expect(screen.getByRole('button', { name: /November 2026/ })).toBeTruthy();
		expect(document.activeElement?.getAttribute('aria-label')).toBe('Sunday, November 1, 2026');
		await fireEvent.keyDown(document.activeElement!, { key: 'PageUp' });
		await tick();
		expect(document.activeElement?.getAttribute('aria-label')).toBe('Thursday, October 1, 2026');
		await fireEvent.keyDown(document.activeElement!, { key: 'Enter' });
		expect(onChoose).toHaveBeenCalledWith('2026-10-01');
		await fireEvent.keyDown(document.activeElement!, { key: 'Escape' });
		expect(onCancel).toHaveBeenCalled();
	});

	it('jumps months from the title, offers Today and Clear', async () => {
		const { onChoose } = setup('date', '', '2026-01-01', '2026-12-31');
		expect(day('Wednesday, October 21, 2026').getAttribute('aria-current')).toBe('date');
		await fireEvent.click(screen.getByRole('button', { name: /choose a month/ }));
		await fireEvent.click(screen.getByRole('gridcell', { name: 'Mar' }));
		expect(screen.getByRole('button', { name: /March 2026/ })).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Today' }));
		expect(onChoose).toHaveBeenLastCalledWith('2026-10-21');
		await fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
		expect(onChoose).toHaveBeenLastCalledWith('');
	});

	it('disables Today when today is out of range', () => {
		setup('date', '', '2027-01-01');
		expect((screen.getByRole('button', { name: 'Today' }) as HTMLButtonElement).disabled).toBe(true);
	});
});

describe('BrowserDateTimePicker — month and week', () => {
	it('picks a month from a year grid with min/max', async () => {
		const { onChoose } = setup('month', '2026-03', '2026-02', '2026-11');
		expect(screen.getByRole('gridcell', { name: 'Mar' }).getAttribute('aria-selected')).toBe('true');
		expect(screen.getByRole('gridcell', { name: 'Jan' }).getAttribute('aria-disabled')).toBe('true');
		expect(screen.getByRole('gridcell', { name: 'Dec' }).getAttribute('aria-disabled')).toBe('true');
		await fireEvent.click(screen.getByRole('gridcell', { name: 'Jun' }));
		expect(onChoose).toHaveBeenCalledWith('2026-06');
	});

	it('picks an ISO week, Monday first, with week numbers', async () => {
		const { onChoose } = setup('week', '2026-W43');
		expect(screen.getAllByRole('columnheader')[1].getAttribute('aria-label')).toBe('Monday');
		expect(day('Week 43, Wednesday, October 21, 2026').getAttribute('aria-selected')).toBe('true');
		await fireEvent.click(day('Week 44, Thursday, October 29, 2026'));
		expect(onChoose).toHaveBeenCalledWith('2026-W44');
	});
});

describe('BrowserDateTimePicker — time and date-and-time', () => {
	it('lists only on-step minutes, disables out-of-range hours and sets the value', async () => {
		const { onChoose } = setup('time', '10:45', '09:00', '17:30', '900');
		const minutes = screen.getByRole('listbox', { name: 'Minutes' });
		expect([...minutes.querySelectorAll('[role=option]')].map((o) => o.textContent?.trim())).toEqual(['00', '15', '30', '45']);
		const hours = screen.getByRole('listbox', { name: 'Hours' });
		const hour = (label: string) => [...hours.querySelectorAll('[role=option]')].find((o) => o.textContent?.trim() === label)!;
		expect(hour('8 AM').getAttribute('aria-disabled')).toBe('true');
		expect(hour('10 AM').getAttribute('aria-selected')).toBe('true');
		expect(screen.getByText('10:45 AM')).toBeTruthy(); // the readout
		// 17:45 is past max: choosing 5 PM keeps the hour and moves the minutes.
		await fireEvent.click(hour('5 PM'));
		expect(screen.getByText('5:30 PM')).toBeTruthy();
		const minute45 = [...minutes.querySelectorAll('[role=option]')].find((o) => o.textContent?.trim() === '45')!;
		expect(minute45.getAttribute('aria-disabled')).toBe('true');
		await fireEvent.click(minute45);
		expect(screen.getByText('5:30 PM')).toBeTruthy();
		await fireEvent.click(screen.getByRole('button', { name: 'Set' }));
		expect(onChoose).toHaveBeenCalledWith('17:30');
	});

	it('steps through enabled values by keyboard and shows seconds when the step needs them', async () => {
		const { onChoose } = setup('time', '08:00:00', undefined, undefined, '30');
		const seconds = screen.getByRole('listbox', { name: 'Seconds' });
		expect([...seconds.querySelectorAll('[role=option]')].map((o) => o.textContent?.trim())).toEqual(['00', '30']);
		await fireEvent.keyDown(seconds, { key: 'ArrowDown' });
		await fireEvent.keyDown(seconds, { key: 'Enter' });
		expect(onChoose).toHaveBeenCalledWith('08:00:30');
	});

	it('needs a day and a time for date-and-time, then sets both', async () => {
		const { onChoose } = setup('datetime-local', '', '2026-10-21T10:00', '2026-10-30T18:00');
		expect(day('Tuesday, October 20, 2026').getAttribute('aria-disabled')).toBe('true');
		expect((screen.getByRole('button', { name: 'Set' }) as HTMLButtonElement).disabled).toBe(true);
		await fireEvent.click(day('Friday, October 23, 2026'));
		const hours = screen.getByRole('listbox', { name: 'Hours' });
		await fireEvent.click([...hours.querySelectorAll('[role=option]')].find((o) => o.textContent?.trim() === '2 PM')!);
		await fireEvent.click(screen.getByRole('button', { name: 'Set' }));
		// The time started at the first allowed one (10:00 on the 21st), then the hour moved.
		expect(screen.getByText('Fri, Oct 23, 2026 · 2:00 PM')).toBeTruthy();
		expect(onChoose).toHaveBeenCalledWith('2026-10-23T14:00');
	});
});
