/** Claude subscription > Models: aliases always on, CLI versions toggled and saved. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, put: mocks.put, post: mocks.post },
	getErrorMessage: (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback)
}));
import ClaudeCliModels from './ClaudeCliModels.svelte';
import { hydrateModelRegistry } from '$lib/constants/models.svelte';

const payload = (enabled: string[]) => ({
	default: 'sonnet',
	legacy_map: {},
	models: [
		{ value: 'haiku', label: 'Haiku 5.5', family: 'haiku' },
		{ value: 'sonnet', label: 'Sonnet 5.5', family: 'sonnet' },
		{ value: 'opus', label: 'Opus 5.5', family: 'opus' },
		{ value: 'fable', label: 'Fable 5.2', family: 'fable' }
	],
	versioned_models: [
		{ value: 'claude-opus-4-6', label: 'Opus 4.6', family: 'opus', enabled: enabled.includes('claude-opus-4-6') },
		{ value: 'claude-opus-5', label: 'Opus 5', family: 'opus', enabled: enabled.includes('claude-opus-5') }
	],
	providers: [],
	claude: { auth_kind: 'subscription', plan_type: null, cli_models: { cli_version: '2.1.325', captured_at: '2026-11-14T00:00:00Z' } }
});

beforeEach(async () => {
	mocks.get.mockResolvedValue(payload(['claude-opus-5']));
	mocks.put.mockResolvedValue({ models: [] });
	mocks.post.mockResolvedValue({});
	await hydrateModelRegistry();
});
afterEach(() => {
	cleanup();
	vi.clearAllMocks();
});

it('lists the aliases as the CLI names them and the versions with their saved state', () => {
	render(ClaudeCliModels);
	const aliases = screen.getByTestId('claude-alias-models');
	expect(within(aliases).getByText('Opus 5.5')).toBeTruthy();
	const versions = screen.getByTestId('claude-versioned-models');
	const boxes = within(versions).getAllByRole('checkbox') as HTMLInputElement[];
	expect(boxes.map((b) => b.checked)).toEqual([false, true]);
	expect(screen.getByText('Claude Code 2.1.325')).toBeTruthy();
});

it('saves only the id that was toggled, so ids this worker does not list survive', async () => {
	render(ClaudeCliModels);
	mocks.get.mockResolvedValue(payload(['claude-opus-4-6', 'claude-opus-5']));
	const versions = screen.getByTestId('claude-versioned-models');
	await fireEvent.click(within(versions).getAllByRole('checkbox')[0]);
	await waitFor(() =>
		expect(mocks.put).toHaveBeenCalledWith('/models/claude/enabled', {
			model: 'claude-opus-4-6',
			enabled: true
		})
	);
	await fireEvent.click(within(versions).getAllByRole('checkbox')[1]);
	await waitFor(() =>
		expect(mocks.put).toHaveBeenLastCalledWith('/models/claude/enabled', {
			model: 'claude-opus-5',
			enabled: false
		})
	);
});

it('refresh asks the CLI again', async () => {
	render(ClaudeCliModels);
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh model list' }));
	await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/models/claude/refresh', {}));
});

it('a viewer without settings access cannot toggle', () => {
	render(ClaudeCliModels, { props: { readOnly: true } });
	const boxes = within(screen.getByTestId('claude-versioned-models')).getAllByRole('checkbox') as HTMLInputElement[];
	expect(boxes.every((b) => b.disabled)).toBe(true);
});
