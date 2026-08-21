import { afterEach, describe, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import { createRawSnippet } from 'svelte';
import type { ChatMessage } from '$lib/stores/types';
import WorkLogGroup from './WorkLogGroup.svelte';

const message = {
	id: 'tool-1',
	role: 'assistant',
	type: 'tool_use',
	content: '',
	toolName: 'Read',
	toolStatus: 'complete'
} as ChatMessage;

const row = createRawSnippet<[ChatMessage, number]>((getMessage) => ({
	render: () => `<div data-testid="work-row">${getMessage().content || getMessage().toolName}</div>`
}));

const baseProps = {
	folded: [{ message, index: 0 }],
	tail: [],
	durationLabel: null,
	summary: 'Read 1 file',
	row
};

afterEach(cleanup);

describe('WorkLogGroup', () => {
	it('uses action text for a collapsed live group', () => {
		render(WorkLogGroup, { props: { ...baseProps, mode: 'live' } });
		expect(screen.getByRole('button', { name: /Show work details: Read 1 file/i })).toBeTruthy();
	});

	it('closes an inspected live group when it settles', async () => {
		const view = render(WorkLogGroup, { props: { ...baseProps, mode: 'live' as const } });
		await fireEvent.click(screen.getByRole('button', { name: /Show work details/i }));
		expect(screen.getByTestId('work-row')).toBeTruthy();

		await view.rerender({ ...baseProps, mode: 'settled', durationLabel: 'Worked for 4s' });
		expect(screen.queryByTestId('work-row')).toBeNull();
		expect(screen.getByRole('button', { name: /Show work details: Worked for 4s, Read 1 file/i })).toBeTruthy();
	});

	it('keeps the terminal answer visible while settled work is collapsed', async () => {
		const answer = {
			id: 'answer-1',
			role: 'assistant',
			type: 'text',
			content: 'Final answer'
		} as ChatMessage;
		render(WorkLogGroup, {
			props: {
				...baseProps,
				mode: 'settled',
				durationLabel: 'Worked for 4s',
				sequence: [
					{ message, index: 0, hiddenWhenCollapsed: true },
					{ message: answer, index: 1, hiddenWhenCollapsed: false }
				]
			}
		});

		expect(screen.getByText('Final answer')).toBeTruthy();
		expect(screen.queryByText('Read')).toBeNull();
		await fireEvent.click(screen.getByRole('button', { name: /Show work details/i }));
		expect(screen.getByText('Read')).toBeTruthy();
		expect(screen.getByText('Final answer')).toBeTruthy();
	});
});
