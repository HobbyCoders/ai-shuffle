import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const { get } = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: { get },
	getErrorMessage: (err: Error) => err.message
}));
import ClaudeApiModels from './ClaudeApiModels.svelte';

afterEach(() => {
	cleanup();
	get.mockReset();
});

it('reports a load error, then retries the same key and renders Anthropic’s list', async () => {
	get.mockRejectedValueOnce(new Error('Anthropic did not accept this API key.'))
		.mockResolvedValueOnce({ models: [{ id: 'claude-test', label: 'Claude Test' }] });
	render(ClaudeApiModels, { props: { accountId: 'account/a' } });
	expect((await screen.findByRole('alert')).textContent).toBe('Anthropic did not accept this API key.');
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh models' }));
	expect(await screen.findByText('Claude Test')).toBeTruthy();
	expect(screen.queryByRole('alert')).toBeNull();
	expect(get.mock.calls.map(([path]) => path)).toEqual(Array(2).fill('/models/claude/api-key/account%2Fa'));
});

it('distinguishes an empty upstream list from loading', async () => {
	get.mockResolvedValueOnce({ models: [] });
	render(ClaudeApiModels, { props: { accountId: 'selected' } });
	expect(await screen.findByText('Anthropic did not list any models for this key.')).toBeTruthy();
	await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
});
