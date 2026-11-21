/** Claude subscription > Models: curated checklist of every model Claude Code offers. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({
	get: vi.fn(),
	post: vi.fn(),
	put: vi.fn(),
	hydrate: vi.fn().mockResolvedValue(true),
	notify: vi.fn(),
	flashSaved: vi.fn(),
	watchResources: vi.fn()
}));

vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, post: mocks.post, put: mocks.put },
	getErrorMessage: (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback)
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

import ClaudeCliModels from './ClaudeCliModels.svelte';

const subscriptionResponse = {
	captured: true,
	cli_version: '2.1.325',
	captured_at: '2026-11-14T00:00:00Z',
	models: [
		{ id: 'haiku', slug: 'claude-haiku-5-5', label: 'Haiku 5.5', enabled: true },
		{ id: 'sonnet', slug: 'claude-sonnet-5-5', label: 'Sonnet 5.5', enabled: true },
		{ id: 'opus', slug: 'claude-opus-5-5', label: 'Opus 5.5', enabled: true },
		{ id: 'fable', slug: 'claude-fable-5-2', label: 'Fable 5.2', enabled: true },
		{ id: 'claude-mythos-5-1', slug: 'claude-mythos-5-1', label: 'Mythos 5.1', enabled: true },
		{ id: 'claude-opus-4-6', label: 'Opus 4.6', enabled: false }
	]
};

beforeEach(() => {
	mocks.get.mockResolvedValue(subscriptionResponse);
	mocks.post.mockResolvedValue({});
	mocks.put.mockResolvedValue({ disabled_models: ['claude-opus-4-6', 'claude-mythos-5-1'] });
});

afterEach(() => {
	cleanup();
	vi.clearAllMocks();
});

it('lists every model Claude Code offers with Claude artwork, checkboxes, and filter', async () => {
	render(ClaudeCliModels);
	const list = await screen.findByTestId('claude-cli-models');
	for (const label of ['Opus 5.5', 'Fable 5.2', 'Mythos 5.1', 'Opus 4.6']) {
		expect(within(list).getByText(label)).toBeTruthy();
	}
	expect(within(list).getByText('claude-mythos-5-1')).toBeTruthy();
	// Aliases show the model they run; a row without a slug (older worker) shows its id.
	for (const slug of ['claude-haiku-5-5', 'claude-sonnet-5-5', 'claude-opus-5-5', 'claude-fable-5-2', 'claude-opus-4-6']) {
		expect(within(list).getByText(slug)).toBeTruthy();
	}
	expect(within(list).queryByText('opus')).toBeNull();
	expect(list.querySelectorAll('.pv-mrow [data-provider-logo="claude"][aria-hidden="true"]')).toHaveLength(
		subscriptionResponse.models.length
	);
	const checkboxes = within(list).getAllByRole('checkbox') as HTMLInputElement[];
	expect(checkboxes).toHaveLength(subscriptionResponse.models.length);
	expect(checkboxes[0].checked).toBe(true);
	expect(checkboxes[5].checked).toBe(false);
	expect(screen.getByText('5 of 6')).toBeTruthy();
	expect(screen.queryByText(/Listed by Claude Code/)).toBeNull();

	const search = screen.getByRole('searchbox', { name: 'Filter models' });
	await fireEvent.input(search, { target: { value: 'mythos' } });
	expect(within(list).getAllByRole('checkbox')).toHaveLength(1);
	expect(within(list).getByText('Mythos 5.1')).toBeTruthy();

	await fireEvent.input(search, { target: { value: 'opus-5-5' } });
	expect(within(list).getAllByRole('checkbox')).toHaveLength(1);
	expect(within(list).getByText('claude-opus-5-5')).toBeTruthy();
	await fireEvent.input(search, { target: { value: 'missing' } });
	expect(screen.getByText(/No models match/)).toBeTruthy();
	await fireEvent.click(screen.getByRole('button', { name: 'Clear' }));
	expect(within(list).getAllByRole('checkbox')).toHaveLength(subscriptionResponse.models.length);
});

it('toggles a model via PUT, refreshes registry, and rolls back on failure', async () => {
	render(ClaudeCliModels);
	const list = await screen.findByTestId('claude-cli-models');
	const checkboxes = within(list).getAllByRole('checkbox') as HTMLInputElement[];

	await fireEvent.click(checkboxes[4]); // claude-mythos-5-1
	await waitFor(() =>
		expect(mocks.put).toHaveBeenCalledWith('/models/claude/subscription/models', {
			model: 'claude-mythos-5-1',
			enabled: false
		})
	);
	expect(mocks.hydrate).toHaveBeenCalled();
	expect(mocks.flashSaved).toHaveBeenCalled();
	expect(checkboxes[4].checked).toBe(false);

	mocks.put.mockRejectedValueOnce(new Error('Network error'));
	await fireEvent.click(checkboxes[0]); // haiku
	await waitFor(() => expect(mocks.notify).toHaveBeenCalledWith('Network error', { severity: 'error' }));
	expect(checkboxes[0].checked).toBe(true);
});

it('refresh asks the CLI again and reloads the subscription list', async () => {
	render(ClaudeCliModels);
	await screen.findByTestId('claude-cli-models');
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh model list' }));
	await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/models/claude/refresh', {}));
	await waitFor(() => expect(mocks.get).toHaveBeenCalledTimes(2));
	expect(mocks.hydrate).toHaveBeenCalled();
});

it('a viewer without settings access cannot refresh or toggle checkboxes', async () => {
	render(ClaudeCliModels, { props: { readOnly: true } });
	const list = await screen.findByTestId('claude-cli-models');
	expect((screen.getByRole('button', { name: 'Refresh model list' }) as HTMLButtonElement).disabled).toBe(true);
	for (const cb of within(list).getAllByRole('checkbox') as HTMLInputElement[]) {
		expect(cb.disabled).toBe(true);
	}
});

it('hides the refresh button when canRefresh is false', async () => {
	render(ClaudeCliModels, { props: { canRefresh: false } });
	await screen.findByTestId('claude-cli-models');
	expect(screen.queryByRole('button', { name: 'Refresh model list' })).toBeNull();
});
