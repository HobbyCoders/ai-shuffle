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
		expect(screen.queryByRole('combobox')).toBeNull();
		expect(screen.getAllByRole('button')).toHaveLength(14);
		await fireEvent.click(screen.getByRole('button', { name: 'First user turn' }));
		expect(onSelect).toHaveBeenLastCalledWith(0);
		await fireEvent.click(screen.getByRole('button', { name: 'Last user turn' }));
		expect(onSelect).toHaveBeenLastCalledWith(999);
		await fireEvent.click(screen.getByRole('button', { name: /Browse session timeline/ }));
		const search = screen.getByRole('searchbox');
		expect(document.activeElement).toBe(search);
		await fireEvent.input(search, { target: { value: '124' } });
		await fireEvent.click(screen.getByRole('button', { name: '124 Turn 123' }));
		expect(onSelect).toHaveBeenLastCalledWith(123);
		expect(screen.queryByRole('searchbox')).toBeNull();
		expect(document.activeElement).toBe(screen.getByRole('button', { name: /Browse session timeline/ }));
	});

	it('searches previews, handles empty results, and dismisses without navigating', async () => {
		const onSelect = vi.fn();
		const many = Array.from({ length: 100 }, (_, i) => ({ id: String(i), label: `Prompt ${i}`, title: `Prompt ${i}`, preview: i === 75 ? 'A unique detail' : '' }));
		render(TurnNavigator, { props: { items: many, currentIndex: 50, onSelect } });
		const trigger = screen.getByRole('button', { name: /Browse session timeline/ });
		await fireEvent.click(trigger);
		await fireEvent.input(screen.getByRole('searchbox'), { target: { value: 'unique detail' } });
		expect(screen.getByRole('button', { name: '76 Prompt 75 A unique detail' })).toBeTruthy();
		await fireEvent.input(screen.getByRole('searchbox'), { target: { value: 'no such message' } });
		expect(screen.getByText('No messages found.')).toBeTruthy();
		await fireEvent.keyDown(screen.getByRole('searchbox'), { key: 'Escape' });
		expect(screen.queryByRole('searchbox')).toBeNull();
		expect(document.activeElement).toBe(trigger);
		await fireEvent.click(trigger);
		await fireEvent.pointerDown(document.body);
		expect(screen.queryByRole('searchbox')).toBeNull();
		expect(onSelect).not.toHaveBeenCalled();
	});

	it('keeps nearby targets and endpoint controls in bounds as the reading position changes', async () => {
		const onSelect = vi.fn();
		const many = Array.from({ length: 45 }, (_, i) => ({ id: String(i), label: `Prompt ${i}`, title: `Prompt ${i}`, preview: '' }));
		const view = render(TurnNavigator, { props: { items: many, currentIndex: 0, onSelect } });
		expect((screen.getByRole('button', { name: 'Previous user turn' }) as HTMLButtonElement).disabled).toBe(true);
		expect(screen.getByRole('button', { name: 'User message 1 of 45: Prompt 0' }).getAttribute('aria-current')).toBe('true');
		await view.rerender({ items: many, currentIndex: 44, onSelect });
		expect((screen.getByRole('button', { name: 'Next user turn' }) as HTMLButtonElement).disabled).toBe(true);
		expect(screen.getByRole('button', { name: 'User message 45 of 45: Prompt 44' }).getAttribute('aria-current')).toBe('true');
		const trigger = screen.getByRole('button', { name: /Browse session timeline/ });
		await fireEvent.keyDown(trigger, { key: 'Home' });
		expect(onSelect).toHaveBeenLastCalledWith(0);
		await fireEvent.keyDown(trigger, { key: 'ArrowUp' });
		expect(onSelect).toHaveBeenLastCalledWith(43);
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
