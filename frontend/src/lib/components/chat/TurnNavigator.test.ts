import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import TurnNavigator from './TurnNavigator.svelte';

const items = [
	{ id: 'one', label: 'Set up the project' },
	{ id: 'two', label: 'Fix the transcript' },
	{ id: 'three', label: 'Run verification' }
];

afterEach(cleanup);

describe('TurnNavigator', () => {
	it('exposes contextual previous and next controls', async () => {
		const onSelect = vi.fn();
		render(TurnNavigator, { props: { items, currentIndex: 1, onSelect } });

		await fireEvent.click(screen.getByRole('button', { name: 'Previous user message: Set up the project' }));
		expect(onSelect).toHaveBeenLastCalledWith(0);
		await fireEvent.click(screen.getByRole('button', { name: 'Next user message: Run verification' }));
		expect(onSelect).toHaveBeenLastCalledWith(2);
	});

	it('disables unavailable directions at the transcript edge', () => {
		render(TurnNavigator, { props: { items, currentIndex: 0, onSelect: vi.fn() } });
		expect(screen.getByRole('button', { name: 'No previous user message' }).hasAttribute('disabled')).toBe(true);
	});
});
