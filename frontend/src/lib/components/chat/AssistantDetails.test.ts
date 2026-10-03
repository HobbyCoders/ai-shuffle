import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import AssistantDetails from './AssistantDetails.svelte';
import type { ChatMessage } from '$lib/stores/types';

const row = createRawSnippet((message: () => ChatMessage) => ({
	render: () => `<div data-testid="row">${message().id}</div>`
}));
const entries = ['k1', 't1', 't2'].map((id, index) => ({ message: { id, role: 'assistant', content: '', type: 'tool_use' } as ChatMessage, index }));

afterEach(cleanup);

describe('AssistantDetails', () => {
	it('starts collapsed with a short label and the step count', () => {
		render(AssistantDetails, { props: { entries, row } });
		const toggle = screen.getByRole('button', { name: /Details/ });
		expect(toggle.getAttribute('aria-expanded')).toBe('false');
		expect(toggle.textContent).toContain('3 steps');
		expect(screen.queryAllByTestId('row')).toHaveLength(0);
	});

	it('opens to the rows in order and closes again', async () => {
		render(AssistantDetails, { props: { entries, row } });
		const toggle = screen.getByRole('button', { name: /Details/ });
		await fireEvent.click(toggle);
		expect(toggle.getAttribute('aria-expanded')).toBe('true');
		expect(screen.getAllByTestId('row').map((el) => el.textContent)).toEqual(['k1', 't1', 't2']);
		await fireEvent.click(toggle);
		expect(screen.queryAllByTestId('row')).toHaveLength(0);
	});

	it('says when the turn is still working', () => {
		render(AssistantDetails, { props: { entries: entries.slice(0, 1), live: true, row } });
		const toggle = screen.getByRole('button', { name: /Details/ });
		expect(toggle.textContent).toContain('1 step');
		expect(toggle.textContent).toContain('still working');
	});
});
