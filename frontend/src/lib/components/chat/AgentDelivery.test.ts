import { afterEach, expect, it } from 'vitest';
import { cleanup, fireEvent, render, screen } from '@testing-library/svelte';
import ToolUseMessage from './ToolUseMessage.svelte';
afterEach(cleanup);
it('shows a peer response as a tool row with sender and expandable content', async () => {
 render(ToolUseMessage, { name: 'Agent message', input: { label: 'Reviewer' }, result: 'Review complete.', status: 'complete' });
 const row = screen.getByRole('button', { name: /Agent message: Reviewer/ });
 expect(screen.queryByText('Review complete.')).toBeNull();
 await fireEvent.click(row);
 expect(screen.getByText('Review complete.')).toBeTruthy();
});
