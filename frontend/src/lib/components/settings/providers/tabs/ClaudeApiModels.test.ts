import { afterEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	put: vi.fn(),
	hydrate: vi.fn().mockResolvedValue(true),
	notify: vi.fn(),
	flashSaved: vi.fn(),
	watchResources: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, put: mocks.put },
	getErrorMessage: (err: Error, fallback: string) => err?.message || fallback
}));
vi.mock('$lib/constants/models.svelte', () => ({
	hydrateModelRegistry: mocks.hydrate
}));
vi.mock('$lib/stores/notifications', () => ({
	notify: mocks.notify
}));
vi.mock('$lib/settings/save-status.svelte', () => ({
	flashSaved: mocks.flashSaved
}));
vi.mock('$lib/services/watch-resources', () => ({
	watchResources: mocks.watchResources
}));

import ClaudeApiModels from './ClaudeApiModels.svelte';

afterEach(() => {
	cleanup();
	vi.clearAllMocks();
});

it('reports a load error, then retries with refresh=true and renders Anthropic’s list with checkboxes', async () => {
	mocks.get
		.mockRejectedValueOnce(new Error('Anthropic did not accept this API key.'))
		.mockResolvedValueOnce({
			models: [{ id: 'claude-test', label: 'Claude Test' }],
			curated: null,
			fetched_at: '2026-11-20T00:00:00Z'
		});
	render(ClaudeApiModels, { props: { accountId: 'account/a' } });
	expect((await screen.findByRole('alert')).textContent).toBe('Anthropic did not accept this API key.');
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh model list' }));
	expect(await screen.findByText('Claude Test')).toBeTruthy();
	expect(screen.queryByRole('alert')).toBeNull();
	const checkbox = screen.getByRole('checkbox') as HTMLInputElement;
	expect(checkbox.checked).toBe(true);
	expect(mocks.get.mock.calls.map(([path]) => path)).toEqual([
		'/models/claude/api-key/account%2Fa',
		'/models/claude/api-key/account%2Fa?refresh=true'
	]);
});

it('clears an uncurated catalog if refreshing the same key is rejected, but keeps saved curation', async () => {
	mocks.get
		.mockResolvedValueOnce({
			models: [{ id: 'claude-test', label: 'Claude Test' }],
			curated: null,
			fetched_at: '2026-11-20T00:00:00Z'
		})
		.mockRejectedValueOnce(new Error('Anthropic did not accept this API key.'));
	render(ClaudeApiModels, { props: { accountId: 'selected' } });
	expect(await screen.findByText('Claude Test')).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh model list' }));
	expect((await screen.findByRole('alert')).textContent).toBe('Anthropic did not accept this API key.');
	expect(screen.queryByText('Claude Test')).toBeNull();

	cleanup();
	mocks.get
		.mockResolvedValueOnce({
			models: [
				{ id: 'claude-test', label: 'Claude Test' },
				{ id: 'claude-other', label: 'Claude Other' }
			],
			curated: [{ id: 'claude-test', label: 'Claude Test' }],
			fetched_at: '2026-11-20T00:00:00Z'
		})
		.mockRejectedValueOnce(new Error('Anthropic did not accept this API key.'));
	render(ClaudeApiModels, { props: { accountId: 'selected' } });
	expect(await screen.findByText('Claude Test')).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh model list' }));
	expect((await screen.findByRole('alert')).textContent).toBe('Anthropic did not accept this API key.');
	expect(screen.getByText('Claude Test')).toBeTruthy();
	expect(screen.queryByText('Claude Other')).toBeNull();
});

