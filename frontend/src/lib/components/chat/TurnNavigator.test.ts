import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import TurnNavigator from './TurnNavigator.svelte';

const items = [
	{ id: 'one', label: 'Set up the project', title: 'Set up the project', preview: '' },
	{
		id: 'two',
		label: 'Fix the transcript',
		title: 'Fix the transcript',
		preview: 'Details about the bug'
	},
	{ id: 'three', label: 'Run verification', title: 'Run verification', preview: '' }
];

afterEach(cleanup);

describe('TurnNavigator', () => {
	it('keeps every long-history target available through bounded controls', async () => {
		const onSelect = vi.fn();
		const many = Array.from({ length: 1000 }, (_, i) => ({ id: String(i), label: `Turn ${i}`, title: `Turn ${i}`, preview: '' }));
		render(TurnNavigator, { props: { items: many, currentIndex: 500, onSelect } });
		expect(screen.getAllByRole('option')).toHaveLength(1000);
		expect(screen.getAllByRole('button')).toHaveLength(4);
		await fireEvent.click(screen.getByRole('button', { name: 'First user turn' }));
		expect(onSelect).toHaveBeenLastCalledWith(0);
		await fireEvent.click(screen.getByRole('button', { name: 'Last user turn' }));
		expect(onSelect).toHaveBeenLastCalledWith(999);
		await fireEvent.change(screen.getByRole('combobox'), { target: { value: '123' } });
		expect(onSelect).toHaveBeenLastCalledWith(123);
		onSelect.mockClear();
		await fireEvent.keyDown(screen.getByRole('combobox'), { key: 'ArrowDown' });
		expect(onSelect).not.toHaveBeenCalled();
	});
	it('selects a turn from the timeline', async () => {
		const onSelect = vi.fn();
		render(TurnNavigator, { props: { items, currentIndex: 1, onSelect } });

		await fireEvent.click(screen.getByRole('button', { name: 'User message 3 of 3: Run verification' }));
		expect(onSelect).toHaveBeenCalledWith(2);
	});

	it('marks the current turn and exposes the hovered preview', () => {
		render(TurnNavigator, { props: { items, currentIndex: 0, onSelect: vi.fn() } });
		expect(
			screen.getByRole('button', { name: 'User message 1 of 3: Set up the project' }).getAttribute('aria-current')
		).toBe('true');
		expect(screen.getByText('Details about the bug')).toBeTruthy();
	});

	it('moves a ripple between neighboring lines and resets when the cursor leaves', async () => {
		render(TurnNavigator, { props: { items, currentIndex: 0, onSelect: vi.fn() } });
		const ticks = screen.getAllByRole('button');
		const proximity = () => ticks.map((tick) => Number(tick.style.getPropertyValue('--proximity')));
		expect(proximity()).toEqual([0, 0, 0]);

		await fireEvent.mouseEnter(ticks[1]);
		expect(proximity()[1]).toBe(1);
		expect(proximity()[0]).toBeCloseTo(2 / 3);
		expect(proximity()[2]).toBeCloseTo(2 / 3);

		// Crossing the gap should not snap the ripple back to its resting position.
		await fireEvent.mouseLeave(ticks[1]);
		expect(proximity()[1]).toBe(1);
		await fireEvent.mouseEnter(ticks[2]);
		expect(proximity()[2]).toBe(1);
		expect(proximity()[0]).toBeCloseTo(1 / 3);

		await fireEvent.mouseLeave(screen.getByRole('navigation'));
		expect(proximity()).toEqual([0, 0, 0]);
	});

	it('shows the same ripple and preview for keyboard focus', async () => {
		render(TurnNavigator, { props: { items, currentIndex: 0, onSelect: vi.fn() } });
		const tick = screen.getAllByRole('button')[1];
		await fireEvent.focus(tick);
		expect(tick.classList.contains('is-hovered')).toBe(true);
		expect(tick.style.getPropertyValue('--proximity')).toBe('1');
		await fireEvent.blur(tick);
		expect(tick.classList.contains('is-hovered')).toBe(false);
		expect(tick.style.getPropertyValue('--proximity')).toBe('0');
	});

	it('moves with the arrow keys while focused', async () => {
		const onSelect = vi.fn();
		render(TurnNavigator, { props: { items, currentIndex: 1, onSelect } });
		const current = screen.getByRole('button', { name: 'User message 2 of 3: Fix the transcript' });
		await fireEvent.keyDown(current, { key: 'ArrowDown' });
		expect(onSelect).toHaveBeenCalledWith(2);
		await fireEvent.keyDown(current, { key: 'ArrowUp' });
		expect(onSelect).toHaveBeenCalledWith(0);
	});
});
