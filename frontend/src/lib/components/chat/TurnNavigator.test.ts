import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import TurnNavigator from './TurnNavigator.svelte';

const items = [
	{ id: 'one', label: 'Set up the project', title: 'Set up the project', preview: '', weight: 0.4 },
	{
		id: 'two',
		label: 'Fix the transcript',
		title: 'Fix the transcript',
		preview: 'Details about the bug',
		weight: 0.7
	},
	{ id: 'three', label: 'Run verification', title: 'Run verification', preview: '', weight: 0.5 }
];

afterEach(cleanup);

describe('TurnNavigator', () => {
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