it('toggles curation, persists via PUT, refreshes registry, and rolls back on failure', async () => {
	mocks.get.mockResolvedValueOnce({
		models: [
			{ id: 'claude-sonnet-5', label: 'Sonnet 5' },
			{ id: 'claude-haiku-5-5', label: 'Haiku 5.5' }
		],
		curated: null,
		fetched_at: '2026-11-20T00:00:00Z'
	});
	mocks.put.mockResolvedValueOnce({ models: [{ id: 'claude-haiku-5-5', label: 'Haiku 5.5' }] });
	render(ClaudeApiModels, { props: { accountId: 'acct-1' } });
	const checkboxes = (await screen.findAllByRole('checkbox')) as HTMLInputElement[];
	expect(checkboxes).toHaveLength(2);
	expect(checkboxes.every((c) => c.checked)).toBe(true);

	await fireEvent.click(checkboxes[0]);
	await waitFor(() =>
		expect(mocks.put).toHaveBeenCalledWith('/models/claude/api-key/acct-1/models', {
			models: [{ id: 'claude-haiku-5-5', label: 'Haiku 5.5' }]
		})
	);
	expect(mocks.hydrate).toHaveBeenCalled();
	expect(mocks.flashSaved).toHaveBeenCalled();
	expect(checkboxes[0].checked).toBe(false);

	// Failure rolls back optimistic toggle and notifies.
	mocks.put.mockRejectedValueOnce(new Error('Save failed'));
	await fireEvent.click(checkboxes[1]);
	await waitFor(() => expect(mocks.notify).toHaveBeenCalledWith('Save failed', { severity: 'error' }));
	expect(checkboxes[1].checked).toBe(true);
});

it('filters models and supports clearing an empty filter match', async () => {
	mocks.get.mockResolvedValueOnce({
		models: [
			{ id: 'claude-sonnet-5', label: 'Sonnet 5' },
			{ id: 'claude-haiku-5-5', label: 'Haiku 5.5' }
		],
		curated: null,
		fetched_at: '2026-11-20T00:00:00Z'
	});
	render(ClaudeApiModels, { props: { accountId: 'acct-1' } });
	await screen.findByText('Sonnet 5');
	const search = screen.getByRole('searchbox', { name: 'Filter models' });
	await fireEvent.input(search, { target: { value: 'haiku' } });
	expect(screen.queryByText('Sonnet 5')).toBeNull();
	expect(screen.getByText('Haiku 5.5')).toBeTruthy();

	await fireEvent.input(search, { target: { value: 'does-not-exist' } });
	expect(screen.getByText(/No models match/)).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
	expect(screen.getByText('Sonnet 5')).toBeTruthy();
});

it('distinguishes an empty upstream list from loading and permits refresh after the response', async () => {
	let respond!: (result: { models: []; curated: null; fetched_at: null }) => void;
	mocks.get.mockReturnValueOnce(new Promise((resolve) => { respond = resolve; }));
	render(ClaudeApiModels, { props: { accountId: 'selected' } });
	const refresh = screen.getByRole<HTMLButtonElement>('button', { name: 'Refresh model list' });
	expect(screen.getByTestId('curation-fetching').textContent).toBe('Fetching list');
	expect(screen.queryByText('Anthropic did not list any models for this key.')).toBeNull();
	expect(refresh.disabled).toBe(true);
	respond({ models: [], curated: null, fetched_at: null });
	expect(await screen.findByText('Anthropic did not list any models for this key.')).toBeTruthy();
	await waitFor(() => expect(screen.queryByTestId('curation-fetching')).toBeNull());
	expect(refresh.disabled).toBe(false);
});

it('disables checkboxes and refresh button when readOnly is true', async () => {
	mocks.get.mockResolvedValueOnce({
		models: [{ id: 'claude-sonnet-5', label: 'Sonnet 5' }],
		curated: null,
		fetched_at: '2026-11-20T00:00:00Z'
	});
	render(ClaudeApiModels, { props: { accountId: 'acct-1', readOnly: true } });
	const checkbox = (await screen.findByRole('checkbox')) as HTMLInputElement;
	const refresh = screen.getByRole<HTMLButtonElement>('button', { name: 'Refresh model list' });
	expect(checkbox.disabled).toBe(true);
	expect(refresh.disabled).toBe(true);
});
