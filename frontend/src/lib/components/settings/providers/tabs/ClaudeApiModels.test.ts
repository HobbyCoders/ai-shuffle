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

it('clears the previous catalog if refreshing the same key is rejected', async () => {
	get.mockResolvedValueOnce({ models: [{ id: 'claude-test', label: 'Claude Test' }] })
		.mockRejectedValueOnce(new Error('Anthropic did not accept this API key.'));
	render(ClaudeApiModels, { props: { accountId: 'selected' } });
	expect(await screen.findByText('Claude Test')).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh models' }));
	expect((await screen.findByRole('alert')).textContent).toBe('Anthropic did not accept this API key.');
	expect(screen.queryByText('Claude Test')).toBeNull();
});

it('distinguishes an empty upstream list from loading and permits refresh after the response', async () => {
	let respond!: (result: { models: [] }) => void;
	get.mockReturnValueOnce(new Promise((resolve) => { respond = resolve; }));
	render(ClaudeApiModels, { props: { accountId: 'selected' } });
	const refresh = screen.getByRole<HTMLButtonElement>('button', { name: 'Refresh models' });
	expect(screen.getByRole('status').textContent).toBe('Loading Anthropic models…');
	expect(screen.queryByText('Anthropic did not list any models for this key.')).toBeNull();
	expect(refresh.disabled).toBe(true);
	respond({ models: [] });
	expect(await screen.findByText('Anthropic did not list any models for this key.')).toBeTruthy();
	await waitFor(() => expect(screen.queryByRole('status')).toBeNull());
	expect(refresh.disabled).toBe(false);
});
