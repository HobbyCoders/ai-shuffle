/** Claude subscription > Models: one list of every model Claude Code offers. */
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/svelte';

const mocks = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn() }));
vi.mock('$lib/api/client', () => ({
	api: { get: mocks.get, post: mocks.post },
	getErrorMessage: (e: unknown, fallback: string) => (e instanceof Error ? e.message : fallback)
}));
import ClaudeCliModels from './ClaudeCliModels.svelte';
import { hydrateModelRegistry } from '$lib/constants/models.svelte';

const payload = {
	default: 'sonnet',
	legacy_map: {},
	models: [
		{ value: 'haiku', label: 'Haiku 5.5', family: 'haiku' },
		{ value: 'sonnet', label: 'Sonnet 5.5', family: 'sonnet' },
		{ value: 'opus', label: 'Opus 5.5', family: 'opus' },
		{ value: 'fable', label: 'Fable 5.2', family: 'fable' },
		{ value: 'claude-mythos-5-1', label: 'Mythos 5.1', family: 'mythos' },
		{ value: 'claude-opus-4-6', label: 'Opus 4.6', family: 'opus' }
	],
	providers: [],
	claude: { auth_kind: 'subscription', plan_type: null, cli_models: { cli_version: '2.1.325', captured_at: '2026-11-14T00:00:00Z' } }
};

beforeEach(async () => {
	mocks.get.mockResolvedValue(payload);
	mocks.post.mockResolvedValue({});
	await hydrateModelRegistry();
});
afterEach(() => {
	cleanup();
	vi.clearAllMocks();
});

it('lists every model Claude Code offers with Claude artwork and no checkboxes', () => {
	render(ClaudeCliModels);
	const list = screen.getByTestId('claude-cli-models');
	for (const label of ['Opus 5.5', 'Fable 5.2', 'Mythos 5.1', 'Opus 4.6']) {
		expect(within(list).getByText(label)).toBeTruthy();
	}
	expect(within(list).getByText('claude-mythos-5-1')).toBeTruthy();
	expect(list.querySelectorAll('.pv-mrow [data-provider-logo="claude"][aria-hidden="true"]')).toHaveLength(payload.models.length);
	expect(screen.queryAllByRole('checkbox')).toHaveLength(0);
	expect(screen.queryByText(/Listed by Claude Code/)).toBeNull();
});

it('refresh asks the CLI again', async () => {
	render(ClaudeCliModels);
	await fireEvent.click(screen.getByRole('button', { name: 'Refresh model list' }));
	await waitFor(() => expect(mocks.post).toHaveBeenCalledWith('/models/claude/refresh', {}));
});

it('a viewer without settings access cannot refresh', () => {
	render(ClaudeCliModels, { props: { readOnly: true } });
	expect((screen.getByRole('button', { name: 'Refresh model list' }) as HTMLButtonElement).disabled).toBe(true);
});
